import sys
import types
import unittest
from unittest.mock import patch

from resource_monitor import (
    WhisperController,
    WhisperPool,
    adjust_inference,
    adjust_llm,
    normalize_whisper_mode,
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


if __name__ == "__main__":
    unittest.main()
