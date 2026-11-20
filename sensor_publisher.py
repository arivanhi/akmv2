#!/usr/bin/env python3
"""
sensor_publisher.py - Background Service Pembacaan Sensor Medis Kopersakti untuk AKMv2
1. Suhu Tubuh: MLX90614 (I2C 0x5A)
2. PPG Optik 3-in-1: MAX30102 (I2C 0x57) -> SpO2 (%), BPM, Tekanan Darah (SYS/DIA mmHg)
3. Multi-Biomarker Spektral: AS7265x Triad 18-Channel (I2C 0x49) -> Gula Darah, Kolesterol, Asam Urat

Peningkatan pada AKMv2:
- Thread-safe I2C mutex lock di seluruh sensor
- Deteksi dini sensor lepas (fail-fast dengan pesan jujur, tanpa nilai dummy/acak)
- Sinkronisasi penuh calibration_config.json (suhu, spo2, bpm, tekanan_darah, gula_darah, kolesterol, asam_urat)
"""

import os
import time
import json
import threading
import paho.mqtt.client as mqtt

try:
    import smbus2
    HAS_SMBUS = True
except ImportError:
    HAS_SMBUS = False

from drivers.driver_max30102 import MAX30102
from drivers.driver_as7265x_spektro import MultiBiomarkerModel

BROKER           = os.getenv("MQTT_BROKER_HOST", "localhost")
PORT             = int(os.getenv("MQTT_BROKER_PORT", "1883"))
I2C_BUS_NUM      = int(os.getenv("I2C_BUS_NUM", "1"))
SCAN_DURATION    = 5  # Suhu tubuh MLX90614 (5 detik @ 1 Hz)
PPG_DURATION     = 6  # Oximeter/Tensi MAX30102 (6 detik, ADC 100Hz / FIFO avg 4 = 25Hz -> ~150 sampel)
SPEKTRO_DURATION = 7  # Spektroskopi AS7265x (7 detik @ ~3.4 Hz kontinu -> ~22-25 frame x 18 kanal)
CALIBRATION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "calibration_config.json")

MLX_ADDR = 0x5A
TA_REG   = 0x06
TOBJ_REG = 0x07

I2C_LOCK       = threading.Lock()
suhu_active    = False
spo2_active    = False
spektro_active = False

max_sensor     = None
spektro_engine = None


def load_calibration():
    default_config = {
        "suhu": {"bias": 0.0},
        "spo2": {"bias": 0.0},
        "bpm": {"bias": 0.0},
        "tekanan_darah": {"sys_bias": 0.0, "dia_bias": 0.0},
        "gula_darah": {"gain": 1.0, "bias": 0.0},
        "kolesterol": {"gain": 1.0, "bias": 0.0},
        "asam_urat": {"gain": 1.0, "bias": 0.0}
    }
    if os.path.exists(CALIBRATION_FILE):
        try:
            with open(CALIBRATION_FILE, "r") as f:
                data = json.load(f)
                default_config.update(data)
                return default_config
        except Exception:
            pass
    return default_config


def init_hardware():
    global max_sensor, spektro_engine
    print("[INIT] Menginisialisasi sensor I2C Kopersakti (MLX90614, MAX30102, AS7265x)...")
    if not os.path.exists(f"/dev/i2c-{I2C_BUS_NUM}"):
        print(f"[INIT WARN] /dev/i2c-{I2C_BUS_NUM} tidak ditemukan pada host ini.")
        return

    try:
        max_sensor = MAX30102(bus_num=I2C_BUS_NUM, i2c_lock=I2C_LOCK)
    except Exception as e:
        print(f"[INIT WARN] MAX30102: {e}")

    try:
        spektro_engine = MultiBiomarkerModel(i2c_lock=I2C_LOCK)
    except Exception as e:
        print(f"[INIT WARN] Spektrometri AS7265x: {e}")


def read_mlx_temp(bus, reg):
    with I2C_LOCK:
        raw = bus.read_word_data(MLX_ADDR, reg)
    return round((raw * 0.02) - 273.15, 2)


