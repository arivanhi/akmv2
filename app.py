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

            # Pastikan kolom baru spo2_value, temperature_value, dan kolom pembanding Tensi (Omron vs Sensor) tersedia
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS blood_pressure_sys INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS blood_pressure_dia INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS blood_pressure_pulse INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS bp_source VARCHAR(30) DEFAULT 'sensor';")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sensor_bp_sys INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sensor_bp_dia INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS sensor_bp_pulse INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS omron_bp_sys INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS omron_bp_dia INT DEFAULT 0;")
            cur.execute("ALTER TABLE measurements ADD COLUMN IF NOT EXISTS omron_bp_pulse INT DEFAULT 0;")
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
                    measurement_id INT REFERENCES measurements(id) ON DELETE SET NULL,
                    sensor_type VARCHAR(50) NOT NULL,
                    raw_values JSONB,
                    raw_frames JSONB,
                    context_features JSONB,
                    predicted_value FLOAT,
                    ground_truth_value FLOAT,
                    ground_truth_source VARCHAR(100),
                    notes TEXT,
                    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP WITH TIME ZONE
                );
            """)
            cur.execute("ALTER TABLE raw_sensor_data ADD COLUMN IF NOT EXISTS measurement_id INT REFERENCES measurements(id) ON DELETE SET NULL;")
            cur.execute("ALTER TABLE raw_sensor_data ADD COLUMN IF NOT EXISTS raw_frames JSONB;")
            cur.execute("ALTER TABLE raw_sensor_data ADD COLUMN IF NOT EXISTS context_features JSONB;")
            cur.execute("ALTER TABLE raw_sensor_data ADD COLUMN IF NOT EXISTS ground_truth_value FLOAT;")
            cur.execute("ALTER TABLE raw_sensor_data ADD COLUMN IF NOT EXISTS ground_truth_source VARCHAR(100);")
            cur.execute("ALTER TABLE raw_sensor_data ADD COLUMN IF NOT EXISTS notes TEXT;")
            cur.execute("ALTER TABLE raw_sensor_data ADD COLUMN IF NOT EXISTS updated_at TIMESTAMP WITH TIME ZONE;")
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


def save_raw_sensor_log(sensor_type, raw_vals, predicted_val, raw_frames=None):
    if not active_patient_id:
        return None
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        ctx = {}
        try:
            cur.execute("SELECT umur, jenis_kelamin FROM patients WHERE id = %s", (active_patient_id,))
            prow = cur.fetchone()
            if prow:
                ctx = {"umur": prow[0], "jenis_kelamin": prow[1]}
        except Exception:
            pass

        cur.execute(
            """
            INSERT INTO raw_sensor_data (
                patient_id, sensor_type, raw_values, raw_frames, context_features, predicted_value
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                active_patient_id,
                sensor_type,
                json.dumps(raw_vals),
                json.dumps(raw_frames) if raw_frames is not None else None,
                json.dumps(ctx) if ctx else None,
                float(predicted_val)
            )
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return new_id
    except Exception as e:
        print(f"[RAW LOG WARN] {e}")
        return None


