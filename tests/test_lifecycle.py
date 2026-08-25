import threading
import time
import unittest
from unittest.mock import patch

from integrations import IntegrationBridge
from history_crypto import HistoryCryptoError
from LLM_Local_Chat import APP_VERSION, ChatApp, main
from llm_service import LLMService


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

    def test_close_stops_components_and_destroys_when_idle(self):
        root = _Root()
        app = ChatApp.__new__(ChatApp)
        app.root = root
        app._closing = False
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
        service = type("Service", (), {"is_running": lambda self: False})()
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
