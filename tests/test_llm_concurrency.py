import threading
import time
import unittest
from unittest.mock import patch

import controller as controller_module
from controller import (
    Controller,
    ControllerDeps,
    LeadingJsonFilter,
    TokenCostCache,
)
from llm_service import LLMService


class _Value:
    def __init__(self, value=None):
        self.value = value

    def set(self, value):
        self.value = value


class _Widget:
    def __init__(self):
        self.state = None
        self.text = ""
        self.insertions = []

    def config(self, **kwargs):
        self.state = kwargs.get("state", self.state)

    def focus_set(self):
        pass

    def insert(self, _where, text, *_args):
        self.insertions.append(text)
        self.text += text

    def get(self, *_args):
        return self.text

    def delete(self, *_args):
        self.text = ""

    def index(self, _where):
        return "1.0"


class _Root:
    def after(self, _delay, callback):
        callback()


class _TTS:
    def __init__(self):
        self.spoken = []
        self.stream_ended = 0

    def speak(self, _text):
        self.spoken.append(_text)

    def end_stream(self):
        self.stream_ended += 1

    def begin_stream(self):
        pass

    def stop_all(self):
        pass


class _Avatar:
    def __init__(self):
        self.win = _Root()

    def stop_speaking(self):
        pass


class _ResourceMonitor:
    pass


class _WhisperControl:
    delta_gpu_pct = 0


class _WhisperPool:
    _ctrl = _WhisperControl()


class _App:
    SYSTEM_PROMPT = "system"

    def __init__(self):
        self.llm = object()
        self.root = _Root()
        self._is_thinking = False
        self._llm_abort = False
        self._btn_send = _Widget()
        self._entry = _Widget()
        self._stream_buf = ""
        self._stream_mark = None
        self._chat_text = _Widget()
        self._current_session = {"title": "新しいチャット", "history": []}
        self._title_var = _Value()
        self._kakeibo_pending_text = None
        self._health_pending_text = None
        self.tts = _TTS()
        self.avatar = _Avatar()
        self._voice = None
        self._vad_thresh = 100
        self._kakeibo_mode = False
        self._health_mode = False
        self._n_ctx = 1024
        self._max_tokens = 100
        self._temperature = 0.3
        self._history_budget_ratio = 0.5
        self._system_buf_tokens = 100
        self._count_tokens = lambda *_args: 1
        self.writes = []
        self.saved = 0
        self.biolog_records = []
        self.biolog_explicit_fields = []
        self.kakeibo_confirm_calls = []

    def _update_status(self):
        pass

    def _chat_write(self, text, tag):
        self.writes.append((text, tag))

    def _save_now(self):
        self.saved += 1

    def _append_stream_token(self, token):
        self._stream_buf += token

    def _confirm_and_send_kakeibo(self, candidates):
        # 複数取引対応では候補リストを1引数で受け取り、1件ずつ確認・送信する。
        self.kakeibo_confirm_calls.append(list(candidates))

    def _confirm_and_send_biolog(self, record, explicit_fields=None):
        self.biolog_records.append(record)
        self.biolog_explicit_fields.append(
            frozenset(explicit_fields or ())
        )

    def _build_summary_prompt(self):
        return "summary prompt"

    def _apply_summary(self, summary):
        self._current_session["summary"] = summary


def _controller(app, threshold=99):
    return Controller(
        app,
        ControllerDeps(
            res_monitor=_ResourceMonitor(),
            whisper_pool=_WhisperPool(),
            summary_threshold=threshold,
        ),
    )


class _BlockingLLM:
    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()

    def reset(self):
        pass

    def create_chat_completion(self, **_kwargs):
        self.entered.set()
        self.release.wait(2)
        yield {"choices": [{"delta": {"content": "ok"}}]}

    def __call__(self, *_args, **_kwargs):
        self.entered.set()
        self.release.wait(2)
        return {"choices": [{"text": "summary"}]}


class _FailingSummaryLLM:
    def reset(self):
        pass

    def __call__(self, *_args, **_kwargs):
        raise RuntimeError("summary failed")


