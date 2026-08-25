import sys
import threading
import types
import unittest
from unittest.mock import patch

from resource_monitor import (
    WhisperController,
    WhisperPool,
    adjust_inference,
    adjust_llm,
    build_partial_layer_candidates,
    normalize_llm_gpu_offload_mode,
    normalize_whisper_mode,
    read_gguf_total_layers,
    required_vram_mb_for_layers,
    select_llm_offload,
    select_whisper_profile,
)


class FakeMonitor:
    def __init__(self, total=8192, used=0, *, llm_uses_gpu=True):
        self.total = total
        self.used = used
        self.llm_uses_gpu = llm_uses_gpu
        self.gpu_pct = 0.0
        self.vram_total_mb = total
        self.vram_instant_mb = float(used)

    def snapshot(self):
        free = max(0, self.total - self.used)
        return {
            "available": self.total > 0,
            "total_mb": self.total,
            "used_mb": self.used,
            "free_mb": free,
            "used_ratio": self.used / self.total if self.total else 0.0,
        }


class SequencedMonitor(FakeMonitor):
    def __init__(self, snapshots):
        super().__init__()
        self._snapshots = iter(snapshots)

    def snapshot(self):
        total, used = next(self._snapshots)
        self.total, self.used = total, used
        return super().snapshot()


class ResourceDecisionTests(unittest.TestCase):
    def test_six_gb_used_on_eight_gb_is_allowed(self):
        result = adjust_inference(FakeMonitor(8192, 6000), 1024)
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_tokens"], 1024)

    def test_seven_gb_used_is_allowed_with_soft_reduction(self):
        result = adjust_inference(FakeMonitor(8192, 7000), 1024)
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_tokens"], 512)

    def test_blocks_only_below_dynamic_hard_reserve(self):
        result = adjust_inference(FakeMonitor(8192, 7700), 1024)
        self.assertFalse(result["ok"])
        self.assertIn("free<", result["reason"])

    def test_quarter_limit_below_one_gb_free(self):
        result = adjust_inference(FakeMonitor(8192, 7300), 1024)
        self.assertEqual(result["max_tokens"], 256)

    def test_cpu_inference_is_not_blocked_by_gpu_usage(self):
        result = adjust_inference(
            FakeMonitor(8192, 8100, llm_uses_gpu=False), 1024)
        self.assertTrue(result["ok"])
        self.assertEqual(result["max_tokens"], 1024)

    def test_llm_uses_gpu_when_model_and_reserve_fit(self):
        with patch("resource_monitor.os.path.getsize", return_value=5 * 1024**3):
            result = adjust_llm(
                FakeMonitor(8192, 500), "model.gguf",
                gpu_offload_supported=True,
            )
        self.assertEqual(result["n_gpu_layers"], -1)

    def test_llm_falls_back_when_backend_is_not_cuda(self):
        result = adjust_llm(
            FakeMonitor(8192, 0), gpu_offload_supported=False)
        self.assertEqual(result["n_gpu_layers"], 0)

    def test_llm_falls_back_when_model_does_not_fit(self):
        with patch("resource_monitor.os.path.getsize", return_value=7 * 1024**3):
            result = adjust_llm(
                FakeMonitor(8192, 1000), "model.gguf",
                gpu_offload_supported=True,
            )
        self.assertEqual(result["n_gpu_layers"], 0)