def auto_link_ppg_omron_pair(patient_id, ppg_log_id=None, omron_log_id=None, measurement_id=None):
    """
    Menautkan rekaman Sensor MAX30102 (ppg_max30102) dengan Tensimeter Omron HEM-7156T (tensi_omron_ble)
    pada sesi pasien yang sama agar tersimpan sebagai pasangan pembanding (Ground-Truth)
    untuk fine-tuning & kalibrasi.
    """
    if not patient_id:
        return None
    try:
        conn = get_db_connection()
        cur = conn.cursor()

        # 1. Cari baris ppg_max30102 terbaru untuk pasien ini dalam 60 menit terakhir jika ppg_log_id belum diberikan
        if ppg_log_id:
            cur.execute(
                "SELECT id, raw_values, raw_frames, context_features FROM raw_sensor_data WHERE id = %s AND sensor_type = 'ppg_max30102'",
                (int(ppg_log_id),)
            )
        else:
            cur.execute(
                """
                SELECT id, raw_values, raw_frames, context_features
                FROM raw_sensor_data
                WHERE patient_id = %s
                  AND sensor_type = 'ppg_max30102'
                  AND created_at >= NOW() - INTERVAL '60 minutes'
                ORDER BY id DESC LIMIT 1
                """,
                (patient_id,)
            )
        ppg_row = cur.fetchone()

        # 2. Cari baris tensi_omron_ble terbaru untuk pasien ini dalam 60 menit terakhir jika omron_log_id belum diberikan
        if omron_log_id:
            cur.execute(
                "SELECT id, raw_values, context_features FROM raw_sensor_data WHERE id = %s AND sensor_type = 'tensi_omron_ble'",
                (int(omron_log_id),)
            )
        else:
            cur.execute(
                """
                SELECT id, raw_values, context_features
                FROM raw_sensor_data
                WHERE patient_id = %s
                  AND sensor_type = 'tensi_omron_ble'
                  AND created_at >= NOW() - INTERVAL '60 minutes'
                ORDER BY id DESC LIMIT 1
                """,
                (patient_id,)
            )
        omron_row = cur.fetchone()

        if not ppg_row or not omron_row:
            cur.close()
            conn.close()
            return None

        pid_log, ppg_rvals, ppg_rframes, ppg_ctx = ppg_row
        oid_log, om_rvals, om_ctx = omron_row

        ppg_rvals = ppg_rvals if isinstance(ppg_rvals, dict) else (json.loads(ppg_rvals) if ppg_rvals else {})
        ppg_rframes = ppg_rframes if isinstance(ppg_rframes, dict) else (json.loads(ppg_rframes) if ppg_rframes else {})
        ppg_ctx = ppg_ctx if isinstance(ppg_ctx, dict) else (json.loads(ppg_ctx) if ppg_ctx else {})
        om_rvals = om_rvals if isinstance(om_rvals, dict) else (json.loads(om_rvals) if om_rvals else {})
        om_ctx = om_ctx if isinstance(om_ctx, dict) else (json.loads(om_ctx) if om_ctx else {})

        s_sys = int(ppg_rvals.get("sys") or 0)
        s_dia = int(ppg_rvals.get("dia") or 0)
        s_bpm = int(ppg_rvals.get("bpm") or 0)
        s_spo2 = float(ppg_rvals.get("spo2") or 0)

        o_sys = int(om_rvals.get("sys") or 0)
        o_dia = int(om_rvals.get("dia") or 0)
        o_bpm = int(om_rvals.get("pulse") or 0)
        o_map = int(om_rvals.get("map") or 0)

        if s_sys <= 0 or o_sys <= 0:
            cur.close()
            conn.close()
            return None

        err_sys = o_sys - s_sys
        err_dia = o_dia - s_dia
        err_bpm = (o_bpm - s_bpm) if (o_bpm > 0 and s_bpm > 0) else 0

        pair_meta = {
            "paired_omron_log_id": oid_log,
            "paired_ppg_log_id": pid_log,
            "omron_sys": o_sys,
            "omron_dia": o_dia,
            "omron_bpm": o_bpm,
            "omron_map": o_map,
            "sensor_sys": s_sys,
            "sensor_dia": s_dia,
            "sensor_bpm": s_bpm,
            "sensor_spo2": s_spo2,
            "raw_sys_unbiased": ppg_rframes.get("raw_sys_unbiased", s_sys),
            "raw_dia_unbiased": ppg_rframes.get("raw_dia_unbiased", s_dia),
            "raw_bpm_unbiased": ppg_rframes.get("raw_bpm", s_bpm),
            "err_sys": err_sys,
            "err_dia": err_dia,
            "err_bpm": err_bpm
        }

        ppg_ctx.update(pair_meta)
        om_ctx.update(pair_meta)
        note_str = f"Pembanding Omron: {o_sys}/{o_dia} mmHg ({o_bpm} bpm) vs Sensor: {s_sys}/{s_dia} mmHg ({s_bpm} bpm) | ΔSYS={err_sys:+d}, ΔDIA={err_dia:+d}, ΔBPM={err_bpm:+d}"

        cur.execute(
            """
            UPDATE raw_sensor_data
            SET predicted_value = %s,
                ground_truth_value = %s,
                ground_truth_source = 'Omron HEM-7156T (BLE)',
                context_features = %s,
                notes = %s,
                measurement_id = COALESCE(%s, measurement_id),
                updated_at = NOW()
            WHERE id = %s
            """,
            (float(s_sys), float(o_sys), json.dumps(ppg_ctx), note_str, measurement_id, pid_log)
        )

        cur.execute(
            """
            UPDATE raw_sensor_data
            SET ground_truth_value = %s,
                ground_truth_source = 'Omron HEM-7156T (Standar)',
                context_features = %s,
                notes = %s,
                measurement_id = COALESCE(%s, measurement_id),
                updated_at = NOW()
            WHERE id = %s
            """,
            (float(o_sys), json.dumps(om_ctx), note_str, measurement_id, oid_log)
        )

        conn.commit()
        cur.close()
        conn.close()
        print(f"[PAIR LINKED] PPG #{pid_log} <-> Omron #{oid_log}: {note_str}")
        return pair_meta
    except Exception as e:
        print(f"[PAIR LINK WARN] {e}")
        return None


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
        "asam_urat": {"gain": 1.0, "bias": 0.0},
        "omron": {
            "mac": os.getenv("BLE_OMRON_MAC", "00:5F:BF:E2:25:DC").upper(),
            "name": "BLEsmart_0000057A005FBFE225DC",
            "model": "HEM-7156T"
        }
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
    client.subscribe("medical/omron")
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

    # 0. Status Pairing Tensimeter Omron HEM-7156T (medical/omron)
    if topic == "medical/omron" and status in ("pairing_progress", "paired", "pair_error"):
        socketio.emit("omron_pair_status", data)
        return

    # 1. Progress pemindaian dari sensor_publisher Kopersakti maupun Tensimeter BLE Omron
    if status == "scanning":
        socketio.emit("measurement_progress", {
            "remaining": data.get("remaining", 0),
            "progress": data.get("progress", 0),
            "frames_collected": data.get("frames_collected"),
            "message": data.get("message"),
            "stage": data.get("stage"),
            "source": "omron" if topic == "medical/omron" else "sensor"
        })
        return

    # 2. Error jujur jika sensor I2C/BLE belum terpasang atau jari tidak menempel
    if status == "error":
        socketio.emit("measurement_error", {
            "type": data.get("type", target_mode),
            "source": "omron" if topic == "medical/omron" else "sensor",
            "message": data.get("message", "Pembacaan sensor gagal.")
        })
        return

    # 2b. Hasil Tensimeter BLE Omron HEM-7156T (medical/omron)
    if topic == "medical/omron" and status == "done":
        sys_val   = int(data.get("sys", 0))
        dia_val   = int(data.get("dia", 0))
        pulse_val = int(data.get("pulse", 0))
        map_val   = int(data.get("map", 0))
        dev_mac   = data.get("mac", "")
        dev_name  = data.get("name", "Omron HEM-7156T")
        m_at      = data.get("measured_at", "")
        raw_meta  = data.get("raw_meta") or {}

        log_id = save_raw_sensor_log(
            "tensi_omron_ble",
            {
                "sys": sys_val,
                "dia": dia_val,
                "pulse": pulse_val,
                "map": map_val,
                "mac": dev_mac,
                "device_name": dev_name,
                "measured_at": m_at
            },
            float(sys_val),
            raw_frames=raw_meta
        )
        pair_info = auto_link_ppg_omron_pair(active_patient_id, omron_log_id=log_id)
        socketio.emit("measurement_update", {
            "type": "tensi",
            "sys": sys_val,
            "dia": dia_val,
            "pulse": pulse_val,
            "map": map_val,
            "source": "omron",
            "source_label": "Omron HEM-7156T (BLE)",
            "measured_at": m_at,
            "raw_log_id": log_id,
            "pair_info": pair_info
        })
        return

    # 3. Hasil Sensor Suhu MLX90614 (medical/suhu)
    if topic == "medical/suhu" and status == "done":
        val = float(data.get("objek", 0))
        samples = data.get("samples", [])
        log_id = save_raw_sensor_log(
            "suhu_mlx90614",
            samples,
            val,
            raw_frames={
                "ambient": data.get("ambient"),
                "n_samples": len(samples),
                "sampling_hz": 1.0,
                "duration_sec": 5.0
            }
        )
        socketio.emit("measurement_update", {"type": "suhu", "value": val, "raw_log_id": log_id})
        return

    # 4. Hasil Sensor PPG MAX30102 (medical/spo2 -> Tensi, SpO2, BPM)
    if topic == "medical/spo2" and status == "done":
        spo2_val = data.get("spo2")
        bpm_val  = data.get("bpm")
        sys_val  = data.get("sys")
        dia_val  = data.get("dia")
        raw_ppg  = data.get("raw_ppg") or {}

        log_id = save_raw_sensor_log(
            "ppg_max30102",
            {
                "sys": sys_val,
                "dia": dia_val,
                "bpm": bpm_val,
                "spo2": spo2_val,
                "raw_sys_unbiased": raw_ppg.get("raw_sys_unbiased", sys_val),
                "raw_dia_unbiased": raw_ppg.get("raw_dia_unbiased", dia_val),
                "raw_bpm": raw_ppg.get("raw_bpm", bpm_val),
                "dc_red": raw_ppg.get("dc_red"),
                "dc_ir": raw_ppg.get("dc_ir"),
                "rms_red": raw_ppg.get("rms_red"),
                "rms_ir": raw_ppg.get("rms_ir"),
                "ac_dc_red": raw_ppg.get("ac_dc_red"),
                "ac_dc_ir": raw_ppg.get("ac_dc_ir"),
                "r_ratio": raw_ppg.get("r_ratio"),
                "ts_rise_sec": raw_ppg.get("ts_rise_sec"),
                "td_decay_sec": raw_ppg.get("td_decay_sec"),
                "rsd_ratio": raw_ppg.get("rsd_ratio"),
                "n_samples": raw_ppg.get("n_samples"),
                "sampling_hz": raw_ppg.get("sampling_hz")
            },
            float(sys_val or spo2_val or 0),
            raw_frames=raw_ppg
        )
        pair_info = auto_link_ppg_omron_pair(active_patient_id, ppg_log_id=log_id)

        if target_mode in ("tensi", "tekanan", "ppg_all"):
            socketio.emit("measurement_update", {
                "type": "tensi",
                "sys": sys_val,
                "dia": dia_val,
                "pulse": bpm_val,
                "source": "sensor",
                "source_label": "Sensor MAX30102 (PPG)",
                "raw_log_id": log_id,
                "pair_info": pair_info
            })
        if target_mode in ("spo2", "ppg_all"):
            socketio.emit("measurement_update", {"type": "spo2", "value": spo2_val, "raw_log_id": log_id})
        if target_mode in ("bpm", "ppg_all"):
            socketio.emit("measurement_update", {"type": "bpm", "value": bpm_val, "raw_log_id": log_id})
        return

    # 5. Hasil Sensor Spektroskopi 18-Kanal AS7265x (medical/spektro -> Gula, Kolesterol, Asam Urat)
    if topic == "medical/spektro" and status == "done":
        gula_val = float(data.get("gula", 0))
        kol_val  = float(data.get("kolesterol", 0))
        urat_val = float(data.get("asam_urat", 0))
        raw_18   = data.get("raw_18ch", [])
        raw_meta = data.get("raw_meta") or {}

        sampling_info = {
            "sampling_hz": raw_meta.get("sampling_hz", 0),
            "valid_frames": raw_meta.get("valid_frames", 0),
            "total_frames": raw_meta.get("total_frames", 0),
            "model_source": raw_meta.get("model_source", {})
        }

        if target_mode in ("gula_darah", "gula", "spektro_all", "spektro"):
            lid_g = save_raw_sensor_log("gula_as7265x", raw_18, gula_val, raw_frames=raw_meta)
            socketio.emit("prediction_result", {
                "type": "gula_darah",
                "value": gula_val,
                "raw_log_id": lid_g,
                **sampling_info
            })
        if target_mode in ("cholesterol", "kolesterol", "spektro_all", "spektro"):
            lid_k = save_raw_sensor_log("kolesterol_as7265x", raw_18, kol_val, raw_frames=raw_meta)
            socketio.emit("prediction_result", {
                "type": "cholesterol",
                "value": kol_val,
                "raw_log_id": lid_k,
                **sampling_info
            })
        if target_mode in ("asam_urat", "spektro_all", "spektro"):
            lid_u = save_raw_sensor_log("asam_urat_as7265x", raw_18, urat_val, raw_frames=raw_meta)
            socketio.emit("prediction_result", {
                "type": "asam_urat",
                "value": urat_val,
                "raw_log_id": lid_u,
                **sampling_info
            })
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
    elif flag == "tensi_omron" or mtype == "tensi_omron":
        mqtt_client.publish("medical/cmd/omron", json.dumps({"cmd": "start", "target_mode": "tensi_omron"}))
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
    tensi_omron = results.get("tensi_omron") if isinstance(results.get("tensi_omron"), dict) else None
    tensi_sensor = results.get("tensi_sensor") if isinstance(results.get("tensi_sensor"), dict) else None

    omron_sys   = int(tensi_omron.get("sys", 0) or 0) if tensi_omron else 0
    omron_dia   = int(tensi_omron.get("dia", 0) or 0) if tensi_omron else 0
    omron_pulse = int(tensi_omron.get("pulse", 0) or 0) if tensi_omron else 0

    sensor_sys   = int(tensi_sensor.get("sys", 0) or 0) if tensi_sensor else 0
    sensor_dia   = int(tensi_sensor.get("dia", 0) or 0) if tensi_sensor else 0
    sensor_pulse = int(tensi_sensor.get("pulse", 0) or 0) if tensi_sensor else 0

    if isinstance(tensi_data, dict):
        bp_sys   = int(tensi_data.get("sys", 0) or 0)
        bp_dia   = int(tensi_data.get("dia", 0) or 0)
        bp_pulse = int(tensi_data.get("pulse", 0) or bpm_standalone)
        src      = tensi_data.get("source", "sensor")
        if src == "omron" and omron_sys == 0:
            omron_sys, omron_dia, omron_pulse = bp_sys, bp_dia, bp_pulse
        elif src != "omron" and sensor_sys == 0:
            sensor_sys, sensor_dia, sensor_pulse = bp_sys, bp_dia, bp_pulse
    else:
        bp_sys   = omron_sys or sensor_sys
        bp_dia   = omron_dia or sensor_dia
        bp_pulse = omron_pulse or sensor_pulse or bpm_standalone

    # Jika kedua metode (Omron & Sensor) diukur, utamakan angka klinis Omron sebagai nilai utama rekam medis,
    # sambil menyimpan kedua data di kolom sensor_bp_* dan omron_bp_* sebagai pembanding
    if omron_sys > 0 and sensor_sys > 0:
        bp_source = "both"
        bp_sys, bp_dia = omron_sys, omron_dia
        if omron_pulse > 0:
            bp_pulse = omron_pulse
    elif omron_sys > 0:
        bp_source = "omron"
    else:
        bp_source = "sensor"

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
                bp_source, sensor_bp_sys, sensor_bp_dia, sensor_bp_pulse,
                omron_bp_sys, omron_bp_dia, omron_bp_pulse,
                spo2_value, temperature_value,
                height, weight, measured_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
            RETURNING id
            """,
            (
                patient_id, user_id,
                cholesterol, uric_acid, blood_sugar,
                bp_sys, bp_dia, bp_pulse,
                bp_source, sensor_sys, sensor_dia, sensor_pulse,
                omron_sys, omron_dia, omron_pulse,
                spo2_val, temp_val,
                height, weight
            )
        )
        measurement_id = cur.fetchone()[0]

        # Tautkan raw_sensor_data pada sesi ini ke measurement_id & perkaya fitur konteks fisiologis untuk ML
        raw_log_ids = data.get("raw_log_ids") or {}
        id_list = list({int(v) for v in raw_log_ids.values() if v})
        if id_list:
            cur.execute("SELECT umur, jenis_kelamin FROM patients WHERE id = %s", (patient_id,))
            pinfo = cur.fetchone()
            session_ctx = {
                "umur": pinfo[0] if pinfo else None,
                "jenis_kelamin": pinfo[1] if pinfo else None,
                "suhu": temp_val if temp_val > 0 else None,
                "spo2": spo2_val if spo2_val > 0 else None,
                "bpm": bp_pulse if bp_pulse > 0 else None,
                "sys": bp_sys if bp_sys > 0 else None,
                "dia": bp_dia if bp_dia > 0 else None,
                "bp_source": bp_source,
                "sensor_sys": sensor_sys if sensor_sys > 0 else None,
                "sensor_dia": sensor_dia if sensor_dia > 0 else None,
                "sensor_bpm": sensor_pulse if sensor_pulse > 0 else None,
                "omron_sys": omron_sys if omron_sys > 0 else None,
                "omron_dia": omron_dia if omron_dia > 0 else None,
                "omron_bpm": omron_pulse if omron_pulse > 0 else None,
                "height": height if height > 0 else None,
                "weight": weight if weight > 0 else None
            }
            cur.execute(
                """
                UPDATE raw_sensor_data
                SET measurement_id = %s,
                    context_features = COALESCE(context_features, '{}'::jsonb) || %s::jsonb,
                    updated_at = NOW()
                WHERE id = ANY(%s)
                """,
                (measurement_id, json.dumps(session_ctx), id_list)
            )

        conn.commit()

        # Pastikan pasangan pembanding PPG MAX30102 <-> Omron HEM-7156T ditautkan penuh
        ppg_lid = raw_log_ids.get("tensi_sensor") or raw_log_ids.get("spo2") or raw_log_ids.get("bpm") or raw_log_ids.get("tensi")
        om_lid  = raw_log_ids.get("tensi_omron")
        auto_link_ppg_omron_pair(patient_id, ppg_log_id=ppg_lid, omron_log_id=om_lid, measurement_id=measurement_id)
        emit("save_status", {"status": "success", "measurement_id": measurement_id, "message": "Data sesi pengukuran & rekaman raw sensor ML berhasil disimpan ke database!"})
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


@app.route("/api/hardware/i2c_status", methods=["GET", "POST"])
def check_i2c_status():
    """
    Memeriksa ketiga modul I2C Kopersakti dengan protokol register masing-masing:
    - MLX90614 (0x5A): SMBus Read Word (register 0x07; read_byte ditolak oleh protokol SMBus MLX)
    - MAX30102 (0x57): Read Part ID register 0xFF (0x15)
    - AS7265x  (0x49): Read Status register 0x00
    Jika dipanggil via POST (tombol Cek Ulang Bus I2C), memicu re-init driver di sensor_publisher.
    """
    if g.user is None:
        return jsonify({"error": "Unauthorized"}), 401

    if request.method == "POST" or request.args.get("reinit") == "1":
        mqtt_client.publish("medical/cmd/reinit", "reinit")

    res = {
        "bus_exists": False,
        "mlx90614": False,
        "mlx90614_detail": "Tidak Terdeteksi",
        "max30102": False,
        "max30102_detail": "Tidak Terdeteksi",
        "as7265x": False,
        "as7265x_detail": "Tidak Terdeteksi",
        "active_count": 0,
        "checked_at": datetime.now().strftime("%H:%M:%S")
    }
    dev_path = f"/dev/i2c-{I2C_BUS_NUM}"
    if not HAS_SMBUS or not os.path.exists(dev_path):
        return jsonify(res)

    res["bus_exists"] = True
    bus = None
    try:
        bus = smbus2.SMBus(I2C_BUS_NUM)

        # 1. MLX90614 (0x5A) wajib menggunakan read_word_data (SMBus Read Word)
        try:
            raw_mlx = bus.read_word_data(0x5A, 0x07)
            temp_c = round((raw_mlx * 0.02) - 273.15, 1)
            if -40.0 < temp_c < 125.0:
                res["mlx90614"] = True
                res["mlx90614_detail"] = f"Terhubung • Bacaan {temp_c} °C"
        except Exception:
            pass

        # 2. MAX30102 (0x57) baca Part ID register 0xFF
        try:
            part_id = bus.read_byte_data(0x57, 0xFF)
            res["max30102"] = True
            res["max30102_detail"] = f"Terhubung • Part ID 0x{part_id:02X}"
        except Exception:
            pass

        # 3. AS7265x Triad (0x49) baca status register 0x00
        try:
            _ = bus.read_byte_data(0x49, 0x00)
            res["as7265x"] = True
            res["as7265x_detail"] = "Terhubung • 18-Kanal Siap"
        except Exception:
            pass

        res["active_count"] = sum(1 for k in ("mlx90614", "max30102", "as7265x") if res[k])
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
        printer_instance.text("MEDISYN - Medical System\n")
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
    omron_st = get_omron_bluez_status()
    is_active = ble_is_connected or omron_st.get("paired", False)
    return jsonify({
        "status": "active" if is_active else "inactive",
        "scale_connected": ble_is_connected,
        "omron": omron_st
    })


def get_omron_bluez_status():
    """Memeriksa status Pairing / Bonding / Trust Tensimeter Omron HEM-7156T pada BlueZ host."""
    cfg = load_calibration()
    om_cfg = cfg.get("omron") if isinstance(cfg.get("omron"), dict) else {}
    mac = (om_cfg.get("mac") or os.getenv("BLE_OMRON_MAC", "00:5F:BF:E2:25:DC")).strip().upper()
    name = om_cfg.get("name") or "BLEsmart_0000057A005FBFE225DC"
    model = om_cfg.get("model") or "HEM-7156T"
    paired_at = om_cfg.get("paired_at") or "-"

    res = {
        "mac": mac,
        "name": name,
        "model": model,
        "paired": False,
        "bonded": False,
        "trusted": False,
        "connected": False,
        "paired_at": paired_at,
        "adapter_powered": False
    }

    try:
        show_proc = subprocess.run(["bluetoothctl", "show"], capture_output=True, text=True, timeout=4)
        if "Powered: yes" in (show_proc.stdout or ""):
            res["adapter_powered"] = True
    except Exception:
        pass

    try:
        # Jika MAC belum terdeteksi di bluetoothctl, cek daftar devices BlueZ yang berawalan 00:5F:BF atau BLEsmart
        info_proc = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=4)
        out = info_proc.stdout or ""
        if "not available" in out.lower():
            devs_proc = subprocess.run(["bluetoothctl", "devices"], capture_output=True, text=True, timeout=4)
            for line in (devs_proc.stdout or "").splitlines():
                if "BLESMART" in line.upper() or "00:5F:BF:" in line.upper() or "OMRON" in line.upper():
                    parts = line.split(None, 2)
                    if len(parts) >= 2:
                        mac = parts[1].strip().upper()
                        res["mac"] = mac
                        if len(parts) >= 3:
                            res["name"] = parts[2].strip()
                        info_proc = subprocess.run(["bluetoothctl", "info", mac], capture_output=True, text=True, timeout=4)
                        out = info_proc.stdout or ""
                        break

        for line in out.splitlines():
            s = line.strip()
            if s.startswith("Name:"):
                val = s.split(":", 1)[1].strip()
                if val:
                    res["name"] = val
            elif s.startswith("Paired:"):
                res["paired"] = (s.split(":", 1)[1].strip().lower() == "yes")
            elif s.startswith("Bonded:"):
                res["bonded"] = (s.split(":", 1)[1].strip().lower() == "yes")
            elif s.startswith("Trusted:"):
                res["trusted"] = (s.split(":", 1)[1].strip().lower() == "yes")
            elif s.startswith("Connected:"):
                res["connected"] = (s.split(":", 1)[1].strip().lower() == "yes")
    except Exception as e:
        print(f"[OMRON STATUS WARN] {e}")

    return res


@app.route("/api/hardware/omron_status")
def api_omron_status():
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    st = get_omron_bluez_status()
    return jsonify({"status": "success", **st})


@app.route("/api/hardware/omron_pair", methods=["POST"])
def api_omron_pair():
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    req_mac = (payload.get("mac") or request.form.get("mac") or "").strip().upper() or None
    mqtt_client.publish("medical/cmd/omron", json.dumps({"cmd": "pair", "mac": req_mac}))
    return jsonify({
        "status": "started",
        "message": "Memulai pemindaian & pairing Tensimeter Omron HEM-7156T. Pastikan tensimeter menampilkan '-P-' berkedip."
    })


@app.route("/api/hardware/omron_save_mac", methods=["POST"])
def api_omron_save_mac():
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    payload = request.get_json(silent=True) or {}
    new_mac = (payload.get("mac") or request.form.get("mac") or "").strip().upper()
    if not new_mac or len(new_mac) != 17 or new_mac.count(":") != 5:
        return jsonify({"status": "error", "message": "Format MAC Address tidak valid (contoh: 00:5F:BF:E2:25:DC)."}), 400

    cfg = load_calibration()
    om = cfg.get("omron") if isinstance(cfg.get("omron"), dict) else {}
    om["mac"] = new_mac
    om["model"] = om.get("model") or "HEM-7156T"
    cfg["omron"] = om
    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
        subprocess.run(["bluetoothctl", "trust", new_mac], capture_output=True, timeout=4)
        st = get_omron_bluez_status()
        return jsonify({
            "status": "success",
            "message": f"MAC Address Omron ({new_mac}) berhasil disimpan.",
            "omron": st
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menyimpan MAC Omron: {e}"}), 500


@app.route("/api/hardware/reset_bt", methods=["POST"])
def reset_bt():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401
    try:
        subprocess.run(["rfkill", "unblock", "bluetooth"], capture_output=True, timeout=5)
        subprocess.run(["hciconfig", "hci0", "reset"], capture_output=True, timeout=5)
        st = get_omron_bluez_status()
        if st.get("mac"):
            subprocess.run(["bluetoothctl", "trust", st["mac"]], capture_output=True, timeout=4)
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


SPECTRAL_18_CHANNELS = [
    "A_410", "B_435", "C_460", "D_485", "E_510", "F_535",
    "G_560", "H_585", "R_610", "I_645", "S_680", "J_705",
    "T_730", "U_760", "V_810", "W_860", "K_900", "L_940"
]

TARGET_TO_SENSOR_TYPE = {
    "gula_darah": "gula_as7265x",
    "kolesterol": "kolesterol_as7265x",
    "asam_urat":  "asam_urat_as7265x"
}


@app.route("/api/raw_sensor_data/<int:log_id>/ground_truth", methods=["POST"])
def update_raw_ground_truth(log_id):
    if g.user is None:
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    gt_raw = payload.get("ground_truth_value")
    gt_source = (payload.get("ground_truth_source") or "").strip() or None
    notes = (payload.get("notes") or "").strip() or None

    if gt_raw in (None, ""):
        gt_val = None
    else:
        try:
            gt_val = float(gt_raw)
        except ValueError:
            return jsonify({"status": "error", "message": "Nilai ground-truth harus berupa angka."}), 400

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            UPDATE raw_sensor_data
            SET ground_truth_value = %s,
                ground_truth_source = COALESCE(%s, ground_truth_source),
                notes = COALESCE(%s, notes),
                updated_at = NOW()
            WHERE id = %s
            RETURNING id, sensor_type, predicted_value, ground_truth_value, ground_truth_source, notes
            """,
            (gt_val, gt_source, notes, log_id)
        )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            return jsonify({"status": "error", "message": "Rekaman raw_sensor_data tidak ditemukan."}), 404
        conn.commit()
        pred_v = row[2]
        abs_err = round(abs(float(pred_v) - float(gt_val)), 2) if (pred_v is not None and gt_val is not None) else None
        return jsonify({
            "status": "success",
            "id": row[0],
            "sensor_type": row[1],
            "predicted_value": pred_v,
            "ground_truth_value": row[3],
            "ground_truth_source": row[4],
            "notes": row[5],
            "abs_error": abs_err,
            "message": "Nilai pembanding (Ground-Truth) berhasil disimpan untuk dataset Machine Learning."
        })
    except Exception as e:
        conn.rollback()
        return jsonify({"status": "error", "message": f"Gagal menyimpan Ground-Truth: {e}"}), 500
    finally:
        cur.close()
        conn.close()


