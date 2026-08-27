import hashlib
import os
import tempfile
import unittest
import uuid
from unittest.mock import Mock, patch

from attachment_store import (
    AttachmentStore,
    AttachmentStoreError,
    validate_attachment_metadata,
)


def _id() -> str:
    return uuid.uuid4().hex


def _metadata(attachment_id: str, data: bytes, extension=".txt", name="sample.txt"):
    return {
        "id": attachment_id,
        "name": name,
        "kind": "text" if extension in {".txt", ".md", ".json", ".csv"} else "image",
        "mime_type": "text/plain" if extension in {".txt", ".md", ".json", ".csv"} else ("image/png" if extension == ".png" else "image/jpeg"),
        "extension": extension,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class AttachmentStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="attachment_store_")
        self.store = AttachmentStore(os.path.join(self.temp.name, "attachments"))
        self.sid = _id()

    def tearDown(self):
        self.temp.cleanup()

    def _commit_add(self, metadata, data):
        self.store.write_pending_add(self.sid, metadata, data)
        self.store.finalize_add(self.sid, metadata)

    def test_metadata_rejects_path_and_data_keys(self):
        data = b"safe text"
        item = _metadata(_id(), data)
        item["path"] = r"C:\\secret.txt"
        with self.assertRaisesRegex(AttachmentStoreError, "形式"):
            validate_attachment_metadata(item)

    def test_invalid_ids_and_extension_are_rejected(self):
        with self.assertRaises(AttachmentStoreError):
            self.store.write_pending_add("../bad", _metadata(_id(), b"x"), b"x")
        item = _metadata(_id(), b"x", extension=".exe", name="bad.exe")
        with self.assertRaises(AttachmentStoreError):
            self.store.write_pending_add(self.sid, item, b"x")

    def test_same_name_different_ids_are_independent(self):
        first = _metadata(_id(), b"first", name="same.txt")
        second = _metadata(_id(), b"second", name="same.txt")
        self._commit_add(first, b"first")
        self._commit_add(second, b"second")
        self.assertEqual(self.store.read(self.sid, first), b"first")
        self.assertEqual(self.store.read(self.sid, second), b"second")

    def test_pending_add_recovers_after_metadata_commit(self):
        item = _metadata(_id(), b"pending")
        self.store.write_pending_add(self.sid, item, b"pending")
        self.assertEqual(self.store.recover_session(self.sid, [item]), [])
        self.assertEqual(self.store.read(self.sid, item), b"pending")

    def test_pending_add_without_metadata_is_cleaned(self):
        item = _metadata(_id(), b"pending")
        self.store.write_pending_add(self.sid, item, b"pending")
        self.assertEqual(self.store.recover_session(self.sid, []), [])
        with self.assertRaises(AttachmentStoreError) as caught:
            self.store.read(self.sid, item)
        self.assertEqual(caught.exception.code, "missing")

    def test_pending_delete_restores_or_commits_from_metadata(self):
        item = _metadata(_id(), b"delete")
        self._commit_add(item, b"delete")
        self.store.stage_delete(self.sid, item)
        self.assertEqual(self.store.recover_session(self.sid, [item]), [])
        self.assertEqual(self.store.read(self.sid, item), b"delete")
        self.store.stage_delete(self.sid, item)
        self.assertEqual(self.store.recover_session(self.sid, []), [])
        with self.assertRaises(AttachmentStoreError):
            self.store.read(self.sid, item)

    def test_missing_size_and_hash_mismatches_have_distinct_reasons(self):
        item = _metadata(_id(), b"value")
        self._commit_add(item, b"value")
        path = os.path.join(self.store.root, self.sid, item["id"] + item["extension"])
        os.remove(path)
        with self.assertRaises(AttachmentStoreError) as missing:
            self.store.read(self.sid, item)
        self.assertEqual(missing.exception.code, "missing")
        self._commit_add(item, b"value")
        with open(path, "wb") as handle:
            handle.write(b"x")
        with self.assertRaises(AttachmentStoreError) as size:
            self.store.read(self.sid, item)
        self.assertEqual(size.exception.code, "size_mismatch")
        with open(path, "wb") as handle:
            handle.write(b"other")
        with self.assertRaises(AttachmentStoreError) as digest:
            self.store.read(self.sid, item)
        self.assertEqual(digest.exception.code, "hash_mismatch")

    def test_session_delete_pending_does_not_touch_other_session(self):
        first = _metadata(_id(), b"one")
        second_sid = _id()
        second = _metadata(_id(), b"two")
        self._commit_add(first, b"one")
        self.store.write_pending_add(second_sid, second, b"two")
        self.store.finalize_add(second_sid, second)
        transaction_id = _id()
        self.assertIsNotNone(self.store.stage_session_delete(
            self.sid, transaction_id))
        result = self.store.commit_session_delete(self.sid, transaction_id)
        self.assertEqual(result.failed + result.skipped + result.cleanup_pending, 0)
        self.assertEqual(self.store.read(second_sid, second), b"two")

    def test_session_delete_can_be_restored_before_commit(self):
        item = _metadata(_id(), b"restore")
        self._commit_add(item, b"restore")
        transaction_id = _id()
        self.store.stage_session_delete(self.sid, transaction_id)
        self.assertTrue(self.store.restore_session_delete(
            self.sid, transaction_id))
        self.assertEqual(self.store.read(self.sid, item), b"restore")

    def test_unknown_or_symlink_entry_blocks_namespace_cleanup(self):
        item = _metadata(_id(), b"safe")
        self._commit_add(item, b"safe")
        transaction_id = _id()
        pending = self.store.stage_session_delete(self.sid, transaction_id)
        with open(os.path.join(pending, "unexpected.bin"), "wb") as handle:
            handle.write(b"keep")
        result = self.store.commit_session_delete(self.sid, transaction_id)
        self.assertTrue(result.cleanup_pending)
        self.assertTrue(os.path.exists(os.path.join(pending, "unexpected.bin")))

    def test_session_namespaces_excludes_windows_reparse_points(self):
        entry = Mock()
        entry.name = self.sid
        entry.path = os.path.join(self.store.root, self.sid)
        entry.is_symlink.return_value = False
        entry.is_dir.return_value = True
        with (
            patch("attachment_store.os.path.lexists", return_value=True),
            patch.object(
                self.store, "_ensure_root", return_value=self.store.root),
            patch("attachment_store.os.scandir", return_value=[entry]),
            patch("attachment_store._is_reparse", return_value=True),
        ):
            self.assertEqual(self.store.session_namespaces(), [])

    @unittest.skipUnless(hasattr(os, "symlink"), "symlink unavailable")
    def test_symlink_is_rejected_when_platform_permits_creation(self):
        item = _metadata(_id(), b"safe")
        self._commit_add(item, b"safe")
        path = os.path.join(self.store.root, self.sid, item["id"] + item["extension"])
        target = os.path.join(self.temp.name, "outside.txt")
        with open(target, "wb") as handle:
            handle.write(b"outside")
        try:
            os.remove(path)
            os.symlink(target, path)
        except OSError:
            self.skipTest("symlink creation is not permitted")
        with self.assertRaises(AttachmentStoreError) as caught:
            self.store.read(self.sid, item)
        self.assertEqual(caught.exception.code, "unsafe_path")


if __name__ == "__main__":
    unittest.main()