# ── Thread 1: Suhu Tubuh (MLX90614, 5s Scan) ───────────
def suhu_scan_worker(client, target_mode="suhu"):
    global suhu_active
    print("[SUHU] Memulai scan MLX90614 (0x5A)...")

    if not HAS_SMBUS or not os.path.exists(f"/dev/i2c-{I2C_BUS_NUM}"):
        suhu_active = False
        client.publish("medical/suhu", json.dumps({
            "status": "error",
            "target_mode": target_mode,
            "message": f"Bus I2C (/dev/i2c-{I2C_BUS_NUM}) tidak tersedia pada perangkat."
        }))
        return

    bus = None
    try:
        bus = smbus2.SMBus(I2C_BUS_NUM)
        # Uji koneksi awal ke alamat 0x5A (fail-fast jika sensor lepas)
        try:
            _ = read_mlx_temp(bus, TOBJ_REG)
        except Exception as e:
            print(f"[SUHU ERROR] Sensor MLX90614 (0x5A) tidak merespons: {e}")
            suhu_active = False
            client.publish("medical/suhu", json.dumps({
                "status": "error",
                "target_mode": target_mode,
                "message": "Sensor Suhu MLX90614 (0x5A) tidak terdeteksi di jalur I2C. Periksa kabel konektor sensor."
            }))
            return

        calib = load_calibration()
        suhu_bias = float(calib.get("suhu", {}).get("bias", 0.0))

        samples = []
        t_ambient = 26.0
        start_time = time.time()

        while suhu_active:
            elapsed = time.time() - start_time
            remaining = max(0, int(round(SCAN_DURATION - elapsed)))
            progress = min(99, int(round((elapsed / SCAN_DURATION) * 100)))

            try:
                t_obj = read_mlx_temp(bus, TOBJ_REG)
                t_ambient = read_mlx_temp(bus, TA_REG)
                if -20.0 < t_obj < 100.0:
                    samples.append(t_obj)
            except Exception as e:
                print(f"[SUHU READ ERROR] {e}")

            client.publish("medical/suhu", json.dumps({
                "status": "scanning",
                "target_mode": target_mode,
                "objek": "--",
                "ambient": t_ambient,
                "remaining": remaining,
                "progress": progress
            }))

            if elapsed >= SCAN_DURATION:
                break
            time.sleep(1.0)

        if not suhu_active:
            return

        suhu_active = False
        if not samples:
            client.publish("medical/suhu", json.dumps({
                "status": "error",
                "target_mode": target_mode,
                "message": "Gagal membaca sampel suhu dari sensor MLX90614."
            }))
            return

        max_s = max(samples)
        if max_s > 30.0:
            valid_samples = [s for s in samples if s >= max_s - 1.0]
            final_obj = round((sum(valid_samples) / len(valid_samples)) + suhu_bias, 1)
        else:
            final_obj = round(samples[-1] + suhu_bias, 1)

        print(f"[SUHU SELESAI] Hasil Terkunci: {final_obj} °C (Ambient: {t_ambient} °C)")
        client.publish("medical/suhu", json.dumps({
            "status": "done",
            "target_mode": target_mode,
            "objek": final_obj,
            "ambient": t_ambient,
            "samples": samples,
            "remaining": 0,
            "progress": 100
        }))

    finally:
        suhu_active = False
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass


