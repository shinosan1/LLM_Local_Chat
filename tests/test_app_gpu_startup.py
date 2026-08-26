import unittest
import types
import threading
import time
import json
import tempfile
from pathlib import Path
from unittest.mock import mock_open, patch

import LLM_Local_Chat
from LLM_Local_Chat import (
    ChatApp,
    format_llm_offload_state,
    init_llm,
    load_settings,
)


class _Monitor:
    def __init__(self, snapshots=None):
        self.llm_uses_gpu = None
        self._snapshots = iter(snapshots) if snapshots is not None else None

    def snapshot(self):
        if self._snapshots is not None:
            return next(self._snapshots)
        return {
            "available": True, "total_mb": 8192,
            "used_mb": 1000, "free_mb": 7192,
            "used_ratio": 1000 / 8192,
        }


def _snapshot(used):
    return {
        "available": True,
        "total_mb": 8192,
        "used_mb": used,
        "free_mb": 8192 - used,
        "used_ratio": used / 8192,
    }


class _FakeModel:
    def __init__(self, close_error=None, state=None):
        self.closed = 0
        self.close_error = close_error
        if state is not None:
            self._offload_runtime_state = state

    def close(self):
        self.closed += 1
        if self.close_error:
            raise self.close_error


def _decision(candidates, total_layers=42):
    return {
        "n_gpu_layers": candidates[0][0],
        "snapshot": _snapshot(1000),
        "model_mb": 5088,
        "total_layers": total_layers,
        "load_candidates": [
            {
                "n_gpu_layers": layers,
                "mode": mode,
                "reason": reason,
                "required_mb": required,
            }
            for layers, mode, reason, required in candidates
        ],
    }