@app.route("/api/ml_dataset/export_csv")
def export_ml_dataset_csv():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    sensor_filter = request.args.get("sensor_type", "spektro").strip()
    labeled_only = request.args.get("labeled_only", "0") == "1"

    conn = get_db_connection()
    cur = conn.cursor()
    try:
        where_clauses = []
        params = []
        if sensor_filter == "spektro":
            where_clauses.append("r.sensor_type IN ('gula_as7265x', 'kolesterol_as7265x', 'asam_urat_as7265x')")
        elif sensor_filter != "all":
            where_clauses.append("r.sensor_type = %s")
            params.append(sensor_filter)

        if labeled_only:
            where_clauses.append("r.ground_truth_value IS NOT NULL")

        where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        cur.execute(
            f"""
            SELECT r.id, r.created_at, r.patient_id, COALESCE(p.nama_lengkap, '-'),
                   p.umur, p.jenis_kelamin, r.measurement_id, r.sensor_type,
                   r.raw_values, r.raw_frames, r.context_features,
                   r.predicted_value, r.ground_truth_value, r.ground_truth_source, r.notes
            FROM raw_sensor_data r
            LEFT JOIN patients p ON p.id = r.patient_id
            {where_sql}
            ORDER BY r.id DESC
            """,
            tuple(params)
        )
        rows = cur.fetchall()

        output = io.StringIO()
        writer = csv.writer(output)
        l2_cols = [f"L2_{c}" for c in SPECTRAL_18_CHANNELS]
        header = (
            [
                "id", "created_at", "patient_id", "patient_name", "umur", "jenis_kelamin",
                "measurement_id", "sensor_type", "valid_frames", "total_frames", "sampling_hz"
            ]
            + SPECTRAL_18_CHANNELS
            + l2_cols
            + [
                "suhu_c", "spo2_pct", "bpm", "sys_mmhg", "dia_mmhg",
                "predicted_value", "ground_truth_value", "ground_truth_source", "abs_error", "notes"
            ]
        )
        writer.writerow(header)

        for row in rows:
            rid, cat, pid, pname, pumur, pjk, mid, stype, rvals, rframes, ctx, pred, gt, gtsrc, notes = row
            rvals = rvals if isinstance(rvals, (list, dict)) else (json.loads(rvals) if rvals else [])
            rframes = rframes if isinstance(rframes, dict) else (json.loads(rframes) if rframes else {})
            ctx = ctx if isinstance(ctx, dict) else (json.loads(ctx) if ctx else {})

            umur = ctx.get("umur") if ctx.get("umur") is not None else pumur
            jk = ctx.get("jenis_kelamin") or pjk or ""

            if isinstance(rvals, list) and len(rvals) == 18:
                ch18 = [round(float(x), 3) for x in rvals]
                l2_list = rframes.get("l2_norm_18ch")
                if not l2_list or len(l2_list) != 18:
                    norm = float(np.linalg.norm(ch18))
                    l2_list = [round(float(x / norm), 5) for x in ch18] if norm > 1e-6 else [0.0] * 18
            else:
                ch18 = [""] * 18
                l2_list = [""] * 18

            v_frames = rframes.get("valid_frames") or rframes.get("n_samples") or ""
            t_frames = rframes.get("total_frames") or rframes.get("n_samples") or ""
            s_hz     = rframes.get("sampling_hz") or ""

            abs_err = round(abs(float(pred) - float(gt)), 2) if (pred is not None and gt is not None) else ""
            writer.writerow(
                [
                    rid,
                    cat.strftime("%Y-%m-%d %H:%M:%S") if cat else "",
                    pid or "",
                    pname,
                    umur if umur is not None else "",
                    jk,
                    mid or "",
                    stype,
                    v_frames,
                    t_frames,
                    s_hz
                ]
                + ch18
                + l2_list
                + [
                    ctx.get("suhu") if ctx.get("suhu") is not None else "",
                    ctx.get("spo2") if ctx.get("spo2") is not None else "",
                    ctx.get("bpm") if ctx.get("bpm") is not None else "",
                    ctx.get("sys") if ctx.get("sys") is not None else "",
                    ctx.get("dia") if ctx.get("dia") is not None else "",
                    pred if pred is not None else "",
                    gt if gt is not None else "",
                    gtsrc or "",
                    abs_err,
                    notes or ""
                ]
            )

        suffix = "berlabel_gt" if labeled_only else "semua"
        fname = f"akmv2_ml_dataset_{sensor_filter}_{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={fname}"}
        )
    finally:
        cur.close()
        conn.close()


