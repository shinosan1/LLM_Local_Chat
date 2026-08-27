import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest.mock import patch

from history_crypto import HistoryCryptoError
from prompt_inputs import load_attachment, load_attachment_bytes
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
        self.assertEqual(self.store.metadata_reads, 0)
        self.assertEqual(
            [item["title"] for item in self.store.list_sessions("sec")],
            ["Beta"],
        )
        self.assertEqual(self.store.metadata_reads, 0)

    def test_unchanged_index_does_not_rehash_history_files(self):
        self._write("chat_1.json", "Alpha")
        self._write("chat_2.json", "Beta")
        with patch.object(
            self.store,
            "_file_signature",
            wraps=self.store._file_signature,
        ) as file_signature:
            self.store.list_sessions()
            self.assertEqual(file_signature.call_count, 2)
            file_signature.reset_mock()
            self.store.list_sessions()
            self.assertEqual(file_signature.call_count, 0)

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
        self.assertEqual(self.store.metadata_reads, 0)

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


class PersistentAttachmentSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="test_persistent_attachments_")
        self.store = SessionStore(
            self.temp_dir.name, protector=FakeProtector())

    def tearDown(self):
        self.temp_dir.cleanup()

    def _saved_chat(self, text="hello"):
        session = self.store.new_session()
        session["history"] = [{"user": text, "assistant": "answer"}]
        return session, self.store.save(session, None)

    def test_attachment_copy_survives_source_change_delete_and_restart(self):
        source = os.path.join(self.temp_dir.name, "source.csv")
        original = b"date,meal\n2026-08-27,rice\n"
        with open(source, "wb") as handle:
            handle.write(original)
        attachment = load_attachment(source)
        session = self.store.new_session()
        path, added, warnings = self.store.add_attachments(
            session, None, [attachment])
        self.assertEqual(len(added), 1)
        self.assertEqual(warnings, [])

        with open(source, "wb") as handle:
            handle.write(b"changed")
        os.remove(source)
        restarted = SessionStore(
            self.temp_dir.name, protector=FakeProtector())
        restored = restarted.load(path)
        loaded, restore_warnings = restarted.load_attachments(restored)
        self.assertEqual(restore_warnings, [])
        self.assertEqual([data for _metadata, data in loaded], [original])

        _decoded, _encrypted, plaintext = restarted._read_document(path)
        text = plaintext.decode("utf-8")
        self.assertNotIn(original.decode("utf-8"), text)
        self.assertNotIn(source, text)
        self.assertNotIn("data:", text)
        self.assertEqual(set(restored["attachments"][0]), {
            "id", "name", "kind", "mime_type", "extension", "size", "sha256",
        })

    def test_same_name_different_content_and_individual_delete_use_ids(self):
        session = self.store.new_session()
        path, added, _warnings = self.store.add_attachments(
            session,
            None,
            [
                load_attachment_bytes("data.csv", b"a,1\n"),
                load_attachment_bytes("data.csv", b"b,2\n"),
            ],
        )
        self.assertEqual(len(added), 2)
        rows = self.store.list_saved_attachments()
        self.assertEqual(
            [row["display_name"] for row in rows],
            ["data.csv", "data.csv (2)"],
        )
        result = self.store.delete_saved_attachment(rows[0])
        self.assertEqual(result["succeeded"], 1)
        remaining = self.store.load(path)
        self.assertEqual(
            [item["id"] for item in remaining["attachments"]],
            [added[1]["id"]],
        )
        loaded, warnings = self.store.load_attachments(remaining)
        self.assertEqual(warnings, [])
        self.assertEqual(loaded[0][1], b"b,2\n")

    def test_bulk_delete_preserves_conversation_and_reports_unknown_entry(self):
        first, first_path = self._saved_chat("first")
        first_path, _added, _warnings = self.store.add_attachments(
            first, first_path, [load_attachment_bytes("one.txt", b"one")])
        second, second_path = self._saved_chat("second")
        second_path, _added, _warnings = self.store.add_attachments(
            second, second_path, [load_attachment_bytes("two.txt", b"two")])
        unknown = os.path.join(
            self.store._attachment_store.root,
            first["session_id"],
            "unexpected.bin",
        )
        with open(unknown, "wb") as handle:
            handle.write(b"unknown")

        result = self.store.delete_all_saved_attachments()
        self.assertEqual(result["succeeded"], 2)
        self.assertGreaterEqual(result["skipped"], 1)
        self.assertEqual(
            self.store.load(first_path)["history"][0]["user"], "first")
        self.assertEqual(
            self.store.load(second_path)["history"][0]["user"], "second")
        self.assertEqual(self.store.load(first_path)["attachments"], [])
        self.assertEqual(self.store.load(second_path)["attachments"], [])
        self.assertTrue(os.path.exists(unknown))

    def test_missing_and_modified_sidecars_are_not_loaded_or_metadata_deleted(self):
        for mode in ("missing", "size", "hash"):
            with self.subTest(mode=mode):
                session = self.store.new_session()
                raw = b"abcde"
                path, added, _warnings = self.store.add_attachments(
                    session, None,
                    [load_attachment_bytes(f"{mode}.txt", raw)],
                )
                metadata = added[0]
                sidecar = os.path.join(
                    self.store._attachment_store.root,
                    session["session_id"],
                    metadata["id"] + metadata["extension"],
                )
                if mode == "missing":
                    os.remove(sidecar)
                elif mode == "size":
                    with open(sidecar, "wb") as handle:
                        handle.write(b"x")
                else:
                    with open(sidecar, "wb") as handle:
                        handle.write(b"vwxyz")
                loaded, warnings = self.store.load_attachments(
                    self.store.load(path))
                self.assertEqual(loaded, [])
                self.assertTrue(warnings)
                row = next(
                    item for item in self.store.list_saved_attachments()
                    if item["attachment_id"] == metadata["id"])
                result = self.store.delete_saved_attachment(row)
                self.assertEqual(result["failed"], 1)
                self.assertEqual(
                    len(self.store.load(path)["attachments"]), 1)

    def test_chat_delete_removes_only_its_attachment_namespace(self):
        first, first_path = self._saved_chat("first")
        first_path, _added, _warnings = self.store.add_attachments(
            first, first_path, [load_attachment_bytes("same.txt", b"one")])
        second, second_path = self._saved_chat("second")
        second_path, _added, _warnings = self.store.add_attachments(
            second, second_path, [load_attachment_bytes("same.txt", b"two")])
        result = self.store.delete(first_path)
        self.assertEqual(result["failed"] + result["skipped"], 0)
        self.assertFalse(os.path.exists(first_path))
        restored = self.store.load(second_path)
        loaded, warnings = self.store.load_attachments(restored)
        self.assertEqual(warnings, [])
        self.assertEqual(loaded[0][1], b"two")

    def test_delete_io_failure_keeps_tracked_pending_state_until_recovery(self):
        session = self.store.new_session()
        path, _added, _warnings = self.store.add_attachments(
            session, None, [load_attachment_bytes("pending.txt", b"pending")])
        row = self.store.list_saved_attachments()[0]
        with patch.object(
            self.store._attachment_store,
            "commit_delete",
            return_value=False,
        ):
            result = self.store.delete_saved_attachment(row)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["cleanup_pending"], 1)
        self.assertEqual(self.store.load(path)["attachments"], [])
        session_dir = os.path.join(
            self.store._attachment_store.root, session["session_id"])
        self.assertFalse(os.path.exists(session_dir))

    def test_orphan_pending_add_without_chat_is_removed_on_restart(self):
        session_id = self.store.new_session()["session_id"]
        attachment = load_attachment_bytes("private.txt", b"private")
        metadata, data = self.store._attachment_metadata(attachment)
        self.store._attachment_store.write_pending_add(
            session_id, metadata, data)
        pending = os.path.join(
            self.store._attachment_store.root,
            session_id,
            f".pending-add-{metadata['id']}{metadata['extension']}",
        )
        self.assertTrue(os.path.exists(pending))

        restarted = SessionStore(
            self.temp_dir.name, protector=FakeProtector())
        self.assertEqual(restarted.list_sessions(), [])
        self.assertFalse(os.path.exists(pending))

    def test_chat_history_pending_delete_finishes_after_restart(self):
        session = self.store.new_session()
        path, _added, _warnings = self.store.add_attachments(
            session, None,
            [load_attachment_bytes("delete.txt", b"delete")],
        )
        with patch.object(
            self.store,
            "_commit_pending_history_delete",
            return_value=False,
        ):
            result = self.store.delete(path)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["cleanup_pending"], 1)
        self.assertFalse(os.path.exists(path))
        self.assertTrue(any(
            name.startswith(".pending-session-delete-")
            and name.endswith(".bin")
            for name in os.listdir(self.temp_dir.name)
        ))

        restarted = SessionStore(
            self.temp_dir.name, protector=FakeProtector())
        self.assertEqual(restarted.list_sessions(), [])
        self.assertFalse(any(
            name.startswith(".pending-session-delete-")
            for name in os.listdir(self.temp_dir.name)
        ))
        self.assertFalse(os.path.exists(os.path.join(
            restarted._attachment_store.root, session["session_id"])))

    def test_empty_attachment_id_selection_does_not_delete_all(self):
        session = self.store.new_session()
        path, _added, _warnings = self.store.add_attachments(
            session, None, [load_attachment_bytes("keep.txt", b"keep")])
        data, result = self.store.delete_saved_attachments(path, [])
        self.assertEqual(result, {
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "cleanup_pending": 0,
        })
        self.assertEqual(len(data["attachments"]), 1)
        self.assertEqual(len(self.store.list_saved_attachments()), 1)

    def _saved_attachment_chats(self, count=4, attachments_per_chat=1):
        rows = []
        for chat_index in range(count):
            session, path = self._saved_chat(f"chat-{chat_index}")
            items = [
                load_attachment_bytes(
                    f"data-{chat_index}-{item_index}.txt",
                    f"{chat_index}:{item_index}".encode("utf-8"),
                )
                for item_index in range(attachments_per_chat)
            ]
            path, added, _warnings = self.store.add_attachments(
                session, path, items)
            rows.append((session, path, added))
        return rows

    def test_attachment_list_runs_namespace_recovery_once(self):
        self._saved_attachment_chats(count=6)
        with patch.object(
            self.store,
            "_recover_attachment_namespaces",
            wraps=self.store._recover_attachment_namespaces,
        ) as recovery:
            rows = self.store.list_saved_attachments()
        self.assertEqual(len(rows), 6)
        self.assertEqual(recovery.call_count, 1)

    def test_individual_delete_runs_namespace_recovery_once(self):
        self._saved_attachment_chats(count=5)
        row = self.store.list_saved_attachments()[0]
        with patch.object(
            self.store,
            "_recover_attachment_namespaces",
            wraps=self.store._recover_attachment_namespaces,
        ) as recovery:
            result = self.store.delete_saved_attachment(row)
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(recovery.call_count, 1)

    def test_bulk_delete_runs_namespace_recovery_once(self):
        self._saved_attachment_chats(count=5, attachments_per_chat=2)
        with patch.object(
            self.store,
            "_recover_attachment_namespaces",
            wraps=self.store._recover_attachment_namespaces,
        ) as recovery:
            result = self.store.delete_all_saved_attachments()
        self.assertEqual(result["succeeded"], 10)
        self.assertEqual(recovery.call_count, 1)

    def test_chat_delete_runs_namespace_recovery_once(self):
        chats = self._saved_attachment_chats(count=4)
        with patch.object(
            self.store,
            "_recover_attachment_namespaces",
            wraps=self.store._recover_attachment_namespaces,
        ) as recovery:
            self.store.delete(chats[0][1])
        self.assertEqual(recovery.call_count, 1)

    def test_portable_list_runs_namespace_recovery_once(self):
        self._saved_attachment_chats(count=4)
        with patch.object(
            self.store,
            "_recover_attachment_namespaces",
            wraps=self.store._recover_attachment_namespaces,
        ) as recovery:
            exported = self.store.exportable_sessions()
        self.assertEqual(len(exported), 4)
        self.assertEqual(recovery.call_count, 1)

    def test_manager_snapshot_verifies_each_attachment_once(self):
        chats = self._saved_attachment_chats(
            count=3, attachments_per_chat=2)
        current_path = chats[0][1]
        with patch.object(
            self.store._attachment_store,
            "read",
            wraps=self.store._attachment_store.read,
        ) as read:
            snapshot = self.store.saved_attachment_manager_snapshot(
                current_path)
        self.assertEqual(len(snapshot["items"]), 6)
        self.assertEqual(len(snapshot["current_attachments"]), 2)
        self.assertEqual(read.call_count, 6)

    def test_same_session_stage_failure_rolls_back_all_metadata(self):
        session, path = self._saved_chat("atomic")
        path, added, _warnings = self.store.add_attachments(
            session,
            path,
            [
                load_attachment_bytes("one.txt", b"one"),
                load_attachment_bytes("two.txt", b"two"),
            ],
        )
        real_stage = self.store._attachment_store.stage_delete
        calls = 0

        def fail_second(session_id, metadata):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated stage failure")
            return real_stage(session_id, metadata)

        with patch.object(
            self.store._attachment_store,
            "stage_delete",
            side_effect=fail_second,
        ):
            data, result = self.store.delete_saved_attachments(path)
        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(data["attachments"], added)
        persisted = self.store.load(path)
        self.assertEqual(persisted["attachments"], added)
        loaded, warnings = self.store.load_attachments(persisted)
        self.assertEqual(warnings, [])
        self.assertEqual(len(loaded), 2)

    def test_bulk_partial_failure_keeps_failed_session_atomic(self):
        first, first_path = self._saved_chat("first")
        first_path, first_added, _warnings = self.store.add_attachments(
            first,
            first_path,
            [
                load_attachment_bytes("one.txt", b"one"),
                load_attachment_bytes("two.txt", b"two"),
            ],
        )
        second, second_path = self._saved_chat("second")
        second_path, _added, _warnings = self.store.add_attachments(
            second, second_path,
            [load_attachment_bytes("three.txt", b"three")],
        )
        failed_id = first_added[1]["id"]
        real_stage = self.store._attachment_store.stage_delete

        def fail_selected(session_id, metadata):
            if metadata["id"] == failed_id:
                raise OSError("simulated stage failure")
            return real_stage(session_id, metadata)

        with patch.object(
            self.store._attachment_store,
            "stage_delete",
            side_effect=fail_selected,
        ):
            result = self.store.delete_all_saved_attachments()
        self.assertEqual(result["succeeded"], 1)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(
            self.store.load(first_path)["attachments"], first_added)
        self.assertEqual(self.store.load(second_path)["attachments"], [])

    def test_save_failure_rolls_back_staged_session_batch(self):
        session, path = self._saved_chat("save-failure")
        path, added, _warnings = self.store.add_attachments(
            session,
            path,
            [
                load_attachment_bytes("one.txt", b"one"),
                load_attachment_bytes("two.txt", b"two"),
            ],
        )
        with patch.object(
            self.store,
            "_save_attachment_metadata_locked",
            side_effect=OSError("simulated save failure"),
        ):
            data, result = self.store.delete_saved_attachments(path)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["cleanup_pending"], 0)
        self.assertEqual(data["attachments"], added)
        persisted, _encrypted, _raw = self.store._read_document(path)
        self.assertEqual(persisted["attachments"], added)

    def test_rollback_failure_is_reported_as_cleanup_pending(self):
        session, path = self._saved_chat("rollback-failure")
        path, _added, _warnings = self.store.add_attachments(
            session,
            path,
            [
                load_attachment_bytes("one.txt", b"one"),
                load_attachment_bytes("two.txt", b"two"),
            ],
        )
        real_stage = self.store._attachment_store.stage_delete
        calls = 0

        def fail_second(session_id, metadata):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated stage failure")
            return real_stage(session_id, metadata)

        with (
            patch.object(
                self.store._attachment_store,
                "stage_delete",
                side_effect=fail_second,
            ),
            patch.object(
                self.store._attachment_store,
                "rollback_delete",
                return_value=False,
            ),
        ):
            _data, result = self.store.delete_saved_attachments(path)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(result["cleanup_pending"], 1)

    def test_duplicate_session_id_blocks_individual_attachment_delete(self):
        session, path = self._saved_chat("duplicate")
        path, added, _warnings = self.store.add_attachments(
            session,
            path,
            [load_attachment_bytes("shared.txt", b"shared")],
        )
        duplicate_path = os.path.join(
            self.temp_dir.name, "chat_duplicate.json")
        shutil.copyfile(path, duplicate_path)
        rows = self.store.list_saved_attachments()
        self.assertEqual(len(rows), 2)

        result = self.store.delete_saved_attachment(rows[0])

        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(self.store.load(path)["attachments"], added)
        self.assertEqual(
            self.store.load(duplicate_path)["attachments"], added)
        self.assertEqual(
            self.store._attachment_store.read(
                session["session_id"], added[0]),
            b"shared",
        )

    def test_duplicate_session_id_blocks_bulk_attachment_delete(self):
        session, path = self._saved_chat("duplicate-bulk")
        path, added, _warnings = self.store.add_attachments(
            session,
            path,
            [load_attachment_bytes("shared.txt", b"shared")],
        )
        duplicate_path = os.path.join(
            self.temp_dir.name, "chat_duplicate_bulk.json")
        shutil.copyfile(path, duplicate_path)

        result = self.store.delete_all_saved_attachments()

        self.assertEqual(result["succeeded"], 0)
        self.assertEqual(result["failed"], 2)
        self.assertEqual(self.store.load(path)["attachments"], added)
        self.assertEqual(
            self.store.load(duplicate_path)["attachments"], added)
        self.assertEqual(
            self.store._attachment_store.read(
                session["session_id"], added[0]),
            b"shared",
        )


if __name__ == "__main__":
    unittest.main()
