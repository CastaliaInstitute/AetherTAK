#!/usr/bin/env python3
"""Publish configured ChirpStack v4 uplinks into the Aether Field change feed."""

from __future__ import annotations

import json
import math
import os
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from aether_field_api import FieldStore, PublishedRecord


READING_NAMESPACE = uuid.UUID("74f0f96d-4446-48ac-a406-102de6632b30")
MEASUREMENTS = {
    "soil_moisture",
    "air_temperature",
    "soil_temperature",
    "humidity",
    "water_level",
    "conductivity",
    "ph",
}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if math.isfinite(numeric) else None


def _value_at_path(value: dict[str, Any], path: str) -> Any:
    current: Any = value
    for key in path.split("."):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


def _coordinate(value: dict[str, Any]) -> dict[str, Any]:
    latitude = _number(value.get("latitude"))
    longitude = _number(value.get("longitude"))
    if latitude is None or longitude is None:
        raise ValueError("A ChirpStack binding requires latitude and longitude.")
    return {
        "latitude": latitude,
        "longitude": longitude,
        "altitudeMeters": _number(value.get("altitudeMeters")),
        "horizontalAccuracyMeters": _number(value.get("horizontalAccuracyMeters")),
        "verticalAccuracyMeters": _number(value.get("verticalAccuracyMeters")),
        "headingDegrees": _number(value.get("headingDegrees")),
    }


def _gateway_coordinate(
    gateway: dict[str, Any] | None, fallback: dict[str, Any]
) -> dict[str, Any]:
    location = gateway.get("location") if gateway else None
    if not isinstance(location, dict):
        return fallback
    latitude = _number(location.get("latitude"))
    longitude = _number(location.get("longitude"))
    if latitude is None or longitude is None:
        return fallback
    return {
        "latitude": latitude,
        "longitude": longitude,
        "altitudeMeters": _number(location.get("altitude")),
        "horizontalAccuracyMeters": None,
        "verticalAccuracyMeters": None,
        "headingDegrees": None,
    }


def normalize_uplink(
    uplink: dict[str, Any], binding: dict[str, Any]
) -> list[dict[str, Any]]:
    device = uplink.get("deviceInfo")
    decoded = uplink.get("object")
    if not isinstance(device, dict) or not isinstance(decoded, dict):
        raise ValueError("ChirpStack uplink is missing deviceInfo or decoded object.")
    dev_eui = str(device.get("devEui", "")).lower()
    application_id = str(device.get("applicationId", ""))
    frame_counter = uplink.get("fCnt")
    f_port = uplink.get("fPort")
    if (
        not dev_eui
        or not application_id
        or isinstance(frame_counter, bool)
        or not isinstance(frame_counter, int)
        or frame_counter < 0
        or isinstance(f_port, bool)
        or not isinstance(f_port, int)
        or not 0 <= f_port <= 255
    ):
        raise ValueError("ChirpStack uplink identity and frame metadata are invalid.")
    recorded = datetime.fromisoformat(
        str(uplink.get("time", "")).replace("Z", "+00:00")
    )
    if recorded.tzinfo is None:
        raise ValueError("ChirpStack uplink time must include a UTC offset.")
    recorded_at = recorded.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    fallback = _coordinate(binding.get("coordinate", {}))
    rx_info = [
        item for item in uplink.get("rxInfo", []) if isinstance(item, dict)
    ]
    gateway = max(
        rx_info,
        key=lambda item: _number(item.get("rssi"))
        if _number(item.get("rssi")) is not None
        else -999.0,
        default=None,
    )
    coordinate = _gateway_coordinate(gateway, fallback)
    tx_info = uplink.get("txInfo")
    tx_info = tx_info if isinstance(tx_info, dict) else {}
    modulation = tx_info.get("modulation")
    modulation = modulation if isinstance(modulation, dict) else {}
    lora = modulation.get("lora")
    lora = lora if isinstance(lora, dict) else {}
    readings: list[dict[str, Any]] = []

    for mapping in binding.get("measurements", []):
        if not isinstance(mapping, dict):
            continue
        raw = _number(_value_at_path(decoded, str(mapping.get("key", ""))))
        measurement = mapping.get("measurement")
        unit = mapping.get("unit")
        if raw is None or measurement not in MEASUREMENTS or not isinstance(unit, str):
            continue
        scale_value = _number(mapping.get("scale"))
        offset_value = _number(mapping.get("offset"))
        scale = scale_value if scale_value is not None else 1.0
        offset = offset_value if offset_value is not None else 0.0
        spreading_factor = lora.get("spreadingFactor")
        if isinstance(spreading_factor, bool) or not isinstance(
            spreading_factor, int
        ):
            spreading_factor = None
        frequency = tx_info.get("frequency")
        if isinstance(frequency, bool) or not isinstance(frequency, int):
            frequency = None
        reading_id = str(
            uuid.uuid5(
                READING_NAMESPACE,
                f"{dev_eui}:{frame_counter}:{mapping['key']}",
            )
        )
        readings.append(
            {
                "id": reading_id,
                "deviceId": dev_eui,
                "fieldId": binding.get("fieldId"),
                "siteId": binding.get("siteId"),
                "label": mapping.get("label")
                or f"{binding.get('label', dev_eui)} {measurement}",
                "measurement": measurement,
                "value": round(raw * scale + offset, 12),
                "unit": unit,
                "quality": "good" if gateway else "estimated",
                "lorawan": {
                    "applicationId": application_id,
                    "devEui": dev_eui,
                    "fPort": f_port,
                    "frameCounter": frame_counter,
                    "gatewayIds": [
                        str(item["gatewayId"])
                        for item in rx_info
                        if item.get("gatewayId")
                    ],
                    "rssi": _number(gateway.get("rssi")) if gateway else None,
                    "snr": _number(gateway.get("snr")) if gateway else None,
                    "spreadingFactor": spreading_factor,
                    "frequencyHz": frequency,
                },
                "coordinate": coordinate,
                "recordedAt": recorded_at,
            }
        )
    return readings


