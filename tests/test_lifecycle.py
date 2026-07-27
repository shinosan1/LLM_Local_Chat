import threading
import unittest
from unittest.mock import patch

from integrations import IntegrationBridge
from history_crypto import HistoryCryptoError
from LLM_Local_Chat import ChatApp, main
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

    def after(self, _delay, callback):
        self.callbacks.append(callback)

    def destroy(self):
        self.destroyed = True


class ModelReferenceTests(unittest.TestCase):
    def test_service_detach_and_attach_require_idle_state(self):
        model = object()
        service = LLMService(model)
        self.assertIs(service.detach_llm(), model)
        self.assertIsNone(service.llm)
        replacement = object()
        service.attach_llm(replacement)
        self.assertIs(service.llm, replacement)

    def test_detach_closes_old_model_reference(self):
        model = _Model()
        app = ChatApp.__new__(ChatApp)
        app.llm = model
        app._ctrl = type("Ctrl", (), {
            "_llm_service": LLMService(model),
        })()
        with patch("LLM_Local_Chat.gc.collect"):
            app._detach_current_llm()
        self.assertIsNone(app.llm)
        self.assertIsNone(app._ctrl._llm_service.llm)
        self.assertEqual(model.closed, 1)

    def test_stale_load_result_is_closed_instead_of_attached(self):
        model = _Model()
        app = ChatApp.__new__(ChatApp)
        app._llm_load_generation = 2
        app._closing = False
        with patch("LLM_Local_Chat.gc.collect"):
            app._on_llm_ready(1, model, None)
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
    def test_close_stops_components_and_destroys_when_idle(self):
        root = _Root()
        app = ChatApp.__new__(ChatApp)
        app.root = root
        app._closing = False
        app._llm_load_generation = 4
        app._llm_load_active = threading.Event()
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


if __name__ == "__main__":
    unittest.main()
