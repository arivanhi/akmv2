#!/bin/bash
set -e

echo "=== Memulai Layanan AKMv2 (UI AKM + Sensor I2C Kopersakti) ==="

# 1. Jalankan Daemon Sensor I2C Kopersakti (MLX90614, MAX30102, AS7265x) di background
python3 -u sensor_publisher.py &

# 2. Jalankan Daemon Opsional BLE Timbangan & Tinggi Badan di background
python3 -u ble_scale_service.py &

# 3. Jalankan Server Utama Flask-SocketIO di foreground
exec python3 -u app.py
