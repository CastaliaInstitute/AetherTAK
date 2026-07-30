import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from aether_field_api import (
    ApiError,
    FieldStore,
    Mutation,
    PublishedRecord,
    field_identity,
)


class FieldStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = FieldStore(root / "field.sqlite3", root / "media")

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def guardian_participant():
        participant_id = "208176c4-c5fe-4dce-a774-4d7128db06c4"
        return {
            "id": participant_id,
            "displayName": "River",
            "mode": "child",
            "team": "Castalia",
            "state": "caution",
            "zone": "Orchard",
            "alertState": "warning",
            "checkIn": "due",
            "location": {
                "coordinate": {
                    "latitude": 39.7392,
                    "longitude": -104.9903,
                    "altitudeMeters": 1609.0,
                    "horizontalAccuracyMeters": 8.0,
                    "verticalAccuracyMeters": None,
                    "headingDegrees": 75.0,
                },
                "source": "watch_gnss",
                "confidence": "good",
                "observedAt": "2026-07-30T08:15:00.000Z",
            },
            "device": {
                "connectivity": "guardian_ble",
                "lastContactAt": "2026-07-30T08:15:00.000Z",
                "batteryPercent": 72.0,
            },
            "updatedAt": "2026-07-30T08:15:00.000Z",
        }

    @staticmethod
    def guardian_alert():
        return {
            "id": "a62a3f50-b222-49e5-973a-0f5731d1ff8d",
            "participantId": "208176c4-c5fe-4dce-a774-4d7128db06c4",
            "ruleId": "check-in-overdue",
            "severity": "warning",
            "status": "active",
            "reasonCode": "MISSED_CHECK_IN",
            "title": "Check-in overdue",
            "detail": "River has not checked in.",
            "openedAt": "2026-07-30T08:10:00.000Z",
            "acknowledgedAt": None,
            "resolvedAt": None,
            "resolutionReason": None,
            "updatedAt": "2026-07-30T08:10:00.000Z",
        }

    @staticmethod
    def guardian_zone():
        return {
            "id": "51cc59ca-7298-4b28-8105-702010cdcc71",
            "propertyId": "e969f10f-9a45-4964-9646-d8f3dc10d506",
            "name": "Creek caution area",
            "level": "yellow",
            "boundary": [
                [-104.9917, 39.7418],
                [-104.9887, 39.7413],
                [-104.9892, 39.7388],
                [-104.9917, 39.7418],
            ],
            "enterDwellSeconds": 10,
            "exitDwellSeconds": 30,
            "active": True,
            "updatedAt": "2026-07-30T08:10:00.000Z",
        }

    def publish_guardian(self, entity_type, payload):
        record = PublishedRecord.from_json(
            {
                "entityType": entity_type,
                "entityId": payload["id"],
                "operation": "upsert",
                "payload": payload,
            }
        )
        return self.store.publish_record(record, "Guardian Fusion")

    def test_idempotent_mutation_and_cursor_changes(self):
        mutation = Mutation(
            id="mutation-1",
            entity_type="observation",
            entity_id="observation-1",
            operation="create",
            payload={"title": "Canopy check"},
            base_revision=None,
        )
        first = self.store.apply_mutation(mutation, "Field One")
        replay = self.store.apply_mutation(mutation, "Field One")
        self.assertFalse(first["idempotentReplay"])
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(first["revision"], replay["revision"])
        self.assertEqual(first["revision"], 1)
        page = self.store.changes(0, 100)
        self.assertEqual(len(page["changes"]), 1)
        self.assertEqual(page["nextCursor"], first["cursor"])

    def test_field_identity_reports_effective_certificate_roles(self):
        identity = field_identity(
            "Field Supervisor",
            publisher_cns=frozenset({"Al"}),
            guardian_checkin_cns=frozenset({"Field Participant"}),
            guardian_supervisor_cns=frozenset({"Field Supervisor"}),
        )
        self.assertEqual(
            identity,
            {
                "authenticated": True,
                "commonName": "Field Supervisor",
                "permissions": {
                    "publisher": False,
                    "guardianCheckIn": True,
                    "guardianSupervisor": True,
                },
            },
        )

    def test_field_identity_is_bounded(self):
        with self.assertRaises(ApiError) as caught:
            field_identity(
                "x" * 129,
                publisher_cns=frozenset(),
                guardian_checkin_cns=frozenset(),
                guardian_supervisor_cns=frozenset(),
            )
        self.assertEqual(caught.exception.code, "CLIENT_IDENTITY_INVALID")

    def test_stale_update_returns_current_entity(self):
        created = self.store.apply_mutation(
            Mutation("create", "field", "field-1", "create", {"name": "North"}, None),
            "Field One",
        )
        self.store.apply_mutation(
            Mutation(
                "update-1",
                "field",
                "field-1",
                "update",
                {"name": "North A"},
                created["revision"],
            ),
            "Field One",
        )
        with self.assertRaises(ApiError) as caught:
            self.store.apply_mutation(
                Mutation("update-2", "field", "field-1", "update", {"name": "Old"}, 1),
                "Field Two",
            )
        self.assertEqual(caught.exception.code, "REVISION_CONFLICT")
        self.assertEqual(caught.exception.details["current"]["revision"], 2)

    def test_publisher_managed_entities_are_read_only_for_mobile_mutations(self):
        for entity_type in (
            "sensor_reading",
            "al_insight",
            "guardian_participant",
            "guardian_alert",
            "guardian_zone",
        ):
            with self.subTest(entity_type=entity_type), self.assertRaises(ApiError) as caught:
                Mutation.from_json(
                    {
                        "id": f"mutation-{entity_type}",
                        "entityType": entity_type,
                        "entityId": "published-1",
                        "operation": "create",
                        "payload": {},
                    }
                )
            self.assertEqual(caught.exception.code, "READ_ONLY_ENTITY")

    def test_guardian_participant_schema_rejects_biometric_leakage(self):
        participant = self.guardian_participant()
        participant["heartRate"] = 91
        with self.assertRaises(ApiError) as caught:
            PublishedRecord.from_json(
                {
                    "entityType": "guardian_participant",
                    "entityId": participant["id"],
                    "operation": "upsert",
                    "payload": participant,
                }
            )
        self.assertEqual(caught.exception.code, "INVALID_PUBLISHED_PAYLOAD")

    def test_guardian_alert_schema_enforces_lifecycle(self):
        alert = self.guardian_alert()
        alert["status"] = "resolved"
        with self.assertRaises(ApiError) as caught:
            PublishedRecord.from_json(
                {
                    "entityType": "guardian_alert",
                    "entityId": alert["id"],
                    "operation": "upsert",
                    "payload": alert,
                }
            )
        self.assertEqual(caught.exception.code, "INVALID_PUBLISHED_PAYLOAD")

    def test_guardian_zone_schema_is_strict_and_requires_closed_geometry(self):
        zone = self.guardian_zone()
        record = PublishedRecord.from_json(
            {
                "entityType": "guardian_zone",
                "entityId": zone["id"],
                "operation": "upsert",
                "payload": zone,
            }
        )
        self.assertEqual(record.payload["level"], "yellow")

        zone["boundary"] = zone["boundary"][:-1]
        with self.assertRaises(ApiError) as caught:
            PublishedRecord.from_json(
                {
                    "entityType": "guardian_zone",
                    "entityId": zone["id"],
                    "operation": "upsert",
                    "payload": zone,
                }
            )
        self.assertEqual(caught.exception.code, "INVALID_PUBLISHED_PAYLOAD")

    def test_guardian_actions_update_change_feed_and_replay_idempotently(self):
        participant = self.guardian_participant()
        alert = self.guardian_alert()
        self.publish_guardian("guardian_participant", participant)
        self.publish_guardian("guardian_alert", alert)

        check_in_key = "122ed00d-6f35-4b49-b9cf-f8c7144303bd"
        checked_in = self.store.apply_guardian_action(
            idempotency_key=check_in_key,
            action="check_in",
            target_id=participant["id"],
            reason=None,
            observed_at="2026-07-30T08:20:00.000Z",
            author_cn="Field One",
        )
        replay = self.store.apply_guardian_action(
            idempotency_key=check_in_key,
            action="check_in",
            target_id=participant["id"],
            reason=None,
            observed_at="2026-07-30T08:20:00.000Z",
            author_cn="Field One",
        )
        acknowledged = self.store.apply_guardian_action(
            idempotency_key="acefbff6-7474-40c6-84d7-117702944808",
            action="acknowledge",
            target_id=alert["id"],
            reason=None,
            observed_at=None,
            author_cn="Supervisor One",
        )
        resolved = self.store.apply_guardian_action(
            idempotency_key="7ddc9244-4141-4169-9970-0853123804e7",
            action="resolve",
            target_id=alert["id"],
            reason="Participant safely returned home.",
            observed_at=None,
            author_cn="Supervisor One",
        )

        self.assertEqual(
            set(checked_in),
            {
                "accepted",
                "idempotencyKey",
                "action",
                "targetId",
                "serverTime",
                "entityVersion",
                "idempotentReplay",
            },
        )
        self.assertTrue(replay["idempotentReplay"])
        self.assertEqual(checked_in["entityVersion"], replay["entityVersion"])
        self.assertEqual(acknowledged["entityVersion"], 2)
        self.assertEqual(resolved["entityVersion"], 3)
        participant_entity = self.store._entity_from_row(
            self._entity_row("guardian_participant", participant["id"])
        )
        alert_entity = self.store._entity_from_row(
            self._entity_row("guardian_alert", alert["id"])
        )
        self.assertEqual(participant_entity["payload"]["checkIn"], "current")
        self.assertEqual(alert_entity["payload"]["status"], "resolved")
        self.assertEqual(
            alert_entity["payload"]["resolutionReason"],
            "Participant safely returned home.",
        )
        changes = self.store.changes(0, 100)["changes"]
        self.assertEqual(len(changes), 5)

    def test_guardian_actions_reject_missing_targets_and_invalid_transitions(self):
        with self.assertRaises(ApiError) as missing:
            self.store.apply_guardian_action(
                idempotency_key="bdd40a8e-9ce0-48e8-9e30-274b2f8236ef",
                action="check_in",
                target_id="6df2a29b-8d46-4a05-a647-f3a1ea659c13",
                reason=None,
                observed_at="2026-07-30T08:20:00.000Z",
                author_cn="Field One",
            )
        self.assertEqual(missing.exception.code, "GUARDIAN_TARGET_NOT_FOUND")

        alert = self.guardian_alert()
        self.publish_guardian("guardian_alert", alert)
        self.store.apply_guardian_action(
            idempotency_key="1f70e41c-f21a-4094-84da-e781abce30bf",
            action="resolve",
            target_id=alert["id"],
            reason="False alarm confirmed by supervisor.",
            observed_at=None,
            author_cn="Supervisor One",
        )
        with self.assertRaises(ApiError) as transition:
            self.store.apply_guardian_action(
                idempotency_key="d01408fc-d3dc-49e1-beef-30bdf52c62f0",
                action="acknowledge",
                target_id=alert["id"],
                reason=None,
                observed_at=None,
                author_cn="Supervisor One",
            )
        self.assertEqual(transition.exception.code, "GUARDIAN_ALERT_RESOLVED")

    def test_guardian_idempotency_key_cannot_change_target_or_body(self):
        participant = self.guardian_participant()
        self.publish_guardian("guardian_participant", participant)
        key = "e4150473-14be-47f0-8cd8-e4215c1fa95e"
        self.store.apply_guardian_action(
            idempotency_key=key,
            action="check_in",
            target_id=participant["id"],
            reason=None,
            observed_at="2026-07-30T08:20:00.000Z",
            author_cn="Field One",
        )
        with self.assertRaises(ApiError) as reused:
            self.store.apply_guardian_action(
                idempotency_key=key,
                action="check_in",
                target_id="6df2a29b-8d46-4a05-a647-f3a1ea659c13",
                reason=None,
                observed_at="2026-07-30T08:21:00.000Z",
                author_cn="Field One",
            )
        self.assertEqual(reused.exception.code, "IDEMPOTENCY_KEY_REUSED")
        with self.assertRaises(ApiError) as changed_body:
            self.store.apply_guardian_action(
                idempotency_key=key,
                action="check_in",
                target_id=participant["id"],
                reason=None,
                observed_at="2026-07-30T08:22:00.000Z",
                author_cn="Field One",
            )
        self.assertEqual(changed_body.exception.code, "IDEMPOTENCY_KEY_REUSED")

    def test_rejects_published_payloads_that_could_poison_mobile_sync(self):
        with self.assertRaises(ApiError) as caught:
            PublishedRecord.from_json(
                {
                    "entityType": "al_insight",
                    "entityId": "1902a29f-b98e-46f0-89d4-d09ac7fecaba",
                    "operation": "upsert",
                    "payload": {
                        "id": "1902a29f-b98e-46f0-89d4-d09ac7fecaba",
                        "title": "Unsafe insight",
                        "summary": "Missing required read-only contract.",
                        "rationale": "",
                        "sourceReadingIds": [],
                        "fieldId": None,
                        "siteId": None,
                        "severity": "info",
                        "generatedAt": "2026-07-30T08:15:00.000Z",
                        "expiresAt": "2026-07-31T08:15:00.000Z",
                        "readOnly": False,
                    },
                }
            )
        self.assertEqual(caught.exception.code, "INVALID_PUBLISHED_PAYLOAD")

    def test_published_records_are_revisioned_idempotent_and_deletable(self):
        created = self.store.publish_record(
            PublishedRecord(
                entity_type="sensor_reading",
                entity_id="reading-1",
                operation="upsert",
                payload={"value": 19.4, "unit": "%"},
            ),
            "Al",
        )
        replay = self.store.publish_record(
            PublishedRecord(
                entity_type="sensor_reading",
                entity_id="reading-1",
                operation="upsert",
                payload={"unit": "%", "value": 19.4},
            ),
            "Al",
        )
        updated = self.store.publish_record(
            PublishedRecord(
                entity_type="sensor_reading",
                entity_id="reading-1",
                operation="upsert",
                payload={"value": 20.1, "unit": "%"},
            ),
            "Al",
        )
        deleted = self.store.publish_record(
            PublishedRecord(
                entity_type="sensor_reading",
                entity_id="reading-1",
                operation="delete",
                payload=None,
            ),
            "Al",
        )

        self.assertEqual(created["revision"], 1)
        self.assertTrue(replay["idempotentReplay"])
        self.assertIsNone(replay["cursor"])
        self.assertEqual(updated["revision"], 2)
        self.assertEqual(deleted["revision"], 3)
        changes = self.store.changes(0, 100)["changes"]
        self.assertEqual(
            [change["operation"] for change in changes],
            ["create", "update", "delete"],
        )
        self.assertTrue(
            self.store._entity_from_row(
                self._entity_row("sensor_reading", "reading-1")
            )["deleted"]
        )

    def _entity_row(self, entity_type, entity_id):
        with self.store._connect() as connection:
            return connection.execute(
                "SELECT * FROM entities WHERE entity_type = ? AND entity_id = ?",
                (entity_type, entity_id),
            ).fetchone()

    def test_media_is_checksummed_and_idempotent(self):
        content = b"portable point cloud"
        checksum = hashlib.sha256(content).hexdigest()
        first = self.store.save_media(
            media_id="media-1",
            content_type="model/ply",
            expected_sha256=checksum,
            observation_id="observation-1",
            role="point_cloud",
            author_cn="Field One",
            stream=io.BytesIO(content),
            content_length=len(content),
        )
        replay = self.store.save_media(
            media_id="media-1",
            content_type="model/ply",
            expected_sha256=checksum,
            observation_id="observation-1",
            role="point_cloud",
            author_cn="Field One",
            stream=io.BytesIO(content),
            content_length=len(content),
        )
        self.assertFalse(first["idempotentReplay"])
        self.assertTrue(replay["idempotentReplay"])
        record, path = self.store.media_download("media-1")
        self.assertEqual(path.read_bytes(), content)
        self.assertEqual(record["content_type"], "model/ply")
        self.assertEqual(record["sha256"], checksum)

    def test_missing_and_invalid_indexed_media_are_not_downloaded(self):
        with self.assertRaises(ApiError) as missing:
            self.store.media_download("does-not-exist")
        self.assertEqual(missing.exception.code, "MEDIA_NOT_FOUND")

        content = b"field photo"
        checksum = hashlib.sha256(content).hexdigest()
        self.store.save_media(
            media_id="media-2",
            content_type="image/jpeg",
            expected_sha256=checksum,
            observation_id=None,
            role=None,
            author_cn="Field One",
            stream=io.BytesIO(content),
            content_length=len(content),
        )
        record, path = self.store.media_download("media-2")
        path.unlink()
        with self.assertRaises(ApiError) as unavailable:
            self.store.media_download(record["media_id"])
        self.assertEqual(unavailable.exception.code, "MEDIA_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
