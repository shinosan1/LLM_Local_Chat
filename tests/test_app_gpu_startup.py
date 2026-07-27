import unittest
from unittest.mock import patch

import LLM_Local_Chat
from LLM_Local_Chat import ChatApp, init_llm


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

        class Monitor:
            llm_uses_gpu = None

            @staticmethod
            def snapshot():
                return {
                    "available": True, "total_mb": 8192,
                    "used_mb": 1000, "free_mb": 7192,
                    "used_ratio": 1000 / 8192,
                }

        def fake_llama(**kwargs):
            calls.append(kwargs["n_gpu_layers"])
            if kwargs["n_gpu_layers"] == -1:
                raise RuntimeError("CUDA out of memory")
            return object()

        decision = {
            "n_gpu_layers": -1,
            "snapshot": Monitor.snapshot(),
        }
        with (
            patch.object(LLM_Local_Chat, "adjust_llm", return_value=decision),
            patch.object(LLM_Local_Chat, "Llama", side_effect=fake_llama),
        ):
            model = init_llm(__file__, 1024, Monitor())

        self.assertIsNotNone(model)
        self.assertEqual(calls[-1], 0)


if __name__ == "__main__":
    unittest.main()
