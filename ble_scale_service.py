#!/usr/bin/env python3
"""
ble_scale_service.py - Layanan BLE Terpadu MEDISYN (AKMv2)
1. Tensimeter BLE Omron HEM-7156T (Service 0x1810, Characteristic 0x2A35 Blood Pressure Measurement)
   - Mendukung Pairing & Bonding otomatis (-P-) serta akuisisi tekanan darah (SYS, DIA, MAP, Pulse)
   - Menggunakan single-radio adapter lock (hci0) agar tidak bentrok dengan pemindaian BLE lain di Raspberry Pi
2. Timbangan Berat Badan & Sensor Tinggi Badan BLE Opsional (akm/command '5' & '6')
"""

import os
import time
import json
import struct
import asyncio
import subprocess
from datetime import datetime
import paho.mqtt.client as mqtt

try:
    from bleak import BleakClient, BleakScanner
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

BASE_DIR              = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_FILE      = os.path.join(BASE_DIR, "calibration_config.json")

MQTT_BROKER           = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_PORT             = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_CMD_TOPIC        = "akm/command"
MQTT_OMRON_CMD_TOPIC  = "medical/cmd/omron"
MQTT_STOP_TOPIC       = "medical/cmd/stop_all"
MQTT_OMRON_DATA_TOPIC = "medical/omron"
MQTT_PREDICTION_TOPIC = "akm/prediction_data"
MQTT_STATUS_TOPIC     = "akm/connect"

# Tensimeter Omron HEM-7156T Standard BLE Blood Pressure UUIDs
DEFAULT_OMRON_MAC     = os.getenv("BLE_OMRON_MAC", "00:5F:BF:E2:25:DC").upper()
DEFAULT_OMRON_NAME    = "BLEsmart_0000057A005FBFE225DC"
UUID_BP_SERVICE       = "00001810-0000-1000-8000-00805f9b34fb"
UUID_BP_MEASUREMENT   = "00002a35-0000-1000-8000-00805f9b34fb"
UUID_CURRENT_TIME     = "00002a2b-0000-1000-8000-00805f9b34fb"
UUID_BATTERY_LEVEL    = "00002a19-0000-1000-8000-00805f9b34fb"
UUID_RACP             = "00002a52-0000-1000-8000-00805f9b34fb"
OMRON_COMPANY_ID      = 0x020E  # 526 (Omron Healthcare Co., Ltd.)

# Timbangan & Tinggi Badan BLE
WEIGHT_ADDRESS        = os.getenv("BLE_WEIGHT_MAC", "d8:e7:2f:0a:94:44")
WEIGHT_UUID           = "00002a9c-0000-1000-8000-00805f9b34fb"
HEIGHT_NAME           = os.getenv("BLE_HEIGHT_NAME", "bleScaleHeight")
HEIGHT_CHAR_UUID      = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

mqtt_client           = None
main_loop             = None
ble_adapter_lock      = None

omron_busy            = False
omron_cancel_requested = False

weight_state = {
    "latest": 0.0,
    "stable": False,
    "connected": False,
    "capture_request": False
}

height_state = {
    "latest": 0.0,
    "connected": False,
    "capture_request": False
}


def load_omron_config():
    cfg = {
        "mac": DEFAULT_OMRON_MAC,
        "name": DEFAULT_OMRON_NAME,
        "model": "HEM-7156T"
    }
    if os.path.exists(CALIBRATION_FILE):
        try:
            with open(CALIBRATION_FILE, "r") as f:
                data = json.load(f)
                om = data.get("omron")
                if isinstance(om, dict):
                    if om.get("mac"):
                        cfg["mac"] = str(om["mac"]).strip().upper()
                    if om.get("name"):
                        cfg["name"] = str(om["name"]).strip()
                    if om.get("model"):
                        cfg["model"] = str(om["model"]).strip()
        except Exception:
            pass
    return cfg


