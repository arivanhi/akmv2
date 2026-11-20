#!/usr/bin/env python3
"""
app.py - Server Utama AKMv2 (Koper Sakti UDINUS x Dinkes)
Perpaduan UI & Fitur Lengkap AKM (Flask-SocketIO + PostgreSQL + Printer ESC/POS + Export + API Dinkes)
dengan Engine Pembacaan Sensor I2C Kopersakti (MLX90614, MAX30102, AS7265x) via MQTT.
"""

import os
import io
import csv
import time
import json
import subprocess
from datetime import date, datetime, timedelta
from dateutil.relativedelta import relativedelta

import bcrypt
import joblib
import numpy as np
import psycopg2
import requests
import xgboost as xgb
import paho.mqtt.client as mqtt
from dotenv import load_dotenv
from flask import (
    Flask, Response, flash, g, jsonify, redirect,
    render_template, request, session, url_for
)
from flask_socketio import SocketIO, emit

try:
    from escpos.printer import Usb
    HAS_ESCPOS = True
except ImportError:
    HAS_ESCPOS = False

try:
    import smbus2
    HAS_SMBUS = True
except ImportError:
    HAS_SMBUS = False

load_dotenv()

BASE_DIR         = os.path.dirname(os.path.abspath(__file__))
CALIBRATION_FILE = os.path.join(BASE_DIR, "calibration_config.json")
I2C_BUS_NUM      = int(os.getenv("I2C_BUS_NUM", "1"))

MQTT_BROKER_HOST      = os.getenv("MQTT_BROKER_HOST", "127.0.0.1")
MQTT_BROKER_PORT      = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_COMMAND_TOPIC    = "akm/command"
MQTT_PREDICTION_TOPIC = "akm/prediction_data"

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "akmv2_kopersakti_udinus_secret_key_2026")
socketio = SocketIO(app, async_mode="threading", cors_allowed_origins="*")

active_patient_id   = None
active_patient_name = None
ble_is_connected    = False
printer_instance    = None

# ── Muat Model ML (Digunakan untuk halaman Evaluasi AI AKM) ──
models_xgb = {}
models_knn = {}
try:
    models_xgb["asam_urat"] = xgb.XGBRegressor()
    models_xgb["asam_urat"].load_model(os.path.join(BASE_DIR, "model/xgboost_asam_urat_regressor.json"))
    models_knn["asam_urat"] = joblib.load(os.path.join(BASE_DIR, "model/knn_asam_urat_regressor.pkl"))

    models_xgb["cholesterol"] = xgb.XGBRegressor()
    models_xgb["cholesterol"].load_model(os.path.join(BASE_DIR, "model/xgboost_kolesterol_regressor.json"))
    models_knn["cholesterol"] = joblib.load(os.path.join(BASE_DIR, "model/knn_kolesterol_regressor.pkl"))

    models_knn["gula_darah"] = joblib.load(os.path.join(BASE_DIR, "model/pipeline_glucose_model.pkl"))
    print("-> Artefak model Evaluasi AI berhasil dimuat.")
except Exception as e:
    print(f"[MODEL WARN] Beberapa artefak evaluasi tidak dimuat: {e}")


# ── Database Helpers ─────────────────────────────────────────
def get_db_connection():
    return psycopg2.connect(
        dbname=os.getenv("DB_NAME", "health_monitoring_db"),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "akmv2_udinus_password"),
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=os.getenv("DB_PORT", "5433")
    )


