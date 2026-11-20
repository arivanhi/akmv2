#!/usr/bin/env python3
"""
driver_max30102.py - Driver & Processing Engine untuk Sensor MAX30102 (AKMv2)
Perbaikan utama:
- enable_leds() selalu menulis ulang seluruh register konfigurasi (FIFO, SpO2, LED1_PA, LED2_PA)
  sehingga tahan terhadap hot-plug / Power-On Reset (mencegah LED arus 0 mA)
- Pembuangan transien awal & outlier clamping + fallback autokorelasi agar deteksi nadi tetap stabil
  meskipun jari sedikit bergerak atau menekan agak kuat
"""

import time
import threading
import numpy as np

try:
    import smbus2
    HAS_SMBUS = True
except ImportError:
    HAS_SMBUS = False

MAX_ADDR = 0x57

REG_FIFO_WR_PTR = 0x04
REG_OVF_COUNTER = 0x05
REG_FIFO_RD_PTR = 0x06
REG_FIFO_DATA   = 0x07
REG_FIFO_CONFIG = 0x08
REG_MODE_CONFIG = 0x09
REG_SPO2_CONFIG = 0x0A
REG_LED1_PA     = 0x0C  # RED LED
REG_LED2_PA     = 0x0D  # IR LED
REG_PART_ID     = 0xFF  # Expected 0x15

# Ambang IR disesuaikan dengan refleksi kaca koper (~10.000 saat kosong, >18.000 saat jari menempel)
FINGER_IR_THRESHOLD = 18000.0


