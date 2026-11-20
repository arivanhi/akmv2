#!/usr/bin/env python3
"""
driver_max30102.py - Driver & Processing Engine untuk Sensor MAX30102
Diadaptasi dari Kopersakti-UDINUS untuk AKMv2:
- Thread-safe I2C lock & auto-reconnect saat sensor baru dipasang
- Mendukung bpm_bias, spo2_bias, sys_bias, dia_bias
- Fallback BPM acak dinonaktifkan (mengembalikan jujur False jika denyut tidak terdeteksi)
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


class MAX30102:
    def __init__(self, bus_num=1, i2c_lock=None):
        self.bus_num = bus_num
        self.i2c_lock = i2c_lock or threading.Lock()
        self.bus = None
        self.connected = False
        self._init_sensor()

    def _init_sensor(self):
        if not HAS_SMBUS:
            self.connected = False
            return False
        bus = None
        try:
            with self.i2c_lock:
                if self.bus is not None:
                    try:
                        self.bus.close()
                    except Exception:
                        pass
                bus = smbus2.SMBus(self.bus_num)
                # Reset MAX30102
                bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x40)
                time.sleep(0.05)

                # FIFO Config: Sample avg = 4 (0x40), FIFO rollover enable (0x10) -> 0x50 (25 Hz effective)
                bus.write_byte_data(MAX_ADDR, REG_FIFO_CONFIG, 0x50)
                # Mode: SpO2 mode (Red + IR) = 0x03
                bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x03)
                # SpO2 Config: ADC range 4096nA, 100 Hz, 411us pulse width (18 bit) -> 0x27
                bus.write_byte_data(MAX_ADDR, REG_SPO2_CONFIG, 0x27)
                # LED Currents: ~7.6mA (0x2F)
                bus.write_byte_data(MAX_ADDR, REG_LED1_PA, 0x2F)
                bus.write_byte_data(MAX_ADDR, REG_LED2_PA, 0x2F)

                bus.write_byte_data(MAX_ADDR, REG_FIFO_WR_PTR, 0x00)
                bus.write_byte_data(MAX_ADDR, REG_OVF_COUNTER, 0x00)
                bus.write_byte_data(MAX_ADDR, REG_FIFO_RD_PTR, 0x00)
                # Standby LED OFF
                bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x80)

                self.bus = bus
                self.connected = True
                print("[MAX30102] Berhasil diinisialisasi (Standby LED OFF).")
                return True
        except Exception as e:
            print(f"[MAX30102 ERROR] Inisialisasi gagal: {e}")
            if bus is not None:
                try:
                    bus.close()
                except Exception:
                    pass
            self.bus = None
            self.connected = False
            return False

    def ensure_connected(self):
        if self.connected and self.bus is not None:
            return True
        return self._init_sensor()

    def enable_leds(self):
        if not self.ensure_connected():
            return False
        try:
            with self.i2c_lock:
                self.bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x03)
            print("[MAX30102] LED Red & IR dinyalakan untuk pengukuran.")
            return True
        except Exception as e:
            print(f"[MAX30102 LED ON ERROR] {e}")
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
        if not self.connected or self.bus is None:
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

    def compute_metrics(self, red_samples, ir_samples, sample_rate=25.0,
                        sys_bias=0.0, dia_bias=0.0, spo2_bias=0.0, bpm_bias=0.0):
        """
        Menghitung SpO2 (%), BPM, dan Estimasi Tekanan Darah (SYS/DIA, mmHg).
        Mengembalikan tuple: (spo2, bpm, sys, dia, is_finger_detected)
        """
        if len(red_samples) < 20 or len(ir_samples) < 20:
            return None, None, None, None, False

        red_arr = np.array(red_samples, dtype=np.float64)
        ir_arr  = np.array(ir_samples, dtype=np.float64)

        mean_ir = np.mean(ir_arr)
        if mean_ir < 10000.0:
            return None, None, None, None, False

        dc_red = np.mean(red_arr)
        dc_ir  = np.mean(ir_arr)

        win = min(len(red_arr) // 2, int(sample_rate))
        if win % 2 == 0:
            win += 1
        if win < 3:
            win = 3

        kernel = np.ones(win) / win
        pad_r = np.pad(red_arr, (win // 2, win // 2), mode='edge')
        pad_ir = np.pad(ir_arr, (win // 2, win // 2), mode='edge')
        trend_red = np.convolve(pad_r, kernel, mode='valid')[:len(red_arr)]
        trend_ir  = np.convolve(pad_ir, kernel, mode='valid')[:len(ir_arr)]

        ac_red = red_arr - trend_red
        ac_ir  = ir_arr - trend_ir

        rms_red = np.sqrt(np.mean(ac_red ** 2))
        rms_ir  = np.sqrt(np.mean(ac_ir ** 2))

        if dc_red <= 0 or dc_ir <= 0 or rms_ir <= 0:
            return None, None, None, None, False

        r_ratio = (rms_red / dc_red) / (rms_ir / dc_ir)

        # Perhitungan SpO2 (Maxim Integrated AN6845)
        if 0.35 <= r_ratio <= 0.85:
            calc_spo2 = -45.060 * (r_ratio ** 2) + 30.354 * r_ratio + 94.845
            if calc_spo2 < 90.0 or calc_spo2 > 100.0:
                calc_spo2 = 104.0 - 13.0 * r_ratio
        elif r_ratio < 0.35:
            calc_spo2 = 99.0
        else:
            calc_spo2 = 104.0 - 17.0 * min(r_ratio, 2.0)

        calc_spo2 += spo2_bias
        spo2 = int(round(np.clip(calc_spo2, 70.0, 100.0)))

        # Perhitungan BPM & Morfologi Gelombang Pulsa (PWA)
        window = 3
        smooth_ir = np.convolve(ac_ir, np.ones(window) / window, mode='same')

        min_distance = int(sample_rate * 0.40)  # Maks ~150 bpm
        peaks = []
        peak_threshold = 0.25 * np.max(smooth_ir) if np.max(smooth_ir) > 0 else 0

        for i in range(1, len(smooth_ir) - 1):
            if smooth_ir[i] > smooth_ir[i - 1] and smooth_ir[i] > smooth_ir[i + 1]:
                if smooth_ir[i] > peak_threshold:
                    if not peaks or (i - peaks[-1]) >= min_distance:
                        peaks.append(i)

        if len(peaks) < 2:
            # Tidak memakai angka acak; laporkan gagal deteksi denyut
            return None, None, None, None, False

        intervals = np.diff(peaks) / sample_rate
        avg_interval = np.median(intervals)
        if avg_interval <= 0:
            return None, None, None, None, False

        bpm = int(round(np.clip((60.0 / avg_interval) + bpm_bias, 40.0, 180.0)))

        rise_times = []
        decay_times = []
        for k in range(len(peaks) - 1):
            p1 = peaks[k]
            p2 = peaks[k + 1]
            segment = smooth_ir[p1:p2]
            if len(segment) > 2:
                v_idx = p1 + np.argmin(segment)
                t_decay = (v_idx - p1) / sample_rate
                t_rise  = (p2 - v_idx) / sample_rate
                if 0.06 <= t_rise <= 0.30 and 0.20 <= t_decay <= 0.80:
                    rise_times.append(t_rise)
                    decay_times.append(t_decay)

        ts = np.median(rise_times) if rise_times else 0.13
        td = np.median(decay_times) if decay_times else 0.45
        rsd = ts / max(0.01, td)

        delta_sbp = 0.50 * (bpm - 72.0) - 80.0 * (ts - 0.13) + 18.0 * (rsd - 0.30)
        delta_dbp = 0.30 * (bpm - 72.0) - 45.0 * (td - 0.45)

        base_sys = 118.0 + delta_sbp + sys_bias
        base_dia = 76.0 + delta_dbp + dia_bias

        sys_val = int(round(np.clip(base_sys, 80.0, 200.0)))
        dia_val = int(round(np.clip(base_dia, 50.0, 130.0)))

        if (sys_val - dia_val) < 25:
            dia_val = sys_val - 30

        return spo2, bpm, sys_val, dia_val, True

    def close(self):
        try:
            if self.bus:
                with self.i2c_lock:
                    self.bus.write_byte_data(MAX_ADDR, REG_MODE_CONFIG, 0x80)
                    self.bus.close()
        except Exception:
            pass
