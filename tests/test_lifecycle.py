import ctypes
import os
import sys
import tempfile
import threading
import time
import unittest
from ctypes import wintypes
from unittest.mock import Mock, patch

import tkinter as tk

from integrations import IntegrationBridge
from history_crypto import HistoryCryptoError
from LLM_Local_Chat import (
    APP_VERSION,
    FONT_CHAT,
    ChatApp,
    SavedAttachmentsDialog,
    SettingsDialog,
    _LOGFONTW,
    _set_windows_ime_composition_font,
    main,
)
from llm_service import LLMService
from prompt_inputs import load_attachment_bytes
from session_store import SessionStore


class _FakeProtector:
    def protect(self, data):
        return b"protected:" + data[::-1]

    def unprotect(self, data):
        if not data.startswith(b"protected:"):
            raise HistoryCryptoError("invalid ciphertext")
        return data[len(b"protected:"):][::-1]


class _Model:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


class _Root:
    def __init__(self):
        self.callbacks = []
        self.destroyed = False
        self.withdrawn = False
        self.deiconified = False

    def after(self, _delay, callback):
        self.callbacks.append(callback)

    def withdraw(self):
        self.withdrawn = True

    def deiconify(self):
        self.deiconified = True

    def destroy(self):
        self.destroyed = True


class HelpDialogTests(unittest.TestCase):
    def setUp(self):
        self.app = ChatApp.__new__(ChatApp)
        self.app.root = object()

    @patch("LLM_Local_Chat.messagebox.showinfo")
    def test_about_shows_current_version_and_license(self, showinfo):
        self.app._show_about()
        message = showinfo.call_args.args[1]
        self.assertIn(APP_VERSION, message)
        self.assertIn("MIT License", message)

    @patch("LLM_Local_Chat.messagebox.showinfo")
    def test_history_help_explains_dpapi_recovery(self, showinfo):
        self.app._show_history_help()
        message = showinfo.call_args.args[1]
        self.assertIn("DPAPI", message)
        self.assertIn("削除せず", message)


@unittest.skipUnless(sys.platform == "win32", "Windows IME専用")
class WindowsImeFontTests(unittest.TestCase):
    def test_chat_font_is_applied_to_ime_composition(self):
        root = tk.Tk()
        root.withdraw()
        entry = tk.Text(root, font=FONT_CHAT)
        entry.pack()
        root.update_idletasks()
        try:
            self.assertTrue(_set_windows_ime_composition_font(entry))

            imm32 = ctypes.WinDLL("imm32", use_last_error=True)
            imm32.ImmGetContext.argtypes = [wintypes.HWND]
            imm32.ImmGetContext.restype = wintypes.HANDLE
            imm32.ImmGetCompositionFontW.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(_LOGFONTW),
            ]
            imm32.ImmGetCompositionFontW.restype = wintypes.BOOL
            imm32.ImmReleaseContext.argtypes = [
                wintypes.HWND,
                wintypes.HANDLE,
            ]
            imm32.ImmReleaseContext.restype = wintypes.BOOL

            hwnd = entry.winfo_id()
            input_context = imm32.ImmGetContext(hwnd)
            self.assertTrue(input_context)
            try:
                actual = _LOGFONTW()
                self.assertTrue(imm32.ImmGetCompositionFontW(
                    input_context, ctypes.byref(actual)))
            finally:
                imm32.ImmReleaseContext(hwnd, input_context)

            font_name = entry.cget("font")
            expected_family = str(entry.tk.call(
                "font", "actual", font_name, "-family"))
            point_size = int(entry.tk.call(
                "font", "actual", font_name, "-size"))
            expected_height = -max(
                1,
                round(point_size * float(entry.winfo_fpixels("1i")) / 72),
            )
            self.assertEqual(actual.lfFaceName, expected_family[:31])
            self.assertEqual(actual.lfHeight, expected_height)
        finally:
            root.destroy()


