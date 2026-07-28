import os
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import LLM_Local_Chat as app_module
from LLM_Local_Chat import (
    ChatApp,
    DuplicatePolicy,
    PortableExportDialog,
    PortableHistoryError,
    PortableImportDialog,
    export_destination_signature,
    count_import_duplicates,
    validate_export_destination,
)
from portable_history import MAX_ARCHIVE_BYTES, session_digest
from session_store import SessionStore


def sample_session(title="saved"):
    return {
        "title": title,
        "history": [{"role": "user", "content": "hello"}],
        "summary": "",
    }


class _ImmediateThread:
    def __init__(self, *, target, daemon):
        self.target = target
        self.daemon = daemon

    def start(self):
        self.target()


class PortableExportUiTests(unittest.TestCase):
    def test_export_dialog_references_only_defined_font_constants(self):
        referenced = {
            name for name in PortableExportDialog.__init__.__code__.co_names
            if name.startswith("FONT_")
        }
        self.assertTrue(referenced)
        for name in referenced:
            self.assertTrue(
                hasattr(app_module, name),
                f"undefined dialog font constant: {name}",
            )

    def make_app(self):
        app = ChatApp.__new__(ChatApp)
        app.root = object()
        app._closing = False
        app._portable_pending = threading.Event()
        app._ctrl = type("Ctrl", (), {"is_busy": lambda self: False})()
        app._post_ui = lambda callback: callback() or True
        app._is_guest = False
        app._current_path = "saved.json"
        app._session_store = Mock()
        app._session_store.load_managed_session.return_value = sample_session()
        app._session_store.list_sessions.return_value = [{"path": "saved.json"}]
        return app

    @patch("LLM_Local_Chat.messagebox.showerror")
    def test_current_export_rejects_guest_before_dialog(self, showerror):
        app = self.make_app()
        app._is_guest = True
        with patch("LLM_Local_Chat.PortableExportDialog") as dialog:
            app._export_current_portable_history()
        dialog.assert_not_called()
        showerror.assert_called_once()

    def test_current_export_uses_captured_managed_path_and_disk_session(self):
        app = self.make_app()
        app._current_path = "first.json"

        class ThreadChangingSelection(_ImmediateThread):
            def start(inner_self):
                app._current_path = "second.json"
                self.assertTrue(app._portable_pending.is_set())
                super().start()

        with (
            patch("LLM_Local_Chat.PortableExportDialog") as dialog,
            patch("LLM_Local_Chat.threading.Thread", ThreadChangingSelection),
            patch("LLM_Local_Chat.export_archive", return_value=b"archive"),
            patch("LLM_Local_Chat.atomic_write_bytes"),
            patch("LLM_Local_Chat.messagebox.showinfo"),
        ):
            dialog.return_value.show.return_value = (
                "out.shiro-export", "long passphrase"
            )
            app._export_current_portable_history()

        self.assertEqual(
            app._session_store.load_managed_session.call_args_list,
            [unittest.mock.call("first.json"), unittest.mock.call("first.json")],
        )
        self.assertFalse(app._portable_pending.is_set())

    def test_all_export_uses_store_snapshot_not_current_memory(self):
        app = self.make_app()
        stored = [sample_session("one"), sample_session("two")]
        app._current_session = sample_session("unsaved memory")
        app._session_store.exportable_sessions.return_value = stored
        with (
            patch("LLM_Local_Chat.PortableExportDialog") as dialog,
            patch("LLM_Local_Chat.threading.Thread", _ImmediateThread),
            patch("LLM_Local_Chat.export_archive", return_value=b"archive") as export,
            patch("LLM_Local_Chat.atomic_write_bytes"),
            patch("LLM_Local_Chat.messagebox.showinfo"),
        ):
            dialog.return_value.show.return_value = (
                "out.shiro-export", "long passphrase"
            )
            app._export_all_portable_history()
        export.assert_called_once_with(stored, "long passphrase")

    def test_thread_start_failure_clears_pending(self):
        app = self.make_app()

        class BrokenThread:
            def __init__(self, **_kwargs):
                pass

            def start(self):
                raise RuntimeError("start failed")

        with (
            patch("LLM_Local_Chat.PortableExportDialog") as dialog,
            patch("LLM_Local_Chat.threading.Thread", BrokenThread),
            patch("LLM_Local_Chat.messagebox.showerror"),
        ):
            dialog.return_value.show.return_value = (
                "out.shiro-export", "long passphrase"
            )
            app._export_current_portable_history()
        self.assertFalse(app._portable_pending.is_set())

    def test_export_destination_requires_shiro_extension(self):
        with self.assertRaises(PortableHistoryError):
            validate_export_destination("history.json")
        valid = validate_export_destination("history.SHIRO-EXPORT")
        self.assertTrue(valid.lower().endswith(".shiro-export"))

    def test_destination_signature_rejects_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(PortableHistoryError):
                export_destination_signature(temp_dir)

    def test_destination_change_stops_before_atomic_write(self):
        app = self.make_app()
        changed = (1, 2, 3, 4, 5, 6)
        with (
            patch("LLM_Local_Chat.PortableExportDialog") as dialog,
            patch("LLM_Local_Chat.threading.Thread", _ImmediateThread),
            patch("LLM_Local_Chat.os.path.exists", return_value=False),
            patch(
                "LLM_Local_Chat.export_destination_signature",
                side_effect=[None, changed],
            ),
            patch("LLM_Local_Chat.export_archive", return_value=b"archive"),
            patch("LLM_Local_Chat.atomic_write_bytes") as write,
            patch("LLM_Local_Chat.messagebox.showerror") as showerror,
        ):
            dialog.return_value.show.return_value = (
                "out.shiro-export", "long passphrase"
            )
            app._export_current_portable_history()
        write.assert_not_called()
        showerror.assert_called_once()
        self.assertFalse(app._portable_pending.is_set())

    def test_unsaved_current_export_is_rejected_before_dialog(self):
        app = self.make_app()
        app._current_path = None
        with (
            patch("LLM_Local_Chat.PortableExportDialog") as dialog,
            patch("LLM_Local_Chat.messagebox.showerror") as showerror,
        ):
            app._export_current_portable_history()
        dialog.assert_not_called()
        showerror.assert_called_once()

    def test_export_dialog_submit_rejects_invalid_inputs(self):
        dialog = object.__new__(PortableExportDialog)
        dialog.path_entry = Mock()
        dialog.passphrase_entry = Mock()
        dialog.confirm_entry = Mock()
        dialog.destroy = Mock()
        cases = [
            ("", "long enough pass", "long enough pass"),
            ("out.shiro-export", "short", "short"),
            ("out.shiro-export", "long enough pass", "different value"),
            ("out.json", "long enough pass", "long enough pass"),
        ]
        with patch("LLM_Local_Chat.messagebox.showerror") as showerror:
            for path, passphrase, confirmation in cases:
                dialog.path_entry.get.return_value = path
                dialog.passphrase_entry.get.return_value = passphrase
                dialog.confirm_entry.get.return_value = confirmation
                PortableExportDialog._submit(dialog)
        self.assertEqual(showerror.call_count, len(cases))
        dialog.destroy.assert_not_called()