class MAX30102:
    def __init__(self, bus_num=1, i2c_lock=None):
        self.bus_num = bus_num
        self.i2c_lock = i2c_lock or threading.Lock()
        self.bus = None
        self.connected = False
        self._init_sensor()

    def _open_bus(self):
        if not HAS_SMBUS:
            return False
        if self.bus is None:
            self.bus = smbus2.SMBus(self.bus_num)
        return True

    def _configure_registers(self, active_leds=False):
        """Menulis penuh konfigurasi register MAX30102 (aman setelah POR / cabut-pasang kabel)."""
        # Cek Part ID terlebih dahulu
        part_id = self.bus.read_byte_data(MAX_ADDR, REG_PART_ID)
        if part_id != 0x15:
            print(f"[MAX30102 WARN] Unexpected Part ID: 0x{part_id:02X}")

        # Soft reset
        self.bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x40)
        time.sleep(0.05)

        # Sample avg = 4, rollover enable -> 25 Hz efektif (100 Hz / 4)
        self.bus.write_byte_data(MAX_ADDR, REG_FIFO_CONFIG, 0x50)
        # ADC range 8192nA (0x40) + 100 Hz (0x04) + 411us 18-bit (0x03) -> 0x47 (anti-saturasi saat ditekan agak kuat)
        self.bus.write_byte_data(MAX_ADDR, REG_SPO2_CONFIG, 0x47)
        # Arus LED Red & IR ~11mA (0x37) agar penetrasi kapiler kuat namun tidak saturasi
        self.bus.write_byte_data(MAX_ADDR, REG_LED1_PA, 0x37)
        self.bus.write_byte_data(MAX_ADDR, REG_LED2_PA, 0x37)

        self.bus.write_byte_data(MAX_ADDR, REG_FIFO_WR_PTR, 0x00)
        self.bus.write_byte_data(MAX_ADDR, REG_OVF_COUNTER, 0x00)
        self.bus.write_byte_data(MAX_ADDR, REG_FIFO_RD_PTR, 0x00)

        # Mode SpO2 aktif (0x03) atau Shutdown hemat daya (0x80)
        self.bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x03 if active_leds else 0x80)

    def _init_sensor(self):
        if not HAS_SMBUS:
            self.connected = False
            return False
        try:
            with self.i2c_lock:
                self._open_bus()
                self._configure_registers(active_leds=False)
                self.connected = True
                print("[MAX30102] Berhasil diinisialisasi (Standby LED OFF).")
                return True
        except Exception as e:
            print(f"[MAX30102 ERROR] Inisialisasi gagal: {e}")
            if self.bus is not None:
                try:
                    self.bus.close()
                except Exception:
                    pass
            self.bus = None
            self.connected = False
            return False

    def enable_leds(self):
        """Menyalakan LED Red & IR sekaligus menjamin seluruh register terkonfigurasi penuh."""
        try:
            with self.i2c_lock:
                self._open_bus()
                self._configure_registers(active_leds=True)
            # Beri waktu 0.18s agar LED & DC level stabil lalu bersihkan FIFO transien awal
            time.sleep(0.18)
            self.clear_fifo()
            self.connected = True
            print("[MAX30102] Konfigurasi penuh & LED Red/IR dinyalakan.")
            return True
        except Exception as e:
            print(f"[MAX30102 LED ON ERROR] {e}")
            if self.bus is not None:
                try:
                    self.bus.close()
                except Exception:
                    pass
            self.bus = None
            self.connected = False
            return False

    def disable_leds(self):
        if self.bus is None:
            return
        try:
            with self.i2c_lock:
                self.bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x80)
            print("[MAX30102] Sensor di-standby / LED dimatikan.")
        except Exception as e:
            print(f"[MAX30102 LED OFF ERROR] {e}")

    def clear_fifo(self):
        if self.bus is None:
            return
        try:
            with self.i2c_lock:
                self.bus.write_byte_data(MAX_ADDR, REG_FIFO_WR_PTR, 0x00)
                self.bus.write_byte_data(MAX_ADDR, REG_OVF_COUNTER, 0x00)
                self.bus.write_byte_data(MAX_ADDR, REG_FIFO_RD_PTR, 0x00)
        except Exception as e:
            print(f"[MAX30102 WARN] Clear FIFO: {e}")

    def read_fifo(self):
        if not self.connected or self.bus is None:
            return []
        samples = []
        try:
            with self.i2c_lock:
                wr_ptr = self.bus.read_byte_data(MAX_ADDR, REG_FIFO_WR_PTR)
                rd_ptr = self.bus.read_byte_data(MAX_ADDR, REG_FIFO_RD_PTR)
                num_samples = (wr_ptr - rd_ptr) % 32
                if num_samples > 0:
                    for _ in range(num_samples):
                        b = self.bus.read_i2c_block_data(MAX_ADDR, REG_FIFO_DATA, 6)
                        red = ((b[0] << 16) | (b[1] << 8) | b[2]) & 0x03FFFF
                        ir  = ((b[3] << 16) | (b[4] << 8) | b[5]) & 0x03FFFF
                        samples.append((red, ir))
        except Exception as e:
            print(f"[MAX30102 READ ERROR] {e}")
            self.connected = False
        return samples

    def _estimate_bpm_autocorr(self, sig, sample_rate):
        """Fallback estimasi BPM berbasis autokorelasi jika amplitudo puncak kecil saat jari menekan kuat."""
        n = len(sig)
        min_lag = int(sample_rate * 0.35)  # ~171 bpm
        max_lag = min(n // 2, int(sample_rate * 1.4))  # ~43 bpm
        if max_lag <= min_lag + 2:
            return None
        s = sig - np.mean(sig)
        denom = float(np.sum(s * s))
        if denom <= 1e-6:
            return None
        acorr = np.correlate(s, s, mode="full")[n - 1:] / denom
        region = acorr[min_lag:max_lag + 1]
        best_idx = int(np.argmax(region))
        best_corr = float(region[best_idx])
        if best_corr < 0.18:
            return None
        lag = min_lag + best_idx
        return 60.0 * sample_rate / float(lag)

    def compute_metrics(self, red_samples, ir_samples, sample_rate=25.0,
                        sys_bias=0.0, dia_bias=0.0, spo2_bias=0.0, bpm_bias=0.0):
        """
        Menghitung SpO2 (%), BPM, dan Estimasi Tekanan Darah (SYS/DIA, mmHg).
        Mengembalikan tuple: (spo2, bpm, sys, dia, status_code)
        status_code: 'ok' | 'no_finger' | 'weak_pulse'
        """
        if len(red_samples) < 25 or len(ir_samples) < 25:
            return None, None, None, None, "weak_pulse"

        red_arr = np.array(red_samples, dtype=np.float64)
        ir_arr  = np.array(ir_samples, dtype=np.float64)

        # Buang 10 sampel pertama (~0.4 detik transien saat jari baru diletakkan)
        if len(ir_arr) > 45:
            red_arr = red_arr[10:]
            ir_arr  = ir_arr[10:]

        # Ambil segmen di mana jari benar-benar menempel (IR >= ambang)
        finger_mask = ir_arr >= FINGER_IR_THRESHOLD
        if np.sum(finger_mask) < 25:
            print(f"[MAX30102 DIAG] No finger: max_ir={np.max(ir_arr):.0f}, mean_ir={np.mean(ir_arr):.0f}")
            return None, None, None, None, "no_finger"

        # Gunakan segmen kontigu saat jari menempel
        valid_idx = np.where(finger_mask)[0]
        red_arr = red_arr[valid_idx[0]:valid_idx[-1] + 1]
        ir_arr  = ir_arr[valid_idx[0]:valid_idx[-1] + 1]

        dc_red = float(np.mean(red_arr))
        dc_ir  = float(np.mean(ir_arr))
        if dc_red <= 0 or dc_ir < FINGER_IR_THRESHOLD:
            return None, None, None, None, "no_finger"

        # Detrending baseline drift (window ~0.9 detik)
        win = min(len(red_arr) // 2, int(round(sample_rate * 0.9)))
        if win % 2 == 0:
            win += 1
        win = max(5, win)

        kernel = np.ones(win) / win
        pad_r  = np.pad(red_arr, (win // 2, win // 2), mode="edge")
        pad_ir = np.pad(ir_arr, (win // 2, win // 2), mode="edge")
        trend_red = np.convolve(pad_r, kernel, mode="valid")[:len(red_arr)]
        trend_ir  = np.convolve(pad_ir, kernel, mode="valid")[:len(ir_arr)]

        ac_red = red_arr - trend_red
        ac_ir  = ir_arr - trend_ir

        # Redam lonjakan gerak sesaat (motion artifact clamping berbasis MAD) agar tidak merusak threshold puncak
        med_ir = np.median(ac_ir)
        mad_ir = np.median(np.abs(ac_ir - med_ir)) * 1.4826
        if mad_ir > 1.0:
            ac_ir_clean  = np.clip(ac_ir,  med_ir - 4.0 * mad_ir, med_ir + 4.0 * mad_ir)
            med_r = np.median(ac_red)
            mad_r = max(1.0, np.median(np.abs(ac_red - med_r)) * 1.4826)
            ac_red_clean = np.clip(ac_red, med_r - 4.0 * mad_r, med_r + 4.0 * mad_r)
        else:
            ac_ir_clean  = ac_ir
            ac_red_clean = ac_red

        rms_red = float(np.sqrt(np.mean(ac_red_clean ** 2)))
        rms_ir  = float(np.sqrt(np.mean(ac_ir_clean ** 2)))

        if rms_ir <= 0.5:
            print(f"[MAX30102 DIAG] Flat signal: dc_ir={dc_ir:.0f}, rms_ir={rms_ir:.2f}")
            return None, None, None, None, "weak_pulse"

        r_ratio = (rms_red / dc_red) / (rms_ir / dc_ir)

        # Perhitungan SpO2 (Maxim AN6845)
        if 0.35 <= r_ratio <= 0.85:
            calc_spo2 = -45.060 * (r_ratio ** 2) + 30.354 * r_ratio + 94.845
            if calc_spo2 < 90.0 or calc_spo2 > 100.0:
                calc_spo2 = 104.0 - 13.0 * r_ratio
        elif r_ratio < 0.35:
            calc_spo2 = 99.0
        else:
            calc_spo2 = max(88.0, 104.0 - 14.0 * min(r_ratio, 1.3))

        calc_spo2 += spo2_bias
        spo2 = int(round(np.clip(calc_spo2, 70.0, 100.0)))

        # Smoothing 3 titik untuk deteksi puncak detak jantung
        smooth_ir = np.convolve(ac_ir_clean, np.ones(3) / 3.0, mode="same")
        min_distance = max(7, int(sample_rate * 0.36))  # Maks ~165 bpm
        p90 = float(np.percentile(smooth_ir, 90))
        peak_threshold = max(2.0, 0.20 * p90)

        peaks = []
        for i in range(1, len(smooth_ir) - 1):
            if smooth_ir[i] >= smooth_ir[i - 1] and smooth_ir[i] > smooth_ir[i + 1]:
                if smooth_ir[i] >= peak_threshold:
                    if not peaks or (i - peaks[-1]) >= min_distance:
                        peaks.append(i)
                    elif smooth_ir[i] > smooth_ir[peaks[-1]]:
                        peaks[-1] = i

        raw_bpm = None
        rise_times = []
        decay_times = []

        if len(peaks) >= 2:
            intervals = np.diff(peaks) / sample_rate
            valid_iv = intervals[(intervals >= 0.35) & (intervals <= 1.5)]
            if len(valid_iv) >= 1:
                raw_bpm = 60.0 / float(np.median(valid_iv))

            for k in range(len(peaks) - 1):
                p1, p2 = peaks[k], peaks[k + 1]
                segment = smooth_ir[p1:p2]
                if len(segment) > 2:
                    v_idx = p1 + int(np.argmin(segment))
                    t_decay = (v_idx - p1) / sample_rate
                    t_rise  = (p2 - v_idx) / sample_rate
                    if 0.06 <= t_rise <= 0.35 and 0.18 <= t_decay <= 0.90:
                        rise_times.append(t_rise)
                        decay_times.append(t_decay)

        # Jika puncak lokal kurang akibat penekanan jari agak kuat, gunakan autokorelasi periodik
        if raw_bpm is None:
            raw_bpm = self._estimate_bpm_autocorr(smooth_ir, sample_rate)

        print(f"[MAX30102 DIAG] n={len(ir_arr)}, dc_ir={dc_ir:.0f}, rms_ir={rms_ir:.1f}, peaks={len(peaks)}, raw_bpm={raw_bpm}")

        if raw_bpm is None:
            return None, None, None, None, "weak_pulse"

        bpm = int(round(np.clip(raw_bpm + bpm_bias, 40.0, 180.0)))

        ts = float(np.median(rise_times)) if rise_times else 0.13
        td = float(np.median(decay_times)) if decay_times else 0.45
        rsd = ts / max(0.01, td)

        delta_sbp = 0.50 * (bpm - 72.0) - 80.0 * (ts - 0.13) + 18.0 * (rsd - 0.30)
        delta_dbp = 0.30 * (bpm - 72.0) - 45.0 * (td - 0.45)

        sys_val = int(round(np.clip(118.0 + delta_sbp + sys_bias, 80.0, 200.0)))
        dia_val = int(round(np.clip(76.0 + delta_dbp + dia_bias, 50.0, 130.0)))
        if (sys_val - dia_val) < 25:
            dia_val = sys_val - 30

        return spo2, bpm, sys_val, dia_val, "ok"

    def close(self):
        try:
            if self.bus:
                with self.i2c_lock:
                    self.bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x80)
                    self.bus.close()
        except Exception:
            pass