class _BlockingHealthLLM:
    def __init__(self, result):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.result = result

    def reset(self):
        pass

    def __call__(self, *_args, **_kwargs):
        self.entered.set()
        self.release.wait(2)
        return {
            "choices": [{
                "text": self.result,
                "finish_reason": "stop",
            }]
        }


def _wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class LLMServiceConcurrencyTests(unittest.TestCase):
    def test_summary_cannot_start_while_generation_is_running(self):
        llm = _BlockingLLM()
        service = LLMService(llm)
        done = threading.Event()

        self.assertTrue(service.generate(
            messages=[], max_tokens=1, temperature=0,
            on_token=lambda _token: None,
            on_done=lambda _reply: done.set(),
            on_error=lambda _error: done.set(),
        ))
        self.assertTrue(llm.entered.wait(1))
        self.assertTrue(service.is_running())
        self.assertFalse(service.summarize("prompt", lambda _s: None, lambda _e: None))

        llm.release.set()
        self.assertTrue(done.wait(1))
        self.assertFalse(service.is_running())

    def test_health_extraction_cannot_start_during_generation(self):
        llm = _BlockingLLM()
        service = LLMService(llm)
        done = threading.Event()

        self.assertTrue(service.generate(
            messages=[], max_tokens=1, temperature=0,
            on_token=lambda _token: None,
            on_done=lambda _reply: done.set(),
            on_error=lambda _error: done.set(),
        ))
        self.assertTrue(llm.entered.wait(1))
        self.assertFalse(service.extract_health(
            "prompt", 100, lambda _r: None, lambda _e: None
        ))

        llm.release.set()
        self.assertTrue(done.wait(1))


class LeadingJsonFilterTests(unittest.TestCase):
    def test_fenced_json_is_hidden_across_token_boundaries(self):
        stream_filter = LeadingJsonFilter()
        output = "".join(
            stream_filter.feed(chunk)
            for chunk in ("`", "``json\n{\"body_fat\":18}", "\n```", "\n正常です。")
        )
        self.assertEqual(output, "正常です。")

    def test_bare_json_is_hidden_and_natural_text_is_preserved(self):
        stream_filter = LeadingJsonFilter()
        output = stream_filter.feed('{"body_fat":')
        output += stream_filter.feed('18}\n記録します。')
        self.assertEqual(output, "記録します。")

    def test_incomplete_json_is_discarded(self):
        stream_filter = LeadingJsonFilter()
        self.assertEqual(stream_filter.feed('```json\n{"body_fat":'), "")
        self.assertEqual(stream_filter.finalize(), "")

    def test_natural_text_start_switches_to_passthrough(self):
        stream_filter = LeadingJsonFilter()
        self.assertEqual(stream_filter.feed("自然な"), "自然な")
        self.assertEqual(stream_filter.feed("回答です。"), "回答です。")