@app.route("/api/ml_dataset/export_json")
def export_ml_dataset_json():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    labeled_only = request.args.get("labeled_only", "0") == "1"
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        where_sql = "WHERE r.ground_truth_value IS NOT NULL" if labeled_only else ""
        cur.execute(
            f"""
            SELECT r.id, r.created_at, r.patient_id, COALESCE(p.nama_lengkap, '-'),
                   r.measurement_id, r.sensor_type, r.raw_values, r.raw_frames,
                   r.context_features, r.predicted_value, r.ground_truth_value,
                   r.ground_truth_source, r.notes
            FROM raw_sensor_data r
            LEFT JOIN patients p ON p.id = r.patient_id
            {where_sql}
            ORDER BY r.id DESC
            LIMIT 1000
            """
        )
        items = []
        for row in cur.fetchall():
            items.append({
                "id": row[0],
                "created_at": row[1].isoformat() if row[1] else None,
                "patient_id": row[2],
                "patient_name": row[3],
                "measurement_id": row[4],
                "sensor_type": row[5],
                "channel_names": SPECTRAL_18_CHANNELS if "as7265x" in (row[5] or "") else None,
                "raw_values": row[6],
                "raw_frames": row[7],
                "context_features": row[8],
                "predicted_value": row[9],
                "ground_truth_value": row[10],
                "ground_truth_source": row[11],
                "notes": row[12]
            })
        fname = f"akmv2_ml_timeseries_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        return Response(
            json.dumps({"exported_at": datetime.now().isoformat(), "count": len(items), "records": items}, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename={fname}"}
        )
    finally:
        cur.close()
        conn.close()


