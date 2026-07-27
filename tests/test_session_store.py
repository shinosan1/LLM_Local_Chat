import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from history_crypto import HistoryCryptoError
from session_store import (
    HistoryMigrationError,
    SessionStore,
    normalize_retention_days,
)


class FakeProtector:
    def protect(self, data):
        return b"protected:" + data[::-1]

    def unprotect(self, data):
        if not data.startswith(b"protected:"):
            raise HistoryCryptoError("invalid ciphertext")
        return data[len(b"protected:"):][::-1]


class CountingSessionStore(SessionStore):
    def __init__(self, log_dir):
        super().__init__(log_dir, protector=FakeProtector())
        self.metadata_reads = 0

    def _read_metadata(self, path, fallback_title):
        self.metadata_reads += 1
        return super()._read_metadata(path, fallback_title)


class SessionStoreIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="test_session_store_")
        self.store = CountingSessionStore(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, filename, title, summary="", activity=None):
        path = os.path.join(self.temp_dir.name, filename)
        data = {"title": title, "summary": summary, "history": []}
        if activity:
            data["last_activity_at"] = activity
        self.store._write_encrypted(path, data)
        return path

    def _write_legacy(self, filename, data, indent=None):
        path = os.path.join(self.temp_dir.name, filename)
        with open(path, "w", encoding="utf-8", newline="") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent)
        return path

    def test_repeated_search_reuses_decrypted_metadata(self):
        self._write("chat_1.json", "Alpha", "first")
        self._write("chat_2.json", "Beta", "second")
        self.assertEqual(len(self.store.list_sessions()), 2)
        self.assertEqual(self.store.metadata_reads, 2)
        self.assertEqual(
            [item["title"] for item in self.store.list_sessions("sec")],
            ["Beta"],
        )
        self.assertEqual(self.store.metadata_reads, 2)

    def test_save_is_encrypted_and_rename_delete_update_index(self):
        session = {
            "title": "Saved",
            "summary": "memo",
            "history": [{"user": "private text"}],
        }
        path = self.store.save(session, None)
        with open(path, "rb") as handle:
            stored = handle.read()
        self.assertNotIn(b"private text", stored)
        self.assertEqual(self.store.load(path)["title"], "Saved")
        self.store.rename(path, "Renamed")
        self.assertEqual(self.store.list_sessions()[0]["title"], "Renamed")
        self.store.delete(path)
        self.assertEqual(self.store.list_sessions(), [])

    def test_external_encrypted_change_and_new_file_are_detected(self):
        path = self._write("chat_1.json", "Before")
        self.store.list_sessions()
        self.store._write_encrypted(
            path, {"title": "After with longer title", "history": []})
        self._write("chat_2.json", "New")
        titles = [item["title"] for item in self.store.list_sessions()]
        self.assertEqual(titles, ["New", "After with longer title"])
        self.assertEqual(self.store.metadata_reads, 3)

    def test_external_replace_is_detected_with_same_mtime(self):
        path = self._write("chat_1.json", "Before")
        self.store.list_sessions()
        before = os.stat(path)
        self.store._write_encrypted(
            path, {"title": "After!", "history": []})
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        titles = [item["title"] for item in self.store.list_sessions()]
        self.assertEqual(titles, ["After!"])

    def test_broken_envelope_is_ignored_until_it_changes(self):
        broken = os.path.join(self.temp_dir.name, "chat_2.json")
        with open(broken, "wb") as handle:
            handle.write(b"{")
        self._write("chat_1.json", "Valid")
        self.assertEqual(
            [item["title"] for item in self.store.list_sessions()], ["Valid"])
        reads_after_first_scan = self.store.metadata_reads
        self.store.list_sessions("anything")
        self.assertEqual(self.store.metadata_reads, reads_after_first_scan)

    def test_new_session_paths_are_unique_when_timestamp_collides(self):
        fixed = datetime.datetime(2026, 7, 19, 12, 34, 56, 123456)
        session = {"title": "Saved", "history": [{"user": "u"}]}
        with patch("session_store.datetime.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = fixed
            mocked_datetime.side_effect = lambda *a, **k: datetime.datetime(*a, **k)
            first = self.store.save(session, None)
            second = self.store.save(session, None)
        self.assertNotEqual(first, second)

    def test_migration_preserves_exact_plaintext_bytes(self):
        path = self._write_legacy(
            "chat_1.json",
            {"title": "旧", "history": [], "unknown": {"x": 1}},
            indent=4,
        )
        with open(path, "rb") as handle:
            original = handle.read()
        legacy, errors = self.store.scan_legacy()
        self.assertEqual(errors, [])
        self.assertEqual(legacy, [path])
        self.assertEqual(self.store.migrate_legacy(legacy), 1)
        data, encrypted, plaintext = self.store._read_document(path)
        self.assertTrue(encrypted)
        self.assertEqual(plaintext, original)
        self.assertEqual(data["unknown"], {"x": 1})
        self.assertEqual(self.store.migrate_legacy([path]), 0)

    def test_migration_rescans_and_encrypts_new_legacy_file(self):
        first = self._write_legacy(
            "chat_1.json", {"title": "First", "history": []})
        second = self._write_legacy(
            "chat_2.json", {"title": "Second", "history": []})
        self.assertEqual(self.store.migrate_legacy([first]), 2)
        for path in (first, second):
            _data, encrypted, _raw = self.store._read_document(path)
            self.assertTrue(encrypted)

    def test_existing_migration_lock_is_not_removed_or_mutated(self):
        path = self._write_legacy(
            "chat_1.json", {"title": "旧", "history": []})
        with open(path, "rb") as handle:
            before = handle.read()
        lock_path = os.path.join(
            self.temp_dir.name, ".history_migration.lock")
        with open(lock_path, "wb") as handle:
            handle.write(b"12345")

        with self.assertRaisesRegex(
            HistoryCryptoError, "すべてのShiroが終了"
        ):
            self.store.migrate_legacy([path])

        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), before)
        with open(lock_path, "rb") as handle:
            self.assertEqual(handle.read(), b"12345")

    def test_migration_failure_keeps_safe_diagnostics_and_can_resume(self):
        first = self._write_legacy(
            "chat_1.json", {"title": "First", "history": []})
        second = self._write_legacy(
            "chat_2.json", {"title": "Second", "history": []})
        real_write = __import__("session_store").atomic_write_bytes

        def fail_second(path, data):
            if path == second:
                raise OSError("secret diagnostic text")
            return real_write(path, data)

        with patch("session_store.atomic_write_bytes", side_effect=fail_second):
            with self.assertRaises(HistoryMigrationError) as caught:
                self.store.migrate_legacy([first, second])

        details = caught.exception.diagnostics
        self.assertTrue(any(
            detail == {
                "name": "chat_2.json",
                "phase": "encrypt",
                "error_type": "OSError",
            }
            for detail in details
        ))
        self.assertNotIn("secret diagnostic text", repr(details))
        self.assertTrue(self.store._read_document(first)[1])
        self.assertFalse(self.store._read_document(second)[1])
        self.assertEqual(self.store.migrate_legacy([second]), 1)
        self.assertTrue(self.store._read_document(second)[1])

    def test_same_size_same_mtime_content_change_is_not_overwritten(self):
        path = self._write_legacy(
            "chat_1.json", {"title": "AAAA", "history": []})
        before = os.stat(path)
        original_open = open
        read_count = 0

        def replace_before_final_read(*args, **kwargs):
            nonlocal read_count
            handle = original_open(*args, **kwargs)
            if args[0] == path and args[1] == "rb":
                read_count += 1
                if read_count == 3:
                    handle.close()
                    with original_open(path, "r+b") as writer:
                        data = writer.read().replace(b"AAAA", b"BBBB")
                        writer.seek(0)
                        writer.write(data)
                    os.utime(
                        path,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )
                    return original_open(*args, **kwargs)
            return handle

        with patch("builtins.open", side_effect=replace_before_final_read):
            with self.assertRaises(HistoryMigrationError):
                self.store.migrate_legacy([path])
        with original_open(path, "rb") as handle:
            stored = handle.read()
        self.assertIn(b"BBBB", stored)
        self.assertNotIn(b"llm-local-chat-dpapi", stored)

    def test_corrupt_history_is_not_pruned_or_modified(self):
        path = os.path.join(self.temp_dir.name, "chat_broken.json")
        with open(path, "wb") as handle:
            handle.write(b"{broken")
        before = os.stat(path)
        self.assertEqual(self.store.expired_paths(30), [])
        after = os.stat(path)
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), b"{broken")
        self.assertEqual(
            (after.st_mtime_ns, after.st_size),
            (before.st_mtime_ns, before.st_size),
        )

    def test_load_rejects_unmigrated_plaintext(self):
        path = self._write_legacy(
            "chat_1.json", {"title": "旧", "history": []})
        with self.assertRaises(HistoryCryptoError):
            self.store.load(path)

    def test_retention_uses_activity_and_excludes_current(self):
        now = datetime.datetime(2026, 7, 27, tzinfo=datetime.timezone.utc)
        old = self._write(
            "chat_old.json", "Old",
            activity="2026-04-01T00:00:00+00:00")
        current = self._write(
            "chat_current.json", "Current",
            activity="2026-04-01T00:00:00+00:00")
        recent = self._write(
            "chat_recent.json", "Recent",
            activity="2026-07-01T00:00:00+00:00")
        self.assertEqual(
            self.store.expired_paths(90, current, now), [old])
        self.assertTrue(os.path.exists(recent))

    def test_retention_cutoff_is_strict_and_zero_is_disabled(self):
        now = datetime.datetime(2026, 7, 27, tzinfo=datetime.timezone.utc)
        exact = self._write(
            "chat_exact.json", "Exact",
            activity="2026-04-28T00:00:00+00:00")
        self.assertEqual(self.store.expired_paths(0, now=now), [])
        self.assertEqual(self.store.expired_paths(90, now=now), [])
        self.assertTrue(os.path.exists(exact))

    def test_retention_normalization_rejects_bool_and_unknown(self):
        self.assertEqual(normalize_retention_days(True), 0)
        self.assertEqual(normalize_retention_days(45), 0)
        self.assertEqual(normalize_retention_days("90"), 90)


if __name__ == "__main__":
    unittest.main()