class AdaptiveOffloadSelectorTests(unittest.TestCase):
    MODEL_MB = 5088
    RESERVE_MB = 1024

    def select(self, free_mb, total_layers=42, **overrides):
        values = dict(
            free_mb=free_mb,
            model_mb=self.MODEL_MB,
            reserve_mb=self.RESERVE_MB,
            total_layers=total_layers,
            gpu_offload_supported=True,
            gpu_memory_available=True,
        )
        values.update(overrides)
        return select_llm_offload(**values)

    def test_full_is_preserved_when_existing_requirement_fits(self):
        self.assertEqual(self.select(7000)["n_gpu_layers"], -1)

    def test_external_gpu_idle_load_selects_three_quarters(self):
        self.assertEqual(self.select(5976)["n_gpu_layers"], 32)

    def test_external_gpu_peak_load_selects_three_quarters(self):
        self.assertEqual(self.select(5771)["n_gpu_layers"], 32)

    def test_falls_to_half_at_three_quarter_boundary(self):
        required_75 = required_vram_mb_for_layers(
            self.MODEL_MB, self.RESERVE_MB, 32, 42)
        result = self.select(required_75 - 1)
        self.assertEqual(result["n_gpu_layers"], 21)

    def test_falls_to_quarter_at_half_boundary(self):
        required_50 = required_vram_mb_for_layers(
            self.MODEL_MB, self.RESERVE_MB, 21, 42)
        result = self.select(required_50 - 1)
        self.assertEqual(result["n_gpu_layers"], 11)

    def test_falls_to_cpu_below_quarter_boundary(self):
        required_25 = required_vram_mb_for_layers(
            self.MODEL_MB, self.RESERVE_MB, 11, 42)
        result = self.select(required_25 - 1)
        self.assertEqual(result["n_gpu_layers"], 0)

    def test_equal_partial_boundary_is_eligible(self):
        required_75 = required_vram_mb_for_layers(
            self.MODEL_MB, self.RESERVE_MB, 32, 42)
        self.assertEqual(self.select(required_75)["n_gpu_layers"], 32)

    def test_cuda_unavailable_always_selects_cpu(self):
        result = self.select(7000, gpu_offload_supported=False)
        self.assertEqual(result["n_gpu_layers"], 0)

    def test_gpu_snapshot_unavailable_always_selects_cpu(self):
        result = self.select(7000, gpu_memory_available=False)
        self.assertEqual(result["n_gpu_layers"], 0)

    def test_unknown_metadata_preserves_only_safe_legacy_choices(self):
        self.assertEqual(self.select(7000, total_layers=None)["n_gpu_layers"], -1)
        self.assertEqual(self.select(5976, total_layers=None)["n_gpu_layers"], 0)

    def test_unknown_model_size_never_selects_gpu(self):
        result = self.select(7000, model_mb=0)
        self.assertEqual(result["n_gpu_layers"], 0)

    def test_manual_modes_and_full_safety_fallback_use_dynamic_layers(self):
        self.assertEqual(self.select(7000, offload_mode="full")["n_gpu_layers"], -1)
        self.assertEqual(self.select(5976, offload_mode="full")["n_gpu_layers"], 32)
        self.assertEqual(self.select(7000, offload_mode="75")["n_gpu_layers"], 32)
        self.assertEqual(self.select(7000, offload_mode="50")["n_gpu_layers"], 21)
        self.assertEqual(self.select(7000, offload_mode="25")["n_gpu_layers"], 11)
        self.assertEqual(self.select(7000, offload_mode="cpu")["n_gpu_layers"], 0)

    def test_auto_downshift_never_reselects_current_or_higher_layer(self):
        cases = ((-1, 32), (32, 21), (21, 11), (11, 0), (0, 0))
        for current, expected in cases:
            with self.subTest(current=current):
                result = self.select(
                    7000,
                    offload_mode="auto",
                    downshift_from_layers=current,
                )
                self.assertEqual(result["n_gpu_layers"], expected)

    def test_unknown_metadata_downshift_and_manual_partial_fail_closed_to_cpu(self):
        self.assertEqual(
            self.select(
                7000, total_layers=None,
                offload_mode="auto", downshift_from_layers=-1,
            )["n_gpu_layers"],
            0,
        )
        self.assertEqual(
            self.select(7000, total_layers=None, offload_mode="75")[
                "n_gpu_layers"
            ],
            0,
        )

    def test_invalid_saved_offload_mode_normalizes_to_auto(self):
        for value in (None, "", "100", True, 1):
            with self.subTest(value=value):
                self.assertEqual(normalize_llm_gpu_offload_mode(value), "auto")

    def test_layer_candidates_are_dynamic_half_up_and_deduplicated(self):
        expected = {
            32: [24, 16, 8],
            40: [30, 20, 10],
            42: [32, 21, 11],
            80: [60, 40, 20],
            1: [],
            2: [1],
            3: [2, 1],
        }
        for total, layers in expected.items():
            with self.subTest(total=total):
                actual = [
                    item["n_gpu_layers"]
                    for item in build_partial_layer_candidates(total)
                ]
                self.assertEqual(actual, layers)
                self.assertEqual(actual, sorted(set(actual), reverse=True))
                self.assertTrue(all(0 < layer < total for layer in actual))

    def test_invalid_layer_counts_produce_no_partial_candidates(self):
        for total in (None, True, 0, -1, 4097):
            with self.subTest(total=total):
                self.assertEqual(build_partial_layer_candidates(total), [])

    def test_partial_required_uses_ceiling(self):
        self.assertEqual(
            required_vram_mb_for_layers(5088, 1024, 11, 42),
            2357,
        )


