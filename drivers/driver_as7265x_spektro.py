#!/usr/bin/env python3
"""
driver_as7265x_spektro.py - 18-Channel Multi-Biomarker Engine untuk Sensor AS7265x Triad
Diadaptasi dari Kopersakti-UDINUS untuk AKMv2:
- Path calibration_config.json disatukan ke root akmv2/calibration_config.json
- Thread-safe I2C lock
- Fallback angka dummy dinonaktifkan agar melaporkan error jujur saat sensor belum terpasang
"""

import os
import json
import time
import threading
import numpy as np

try:
    import qwiic_as7265x
    HAS_AS7265X = True
except ImportError:
    HAS_AS7265X = False

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALIBRATION_FILE = os.path.join(ROOT_DIR, "calibration_config.json")

CHANNEL_NAMES = [
    'A_410', 'B_435', 'C_460', 'D_485', 'E_510', 'F_535',
    'G_560', 'H_585', 'R_610', 'I_645', 'S_680', 'J_705',
    'T_730', 'U_760', 'V_810', 'W_860', 'K_900', 'L_940'
]


class MultiBiomarkerModel:
    def __init__(self, i2c_lock=None):
        self.i2c_lock = i2c_lock or threading.Lock()
        self.sensor = None
        self.load_or_create_calibration()
        self.init_sensor(max_retries=2, retry_delay=0.4)

    def load_or_create_calibration(self):
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
                    self.calib = json.load(f)
                return
            except Exception as e:
                print(f"[CALIB WARN] Gagal memuat file kalibrasi: {e}")

        self.calib = default_config
        try:
            with open(CALIBRATION_FILE, "w") as f:
                json.dump(default_config, f, indent=4)
        except Exception as e:
            print(f"[CALIB WARN] Gagal membuat file: {e}")

    def init_sensor(self, max_retries=3, retry_delay=0.5):
        if not HAS_AS7265X:
            print("[SPEKTRO WARN] Library qwiic_as7265x tidak tersedia.")
            self.sensor = None
            return None

        for attempt in range(1, max_retries + 1):
            try:
                with self.i2c_lock:
                    s = qwiic_as7265x.QwiicAS7265x()
                    time.sleep(0.08)
                    if s.begin():
                        time.sleep(0.12)
                        if hasattr(s, 'set_gain') and hasattr(s, 'kGain64x'):
                            s.set_gain(s.kGain64x)
                        if hasattr(s, 'set_bulb_current'):
                            s.set_bulb_current(s.kLedCurrentLimit25mA, s.kLedWhite)
                            s.set_bulb_current(s.kLedCurrentLimit25mA, s.kLedIr)
                            s.set_bulb_current(s.kLedCurrentLimit25mA, s.kLedUv)
                        try:
                            s.disable_bulb(s.kLedWhite)
                            s.disable_bulb(s.kLedIr)
                            s.disable_bulb(s.kLedUv)
                        except Exception:
                            pass
                        self.sensor = s
                        print(f"[SPEKTRO SUCCESS] AS7265x 18-Channel Siap (Percobaan {attempt}/{max_retries}, Standby LED OFF).")
                        return self.sensor
                    else:
                        print(f"[SPEKTRO WARN] begin() return False (Percobaan {attempt}/{max_retries})")
            except Exception as e:
                print(f"[SPEKTRO WARN] Inisialisasi percobaan {attempt}/{max_retries} gagal: {e}")

            if attempt < max_retries:
                time.sleep(retry_delay)

        self.sensor = None
        return None

    def enable_leds(self):
        if self.sensor is None:
            self.init_sensor(max_retries=2, retry_delay=0.3)

        if self.sensor:
            try:
                with self.i2c_lock:
                    if hasattr(self.sensor, 'set_bulb_current'):
                        self.sensor.set_bulb_current(self.sensor.kLedCurrentLimit25mA, self.sensor.kLedWhite)
                        self.sensor.set_bulb_current(self.sensor.kLedCurrentLimit25mA, self.sensor.kLedIr)
                        self.sensor.set_bulb_current(self.sensor.kLedCurrentLimit25mA, self.sensor.kLedUv)
                    self.sensor.enable_bulb(self.sensor.kLedWhite)
                    self.sensor.enable_bulb(self.sensor.kLedIr)
                    self.sensor.enable_bulb(self.sensor.kLedUv)
                print("[SPEKTRO] 3 LED dinyalakan untuk pengukuran.")
                return True
            except Exception as e:
                print(f"[SPEKTRO LED ON ERROR] {e}. Mencoba re-koneksi...")
                self.sensor = None
                self.init_sensor(max_retries=2, retry_delay=0.3)
                if self.sensor:
                    try:
                        with self.i2c_lock:
                            self.sensor.enable_bulb(self.sensor.kLedWhite)
                            self.sensor.enable_bulb(self.sensor.kLedIr)
                            self.sensor.enable_bulb(self.sensor.kLedUv)
                        return True
                    except Exception as e2:
                        print(f"[SPEKTRO RECONNECT LED ERROR] {e2}")
                        self.sensor = None
        return False

    def disable_leds(self):
        if self.sensor:
            try:
                with self.i2c_lock:
                    self.sensor.disable_bulb(self.sensor.kLedWhite)
                    self.sensor.disable_bulb(self.sensor.kLedIr)
                    self.sensor.disable_bulb(self.sensor.kLedUv)
                print("[SPEKTRO] 3 LED dimatikan (standby).")
            except Exception as e:
                print(f"[SPEKTRO LED OFF ERROR] {e}")

    def read_18_channels(self):
        """Membaca 18 channel spektral riil dari Triad AS7265x (tanpa dummy fallback)."""
        if self.sensor is None:
            self.init_sensor(max_retries=1, retry_delay=0.2)

        if self.sensor is not None:
            try:
                with self.i2c_lock:
                    self.sensor.take_measurements()
                    time.sleep(0.05)
                    vals = [
                        float(self.sensor.get_a()), float(self.sensor.get_b()), float(self.sensor.get_c()),
                        float(self.sensor.get_d()), float(self.sensor.get_e()), float(self.sensor.get_f()),
                        float(self.sensor.get_g()), float(self.sensor.get_h()), float(self.sensor.get_r()),
                        float(self.sensor.get_i()), float(self.sensor.get_s()), float(self.sensor.get_j()),
                        float(self.sensor.get_t()), float(self.sensor.get_u()), float(self.sensor.get_v()),
                        float(self.sensor.get_w()), float(self.sensor.get_k()), float(self.sensor.get_l())
                    ]
                return np.array(vals, dtype=float)
            except Exception as e:
                print(f"[SPEKTRO READ ERROR] {e}")
                self.sensor = None

        return None

    def predict_biomarkers(self, raw_18_samples):
        """
        Menerima list batch sample 18-channel (dari akumulasi scan 5 detik).
        Menghasilkan dictionary hasil prediksi 3 biomarker + vektor rata-rata 18 kanal.
        """
        if not raw_18_samples:
            return None

        avg_channels = np.mean(raw_18_samples, axis=0)
        a410, b435, c460, d485, e510, f535 = avg_channels[0:6]
        g560, h585, r610, i645, s680, j705 = avg_channels[6:12]
        t730, u760, v810, w860, k900, l940 = avg_channels[12:18]

        total_intensity = float(np.sum(avg_channels))

        # Validasi Jari Menempel (pantulan difus jari L_940 > 400 dan Total > 800)
        if total_intensity < 800.0 or l940 < 400.0:
            return {
                "finger_detected": False,
                "gula": None,
                "kolesterol": None,
                "asam_urat": None,
                "raw_avg": [round(float(x), 2) for x in avg_channels]
            }

        # 1. ESTIMASI GULA DARAH (mg/dL)
        r_gula_1 = (l940 / max(1.0, w860)) / 27.60
        r_gula_2 = (k900 / max(1.0, t730)) / 2.42
        pred_g = 98.0 + 55.0 * (r_gula_1 - 1.0) + 40.0 * (r_gula_2 - 1.0)

        calib_g = self.calib.get("gula_darah", {"gain": 1.0, "bias": 0.0})
        final_g = pred_g * float(calib_g.get("gain", 1.0)) + float(calib_g.get("bias", 0.0))
        final_g = round(float(np.clip(final_g, 50.0, 240.0)), 1)

        # 2. ESTIMASI KOLESTEROL TOTAL (mg/dL)
        r_kol_1 = (s680 / max(1.0, g560)) / 2.53
        r_kol_2 = (v810 / max(1.0, t730)) / 0.57
        pred_k = 172.0 + 50.0 * (r_kol_1 - 1.0) + 45.0 * (r_kol_2 - 1.0)

        calib_k = self.calib.get("kolesterol", {"gain": 1.0, "bias": 0.0})
        final_k = pred_k * float(calib_k.get("gain", 1.0)) + float(calib_k.get("bias", 0.0))
        final_k = round(float(np.clip(final_k, 100.0, 320.0)), 1)

        # 3. ESTIMASI ASAM URAT (mg/dL)
        r_urat_1 = (a410 / max(1.0, u760)) / 0.525
        r_urat_2 = (b435 / max(1.0, h585)) / 0.320
        pred_u = 4.8 + 2.5 * (r_urat_1 - 1.0) + 2.0 * (r_urat_2 - 1.0)

        calib_u = self.calib.get("asam_urat", {"gain": 1.0, "bias": 0.0})
        final_u = pred_u * float(calib_u.get("gain", 1.0)) + float(calib_u.get("bias", 0.0))
        final_u = round(float(np.clip(final_u, 2.0, 13.0)), 1)

        return {
            "finger_detected": True,
            "gula": final_g,
            "kolesterol": final_k,
            "asam_urat": final_u,
            "raw_avg": [round(float(x), 2) for x in avg_channels]
        }
