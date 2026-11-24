# Alat Ukur Tinggi Badan Ultrasonik BLE — MEDISYN (`bleScaleHeight`)

Firmware mandiri (*standalone*) dan terintegrasi BLE untuk **Alat Ukur Tinggi Badan Digital (Stadiometer Ultrasonik)** menggunakan **ESP32-C3 Super Mini**, sensor jarak **HC-SR04**, layar **LCD OLED 0.91 inch (`128×32` I2C SSD1306)**, serta **3 Push Button**.

Alat ini dapat dipakai **secara mandiri tanpa MEDISYN** (pembacaan langsung, penguncian `HOLD`, dan kalibrasi tampil penuh di layar OLED), sekaligus otomatis siap terhubung secara nirkabel ke **MEDISYN (`ble_scale_service.py`)** melalui Bluetooth Low Energy (`bleScaleHeight`).

---

## 1. Tabel Wiring Lengkap (`ESP32-C3 Super Mini`)

> **Pemilihan Pin Aman ESP32-C3 Super Mini:**
> Pin strapping bawaan board (`GPIO2`, `GPIO8` LED onboard, dan `GPIO9` tombol BOOT) sengaja **tidak dipakai** untuk sensor/tombol eksternal agar proses *booting* dan *upload* program selalu lancar.

### A. Ringkasan Koneksi Per Komponen

| Komponen | Pin Komponen | Pin ESP32-C3 Super Mini | Keterangan Elektrikal |
| :--- | :--- | :--- | :--- |
| **LCD OLED 0.91"**<br>*(I2C SSD1306 128×32, `0x3C`)* | `VCC` | **`3.3V`** | Catu daya logika `3.3V` |
| | `GND` | **`GND`** | Ground bersama |
| | `SDA` | **`GPIO6`** | Jalur data I2C (`Wire.begin(6, 7)`) |
| | `SCL` | **`GPIO7`** | Jalur clock I2C |
| **Sensor Ultrasonik**<br>**`HC-SR04`** | `VCC` | **`5V`** *(atau `3.3V` khusus versi `HC-SR04P`/`+`)* | `HC-SR04` standar wajib `5V`; versi `HC-SR04P`/`RCWL-1601` bisa langsung `3.3V` |
| | `GND` | **`GND`** | Ground bersama |
| | `TRIG` | **`GPIO4`** | Output pemicu `3.3V` langsung ke `TRIG` |
| | `ECHO` | **`GPIO5`** *(via Pembagi Tegangan jika `VCC = 5V`)* | **Wajib** lewat resistor `1 kΩ` + `2 kΩ` jika `VCC` sensor `5V` (lihat skema di bawah) |
| **Push Button 1**<br>*(UKUR / HOLD / `+`)* | Kaki 1 | **`GPIO0`** | Menggunakan `INPUT_PULLUP` internal (tanpa resistor luar) |
| | Kaki 2 | **`GND`** | Aktif `LOW` saat ditekan |
| **Push Button 2**<br>*(KALIBRASI / TARE)* | Kaki 1 | **`GPIO1`** | Menggunakan `INPUT_PULLUP` internal (tanpa resistor luar) |
| | Kaki 2 | **`GND`** | Aktif `LOW` saat ditekan |
| **Push Button 3**<br>*(RESET / MODE / `-`)* | Kaki 1 | **`GPIO3`** | Menggunakan `INPUT_PULLUP` internal (tanpa resistor luar) |
| | Kaki 2 | **`GND`** | Aktif `LOW` saat ditekan |
| **LED Status BLE** | *(Internal)* | **`GPIO8`** *(Onboard)* | LED biru bawaan board (Berkedip = *Advertising*, Menyala = *BLE Connected*) |

---

### B. Catatan Penting Jalur `ECHO` Sensor `HC-SR04` (`5V` ke `3.3V`)

Pin GPIO pada **ESP32-C3 Super Mini** bekerja pada tegangan maksimum **`3.3V`**:
1. **Jika menggunakan `HC-SR04` standar (`VCC` dihubungkan ke `5V`)**:
   Pin `ECHO` mengeluarkan sinyal `5V`. Pasang **2 resistor pembagi tegangan** agar turun menjadi `~3.3V`:
   ```text
   HC-SR04 [ECHO] ───[ Resistor 1 kΩ ]───┬───► GPIO5 (ESP32-C3)
                                         │
                                  [ Resistor 2 kΩ (atau 2.2 kΩ) ]
                                         │
                                        GND
   ```