class GgufMetadataTests(unittest.TestCase):
    @staticmethod
    def _fake_package(metadata=None, metadata_error=None, close_error=None):
        instances = []

        class FakeModel:
            def __init__(self, *, path_model, params, verbose):
                self.params = params
                self.closed = False
                instances.append(self)

            def metadata(self):
                if metadata_error:
                    raise metadata_error
                return metadata

            def close(self):
                self.closed = True
                if close_error:
                    raise close_error

        params = types.SimpleNamespace(
            vocab_only=False,
            n_gpu_layers=-1,
            use_mmap=False,
        )
        low_level = types.SimpleNamespace(
            llama_model_default_params=lambda: params,
        )
        internals = types.SimpleNamespace(LlamaModel=FakeModel)
        package = types.SimpleNamespace(
            _internals=internals,
            llama_cpp=low_level,
        )
        return package, instances, params

    def test_reads_architecture_specific_block_count_and_closes(self):
        package, instances, params = self._fake_package({
            "general.architecture": "gemma4",
            "gemma4.block_count": "42",
        })
        with patch.dict(sys.modules, {"llama_cpp": package}):
            result = read_gguf_total_layers("model.gguf")
        self.assertEqual(result, 42)
        self.assertTrue(instances[0].closed)
        self.assertTrue(params.vocab_only)
        self.assertEqual(params.n_gpu_layers, 0)
        self.assertTrue(params.use_mmap)

    def test_does_not_rescue_invalid_declared_architecture_with_other_key(self):
        package, instances, _params = self._fake_package({
            "general.architecture": "gemma4",
            "gemma4.block_count": "invalid",
            "llama.block_count": "32",
        })
        with patch.dict(sys.modules, {"llama_cpp": package}):
            result = read_gguf_total_layers("model.gguf")
        self.assertIsNone(result)
        self.assertTrue(instances[0].closed)

    def test_architecture_missing_allows_only_one_valid_block_count(self):
        package, _instances, _params = self._fake_package({
            "gemma4.block_count": "42",
        })
        with patch.dict(sys.modules, {"llama_cpp": package}):
            self.assertEqual(read_gguf_total_layers("model.gguf"), 42)
        package, _instances, _params = self._fake_package({
            "gemma4.block_count": "42",
            "llama.block_count": "32",
        })
        with patch.dict(sys.modules, {"llama_cpp": package}):
            self.assertIsNone(read_gguf_total_layers("model.gguf"))

    def test_invalid_metadata_values_are_rejected(self):
        for value in ("0", "-1", "+42", "42.0", "4097", 42):
            package, _instances, _params = self._fake_package({
                "general.architecture": "gemma4",
                "gemma4.block_count": value,
            })
            with self.subTest(value=value), patch.dict(
                sys.modules, {"llama_cpp": package}
            ):
                self.assertIsNone(read_gguf_total_layers("model.gguf"))

    def test_metadata_and_close_failures_return_none_and_attempt_close(self):
        package, instances, _params = self._fake_package(
            metadata_error=RuntimeError("broken metadata"))
        with patch.dict(sys.modules, {"llama_cpp": package}):
            self.assertIsNone(read_gguf_total_layers("model.gguf"))
        self.assertTrue(instances[0].closed)

        package, instances, _params = self._fake_package(
            {"general.architecture": "gemma4", "gemma4.block_count": "42"},
            close_error=RuntimeError("close failed"),
        )
        with patch.dict(sys.modules, {"llama_cpp": package}):
            self.assertIsNone(read_gguf_total_layers("model.gguf"))
        self.assertTrue(instances[0].closed)