class ModelReferenceTests(unittest.TestCase):
    def test_service_detach_and_attach_require_idle_state(self):
        model = object()
        service = LLMService(model)
        self.assertIs(service.detach_llm(), model)
        self.assertIsNone(service.llm)
        replacement = object()
        service.attach_llm(replacement)
        self.assertIs(service.llm, replacement)

    def test_detach_only_removes_references_and_worker_owns_close(self):
        model = _Model()
        app = ChatApp.__new__(ChatApp)
        app.llm = model
        app._ctrl = type("Ctrl", (), {
            "_llm_service": LLMService(model),
        })()
        detached = app._detach_current_llm()
        self.assertIsNone(app.llm)
        self.assertIsNone(app._ctrl._llm_service.llm)
        self.assertEqual(detached, [model])
        self.assertEqual(model.closed, 0)
        with patch("LLM_Local_Chat.gc.collect"):
            app._close_llm_instance(detached[0], strict=True)
        self.assertEqual(model.closed, 1)

    def test_stale_load_result_is_closed_instead_of_attached(self):
        model = _Model()
        app = ChatApp.__new__(ChatApp)
        app._llm_load_generation = 2
        app._closing = False
        with patch("LLM_Local_Chat.gc.collect"):
            app._on_llm_ready(1, model, None)
            deadline = time.monotonic() + 1
            while model.closed == 0 and time.monotonic() < deadline:
                time.sleep(0.01)
        self.assertEqual(model.closed, 1)

    def test_failed_new_model_restores_previous_configuration(self):
        app = ChatApp.__new__(ChatApp)
        app._llm_load_generation = 3
        app._closing = False
        app._llm_loading = True
        app._model_path = "new.gguf"
        app._n_ctx = 4096
        app._cfg = {"model_path": "new.gguf", "n_ctx": 4096}
        reloads = []
        app._reload_llm = lambda **kwargs: reloads.append(kwargs)
        with patch("LLM_Local_Chat.save_settings"):
            app._on_llm_ready(
                3, None, RuntimeError("load failed"), ("old.gguf", 8192)
            )
        self.assertEqual((app._model_path, app._n_ctx), ("old.gguf", 8192))
        self.assertEqual(app._cfg, {"model_path": "old.gguf", "n_ctx": 8192})
        self.assertEqual(reloads, [{"recovery": True}])


class IntegrationLifecycleTests(unittest.TestCase):
    def test_tracks_worker_until_completion(self):
        root = _Root()
        bridge = IntegrationBridge(root, lambda *_args: None)
        entered = threading.Event()
        release = threading.Event()

        def worker():
            entered.set()
            release.wait(1)

        self.assertTrue(bridge._start_worker("api", worker))
        self.assertTrue(entered.wait(1))
        self.assertEqual(bridge.pending_operations(), ["api"])
        release.set()
        for thread in list(bridge._workers):
            thread.join(1)
        self.assertEqual(bridge.pending_operations(), [])

    def test_closing_rejects_new_workers_and_ui_callbacks(self):
        root = _Root()
        writes = []
        bridge = IntegrationBridge(root, lambda text, tag: writes.append((text, tag)))
        bridge.begin_closing()

        self.assertFalse(bridge._start_worker("api", lambda: None))
        bridge._post_ui(lambda: writes.append(("late", "err")))
        self.assertEqual(root.callbacks, [])
        self.assertEqual(writes, [])


class SessionAttachmentLifetimeTests(unittest.TestCase):
    def _session_app(self):
        app = ChatApp.__new__(ChatApp)
        app._attachments = [object()]
        app._ctrl = Mock()
        app._ctrl.is_busy.return_value = False
        app._session_store = Mock()
        app._session_store.new_session.return_value = {
            "title": "新しいチャット",
            "history": [],
        }
        app._title_var = Mock()
        app._summary_var = Mock()
        app._chat_text = Mock()
        app._entry = Mock()
        app._update_status = Mock()
        return app

    def test_new_session_discards_session_attachments(self):
        app = self._session_app()
        app._new_session()
        self.assertEqual(app._attachments, [])

    def test_busy_new_session_does_not_discard_attachments(self):
        app = self._session_app()
        app._ctrl.is_busy.return_value = True
        with patch("LLM_Local_Chat.messagebox.showwarning"):
            app._new_session()
        self.assertEqual(len(app._attachments), 1)

    def test_loading_another_session_discards_attachments(self):
        app = self._session_app()
        app._chat_list = Mock()
        app._chat_list.curselection.return_value = (0,)
        app._files = ["session-path"]
        app._session_store.load.return_value = {
            "title": "保存済みチャット",
            "summary": "",
            "history": [],
        }
        app._load_selected()
        self.assertEqual(app._attachments, [])

    def test_current_chat_attachment_clear_requires_confirmation(self):
        attachment = Mock()
        attachment.attachment_id = "a" * 32
        app = ChatApp.__new__(ChatApp)
        app.root = object()
        app._is_guest = False
        app._current_path = "chat.json"
        app._attachments = [attachment]
        app._session_store = Mock()
        app._update_attachment_display = Mock()

        with patch("LLM_Local_Chat.messagebox.askyesno", return_value=False):
            app._clear_attachments()

        app._session_store.delete_saved_attachments.assert_not_called()
        self.assertEqual(app._attachments, [attachment])