@app.route("/api/ml_dataset/train_model", methods=["POST"])
def train_ml_from_ground_truth():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    payload = request.get_json(silent=True) or {}
    target_key = payload.get("target", "").strip()
    if target_key not in TARGET_TO_SENSOR_TYPE:
        return jsonify({"status": "error", "message": "Target biomarker tidak valid."}), 400

    sensor_type = TARGET_TO_SENSOR_TYPE[target_key]
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT raw_values, ground_truth_value
            FROM raw_sensor_data
            WHERE sensor_type = %s
              AND ground_truth_value IS NOT NULL
              AND raw_values IS NOT NULL
            ORDER BY id ASC
            """,
            (sensor_type,)
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    X_list, y_list = [], []
    for rvals, gt in rows:
        arr = rvals if isinstance(rvals, list) else (json.loads(rvals) if isinstance(rvals, str) else [])
        if isinstance(arr, list) and len(arr) == 18 and gt is not None:
            vec = np.array(arr, dtype=float)
            norm = float(np.linalg.norm(vec))
            l2 = (vec / norm) if norm > 1e-6 else np.zeros(18, dtype=float)
            X_list.append(np.concatenate([vec, l2]))
            y_list.append(float(gt))

    n_samples = len(X_list)
    if n_samples < 8:
        return jsonify({
            "status": "error",
            "n_samples": n_samples,
            "message": f"Baru tersedia {n_samples} data berlabel Ground-Truth untuk {target_key}. Kumpulkan minimal 8 sampel Ground-Truth agar model Machine Learning valid dan tidak overfitting."
        }), 400

    from sklearn.ensemble import RandomForestRegressor, VotingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import LeaveOneOut, KFold, cross_val_predict
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    X = np.array(X_list, dtype=float)
    y = np.array(y_list, dtype=float)

    rf = RandomForestRegressor(n_estimators=150, max_depth=6, min_samples_leaf=2, random_state=42)
    ridge_pipe = Pipeline([("scaler", StandardScaler()), ("ridge", Ridge(alpha=2.0))])
    ensemble = VotingRegressor([("rf", rf), ("ridge", ridge_pipe)])

    cv = LeaveOneOut() if n_samples < 20 else KFold(n_splits=5, shuffle=True, random_state=42)
    y_cv_pred = cross_val_predict(ensemble, X, y, cv=cv)

    cv_mae = float(mean_absolute_error(y, y_cv_pred))
    cv_rmse = float(np.sqrt(mean_squared_error(y, y_cv_pred)))
    cv_r2 = float(r2_score(y, y_cv_pred)) if np.std(y) > 1e-6 else 0.0
    cv_mape = float(np.mean(np.abs((y - y_cv_pred) / (np.abs(y) + 1e-10))) * 100.0)
    cv_acc = round(max(0.0, 100.0 - cv_mape), 2)

    ensemble.fit(X, y)
    os.makedirs(os.path.join(BASE_DIR, "model"), exist_ok=True)
    model_path = os.path.join(BASE_DIR, "model", f"as7265x_{target_key}.joblib")
    bundle = {
        "model": ensemble,
        "feature_mode": "raw18_plus_l2",
        "target": target_key,
        "sensor_type": sensor_type,
        "n_samples": n_samples,
        "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cv_metrics": {
            "accuracy": cv_acc,
            "mae": round(cv_mae, 2),
            "rmse": round(cv_rmse, 2),
            "r2": round(cv_r2, 4)
        }
    }
    joblib.dump(bundle, model_path)

    return jsonify({
        "status": "success",
        "target": target_key,
        "n_samples": n_samples,
        "metrics": bundle["cv_metrics"],
        "trained_at": bundle["trained_at"],
        "message": f"Model ML {target_key} berhasil dilatih dari {n_samples} sampel Ground-Truth (MAE CV: ±{cv_mae:.2f} mg/dL, Akurasi: {cv_acc}%) dan langsung aktif!"
    })


def hitung_evaluasi_data_science():
    import pandas as pd
    from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score

    eval_results = {
        "gula": {"accuracy": 98.71, "mae": 0.70, "r2": 0.9812, "rmse": 1.03, "is_live": False, "source_label": "Benchmark Standar", "n_gt": 0},
        "asam_urat": {"accuracy": 84.01, "mae": 0.79, "r2": 0.4175, "rmse": 1.02, "is_live": False, "source_label": "Benchmark Standar", "n_gt": 0},
        "kolesterol": {"accuracy": 89.50, "mae": 20.94, "r2": 0.4648, "rmse": 26.84, "is_live": False, "source_label": "Benchmark Standar", "n_gt": 0}
    }

    # Fallback CSV historis AKM jika belum ada Ground-Truth AS7265x di PostgreSQL
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
            eval_results["asam_urat"].update({
                "accuracy": round(max(0.0, 100 - mape), 2),
                "mae": round(mae, 2),
                "r2": round(r2, 4),
                "rmse": round(rmse, 2),
                "is_live": True,
                "source_label": "Dataset CSV Awal"
            })
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
            eval_results["kolesterol"].update({
                "accuracy": round(max(0.0, 100 - mape), 2),
                "mae": round(mae, 2),
                "r2": round(r2, 4),
                "rmse": round(rmse, 2),
                "is_live": True,
                "source_label": "Dataset CSV Awal"
            })
    except Exception as e:
        print(f"[EVAL KOL WARN] {e}")

    # Prioritas Utama: Evaluasi Live dari data Ground-Truth AS7265x di PostgreSQL AKMv2
    gt_mapping = [
        ("gula", "gula_as7265x"),
        ("asam_urat", "asam_urat_as7265x"),
        ("kolesterol", "kolesterol_as7265x")
    ]
    try:
        conn = get_db_connection()
        cur = conn.cursor()
        for metric_key, stype in gt_mapping:
            cur.execute(
                """
                SELECT predicted_value, ground_truth_value
                FROM raw_sensor_data
                WHERE sensor_type = %s
                  AND predicted_value IS NOT NULL
                  AND ground_truth_value IS NOT NULL
                """,
                (stype,)
            )
            pairs = cur.fetchall()
            n_gt = len(pairs)
            eval_results[metric_key]["n_gt"] = n_gt
            if n_gt >= 2:
                y_pred = np.array([float(p[0]) for p in pairs], dtype=float)
                y_true = np.array([float(p[1]) for p in pairs], dtype=float)
                mae = float(mean_absolute_error(y_true, y_pred))
                rmse = float(np.sqrt(mean_squared_error(y_true, y_pred)))
                r2 = float(r2_score(y_true, y_pred)) if np.std(y_true) > 1e-6 else 0.0
                mape = float(np.mean(np.abs((y_true - y_pred) / (np.abs(y_true) + 1e-10))) * 100.0)
                eval_results[metric_key].update({
                    "accuracy": round(max(0.0, 100.0 - mape), 2),
                    "mae": round(mae, 2),
                    "r2": round(r2, 4),
                    "rmse": round(rmse, 2),
                    "is_live": True,
                    "source_label": f"Live Ground-Truth AS7265x ({n_gt} sampel)"
                })
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[EVAL PG GT WARN] {e}")

    return eval_results


def get_ppg_omron_comparison_stats():
    """
    Mengumpulkan seluruh pasangan data pembanding antara Sensor MAX30102 (ppg_max30102)
    dengan Tensimeter Omron HEM-7156T (tensi_omron_ble) untuk analisis error, auto-kalibrasi offset,
    dan fine-tuning model ML PPG.
    """
    calib = load_calibration()
    bp_cal = calib.get("tekanan_darah") or {}
    bpm_cal = calib.get("bpm") or {}

    stats = {
        "n_pairs": 0,
        "sys_mae": None,
        "sys_rmse": None,
        "sys_bias_err": None,
        "dia_mae": None,
        "dia_rmse": None,
        "dia_bias_err": None,
        "bpm_mae": None,
        "bpm_rmse": None,
        "bpm_bias_err": None,
        "current_sys_bias": float(bp_cal.get("sys_bias", 0.0)),
        "current_dia_bias": float(bp_cal.get("dia_bias", 0.0)),
        "current_bpm_bias": float(bpm_cal.get("bias", 0.0)),
        "recommended_sys_bias": float(bp_cal.get("sys_bias", 0.0)),
        "recommended_dia_bias": float(bp_cal.get("dia_bias", 0.0)),
        "recommended_bpm_bias": float(bpm_cal.get("bias", 0.0)),
        "active_model": None,
        "pairs": []
    }

    mpath = os.path.join(BASE_DIR, "model", "max30102_ppg_omron.joblib")
    if os.path.exists(mpath):
        try:
            b = joblib.load(mpath)
            if isinstance(b, dict):
                stats["active_model"] = {
                    "n_samples": b.get("n_samples", 0),
                    "trained_at": b.get("trained_at", "-"),
                    "cv_metrics": b.get("cv_metrics", {})
                }
        except Exception:
            pass

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT r.id, r.created_at, COALESCE(p.nama_lengkap, 'Tanpa Nama'),
                   p.umur, p.jenis_kelamin, r.raw_values, r.raw_frames,
                   r.context_features, r.ground_truth_value
            FROM raw_sensor_data r
            LEFT JOIN patients p ON p.id = r.patient_id
            WHERE r.sensor_type = 'ppg_max30102'
            ORDER BY r.id DESC
            """
        )
        rows = cur.fetchall()
        cur.close()
        conn.close()

        s_sys_list, o_sys_list, u_sys_list = [], [], []
        s_dia_list, o_dia_list, u_dia_list = [], [], []
        s_bpm_list, o_bpm_list, u_bpm_list = [], [], []

        for rid, cat, pname, pumur, pjk, rvals, rframes, ctx, gt_val in rows:
            rvals = rvals if isinstance(rvals, dict) else (json.loads(rvals) if rvals else {})
            rframes = rframes if isinstance(rframes, dict) else (json.loads(rframes) if rframes else {})
            ctx = ctx if isinstance(ctx, dict) else (json.loads(ctx) if ctx else {})

            s_sys = int(ctx.get("sensor_sys") or rvals.get("sys") or 0)
            s_dia = int(ctx.get("sensor_dia") or rvals.get("dia") or 0)
            s_bpm = int(ctx.get("sensor_bpm") or rvals.get("bpm") or 0)
            s_spo2 = float(ctx.get("sensor_spo2") or rvals.get("spo2") or 0)

            o_sys = int(ctx.get("omron_sys") or (gt_val if gt_val else 0) or 0)
            o_dia = int(ctx.get("omron_dia") or 0)
            o_bpm = int(ctx.get("omron_bpm") or 0)

            if s_sys <= 0 or o_sys <= 0:
                continue

            raw_sys_unb = float(rframes.get("raw_sys_unbiased") or rvals.get("raw_sys_unbiased") or (s_sys - stats["current_sys_bias"]))
            raw_dia_unb = float(rframes.get("raw_dia_unbiased") or rvals.get("raw_dia_unbiased") or (s_dia - stats["current_dia_bias"]))
            raw_bpm_unb = float(rframes.get("raw_bpm") or rvals.get("raw_bpm") or (s_bpm - stats["current_bpm_bias"]))

            s_sys_list.append(float(s_sys))
            o_sys_list.append(float(o_sys))
            u_sys_list.append(raw_sys_unb)

            if s_dia > 0 and o_dia > 0:
                s_dia_list.append(float(s_dia))
                o_dia_list.append(float(o_dia))
                u_dia_list.append(raw_dia_unb)

            if s_bpm > 0 and o_bpm > 0:
                s_bpm_list.append(float(s_bpm))
                o_bpm_list.append(float(o_bpm))
                u_bpm_list.append(raw_bpm_unb)

            stats["pairs"].append({
                "ppg_id": rid,
                "omron_id": ctx.get("paired_omron_log_id"),
                "created_at": cat.strftime("%d/%m/%Y %H:%M") if cat else "-",
                "patient_name": pname,
                "umur": pumur or ctx.get("umur") or "-",
                "jenis_kelamin": pjk or ctx.get("jenis_kelamin") or "-",
                "sensor_sys": s_sys,
                "sensor_dia": s_dia,
                "sensor_bpm": s_bpm,
                "sensor_spo2": s_spo2,
                "omron_sys": o_sys,
                "omron_dia": o_dia,
                "omron_bpm": o_bpm,
                "err_sys": o_sys - s_sys,
                "err_dia": (o_dia - s_dia) if o_dia > 0 else None,
                "err_bpm": (o_bpm - s_bpm) if o_bpm > 0 else None,
                "raw_sys_unbiased": round(raw_sys_unb, 1),
                "raw_dia_unbiased": round(raw_dia_unb, 1),
                "raw_bpm_unbiased": round(raw_bpm_unb, 1),
                "rvals": rvals,
                "rframes": rframes
            })

        n_p = len(s_sys_list)
        stats["n_pairs"] = n_p
        if n_p >= 1:
            arr_s_sys = np.array(s_sys_list)
            arr_o_sys = np.array(o_sys_list)
            arr_u_sys = np.array(u_sys_list)
            stats["sys_mae"] = round(float(np.mean(np.abs(arr_o_sys - arr_s_sys))), 2)
            stats["sys_rmse"] = round(float(np.sqrt(np.mean((arr_o_sys - arr_s_sys) ** 2))), 2)
            stats["sys_bias_err"] = round(float(np.mean(arr_o_sys - arr_s_sys)), 2)
            stats["recommended_sys_bias"] = round(float(np.mean(arr_o_sys - arr_u_sys)), 1)

        if len(s_dia_list) >= 1:
            arr_s_dia = np.array(s_dia_list)
            arr_o_dia = np.array(o_dia_list)
            arr_u_dia = np.array(u_dia_list)
            stats["dia_mae"] = round(float(np.mean(np.abs(arr_o_dia - arr_s_dia))), 2)
            stats["dia_rmse"] = round(float(np.sqrt(np.mean((arr_o_dia - arr_s_dia) ** 2))), 2)
            stats["dia_bias_err"] = round(float(np.mean(arr_o_dia - arr_s_dia)), 2)
            stats["recommended_dia_bias"] = round(float(np.mean(arr_o_dia - arr_u_dia)), 1)

        if len(s_bpm_list) >= 1:
            arr_s_bpm = np.array(s_bpm_list)
            arr_o_bpm = np.array(o_bpm_list)
            arr_u_bpm = np.array(u_bpm_list)
            stats["bpm_mae"] = round(float(np.mean(np.abs(arr_o_bpm - arr_s_bpm))), 2)
            stats["bpm_rmse"] = round(float(np.sqrt(np.mean((arr_o_bpm - arr_s_bpm) ** 2))), 2)
            stats["bpm_bias_err"] = round(float(np.mean(arr_o_bpm - arr_s_bpm)), 2)
            stats["recommended_bpm_bias"] = round(float(np.mean(arr_o_bpm - arr_u_bpm)), 1)

    except Exception as e:
        print(f"[PPG OMRON STATS WARN] {e}")

    return stats


