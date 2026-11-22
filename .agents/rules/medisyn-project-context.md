# Konteks Proyek MEDISYN (AKMv2)

Dokumen aturan konteks otomatis untuk Google Antigravity saat membuka workspace `akmv2` di PC mana pun.

## 1. Identitas & Arsitektur Sistem
- **Nama Sistem**: **MEDISYN** (*Medical System Integration* — pengembangan lanjutan penggabungan UI/fitur lengkap `AKM` dengan engine sensor I2C `Kopersakti-UDINUS`, tanpa logo Dinkes).
- **Repositori GitHub**: `https://github.com/arivanhi/akmv2.git` (branch utama: `main`).
- **Perangkat Target (Live Raspberry Pi)**:
  - Model: Raspberry Pi 5 (8GB RAM), OS Debian 13 (Trixie) arm64, layar sentuh 7" `QDTECH MPI7002` (`1024x600`).
  - Host / SSH: `admin@192.168.100.19` (sebelumnya `192.168.100.166`, password: `admin`, direktori aplikasi: `/home/admin/akmv2`).
  - Kiosk Autostart: `~/.config/labwc/autostart` menjalankan `/home/admin/akmv2/kiosk_autostart.sh` (Chromium fullscreen `http://127.0.0.1:5000`).
  - Penting: Jangan memutus koneksi `wlan0` saat mengakses Raspi melalui SSH.

## 2. Layanan Docker (`docker-compose.yml`)
1. **`akmv2_web`**: Flask-SocketIO (`0.0.0.0:5000`, `network_mode: host`, `privileged: true`, mount `.:/app` dan `/var/run/dbus`). Menjalankan `sensor_publisher.py`, `ble_scale_service.py`, dan `app.py` via `start.sh`.
   - Login Web Default: `admin` / `admin123` (Administrator), `operator` / `operator123` (Operator).
2. **`akmv2_postgres`**: PostgreSQL 15 (`5433:5432`), DB: `health_monitoring_db`, User: `admin`, Password: `akmv2_udinus_password`.
3. **`akmv2_mqtt`**: Eclipse Mosquitto 2 (`1883:1883`).
4. **`akmv2_adminer`**: Adminer (`8080:8080`, Sistem: `PostgreSQL`, Server: `db`, Pengguna: `admin`, Sandi: `akmv2_udinus_password`, DB: `health_monitoring_db`).

## 3. Konfigurasi Sensor I2C (`/dev/i2c-1`) & Literatur Spektral
1. **`0x5A` — `MLX90614` (Suhu Tubuh Inframerah)**:
   - Durasi scan `5 detik` (`1 Hz`). Deteksi bus wajib menggunakan SMBus `read_word_data(0x5A, 0x07)` (bukan `read_byte`).
2. **`0x57` — `MAX30102` (Oximeter SpO2, BPM, Estimasi Tensi SYS/DIA)** & **Tensimeter BLE `Omron HEM-7156T`**:
   - **2 Metode Pembacaan Tekanan Darah (`templates/measure.html`)**:
     1. **Tensimeter Omron HEM-7156T (Bluetooth BLE)**:
        - Nama perangkat: `BLEsmart_0000057A005FBFE225DC`, MAC Default: `00:5F:BF:E2:25:DC` (Service `0x1810`, Characteristic `0x2A35` IEEE-11073 16-bit SFLOAT, Time Sync `0x2A2B`, RACP `0x2A52`).
        - Dikelola oleh `ble_scale_service.py` dengan *single-radio adapter lock* (`hci0`) via topik MQTT `medical/cmd/omron` -> `medical/omron`, serta dicatat ke `raw_sensor_data` dengan `sensor_type = 'tensi_omron_ble'`.
        - Fitur **Scan & Pair Omron (`-P-`)** tersedia di halaman **Pengaturan** (`/settings`) maupun langsung di modal **Pilih Metode Pengukuran Tekanan Darah** (`/measure`).
     2. **Sensor Optik `MAX30102` (`0x57`)**:
        - Durasi scan `6 detik` (ADC internal `100 Hz` dengan FIFO averaging `4` = output `25 Hz` ≈ `150 sampel` Red & IR).
        - `enable_leds()` selalu menulis ulang register `FIFO=0x50`, `SPO2=0x47`, `LED=0x37` untuk mencegah reset POR saat sensor cabut-pasang.