# ── Thread 2: SpO2, BPM & Tekanan Darah (MAX30102, 5s Scan) ──
def spo2_scan_worker(client, target_mode="ppg_all"):
    global spo2_active, max_sensor
    print(f"[MAX30102] Memulai scan 5 detik (mode={target_mode})...")

    if not HAS_SMBUS or not os.path.exists(f"/dev/i2c-{I2C_BUS_NUM}"):
        spo2_active = False
        client.publish("medical/spo2", json.dumps({
            "status": "error",
            "target_mode": target_mode,
            "message": f"Bus I2C (/dev/i2c-{I2C_BUS_NUM}) tidak tersedia pada perangkat."
        }))
        return

    if max_sensor is None:
        max_sensor = MAX30102(bus_num=I2C_BUS_NUM, i2c_lock=I2C_LOCK)

    if not max_sensor.enable_leds():
        spo2_active = False
        client.publish("medical/spo2", json.dumps({
            "status": "error",
            "target_mode": target_mode,
            "message": "Sensor Oximeter/Tensi MAX30102 (0x57) tidak terdeteksi di jalur I2C. Periksa kabel konektor sensor."
        }))
        return

    max_sensor.clear_fifo()
    calib = load_calibration()
    bp_calib = calib.get("tekanan_darah", {})
    sys_bias  = float(bp_calib.get("sys_bias", 0.0))
    dia_bias  = float(bp_calib.get("dia_bias", 0.0))
    spo2_bias = float(calib.get("spo2", {}).get("bias", 0.0))
    bpm_bias  = float(calib.get("bpm", {}).get("bias", 0.0))

    red_samples = []
    ir_samples  = []
    start_time  = time.time()
    last_pub    = 0

    try:
        while spo2_active:
            now = time.time()
            elapsed = now - start_time
            remaining = max(0, int(round(PPG_DURATION - elapsed)))
            progress = min(99, int(round((elapsed / PPG_DURATION) * 100)))

            batch = max_sensor.read_fifo()
            for r, ir in batch:
                red_samples.append(r)
                ir_samples.append(ir)

            if now - last_pub >= 0.8:
                last_pub = now
                client.publish("medical/spo2", json.dumps({
                    "status": "scanning",
                    "target_mode": target_mode,
                    "remaining": remaining,
                    "progress": progress
                }))

            if elapsed >= PPG_DURATION:
                break
            time.sleep(0.06)

        max_sensor.disable_leds()
        if not spo2_active:
            return
        spo2_active = False

        if not red_samples or not ir_samples:
            client.publish("medical/spo2", json.dumps({
                "status": "error",
                "target_mode": target_mode,
                "message": "Tidak ada sampel data dari sensor MAX30102. Periksa sambungan sensor."
            }))
            return

        spo2_val, bpm_val, s_val, d_val, status_code = max_sensor.compute_metrics(
            red_samples, ir_samples,
            sys_bias=sys_bias, dia_bias=dia_bias,
            spo2_bias=spo2_bias, bpm_bias=bpm_bias
        )

        if status_code != "ok" or spo2_val is None or bpm_val is None:
            err_msg = (
                "Jari belum terdeteksi di atas sensor Oximeter MAX30102. Pastikan ujung jari menutupi cahaya merah sensor."
                if status_code == "no_finger"
                else "Gelombang denyut nadi belum stabil. Letakkan jari dengan rileks tanpa bergerak selama pemindaian."
            )
            print(f"[MAX30102] Gagal ({status_code}): {err_msg}")
            client.publish("medical/spo2", json.dumps({
                "status": "error",
                "target_mode": target_mode,
                "message": err_msg
            }))
            return

        actual_ppg_dur = max(0.5, time.time() - start_time)
        ppg_hz = round(len(ir_samples) / actual_ppg_dur, 2)
        dc_red = round(float(sum(red_samples) / max(1, len(red_samples))), 1)
        dc_ir  = round(float(sum(ir_samples) / max(1, len(ir_samples))), 1)

        print(f"[MAX30102 SELESAI] SpO2={spo2_val}%, BPM={bpm_val}, Tensi={s_val}/{d_val} mmHg ({len(ir_samples)} sampel @ {ppg_hz} Hz)")
        client.publish("medical/spo2", json.dumps({
            "status": "done",
            "target_mode": target_mode,
            "spo2": spo2_val,
            "bpm": bpm_val,
            "sys": s_val,
            "dia": d_val,
            "tekanan": f"{s_val}/{d_val}",
            "raw_ppg": {
                "n_samples": len(ir_samples),
                "sampling_hz": ppg_hz,
                "hardware_adc_hz": 100.0,
                "fifo_averaging": 4,
                "duration_sec": round(actual_ppg_dur, 2),
                "dc_red": dc_red,
                "dc_ir": dc_ir,
                "red_waveform": [int(x) for x in red_samples],
                "ir_waveform": [int(x) for x in ir_samples]
            },
            "remaining": 0,
            "progress": 100
        }))

    except Exception as e:
        print(f"[MAX30102 ERROR] {e}")
        max_sensor.disable_leds()
        spo2_active = False
        client.publish("medical/spo2", json.dumps({
            "status": "error",
            "target_mode": target_mode,
            "message": f"Kesalahan pembacaan MAX30102: {e}"
        }))