class PortableImportUiTests(unittest.TestCase):
    def make_app(self):
        app = ChatApp.__new__(ChatApp)
        app.root = object()
        app._closing = False
        app._portable_pending = threading.Event()
        app._ctrl = type("Ctrl", (), {"is_busy": lambda self: False})()
        app._session_store = Mock()
        app._cfg = {"history_retention_days": 0}
        return app

    def test_archive_internal_duplicates_are_counted(self):
        first = sample_session("same")
        second = dict(first)
        existing = {session_digest(sample_session("existing"))}
        self.assertEqual(
            count_import_duplicates([first, second], existing),
            1,
        )

    def test_existing_and_archive_internal_duplicates_are_counted(self):
        first = sample_session("same")
        digest = session_digest(first)
        self.assertEqual(
            count_import_duplicates([first, dict(first)], {digest}),
            2,
        )

    def test_import_accepts_legacy_short_passphrase(self):
        app = ChatApp.__new__(ChatApp)
        app.root = object()
        app._closing = False
        app._portable_pending = threading.Event()
        app._ctrl = type("Ctrl", (), {"is_busy": lambda self: False})()
        app._session_store = Mock()
        app._session_store.existing_session_digests.return_value = set()
        app._cfg = {"history_retention_days": 0}
        app._post_portable_import_confirmation = Mock(return_value=True)
        with tempfile.TemporaryDirectory() as temp_dir:
            archive_path = os.path.join(temp_dir, "archive.shiro-export")
            with open(archive_path, "wb") as archive:
                archive.write(b"encrypted")
            with (
                patch("LLM_Local_Chat.PortableImportDialog") as dialog,
                patch("LLM_Local_Chat.threading.Thread", _ImmediateThread),
                patch(
                    "LLM_Local_Chat.import_archive",
                    return_value=[sample_session()],
                ) as importer,
            ):
                dialog.return_value.show.return_value = (archive_path, "short")
                app._import_portable_history()
        importer.assert_called_once_with(b"encrypted", "short")
        self.assertTrue(app._portable_pending.is_set())

    def test_explicit_copy_policy_maps_to_import_flag(self):
        app = ChatApp.__new__(ChatApp)
        app.root = object()
        app._portable_pending = threading.Event()
        app._portable_pending.set()
        app._session_store = Mock()
        app._session_store.import_sessions.return_value = (["new.json"], 0)
        app._post_ui = lambda callback: callback() or True
        app._refresh_chat_list = Mock()
        with (
            patch("LLM_Local_Chat.DuplicateImportDialog") as dialog,
            patch("LLM_Local_Chat.threading.Thread", _ImmediateThread),
            patch("LLM_Local_Chat.messagebox.showinfo"),
        ):
            dialog.return_value.show.return_value = DuplicatePolicy.COPY
            app._confirm_portable_import([sample_session()], 0, 0)
        app._session_store.import_sessions.assert_called_once_with(
            [sample_session()], skip_duplicates=False
        )
        self.assertFalse(app._portable_pending.is_set())

    def test_cancel_policy_clears_pending_without_import(self):
        app = ChatApp.__new__(ChatApp)
        app.root = object()
        app._portable_pending = threading.Event()
        app._portable_pending.set()
        app._session_store = Mock()
        with patch("LLM_Local_Chat.DuplicateImportDialog") as dialog:
            dialog.return_value.show.return_value = DuplicatePolicy.CANCEL
            app._confirm_portable_import([sample_session()], 0, 0)
        app._session_store.import_sessions.assert_not_called()
        self.assertFalse(app._portable_pending.is_set())

    def test_missing_import_path_shows_error_without_pending(self):
        app = self.make_app()
        with (
            patch("LLM_Local_Chat.PortableImportDialog") as dialog,
            patch("LLM_Local_Chat.messagebox.showerror") as showerror,
        ):
            dialog.return_value.show.return_value = (
                "missing.shiro-export", "passphrase"
            )
            app._import_portable_history()
        showerror.assert_called_once()
        self.assertFalse(app._portable_pending.is_set())

    def test_oversize_import_shows_error_without_pending(self):
        app = self.make_app()
        with (
            patch("LLM_Local_Chat.PortableImportDialog") as dialog,
            patch("LLM_Local_Chat.os.path.isfile", return_value=True),
            patch(
                "LLM_Local_Chat.os.path.getsize",
                return_value=MAX_ARCHIVE_BYTES + 1,
            ),
            patch("LLM_Local_Chat.messagebox.showerror") as showerror,
        ):
            dialog.return_value.show.return_value = (
                "large.shiro-export", "passphrase"
            )
            app._import_portable_history()
        showerror.assert_called_once()
        self.assertFalse(app._portable_pending.is_set())

    def test_confirmation_dialog_error_clears_pending_before_notification(self):
        app = self.make_app()
        app._portable_pending.set()

        def failed_notification(*_args, **_kwargs):
            self.assertFalse(app._portable_pending.is_set())
            raise RuntimeError("notification failed")

        with (
            patch(
                "LLM_Local_Chat.DuplicateImportDialog",
                side_effect=RuntimeError("dialog failed"),
            ),
            patch(
                "LLM_Local_Chat.messagebox.showerror",
                side_effect=failed_notification,
            ),
        ):
            app._confirm_portable_import([], 0, 0)
        self.assertFalse(app._portable_pending.is_set())

    def test_invalid_policy_is_cancelled(self):
        app = self.make_app()
        app._portable_pending.set()
        with patch("LLM_Local_Chat.DuplicateImportDialog") as dialog:
            dialog.return_value.show.return_value = None
            app._confirm_portable_import([sample_session()], 0, 0)
        app._session_store.import_sessions.assert_not_called()
        self.assertFalse(app._portable_pending.is_set())

    def test_policy_with_failing_equality_is_cancelled(self):
        class FailingEquality:
            def __eq__(self, _other):
                raise RuntimeError("equality must not be evaluated")

        app = self.make_app()
        app._portable_pending.set()
        with patch("LLM_Local_Chat.DuplicateImportDialog") as dialog:
            dialog.return_value.show.return_value = FailingEquality()
            app._confirm_portable_import([sample_session()], 0, 0)
        app._session_store.import_sessions.assert_not_called()
        self.assertFalse(app._portable_pending.is_set())

    def test_import_dialog_submit_rejects_empty_values(self):
        dialog = object.__new__(PortableImportDialog)
        dialog.path_entry = Mock()
        dialog.passphrase_entry = Mock()
        dialog.destroy = Mock()
        with patch("LLM_Local_Chat.messagebox.showerror") as showerror:
            dialog.path_entry.get.return_value = ""
            dialog.passphrase_entry.get.return_value = "pass"
            PortableImportDialog._submit(dialog)
            dialog.path_entry.get.return_value = "archive.shiro-export"
            dialog.passphrase_entry.get.return_value = ""
            PortableImportDialog._submit(dialog)
        self.assertEqual(showerror.call_count, 2)
        dialog.destroy.assert_not_called()


class ManagedSessionPathTests(unittest.TestCase):
    def test_load_managed_session_rejects_outside_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(
                os.path.join(root, "chat_logs"),
                protector=Mock(),
            )
            outside = os.path.join(root, "outside.json")
            with open(outside, "wb") as handle:
                handle.write(b"not read")
            with self.assertRaises(Exception):
                store.load_managed_session(outside)

    def test_load_managed_session_rejects_deleted_path(self):
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(root, protector=Mock())
            with self.assertRaises(Exception):
                store.load_managed_session(os.path.join(root, "missing.json"))


if __name__ == "__main__":
    unittest.main()
