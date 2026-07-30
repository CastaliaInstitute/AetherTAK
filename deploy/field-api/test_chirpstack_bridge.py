import unittest

from aether_field_api import PublishedRecord
from chirpstack_bridge import normalize_uplink


BINDING = {
    "fieldId": "28f77310-f12d-4fd5-8097-3387e83fd49f",
    "siteId": None,
    "label": "Bed 4",
    "coordinate": {
        "latitude": 39.7411,
        "longitude": -104.9949,
        "altitudeMeters": 1609,
        "horizontalAccuracyMeters": 3,
        "verticalAccuracyMeters": 5,
        "headingDegrees": None,
    },
    "measurements": [
        {
            "key": "soil.moisture",
            "measurement": "soil_moisture",
            "unit": "%",
            "label": "Bed 4 moisture",
            "scale": 0.1,
        },
        {
            "key": "air.temperature",
            "measurement": "air_temperature",
            "unit": "°C",
        },
    ],
}

UPLINK = {
    "time": "2026-07-30T05:00:00.000Z",
    "deviceInfo": {
        "applicationId": "aether-field",
        "applicationName": "Aether Field",
        "deviceName": "soil-001",
        "devEui": "0102030405060708",
    },
    "fCnt": 1432,
    "fPort": 10,
    "object": {
        "soil": {"moisture": 314},
        "air": {"temperature": 24.6},
    },
    "rxInfo": [
        {"gatewayId": "weak", "rssi": -110, "snr": -2},
        {
            "gatewayId": "aether-gw-01",
            "rssi": -87,
            "snr": 7.5,
            "location": {
                "latitude": 39.74,
                "longitude": -104.99,
                "altitude": 1610,
            },
        },
    ],
    "txInfo": {
        "frequency": 904300000,
        "modulation": {"lora": {"spreadingFactor": 7}},
    },
}


class ChirpStackBridgeTests(unittest.TestCase):
    def test_normalizes_bound_measurements_and_radio_metadata(self):
        readings = normalize_uplink(UPLINK, BINDING)

        self.assertEqual(len(readings), 2)
        self.assertEqual(readings[0]["measurement"], "soil_moisture")
        self.assertEqual(readings[0]["value"], 31.4)
        self.assertEqual(readings[0]["coordinate"]["latitude"], 39.74)
        self.assertEqual(readings[0]["lorawan"]["rssi"], -87)
        self.assertEqual(readings[0]["lorawan"]["spreadingFactor"], 7)
        PublishedRecord.from_json(
            {
                "entityType": "sensor_reading",
                "entityId": readings[0]["id"],
                "operation": "upsert",
                "payload": readings[0],
            }
        )

    def test_replay_generates_stable_reading_ids(self):
        first = normalize_uplink(UPLINK, BINDING)
        replay = normalize_uplink(UPLINK, BINDING)
        next_frame = normalize_uplink({**UPLINK, "fCnt": 1433}, BINDING)

        self.assertEqual(first[0]["id"], replay[0]["id"])
        self.assertNotEqual(first[0]["id"], next_frame[0]["id"])

    def test_uses_bound_location_without_gateway_metadata(self):
        readings = normalize_uplink({**UPLINK, "rxInfo": []}, BINDING)

        self.assertEqual(readings[0]["quality"], "estimated")
        self.assertEqual(readings[0]["coordinate"], BINDING["coordinate"])

    def test_rejects_undecoded_uplinks(self):
        with self.assertRaises(ValueError):
            normalize_uplink({**UPLINK, "object": None}, BINDING)


if __name__ == "__main__":
    unittest.main()