@app.route("/api/ml_dataset/calibrate_ppg_from_omron", methods=["POST"])
def calibrate_ppg_from_omron():
    """
    Menghitung koreksi offset optimal (sys_bias, dia_bias, bpm_bias) dari seluruh pasangan
    pengukuran Sensor MAX30102 vs Tensimeter Omron HEM-7156T dan menerapkannya ke calibration_config.json.
    """
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    st = get_ppg_omron_comparison_stats()
    if st["n_pairs"] < 1:
        return jsonify({
            "status": "error",
            "message": "Belum ada pasangan data pembanding Sensor MAX30102 vs Tensimeter Omron HEM-7156T. Lakukan pengukuran dengan kedua metode pada sesi pasien terlebih dahulu."
        }), 400

    cfg = load_calibration()
    cfg["tekanan_darah"] = {
        "sys_bias": float(st["recommended_sys_bias"]),
        "dia_bias": float(st["recommended_dia_bias"])
    }
    cfg["bpm"] = {
        "bias": float(st["recommended_bpm_bias"])
    }
    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
        return jsonify({
            "status": "success",
            "n_pairs": st["n_pairs"],
            "new_sys_bias": cfg["tekanan_darah"]["sys_bias"],
            "new_dia_bias": cfg["tekanan_darah"]["dia_bias"],
            "new_bpm_bias": cfg["bpm"]["bias"],
            "message": (
                f"Auto-Kalibrasi berhasil dari {st['n_pairs']} pasangan pembanding Omron vs MAX30102! "
                f"Offset baru diterapkan: SYS {cfg['tekanan_darah']['sys_bias']:+.1f} mmHg, "
                f"DIA {cfg['tekanan_darah']['dia_bias']:+.1f} mmHg, "
                f"BPM {cfg['bpm']['bias']:+.1f} bpm."
            )
        })
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menyimpan kalibrasi: {e}"}), 500