3. **`0x49` — `AS7265x` Triad 18-Channel (`410–940 nm`, Gula Darah, Kolesterol, Asam Urat)**:
   - Sampling kontinu optimal **`~3.3 – 3.5 Hz`** (`INTEG = 49` ≈ `278 ms/frame`, `Gain = 64x`, arus 3 LED `25 mA`), durasi scan **`7 detik`** (`~20–25 frame` × 18 kanal).
   - Pra-pemrosesan: buang `2 frame` transien awal (*settling time*), saring *outlier* goyangan jari per kanal dengan metode **IQR (*Interquartile Range*)**, dan hitung normalisasi **L2**.
   - **Kanal Aktif Berbasis Literatur**:
     - **Gula Darah**: `D(485)`, `I(645)`, `V(810-isosbestik)`, `W(860)`, `K(900)`, `L(940)` (*Shokrekhodaei et al., 2021; Hina & Saadeh, 2022*).
     - **Kolesterol**: `R(610)`, `S(680)`, `V(810)`, `W(860)`, `L(940)` (*Arnita et al., 2026; Umar et al., 2020*).
     - **Asam Urat**: `A(410)`, `B(435)`, `U(760)`, `V(810)`, `K(900)`, `L(940)` (*Umar & Alyah, 2020; Sumarti et al., 2024; Lin et al., 2022*).

## 4. Ground-Truth & Pipeline Machine Learning (`raw_sensor_data`)
- Tabel `raw_sensor_data` menyimpan `raw_values` (18-kanal bersih IQR), `raw_frames` (matriks penuh `N×18` frame + gelombang PPG + Hz), `context_features` (`umur`, `jenis_kelamin`, `suhu`, `spo2`, `bpm`, `sys`, `dia`), serta `ground_truth_value` & `ground_truth_source`.
- Input *Ground-Truth* tersedia langsung di kartu hasil pengukuran (`templates/measure.html`) maupun di tabel halaman **Evaluasi AI** (`templates/evaluation.html`).
- Ekspor dataset siap latih tersedia di `/api/ml_dataset/export_csv` dan `/api/ml_dataset/export_json`. Pelatihan model otomatis (`RandomForest + Ridge` -> `model/as7265x_<target>.joblib`) tersedia melalui `/api/ml_dataset/train_model` minimal 8 sampel *Ground-Truth*.

## 5. UX Khusus & Aset Logo
- **Touch Virtual Keyboard**: `static/virtual-keyboard.js` menangani input layar sentuh `QDTECH MPI7002` secara native di web.
- **Alur Konfirmasi Pengukuran**: Kartu hasil menampilkan tombol `[Ulang]` dan `[Selesai]` -> memunculkan modal konfirmasi *"Apakah hasil pengukuran berikut sudah sesuai semua?"* -> mengunci parameter yang sudah sesuai dan mengubah tombol pilihannya menjadi **abu-abu (`grey`)**.
- **Sidebar & Logo**: Saat sidebar di-*minimize*, hanya menampilkan ikon huruf **`M` MEDISYN** (`static/img/logo_medisyn_icon.png`). File master PNG resolusi tinggi `300 DPI` tersimpan di folder `logo/` (`logo_m.png`, `logo_medisyn_penuh.png`, `logo_udinus.png`).
- Riwayat percakapan lengkap pembangunan sistem ini tersimpan di `HANDOFF_CHAT_HISTORY.md`.