class SavedAttachmentDialogTests(unittest.TestCase):
    def test_bulk_delete_cancel_changes_nothing(self):
        dialog = Mock()
        dialog._rows = {"row": {"attachment_id": "id"}}
        dialog._delete_all = Mock()
        dialog._modification_block_reason.return_value = None
        with patch("LLM_Local_Chat.messagebox.askyesno", return_value=False):
            SavedAttachmentsDialog._delete_everything(dialog)
        dialog._delete_all.assert_not_called()

    def test_individual_delete_cancel_changes_nothing(self):
        dialog = Mock()
        dialog._selected_item.return_value = {
            "display_name": "data.csv",
            "attachment_id": "id",
        }
        dialog._delete_one = Mock()
        dialog._modification_block_reason.return_value = None
        with patch("LLM_Local_Chat.messagebox.askyesno", return_value=False):
            SavedAttachmentsDialog._delete_selected(dialog)
        dialog._delete_one.assert_not_called()

    def test_single_worker_rejects_overlapping_operation(self):
        dialog = Mock()
        dialog._operation_busy = threading.Event()
        dialog._operation_serial = 0
        dialog._current_context.return_value = {
            "path": "chat.json", "session_id": "s", "guest": False}
        dialog._set_busy = Mock()
        dialog._modification_block_reason.return_value = None
        callbacks = []
        dialog._post_ui.side_effect = lambda callback: (
            callbacks.append(callback) or True)
        entered = threading.Event()
        release = threading.Event()

        def operation(_context):
            entered.set()
            release.wait(1)
            return {"items": [], "total_size": 0}

        self.assertTrue(SavedAttachmentsDialog._start_operation(
            dialog, operation, action="load"))
        self.assertTrue(entered.wait(1))
        self.assertFalse(SavedAttachmentsDialog._start_operation(
            dialog, operation, action="load"))
        release.set()
        deadline = time.monotonic() + 1
        while not callbacks and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(len(callbacks), 1)

    def test_failed_ui_post_clears_logical_busy_state(self):
        dialog = Mock()
        dialog._operation_busy = threading.Event()
        dialog._operation_serial = 0
        dialog._current_context.return_value = {}
        dialog._set_busy = Mock()
        dialog._modification_block_reason.return_value = None
        dialog._post_ui.return_value = False
        self.assertTrue(SavedAttachmentsDialog._start_operation(
            dialog,
            lambda _context: {"items": [], "total_size": 0},
            action="load",
        ))
        deadline = time.monotonic() + 1
        while dialog._operation_busy.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(dialog._operation_busy.is_set())

    def test_worker_error_restores_ui_state_without_destroying_dialog(self):
        dialog = Mock()
        dialog._operation_busy = threading.Event()
        dialog._operation_busy.set()
        dialog._alive = True
        dialog._operation_serial = 3
        dialog.winfo_exists.return_value = True
        dialog._set_busy = Mock()
        with patch("LLM_Local_Chat.messagebox.showerror") as showerror:
            SavedAttachmentsDialog._finish_operation(
                dialog,
                3,
                {},
                ("error", RuntimeError("failed")),
                action="delete_one",
                single=True,
            )
        self.assertFalse(dialog._operation_busy.is_set())
        dialog._set_busy.assert_called_once_with(False)
        showerror.assert_called_once()

    def test_closed_dialog_discards_late_worker_result(self):
        dialog = Mock()
        dialog._operation_busy = threading.Event()
        dialog._operation_busy.set()
        dialog._alive = False
        dialog._operation_serial = 2
        dialog._set_busy = Mock()
        SavedAttachmentsDialog._finish_operation(
            dialog,
            1,
            {},
            ("success", {"items": [], "total_size": 0}),
            action="load",
            single=None,
        )
        self.assertFalse(dialog._operation_busy.is_set())
        dialog._set_busy.assert_not_called()

    def test_busy_dialog_rejects_close_until_worker_finishes(self):
        dialog = Mock()
        dialog._operation_busy = threading.Event()
        dialog._operation_busy.set()
        dialog.destroy = Mock()
        with patch("LLM_Local_Chat.messagebox.showwarning") as warning:
            SavedAttachmentsDialog._close(dialog)
        warning.assert_called_once()
        dialog.destroy.assert_not_called()


