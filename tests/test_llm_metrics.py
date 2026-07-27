import threading
import unittest

from llm_service import LLMService, count_generated_tokens


class _Tokenizer:
    def __init__(self):
        self.calls = []

    def tokenize(self, value, add_bos=False):
        self.calls.append((value, add_bos))
        return [10, 20, 30]


class LlmMetricTests(unittest.TestCase):
    def test_generated_token_count_uses_model_tokenizer(self):
        llm = _Tokenizer()
        self.assertEqual(count_generated_tokens(llm, "生成本文"), 3)
        self.assertEqual(llm.calls, [("生成本文".encode("utf-8"), False)])

    def test_empty_text_has_zero_tokens_without_tokenizer_call(self):
        llm = _Tokenizer()
        self.assertEqual(count_generated_tokens(llm, ""), 0)
        self.assertEqual(llm.calls, [])

    def test_reset_failure_reports_original_error_without_unbound_reply(self):
        class _ResetFailure:
            def reset(self):
                raise RuntimeError("reset failed")

            def tokenize(self, *_args, **_kwargs):
                raise AssertionError("empty reply must not be tokenized")

        completed = threading.Event()
        errors = []
        service = LLMService(_ResetFailure())
        self.assertTrue(service.generate(
            [], 10, 0.1,
            on_token=lambda _token: None,
            on_done=lambda _reply: completed.set(),
            on_error=lambda error: (errors.append(error), completed.set()),
        ))

        self.assertTrue(completed.wait(1))
        self.assertEqual(str(errors[0]), "reset failed")
        self.assertFalse(service.is_running())


if __name__ == "__main__":
    unittest.main()
