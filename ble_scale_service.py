#!/usr/bin/env python3
"""
ble_scale_service.py - Layanan opsional BLE Tinggi & Berat Badan untuk AKMv2
Meneruskan hasil langsung ke topik akm/prediction_data (tanpa ketergantungan Node-RED)
dan mengembalikan status error jujur bila perangkat BLE sedang tidak aktif/terhubung.
"""

import os
import json
import asyncio
import paho.mqtt.client as mqtt

try:
    from bleak import BleakClient, BleakScanner
    HAS_BLEAK = True
except ImportError:
    HAS_BLEAK = False

MQTT_BROKER           = os.getenv("MQTT_BROKER_HOST", "localhost")
MQTT_PORT             = int(os.getenv("MQTT_BROKER_PORT", "1883"))
MQTT_CMD_TOPIC        = "akm/command"
MQTT_PREDICTION_TOPIC = "akm/prediction_data"
MQTT_STATUS_TOPIC     = "akm/connect"

WEIGHT_ADDRESS   = os.getenv("BLE_WEIGHT_MAC", "d8:e7:2f:0a:94:44")
WEIGHT_UUID      = "00002a9c-0000-1000-8000-00805f9b34fb"
HEIGHT_NAME      = os.getenv("BLE_HEIGHT_NAME", "bleScaleHeight")
HEIGHT_CHAR_UUID = "beb5483e-36e1-4688-b7f5-ea07361b26a8"

mqtt_client = None

weight_state = {
    "latest": 0.0,
    "stable": False,
    "connected": False,
    "capture_request": False
}

height_state = {
    "latest": 0.0,
    "connected": False,
    "capture_request": False
}


def publish_measurement(tipe, nilai):
    if mqtt_client:
        mqtt_client.publish(MQTT_PREDICTION_TOPIC, json.dumps({
            "type": tipe,
            "status": "done",
            "value": round(float(nilai), 2)
        }))
        print(f"[BLE SUCCESS] {tipe}: {round(float(nilai), 2)}")


def publish_ble_error(tipe, message):
    if mqtt_client:
        mqtt_client.publish(MQTT_PREDICTION_TOPIC, json.dumps({
            "type": tipe,
            "status": "error",
            "message": message
        }))


def on_connect(client, userdata, flags, reason_code, properties=None):
    print(f"[BLE MQTT] Terhubung ke Broker ({reason_code}). Subscribe ke {MQTT_CMD_TOPIC}")
    client.subscribe(MQTT_CMD_TOPIC)


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8").strip()
        if payload == "6":
            if not weight_state["connected"]:
                publish_ble_error(
                    "berat_badan",
                    "Timbangan BLE belum terhubung. Nyalakan timbangan atau gunakan tombol Input Manual."
                )
                return
            if weight_state["stable"] and weight_state["latest"] > 0:
                publish_measurement("berat_badan", weight_state["latest"])
                weight_state["capture_request"] = False
            else:
                weight_state["capture_request"] = True

        elif payload == "5":
            if not height_state["connected"]:
                publish_ble_error(
                    "tinggi_badan",
                    "Sensor Tinggi Badan BLE belum terhubung. Nyalakan alat atau gunakan tombol Input Manual."
                )
                return
            height_state["capture_request"] = True

    except Exception as e:
        print(f"[BLE MQTT] Error: {e}")


def parse_weight_data(data: bytearray):
    if len(data) < 13:
        return None
    ctrl_byte = data[0]
    stable = (ctrl_byte & 0x02) != 0
    catty = (ctrl_byte & 0x01) != 0
    lbs = (ctrl_byte & 0x10) != 0

    raw = int.from_bytes(data[11:13], byteorder="little")
    if catty:
        kg = (raw / 100.0) * 0.5
    elif lbs:
        kg = (raw / 100.0) * 0.453592
    else:
        kg = raw / 200.0

    return {"val": kg, "stable": stable}


def weight_notification_handler(sender, data):
    res = parse_weight_data(data)
    if not res:
        return
    weight_state["latest"] = res["val"]
    weight_state["stable"] = res["stable"]
    if weight_state["capture_request"] and weight_state["stable"] and weight_state["latest"] > 0:
        publish_measurement("berat_badan", weight_state["latest"])
        weight_state["capture_request"] = False


def height_notification_handler(sender, data):
    try:
        val = float(data.decode("utf-8").strip())
        height_state["latest"] = val
        if height_state["capture_request"]:
            publish_measurement("tinggi_badan", val)
            height_state["capture_request"] = False
    except Exception as e:
        print(f"[HEIGHT] Error decode: {e}")


async def run_weight_scale():
    if not HAS_BLEAK:
        return
    while True:
        weight_state["connected"] = False
        try:
            device = await BleakScanner.find_device_by_address(WEIGHT_ADDRESS, timeout=6.0)
            if device:
                async with BleakClient(device, timeout=25.0) as client:
                    mqtt_client.publish(MQTT_STATUS_TOPIC, "connected_ble_scale")
                    weight_state["connected"] = True
                    await client.start_notify(WEIGHT_UUID, weight_notification_handler)
                    while client.is_connected:
                        await asyncio.sleep(1)
        except Exception:
            if mqtt_client:
                mqtt_client.publish(MQTT_STATUS_TOPIC, "disconnected_ble_scale")
        await asyncio.sleep(5)


async def run_height_scale():
    if not HAS_BLEAK:
        return
    while True:
        height_state["connected"] = False
        try:
            device = await BleakScanner.find_device_by_name(HEIGHT_NAME, timeout=6.0)
            if device:
                async with BleakClient(device, timeout=25.0) as client:
                    height_state["connected"] = True
                    await client.start_notify(HEIGHT_CHAR_UUID, height_notification_handler)
                    while client.is_connected:
                        await asyncio.sleep(1)
        except Exception:
            pass
        await asyncio.sleep(5)


async def main():
    global mqtt_client
    mqtt_client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    mqtt_client.on_connect = on_connect
    mqtt_client.on_message = on_message

    while True:
        try:
            mqtt_client.connect(MQTT_BROKER, MQTT_PORT, 60)
            mqtt_client.loop_start()
            break
        except Exception as e:
            print(f"[BLE] Menunggu MQTT broker: {e}")
            await asyncio.sleep(3)

    if not HAS_BLEAK:
        print("[BLE] Library bleak tidak tersedia; layanan BLE pasif.")
        while True:
            await asyncio.sleep(60)

    await asyncio.gather(run_weight_scale(), run_height_scale())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