def _wait_until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class StartupOrderingTests(unittest.TestCase):
    def test_whisper_starts_only_once_after_llm_completion(self):
        app = ChatApp.__new__(ChatApp)
        app._whisper_load_started = False
        calls = []
        app._load_whisper_async = lambda: calls.append("whisper")

        app._start_whisper_after_llm_once()
        app._start_whisper_after_llm_once()

        self.assertEqual(calls, ["whisper"])
        self.assertTrue(app._whisper_load_started)

    def test_gpu_load_failure_retries_on_cpu(self):
        calls = []

        def fake_llama(**kwargs):
            calls.append(kwargs["n_gpu_layers"])
            if kwargs["n_gpu_layers"] == -1:
                raise RuntimeError("CUDA out of memory")
            return _FakeModel()

        decision = _decision([
            (-1, "full", "gpu_full_offload", 6112),
            (0, "cpu", "cpu_after_gpu_fallback", 0),
        ])
        monitor = _Monitor()
        with (
            patch.object(LLM_Local_Chat, "adjust_llm", return_value=decision),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
        ):
            model = init_llm(__file__, 1024, monitor)

        self.assertIsNotNone(model)
        self.assertEqual(calls[-1], 0)
        self.assertEqual(model._offload_runtime_state["mode"], "cpu")
        self.assertIsNone(monitor.llm_uses_gpu)

    def test_partial_oom_falls_from_three_quarters_to_half(self):
        calls = []

        def fake_llama(**kwargs):
            calls.append(kwargs["n_gpu_layers"])
            if kwargs["n_gpu_layers"] == 32:
                raise RuntimeError("ggml_cuda failed to allocate: out of memory")
            return _FakeModel()

        decision = _decision([
            (32, "partial", "partial_75", 4901),
            (21, "partial", "partial_50", 3568),
            (11, "partial", "partial_25", 2357),
            (0, "cpu", "cpu_after_gpu_fallback", 0),
        ])
        with (
            patch.object(LLM_Local_Chat, "adjust_llm", return_value=decision),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
            patch.object(LLM_Local_Chat, "_release_cuda_resources"),
        ):
            model = init_llm(__file__, 1024, _Monitor())
        self.assertEqual(calls, [32, 21])
        self.assertEqual(model._offload_runtime_state["n_gpu_layers"], 21)
        self.assertEqual(model._offload_runtime_state["mode"], "partial")

    def test_post_load_reserve_violation_closes_then_uses_lower_candidate(self):
        loaded = []

        def fake_llama(**_kwargs):
            model = _FakeModel()
            loaded.append(model)
            return model

        monitor = _Monitor([
            _snapshot(1000), _snapshot(7900),
            _snapshot(2000), _snapshot(4000),
        ])
        decision = _decision([
            (32, "partial", "partial_75", 4901),
            (21, "partial", "partial_50", 3568),
            (0, "cpu", "cpu_after_gpu_fallback", 0),
        ])
        with (
            patch.object(LLM_Local_Chat, "adjust_llm", return_value=decision),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
            patch.object(LLM_Local_Chat, "_release_cuda_resources"),
        ):
            model = init_llm(__file__, 1024, monitor)
        self.assertEqual(loaded[0].closed, 1)
        self.assertIs(model, loaded[1])
        self.assertEqual(model._offload_runtime_state["n_gpu_layers"], 21)

    def test_close_failure_stops_before_loading_lower_candidate(self):
        calls = []

        def fake_llama(**kwargs):
            calls.append(kwargs["n_gpu_layers"])
            return _FakeModel(RuntimeError("close failed"))

        monitor = _Monitor([_snapshot(1000), _snapshot(7900)])
        decision = _decision([
            (32, "partial", "partial_75", 4901),
            (21, "partial", "partial_50", 3568),
            (0, "cpu", "cpu_after_gpu_fallback", 0),
        ])
        with (
            patch.object(LLM_Local_Chat, "adjust_llm", return_value=decision),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
        ):
            with self.assertRaisesRegex(RuntimeError, "解放に失敗"):
                init_llm(__file__, 1024, monitor)
        self.assertEqual(calls, [32])

    def test_non_vram_error_is_not_retried_at_lower_layer_count(self):
        calls = []

        def fake_llama(**kwargs):
            calls.append(kwargs["n_gpu_layers"])
            raise ValueError("invalid GGUF model")

        decision = _decision([
            (32, "partial", "partial_75", 4901),
            (21, "partial", "partial_50", 3568),
            (0, "cpu", "cpu_after_gpu_fallback", 0),
        ])
        with (
            patch.object(LLM_Local_Chat, "adjust_llm", return_value=decision),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
        ):
            with self.assertRaisesRegex(ValueError, "invalid GGUF"):
                init_llm(__file__, 1024, _Monitor())
        self.assertEqual(set(calls), {32})