def load_bindings(path: Path) -> dict[str, dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    devices = value.get("devices") if isinstance(value, dict) else None
    if not isinstance(devices, dict):
        raise ValueError("ChirpStack bindings must contain a devices object.")
    return {
        str(dev_eui).lower(): binding
        for dev_eui, binding in devices.items()
        if isinstance(binding, dict)
    }


def main() -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as error:
        raise RuntimeError("Install the pinned field API requirements.") from error

    bindings = load_bindings(
        Path(os.environ.get("AETHER_CHIRPSTACK_BINDINGS", "/config/bindings.json"))
    )
    store = FieldStore(
        Path(os.environ.get("AETHER_FIELD_DB", "/data/field.sqlite3")),
        Path(os.environ.get("AETHER_FIELD_MEDIA", "/data/media")),
    )
    password = Path(
        os.environ.get(
            "AETHER_CHIRPSTACK_MQTT_PASSWORD_FILE",
            "/run/secrets/mqtt-password",
        )
    ).read_text(encoding="utf-8").strip()
    username = os.environ.get("AETHER_CHIRPSTACK_MQTT_USERNAME", "aether-field")
    topic = os.environ.get(
        "AETHER_CHIRPSTACK_MQTT_TOPIC",
        "application/+/device/+/event/up",
    )

    def on_connect(client, _userdata, _flags, reason_code, _properties):
        if reason_code != 0:
            print(f"MQTT connection failed: {reason_code}", file=sys.stderr, flush=True)
            return
        client.subscribe(topic, qos=1)
        print(f"Aether Field subscribed to {topic}", flush=True)

    def on_message(_client, _userdata, message):
        try:
            uplink = json.loads(message.payload)
            device = uplink.get("deviceInfo", {})
            dev_eui = str(device.get("devEui", "")).lower()
            binding = bindings.get(dev_eui)
            if not binding:
                return
            for reading in normalize_uplink(uplink, binding):
                store.publish_record(
                    PublishedRecord.from_json(
                        {
                            "entityType": "sensor_reading",
                            "entityId": reading["id"],
                            "operation": "upsert",
                            "payload": reading,
                        }
                    ),
                    "ChirpStack",
                )
            print(f"Published ChirpStack uplink for {dev_eui}", flush=True)
        except Exception as error:
            print(f"Rejected ChirpStack uplink: {error}", file=sys.stderr, flush=True)

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id="aethertak-field-bridge",
        protocol=mqtt.MQTTv311,
    )
    client.username_pw_set(username, password)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(
        os.environ.get("AETHER_CHIRPSTACK_MQTT_HOST", "chirpstack-mosquitto"),
        int(os.environ.get("AETHER_CHIRPSTACK_MQTT_PORT", "1883")),
        keepalive=60,
    )
    client.loop_forever(retry_first_connection=True)


if __name__ == "__main__":
    main()