2. **Jika menggunakan `HC-SR04P` / `HC-SR04+` (mendukung `3.3V`)**:
   Hubungkan `VCC` sensor ke pin **`3.3V`** ESP32-C3, maka pin `ECHO` aman dihubungkan **langsung** ke **`GPIO5`** tanpa resistor tambahan.

---

## 2. Fungsi 3 Push Button

| Tombol | Pin | Tekan Singkat (*Short Press*) | Tekan & Tahan `1.5 Detik` (*Long Press*) | Fungsi Saat Mode `SET TIANG` |
| :--- | :--- | :--- | :--- | :--- |
| **Button 1 (`UKUR / +`)** | `GPIO0` | Mengunci bacaan tinggi badan di layar (**`HOLD`**) & mengirim nilai ke BLE (jika sudah `HOLD`, mengirim ulang ke BLE) | — | Menambah tinggi tiang **`+0.1 cm`** *(tahan = cepat `+0.5 cm`)* |
| **Button 2 (`KALIBRASI`)** | `GPIO1` | Masuk / Simpan & Keluar dari mode atur tinggi tiang manual (**`SET TIANG`**) | **Auto-Tare Lantai Otomatis**: mengukur jarak ke lantai kosong (16 sampel), menyimpan ke memori permanen (`NVS Flash`), dan mengenolkan tampilan jadi `0.0 cm` | Menyimpan nilai tinggi tiang ke memori Flash & kembali ke `LIVE` |
| **Button 3 (`RESET / -`)** | `GPIO3` | Membuka penguncian **`HOLD`** agar kembali ke pengukuran **`LIVE`** untuk pasien berikutnya | Mengubah tampilan antara **Mode Tinggi Badan (`LIVE`)** dan **Mode Diagnostik Jarak Sensor (`JARAK`)** | Mengurangi tinggi tiang **`-0.1 cm`** *(tahan = cepat `-0.5 cm`)* |

---

## 3. Spesifikasi Protokol BLE (Kompatibel MEDISYN & nRF Connect)

Firmware ini sudah disesuaikan langsung dengan `ble_scale_service.py` pada sistem **MEDISYN**:
- **Nama Perangkat BLE**: `bleScaleHeight`
- **Service UUID**: `4fafc201-1fb5-459e-8fcc-c5c9c331914b`
- **Characteristic UUID**: `beb5483e-36e1-4688-b7f5-ea07361b26a8` (`READ` | `NOTIFY`)
- **Payload**: Teks UTF-8 angka desimal dalam satuan `cm`, misalnya `"168.5"`.
- **Cara Uji Tanpa MEDISYN**: Buka aplikasi **nRF Connect** di HP Android, cari perangkat **`bleScaleHeight`**, tekan **Connect**, buka Service `4fafc201...`, lalu aktifkan ikon panah tiga (**Notify**) pada karakteristik `beb5483e...`. Saat **Button 1 (`UKUR`)** ditekan pada tinggi $\ge 30\text{ cm}$, angka tinggi badan langsung masuk ke HP.

---

## 4. Persiapan Arduino IDE (Saat Nanti Akan Di-upload)

1. Pastikan **Board Manager** sudah terpasang **`esp32` by Espressif Systems** (mendukung versi `2.0.x` maupun `3.x`).
2. Buka menu **Sketch → Include Library → Manage Libraries...**, lalu instal:
   - **`Adafruit SSD1306`** (oleh Adafruit)
   - **`Adafruit GFX Library`** (oleh Adafruit)
3. Pengaturan menu **Tools**:
   - **Board**: `ESP32C3 Dev Module`
   - **USB CDC On Boot**: `Enabled` *(agar `Serial Monitor` tampil lewat USB Type-C)*
4. Buka file `esp32c3_height_ble.ino` dan siap di-*Verify/Compile* maupun di-*Upload*.