class VisionHandlerLifecycleTests(unittest.TestCase):
    def test_handler_specific_constructor_arguments_match_034_api(self):
        for handler_name, expects_use_gpu in (("gemma4", True), ("llava15", False)):
            captured = {}

            class ExitStack:
                def close(self):
                    captured["closed"] = captured.get("closed", 0) + 1

            class BaseHandler:
                def __init__(self, **kwargs):
                    captured["kwargs"] = kwargs
                    self._exit_stack = ExitStack()

                    class Params:
                        image_max_tokens = -1

                    class MTMDModule:
                        @staticmethod
                        def mtmd_context_params_default():
                            return Params()

                    self._mtmd_cpp = MTMDModule()

            patch_name = (
                "llama_cpp.llama_chat_format.Gemma4ChatHandler"
                if handler_name == "gemma4"
                else "llama_cpp.llama_chat_format.Llava15ChatHandler"
            )
            settings = {
                "enabled": True,
                "handler": handler_name,
                "projector_path": __file__,
            }
            with self.subTest(handler=handler_name), patch(
                patch_name, BaseHandler
            ):
                handler = LLM_Local_Chat.create_vision_chat_handler(settings)
                self.assertEqual(
                    captured["kwargs"]["clip_model_path"], __file__)
                self.assertFalse(captured["kwargs"]["verbose"])
                self.assertEqual(
                    "use_gpu" in captured["kwargs"], expects_use_gpu)
                params = handler._mtmd_cpp.mtmd_context_params_default()
                self.assertEqual(
                    params.image_max_tokens,
                    LLM_Local_Chat.IMAGE_CONTEXT_TOKEN_RESERVE,
                )
                handler.close()
                handler.close()
                self.assertEqual(captured["closed"], 1)

    def test_text_model_load_keeps_existing_llama_kwargs(self):
        captured = []

        def fake_llama(**kwargs):
            captured.append(kwargs)
            return _FakeModel()

        with (
            patch.object(
                LLM_Local_Chat,
                "adjust_llm",
                return_value=_decision([(0, "cpu", "cpu", 0)]),
            ),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
        ):
            init_llm(__file__, 1024, _Monitor())
        self.assertNotIn("chat_handler", captured[0])

    def test_llama_failure_closes_handler_and_retry_gets_new_handler(self):
        handlers = []
        calls = []

        class Handler:
            def __init__(self):
                self.closed = 0

            def close(self):
                self.closed += 1

        def make_handler(_settings):
            handler = Handler()
            handlers.append(handler)
            return handler

        def fake_llama(**kwargs):
            calls.append(kwargs["chat_handler"])
            if len(calls) == 1:
                raise TypeError("unsupported perf")
            return _FakeModel()

        vision = {
            "enabled": True,
            "handler": "gemma4",
            "projector_path": __file__,
        }
        with (
            patch.object(
                LLM_Local_Chat,
                "adjust_llm",
                return_value=_decision([(0, "cpu", "cpu", 0)]),
            ),
            patch.object(
                LLM_Local_Chat,
                "create_vision_chat_handler",
                side_effect=make_handler,
            ),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
        ):
            model = init_llm(
                __file__, 1024, _Monitor(), vision_settings=vision)

        self.assertEqual(len(handlers), 2)
        self.assertEqual(handlers[0].closed, 1)
        self.assertEqual(handlers[1].closed, 0)
        self.assertIs(model._shiro_vision_handler, handlers[1])

    def test_close_order_is_handler_then_llama_and_is_idempotent(self):
        events = []

        class Handler:
            def close(self):
                events.append("handler")

        class Model:
            _shiro_vision_handler = Handler()

            def close(self):
                events.append("llama")

        model = Model()
        with patch.object(LLM_Local_Chat, "_release_cuda_resources"):
            LLM_Local_Chat._close_loaded_candidate(model)
        LLM_Local_Chat._close_llm_resources(model)
        self.assertEqual(events, ["handler", "llama"])

    def test_invalid_handler_and_missing_projector_are_rejected_before_llama(self):
        for vision, error in (
            ({"enabled": True, "handler": "unknown", "projector_path": __file__},
             "vision_handler"),
            ({"enabled": True, "handler": "gemma4", "projector_path": ""},
             "projector"),
        ):
            with self.subTest(vision=vision), patch.object(
                LLM_Local_Chat, "Llama"
            ) as llama:
                with self.assertRaisesRegex((ValueError, FileNotFoundError), error):
                    init_llm(__file__, 1024, _Monitor(), vision_settings=vision)
                llama.assert_not_called()


class GpuLoadErrorClassificationTests(unittest.TestCase):
    def test_accepts_only_gpu_specific_or_gpu_context_oom(self):
        positives = (
            RuntimeError("CUDA out of memory"),
            RuntimeError("ggml_cuda failed to allocate buffer"),
            RuntimeError("CUBLAS_STATUS_ALLOC_FAILED"),
            RuntimeError("cudaMalloc failed"),
        )
        for exc in positives:
            with self.subTest(exc=exc):
                self.assertTrue(LLM_Local_Chat._is_gpu_vram_load_error(exc))

    def test_rejects_host_memory_and_generic_cuda_errors(self):
        negatives = (
            MemoryError("out of memory"),
            RuntimeError("std::bad_alloc"),
            RuntimeError("paging file is too small"),
            RuntimeError("failed to allocate host buffer"),
            RuntimeError("CUDA backend unsupported"),
            ValueError("Failed to load model from file"),
            TypeError("invalid keyword"),
        )
        for exc in negatives:
            with self.subTest(exc=exc):
                self.assertFalse(LLM_Local_Chat._is_gpu_vram_load_error(exc))