# ── Thread 3: Spektroskopi 18-Channel (Gula, Kolesterol, Asam Urat, 7s Optimal Scan) ──
def spektro_scan_worker(client, target_mode="spektro_all"):
    global spektro_active, spektro_engine
    print(f"[SPEKTRO] Memulai scan optimal {SPEKTRO_DURATION} detik AS7265x (~3.4 Hz kontinu, mode={target_mode})...")

    if not os.path.exists(f"/dev/i2c-{I2C_BUS_NUM}"):
        spektro_active = False
        client.publish("medical/spektro", json.dumps({
            "status": "error",
            "target_mode": target_mode,
            "message": f"Bus I2C (/dev/i2c-{I2C_BUS_NUM}) tidak tersedia pada perangkat."
        }))
        return

    if spektro_engine is None:
        spektro_engine = MultiBiomarkerModel(i2c_lock=I2C_LOCK)

    spektro_engine.load_or_create_calibration()
    spektro_engine.reload_ml_models()
    if not spektro_engine.enable_leds():
        spektro_active = False
        client.publish("medical/spektro", json.dumps({
            "status": "error",
            "target_mode": target_mode,
            "message": "Sensor Spektrometri AS7265x (0x49) tidak terdeteksi di jalur I2C. Periksa kabel konektor sensor."
        }))
        return

    samples_18 = []
    start_time = time.time()
    last_pub   = 0

    try:
        while spektro_active:
            now = time.time()
            elapsed = now - start_time
            remaining = max(0, int(round(SPEKTRO_DURATION - elapsed)))
            progress = min(99, int(round((elapsed / SPEKTRO_DURATION) * 100)))

            ch18 = spektro_engine.read_18_channels()
            if ch18 is not None:
                samples_18.append(ch18)

            now_after = time.time()
            if now_after - last_pub >= 0.6:
                last_pub = now_after
                client.publish("medical/spektro", json.dumps({
                    "status": "scanning",
                    "target_mode": target_mode,
                    "frames_collected": len(samples_18),
                    "remaining": max(0, int(round(SPEKTRO_DURATION - (now_after - start_time)))),
                    "progress": min(99, int(round(((now_after - start_time) / SPEKTRO_DURATION) * 100)))
                }))

            if (now_after - start_time) >= SPEKTRO_DURATION:
                break
            # Jeda tipis 15ms agar bus I2C responsif sementara sampling AS7265x mencapai ~3.3-3.5 Hz penuh
            time.sleep(0.015)

        actual_duration = max(0.5, time.time() - start_time)
        spektro_engine.disable_leds()
        if not spektro_active:
            return
        spektro_active = False

        if not samples_18:
            client.publish("medical/spektro", json.dumps({
                "status": "error",
                "target_mode": target_mode,
                "message": "Gagal membaca 18 kanal dari sensor AS7265x. Periksa sambungan I2C."
            }))
            return

        res = spektro_engine.predict_biomarkers(samples_18, duration_sec=actual_duration)
        if not res or not res.get("finger_detected", False):
            print(f"[SPEKTRO] Jari tidak terdeteksi pada sensor AS7265x ({len(samples_18)} frame dibaca).")
            client.publish("medical/spektro", json.dumps({
                "status": "error",
                "target_mode": target_mode,
                "message": "Jari tidak terdeteksi atau kurang menutup rapat jendela sensor AS7265x. Pastikan ujung jari menutup jendela optik selama pemindaian."
            }))
            return

        print(
            f"[SPEKTRO SELESAI] Gula={res['gula']}, Kolesterol={res['kolesterol']}, Asam Urat={res['asam_urat']} "
            f"| Valid={res['valid_frames']}/{res['total_frames']} frame @ {res['sampling_hz']} Hz"
        )
        client.publish("medical/spektro", json.dumps({
            "status": "done",
            "target_mode": target_mode,
            "gula": res["gula"],
            "kolesterol": res["kolesterol"],
            "asam_urat": res["asam_urat"],
            "raw_18ch": res.get("raw_avg", []),
            "raw_meta": {
                "sampling_hz": res.get("sampling_hz", 0.0),
                "duration_sec": round(actual_duration, 2),
                "total_frames": res.get("total_frames", len(samples_18)),
                "valid_frames": res.get("valid_frames", len(samples_18)),
                "discarded_settling_frames": res.get("discarded_frames", 0),
                "integration_cycles": 49,
                "gain": "64x",
                "std_18ch": res.get("raw_std", []),
                "l2_norm_18ch": res.get("l2_norm", []),
                "channels_used": res.get("channels_used", {}),
                "model_source": res.get("model_source", {}),
                "frames": res.get("all_frames", [])
            },
            "remaining": 0,
            "progress": 100
        }))

    except Exception as e:
        print(f"[SPEKTRO ERROR] {e}")
        spektro_engine.disable_leds()
        spektro_active = False
        client.publish("medical/spektro", json.dumps({
            "status": "error",
            "target_mode": target_mode,
            "message": f"Kesalahan pembacaan AS7265x: {e}"
        }))