class ControllerGenerationTests(unittest.TestCase):
    def test_health_stream_pipeline_hides_json_from_display_and_tts(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"

        for token in ('```json\n{"body_fat":18}\n```', '\n健康的な範囲です。'):
            ctrl._on_stream_token(token, generation)

        self.assertEqual(app._stream_buf, "健康的な範囲です。")
        self.assertEqual(app.tts.spoken, ["健康的な範囲です。"])

    def test_default_stream_pipeline_keeps_existing_behavior(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")

        ctrl._on_stream_token("通常回答です。", generation)

        self.assertEqual(app._stream_buf, "通常回答です。")
        self.assertEqual(app.tts.spoken, ["通常回答です。"])

    def test_kakeibo_stream_pipeline_hides_llm_text_and_tts(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"

        ctrl._on_stream_token("かしこまりました。家計簿へ登録しますね。", generation)

        self.assertEqual(app._stream_buf, "")
        self.assertEqual(app.tts.spoken, [])

    def test_complete_health_json_skips_retry_and_saves_natural_text(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "体脂肪率18%"
        reply = '```json\n{"body_fat":18}\n```\n記録します。'
        ctrl._on_stream_token(reply, generation)

        ctrl._on_llm_done("体脂肪率18%", reply, generation)

        self.assertEqual(app.biolog_records, [{"body_fat": 18}])
        self.assertEqual(
            app._current_session["history"][0]["assistant"], "記録します。"
        )
        self.assertNotIn("body_fat", app._stream_buf)
        self.assertFalse(ctrl.is_busy())

    def test_health_confirmation_receives_memo(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "メモ テストデータ入力"
        reply = '```json\n{"memo":"テストデータ入力"}\n```\n記録します。'
        ctrl._on_stream_token(reply, generation)

        ctrl._on_llm_done("メモ テストデータ入力", reply, generation)

        self.assertEqual(app.biolog_records, [{"memo": "テストデータ入力"}])
        self.assertEqual(app.biolog_explicit_fields, [frozenset({"memo"})])

    def test_initial_health_result_drops_implicit_measurement_memo(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        user_text = "体脂肪率17.9%"
        app._health_pending_text = user_text
        reply = '{"body_fat":17.9,"memo":"体脂肪率17.9%"}'

        ctrl._on_llm_done(user_text, reply, generation)

        self.assertEqual(app.biolog_records, [{"body_fat": 17.9}])
        self.assertEqual(app.biolog_explicit_fields, [frozenset()])
        self.assertFalse(ctrl.is_busy())

    def test_explicit_health_fields_override_partial_llm_result(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        user_text = (
            "食事ログ　コーヒー　水　"
            "メモ　テストデータは食事ログを追加"
        )
        app._health_pending_text = user_text
        reply = '{"meal_detail":"コーヒー","memo":"テストデータ"}'

        ctrl._on_llm_done(user_text, reply, generation)

        self.assertEqual(app.biolog_records, [{
            "meal_detail": "コーヒー　水",
            "memo": "テストデータは食事ログを追加",
        }])
        self.assertEqual(app.biolog_explicit_fields, [frozenset({
            "meal_detail", "memo",
        })])
        self.assertFalse(ctrl.is_busy())

    def test_explicit_text_fields_avoid_retry_for_all_null_json(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        user_text = "食事ログ コーヒー 水 メモ テストデータ"
        app._health_pending_text = user_text

        ctrl._on_llm_done(
            user_text,
            '{"meal_detail":null,"activity_log":null,"memo":null}',
            generation,
        )

        self.assertEqual(app.biolog_records, [{
            "meal_detail": "コーヒー 水",
            "activity_log": None,
            "memo": "テストデータ",
        }])
        self.assertFalse(ctrl._llm_service.is_running())
        self.assertFalse(ctrl.is_busy())

    def test_health_json_is_removed_from_history_if_model_puts_it_last(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "体脂肪率18%"
        reply = '記録します。\n```json\n{"body_fat":18}\n```'
        ctrl._on_stream_token(reply, generation)

        ctrl._on_llm_done("体脂肪率18%", reply, generation)

        self.assertEqual(
            app._current_session["history"][0]["assistant"], "記録します。"
        )

    def test_retry_guard_block_is_fail_closed_without_service_thread(self):
        app = _App()
        ctrl = _controller(app)
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": False, "reason": "test"
        }
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "体脂肪率18%"

        ctrl._on_llm_done("体脂肪率18%", "broken", generation)

        self.assertFalse(ctrl._llm_service.is_running())
        self.assertFalse(ctrl.is_busy())
        self.assertEqual(app.biolog_records, [])
        self.assertTrue(any("健康データを抽出できません" in w[0] for w in app.writes))

    def test_failed_initial_json_retries_and_keeps_busy_until_success(self):
        app = _App()
        ctrl = _controller(app)
        llm = _BlockingHealthLLM('{"body_fat":18}')
        ctrl._llm_service.llm = llm
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": True, "max_tokens": 256
        }
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "体脂肪率18%"

        ctrl._on_llm_done(
            "体脂肪率18%", '```json\n{"body_fat":', generation
        )

        self.assertTrue(llm.entered.wait(1))
        self.assertEqual(ctrl._operation_state, "extracting_health")
        self.assertEqual(app._btn_send.state, "disabled")
        self.assertEqual(app._current_session["history"], [])

        llm.release.set()
        self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
        self.assertEqual(app.biolog_records, [{"body_fat": 18}])
        self.assertEqual(
            app._current_session["history"][0]["assistant"],
            "健康記録を抽出しました。",
        )

    def test_all_null_initial_json_retries_until_text_values_are_extracted(self):
        app = _App()
        ctrl = _controller(app)
        llm = _BlockingHealthLLM(
            '{"meal_detail":"コーヒー 水","memo":"テストデータ"}'
        )
        ctrl._llm_service.llm = llm
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": True, "max_tokens": 256
        }
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "コーヒーと水を飲んだ。備考はテストデータ"

        ctrl._on_llm_done(
            app._health_pending_text,
            '{"meal_detail":null,"activity_log":null,"memo":null}',
            generation,
        )

        self.assertTrue(llm.entered.wait(1))
        self.assertEqual(ctrl._operation_state, "extracting_health")
        self.assertEqual(app.biolog_records, [])
        llm.release.set()

        self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
        self.assertEqual(app.biolog_records, [{
            "meal_detail": "コーヒー 水",
            "memo": "テストデータ",
        }])

    def test_health_retry_drops_implicit_measurement_memo(self):
        app = _App()
        ctrl = _controller(app)
        llm = _BlockingHealthLLM(
            '{"body_fat":17.9,"memo":"体脂肪率17.9%"}'
        )
        ctrl._llm_service.llm = llm
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": True, "max_tokens": 256
        }
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "体脂肪率17.9%"

        ctrl._on_llm_done("体脂肪率17.9%", "broken", generation)
        self.assertTrue(llm.entered.wait(1))
        llm.release.set()

        self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
        self.assertEqual(app.biolog_records, [{"body_fat": 17.9}])

    def test_retry_failure_is_fail_closed(self):
        app = _App()
        ctrl = _controller(app)
        llm = _BlockingHealthLLM("not json")
        ctrl._llm_service.llm = llm
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": True, "max_tokens": 256
        }
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "体脂肪率18%"

        ctrl._on_llm_done("体脂肪率18%", "broken", generation)
        self.assertTrue(llm.entered.wait(1))
        llm.release.set()

        self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
        self.assertEqual(app.biolog_records, [])
        self.assertTrue(any("健康データを抽出できません" in w[0] for w in app.writes))

    def test_all_null_retry_result_is_fail_closed(self):
        app = _App()
        ctrl = _controller(app)
        llm = _BlockingHealthLLM(
            '{"meal_detail":null,"activity_log":null,"memo":null}'
        )
        ctrl._llm_service.llm = llm
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": True, "max_tokens": 256
        }
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "コーヒーと水を摂取した"

        ctrl._on_llm_done(
            app._health_pending_text,
            '{"meal_detail":null,"activity_log":null,"memo":null}',
            generation,
        )
        self.assertTrue(llm.entered.wait(1))
        llm.release.set()

        self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
        self.assertEqual(app.biolog_records, [])
        self.assertTrue(any(
            "健康データを抽出できません" in write[0]
            for write in app.writes
        ))

    def test_stop_during_health_retry_discards_delayed_result(self):
        app = _App()
        ctrl = _controller(app)
        llm = _BlockingHealthLLM('{"body_fat":18}')
        ctrl._llm_service.llm = llm
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": True, "max_tokens": 256
        }
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._health_pending_text = "体脂肪率18%"
        ctrl._on_llm_done("体脂肪率18%", "broken", generation)
        self.assertTrue(llm.entered.wait(1))

        ctrl.stop()
        self.assertEqual(ctrl._operation_state, "stopping")
        self.assertEqual(app._btn_send.state, "disabled")
        llm.release.set()

        self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
        self.assertEqual(app.biolog_records, [])
        self.assertEqual(app._current_session["history"], [])

    def test_invalid_biolog_value_is_rejected_without_retry(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "health"
        app._stream_buf = '```json\n{"weight":999}\n```'

        ctrl._on_llm_done(
            "体重を記録",
            '```json\n{"weight":999}\n```',
            generation,
        )

        self.assertFalse(ctrl._llm_service.is_running())
        self.assertFalse(ctrl.is_busy())
        self.assertEqual(app.biolog_records, [])
        self.assertTrue(any("不正な形式" in text for text, _tag in app.writes))

    def test_done_stores_original_but_error_is_not_history(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._on_llm_done("健康の原文", "回答", generation)
        self.assertEqual(app._current_session["history"][0]["user"], "健康の原文")

        spoken_before_error = list(app.tts.spoken)
        generation = ctrl._begin_operation("generating")
        ctrl._on_llm_error("家計簿の原文", "[エラー]", generation)
        self.assertEqual(len(app._current_session["history"]), 1)
        self.assertEqual(app._entry.text, "家計簿の原文")
        self.assertEqual(app.tts.spoken, spoken_before_error)

    def test_old_done_cannot_modify_new_generation(self):
        app = _App()
        ctrl = _controller(app)
        old_generation = ctrl._begin_operation("generating")
        new_generation = ctrl._begin_operation("generating")

        ctrl._on_llm_done("古い入力", "古い回答", old_generation)

        self.assertEqual(app._current_session["history"], [])
        self.assertEqual(ctrl._operation_generation, new_generation)
        self.assertEqual(ctrl._operation_state, "generating")
        self.assertEqual(app._btn_send.state, "disabled")

    def test_generation_increments_for_send_summary_and_stop(self):
        app = _App()
        ctrl = _controller(app)
        first = ctrl._begin_operation("generating")
        second = ctrl._begin_operation("summarizing")
        third = ctrl._begin_operation("stopping")
        self.assertEqual((first, second, third), (1, 2, 3))

    def test_text_and_voice_are_rejected_for_every_busy_state(self):
        for state in ("generating", "extracting_health", "summarizing", "stopping"):
            with self.subTest(state=state):
                app = _App()
                app._entry.text = "draft"
                ctrl = _controller(app)
                ctrl._begin_operation(state)

                ctrl.handle_text()
                ctrl.handle_voice("voice")

                self.assertEqual(app._entry.text, "draft")
                self.assertEqual(app._entry.insertions, [])
                self.assertEqual(app._current_session["history"], [])

    def test_summary_success_and_failure_restore_idle_state(self):
        for llm, expected_summary in (
            (_BlockingLLM(), "summary"),
            (_FailingSummaryLLM(), None),
        ):
            with self.subTest(llm=type(llm).__name__):
                app = _App()
                ctrl = _controller(app)
                ctrl._llm_service.llm = llm

                ctrl._start_summary()
                if isinstance(llm, _BlockingLLM):
                    self.assertTrue(llm.entered.wait(1))
                    self.assertEqual(ctrl._operation_state, "summarizing")
                    self.assertEqual(app._btn_send.state, "disabled")
                    llm.release.set()

                self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
                self.assertEqual(ctrl._operation_state, "idle")
                self.assertEqual(app._btn_send.state, "normal")
                self.assertEqual(
                    app._current_session.get("summary"), expected_summary)

    def test_stop_during_summary_waits_and_discards_result(self):
        app = _App()
        ctrl = _controller(app)
        llm = _BlockingLLM()
        ctrl._llm_service.llm = llm
        ctrl._start_summary()
        self.assertTrue(llm.entered.wait(1))

        ctrl.stop()
        time.sleep(0.15)
        self.assertEqual(ctrl._operation_state, "stopping")
        self.assertEqual(app._btn_send.state, "disabled")
        self.assertNotIn("summary", app._current_session)

        llm.release.set()
        self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))
        self.assertEqual(app._btn_send.state, "normal")
        self.assertNotIn("summary", app._current_session)

    def test_stop_timeout_keeps_lock_then_recovers_after_worker_finishes(self):
        app = _App()
        ctrl = _controller(app)
        running = threading.Event()
        running.set()
        ctrl._llm_service.is_running = running.is_set
        ctrl._llm_service.abort = lambda: None

        with (
            patch.object(controller_module, "STOP_WAIT_TIMEOUT_SECONDS", 0.02),
            patch.object(controller_module, "STOP_WAIT_POLL_SECONDS", 0.005),
            patch.object(
                controller_module, "STOP_WAIT_TIMEOUT_POLL_SECONDS", 0.005
            ),
            patch.object(controller_module.messagebox, "showwarning") as warning,
        ):
            ctrl.stop()
            self.assertTrue(_wait_until(lambda: warning.call_count == 1))
            self.assertEqual(ctrl._operation_state, "stopping")
            self.assertEqual(app._btn_send.state, "disabled")
            self.assertEqual(warning.call_count, 1)

            time.sleep(0.03)
            self.assertEqual(warning.call_count, 1)
            running.clear()
            self.assertTrue(_wait_until(lambda: not ctrl.is_busy()))

        self.assertEqual(app._btn_send.state, "normal")

    def test_old_stop_timeout_cannot_notify_new_generation(self):
        app = _App()
        ctrl = _controller(app)
        running = threading.Event()
        running.set()
        ctrl._llm_service.is_running = running.is_set
        ctrl._llm_service.abort = lambda: None

        with (
            patch.object(controller_module, "STOP_WAIT_TIMEOUT_SECONDS", 0.02),
            patch.object(controller_module, "STOP_WAIT_POLL_SECONDS", 0.005),
            patch.object(
                controller_module, "STOP_WAIT_TIMEOUT_POLL_SECONDS", 0.005
            ),
            patch.object(controller_module.messagebox, "showwarning") as warning,
        ):
            ctrl.stop()
            ctrl._begin_operation("generating")
            time.sleep(0.05)
            running.clear()

        self.assertEqual(warning.call_count, 0)
        self.assertEqual(ctrl._operation_state, "generating")

    def test_guard_block_completes_without_service_thread(self):
        app = _App()
        app._entry.text = "guarded input"
        ctrl = _controller(app)
        ctrl._prompt_builder.build = lambda *_args, **_kwargs: []
        ctrl._resource_mgr.decide = lambda *_args, **_kwargs: {
            "ok": False, "reason": "test"
        }

        ctrl.handle_text()

        self.assertFalse(ctrl._llm_service.is_running())
        self.assertFalse(ctrl.is_busy())
        self.assertEqual(app._btn_send.state, "normal")
        self.assertEqual(app._current_session["history"], [])
        self.assertEqual(app._current_session["title"], "新しいチャット")
        self.assertEqual(app._entry.text, "guarded input")
        self.assertEqual(app.tts.spoken, [])
        self.assertEqual(app.tts.stream_ended, 1)

    def test_error_clears_pending_and_preserves_existing_draft(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        app._kakeibo_pending_text = "original"
        app._health_pending_text = "original"
        app._entry.text = "new draft"

        ctrl._on_llm_error("original", "[エラー]", generation)

        self.assertIsNone(app._kakeibo_pending_text)
        self.assertIsNone(app._health_pending_text)
        self.assertEqual(app._entry.text, "new draft")
        self.assertEqual(app._current_session["history"], [])

    def test_old_error_does_not_change_current_operation(self):
        app = _App()
        ctrl = _controller(app)
        old_generation = ctrl._begin_operation("generating")
        new_generation = ctrl._begin_operation("generating")

        ctrl._on_llm_error("old", "[エラー]", old_generation)

        self.assertEqual(ctrl._operation_generation, new_generation)
        self.assertEqual(ctrl._operation_state, "generating")
        self.assertEqual(app._entry.text, "")


class KakeiboControllerTests(unittest.TestCase):
    def test_user_text_passed_to_confirm_matches_actual_argument(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        # pendingフラグの中身と実際のuser_textが異なっていても、
        # 候補構築にはuser_textが使われることを確認する。
        app._kakeibo_pending_text = "pending placeholder"
        user_text = "セリアで雑貨を1870円買った"

        ctrl._on_llm_done(user_text, "了解しました。", generation)

        self.assertEqual(len(app.kakeibo_confirm_calls), 1)
        candidates = app.kakeibo_confirm_calls[0]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["source_text"], user_text)
        self.assertNotEqual(candidates[0]["source_text"], "pending placeholder")

    def test_broken_llm_json_still_opens_confirm_dialog_when_amount_found(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"
        user_text = "1000円使った"

        ctrl._on_llm_done(user_text, "壊れたJSON応答", generation)

        self.assertEqual(len(app.kakeibo_confirm_calls), 1)
        candidates = app.kakeibo_confirm_calls[0]
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["amount"], 1000)
        self.assertIsNone(candidates[0]["type"])

    def test_pending_is_cleared_after_normal_completion(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        ctrl._on_llm_done("1000円使った", "reply", generation)

        self.assertIsNone(app._kakeibo_pending_text)

    def _run_with_failing_candidate_builder(self, app, ctrl, generation):
        original = controller_module.build_kakeibo_candidates
        controller_module.build_kakeibo_candidates = lambda *_a, **_k: (
            _ for _ in ()).throw(RuntimeError("boom"))
        try:
            ctrl._on_llm_done("1000円使った", "reply", generation)
        finally:
            controller_module.build_kakeibo_candidates = original

    def test_pending_is_cleared_even_when_candidate_build_raises(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        self._run_with_failing_candidate_builder(app, ctrl, generation)

        self.assertIsNone(app._kakeibo_pending_text)

    def test_state_returns_to_idle_after_candidate_build_exception(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        self._run_with_failing_candidate_builder(app, ctrl, generation)

        self.assertFalse(ctrl.is_busy())
        self.assertEqual(app._btn_send.state, "normal")

    def test_no_confirm_call_when_candidate_build_raises(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        self._run_with_failing_candidate_builder(app, ctrl, generation)

        # 候補構築が例外を出した場合、確認画面呼び出しやAPI送信も発生しない
        self.assertEqual(app.kakeibo_confirm_calls, [])

    def test_error_message_is_shown_after_candidate_build_exception(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        self._run_with_failing_candidate_builder(app, ctrl, generation)

        self.assertTrue(any(tag == "err" for _text, tag in app.writes))

    def test_valid_kakeibo_input_does_not_display_llm_text(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        ctrl._on_llm_done(
            "1000円使った",
            "かしこまりました。家計簿へ登録しますね。とても素晴らしい心がけです。",
            generation,
        )

        self.assertEqual(app._stream_buf, "")
        self.assertEqual(len(app.kakeibo_confirm_calls), 1)

    def test_no_amount_kakeibo_input_shows_error_once_without_llm_text(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        ctrl._on_llm_done(
            "パンを買った",
            "何を買いましたか？よろしければ金額も教えてください。",
            generation,
        )

        self.assertEqual(app._stream_buf, "")
        err_writes = [text for text, tag in app.writes if tag == "err"]
        self.assertEqual(len(err_writes), 1)
        self.assertEqual(app.kakeibo_confirm_calls, [])

    def test_kakeibo_mode_off_still_shows_normal_response(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        # _active_mode は既定の "default" のまま、_kakeibo_pending_text も未設定

        # _finish_operation が完了時に _stream_buf をクリアするため、
        # 表示されたかどうかは _append_stream_token の呼び出し内容で検証する。
        displayed = []
        original_append = app._append_stream_token

        def _record_append(token):
            displayed.append(token)
            original_append(token)

        app._append_stream_token = _record_append

        ctrl._on_llm_done("こんにちは", "こんにちは！今日はどうしましたか？", generation)

        self.assertEqual(displayed, ["こんにちは！今日はどうしましたか？"])
        self.assertFalse(any(tag == "err" for _text, tag in app.writes))

    def test_kakeibo_success_does_not_append_to_existing_history(self):
        app = _App()
        app._current_session["history"] = [
            {"user": "前回の質問", "assistant": "前回の回答"}
        ]
        history_before = list(app._current_session["history"])
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        ctrl._on_llm_done(
            "1000円使った",
            'かしこまりました。\n```json\n{"amount": 1000, "type": "支出"}\n```',
            generation,
        )

        self.assertEqual(app._current_session["history"], history_before)
        self.assertEqual(len(app.kakeibo_confirm_calls), 1)

    def test_kakeibo_no_amount_error_does_not_append_to_existing_history(self):
        app = _App()
        app._current_session["history"] = [
            {"user": "前回の質問", "assistant": "前回の回答"}
        ]
        history_before = list(app._current_session["history"])
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        ctrl._on_llm_done("パンを買った", "何を買いましたか？", generation)

        self.assertEqual(app._current_session["history"], history_before)
        err_writes = [text for text, tag in app.writes if tag == "err"]
        self.assertEqual(len(err_writes), 1)

    def test_kakeibo_processing_does_not_change_title(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"
        title_before = app._current_session["title"]

        ctrl._on_llm_done("1000円使った", "かしこまりました。", generation)

        self.assertEqual(app._current_session["title"], title_before)

    def test_kakeibo_processing_does_not_trigger_summary(self):
        app = _App()
        ctrl = _controller(app, threshold=1)  # 閾値を低くして誘発しやすくする
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"
        summary_calls = []
        ctrl._start_summary = lambda: summary_calls.append(True)

        ctrl._on_llm_done("1000円使った", "かしこまりました。", generation)

        self.assertEqual(summary_calls, [])

    def test_kakeibo_processing_does_not_pollute_next_normal_prompt(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        ctrl._active_mode = "kakeibo"
        app._kakeibo_pending_text = "kakeibo"

        ctrl._on_llm_done(
            "セリアで雑貨を1870円買った",
            'かしこまりました。\n```json\n{"amount": 1870, "type": "支出"}\n```',
            generation,
        )

        messages = ctrl._prompt_builder.build(
            "こんにちは", app._current_session, mode="default")
        combined = str(messages)
        self.assertNotIn("セリアで雑貨を1870円買った", combined)
        self.assertNotIn("amount", combined)


class TokenCostCacheTests(unittest.TestCase):
    def test_evicts_least_recently_used_entry(self):
        cache = TokenCostCache(max_entries=2)
        cache["a"] = 1
        cache["b"] = 2
        self.assertEqual(cache.get("a"), 1)
        cache["c"] = 3

        self.assertIn("a", cache)
        self.assertIn("c", cache)
        self.assertNotIn("b", cache)


if __name__ == "__main__":
    unittest.main()


class KakeiboSequenceBusyTests(unittest.TestCase):
    """家計簿の確認・POSTシーケンス中は新しい入力を開始しない。"""

    def test_is_busy_reflects_kakeibo_sequence(self):
        app = _App()
        ctrl = _controller(app)
        self.assertFalse(ctrl.is_busy())

        app._kakeibo_sequence_active = True
        self.assertTrue(ctrl.is_busy())

        app._kakeibo_sequence_active = False
        self.assertFalse(ctrl.is_busy())

    def test_handle_text_does_not_start_while_sequence_active(self):
        app = _App()
        ctrl = _controller(app)
        app._entry.text = "コンビニで500円"
        app._kakeibo_sequence_active = True

        ctrl.handle_text()

        # 入力欄が消費されず、生成も始まっていない。
        self.assertEqual(app._entry.text, "コンビニで500円")
        self.assertEqual(ctrl._operation_state, "idle")

    def test_handle_voice_does_not_start_while_sequence_active(self):
        app = _App()
        ctrl = _controller(app)
        app._kakeibo_sequence_active = True

        ctrl.handle_voice("コンビニで500円")

        self.assertEqual(app._entry.text, "")
        self.assertEqual(ctrl._operation_state, "idle")

    def test_missing_attribute_does_not_make_controller_busy(self):
        app = _App()
        ctrl = _controller(app)
        if hasattr(app, "_kakeibo_sequence_active"):
            delattr(app, "_kakeibo_sequence_active")
        self.assertFalse(ctrl.is_busy())

    def test_operation_label_is_not_idle_during_kakeibo_sequence(self):
        app = _App()
        ctrl = _controller(app)
        self.assertEqual(ctrl.operation_label(), "✅ 待機中")

        app._kakeibo_sequence_active = True
        self.assertTrue(ctrl.is_busy())
        self.assertNotEqual(ctrl.operation_label(), "✅ 待機中")
        self.assertIn("家計簿", ctrl.operation_label())

        app._kakeibo_sequence_active = False
        self.assertEqual(ctrl.operation_label(), "✅ 待機中")

    def test_finish_operation_keeps_send_button_disabled_during_sequence(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        app._kakeibo_sequence_active = True

        ctrl._finish_operation(generation)

        # 生成は終わったが家計簿シーケンス中なので送信ボタンは戻さない。
        self.assertEqual(app._btn_send.state, "disabled")
        self.assertTrue(ctrl.is_busy())

    def test_finish_operation_restores_send_button_without_sequence(self):
        app = _App()
        ctrl = _controller(app)
        generation = ctrl._begin_operation("generating")
        app._kakeibo_sequence_active = False

        ctrl._finish_operation(generation)

        self.assertEqual(app._btn_send.state, "normal")
        self.assertFalse(ctrl.is_busy())