@app.route("/api/ml_dataset/train_ppg_model", methods=["POST"])
def train_ppg_model_from_omron():
    """
    Fine-tuning model regresi Machine Learning (SYS & DIA) dari fitur morfologi gelombang PPG MAX30102
    terhadap nilai Ground-Truth Tensimeter Omron HEM-7156T.
    """
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    st = get_ppg_omron_comparison_stats()
    X_list, y_sys_list, y_dia_list = [], [], []

    for p in st["pairs"]:
        rf = p.get("rframes") or {}
        rv = p.get("rvals") or {}
        o_sys = p.get("omron_sys")
        o_dia = p.get("omron_dia")
        if not o_sys or not o_dia or o_sys <= 0 or o_dia <= 0:
            continue

        raw_bpm = float(rf.get("raw_bpm") or rv.get("raw_bpm") or p.get("sensor_bpm") or 72.0)
        dc_red = float(rf.get("dc_red") or rv.get("dc_red") or 30000.0)
        dc_ir  = float(rf.get("dc_ir") or rv.get("dc_ir") or 40000.0)
        rms_red = float(rf.get("rms_red") or rv.get("rms_red") or 45.0)
        rms_ir  = float(rf.get("rms_ir") or rv.get("rms_ir") or 65.0)
        ac_dc_ir  = float(rf.get("ac_dc_ir") or rv.get("ac_dc_ir") or (rms_ir / max(1.0, dc_ir)))
        ac_dc_red = float(rf.get("ac_dc_red") or rv.get("ac_dc_red") or (rms_red / max(1.0, dc_red)))
        r_ratio = float(rf.get("r_ratio") or rv.get("r_ratio") or (ac_dc_red / max(1e-6, ac_dc_ir)))
        ts = float(rf.get("ts_rise_sec") or rv.get("ts_rise_sec") or 0.13)
        td = float(rf.get("td_decay_sec") or rv.get("td_decay_sec") or 0.45)
        rsd = float(rf.get("rsd_ratio") or rv.get("rsd_ratio") or (ts / max(0.01, td)))

        X_list.append([raw_bpm, ac_dc_ir, ac_dc_red, r_ratio, ts, td, rsd])
        y_sys_list.append(float(o_sys))
        y_dia_list.append(float(o_dia))

    n_samples = len(X_list)
    if n_samples < 3:
        return jsonify({
            "status": "error",
            "n_samples": n_samples,
            "message": f"Baru tersedia {n_samples} pasangan lengkap (SYS & DIA) antara MAX30102 dan Omron HEM-7156T. Kumpulkan minimal 3 pasangan untuk fine-tuning model ML (atau gunakan tombol Auto-Kalibrasi Offset untuk 1–2 sampel)."
        }), 400

    from sklearn.ensemble import RandomForestRegressor, VotingRegressor
    from sklearn.linear_model import Ridge
    from sklearn.preprocessing import StandardScaler
    from sklearn.pipeline import Pipeline
    from sklearn.model_selection import LeaveOneOut, KFold, cross_val_predict
    from sklearn.metrics import mean_absolute_error, mean_squared_error

    X = np.array(X_list, dtype=float)
    y_sys = np.array(y_sys_list, dtype=float)
    y_dia = np.array(y_dia_list, dtype=float)

    def _build_estimator():
        ridge_pipe = Pipeline([("scaler", StandardScaler()), ("ridge", Ridge(alpha=3.0))])
        if n_samples >= 6:
            rf = RandomForestRegressor(n_estimators=100, max_depth=4, min_samples_leaf=2, random_state=42)
            return VotingRegressor([("ridge", ridge_pipe), ("rf", rf)])
        return ridge_pipe

    model_sys = _build_estimator()
    model_dia = _build_estimator()

    cv = LeaveOneOut() if n_samples < 15 else KFold(n_splits=5, shuffle=True, random_state=42)
    cv_pred_sys = cross_val_predict(model_sys, X, y_sys, cv=cv)
    cv_pred_dia = cross_val_predict(model_dia, X, y_dia, cv=cv)

    mae_sys = round(float(mean_absolute_error(y_sys, cv_pred_sys)), 2)
    rmse_sys = round(float(np.sqrt(mean_squared_error(y_sys, cv_pred_sys))), 2)
    mae_dia = round(float(mean_absolute_error(y_dia, cv_pred_dia)), 2)
    rmse_dia = round(float(np.sqrt(mean_squared_error(y_dia, cv_pred_dia))), 2)

    model_sys.fit(X, y_sys)
    model_dia.fit(X, y_dia)

    os.makedirs(os.path.join(BASE_DIR, "model"), exist_ok=True)
    mpath = os.path.join(BASE_DIR, "model", "max30102_ppg_omron.joblib")
    bundle = {
        "model_sys": model_sys,
        "model_dia": model_dia,
        "features": ["raw_bpm", "ac_dc_ir", "ac_dc_red", "r_ratio", "ts_rise_sec", "td_decay_sec", "rsd_ratio"],
        "n_samples": n_samples,
        "trained_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "cv_metrics": {
            "mae_sys": mae_sys,
            "rmse_sys": rmse_sys,
            "mae_dia": mae_dia,
            "rmse_dia": rmse_dia
        }
    }
    joblib.dump(bundle, mpath)

    # Karena model ML baru sudah menyerap baseline SYS & DIA terhadap Omron, setel sys_bias & dia_bias ke 0.0 agar tidak double-offset
    cfg = load_calibration()
    cfg["tekanan_darah"] = {"sys_bias": 0.0, "dia_bias": 0.0}
    if st.get("recommended_bpm_bias") is not None:
        cfg["bpm"] = {"bias": float(st["recommended_bpm_bias"])}
    try:
        with open(CALIBRATION_FILE, "w") as f:
            json.dump(cfg, f, indent=4)
    except Exception:
        pass

    return jsonify({
        "status": "success",
        "n_samples": n_samples,
        "metrics": bundle["cv_metrics"],
        "message": (
            f"Fine-Tuning Model ML MAX30102 terhadap Omron HEM-7156T berhasil dari {n_samples} sampel! "
            f"(MAE CV: SYS ±{mae_sys} mmHg, DIA ±{mae_dia} mmHg) dan langsung aktif."
        )
    })