# ── MQTT Callbacks ────────────────────────────────────
def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[MQTT SENSOR] Terhubung ke broker {BROKER}:{PORT}, rc={reason_code}")
    client.subscribe("medical/cmd/#")


def on_message(client, userdata, msg):
    global suhu_active, spo2_active, spektro_active
    topic = msg.topic
    raw_payload = msg.payload.decode("utf-8", errors="ignore").strip()
    cmd = raw_payload.lower()
    target_mode = topic.split("/")[-1]

    # Mendukung payload JSON {"cmd": "start", "target_mode": "..."} maupun string "start"
    if raw_payload.startswith("{"):
        try:
            parsed = json.loads(raw_payload)
            cmd = str(parsed.get("cmd", "start")).lower()
            target_mode = parsed.get("target_mode", target_mode)
        except Exception:
            pass

    print(f"[MQTT CMD] {topic} -> cmd={cmd}, target_mode={target_mode}")

    if topic == "medical/cmd/reinit":
        threading.Thread(target=init_hardware, daemon=True).start()
        return

    if topic == "medical/cmd/stop_all" or cmd == "stop":
        suhu_active = False
        spo2_active = False
        spektro_active = False
        if max_sensor is not None:
            max_sensor.disable_leds()
        if spektro_engine is not None:
            spektro_engine.disable_leds()
        return

    if topic == "medical/cmd/suhu" and cmd == "start":
        if not suhu_active:
            suhu_active = True
            threading.Thread(target=suhu_scan_worker, args=(client, "suhu"), daemon=True).start()

    elif topic in ["medical/cmd/spo2", "medical/cmd/bpm", "medical/cmd/tekanan", "medical/cmd/tensi", "medical/cmd/ppg_all"] and cmd == "start":
        if not spo2_active:
            spo2_active = True
            threading.Thread(target=spo2_scan_worker, args=(client, target_mode), daemon=True).start()

    elif topic in ["medical/cmd/spektro", "medical/cmd/spektro_all", "medical/cmd/gula", "medical/cmd/gula_darah", "medical/cmd/kolesterol", "medical/cmd/cholesterol", "medical/cmd/asam_urat"] and cmd == "start":
        if not spektro_active:
            spektro_active = True
            threading.Thread(target=spektro_scan_worker, args=(client, target_mode), daemon=True).start()


if __name__ == "__main__":
    print("=" * 70)
    print("  AKMv2 SENSOR PUBLISHER (Kopersakti I2C Engine)")
    print("  Sensors: MLX90614 (0x5A), MAX30102 (0x57), AS7265x 18-CH (0x49)")
    print("=" * 70)

    init_hardware()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    client.on_connect = on_connect
    client.on_message = on_message

    while True:
        try:
            client.connect(BROKER, PORT, 60)
            client.loop_forever()
        except Exception as e:
            print(f"[MQTT SENSOR] Menunggu broker MQTT ({BROKER}:{PORT}): {e}")
            time.sleep(3)