class WhisperPolicyTests(unittest.TestCase):
    @staticmethod
    def _whisper_module(calls):
        module = types.SimpleNamespace()

        def load_model(name, device):
            calls.append((name, device))
            return object()

        module.load_model = load_model
        return module

    def test_auto_loads_gpu_small_when_between_two_and_four_gb_is_free(self):
        calls = []
        pool = WhisperPool()
        with patch.dict(sys.modules, {"whisper": self._whisper_module(calls)}):
            pool.load(FakeMonitor(8192, 5000))
        self.assertEqual(calls, [("small", "cpu"), ("small", "cuda")])
        self.assertTrue(pool._ctrl.gpu_available)
        self.assertEqual(pool.loaded_profile, "gpu_small")

    def test_loads_gpu_medium_when_headroom_is_sufficient(self):
        calls = []
        pool = WhisperPool()
        monitor = SequencedMonitor([(12288, 4000), (12288, 7000)])
        with patch.dict(sys.modules, {"whisper": self._whisper_module(calls)}):
            pool.load(monitor)
        self.assertEqual(calls, [("small", "cpu"), ("medium", "cuda")])
        self.assertTrue(pool._ctrl.gpu_available)

    def test_auto_uses_cpu_when_less_than_two_gb_is_free(self):
        calls = []
        pool = WhisperPool()
        with patch.dict(sys.modules, {"whisper": self._whisper_module(calls)}):
            pool.load(FakeMonitor(8192, 6300))
        self.assertEqual(calls, [("small", "cpu")])
        self.assertEqual(pool.loaded_profile, "cpu_small")

    def test_manual_gpu_modes_do_not_bypass_safety_thresholds(self):
        snap = FakeMonitor(8192, 5000).snapshot()
        self.assertEqual(
            select_whisper_profile("gpu_medium", snap)[0], "cpu_small")
        self.assertEqual(
            select_whisper_profile("gpu_small", snap)[0], "gpu_small")

    def test_cpu_small_mode_never_loads_cuda_model(self):
        calls = []
        pool = WhisperPool()
        with patch.dict(sys.modules, {"whisper": self._whisper_module(calls)}):
            pool.load(FakeMonitor(12288, 1000), mode="cpu_small")
        self.assertEqual(calls, [("small", "cpu")])

    def test_gpu_model_is_released_if_post_load_reserve_is_too_low(self):
        calls = []
        pool = WhisperPool()
        monitor = SequencedMonitor([(8192, 5900), (8192, 7800)])
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                is_available=lambda: False,
                empty_cache=lambda: None,
            )
        )
        with (
            patch.dict(sys.modules, {
                "whisper": self._whisper_module(calls),
                "torch": fake_torch,
            }),
            patch("resource_monitor.gc.collect"),
        ):
            pool.load(monitor, mode="gpu_small")
        self.assertIsNone(pool._gpu_model)
        self.assertEqual(pool.loaded_profile, "cpu_small")

    def test_gpu_load_failure_falls_back_to_cpu_small(self):
        calls = []
        module = types.SimpleNamespace()

        def load_model(name, device):
            calls.append((name, device))
            if device == "cuda":
                raise RuntimeError("CUDA out of memory")
            return object()

        module.load_model = load_model
        pool = WhisperPool()
        fake_torch = types.SimpleNamespace(
            cuda=types.SimpleNamespace(
                is_available=lambda: False,
                empty_cache=lambda: None,
            )
        )
        with (
            patch.dict(sys.modules, {"whisper": module, "torch": fake_torch}),
            patch("resource_monitor.gc.collect"),
        ):
            pool.load(FakeMonitor(8192, 5000), mode="gpu_small")
        self.assertEqual(pool.loaded_profile, "cpu_small")
        self.assertFalse(pool._ctrl.gpu_available)

    def test_invalid_mode_normalizes_to_auto(self):
        for value in (None, "", "small", True, 1):
            with self.subTest(value=value):
                self.assertEqual(normalize_whisper_mode(value), "auto")

    def test_controller_cannot_recover_to_unloaded_gpu_model(self):
        ctrl = WhisperController()
        monitor = FakeMonitor()
        monitor.gpu_pct = 10
        self.assertEqual(ctrl.update(monitor), "cpu")
        self.assertFalse(ctrl.uses_gpu())

    def test_controller_hysteresis_remains_for_loaded_gpu_model(self):
        ctrl = WhisperController()
        ctrl.gpu_available = True
        ctrl.state = "gpu"
        monitor = FakeMonitor()
        monitor.gpu_pct = 90
        self.assertEqual(ctrl.update(monitor), "cpu")
        monitor.gpu_pct = 60
        self.assertEqual(ctrl.update(monitor), "gpu")

    def test_gpu_delta_is_consumed_only_once(self):
        ctrl = WhisperController()
        monitor = FakeMonitor()
        monitor.gpu_pct = 25
        ctrl.update(monitor)

        self.assertEqual(ctrl.consume_delta_gpu_pct(), 25)
        self.assertEqual(ctrl.consume_delta_gpu_pct(), 0.0)

    def test_reload_pause_waits_for_transcribe_and_blocks_new_transcribe(self):
        entered = threading.Event()
        release = threading.Event()

        class Model:
            def transcribe(self, _audio, **_kwargs):
                entered.set()
                release.wait(1)
                return {"text": "ok"}

        pool = WhisperPool()
        pool._cpu_model = Model()
        monitor = FakeMonitor()
        result = []
        worker = threading.Thread(
            target=lambda: result.append(
                pool.transcribe_guarded(monitor, b"audio")),
            daemon=True,
        )
        worker.start()
        self.assertTrue(entered.wait(1))
        pool.request_reload_pause()
        self.assertIsNone(pool.transcribe_guarded(monitor, b"new"))

        acquired = []
        waiter = threading.Thread(
            target=lambda: acquired.append(pool.begin_llm_reload(timeout=1)),
            daemon=True,
        )
        waiter.start()
        self.assertFalse(acquired)
        release.set()
        worker.join(1)
        waiter.join(1)
        self.assertEqual(acquired, [True])
        pool.end_llm_reload()
        self.assertEqual(result[0][0]["text"], "ok")


if __name__ == "__main__":
    unittest.main()
