import hashlib
import io
import tempfile
import unittest
from pathlib import Path

from aether_field_api import ApiError, FieldStore, Mutation


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