def init_database_and_defaults():
    """Memastikan seluruh tabel, kolom baru AKMv2, dan akun default siap saat startup."""
    for attempt in range(1, 20):
        try:
            conn = get_db_connection()
            cur = conn.cursor()

            # Pastikan kolom baru spo2_value & temperature_value tersedia jika upgrade DB lama
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS blood_pressure_sys INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS blood_pressure_dia INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS blood_pressure_pulse INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS spo2_value FLOAT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS temperature_value FLOAT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS height FLOAT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS weight FLOAT DEFAULT 0;")

            cur.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS tanggal_lahir DATE;")
            cur.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS email VARCHAR(255);")
            cur.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS provinsi VARCHAR(100);")
            cur.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS kecamatan VARCHAR(100);")
            cur.execute("ALTER TABLE patients ADD COLUMN IF NOT EXISTS kelurahan VARCHAR(100);")

            cur.execute("""
                CREATE TABLE IF NOT EXISTS raw_sensor_data (
                    id SERIAL PRIMARY KEY,
                    patient_id INT REFERENCES patients(id) ON DELETE SET NULL,
                    sensor_type VARCHAR(50) NOT NULL,
                    raw_values JSONB,
                    predicted_value FLOAT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS system_config (
                    key VARCHAR(50) PRIMARY KEY,
                    value TEXT
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS api_history (
                    url TEXT PRIMARY KEY,
                    last_used TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

            # Buat akun default admin (admin / admin123) dan operator (operator / operator123)
            cur.execute("SELECT 1 FROM users WHERE username = 'admin'")
            if cur.fetchone() is None:
                pw_admin = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode("utf-8")
                cur.execute(
                    """
                    INSERT INTO users (nama, username, email, no_hp, password_hash, role, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    ("Admin Utama", "admin", "admin@udinus.ac.id", "081234567890", pw_admin, "admin", True)
                )
                print("-> Akun default 'admin' (pass: admin123) dibuat.")

            cur.execute("SELECT 1 FROM users WHERE username = 'operator'")
            if cur.fetchone() is None:
                pw_op = bcrypt.hashpw(b"operator123", bcrypt.gensalt()).decode("utf-8")
                cur.execute(
                    """
                    INSERT INTO users (nama, username, email, no_hp, password_hash, role, is_active)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    """,
                    ("Operator Posbindu", "operator", "operator@udinus.ac.id", "081234567891", pw_op, "operator", True)
                )
                print("-> Akun default 'operator' (pass: operator123) dibuat.")

            conn.commit()
            cur.close()
            conn.close()
            print("-> Database PostgreSQL AKMv2 siap.")
            return
        except Exception as e:
            print(f"[DB INIT] Menunggu PostgreSQL ({attempt}/20): {e}")
            time.sleep(2)


def save_raw_sensor_log(sensor_type, raw_vals, predicted_val):
    if not active_patient_id:
        return
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO raw_sensor_data (patient_id, sensor_type, raw_values, predicted_value)
            VALUES (%s, %s, %s, %s)
            """,
            (active_patient_id, sensor_type, json.dumps(raw_vals), float(predicted_val))
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[RAW LOG WARN] {e}")


# ── Printer Thermal ESC/POS ──────────────────────────────────
def init_printer_connection():
    global printer_instance
    if not HAS_ESCPOS:
        printer_instance = None
        return False
    try:
        if printer_instance:
            try:
                printer_instance.close()
            except Exception:
                pass
        printer_instance = Usb(0x0416, 0x5011, 0, 0x81, 0x03)
        print("-> [PRINTER] Berhasil Terhubung!")
        return True
    except Exception as e:
        print(f"-> [PRINTER] Belum terdeteksi: {e}")
        printer_instance = None
        return False


# ── Kalibrasi Helper ─────────────────────────────────────────
def load_calibration():
    default_cfg = {
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
                default_cfg.update(data)
        except Exception:
            pass
    return default_cfg


# ── Helper Umur & API URL ────────────────────────────────────
def get_active_api_url():
    url = None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT value FROM system_config WHERE key = 'api_endpoint'")
        row = cur.fetchone()
        if row:
            url = row[0]
        cur.close()
        conn.close()
    except Exception:
        pass
    return url or os.getenv("EXTERNAL_API_URL")


def calculate_age_detail(dob):
    if not dob:
        return "-"
    today = date.today()
    if isinstance(dob, str):
        dob = datetime.strptime(dob, "%Y-%m-%d").date()
    delta = relativedelta(today, dob)
    return f"{delta.years} Thn {delta.months} Bln {delta.days} Hr"


def calculate_age_years(dob):
    if not dob:
        return 0
    today = date.today()
    if isinstance(dob, str):
        dob = datetime.strptime(dob, "%Y-%m-%d").date()
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


# ── MQTT Client (Menerima Data Sensor Kopersakti & BLE) ──────
mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)


def on_mqtt_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[MQTT WEB] Terhubung ke Broker ({reason_code})")
    client.subscribe("medical/suhu")
    client.subscribe("medical/spo2")
    client.subscribe("medical/spektro")
    client.subscribe(MQTT_PREDICTION_TOPIC)
    client.subscribe("akm/connect")


def on_mqtt_message(client, userdata, msg):
    global ble_is_connected
    topic = msg.topic
    payload_str = msg.payload.decode("utf-8", errors="ignore")

    if topic == "akm/connect":
        ble_is_connected = (payload_str == "connected_ble_scale")
        return

    try:
        data = json.loads(payload_str)
    except Exception:
        return

    status = data.get("status")
    target_mode = data.get("target_mode", "")

    # 1. Progress 5-detik dari sensor_publisher Kopersakti
    if status == "scanning":
        socketio.emit("measurement_progress", {
            "remaining": data.get("remaining", 0),
            "progress": data.get("progress", 0)
        })
        return

    # 2. Error jujur jika sensor I2C/BLE belum terpasang atau jari tidak menempel
    if status == "error":
        socketio.emit("measurement_error", {
            "type": data.get("type", target_mode),
            "message": data.get("message", "Pembacaan sensor gagal.")
        })
        return

    # 3. Hasil Sensor Suhu MLX90614 (medical/suhu)
    if topic == "medical/suhu" and status == "done":
        val = float(data.get("objek", 0))
        save_raw_sensor_log("suhu_mlx90614", data.get("samples", []), val)
        socketio.emit("measurement_update", {"type": "suhu", "value": val})
        return

    # 4. Hasil Sensor PPG MAX30102 (medical/spo2 -> Tensi, SpO2, BPM)
    if topic == "medical/spo2" and status == "done":
        spo2_val = data.get("spo2")
        bpm_val  = data.get("bpm")
        sys_val  = data.get("sys")
        dia_val  = data.get("dia")

        if target_mode in ("tensi", "tekanan", "ppg_all"):
            socketio.emit("measurement_update", {
                "type": "tensi",
                "sys": sys_val,
                "dia": dia_val,
                "pulse": bpm_val
            })
        if target_mode in ("spo2", "ppg_all"):
            socketio.emit("measurement_update", {"type": "spo2", "value": spo2_val})
        if target_mode in ("bpm", "ppg_all"):
            socketio.emit("measurement_update", {"type": "bpm", "value": bpm_val})
        save_raw_sensor_log("ppg_max30102", {"sys": sys_val, "dia": dia_val, "bpm": bpm_val}, float(spo2_val or 0))
        return

    # 5. Hasil Sensor Spektroskopi 18-Kanal AS7265x (medical/spektro -> Gula, Kolesterol, Asam Urat)
    if topic == "medical/spektro" and status == "done":
        gula_val = float(data.get("gula", 0))
        kol_val  = float(data.get("kolesterol", 0))
        urat_val = float(data.get("asam_urat", 0))
        raw_18   = data.get("raw_18ch", [])

        if target_mode in ("gula_darah", "gula", "spektro_all", "spektro"):
            socketio.emit("prediction_result", {"type": "gula_darah", "value": gula_val})
            save_raw_sensor_log("gula_as7265x", raw_18, gula_val)
        if target_mode in ("cholesterol", "kolesterol", "spektro_all", "spektro"):
            socketio.emit("prediction_result", {"type": "cholesterol", "value": kol_val})
            save_raw_sensor_log("kolesterol_as7265x", raw_18, kol_val)
        if target_mode in ("asam_urat", "spektro_all", "spektro"):
            socketio.emit("prediction_result", {"type": "asam_urat", "value": urat_val})
            save_raw_sensor_log("asam_urat_as7265x", raw_18, urat_val)
        return

    # 6. Hasil BLE Tinggi & Berat Badan (akm/prediction_data)
    if topic == MQTT_PREDICTION_TOPIC:
        dtype = data.get("type")
        if dtype in ("berat_badan", "tinggi_badan"):
            socketio.emit("measurement_update", {
                "type": dtype,
                "value": float(data.get("value", 0))
            })


mqtt_client.on_connect = on_mqtt_connect
mqtt_client.on_message = on_mqtt_message


def start_mqtt_background():
    def _connect_loop():
        while True:
            try:
                mqtt_client.connect(MQTT_BROKER_HOST, MQTT_BROKER_PORT, 60)
                mqtt_client.loop_forever()
            except Exception as e:
                print(f"[MQTT WEB] Menunggu broker ({MQTT_BROKER_HOST}:{MQTT_BROKER_PORT}): {e}")
                time.sleep(3)
    import threading
    threading.Thread(target=_connect_loop, daemon=True).start()


# ── Middleware Autentikasi ───────────────────────────────────
@app.before_request
def load_logged_in_user():
    user_id = session.get("user_id")
    if user_id is None:
        g.user = None
    else:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT id, username, role FROM users WHERE id = %s AND is_active = TRUE", (user_id,))
            g.user = cur.fetchone()
            cur.close()
            conn.close()
        except Exception:
            g.user = None


# ── Routes Autentikasi & Dashboard ───────────────────────────
@app.route("/")
def index():
    if g.user:
        return redirect(url_for("dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].encode("utf-8")

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT id, username, password_hash, role, is_active FROM users WHERE username = %s", (username,))
        user = cur.fetchone()
        cur.close()
        conn.close()

        if user and bcrypt.checkpw(password, user[2].encode("utf-8")):
            if not user[4]:
                flash("Akun Anda belum diaktifkan oleh Administrator.", "warning")
                return redirect(url_for("login"))
            session["user_id"] = user[0]
            session["user_role"] = user[3]
            flash(f"Selamat datang, {user[1]}!", "success")
            return redirect(url_for("dashboard"))

        flash("Username atau Password salah. Silakan coba lagi.", "danger")
        return redirect(url_for("login"))

    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("Anda telah keluar.", "info")
    return redirect(url_for("login"))


@app.route("/signup", methods=("GET", "POST"))
def signup():
    if request.method == "POST":
        nama = request.form["nama"].strip()
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        no_hp = request.form["no_hp"].strip()
        password = request.form["password"].encode("utf-8")
        confirm_password = request.form["confirm_password"].encode("utf-8")
        role = request.form.get("role", "operator")

        # Cegah privilege escalation menjadi admin melalui form publik
        if role not in ("operator", "pasien"):
            role = "operator"

        if password != confirm_password:
            flash("Password dan konfirmasi password tidak cocok!", "danger")
            return redirect(url_for("signup"))

        hashed_password = bcrypt.hashpw(password, bcrypt.gensalt()).decode("utf-8")
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                INSERT INTO users (nama, username, email, no_hp, password_hash, role, is_active)
                VALUES (%s, %s, %s, %s, %s, %s, TRUE)
                """,
                (nama, username, email, no_hp, hashed_password, role)
            )
            conn.commit()
            flash("Pendaftaran berhasil! Silakan masuk dengan akun Anda.", "success")
            return redirect(url_for("login"))
        except psycopg2.IntegrityError:
            conn.rollback()
            flash("Username atau Email sudah terdaftar.", "danger")
        finally:
            cur.close()
            conn.close()

    return render_template("signup.html")