class SavedAttachmentAppCoordinationTests(unittest.TestCase):
    def _app(self):
        app = ChatApp.__new__(ChatApp)
        app._closing = False
        app._llm_loading = False
        app._llm_load_active = threading.Event()
        app._portable_pending = threading.Event()
        app._ctrl = Mock()
        app._ctrl.is_busy.return_value = False
        return app

    def test_delete_is_blocked_by_generation_reload_portable_and_close(self):
        app = self._app()
        app._ctrl.is_busy.return_value = True
        self.assertIn("AI", app._attachment_modification_block_reason())
        app._ctrl.is_busy.return_value = False
        app._llm_loading = True
        self.assertIn("モデル", app._attachment_modification_block_reason())
        app._llm_loading = False
        app._portable_pending.set()
        self.assertIn("インポート", app._attachment_modification_block_reason())
        app._portable_pending.clear()
        app._closing = True
        self.assertIn("終了", app._attachment_modification_block_reason())

    def test_stale_chat_snapshot_is_not_applied(self):
        app = self._app()
        app._is_guest = False
        app._current_path = "chat-b.json"
        app._current_session = {"session_id": "b"}
        app._attachments = []
        app._update_attachment_display = Mock()
        applied = app._apply_attachment_manager_snapshot(
            {
                "current_path": "chat-a.json",
                "current_session": {"session_id": "a"},
                "current_attachments": [],
                "current_warnings": [],
            },
            {"path": "chat-a.json", "session_id": "a", "guest": False},
        )
        self.assertFalse(applied)
        self.assertEqual(app._current_session, {"session_id": "b"})
        app._update_attachment_display.assert_not_called()


