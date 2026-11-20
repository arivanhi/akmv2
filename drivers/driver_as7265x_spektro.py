#!/usr/bin/env python3
"""
driver_as7265x_spektro.py - 18-Channel Multi-Biomarker Engine untuk Sensor AS7265x Triad
Dioptimalkan berdasarkan studi literatur ilmiah untuk AKMv2:
1. Sampling Kontinu Optimal (~3.3 - 3.5 Hz, INTEG=49 / ~278ms per 18-ch frame, Gain 64x, LED 25mA)
2. Pra-pemrosesan Sinyal:
   - Pembuangan transien awal (settling frames saat LED baru menyala & jari menyesuaikan; Umar & Alyah, 2020)
   - Penolakan outlier per kanal berbasis Interquartile Range / IQR (Arnita et al., 2026)
   - Ekstraksi vektor bersih 18-kanal, standar deviasi stabilitas, dan normalisasi L2 (Yaemsuk & Yammen, 2024)
3. Pemetaan Kanal Spesifik Hasil Penelitian:
   - Gula Darah  : D(485), I(645), V(810-isosbestik), W(860), K(900), L(940) [Shokrekhodaei et al., 2021; Hina & Saadeh, 2022]
   - Kolesterol  : R(610), S(680), V(810), W(860), L(940) [Arnita et al., 2026; Umar et al., 2020/2022]
   - Asam Urat   : A(410), B(435), U(760), V(810), K(900), L(940) [Umar & Alyah, 2020; Sumarti et al., 2024; Lin et al., 2022]
4. Dukungan inferensi otomatis model Machine Learning (.joblib) yang dilatih dari data Ground-Truth AKMv2.
"""

import os
import json
import time
import threading
import numpy as np

try:
    import joblib
    HAS_JOBLIB = True
except ImportError:
    HAS_JOBLIB = False

try:
    import qwiic_as7265x
    HAS_AS7265X = True
except ImportError:
    HAS_AS7265X = False

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CALIBRATION_FILE = os.path.join(ROOT_DIR, "calibration_config.json")
MODEL_DIR = os.path.join(ROOT_DIR, "model")

CHANNEL_NAMES = [
    'A_410', 'B_435', 'C_460', 'D_485', 'E_510', 'F_535',
    'G_560', 'H_585', 'R_610', 'I_645', 'S_680', 'J_705',
    'T_730', 'U_760', 'V_810', 'W_860', 'K_900', 'L_940'
]

# Dokumentasi kanal aktif per biomarker sesuai literatur
LITERATURE_CHANNELS = {
    "gula_darah": ["D_485", "I_645", "V_810", "W_860", "K_900", "L_940"],
    "kolesterol": ["R_610", "S_680", "V_810", "W_860", "L_940"],
    "asam_urat":  ["A_410", "B_435", "U_760", "V_810", "K_900", "L_940"]
}

# Waktu integrasi optimal: 49 -> (49 + 1) * 2.78 ms = 139 ms per bank (x2 bank = ~278 ms / ~3.5 Hz)
OPTIMAL_INTEGRATION_CYCLES = 49
SETTLING_FRAMES_TO_DISCARD = 2