def save_omron_config(mac, name=None):
    try:
        data = {}
        if os.path.exists(CALIBRATION_FILE):
            with open(CALIBRATION_FILE, "r") as f:
                data = json.load(f)
        om = data.get("omron") if isinstance(data.get("omron"), dict) else {}
        om["mac"] = str(mac).strip().upper()
        if name:
            om["name"] = str(name).strip()
        om["model"] = om.get("model") or "HEM-7156T"
        om["paired_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        data["omron"] = om
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        print(f"[OMRON CFG WARN] Gagal menyimpan konfigurasi Omron: {e}")


def decode_ieee11073_sfloat(data_2bytes: bytes) -> float:
    """Menguraikan format IEEE-11073 16-bit SFLOAT (little-endian) dari karakteristik 0x2A35."""
    raw = struct.unpack("<H", data_2bytes)[0]
    mantissa = raw & 0x0FFF
    exponent = (raw >> 12) & 0x0F
    if exponent >= 0x08:
        exponent = -((0x0F + 1) - exponent)
    if mantissa >= 0x0800:
        mantissa = -((0x0FFF + 1) - mantissa)
    return float(mantissa * (10 ** exponent))


def is_omron_ble_device(device, adv_data, target_mac=None) -> bool:
    """Mendeteksi apakah paket iklan BLE berasal dari Tensimeter Omron HEM-7156T / BLEsmart."""
    addr = (device.address or "").upper()
    name = (device.name or (adv_data.local_name if adv_data else "") or "").upper()

    if target_mac and addr == target_mac.upper():
        return True
    if "BLESMART" in name or "OMRON" in name or "HEM-" in name or "HEM7156" in name:
        return True
    if addr.startswith("00:5F:BF") or addr.startswith("28:FF:B2"):
        return True
    if adv_data:
        uuids = [u.lower() for u in (adv_data.service_uuids or [])]
        if UUID_BP_SERVICE in uuids:
            return True
        if OMRON_COMPANY_ID in (adv_data.manufacturer_data or {}):
            return True
    return False


class OmronMeasurementCollector:
    def __init__(self):
        self.records = []
        self.raw_packets = []
        self.last_packet_ts = 0.0
        self._seq = 0

    def notification_handler(self, sender, data: bytearray):
        self.last_packet_ts = time.time()
        raw_hex = bytes(data).hex()
        self.raw_packets.append(raw_hex)
        print(f"[OMRON 0x2A35] Paket masuk ({len(data)} bytes): {raw_hex}")

        if len(data) < 7:
            return

        try:
            flags = data[0]
            unit_is_kpa = bool(flags & 0x01)
            unit = "kPa" if unit_is_kpa else "mmHg"
            has_timestamp = bool(flags & 0x02)
            has_pulse = bool(flags & 0x04)
            has_user_id = bool(flags & 0x08)
            has_status = bool(flags & 0x10)

            idx = 1
            sys_val = decode_ieee11073_sfloat(data[idx:idx + 2])
            dia_val = decode_ieee11073_sfloat(data[idx + 2:idx + 4])
            map_val = decode_ieee11073_sfloat(data[idx + 4:idx + 6])
            idx += 6

            if unit_is_kpa:
                sys_val *= 7.50062
                dia_val *= 7.50062
                map_val *= 7.50062

            dt_obj = datetime(2000, 1, 1, 0, 0, 0)
            ts_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            if has_timestamp and len(data) >= idx + 7:
                year = struct.unpack("<H", data[idx:idx + 2])[0]
                month = data[idx + 2]
                day = data[idx + 3]
                hour = data[idx + 4]
                minute = data[idx + 5]
                second = min(data[idx + 6], 59)
                idx += 7
                try:
                    if 2015 <= year <= 2100 and 1 <= month <= 12 and 1 <= day <= 31:
                        dt_obj = datetime(year, month, day, hour, minute, second)
                        ts_str = dt_obj.strftime("%Y-%m-%d %H:%M:%S")
                except Exception:
                    pass

            pulse_val = 0.0
            if has_pulse and len(data) >= idx + 2:
                pulse_val = decode_ieee11073_sfloat(data[idx:idx + 2])
                idx += 2

            user_id = 1
            if has_user_id and len(data) >= idx + 1:
                user_id = int(data[idx])
                idx += 1

            status_flags = 0
            if has_status and len(data) >= idx + 2:
                status_flags = struct.unpack("<H", data[idx:idx + 2])[0]

            sys_int = int(round(sys_val))
            dia_int = int(round(dia_val))
            map_int = int(round(map_val))
            pulse_int = int(round(pulse_val))

            if 30 <= sys_int <= 300 and 20 <= dia_int <= 220:
                self._seq += 1
                rec = {
                    "seq": self._seq,
                    "datetime": dt_obj,
                    "measured_at": ts_str,
                    "sys": sys_int,
                    "dia": dia_int,
                    "map": map_int,
                    "pulse": pulse_int,
                    "unit": "mmHg",
                    "user_id": user_id,
                    "status_flags": status_flags,
                    "raw_hex": raw_hex
                }
                self.records.append(rec)
                print(f"[OMRON DECODED] #{self._seq}: {sys_int}/{dia_int} mmHg, Pulse={pulse_int} bpm ({ts_str})")
        except Exception as e:
            print(f"[OMRON PARSE ERR] {e}")


def publish_omron_event(payload: dict):
    if mqtt_client:
        mqtt_client.publish(MQTT_OMRON_DATA_TOPIC, json.dumps(payload))


def publish_measurement(tipe, nilai):
    if mqtt_client:
        mqtt_client.publish(MQTT_PREDICTION_TOPIC, json.dumps({
            "type": tipe,
            "status": "done",
            "value": round(float(nilai), 2)
        }))
        print(f"[BLE SUCCESS] {tipe}: {round(float(nilai), 2)}")


def publish_ble_error(tipe, message):
    if mqtt_client:
        mqtt_client.publish(MQTT_PREDICTION_TOPIC, json.dumps({
            "type": tipe,
            "status": "error",
            "message": message
        }))


async def sync_omron_current_time(client: BleakClient):
    """Menyinkronkan jam Raspberry Pi ke Current Time Service (0x1805 / 0x2A2B) Omron jika tersedia."""
    try:
        char = client.services.get_characteristic(UUID_CURRENT_TIME)
        if char and "write" in char.properties:
            now = datetime.now()
            ct_bytes = struct.pack(
                "<HBBBBBBBB",
                now.year, now.month, now.day,
                now.hour, now.minute, now.second,
                now.isoweekday(), 0, 1
            )
            await client.write_gatt_char(UUID_CURRENT_TIME, ct_bytes, response=True)
            print(f"[OMRON TIME] Jam tensimeter disinkronkan ke {now.strftime('%Y-%m-%d %H:%M:%S')}")
    except Exception:
        pass


async def handle_omron_pair_task(requested_mac=None):
    """
    Memindai dan melakukan Pairing + Bonding + Trust + CCCD Subscription (0x2A35)
    dengan Tensimeter Omron HEM-7156T saat tensimeter dalam mode pairing (-P-).
    """
    global omron_busy, omron_cancel_requested
    if not HAS_BLEAK:
        publish_omron_event({
            "status": "pair_error",
            "message": "Library Bleak BLE tidak tersedia pada container."
        })
        return

    cfg = load_omron_config()
    target_mac = (requested_mac or cfg.get("mac") or DEFAULT_OMRON_MAC).strip().upper()

    async with ble_adapter_lock:
        omron_busy = True
        omron_cancel_requested = False
        try:
            publish_omron_event({
                "status": "pairing_progress",
                "message": "Memindai Tensimeter Omron HEM-7156T... Tahan tombol Bluetooth di tensimeter ±3 detik hingga muncul '-P-' berkedip."
            })

            found_holder = [None, None]
            found_event = asyncio.Event()

            def _on_detect(device, adv_data):
                if not found_holder[0] and is_omron_ble_device(device, adv_data, target_mac=target_mac if requested_mac else None):
                    found_holder[0] = device
                    found_holder[1] = adv_data
                    found_event.set()

            scanner = BleakScanner(detection_callback=_on_detect)
            await scanner.start()
            try:
                for sec in range(35, 0, -1):
                    if omron_cancel_requested:
                        await scanner.stop()
                        publish_omron_event({"status": "pair_error", "message": "Pairing dibatalkan."})
                        return
                    if found_event.is_set():
                        break
                    if sec % 5 == 0:
                        publish_omron_event({
                            "status": "pairing_progress",
                            "message": f"Menunggu sinyal '-P-' dari Omron HEM-7156T... (Sisa {sec} dtk)"
                        })
                    await asyncio.sleep(1.0)
            finally:
                try:
                    await scanner.stop()
                except Exception:
                    pass

            if not found_holder[0]:
                publish_omron_event({
                    "status": "pair_error",
                    "message": "Tensimeter Omron tidak terdeteksi. Pastikan tombol Bluetooth di tensimeter ditekan tahan hingga muncul '-P-' berkedip."
                })
                return

            dev = found_holder[0]
            adv = found_holder[1]
            dev_mac = dev.address.upper()
            dev_name = dev.name or (adv.local_name if adv else None) or cfg.get("name") or DEFAULT_OMRON_NAME

            publish_omron_event({
                "status": "pairing_progress",
                "message": f"Ditemukan {dev_name} ({dev_mac}). Menghubungkan & melakukan Bonding BLE..."
            })

            collector = OmronMeasurementCollector()
            async with BleakClient(dev, timeout=18.0) as client:
                try:
                    await client.pair()
                    print(f"[OMRON PAIR] Bleak pair() berhasil untuk {dev_mac}")
                except Exception as e:
                    print(f"[OMRON PAIR INFO] pair(): {e}")

                subprocess.run(["bluetoothctl", "trust", dev_mac], capture_output=True, timeout=5)

                # Sinkronkan jam & aktifkan indikasi 0x2A35 agar tensimeter menampilkan simbol [□] (Pairing Selesai)
                await sync_omron_current_time(client)
                try:
                    await client.start_notify(UUID_BP_MEASUREMENT, collector.notification_handler)
                    await asyncio.sleep(3.5)
                    try:
                        await client.stop_notify(UUID_BP_MEASUREMENT)
                    except Exception:
                        pass
                except Exception as e:
                    print(f"[OMRON PAIR NOTIFY INFO] {e}")

            save_omron_config(dev_mac, dev_name)
            publish_omron_event({
                "status": "paired",
                "mac": dev_mac,
                "name": dev_name,
                "message": f"Tensimeter {dev_name} ({dev_mac}) berhasil di-pair & di-trust! Siap digunakan untuk mengukur Tekanan Darah."
            })

            # Jika saat pairing ada data pengukuran yang ikut terkirim, teruskan juga
            if collector.records:
                latest = sorted(collector.records, key=lambda r: (r["datetime"], r["seq"]))[-1]
                publish_omron_event({
                    "status": "done",
                    "target_mode": "tensi_omron",
                    "sys": latest["sys"],
                    "dia": latest["dia"],
                    "pulse": latest["pulse"],
                    "map": latest["map"],
                    "mac": dev_mac,
                    "name": dev_name,
                    "measured_at": latest["measured_at"],
                    "raw_meta": {
                        "packets_count": len(collector.records),
                        "raw_packets": collector.raw_packets,
                        "status_flags": latest["status_flags"]
                    }
                })

        except Exception as e:
            print(f"[OMRON PAIR ERR] {e}")
            publish_omron_event({
                "status": "pair_error",
                "message": f"Gagal melakukan pairing ke Omron: {e}. Pastikan layar tensimeter menampilkan '-P-' lalu coba lagi."
            })
        finally:
            omron_busy = False


async def handle_omron_measure_task():
    """
    Menunggu sinyal BLE dari Tensimeter Omron HEM-7156T (hingga 90 detik agar cukup
    untuk proses pemompaan manset dari awal maupun transfer langsung), menghubungkan,
    dan membaca karakteristik 0x2A35 (Blood Pressure Measurement).
    """
    global omron_busy, omron_cancel_requested
    if not HAS_BLEAK:
        publish_omron_event({
            "status": "error",
            "target_mode": "tensi_omron",
            "message": "Library Bleak BLE tidak tersedia pada perangkat."
        })
        return

    cfg = load_omron_config()
    target_mac = (cfg.get("mac") or DEFAULT_OMRON_MAC).strip().upper()
    total_wait_sec = 90

    async with ble_adapter_lock:
        omron_busy = True
        omron_cancel_requested = False
        try:
            start_ts = time.time()
            attempt_count = 0

            while True:
                elapsed = int(time.time() - start_ts)
                remaining = max(0, total_wait_sec - elapsed)
                if omron_cancel_requested:
                    print("[OMRON] Pengukuran dibatalkan oleh pengguna.")
                    return

                if remaining <= 0:
                    publish_omron_event({
                        "status": "error",
                        "target_mode": "tensi_omron",
                        "message": (
                            f"Waktu tunggu Tensimeter Omron ({target_mac}) habis (90 detik). "
                            "Pastikan tensimeter dinyalakan dan pengukuran telah selesai, atau tekan tombol Bluetooth pada tensimeter."
                        )
                    })
                    return

                # 1. Scan aktif menunggu paket iklan BLE dari Omron
                found_holder = [None, None]
                found_event = asyncio.Event()

                def _on_detect(device, adv_data):
                    if not found_holder[0] and is_omron_ble_device(device, adv_data, target_mac=target_mac):
                        found_holder[0] = device
                        found_holder[1] = adv_data
                        found_event.set()

                scanner = BleakScanner(detection_callback=_on_detect)
                await scanner.start()
                try:
                    while not found_event.is_set():
                        if omron_cancel_requested:
                            await scanner.stop()
                            return
                        elapsed = int(time.time() - start_ts)
                        remaining = max(0, total_wait_sec - elapsed)
                        if remaining <= 0:
                            break
                        pct = min(80, int((elapsed / float(total_wait_sec)) * 80))
                        publish_omron_event({
                            "status": "scanning",
                            "target_mode": "tensi_omron",
                            "remaining": remaining,
                            "progress": pct,
                            "stage": "waiting_adv",
                            "message": (
                                f"Menunggu sinyal Tensimeter Omron HEM-7156T ({target_mac})... "
                                "Silakan tekan START/STOP untuk mengukur atau tekan tombol Bluetooth pada tensimeter."
                            )
                        })
                        try:
                            await asyncio.wait_for(found_event.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                finally:
                    try:
                        await scanner.stop()
                    except Exception:
                        pass

                if omron_cancel_requested:
                    return
                if not found_holder[0]:
                    continue

                dev = found_holder[0]
                adv = found_holder[1]
                dev_mac = dev.address.upper()
                dev_name = dev.name or (adv.local_name if adv else None) or cfg.get("name") or DEFAULT_OMRON_NAME
                attempt_count += 1

                elapsed = int(time.time() - start_ts)
                remaining = max(1, total_wait_sec - elapsed)
                publish_omron_event({
                    "status": "scanning",
                    "target_mode": "tensi_omron",
                    "remaining": remaining,
                    "progress": 88,
                    "stage": "connecting",
                    "message": f"Sinyal {dev_name} ({dev_mac}) terdeteksi! Mengunduh hasil pengukuran tekanan darah..."
                })

                collector = OmronMeasurementCollector()
                try:
                    async with BleakClient(dev, timeout=18.0) as client:
                        print(f"[OMRON] Terhubung ke {dev_name} ({dev_mac}). Mengaktifkan indikasi 0x2A35...")
                        try:
                            if not client.is_paired:
                                await client.pair()
                        except Exception:
                            pass
                        subprocess.run(["bluetoothctl", "trust", dev_mac], capture_output=True, timeout=4)

                        # Segera subscribe ke 0x2A35 agar burst data Omron tidak terlewat
                        await client.start_notify(UUID_BP_MEASUREMENT, collector.notification_handler)

                        # Tunggu aliran paket indikasi 0x2A35 (maksimal 7.5 detik atau 1.8 detik setelah paket terakhir)
                        wait_loops = 30  # 30 x 0.25s = 7.5s
                        racp_requested = False
                        for step_i in range(wait_loops):
                            if omron_cancel_requested or not client.is_connected:
                                break
                            if collector.records and (time.time() - collector.last_packet_ts) >= 1.8:
                                break
                            # Jika sudah 3.5 detik belum ada indikasi, coba trigger RACP (Report Stored Records) & sync time
                            if step_i == 14 and not collector.records and not racp_requested:
                                racp_requested = True
                                await sync_omron_current_time(client)
                                try:
                                    racp_char = client.services.get_characteristic(UUID_RACP)
                                    if racp_char:
                                        await client.write_gatt_char(UUID_RACP, b"\x01\x01", response=True)
                                except Exception:
                                    pass
                            await asyncio.sleep(0.25)

                        if client.is_connected and not racp_requested:
                            await sync_omron_current_time(client)

                        try:
                            if client.is_connected:
                                await client.stop_notify(UUID_BP_MEASUREMENT)
                        except Exception:
                            pass

                except Exception as e:
                    # Catatan: Omron normalnya langsung memutus koneksi setelah mengirim paket terakhir
                    print(f"[OMRON CONN INFO] {e}")

                save_omron_config(dev_mac, dev_name)

                if collector.records:
                    latest = sorted(collector.records, key=lambda r: (r["datetime"], r["seq"]))[-1]
                    print(
                        f"[OMRON DONE] Tensi={latest['sys']}/{latest['dia']} mmHg, "
                        f"Pulse={latest['pulse']} bpm, Waktu={latest['measured_at']}"
                    )
                    publish_omron_event({
                        "status": "done",
                        "target_mode": "tensi_omron",
                        "sys": latest["sys"],
                        "dia": latest["dia"],
                        "pulse": latest["pulse"],
                        "map": latest["map"],
                        "mac": dev_mac,
                        "name": dev_name,
                        "measured_at": latest["measured_at"],
                        "raw_meta": {
                            "packets_count": len(collector.records),
                            "raw_packets": collector.raw_packets,
                            "status_flags": latest["status_flags"]
                        }
                    })
                    return

                # Jika terhubung namun 0 paket (misal alat baru selesai pairing -P- atau belum ada pengukuran baru)
                print("[OMRON INFO] Terhubung tetapi belum ada rekaman baru yang dikirim pada siklus ini.")
                elapsed = int(time.time() - start_ts)
                remaining = max(0, total_wait_sec - elapsed)
                if remaining > 12:
                    publish_omron_event({
                        "status": "scanning",
                        "target_mode": "tensi_omron",
                        "remaining": remaining,
                        "progress": 50,
                        "stage": "waiting_measurement",
                        "message": (
                            "Tensimeter Omron sudah terhubung/ter-pair, namun belum ada hasil pengukuran baru di memori. "
                            "Silakan pasang manset dan tekan tombol START/STOP pada tensimeter sekarang..."
                        )
                    })
                    await asyncio.sleep(3.5)
                else:
                    publish_omron_event({
                        "status": "error",
                        "target_mode": "tensi_omron",
                        "message": (
                            "Tensimeter Omron terhubung, tetapi tidak ada data pengukuran baru yang dikirim. "
                            "Lakukan pengukuran tekanan darah baru (tekan START/STOP pada tensimeter) lalu ulangi."
                        )
                    })
                    return

        except Exception as e:
            print(f"[OMRON MEASURE ERR] {e}")
            publish_omron_event({
                "status": "error",
                "target_mode": "tensi_omron",
                "message": f"Gagal membaca Tensimeter Omron: {e}"
            })
        finally:
            omron_busy = False


def parse_weight_data(data: bytearray):
    if len(data) < 13:
        return None
    ctrl_byte = data[0]
    stable = (ctrl_byte & 0x02) != 0
    catty = (ctrl_byte & 0x01) != 0
    lbs = (ctrl_byte & 0x10) != 0

    raw = int.from_bytes(data[11:13], byteorder="little")
    if catty:
        kg = (raw / 100.0) * 0.5
    elif lbs:
        kg = (raw / 100.0) * 0.453592
    else:
        kg = raw / 200.0

    return {"val": kg, "stable": stable}


def weight_notification_handler(sender, data):
    res = parse_weight_data(data)
    if not res:
        return
    weight_state["latest"] = res["val"]
    weight_state["stable"] = res["stable"]
    if weight_state["capture_request"] and weight_state["stable"] and weight_state["latest"] > 0:
        publish_measurement("berat_badan", weight_state["latest"])
        weight_state["capture_request"] = False


def height_notification_handler(sender, data):
    try:
        val = float(data.decode("utf-8").strip())
        height_state["latest"] = val
        if height_state["capture_request"]:
            publish_measurement("tinggi_badan", val)
            height_state["capture_request"] = False
    except Exception as e:
        print(f"[HEIGHT] Error decode: {e}")


async def handle_weight_capture_task():
    if not HAS_BLEAK or omron_busy:
        publish_ble_error("berat_badan", "Adapter Bluetooth sedang digunakan oleh Tensimeter Omron.")
        return
    async with ble_adapter_lock:
        try:
            device = await BleakScanner.find_device_by_address(WEIGHT_ADDRESS, timeout=6.0)
            if not device:
                publish_ble_error(
                    "berat_badan",
                    "Timbangan BLE belum terhubung. Nyalakan timbangan atau gunakan tombol Input Manual."
                )
                return
            weight_state["capture_request"] = True
            async with BleakClient(device, timeout=20.0) as client:
                mqtt_client.publish(MQTT_STATUS_TOPIC, "connected_ble_scale")
                weight_state["connected"] = True
                await client.start_notify(WEIGHT_UUID, weight_notification_handler)
                for _ in range(30):
                    if not weight_state["capture_request"] or omron_cancel_requested or not client.is_connected:
                        break
                    await asyncio.sleep(0.5)
                if weight_state["capture_request"]:
                    weight_state["capture_request"] = False
                    publish_ble_error("berat_badan", "Timbangan belum stabil. Silakan coba lagi atau gunakan Input Manual.")
        except Exception as e:
            publish_ble_error("berat_badan", f"Gagal membaca timbangan BLE: {e}")
        finally:
            weight_state["connected"] = False


async def handle_height_capture_task():
    if not HAS_BLEAK or omron_busy:
        publish_ble_error("tinggi_badan", "Adapter Bluetooth sedang digunakan oleh Tensimeter Omron.")
        return
    async with ble_adapter_lock:
        try:
            device = await BleakScanner.find_device_by_name(HEIGHT_NAME, timeout=6.0)
            if not device:
                publish_ble_error(
                    "tinggi_badan",
                    "Sensor Tinggi Badan BLE belum terhubung. Nyalakan alat atau gunakan tombol Input Manual."
                )
                return
            height_state["capture_request"] = True
            async with BleakClient(device, timeout=20.0) as client:
                height_state["connected"] = True
                await client.start_notify(HEIGHT_CHAR_UUID, height_notification_handler)
                for _ in range(24):
                    if not height_state["capture_request"] or omron_cancel_requested or not client.is_connected:
                        break
                    await asyncio.sleep(0.5)
                if height_state["capture_request"]:
                    height_state["capture_request"] = False
                    publish_ble_error("tinggi_badan", "Tidak ada data dari sensor tinggi BLE. Gunakan Input Manual.")
        except Exception as e:
            publish_ble_error("tinggi_badan", f"Gagal membaca sensor tinggi BLE: {e}")
        finally:
            height_state["connected"] = False


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[BLE MQTT] Terhubung ke Broker ({reason_code}). Subscribe ke {MQTT_CMD_TOPIC}, {MQTT_OMRON_CMD_TOPIC}, {MQTT_STOP_TOPIC}")
    client.subscribe(MQTT_CMD_TOPIC)
    client.subscribe(MQTT_OMRON_CMD_TOPIC)
    client.subscribe(MQTT_STOP_TOPIC)


def on_message(client, userdata, msg):
    global omron_cancel_requested
    topic = msg.topic
    payload_str = msg.payload.decode("utf-8", errors="ignore").strip()

    if topic == MQTT_STOP_TOPIC:
        omron_cancel_requested = True
        weight_state["capture_request"] = False
        height_state["capture_request"] = False
        return

    if topic == MQTT_OMRON_CMD_TOPIC:
        try:
            data = json.loads(payload_str) if payload_str.startswith("{") else {"cmd": payload_str}
        except Exception:
            data = {"cmd": payload_str}

        cmd = data.get("cmd", "start")
        if cmd == "stop":
            omron_cancel_requested = True
            return

        if main_loop is None:
            return

        if omron_busy:
            # Batalkan tugas Omron sebelumnya lalu jalankan perintah baru
            omron_cancel_requested = True

        if cmd == "pair":
            req_mac = data.get("mac")
            asyncio.run_coroutine_threadsafe(handle_omron_pair_task(req_mac), main_loop)
        elif cmd == "start":
            asyncio.run_coroutine_threadsafe(handle_omron_measure_task(), main_loop)
        return

    if topic == MQTT_CMD_TOPIC:
        if main_loop is None:
            return
        if payload_str == "6":
            asyncio.run_coroutine_threadsafe(handle_weight_capture_task(), main_loop)
        elif payload_str == "5":
            asyncio.run_coroutine_threadsafe(handle_height_capture_task(), main_loop)


async def main():
    global mqtt_client, main_loop, ble_adapter_lock
    main_loop = asyncio.get_running_loop()
    ble_adapter_lock = asyncio.Lock()

    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    while True:
        try:
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_start()
            break
        except Exception as e:
            print(f"[BLE] Menunggu MQTT broker: {e}")
            await asyncio.sleep(3)

    if not HAS_BLEAK:
        print("[BLE] Library bleak tidak tersedia; layanan BLE pasif.")
    else:
        cfg = load_omron_config()
        print(f"[BLE] Layanan BLE Omron HEM-7156T ({cfg['name']} / {cfg['mac']}) & Antropometri siap (on-demand lock).")

    while True:
        await asyncio.sleep(30)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