class SavedAttachmentTkSmokeTests(unittest.TestCase):
    @staticmethod
    def _settings_config():
        return {
            "model_path": "model.gguf",
            "n_ctx": 8192,
            "max_tokens": 512,
            "temperature": 0.7,
            "vad_threshold": 150,
            "tts_rate": 0,
            "whisper_mode": "gpu_medium",
            "llm_gpu_offload_mode": "auto",
            "history_retention_days": 0,
            "mic_enabled": False,
            "tts_enabled": False,
        }

    @staticmethod
    def _drain(root, predicate, callbacks=None, timeout=3.0):
        callbacks = callbacks if callbacks is not None else []
        deadline = time.monotonic() + timeout
        while not predicate() and time.monotonic() < deadline:
            while callbacks:
                callbacks.pop(0)()
            root.update()
            time.sleep(0.01)
        while callbacks:
            callbacks.pop(0)()
        root.update()
        return bool(predicate())

    def _saved_chat(self, store, names):
        session = store.new_session()
        session["history"] = [{"user": "keep", "assistant": "kept"}]
        path = store.save(session, None)
        attachments = [
            load_attachment_bytes(name, name.encode("utf-8"))
            for name in names
        ]
        path, _added, _warnings = store.add_attachments(
            session, path, attachments)
        return session, path

    def _dialog(self, parent, root, store, session, path, busy):
        context = {
            "path": path,
            "session_id": session["session_id"],
            "guest": False,
        }

        callbacks = []

        def post_ui(callback):
            callbacks.append(callback)
            return True

        dialog = SavedAttachmentsDialog(
            parent,
            lambda current: store.saved_attachment_manager_snapshot(
                current.get("path")),
            lambda item, current: store.delete_saved_attachment_snapshot(
                item, current.get("path")),
            lambda current: store.delete_all_saved_attachments_snapshot(
                current.get("path")),
            post_ui,
            lambda: dict(context),
            lambda _snapshot, _context: True,
            lambda: None,
            busy,
        )
        dialog._test_ui_callbacks = callbacks
        return dialog

    def test_settings_manager_cancel_delete_refresh_and_close(self):
        with tempfile.TemporaryDirectory(
            prefix="test_saved_attachment_tk_"
        ) as temp_dir:
            store = SessionStore(temp_dir, protector=_FakeProtector())
            session, path = self._saved_chat(store, ["one.txt"])
            root = tk.Tk()
            root.withdraw()
            settings = SettingsDialog(root, self._settings_config())
            settings.grab_release()
            busy = threading.Event()
            dialog = self._dialog(
                settings, root, store, session, path, busy)
            try:
                self.assertTrue(self._drain(
                    root, lambda: not busy.is_set(),
                    dialog._test_ui_callbacks))
                row = dialog._tree.get_children()[0]
                dialog._tree.selection_set(row)
                with patch(
                    "LLM_Local_Chat.messagebox.askyesno",
                    return_value=False,
                ):
                    dialog._delete_selected()
                self.assertEqual(len(store.list_saved_attachments()), 1)

                dialog._tree.selection_set(row)
                with (
                    patch(
                        "LLM_Local_Chat.messagebox.askyesno",
                        return_value=True,
                    ),
                    patch("LLM_Local_Chat.messagebox.showinfo"),
                    patch("LLM_Local_Chat.messagebox.showwarning"),
                ):
                    dialog._delete_selected()
                    self.assertTrue(self._drain(
                        root, lambda: not busy.is_set(),
                        dialog._test_ui_callbacks))
                self.assertEqual(dialog._tree.get_children(), ())
                self.assertEqual(store.load(path)["attachments"], [])
                self.assertEqual(
                    store.load(path)["history"][0]["assistant"], "kept")
                self.assertTrue(root.winfo_exists())
            finally:
                if dialog.winfo_exists():
                    dialog._close()
                if settings.winfo_exists():
                    settings.destroy()
                root.destroy()

    def test_bulk_delete_refresh_and_close_keeps_tk_alive(self):
        with tempfile.TemporaryDirectory(
            prefix="test_saved_attachment_bulk_tk_"
        ) as temp_dir:
            store = SessionStore(temp_dir, protector=_FakeProtector())
            session, path = self._saved_chat(
                store, ["one.txt", "two.txt"])
            root = tk.Tk()
            root.withdraw()
            busy = threading.Event()
            dialog = self._dialog(
                root, root, store, session, path, busy)
            try:
                self.assertTrue(self._drain(
                    root, lambda: not busy.is_set(),
                    dialog._test_ui_callbacks))
                with (
                    patch(
                        "LLM_Local_Chat.messagebox.askyesno",
                        return_value=True,
                    ),
                    patch("LLM_Local_Chat.messagebox.showinfo"),
                    patch("LLM_Local_Chat.messagebox.showwarning"),
                ):
                    dialog._delete_everything()
                    self.assertTrue(self._drain(
                        root, lambda: not busy.is_set(),
                        dialog._test_ui_callbacks))
                self.assertEqual(dialog._tree.get_children(), ())
                self.assertEqual(store.load(path)["attachments"], [])
                self.assertTrue(root.winfo_exists())
            finally:
                if dialog.winfo_exists():
                    dialog._close()
                root.destroy()

    def test_real_mainloop_cancel_individual_and_bulk_delete(self):
        with tempfile.TemporaryDirectory(
            prefix="test_saved_attachment_mainloop_"
        ) as temp_dir:
            store = SessionStore(temp_dir, protector=_FakeProtector())
            first, first_path = self._saved_chat(store, ["first.txt"])
            second, second_path = self._saved_chat(
                store, ["second.txt", "third.txt"])
            root = tk.Tk()
            root.withdraw()
            settings = SettingsDialog(root, self._settings_config())
            settings.grab_release()
            app = ChatApp.__new__(ChatApp)
            app.root = root
            app._closing = False
            busy = threading.Event()
            errors = []
            state = {"phase": "first_load", "dialog": None}

            def make_dialog(session, path):
                context = {
                    "path": path,
                    "session_id": session["session_id"],
                    "guest": False,
                }
                return SavedAttachmentsDialog(
                    settings,
                    lambda current: store.saved_attachment_manager_snapshot(
                        current.get("path")),
                    lambda item, current: (
                        store.delete_saved_attachment_snapshot(
                            item, current.get("path"))),
                    lambda current: (
                        store.delete_all_saved_attachments_snapshot(
                            current.get("path"))),
                    app._post_ui,
                    lambda: dict(context),
                    lambda _snapshot, _context: True,
                    lambda: None,
                    busy,
                )

            state["dialog"] = make_dialog(first, first_path)

            def fail_timeout():
                errors.append("Tk mainloop smoke timed out")
                root.quit()

            def drive():
                try:
                    if busy.is_set():
                        root.after(10, drive)
                        return
                    dialog = state["dialog"]
                    phase = state["phase"]
                    if phase == "first_load":
                        row = next(
                            key for key, item in dialog._rows.items()
                            if item["session_path"] == first_path)
                        dialog._tree.selection_set(row)
                        dialog._delete_selected()  # cancel
                        self.assertEqual(
                            len(store.load(first_path)["attachments"]), 1)
                        dialog._tree.selection_set(row)
                        dialog._delete_selected()  # execute
                        state["phase"] = "first_deleted"
                    elif phase == "first_deleted":
                        self.assertEqual(
                            store.load(first_path)["attachments"], [])
                        dialog._close()
                        state["dialog"] = make_dialog(second, second_path)
                        state["phase"] = "bulk_load"
                    elif phase == "bulk_load":
                        dialog._delete_everything()
                        state["phase"] = "bulk_deleted"
                    else:
                        self.assertEqual(
                            store.load(second_path)["attachments"], [])
                        self.assertEqual(dialog._tree.get_children(), ())
                        self.assertTrue(root.winfo_exists())
                        dialog._close()
                        settings.destroy()
                        root.quit()
                        return
                except Exception as exc:
                    errors.append(repr(exc))
                    root.quit()
                    return
                root.after(10, drive)

            root.after(10, drive)
            root.after(8000, fail_timeout)
            try:
                with (
                    patch(
                        "LLM_Local_Chat.messagebox.askyesno",
                        side_effect=[False, True, True],
                    ),
                    patch("LLM_Local_Chat.messagebox.showinfo"),
                    patch("LLM_Local_Chat.messagebox.showwarning"),
                    patch("LLM_Local_Chat.messagebox.showerror") as showerror,
                ):
                    root.mainloop()
                self.assertEqual(errors, [])
                showerror.assert_not_called()
            finally:
                dialog = state.get("dialog")
                if dialog is not None and dialog.winfo_exists():
                    if not busy.is_set():
                        dialog._close()
                    else:
                        dialog.destroy()
                if settings.winfo_exists():
                    settings.destroy()
                root.destroy()


