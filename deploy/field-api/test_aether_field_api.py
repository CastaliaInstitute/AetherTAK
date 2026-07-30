import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from aether_field_api import ApiError, FieldStore, Mutation, PublishedRecord


class FieldStoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.store = FieldStore(root / "field.sqlite3", root / "media")

    def tearDown(self):
        self.temporary.cleanup()

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

    def test_sensor_and_al_entities_are_read_only_for_mobile_mutations(self):
        for entity_type in ("sensor_reading", "al_insight"):
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