class MultiBiomarkerModel:
    def __init__(self, i2c_lock=None):
        self.i2c_lock = i2c_lock or threading.Lock()
        self.sensor = None
        self.ml_models = {}
        self.load_or_create_calibration()
        self.reload_ml_models()
        self.init_sensor(max_retries=2, retry_delay=0.4)

    def load_or_create_calibration(self):
        default_config = {
            "suhu": {"bias": 0.0},
            "spo2": {"bias": 0.0},
            "bpm": {"bias": 0.0},
            "tekanan_darah": {"sys_bias": 0.0, "dia_bias": 0.0},
            "gula_darah": {"gain": 1.0, "bias": 0.0, "use_ml": True},
            "kolesterol": {"gain": 1.0, "bias": 0.0, "use_ml": True},
            "asam_urat": {"gain": 1.0, "bias": 0.0, "use_ml": True}
        }
        if os.path.exists(CALIBRATION_FILE):
            try:
                with open(CALIBRATION_FILE, "r") as f:
                    loaded = json.load(f)
                for k, v in default_config.items():
                    if k in loaded and isinstance(loaded[k], dict):
                        v.update(loaded[k])
                self.calib = default_config
                return
            except Exception as e:
                print(f"[CALIB WARN] Gagal memuat file kalibrasi: {e}")

        self.calib = default_config
        try:
            with open(CALIBRATION_FILE, "w") as f:
                json.dump(default_config, f, indent=4)
        except Exception as e:
            print(f"[CALIB WARN] Gagal membuat file: {e}")

    def reload_ml_models(self):
        """Memuat model ML kustom berbasis Ground-Truth AS7265x jika sudah dilatih."""
        self.ml_models = {}
        if not HAS_JOBLIB or not os.path.isdir(MODEL_DIR):
            return
        mapping = {
            "gula_darah": "as7265x_gula_darah.joblib",
            "kolesterol": "as7265x_kolesterol.joblib",
            "asam_urat":  "as7265x_asam_urat.joblib"
        }
        for key, fname in mapping.items():
            fpath = os.path.join(MODEL_DIR, fname)
            if os.path.exists(fpath):
                try:
                    self.ml_models[key] = joblib.load(fpath)
                    print(f"[SPEKTRO ML] Model Ground-Truth '{fname}' aktif untuk {key}.")
                except Exception as e:
                    print(f"[SPEKTRO ML WARN] Gagal memuat {fname}: {e}")

    def _configure_sensor_registers(self, s):
        """Setel Gain 64x, Integration Cycles=49 (~278ms/frame), dan Arus 3 LED=25mA."""
        if hasattr(s, 'set_gain') and hasattr(s, 'kGain64x'):
            s.set_gain(s.kGain64x)
        if hasattr(s, 'set_integration_cycles'):
            s.set_integration_cycles(OPTIMAL_INTEGRATION_CYCLES)
        if hasattr(s, 'disable_indicator'):
            try:
                s.disable_indicator()
            except Exception:
                pass
        if hasattr(s, 'set_bulb_current'):
            s.set_bulb_current(s.kLedCurrentLimit25mA, s.kLedWhite)
            s.set_bulb_current(s.kLedCurrentLimit25mA, s.kLedIr)
            s.set_bulb_current(s.kLedCurrentLimit25mA, s.kLedUv)

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
                        time.sleep(0.10)
                        self._configure_sensor_registers(s)
                        try:
                            s.disable_bulb(s.kLedWhite)
                            s.disable_bulb(s.kLedIr)
                            s.disable_bulb(s.kLedUv)
                        except Exception:
                            pass
                        self.sensor = s
                        print(f"[SPEKTRO SUCCESS] AS7265x 18-Channel Siap (INTEG={OPTIMAL_INTEGRATION_CYCLES}, Gain=64x, Standby LED OFF).")
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
                    self._configure_sensor_registers(self.sensor)
                    self.sensor.enable_bulb(self.sensor.kLedWhite)
                    self.sensor.enable_bulb(self.sensor.kLedIr)
                    self.sensor.enable_bulb(self.sensor.kLedUv)
                print("[SPEKTRO] 3 LED (UV 405nm, White 5700K, IR 875nm) dinyalakan untuk pengukuran.")
                return True
            except Exception as e:
                print(f"[SPEKTRO LED ON ERROR] {e}. Mencoba re-koneksi...")
                self.sensor = None
                self.init_sensor(max_retries=2, retry_delay=0.3)
                if self.sensor:
                    try:
                        with self.i2c_lock:
                            self._configure_sensor_registers(self.sensor)
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
        """
        Membaca 1 frame 18-channel mentah (raw spectral counts) dari Triad AS7265x (~278 ms/frame @ INTEG=49).
        Tanpa sleep berlebih agar frekuensi sampling mencapai batas optimal perangkat keras (~3.3 - 3.5 Hz).
        """
        if self.sensor is None:
            self.init_sensor(max_retries=1, retry_delay=0.2)

        if self.sensor is not None:
            try:
                with self.i2c_lock:
                    self.sensor.take_measurements()
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

    @staticmethod
    def _iqr_filtered_stats(frames_2d):
        """
        Filter outlier per kanal menggunakan metode Interquartile Range (IQR, Arnita et al., 2026).
        Mengembalikan (clean_mean_18, clean_std_18).
        """
        n_frames, n_ch = frames_2d.shape
        if n_frames < 4:
            return np.mean(frames_2d, axis=0), np.std(frames_2d, axis=0)

        clean_means = np.zeros(n_ch, dtype=float)
        clean_stds  = np.zeros(n_ch, dtype=float)
        for ch in range(n_ch):
            col = frames_2d[:, ch]
            q1, q3 = np.percentile(col, [25, 75])
            iqr = q3 - q1
            if iqr > 1e-6:
                low = q1 - 1.5 * iqr
                high = q3 + 1.5 * iqr
                inliers = col[(col >= low) & (col <= high)]
                if len(inliers) == 0:
                    inliers = col
            else:
                inliers = col
            clean_means[ch] = float(np.mean(inliers))
            clean_stds[ch]  = float(np.std(inliers))
        return clean_means, clean_stds

    def _predict_with_ml_if_available(self, target_key, avg_channels, l2_norm):
        """Jika model ML terlatih dari Ground-Truth tersedia dan diaktifkan, gunakan model tersebut."""
        use_ml = bool(self.calib.get(target_key, {}).get("use_ml", True))
        bundle = self.ml_models.get(target_key)
        if not use_ml or bundle is None:
            return None
        try:
            model = bundle["model"] if isinstance(bundle, dict) and "model" in bundle else bundle
            feat_mode = bundle.get("feature_mode", "raw18_plus_l2") if isinstance(bundle, dict) else "raw18"
            if feat_mode == "raw18_plus_l2":
                x_vec = np.concatenate([avg_channels, l2_norm]).reshape(1, -1)
            else:
                x_vec = np.array(avg_channels, dtype=float).reshape(1, -1)
            pred = float(model.predict(x_vec)[0])
            return pred
        except Exception as e:
            print(f"[SPEKTRO ML PREDICT WARN] ({target_key}): {e}")
            return None

    def predict_biomarkers(self, raw_18_samples, duration_sec=7.0):
        """
        Menerima list batch sample 18-channel dari akumulasi scan (~7 detik @ ~3.3 Hz ≈ 20-24 frame).
        1. Membuang frame transien awal (settling time)
        2. Memvalidasi frame jari menempel
        3. Menyaring outlier gerak jari dengan IQR per kanal
        4. Menghitung estimasi Gula Darah, Kolesterol, dan Asam Urat berbasis kanal literatur / model ML
        """
        if not raw_18_samples:
            return None

        all_frames_arr = np.array(raw_18_samples, dtype=float)
        total_frames = int(all_frames_arr.shape[0])

        # 1. Buang frame transien awal saat LED baru menyala & jari baru menempel (Umar & Alyah, 2020)
        if total_frames >= 6:
            discarded = SETTLING_FRAMES_TO_DISCARD
            working_frames = all_frames_arr[discarded:]
        elif total_frames >= 4:
            discarded = 1
            working_frames = all_frames_arr[1:]
        else:
            discarded = 0
            working_frames = all_frames_arr

        # 2. Filter hanya frame saat jari benar-benar menutup sensor (L_940 >= 350 dan Total >= 750)
        frame_sums = np.sum(working_frames, axis=1)
        l940_vals  = working_frames[:, 17]
        valid_mask = (frame_sums >= 750.0) & (l940_vals >= 350.0)
        valid_frames_arr = working_frames[valid_mask]
        valid_count = int(valid_frames_arr.shape[0])

        sampling_hz = round(total_frames / max(0.5, float(duration_sec)), 2)

        if valid_count < max(2, min(4, len(working_frames) // 2)):
            fallback_avg = np.mean(working_frames, axis=0)
            return {
                "finger_detected": False,
                "gula": None,
                "kolesterol": None,
                "asam_urat": None,
                "raw_avg": [round(float(x), 2) for x in fallback_avg],
                "raw_std": [0.0] * 18,
                "l2_norm": [0.0] * 18,
                "all_frames": [[round(float(v), 2) for v in row] for row in all_frames_arr],
                "total_frames": total_frames,
                "valid_frames": valid_count,
                "discarded_frames": discarded,
                "sampling_hz": sampling_hz,
                "channels_used": LITERATURE_CHANNELS,
                "model_source": {}
            }

        # 3. Filter Outlier IQR per kanal (Arnita et al., 2026) + Normalisasi L2 (Yaemsuk & Yammen, 2024)
        avg_channels, std_channels = self._iqr_filtered_stats(valid_frames_arr)
        l2_denom = float(np.linalg.norm(avg_channels))
        l2_norm = (avg_channels / l2_denom) if l2_denom > 1e-6 else np.zeros(18, dtype=float)

        a410, b435, c460, d485, e510, f535 = avg_channels[0:6]
        g560, h585, r610, i645, s680, j705 = avg_channels[6:12]
        t730, u760, v810, w860, k900, l940 = avg_channels[12:18]

        total_intensity = float(np.sum(avg_channels))
        if total_intensity < 750.0 or l940 < 350.0:
            return {
                "finger_detected": False,
                "gula": None,
                "kolesterol": None,
                "asam_urat": None,
                "raw_avg": [round(float(x), 2) for x in avg_channels],
                "raw_std": [round(float(x), 3) for x in std_channels],
                "l2_norm": [round(float(x), 5) for x in l2_norm],
                "all_frames": [[round(float(v), 2) for v in row] for row in all_frames_arr],
                "total_frames": total_frames,
                "valid_frames": valid_count,
                "discarded_frames": discarded,
                "sampling_hz": sampling_hz,
                "channels_used": LITERATURE_CHANNELS,
                "model_source": {}
            }

        model_source = {}

        # =========================================================================
        # 1. ESTIMASI GULA DARAH (mg/dL)
        # Kanal Literatur: D(485), I(645), V(810-isosbestik), W(860), K(900), L(940)
        # Referensi: Shokrekhodaei et al. (2021) IEEE Access; Hina & Saadeh (2022)
        # =========================================================================
        ml_g = self._predict_with_ml_if_available("gula_darah", avg_channels, l2_norm)
        if ml_g is not None:
            pred_g = ml_g
            model_source["gula_darah"] = "ml_ground_truth"
        else:
            r_gula_nir = (l940 / max(1.0, w860)) / 30.00   # Overtone O-H/C-H glukosa 940nm vs 860nm
            r_gula_vis = (i645 / max(1.0, d485)) / 5.75    # Korelasi spektral 645nm vs 485nm (Shokrekhodaei 2021)
            r_gula_iso = (k900 / max(1.0, v810)) / 5.05    # NIR 900nm ternormalisasi titik isosbestik Hb 810nm
            pred_g = 98.0 + 42.0 * (r_gula_nir - 1.0) + 28.0 * (r_gula_vis - 1.0) + 25.0 * (r_gula_iso - 1.0)
            model_source["gula_darah"] = "literature_spectral_ratio"

        calib_g = self.calib.get("gula_darah", {"gain": 1.0, "bias": 0.0})
        final_g = pred_g * float(calib_g.get("gain", 1.0)) + float(calib_g.get("bias", 0.0))
        final_g = round(float(np.clip(final_g, 50.0, 350.0)), 1)

        # =========================================================================
        # 2. ESTIMASI KOLESTEROL TOTAL (mg/dL)
        # Kanal Literatur: R(610), S(680), V(810), W(860), L(940)
        # Referensi: Arnita et al. (2026) SJI; Umar et al. (2020 & 2022)
        # =========================================================================
        ml_k = self._predict_with_ml_if_available("kolesterol", avg_channels, l2_norm)
        if ml_k is not None:
            pred_k = ml_k
            model_source["kolesterol"] = "ml_ground_truth"
        else:
            r_kol_vis  = (s680 / max(1.0, r610)) / 0.475   # Rasio 680nm vs 610nm (fitur utama Arnita et al., 2026)
            r_kol_nir1 = (w860 / max(1.0, v810)) / 1.020   # Serapan lipid/trigliserida 860nm vs isosbestik 810nm
            r_kol_nir2 = (l940 / max(1.0, s680)) / 7.300   # Ikatan hidrokarbon C-H kolesterol 940nm vs Hb 680nm
            pred_k = 175.0 + 40.0 * (r_kol_vis - 1.0) + 35.0 * (r_kol_nir1 - 1.0) + 30.0 * (r_kol_nir2 - 1.0)
            model_source["kolesterol"] = "literature_spectral_ratio"

        calib_k = self.calib.get("kolesterol", {"gain": 1.0, "bias": 0.0})
        final_k = pred_k * float(calib_k.get("gain", 1.0)) + float(calib_k.get("bias", 0.0))
        final_k = round(float(np.clip(final_k, 100.0, 350.0)), 1)

        # =========================================================================
        # 3. ESTIMASI ASAM URAT (mg/dL)
        # Kanal Literatur: A(410), B(435), U(760), V(810), K(900), L(940)
        # Referensi: Umar & Alyah (2020); Sumarti et al. (2024); Lin et al. (2022)
        # =========================================================================
        ml_u = self._predict_with_ml_if_available("asam_urat", avg_channels, l2_norm)
        if ml_u is not None:
            pred_u = ml_u
            model_source["asam_urat"] = "ml_ground_truth"
        else:
            r_urat_uv  = (a410 / max(1.0, u760)) / 1.450   # Ekor serapan purin UV-tepi 410nm vs oxy-Hb 760nm
            r_urat_vis = (b435 / max(1.0, v810)) / 2.450   # Hamburan violet 435nm vs isosbestik 810nm
            r_urat_nir = (l940 / max(1.0, k900)) / 6.400   # Overtone N-H/C=O asam urat di penetrasi NIR 940nm vs 900nm
            pred_u = 5.2 + 1.8 * (r_urat_uv - 1.0) + 1.4 * (r_urat_vis - 1.0) + 1.8 * (r_urat_nir - 1.0)
            model_source["asam_urat"] = "literature_spectral_ratio"

        calib_u = self.calib.get("asam_urat", {"gain": 1.0, "bias": 0.0})
        final_u = pred_u * float(calib_u.get("gain", 1.0)) + float(calib_u.get("bias", 0.0))
        final_u = round(float(np.clip(final_u, 2.0, 15.0)), 1)

        return {
            "finger_detected": True,
            "gula": final_g,
            "kolesterol": final_k,
            "asam_urat": final_u,
            "raw_avg": [round(float(x), 2) for x in avg_channels],
            "raw_std": [round(float(x), 3) for x in std_channels],
            "l2_norm": [round(float(x), 5) for x in l2_norm],
            "all_frames": [[round(float(v), 2) for v in row] for row in all_frames_arr],
            "total_frames": total_frames,
            "valid_frames": valid_count,
            "discarded_frames": discarded,
            "sampling_hz": sampling_hz,
            "channels_used": LITERATURE_CHANNELS,
            "model_source": model_source
        }