class ShutdownTests(unittest.TestCase):
    def test_failed_import_confirmation_post_clears_pending(self):
        app = ChatApp.__new__(ChatApp)
        app._portable_pending = threading.Event()
        app._portable_pending.set()
        app._post_ui = lambda _callback: False
        posted = app._post_portable_import_confirmation([], 0, 0)
        self.assertFalse(posted)
        self.assertFalse(app._portable_pending.is_set())

    def test_close_is_blocked_while_portable_history_is_running(self):
        app = ChatApp.__new__(ChatApp)
        app.root = _Root()
        app._closing = False
        app._portable_pending = threading.Event()
        app._portable_pending.set()
        app._save_now = lambda **_kwargs: self.fail(
            "save must not run while portable history is active")
        with patch("LLM_Local_Chat.messagebox.showwarning") as warning:
            app._on_close()
        warning.assert_called_once()
        self.assertFalse(app._closing)

    def test_close_is_blocked_while_attachment_manager_worker_is_running(self):
        app = ChatApp.__new__(ChatApp)
        app.root = _Root()
        app._closing = False
        app._attachment_manager_busy = threading.Event()
        app._attachment_manager_busy.set()
        app._portable_pending = threading.Event()
        app._save_now = Mock()
        with patch("LLM_Local_Chat.messagebox.showwarning") as warning:
            app._on_close()
        warning.assert_called_once()
        app._save_now.assert_not_called()
        self.assertFalse(app._closing)

    def test_cancelled_close_keeps_session_attachments(self):
        app = ChatApp.__new__(ChatApp)
        app.root = _Root()
        app._closing = False
        app._attachments = [object()]
        app._portable_pending = threading.Event()
        app._save_now = lambda **_kwargs: False
        with patch("LLM_Local_Chat.messagebox.askyesno", return_value=False):
            app._on_close()
        self.assertFalse(app._closing)
        self.assertEqual(len(app._attachments), 1)

    def test_close_stops_components_and_destroys_when_idle(self):
        root = _Root()
        app = ChatApp.__new__(ChatApp)
        app.root = root
        app._closing = False
        app._attachments = [object()]
        app.llm = None
        app._llm_load_generation = 4
        app._llm_load_active = threading.Event()
        prepare_event = threading.Event()
        accept_event = threading.Event()
        app._active_reload_job = {
            "prepare_event": prepare_event,
            "accept_event": accept_event,
        }
        app._cfg = {}
        app._voice = type("Voice", (), {
            "enabled": True,
            "stop": lambda self: setattr(self, "stopped", True),
        })()
        app.tts = type("TTS", (), {
            "enabled": True,
            "terminate": lambda self: setattr(self, "terminated", True),
        })()
        service = type("Service", (), {
            "is_running": lambda self: False,
            "detach_llm": lambda self: None,
        })()
        app._ctrl = type("Ctrl", (), {
            "_llm_service": service,
            "begin_shutdown": lambda self: setattr(self, "shutdown", True),
        })()
        app._integrations = type("Bridge", (), {
            "begin_closing": lambda self: setattr(self, "closing", True),
            "pending_operations": lambda self: [],
        })()
        app._save_now = lambda **_kwargs: True
        app._deps = type("Deps", (), {
            "res_monitor": type("Monitor", (), {
                "stop": lambda self: setattr(self, "stopped", True),
            })(),
        })()

        with patch("LLM_Local_Chat.save_settings"):
            app._on_close()
        self.assertTrue(app._closing)
        self.assertEqual(app._attachments, [])
        self.assertTrue(app._ctrl.shutdown)
        self.assertTrue(app._voice.stopped)
        self.assertTrue(app.tts.terminated)
        self.assertTrue(app._deps.res_monitor.stopped)
        self.assertTrue(prepare_event.is_set())
        self.assertTrue(accept_event.is_set())
        root.callbacks.pop(0)()
        self.assertTrue(root.destroyed)


