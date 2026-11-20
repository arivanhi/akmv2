# AKMv2 — Koper Sakti Terpadu (UI AKM + Sensor I2C Kopersakti)

Perpaduan arsitektur web **AKM** (`Flask-SocketIO` + `PostgreSQL 15` + `Mosquitto MQTT` + `Adminer` + `ESC/POS Thermal Printer` + `API Dinas Kesehatan`) dengan engine pembacaan sensor optik I2C **Kopersakti-UDINUS** (`MLX90614`, `MAX30102`, `AS7265x 18-Channel`) di atas **Docker**.

## Fitur Utama
1. **Antarmuka & Alur Kerja AKM Lengkap**: Login multi-role (`admin` & `operator`), registrasi pasien dengan autocomplete Provinsi/Kecamatan/Kelurahan Kota Semarang, riwayat & grafik `Chart.js`, edit/hapus pasien, cetak struk thermal USB ESC/POS, ekspor CSV, dan kirim data ke API Puskesmas/Dinkes.
2. **9 Parameter Pemeriksaan**:
   - **Spektroskopi Optik 18-Kanal (`AS7265x`, I2C `0x49`)**: Gula Darah, Kolesterol, Asam Urat (mendukung ukur satuan maupun **Paket Cepat Spektro 3-in-1**).
   - **Oximeter & Morfologi PPG (`MAX30102`, I2C `0x57`)**: Tekanan Darah (`SYS/DIA`), SpO2 (`%`), Detak Jantung (`BPM`) (mendukung ukur satuan maupun **Paket Cepat Oximeter 3-in-1**).
   - **Termometer Inframerah (`MLX90614`, I2C `0x5A`)**: Suhu Tubuh (`°C`).
   - **Antropometri**: Tinggi Badan (`cm`) & Berat Badan (`kg`) via sensor BLE otomatis atau **Input Manual**.
3. **Validasi Sensor Jujur & Kalibrasi Terpusat**: Tanpa angka dummy/acak saat sensor terlepas; status fisik ketiga alamat I2C (`0x49`, `0x57`, `0x5A`) serta offset kalibrasi (`calibration_config.json`) dapat dikelola langsung dari menu **Pengaturan**.
4. **Perbaikan Koneksi WiFi Raspberry Pi (Debian Bookworm)**: Mengatasi bug keyring & konflik profil `nmcli` melalui `psk-flags=0` pada D-Bus `NetworkManager` host.

## Akun Default
- **Administrator**: `admin` / `admin123`
- **Operator**: `operator` / `operator123`

## Cara Menjalankan (Docker)
```bash
cd akmv2
docker compose up -d --build
```
- **Aplikasi Web / Kiosk**: http://localhost:5000
- **Adminer (GUI Database)**: http://localhost:8080 (System: `PostgreSQL`, Server: `db`, Username: `admin`, Password: `akmv2_udinus_password`, Database: `health_monitoring_db`)