class OffloadStateUiTests(unittest.TestCase):
    def test_old_settings_are_not_forced_to_add_new_optional_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = Path(temp_dir) / "chat_settings.json"
            settings_path.write_text(
                json.dumps({"model_path": "old.gguf"}), encoding="utf-8")
            with patch.object(
                LLM_Local_Chat, "SETTINGS_FILE", str(settings_path)
            ):
                loaded = load_settings()
                self.assertTrue(LLM_Local_Chat.save_settings(loaded))
            saved = json.loads(settings_path.read_text(encoding="utf-8"))
        for key in (
            "system_prompt",
            "user_personalization",
            "response_language",
            "reasoning_visibility_instruction",
            "external_prompt_files",
            "vision_enabled",
            "vision_handler",
            "vision_projector_path",
        ):
            self.assertNotIn(key, saved)

    def test_missing_or_invalid_saved_mode_is_backward_compatible_auto(self):
        for payload in ("{}", '{"llm_gpu_offload_mode":"invalid"}'):
            with (
                self.subTest(payload=payload),
                patch("LLM_Local_Chat.os.path.exists", return_value=True),
                patch("builtins.open", mock_open(read_data=payload)),
            ):
                self.assertEqual(
                    load_settings()["llm_gpu_offload_mode"], "auto")

    def test_formats_full_partial_cpu_and_unknown_states(self):
        cases = (
            ({"mode": "full", "n_gpu_layers": -1, "total_layers": 42},
             "100%（42 / 42層・Full GPU・自動選択）"),
            ({"mode": "partial", "n_gpu_layers": 32, "total_layers": 42},
             "76%（32 / 42層・Partial GPU・自動選択）"),
            ({"mode": "partial", "n_gpu_layers": 21, "total_layers": 42},
             "50%（21 / 42層・Partial GPU・自動選択）"),
            ({"mode": "partial", "n_gpu_layers": 11, "total_layers": 42},
             "26%（11 / 42層・Partial GPU・自動選択）"),
            ({"mode": "cpu", "n_gpu_layers": 0, "total_layers": 42},
             "0%（0 / 42層・CPU・自動選択）"),
            (None, "未ロード"),
            ({"mode": "full", "n_gpu_layers": -1, "total_layers": None},
             "Full GPU（自動選択・総レイヤー数不明）"),
            ({"mode": "cpu", "n_gpu_layers": 0, "total_layers": None},
             "CPU（自動選択・総レイヤー数不明）"),
            ({"mode": "partial", "n_gpu_layers": 50, "total_layers": 42},
             "不明"),
            ({
                "mode": "partial", "n_gpu_layers": 21, "total_layers": 42,
                "selection_reason": "auto_downshift",
            }, "50%（21 / 42層・Partial GPU・VRAM不足により自動downshift）"),
            ({
                "mode": "cpu", "n_gpu_layers": 0, "total_layers": 42,
                "selection_reason": "manual_select",
            }, "0%（0 / 42層・CPU・手動選択）"),
        )
        for state, expected in cases:
            with self.subTest(state=state):
                self.assertEqual(format_llm_offload_state(state), expected)

    def test_settings_receives_runtime_state_separately_from_saved_config(self):
        captured = {}

        class FakeDialog:
            result = None

            def __init__(self, _parent, cfg, offload_state=None):
                captured["cfg"] = cfg
                captured["offload_state"] = offload_state

        app = ChatApp.__new__(ChatApp)
        app.root = types.SimpleNamespace(wait_window=lambda _dlg: None)
        app._model_path = "model.gguf"
        app._n_ctx = 8192
        app._max_tokens = 512
        app._temperature = 0.7
        app._vad_thresh = 150
        app.tts = types.SimpleNamespace(rate=0, enabled=False)
        app._cfg = {
            "history_retention_days": 0,
            "mic_enabled": False,
            "whisper_mode": "auto",
        }
        app._llm_offload_state = {
            "mode": "partial", "n_gpu_layers": 32, "total_layers": 42,
        }
        with patch.object(LLM_Local_Chat, "SettingsDialog", FakeDialog):
            app._open_settings()
        self.assertNotIn("llm_offload_state", captured["cfg"])
        self.assertEqual(captured["cfg"]["llm_gpu_offload_mode"], "auto")
        self.assertEqual(captured["offload_state"]["n_gpu_layers"], 32)
        self.assertNotIn("llm_offload_state", app._cfg)

    def test_settings_manual_mode_transitions_request_hot_reload_without_restart(self):
        requested = []
        results = iter(("cpu", "auto", "75", "50", "cpu"))

        class FakeDialog:
            def __init__(self, _parent, cfg, offload_state=None):
                mode = next(results)
                self.result = {
                    **cfg,
                    "llm_gpu_offload_mode": mode,
                    "gpu_reassess": False,
                }

        app = ChatApp.__new__(ChatApp)
        app.root = types.SimpleNamespace(wait_window=lambda _dlg: None)
        app._model_path = "model.gguf"
        app._n_ctx = 8192
        app._max_tokens = 512
        app._temperature = 0.7
        app._vad_thresh = 150
        app._llm_gpu_offload_mode = "auto"
        app._llm_offload_state = {
            "mode": "partial", "n_gpu_layers": 32, "total_layers": 42,
        }
        app._llm_loading = False
        app._voice = None
        app.tts = types.SimpleNamespace(rate=0, enabled=False)
        app._tts_var = types.SimpleNamespace(set=lambda _value: None)
        app._cfg = {
            "model_path": "model.gguf",
            "n_ctx": 8192,
            "max_tokens": 512,
            "temperature": 0.7,
            "vad_threshold": 150,
            "tts_rate": 0,
            "history_retention_days": 0,
            "mic_enabled": False,
            "whisper_mode": "auto",
            "llm_gpu_offload_mode": "auto",
            "tts_enabled": False,
        }
        app._ctrl = types.SimpleNamespace(is_busy=lambda: False)
        app._update_status = lambda: None

        def reload_llm(**kwargs):
            mode = kwargs["target_config"][2]
            requested.append(mode)
            app._llm_gpu_offload_mode = mode
            app._cfg["llm_gpu_offload_mode"] = mode
            return True

        app._reload_llm = reload_llm
        with (
            patch.object(LLM_Local_Chat, "SettingsDialog", FakeDialog),
            patch.object(LLM_Local_Chat, "save_settings", return_value=True),
        ):
            for _ in range(5):
                app._open_settings()

        self.assertEqual(requested, ["cpu", "auto", "75", "50", "cpu"])
        self.assertEqual(app._llm_gpu_offload_mode, "cpu")