class HistoryStartupFailureTests(unittest.TestCase):
    def test_unreported_crypto_error_shows_once_and_stops_monitor(self):
        root = _Root()
        monitor = type("Monitor", (), {
            "stop": lambda self: setattr(self, "stopped", True),
        })()
        deps = type("Deps", (), {"res_monitor": monitor})()
        with (
            patch("LLM_Local_Chat.tk.Tk", return_value=root),
            patch("LLM_Local_Chat.create_app_deps", return_value=deps),
            patch(
                "LLM_Local_Chat.ChatApp",
                side_effect=HistoryCryptoError("DPAPI unavailable"),
            ),
            patch("LLM_Local_Chat.messagebox.showerror") as showerror,
        ):
            main()
        showerror.assert_called_once()
        self.assertNotIn("ciphertext", str(showerror.call_args))
        self.assertTrue(monitor.stopped)
        self.assertTrue(root.destroyed)
        self.assertTrue(root.withdrawn)
        self.assertFalse(root.deiconified)

    def test_reported_crypto_error_does_not_show_twice(self):
        root = _Root()
        monitor = type("Monitor", (), {
            "stop": lambda self: setattr(self, "stopped", True),
        })()
        deps = type("Deps", (), {"res_monitor": monitor})()
        error = HistoryCryptoError("already shown")
        error.user_notified = True
        with (
            patch("LLM_Local_Chat.tk.Tk", return_value=root),
            patch("LLM_Local_Chat.create_app_deps", return_value=deps),
            patch("LLM_Local_Chat.ChatApp", side_effect=error),
            patch("LLM_Local_Chat.messagebox.showerror") as showerror,
        ):
            main()
        showerror.assert_not_called()
        self.assertTrue(monitor.stopped)
        self.assertTrue(root.destroyed)
        self.assertTrue(root.withdrawn)
        self.assertFalse(root.deiconified)


if __name__ == "__main__":
    unittest.main()