@app.route("/api/ml_dataset/export_ppg_omron_csv")
def export_ppg_omron_csv():
    if g.user is None or g.user[2] != "admin":
        return jsonify({"status": "error", "message": "Unauthorized"}), 401

    st = get_ppg_omron_comparison_stats()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ppg_log_id", "omron_log_id", "created_at", "patient_name", "umur", "jenis_kelamin",
        "sensor_sys_mmhg", "sensor_dia_mmhg", "sensor_bpm", "sensor_spo2_pct",
        "omron_sys_mmhg", "omron_dia_mmhg", "omron_bpm",
        "err_sys_mmhg", "err_dia_mmhg", "err_bpm",
        "raw_sys_unbiased", "raw_dia_unbiased", "raw_bpm_unbiased",
        "dc_ir", "dc_red", "rms_ir", "rms_red", "ac_dc_ir", "ac_dc_red", "r_ratio",
        "ts_rise_sec", "td_decay_sec", "rsd_ratio"
    ])
    for p in st["pairs"]:
        rf = p.get("rframes") or {}
        rv = p.get("rvals") or {}
        writer.writerow([
            p["ppg_id"], p.get("omron_id") or "", p["created_at"], p["patient_name"], p["umur"], p["jenis_kelamin"],
            p["sensor_sys"], p["sensor_dia"], p["sensor_bpm"], p["sensor_spo2"],
            p["omron_sys"], p["omron_dia"], p["omron_bpm"],
            p["err_sys"], p["err_dia"] if p["err_dia"] is not None else "", p["err_bpm"] if p["err_bpm"] is not None else "",
            p["raw_sys_unbiased"], p["raw_dia_unbiased"], p["raw_bpm_unbiased"],
            rf.get("dc_ir") or rv.get("dc_ir") or "",
            rf.get("dc_red") or rv.get("dc_red") or "",
            rf.get("rms_ir") or rv.get("rms_ir") or "",
            rf.get("rms_red") or rv.get("rms_red") or "",
            rf.get("ac_dc_ir") or rv.get("ac_dc_ir") or "",
            rf.get("ac_dc_red") or rv.get("ac_dc_red") or "",
            rf.get("r_ratio") or rv.get("r_ratio") or "",
            rf.get("ts_rise_sec") or rv.get("ts_rise_sec") or "",
            rf.get("td_decay_sec") or rv.get("td_decay_sec") or "",
            rf.get("rsd_ratio") or rv.get("rsd_ratio") or ""
        ])

    fname = f"medisyn_pembanding_max30102_vs_omron_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


@app.route("/laporan_evaluasi")
def laporan_evaluasi():
    if g.user is None or g.user[2] != "admin":
        flash("Akses ditolak. Halaman ini khusus untuk Administrator.", "danger")
        return redirect(url_for("dashboard"))

    metrics = hitung_evaluasi_data_science()
    ppg_omron_stats = get_ppg_omron_comparison_stats()
    dataset_stats = {
        "total_spektro": 0,
        "total_labeled": 0,
        "gula_labeled": 0,
        "kol_labeled": 0,
        "urat_labeled": 0,
        "models": {}
    }
    raw_records = []

    for tkey in ("gula_darah", "kolesterol", "asam_urat"):
        mpath = os.path.join(BASE_DIR, "model", f"as7265x_{tkey}.joblib")
        if os.path.exists(mpath):
            try:
                b = joblib.load(mpath)
                if isinstance(b, dict):
                    dataset_stats["models"][tkey] = {
                        "active": True,
                        "n_samples": b.get("n_samples", 0),
                        "trained_at": b.get("trained_at", "-"),
                        "cv_metrics": b.get("cv_metrics", {})
                    }
            except Exception:
                pass

    try:
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute(
            """
            SELECT
                COUNT(*) FILTER (WHERE sensor_type LIKE '%as7265x'),
                COUNT(*) FILTER (WHERE sensor_type LIKE '%as7265x' AND ground_truth_value IS NOT NULL),
                COUNT(*) FILTER (WHERE sensor_type = 'gula_as7265x' AND ground_truth_value IS NOT NULL),
                COUNT(*) FILTER (WHERE sensor_type = 'kolesterol_as7265x' AND ground_truth_value IS NOT NULL),
                COUNT(*) FILTER (WHERE sensor_type = 'asam_urat_as7265x' AND ground_truth_value IS NOT NULL)
            FROM raw_sensor_data
            """
        )
        srow = cur.fetchone()
        if srow:
            dataset_stats["total_spektro"] = srow[0] or 0
            dataset_stats["total_labeled"] = srow[1] or 0
            dataset_stats["gula_labeled"]  = srow[2] or 0
            dataset_stats["kol_labeled"]   = srow[3] or 0
            dataset_stats["urat_labeled"]  = srow[4] or 0

        cur.execute(
            """
            SELECT r.id, r.created_at, COALESCE(p.nama_lengkap, 'Tanpa Nama'),
                   r.sensor_type, r.raw_values, r.raw_frames,
                   r.predicted_value, r.ground_truth_value, r.ground_truth_source, r.notes
            FROM raw_sensor_data r
            LEFT JOIN patients p ON p.id = r.patient_id
            ORDER BY r.id DESC
            LIMIT 80
            """
        )
        for row in cur.fetchall():
            rframes = row[5] if isinstance(row[5], dict) else (json.loads(row[5]) if row[5] else {})
            pred_v = row[6]
            gt_v = row[7]
            abs_err = round(abs(float(pred_v) - float(gt_v)), 2) if (pred_v is not None and gt_v is not None) else None
            raw_records.append({
                "id": row[0],
                "created_at": row[1].strftime("%d/%m/%Y %H:%M") if row[1] else "-",
                "patient_name": row[2],
                "sensor_type": row[3],
                "raw_values": row[4],
                "valid_frames": rframes.get("valid_frames") or rframes.get("n_samples") or "-",
                "total_frames": rframes.get("total_frames") or rframes.get("n_samples") or "-",
                "sampling_hz": rframes.get("sampling_hz") or "-",
                "predicted_value": pred_v,
                "ground_truth_value": gt_v,
                "ground_truth_source": row[8] or "",
                "notes": row[9] or "",
                "abs_error": abs_err
            })
        cur.close()
        conn.close()
    except Exception as e:
        print(f"[EVAL TABLE WARN] {e}")

    return render_template(
        "evaluation.html",
        metrics=metrics,
        ppg_omron_stats=ppg_omron_stats,
        dataset_stats=dataset_stats,
        raw_records=raw_records,
        channel_names=SPECTRAL_18_CHANNELS
    )


if __name__ == "__main__":
    init_database_and_defaults()
    init_printer_connection()
    start_mqtt_background()
    print("Menjalankan server web AKMv2 di 0.0.0.0:5000...")
    socketio.run(app, debug=False, host="0.0.0.0", port=5000, allow_unsafe_werkzeug=True)