@app.route("/dashboard")
def dashboard():
    if g.user is None:
        return redirect(url_for("login"))

    stats = {"total_patients": 0, "total_measurements": 0, "today_measurements": 0}
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM patients")
        stats["total_patients"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM measurements")
        stats["total_measurements"] = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM measurements WHERE measured_at::date = CURRENT_DATE")
        stats["today_measurements"] = cur.fetchone()[0]
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[DASHBOARD STATS WARN] {e}")

    return render_template("dashboard.html", user_data=g.user, stats=stats)


# ── Routes Pasien & Pengukuran ───────────────────────────────
@app.route("/measure", methods=("GET", "POST"))
def measure():
    if g.user is None:
        return redirect(url_for("login"))
    patient_id = request.args.get("patient_id")
    preselected_patient = None

    if patient_id:
        try:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""
                SELECT id, nama_lengkap, nik, alamat, umur, jenis_kelamin,
                       tanggal_lahir, email, provinsi, kecamatan, kelurahan
                FROM patients WHERE id = %s
            """, (patient_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()

            if row:
                umur_str = calculate_age_detail(row[6]) if row[6] else (f"{row[4]} Thn" if row[4] else "-")
                preselected_patient = {
                    "id": row[0], "nama": row[1], "nik": row[2], "alamat": row[3],
                    "umur": umur_str, "jenis_kelamin": row[5], "email": row[7],
                    "provinsi": row[8], "kecamatan": row[9], "kelurahan": row[10]
                }
        except Exception as e:
            print(f"[MEASURE ERR] {e}")

    return render_template("measure.html", preselected_patient=preselected_patient)


@app.route("/input_pasien", methods=["GET", "POST"])
def input_pasien():
    if g.user is None:
        return redirect(url_for("login"))

    source = request.args.get("source")
    if request.method == "POST":
        source = request.form.get("source")
        conn = get_db_connection()
        cur = conn.cursor()
        try:
            nama = request.form["nama_pasien"].strip()
            jk = request.form["jenis_kelamin"]
            alamat = request.form["alamat"].strip()
            nik = request.form["nik"].strip()
            no_hp = request.form["no_hp"].strip()
            tgl_lahir = request.form["tanggal_lahir"]
            email = request.form.get("email", "").strip()
            provinsi = request.form["provinsi"].strip()
            kecamatan = request.form["kecamatan"].strip()
            kelurahan = request.form["kelurahan"].strip()
            umur_thn = calculate_age_years(tgl_lahir) if tgl_lahir else 0

            cur.execute(
                """
                INSERT INTO patients (
                    nama_lengkap, jenis_kelamin, alamat, umur, nik, no_hp,
                    tanggal_lahir, email, provinsi, kecamatan, kelurahan
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (nama, jk, alamat, umur_thn, nik, no_hp, tgl_lahir, email, provinsi, kecamatan, kelurahan)
            )
            new_id = cur.fetchone()[0]
            conn.commit()
            flash(f'Sukses! Pasien "{nama}" berhasil didaftarkan.', "success")
            if source == "measure":
                return redirect(url_for("measure", patient_id=new_id))
        except psycopg2.IntegrityError:
            conn.rollback()
            flash("Gagal: NIK sudah terdaftar.", "danger")
        except Exception as e:
            conn.rollback()
            flash(f"Terjadi kesalahan: {e}", "danger")
        finally:
            cur.close()
            conn.close()

        return redirect(url_for("input_pasien"))

    return render_template("register_patient.html", source=source)


@app.route("/api/search_patients")
def search_patients():
    if g.user is None:
        return jsonify([])
    query = request.args.get("q", "").strip()
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nama_lengkap, nik, alamat, umur, jenis_kelamin,
               tanggal_lahir, email, provinsi, kecamatan, kelurahan
        FROM patients
        WHERE nama_lengkap ILIKE %s OR nik ILIKE %s
        ORDER BY nama_lengkap ASC
        LIMIT 8
    """, (f"%{query}%", f"%{query}%"))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    results = []
    for row in rows:
        umur_str = calculate_age_detail(row[6]) if row[6] else (f"{row[4]} Thn" if row[4] else "-")
        results.append({
            "id": row[0], "nama": row[1], "nik": row[2], "alamat": row[3],
            "umur": umur_str, "jenis_kelamin": row[5], "email": row[7],
            "provinsi": row[8], "kecamatan": row[9], "kelurahan": row[10]
        })
    return jsonify(results)


@app.route("/api/patient/<int:patient_id>")
def get_patient_by_id(patient_id):
    """Route yang diperbaiki agar tombol Edit Pasien di Data Pengukuran berfungsi."""
    if g.user is None:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, nama_lengkap, jenis_kelamin, alamat, umur, nik, no_hp, tanggal_lahir
        FROM patients WHERE id = %s
    """, (patient_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if not row:
        return jsonify({"error": "Not found"}), 404
    umur_val = row[4] if row[4] is not None else calculate_age_years(row[7])
    return jsonify({
        "id": row[0],
        "nama_lengkap": row[1],
        "jenis_kelamin": row[2],
        "alamat": row[3],
        "umur": umur_val,
        "nik": row[5],
        "no_hp": row[6]
    })


# ── WebSocket Pengukuran & Simpan Sesi ───────────────────────
@socketio.on("start_measurement")
def handle_start_measurement(data):
    global active_patient_id, active_patient_name
    flag = str(data.get("flag", ""))
    mtype = str(data.get("type", ""))
    active_patient_id = data.get("patient_id")
    active_patient_name = data.get("patient_name")

    print(f"[MEASURE START] Pasien={active_patient_name} ({active_patient_id}), type={mtype}, flag={flag}")

    if flag == "suhu" or mtype == "suhu":
        mqtt_client.publish("medical/cmd/suhu", json.dumps({"cmd": "start", "target_mode": "suhu"}))
    elif flag in ("tensi", "spo2", "bpm", "ppg_all") or mtype in ("tensi", "spo2", "bpm", "ppg_all"):
        mqtt_client.publish("medical/cmd/spo2", json.dumps({"cmd": "start", "target_mode": mtype}))
    elif flag in ("gula_darah", "cholesterol", "asam_urat", "spektro_all") or mtype in ("gula_darah", "cholesterol", "asam_urat", "spektro_all"):
        mqtt_client.publish("medical/cmd/spektro", json.dumps({"cmd": "start", "target_mode": mtype}))
    elif flag in ("5", "6"):
        mqtt_client.publish(MQTT_COMMAND_TOPIC, flag)


@socketio.on("stop_measurement")
def handle_stop_measurement():
    mqtt_client.publish("medical/cmd/stop_all", "stop")


@socketio.on("save_session_data")
def handle_save_session(data):
    patient_id = data.get("patient_id")
    results = data.get("results") or {}
    user_id = session.get("user_id")

    if not all([patient_id, results, user_id]):
        emit("save_status", {"status": "error", "message": "Data pasien atau hasil pengukuran belum lengkap."})
        return

    cholesterol = float(results.get("cholesterol", 0.0) or 0.0)
    uric_acid   = float(results.get("asam_urat", 0.0) or 0.0)
    blood_sugar = float(results.get("gula_darah", 0.0) or 0.0)
    spo2_val    = float(results.get("spo2", 0.0) or 0.0)
    temp_val    = float(results.get("suhu", 0.0) or 0.0)
    bpm_standalone = int(results.get("bpm", 0) or 0)

    tensi_data = results.get("tensi")
    if isinstance(tensi_data, dict):
        bp_sys   = int(tensi_data.get("sys", 0) or 0)
        bp_dia   = int(tensi_data.get("dia", 0) or 0)
        bp_pulse = int(tensi_data.get("pulse", 0) or bpm_standalone)
    else:
        bp_sys   = 0
        bp_dia   = 0
        bp_pulse = bpm_standalone

    height = float(results.get("tinggi_badan", 0.0) or 0.0)
    weight = float(results.get("berat_badan", 0.0) or 0.0)

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO measurements (
                patient_id, user_id,
                cholesterol_value, uric_acid_value, blood_sugar_value,
                blood_pressure_sys, blood_pressure_dia, blood_pressure_pulse,
                spo2_value, temperature_value,
                height, weight, measured_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            """,
            (
                patient_id, user_id,
                cholesterol, uric_acid, blood_sugar,
                bp_sys, bp_dia, bp_pulse,
                spo2_val, temp_val,
                height, weight
            )
        )
        conn.commit()
        emit("save_status", {"status": "success", "message": "Data sesi pengukuran berhasil disimpan ke database!"})
    except Exception as e:
        conn.rollback()
        print(f"[SAVE ERR] {e}")
        emit("save_status", {"status": "error", "message": f"Gagal menyimpan data: {e}"})
    finally:
        cur.close()
        conn.close()


# ── Riwayat, Export CSV, & Upload API Dinkes ─────────────────
@app.route("/data_pengukuran")
def data_pengukuran():
    if g.user is None:
        return redirect(url_for("login"))

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    search_query = request.args.get("q", "").strip()

    conn = get_db_connection()
    cur = conn.cursor()

    query = """
        SELECT p.id, p.nama_lengkap, MAX(m.measured_at) as last_measured
        FROM patients p
        JOIN measurements m ON p.id = m.patient_id
    """
    conditions = []
    params = []

    if start_date and end_date:
        conditions.append("m.measured_at >= %s AND m.measured_at <= %s")
        params.extend([f"{start_date} 00:00:00", f"{end_date} 23:59:59"])
    if search_query:
        conditions.append("(p.nama_lengkap ILIKE %s OR p.nik ILIKE %s)")
        params.extend([f"%{search_query}%", f"%{search_query}%"])
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " GROUP BY p.id, p.nama_lengkap ORDER BY last_measured DESC"

    cur.execute(query, tuple(params))
    patients_result = cur.fetchall()
    cur.close()
    conn.close()

    return render_template(
        "data_pengukuran.html",
        patients=patients_result,
        start_date=start_date,
        end_date=end_date,
        search_query=search_query
    )


@app.route("/api/patient_measurements/<int:patient_id>")
def get_patient_measurements(patient_id):
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT nama_lengkap, nik, jenis_kelamin, alamat, tanggal_lahir,
               no_hp, email, provinsi, kecamatan, kelurahan, umur
        FROM patients WHERE id = %s
    """, (patient_id,))
    p_row = cur.fetchone()
    if not p_row:
        cur.close()
        conn.close()
        return jsonify({"status": "error", "message": "Pasien tidak ditemukan"}), 404

    tgl_lahir = p_row[4]
    umur_display = calculate_age_detail(tgl_lahir) if tgl_lahir else (f"{p_row[10]} Tahun" if p_row[10] else "-")

    biodata = {
        "nama": p_row[0], "nik": p_row[1], "jenis_kelamin": p_row[2], "alamat": p_row[3],
        "umur": umur_display, "no_hp": p_row[5], "email": p_row[6],
        "provinsi": p_row[7], "kecamatan": p_row[8], "kelurahan": p_row[9],
        "tanggal_lahir": tgl_lahir.strftime("%d-%m-%Y") if tgl_lahir else "-"
    }

    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    q_history = """
        SELECT measured_at, cholesterol_value, uric_acid_value, blood_sugar_value,
               blood_pressure_sys, blood_pressure_dia, blood_pressure_pulse,
               spo2_value, temperature_value, height, weight
        FROM measurements
        WHERE patient_id = %s
    """
    params = [patient_id]
    if start_date and end_date:
        q_history += " AND measured_at >= %s AND measured_at <= %s"
        params.extend([start_date, end_date])
    q_history += " ORDER BY measured_at ASC"

    cur.execute(q_history, tuple(params))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    history = []
    chart_data = {
        "labels": [], "cholesterol": [], "uric_acid": [],
        "blood_sugar": [], "spo2": [], "temperature": []
    }

    for r in rows:
        dt = r[0]
        history.append({
            "formatted_date": dt.strftime("%d-%m-%Y %H:%M"),
            "cholesterol_value": r[1],
            "uric_acid_value": r[2],
            "blood_sugar_value": r[3],
            "blood_pressure_sys": r[4],
            "blood_pressure_dia": r[5],
            "blood_pressure_pulse": r[6],
            "spo2_value": r[7],
            "temperature_value": r[8],
            "height": r[9],
            "weight": r[10]
        })
        chart_data["labels"].append(dt.strftime("%d/%m %H:%M"))
        chart_data["cholesterol"].append(r[1] if r[1] and r[1] > 0 else None)
        chart_data["uric_acid"].append(r[2] if r[2] and r[2] > 0 else None)
        chart_data["blood_sugar"].append(r[3] if r[3] and r[3] > 0 else None)
        chart_data["spo2"].append(r[7] if r[7] and r[7] > 0 else None)
        chart_data["temperature"].append(r[8] if r[8] and r[8] > 0 else None)

    return jsonify({"biodata": biodata, "history": history, "chart_data": chart_data})


@app.route("/patient/edit/<int:patient_id>", methods=["POST"])
def edit_patient(patient_id):
    if g.user is None:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.form
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE patients
            SET nama_lengkap = %s, jenis_kelamin = %s, alamat = %s, umur = %s, nik = %s, no_hp = %s
            WHERE id = %s
            """,
            (data["nama_lengkap"], data["jenis_kelamin"], data["alamat"],
             int(data.get("umur") or 0), data["nik"], data["no_hp"], patient_id)
        )
        conn.commit()
        return jsonify({"status": "success", "message": "Data pasien berhasil diperbarui!"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": f"Gagal memperbarui data: {e}"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/patient/delete/<int:patient_id>", methods=["POST"])
def delete_patient(patient_id):
    if g.user is None:
        return jsonify({"error": "Unauthorized"}), 401
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM patients WHERE id = %s", (patient_id,))
        conn.commit()
        return jsonify({"status": "success", "message": "Data pasien berhasil dihapus!"})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": f"Gagal menghapus data: {e}"}), 500
    finally:
        cur.close()
        conn.close()


CSV_HEADERS = [
    "Nama Pasien", "NIK", "Jenis Kelamin", "Umur", "Provinsi", "Kecamatan", "Kelurahan", "Alamat",
    "Tanggal Pengukuran", "Kolesterol (mg/dL)", "Asam Urat (mg/dL)", "Gula Darah (mg/dL)",
    "Tensi Sys (mmHg)", "Tensi Dia (mmHg)", "Detak Jantung (bpm)", "SpO2 (%)", "Suhu Tubuh (C)",
    "Tinggi Badan (cm)", "Berat Badan (kg)"
]

EXPORT_SELECT_SQL = """
    SELECT
        p.nama_lengkap, p.nik, p.jenis_kelamin, p.tanggal_lahir,
        p.provinsi, p.kecamatan, p.kelurahan, p.alamat,
        m.measured_at, m.cholesterol_value, m.uric_acid_value, m.blood_sugar_value,
        m.blood_pressure_sys, m.blood_pressure_dia, m.blood_pressure_pulse,
        m.spo2_value, m.temperature_value, m.height, m.weight
    FROM measurements m
    JOIN patients p ON m.patient_id = p.id
"""


def _rows_to_csv_response(rows, filename):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(CSV_HEADERS)
    for row in rows:
        row_list = list(row)
        row_list[3] = calculate_age_years(row_list[3]) if row_list[3] else "-"
        writer.writerow(row_list)
    output.seek(0)
    return Response(
        output,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={filename}"}
    )


@app.route("/export/all")
def export_all():
    if g.user is None:
        return redirect(url_for("login"))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(EXPORT_SELECT_SQL + " ORDER BY p.nama_lengkap, m.measured_at;")
    rows = cur.fetchall()
    cur.close()
    conn.close()
    return _rows_to_csv_response(rows, "semua_data_pengukuran.csv")


@app.route("/export/selected")
def export_selected():
    if g.user is None:
        return redirect(url_for("login"))
    patient_ids = request.args.get("ids")
    if not patient_ids:
        flash("Tidak ada pasien yang dipilih untuk diekspor.", "warning")
        return redirect(url_for("data_pengukuran"))
    ids_tuple = tuple(int(pid) for pid in patient_ids.split(",") if pid.strip().isdigit())
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(EXPORT_SELECT_SQL + " WHERE p.id IN %s ORDER BY p.nama_lengkap, m.measured_at;", (ids_tuple,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        flash("Tidak ada data pengukuran untuk pasien yang dipilih.", "warning")
        return redirect(url_for("data_pengukuran"))
    return _rows_to_csv_response(rows, "data_pengukuran_terpilih.csv")


@app.route("/export/patient/<int:patient_id>")
def export_patient(patient_id):
    if g.user is None:
        return redirect(url_for("login"))
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(EXPORT_SELECT_SQL + " WHERE p.id = %s ORDER BY m.measured_at;", (patient_id,))
    rows = cur.fetchall()
    cur.close()
    conn.close()
    if not rows:
        flash("Tidak ada data pengukuran untuk pasien tersebut.", "warning")
        return redirect(url_for("data_pengukuran"))
    safe_name = "".join(c if c.isalnum() else "_" for c in rows[0][0].lower())
    return _rows_to_csv_response(rows, f"data_pengukuran_{safe_name}.csv")


def _send_rows_to_dinkes(rows, selected_puskesmas):
    external_api_url = get_active_api_url()
    if not external_api_url:
        return jsonify({"status": "error", "message": "URL API belum disetting di menu Pengaturan."}), 500

    api_key = os.getenv("API_KEY_DINKES", "")
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-API-Key": api_key
    }
    id_koper = os.getenv("ID_KOPER", "koper_01")
    kota = os.getenv("KOTA_DEFAULT", "Semarang")

    success_count = 0
    fail_count = 0
    errors_log = []

    for row in rows:
        try:
            umur_val = calculate_age_detail(row[3])
            waktu_utc = row[8]
            waktu_wib = waktu_utc + timedelta(hours=7) if waktu_utc.tzinfo is None else waktu_utc

            item_payload = {
                "nama_pasien": row[0],
                "id_koper": id_koper,
                "puskesmas": selected_puskesmas,
                "nik": str(row[1]),
                "jenis_kelamin": row[2],
                "umur": umur_val,
                "provinsi": row[4],
                "kota": kota,
                "kecamatan": row[6],
                "kelurahan": row[5],
                "alamat": row[7],
                "tanggal_pengukuran": waktu_wib.isoformat(),
                "kolesterol": row[9],
                "asam_urat": row[10],
                "gula_darah": row[11],
                "tinggi_badan": row[12],
                "berat_badan": row[13],
                "tensi_sys": row[14],
                "tensi_dia": row[15],
                "detak_jantung": row[16],
                "spo2": row[17],
                "suhu_tubuh": row[18]
            }
            resp = requests.post(external_api_url, json=item_payload, headers=headers, timeout=10)
            if resp.status_code in (200, 201):
                success_count += 1
            else:
                fail_count += 1
                errors_log.append(f"{row[0]}: {resp.text[:100]}")
        except Exception as e:
            fail_count += 1
            errors_log.append(f"{row[0]}: {e}")

    if success_count > 0 and fail_count == 0:
        return jsonify({"status": "success", "message": f"Sukses mengunggah {success_count} data ke API Dinkes!"})
    elif success_count > 0:
        return jsonify({"status": "warning", "message": f"Selesai parsial. Berhasil: {success_count}, Gagal: {fail_count}."})
    err_msg = errors_log[0] if errors_log else "Unknown Error"
    return jsonify({"status": "error", "message": f"Gagal upload data: {err_msg}"}), 500


@app.route("/upload_data", methods=["POST"])
def upload_data():
    if g.user is None:
        return jsonify({"status": "error", "message": "Akses ditolak"}), 401
    req_data = request.json or {}
    selected_puskesmas = req_data.get("puskesmas")
    if not selected_puskesmas:
        return jsonify({"status": "error", "message": "Mohon pilih Puskesmas tujuan terlebih dahulu."}), 400

    start_date = req_data.get("start_date")
    end_date = req_data.get("end_date")
    patient_ids = req_data.get("patient_ids")
    search_query = req_data.get("search_query")

    query = """
        SELECT
            p.nama_lengkap, p.nik, p.jenis_kelamin, p.tanggal_lahir,
            p.provinsi, p.kelurahan, p.kecamatan, p.alamat,
            m.measured_at, m.cholesterol_value, m.uric_acid_value, m.blood_sugar_value,
            m.height, m.weight, m.blood_pressure_sys, m.blood_pressure_dia,
            m.blood_pressure_pulse, m.spo2_value, m.temperature_value
        FROM measurements m
        JOIN patients p ON m.patient_id = p.id
    """
    conditions = []
    params = []
    if start_date and end_date:
        conditions.append("m.measured_at >= %s AND m.measured_at <= %s")
        params.extend([start_date, end_date])
    if patient_ids:
        placeholders = ",".join(["%s"] * len(patient_ids))
        conditions.append(f"p.id IN ({placeholders})")
        params.extend(patient_ids)
    elif search_query:
        conditions.append("(p.nama_lengkap ILIKE %s OR p.nik ILIKE %s)")
        params.extend([f"%{search_query}%", f"%{search_query}%"])
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY m.measured_at ASC"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return jsonify({"status": "warning", "message": "Tidak ada data ditemukan untuk kriteria ini."}), 404
    return _send_rows_to_dinkes(rows, selected_puskesmas)


@app.route("/upload_patient_data/<int:patient_id>", methods=["POST"])
def upload_patient_data(patient_id):
    if g.user is None:
        return jsonify({"status": "error", "message": "Akses ditolak"}), 401
    req_data = request.json or {}
    selected_puskesmas = req_data.get("puskesmas")
    if not selected_puskesmas:
        return jsonify({"status": "error", "message": "Mohon pilih Puskesmas tujuan terlebih dahulu."}), 400

    start_date = req_data.get("start_date")
    end_date = req_data.get("end_date")
    query = """
        SELECT
            p.nama_lengkap, p.nik, p.jenis_kelamin, p.tanggal_lahir,
            p.provinsi, p.kelurahan, p.kecamatan, p.alamat,
            m.measured_at, m.cholesterol_value, m.uric_acid_value, m.blood_sugar_value,
            m.height, m.weight, m.blood_pressure_sys, m.blood_pressure_dia,
            m.blood_pressure_pulse, m.spo2_value, m.temperature_value
        FROM measurements m
        JOIN patients p ON m.patient_id = p.id
        WHERE p.id = %s
    """
    params = [patient_id]
    if start_date and end_date:
        query += " AND m.measured_at >= %s AND m.measured_at <= %s"
        params.extend([start_date, end_date])
    query += " ORDER BY m.measured_at ASC"

    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute(query, tuple(params))
    rows = cur.fetchall()
    cur.close()
    conn.close()

    if not rows:
        return jsonify({"status": "warning", "message": "Tidak ada data pada rentang waktu ini."})
    return _send_rows_to_dinkes(rows, selected_puskesmas)


# ── Autocomplete Wilayah & Puskesmas ─────────────────────────
@app.route("/api/search_wilayah")
def search_wilayah():
    if g.user is None:
        return jsonify([])
    category = request.args.get("category")
    query = request.args.get("q", "").strip()
    parent_name = request.args.get("parent_name", "").strip()
    if len(query) < 1:
        return jsonify([])

    conn = get_db_connection()
    cur = conn.cursor()
    results = []
    try:
        if category == "kelurahan":
            sql = """
                SELECT k.nama, c.nama
                FROM master_kelurahans k
                JOIN master_kecamatans c ON k.kode_parent = c.kode_kecamatan
                WHERE k.nama ILIKE %s
            """
            params = [f"%{query}%"]
            if parent_name:
                sql += " AND c.nama ILIKE %s "
                params.append(parent_name)
            sql += " ORDER BY k.nama ASC LIMIT 15"
            cur.execute(sql, tuple(params))
            for r in cur.fetchall():
                results.append({"nama": r[0], "kecamatan": r[1]})
        elif category == "kecamatan":
            cur.execute("SELECT nama FROM master_kecamatans WHERE nama ILIKE %s ORDER BY nama ASC LIMIT 15", (f"%{query}%",))
            results = [{"nama": r[0]} for r in cur.fetchall()]
        elif category == "provinsi":
            cur.execute("SELECT nama FROM master_provinsis WHERE nama ILIKE %s ORDER BY nama ASC LIMIT 15", (f"%{query}%",))
            results = [{"nama": r[0]} for r in cur.fetchall()]
    except Exception as e:
        print(f"[WILAYAH ERR] {e}")
    finally:
        cur.close()
        conn.close()
    return jsonify(results)


@app.route("/api/puskesmas_list")
def get_puskesmas_list():
    if g.user is None:
        return jsonify([])
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("SELECT nama FROM puskesmas ORDER BY nama ASC")
        result = [r[0] for r in cur.fetchall()]
    except Exception:
        result = []
    finally:
        cur.close()
        conn.close()
    return jsonify(result)


# ── Pengaturan, Cek Sensor I2C, Kalibrasi, Printer, & WiFi ───
@app.route("/settings")
def settings_page():
    if g.user is None:
        return redirect(url_for("login"))
    if g.user[2] != "admin":
        flash("Menu Pengaturan hanya untuk Admin.", "danger")
        return redirect(url_for("dashboard"))

    current_url = get_active_api_url()
    history_urls = []
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT url FROM api_history ORDER BY last_used DESC")
        history_urls = [row[0] for row in cur.fetchall()]
        cur.close()
        conn.close()
    except Exception:
        pass

    return render_template(
        "settings.html",
        current_api_url=current_url,
        history_urls=history_urls,
        calib=load_calibration()
    )


@app.route("/api/settings/save_url", methods=["POST"])
def save_api_url():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error"}), 401
    new_url = (request.form.get("api_url") or "").strip()
    if not new_url:
        return jsonify({"status": "error", "message": "URL kosong"})

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO system_config (key, value) VALUES ('api_endpoint', %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
        """, (new_url,))
        cur.execute("""
            INSERT INTO api_history (url) VALUES (%s)
            ON CONFLICT (url) DO UPDATE SET last_used = CURRENT_TIMESTAMP
        """, (new_url,))
        conn.commit()
        return jsonify({"status": "success", "message": "URL API disimpan & ditambahkan ke riwayat."})
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": str(e)})
    finally:
        cur.close()
        conn.close()


@app.route("/api/settings/calibration", methods=["POST"])
def save_calibration():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    data = request.json or {}
    cfg = load_calibration()
    cfg["suhu"] = {"bias": float(data.get("suhu_bias", 0.0))}
    cfg["spo2"] = {"bias": float(data.get("spo2_bias", 0.0))}
    cfg["bpm"] = {"bias": float(data.get("bpm_bias", 0.0))}
    cfg["tekanan_darah"] = {
        "sys_bias": float(data.get("sys_bias", 0.0)),
        "dia_bias": float(data.get("dia_bias", 0.0))
    }
    cfg["gula_darah"]["bias"] = float(data.get("gula_bias", 0.0))
    cfg["kolesterol"]["bias"] = float(data.get("kol_bias", 0.0))
    cfg["asam_urat"]["bias"] = float(data.get("urat_bias", 0.0))

    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
        return jsonify({"status": "success", "message": "Konfigurasi kalibrasi sensor berhasil disimpan!"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route("/api/hardware/i2c_status")
def check_i2c_status():
    if g.user is None:
        return jsonify({"error": "Unauthorized"}), 401
    res = {"bus_exists": False, "mlx90614": False, "max30102": False, "as7265x": False}
    dev_path = f"/dev/i2c-{I2C_BUS_NUM}"
    if not HAS_SMBUS or not os.path.exists(dev_path):
        return jsonify(res)

    res["bus_exists"] = True
    bus = None
    try:
        bus = smbus2.SMBus(I2C_BUS_NUM)
        for key, addr in (("mlx90614", 0x5A), ("max30102", 0x57), ("as7265x", 0x49)):
            try:
                bus.read_byte(addr)
                res[key] = True
            except Exception:
                res[key] = False
    except Exception:
        pass
    finally:
        if bus is not None:
            try:
                bus.close()
            except Exception:
                pass
    return jsonify(res)


@app.route("/api/print_receipt", methods=["POST"])
def print_receipt():
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    global printer_instance
    if printer_instance is None:
        init_printer_connection()
    if printer_instance is None:
        return jsonify({"status": "error", "message": "Printer USB tidak terdeteksi."}), 500

    data = request.json or {}
    try:
        printer_instance.set(align="center")
        printer_instance.text("Hasil Pemeriksaan Kesehatan\n")
        printer_instance.text("KOPER SAKTI UDINUS x DINKES\n")
        printer_instance.text("================================\n")
        printer_instance.set(align="left")
        printer_instance.text(f"Nama    : {data.get('nama')}\n")
        printer_instance.text(f"Tanggal : {data.get('tanggal')}\n")
        printer_instance.text("--------------------------------\n")
        for res in data.get("hasil", []):
            printer_instance.set(bold=True)
            printer_instance.text(f"{res['tipe']}: {res['nilai']}\n")
            printer_instance.set(bold=False)
            status = res.get("status", "")
            if status and status != "-":
                printer_instance.text(f"Status : {status}\n")
            printer_instance.text("\n")
        printer_instance.set(align="center")
        printer_instance.text("--------------------------------\n")
        printer_instance.text("Terima Kasih - Semoga Sehat\n\n\n")
        return jsonify({"status": "success", "message": "Struk berhasil dicetak!"})
    except Exception as e:
        printer_instance = None
        return jsonify({"status": "error", "message": f"Gagal mencetak: {e}"}), 500


@app.route("/api/debug/restart_printer", methods=["POST"])
def debug_restart_printer():
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    if init_printer_connection():
        return jsonify({"status": "success", "message": "Printer berhasil terhubung ulang!"})
    return jsonify({"status": "error", "message": "Printer belum terdeteksi di port USB."})


@app.route("/api/debug/test_print", methods=["POST"])
def debug_test_print():
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    global printer_instance
    if printer_instance is None and not init_printer_connection():
        return jsonify({"status": "error", "message": "Printer tidak terhubung."}), 500
    try:
        printer_instance.set(align="center")
        printer_instance.text("\n================================\n")
        printer_instance.text("TES KONEKSI PRINTER AKMv2\nBERHASIL\n")
        printer_instance.text("================================\n")
        printer_instance.text(f"Waktu: {datetime.now().strftime('%d-%m-%Y %H:%M')}\n\n\n")
        return jsonify({"status": "success", "message": "Test Print Terkirim!"})
    except Exception as e:
        printer_instance = None
        return jsonify({"status": "error", "message": f"Gagal mencetak: {e}"}), 500


@app.route("/api/hardware/bt_status")
def bt_status():
    if g.user is None:
        return jsonify({"status": "inactive"}), 401
    return jsonify({"status": "active" if ble_is_connected else "inactive"})


@app.route("/api/hardware/reset_bt", methods=["POST"])
def reset_bt():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True, timeout=5)
        subprocess.run(["hciconfig", "hci0", "reset"], capture_output=True, timeout=5)
        return jsonify({"status": "success", "message": "Adapter Bluetooth berhasil direset."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal reset Bluetooth: {e}"})


# ── Perbaikan Total Manajemen WiFi via NetworkManager D-Bus ──
def _split_nmcli_escaped(line):
    """Memecah output `nmcli -t` dengan menghormati escape karakter `\\:` pada SSID."""
    parts = []
    buf = []
    esc = False
    for ch in line:
        if esc:
            buf.append(ch)
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == ":":
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    parts.append("".join(buf))
    return parts


@app.route("/api/wifi/scan")
def wifi_scan():
    if g.user is None:
        return jsonify({"status": "error", "ssids": [], "networks": []}), 401
    try:
        subprocess.run(["nmcli", "radio", "wifi", "on"], capture_output=True, timeout=5)
        subprocess.run(["nmcli", "dev", "wifi", "rescan", "ifname", "wlan0"], capture_output=True, timeout=8)
    except Exception:
        pass

    try:
        res = subprocess.run(
            ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "ifname", "wlan0"],
            capture_output=True, text=True, timeout=10
        )
        if res.returncode != 0:
            res = subprocess.run(
                ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                capture_output=True, text=True, timeout=10
            )

        by_ssid = {}
        for raw_line in res.stdout.splitlines():
            if not raw_line.strip():
                continue
            parts = _split_nmcli_escaped(raw_line)
            if len(parts) < 4:
                continue
            in_use = (parts[0].strip() == "*")
            ssid = parts[1].strip()
            if not ssid:
                continue
            try:
                sig = int(parts[2].strip())
            except ValueError:
                sig = 0
            sec = parts[3].strip()
            prev = by_ssid.get(ssid)
            if prev is None or in_use or sig > prev["signal"]:
                by_ssid[ssid] = {"ssid": ssid, "signal": sig, "security": sec, "in_use": in_use or (prev["in_use"] if prev else False)}

        networks = sorted(by_ssid.values(), key=lambda x: (not x["in_use"], -x["signal"]))
        ssids = [n["ssid"] for n in networks]
        return jsonify({"status": "success", "ssids": ssids, "networks": networks})
    except Exception as e:
        return jsonify({"status": "error", "ssids": [], "networks": [], "message": str(e)})


@app.route("/api/wifi/ip")
def get_network_info():
    if g.user is None:
        return jsonify({"status": "error", "ip": "-", "ssid": "-"}), 401
    ip_address = "-"
    ssid = "Disconnected"
    try:
        dev_res = subprocess.run(
            ["nmcli", "-t", "-f", "GENERAL.STATE,GENERAL.CONNECTION,IP4.ADDRESS", "dev", "show", "wlan0"],
            capture_output=True, text=True, timeout=6
        )
        for line in dev_res.stdout.splitlines():
            if line.startswith("GENERAL.CONNECTION:"):
                val = line.split(":", 1)[1].strip()
                if val and val != "--":
                    ssid = val
            elif line.startswith("IP4.ADDRESS[1]:"):
                val = line.split(":", 1)[1].strip()
                if val:
                    ip_address = val.split("/")[0]

        if ip_address == "-":
            res_ip = subprocess.check_output(["hostname", "-I"], timeout=5).decode("utf-8").strip()
            for candidate in res_ip.split():
                if not candidate.startswith("172.") and not candidate.startswith("127."):
                    ip_address = candidate
                    break
        return jsonify({"status": "success", "ip": ip_address, "ssid": ssid})
    except Exception as e:
        return jsonify({"status": "error", "ip": "-", "ssid": "-", "message": str(e)})


@app.route("/api/wifi/connect", methods=["POST"])
def wifi_connect():
    """
    Menghubungkan wlan0 ke SSID yang dipilih dengan mengatasi bug Debian Bookworm NetworkManager:
    - Menyimpan PSK sebagai system-owned secret (`psk-flags=0`), bukan user keyring (`No keyring secrets found`)
    - Memperbarui atau membuat ulang profil koneksi jika terjadi konflik `key-mgmt: property is missing`
    """
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Hanya Admin yang dapat mengubah WiFi."}), 401

    ssid = (request.form.get("ssid") or "").strip()
    pwd  = (request.form.get("password") or "").strip()
    if not ssid:
        return jsonify({"status": "error", "message": "SSID tidak boleh kosong."}), 400

    try:
        subprocess.run(["nmcli", "radio", "wifi", "on"], capture_output=True, timeout=5)

        # Percobaan 1: nmcli dev wifi connect langsung
        cmd1 = ["nmcli", "--wait", "20", "dev", "wifi", "connect", ssid, "ifname", "wlan0"]
        if pwd:
            cmd1.extend(["password", pwd])
        res1 = subprocess.run(cmd1, capture_output=True, text=True, timeout=25)

        if res1.returncode == 0:
            # Pastikan psk-flags=0 agar permanen saat reboot tanpa keyring
            if pwd:
                subprocess.run(
                    ["nmcli", "connection", "modify", ssid, "802-11-wireless-security.psk-flags", "0"],
                    capture_output=True, timeout=5
                )
            return jsonify({"status": "success", "message": f"Berhasil terhubung ke {ssid}!"})

        # Percobaan 2 (Fallback Bookworm): Hapus profil korup dengan nama sama, buat profil bersih psk-flags=0
        subprocess.run(["nmcli", "connection", "delete", "id", ssid], capture_output=True, timeout=6)

        add_cmd = [
            "nmcli", "connection", "add",
            "type", "wifi",
            "con-name", ssid,
            "ifname", "wlan0",
            "ssid", ssid,
            "connection.autoconnect", "yes"
        ]
        if pwd:
            add_cmd.extend([
                "wifi-sec.key-mgmt", "wpa-psk",
                "wifi-sec.psk", pwd,
                "wifi-sec.psk-flags", "0"
            ])
        res_add = subprocess.run(add_cmd, capture_output=True, text=True, timeout=10)
        if res_add.returncode != 0:
            err = (res_add.stderr or res_add.stdout or res1.stderr).strip()
            return jsonify({"status": "error", "message": f"Gagal membuat profil WiFi: {err}"})

        res_up = subprocess.run(
            ["nmcli", "--wait", "20", "connection", "up", "id", ssid],
            capture_output=True, text=True, timeout=25
        )
        if res_up.returncode == 0:
            return jsonify({"status": "success", "message": f"Berhasil terhubung ke {ssid}!"})

        err_msg = (res_up.stderr or res_up.stdout or res1.stderr or "Periksa kembali password WiFi.").strip()
        return jsonify({"status": "error", "message": err_msg})

    except subprocess.TimeoutExpired:
        return jsonify({"status": "error", "message": "Waktu koneksi habis (timeout). Pastikan sinyal cukup kuat dan password benar."})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Kesalahan WiFi: {e}"})


# ── Manajemen User, Shutdown/Reboot, & Evaluasi AI ───────────
@app.route("/add_user", methods=["GET", "POST"])
def add_user():
    if g.user is None:
        return redirect(url_for("login"))
    if g.user[2] != "admin":
        flash("Akses ditolak.", "danger")
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        nama = request.form["nama"].strip()
        username = request.form["username"].strip()
        password = request.form["password"]
        email = request.form.get("email") or f"{username}@udinus.local"
        no_hp = request.form.get("no_hp") or "-"
        hashed = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        conn = get_db_connection()
        cur = conn.cursor()
        try:
            cur.execute("""
                INSERT INTO users (nama, username, email, no_hp, password_hash, role, is_active)
                VALUES (%s, %s, %s, %s, %s, 'operator', TRUE)
            """, (nama, username, email, no_hp, hashed))
            conn.commit()
            flash(f'Operator "{nama}" berhasil ditambahkan!', "success")
        except psycopg2.IntegrityError:
            conn.rollback()
            flash("Username sudah digunakan.", "danger")
        except Exception as e:
            conn.rollback()
            flash(f"Error: {e}", "danger")
        finally:
            cur.close()
            conn.close()

    return render_template("add_user.html")


@app.route("/shutdown")
def shutdown_page():
    if g.user is None:
        return redirect(url_for("login"))
    if g.user[2] != "admin":
        flash("Menu Daya hanya untuk Admin.", "danger")
        return redirect(url_for("dashboard"))
    return render_template("shutdown.html")


@app.route("/api/system/shutdown", methods=["POST"])
def system_shutdown():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        subprocess.run([
            "dbus-send", "--system", "--print-reply",
            "--dest=org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager.PowerOff",
            "boolean:true"
        ], check=True, timeout=8)
        return jsonify({"status": "success", "message": "Sistem sedang dimatikan."})
    except Exception:
        try:
            subprocess.run(["poweroff"], check=True)
            return jsonify({"status": "success", "message": "Sistem dimatikan."})
        except Exception as e2:
            return jsonify({"status": "error", "message": f"Gagal shutdown: {e2}"}), 500


@app.route("/api/system/reboot", methods=["POST"])
def system_reboot():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        subprocess.run([
            "dbus-send", "--system", "--print-reply",
            "--dest=org.freedesktop.login1",
            "/org/freedesktop/login1",
            "org.freedesktop.login1.Manager.Reboot",
            "boolean:true"
        ], check=True, timeout=8)
        return jsonify({"status": "success", "message": "Sistem sedang direstart."})
    except Exception:
        try:
            subprocess.run(["reboot"], check=True)
            return jsonify({"status": "success", "message": "Sistem direstart."})
        except Exception as e2:
            return jsonify({"status": "error", "message": f"Gagal reboot: {e2}"}), 500


def hitung_evaluasi_data_science():
    import pandas as pd
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    eval_results = {
        "gula": {"accuracy": 98.71, "mae": 0.70, "r2": 0.9812, "rmse": 1.03, "is_live": False},
        "asam_urat": {"accuracy": 84.01, "mae": 0.79, "r2": 0.4175, "rmse": 1.02, "is_live": False},
        "kolesterol": {"accuracy": 89.50, "mae": 20.94, "r2": 0.4648, "rmse": 26.84, "is_live": False}
    }

    try:
        csv_path = os.path.join(BASE_DIR, "data/adc_asam_urat_v3.csv")
        if os.path.exists(csv_path) and "asam_urat" in models_xgb and "asam_urat" in models_knn:
            df = pd.read_csv(csv_path, sep=",")
            X = df.drop(columns=["real"]).values
            y_true = df["real"].values
            y_pred = (models_xgb["asam_urat"].predict(X) + models_knn["asam_urat"].predict(X)) / 2
            mae = mean_absolute_error(y_true, y_pred)
            rmse = np.sqrt(mean_squared_error(y_true, y_pred))
            r2 = r2_score(y_true, y_pred)
            mape = np.mean(np.abs((y_true - y_pred) / (y_true + 1e-10))) * 100
            eval_results["asam_urat"] = {
                "accuracy": round(max(0.0, 100 - mape), 2),
                "mae": round(mae, 2),
                "r2": round(r2, 4),
                "rmse": round(rmse, 2),
                "is_live": True
            }
    except Exception as e:
        print(f"[EVAL AU WARN] {e}")

    try:
        csv_path = os.path.join(BASE_DIR, "data/adc_kolesterol_v2.csv")
        if os.path.exists(csv_path) and "cholesterol" in models_xgb and "cholesterol" in models_knn:
            df = pd.read_csv(csv_path, sep=",")
            df.columns = df.columns.str.strip()
            df = df.replace(",", ".", regex=True).apply(pd.to_numeric, errors="coerce").dropna()
            X = df.drop(columns=["real"]).values
            y_true = df["real"].values
            y_pred = (models_xgb["cholesterol"].predict(X) + models_knn["cholesterol"].predict(X)) / 2
            mae = mean_absolute_error(y_true, y_pred)
            rmse = np.sqrt(mean_squared_error(y_true, y_pred))
            r2 = r2_score(y_true, y_pred)
            mape = np.mean(np.abs((y_true - y_pred) / (y_true + 1e-10))) * 100
            eval_results["kolesterol"] = {
                "accuracy": round(max(0.0, 100 - mape), 2),
                "mae": round(mae, 2),
                "r2": round(r2, 4),
                "rmse": round(rmse, 2),
                "is_live": True
            }
    except Exception as e:
        print(f"[EVAL KOL WARN] {e}")

    return eval_results


@app.route("/laporan_evaluasi")
def laporan_evaluasi():
    if g.user is None or g.user[2] != "admin":
        flash("Akses ditolak. Halaman ini khusus untuk Administrator.", "danger")
        return redirect(url_for("dashboard"))
    return render_template("evaluation.html", metrics=hitung_evaluasi_data_science())


if __name__ == "__main__":
    init_database_and_defaults()
    init_printer_connection()
    start_mqtt_background()
    print("Menjalankan server web AKMv2 di 0.0.0.0:5000...")
    socketio.run(app, debug=False, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