class OffloadRuntimeLifecycleTests(unittest.TestCase):
    def test_reload_detach_failure_preserves_runtime_state(self):
        old_model = _FakeModel()
        old_state = {"mode": "partial", "n_gpu_layers": 32, "total_layers": 42}

        class Service:
            @staticmethod
            def is_running():
                return False

            @staticmethod
            def detach_llm():
                raise RuntimeError("busy")

        monitor = types.SimpleNamespace(llm_uses_gpu=True)
        app = ChatApp.__new__(ChatApp)
        app.llm = old_model
        app._ctrl = types.SimpleNamespace(_llm_service=Service())
        app._llm_load_generation = 7
        app._closing = False
        app._active_reload_job = None
        app._llm_offload_state = old_state
        app._deps = types.SimpleNamespace(
            res_monitor=monitor,
            whisper_pool=types.SimpleNamespace(
                request_reload_pause=lambda: None,
            ),
        )
        job = {
            "generation": 7,
            "prepare_event": threading.Event(),
            "prepared": False,
            "error": None,
        }
        app._active_reload_job = job
        app._prepare_reload_on_main(job)
        self.assertIs(app.llm, old_model)
        self.assertIs(app._llm_offload_state, old_state)
        self.assertTrue(monitor.llm_uses_gpu)
        self.assertEqual(app._llm_load_generation, 7)
        self.assertFalse(job["prepared"])
        self.assertIsInstance(job["error"], RuntimeError)

    def test_hot_reload_closes_old_before_loading_new_and_updates_actual(self):
        events = []
        old_model = _FakeModel()
        new_model = _FakeModel(state={
            "mode": "cpu", "n_gpu_layers": 0,
            "selected_gpu_layers": 0, "total_layers": 42,
            "selection_reason": "manual_select",
        })

        class Service:
            def __init__(self):
                self.llm = old_model

            def is_running(self):
                return False

            def detach_llm(self):
                model, self.llm = self.llm, None
                return model

            def attach_llm(self, model):
                self.llm = model
                events.append("attach")

        class Whisper:
            def request_reload_pause(self):
                events.append("pause")

            def begin_llm_reload(self, **_kwargs):
                events.append("whisper_wait")
                return True

            def end_llm_reload(self):
                events.append("whisper_resume")

        app = ChatApp.__new__(ChatApp)
        app.llm = old_model
        service = Service()
        app._ctrl = types.SimpleNamespace(
            _llm_service=service,
            is_busy=lambda: False,
            clear_token_cache=lambda: None,
        )
        app._deps = types.SimpleNamespace(
            res_monitor=types.SimpleNamespace(llm_uses_gpu=True),
            whisper_pool=Whisper(),
        )
        app._closing = False
        app._closing_event = threading.Event()
        app._llm_loading = False
        app._llm_load_generation = 0
        app._llm_load_active = threading.Event()
        app._active_reload_job = None
        app._llm_offload_state = {
            "mode": "partial", "n_gpu_layers": 32, "total_layers": 42,
        }
        app._model_path = "old.gguf"
        app._n_ctx = 8192
        app._llm_gpu_offload_mode = "auto"
        app._llm_perf_settings = {}
        app._cfg = {}
        app._post_ui = lambda callback: (callback(), True)[1]
        app._set_reload_controls = lambda enabled: events.append(
            f"controls:{enabled}")
        app._status_set = lambda _text: None
        app._update_status = lambda: None
        app._start_whisper_after_llm_once = lambda: None

        def close(model, **kwargs):
            events.append("close_old" if model is old_model else "close_new")
            model.close()

        app._close_llm_instance = close

        def load(*_args, **kwargs):
            self.assertEqual(old_model.closed, 1)
            self.assertEqual(kwargs["offload_mode"], "cpu")
            events.append("load_new")
            return new_model

        with (
            patch.object(app, "_preflight_llm_reload"),
            patch.object(LLM_Local_Chat, "init_llm", side_effect=load),
            patch.object(LLM_Local_Chat, "save_settings", return_value=True),
        ):
            self.assertTrue(app._reload_llm(
                target_config=("new.gguf", 4096, "cpu"),
                persist_on_success=True,
            ))
            self.assertTrue(_wait_until(lambda: not app._llm_load_active.is_set()))

        self.assertLess(events.index("close_old"), events.index("load_new"))
        self.assertLess(events.index("load_new"), events.index("attach"))
        self.assertIs(app.llm, new_model)
        self.assertIs(service.llm, new_model)
        self.assertEqual(app._llm_gpu_offload_mode, "cpu")
        self.assertEqual(app._llm_offload_state["n_gpu_layers"], 0)
        self.assertEqual(app._llm_offload_state["reload_generation"], 1)

    def test_auto_downshift_uses_actual_current_layers(self):
        app = ChatApp.__new__(ChatApp)
        app._llm_offload_state = {
            "mode": "partial", "n_gpu_layers": 32, "total_layers": 42,
        }
        app._model_path = "model.gguf"
        app._n_ctx = 8192
        captured = {}
        app._reload_llm = lambda **kwargs: captured.update(kwargs) or True
        callback = lambda *_args: None
        self.assertTrue(app._request_auto_downshift(callback))
        self.assertEqual(captured["downshift_from_layers"], 32)
        self.assertEqual(captured["target_config"], ("model.gguf", 8192, "auto"))
        self.assertFalse(captured["allow_recovery"])

    def test_stale_or_closing_reload_result_is_never_attached(self):
        model = _FakeModel(state={
            "mode": "partial", "n_gpu_layers": 21, "total_layers": 42,
        })
        attached = []
        app = ChatApp.__new__(ChatApp)
        app._closing = False
        app._llm_load_generation = 3
        current_job = {"generation": 3}
        stale_job = {
            "generation": 2,
            "accept_event": threading.Event(),
            "accepted": False,
        }
        app._active_reload_job = current_job
        app._ctrl = types.SimpleNamespace(
            _llm_service=types.SimpleNamespace(
                attach_llm=lambda value: attached.append(value)),
        )
        app._accept_reload_on_main(stale_job, model)
        self.assertTrue(stale_job["accept_event"].is_set())
        self.assertEqual(attached, [])

        closing_job = {
            "generation": 3,
            "accept_event": threading.Event(),
            "accepted": False,
        }
        app._active_reload_job = closing_job
        app._closing = True
        app._accept_reload_on_main(closing_job, model)
        self.assertTrue(closing_job["accept_event"].is_set())
        self.assertEqual(attached, [])

    def test_accepted_generation_copies_actual_runtime_state(self):
        model = _FakeModel()
        model._offload_runtime_state = {
            "mode": "partial", "n_gpu_layers": 21, "total_layers": 42,
        }
        service = types.SimpleNamespace(attach_llm=lambda _model: None)
        monitor = types.SimpleNamespace(llm_uses_gpu=None)
        app = ChatApp.__new__(ChatApp)
        app._llm_load_generation = 3
        app._closing = False
        app._llm_loading = True
        app._llm_offload_state = None
        app._deps = types.SimpleNamespace(res_monitor=monitor)
        app._ctrl = types.SimpleNamespace(
            _llm_service=service,
            clear_token_cache=lambda: None,
        )
        app._update_status = lambda: None
        app._start_whisper_after_llm_once = lambda: None
        app._on_llm_ready(3, model, None)
        self.assertEqual(app._llm_offload_state["n_gpu_layers"], 21)
        self.assertIsNot(app._llm_offload_state, model._offload_runtime_state)
        self.assertTrue(monitor.llm_uses_gpu)

    def test_stale_generation_does_not_replace_runtime_state(self):
        model = _FakeModel()
        model._offload_runtime_state = {
            "mode": "cpu", "n_gpu_layers": 0, "total_layers": 42,
        }
        old_state = {"mode": "full", "n_gpu_layers": -1, "total_layers": 42}
        app = ChatApp.__new__(ChatApp)
        app._llm_load_generation = 4
        app._closing = False
        app._llm_offload_state = old_state
        with patch.object(LLM_Local_Chat, "_release_cuda_resources"):
            app._on_llm_ready(3, model, None)
        self.assertIs(app._llm_offload_state, old_state)
        self.assertEqual(model.closed, 1)


if __name__ == "__main__":
    unittest.main()
