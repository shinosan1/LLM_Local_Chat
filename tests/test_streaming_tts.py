import inspect
import io
import struct
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from audio_workers import (
    AUDIO_CHUNK,
    AUDIO_RATE,
    DEFAULT_VAD_RMS,
    DEFAULT_TTS_RATE,
    VAD_PREROLL_CHUNKS,
    VAD_PREROLL_SECONDS,
    VAD_SPEECH_CHUNKS,
    WHISPER_REPETITION_MAX_LEN,
    WHISPER_REPETITION_MIN_LEN,
    TTSWorker,
    VoiceRecognizer,
    _segment_metric_summary,
    classify_recognition_quality,
    is_short_phrase_repetition,
    normalize_tts_rate,
    normalize_vad_threshold,
    should_load_whisper,
    should_queue_initial_greeting,
)
from controller import StreamingTTSBuffer


def _make_chunk(amplitude: int, n_samples: int = AUDIO_CHUNK) -> bytes:
    """振幅一定(=RMSがamplitudeと一致)のPCM16LEチャンクを生成する。"""
    return struct.pack(f"{n_samples}h", *([amplitude] * n_samples))


class _FakeStream:
    """VoiceRecognizer._wait_for_speech_onsetへ渡す、決め打ちチャンク列を返す
    フェイクの録音ストリーム。"""

    def __init__(self, chunks: list[bytes]):
        self._chunks = list(chunks)

    def read(self, n, exception_on_overflow=False):
        return self._chunks.pop(0)


class StreamingTTSBufferTests(unittest.TestCase):
    def test_emits_completed_sentences_across_chunks(self):
        buffer = StreamingTTSBuffer()

        self.assertEqual(buffer.feed("こんにちは。今日は"), ["こんにちは。"])
        self.assertEqual(buffer.feed("晴れです！次は"), ["今日は晴れです！"])
        self.assertEqual(buffer.finalize(), "次は")

    def test_newline_is_a_boundary(self):
        buffer = StreamingTTSBuffer()

        self.assertEqual(buffer.feed("一行目\n二行目"), ["一行目"])
        self.assertEqual(buffer.finalize(), "二行目")

    def test_ignores_fenced_code_split_across_chunks(self):
        buffer = StreamingTTSBuffer()

        self.assertEqual(buffer.feed("説明です。``"), ["説明です。"])
        self.assertEqual(buffer.feed("`json\n{\"amount\": 100}\n```続きです。"), ["続きです。"])
        self.assertEqual(buffer.finalize(), "")

    def test_reset_discards_unfinished_text(self):
        buffer = StreamingTTSBuffer()
        buffer.feed("読み上げ途中")

        buffer.reset()

        self.assertEqual(buffer.finalize(), "")


class TTSInterferenceTests(unittest.TestCase):
    def setUp(self):
        self.voice = VoiceRecognizer.__new__(VoiceRecognizer)
        self.voice._tts_active = False
        self.voice._tts_generation = 0

    def test_detects_tts_that_started_and_ended_during_transcription(self):
        generation = self.voice._tts_generation

        self.voice.mark_tts_started()
        self.voice._tts_active = False

        self.assertTrue(self.voice._tts_interfered_since(generation))

    def test_no_interference_without_tts_start(self):
        generation = self.voice._tts_generation

        self.assertFalse(self.voice._tts_interfered_since(generation))

    def test_delayed_stop_does_not_unmute_new_tts_generation(self):
        stopped_generation = self.voice._tts_generation
        self.voice.mark_tts_started()

        self.assertFalse(
            self.voice.finish_tts_if_generation(stopped_generation)
        )
        self.assertTrue(self.voice._tts_active)

    def test_delayed_stop_unmutes_unchanged_generation(self):
        self.voice.mark_tts_started()
        stopped_generation = self.voice._tts_generation

        self.assertTrue(
            self.voice.finish_tts_if_generation(stopped_generation)
        )
        self.assertFalse(self.voice._tts_active)


class MicrophoneReadLoggingTests(unittest.TestCase):
    def setUp(self):
        self.voice = VoiceRecognizer.__new__(VoiceRecognizer)
        self.voice._mic_read_error_last_log = None
        self.voice._mic_read_error_active = False

    def test_read_errors_are_rate_limited_and_recovery_resets_state(self):
        output = io.StringIO()
        with patch("audio_workers.time.monotonic", side_effect=[0.0, 1.0, 31.0]):
            with redirect_stdout(output):
                self.voice._record_mic_read_error(OSError("secret detail"))
                self.voice._record_mic_read_error(RuntimeError("hidden"))
                self.voice._record_mic_read_error(RuntimeError("hidden"))
                self.voice._record_mic_read_success()

        log = output.getvalue()
        self.assertEqual(log.count("read failed"), 2)
        self.assertIn("OSError", log)
        self.assertIn("RuntimeError", log)
        self.assertNotIn("secret detail", log)
        self.assertNotIn("hidden", log)
        self.assertEqual(log.count("read recovered"), 1)
        self.assertFalse(self.voice._mic_read_error_active)
        self.assertIsNone(self.voice._mic_read_error_last_log)


class VadPrerollTests(unittest.TestCase):
    """VADプリロール(_wait_for_speech_onset)が、VAD_SPEECH_CHUNKSとは独立に
    最低0.75秒分のリングバッファを保持し、発話確定前の低RMSチャンクも
    録音本体へ含めることを確認する。"""

    def setUp(self):
        self.voice = VoiceRecognizer.__new__(VoiceRecognizer)
        self.voice._active = True
        self.voice._enabled = threading.Event()
        self.voice._enabled.set()
        self.voice._tts_active = False
        self.voice.vad_threshold = DEFAULT_VAD_RMS

    def test_preroll_chunk_count_covers_at_least_required_seconds(self):
        preroll_seconds = VAD_PREROLL_CHUNKS * AUDIO_CHUNK / AUDIO_RATE
        self.assertGreaterEqual(preroll_seconds, VAD_PREROLL_SECONDS)
        # 1チャンク少ないと要件を満たさない、すなわち必要最小限のチャンク数であること
        self.assertLess(
            (VAD_PREROLL_CHUNKS - 1) * AUDIO_CHUNK / AUDIO_RATE, VAD_PREROLL_SECONDS
        )

    def test_preroll_chunk_count_does_not_derive_from_speech_chunks(self):
        # 旧実装は VAD_SPEECH_CHUNKS + 2 (=8チャンク=0.512秒) だった。
        # 新実装はVAD_SPEECH_CHUNKSと無関係な時間ベースの値(12チャンク)である。
        self.assertNotEqual(VAD_PREROLL_CHUNKS, VAD_SPEECH_CHUNKS + 2)
        self.assertEqual(VAD_PREROLL_CHUNKS, 12)

    def test_pre_confirmation_quiet_chunks_are_included_in_recorded_body(self):
        quiet = _make_chunk(50)   # DEFAULT_VAD_RMS(150)未満
        loud = _make_chunk(1000)  # DEFAULT_VAD_RMS(150)超

        # 発話確定(6連続の閾値超え)より前に、VAD_PREROLL_CHUNKSを超える量の
        # 静かなチャンクを流す。旧実装(VAD_SPEECH_CHUNKS+2=8チャンク)なら
        # 静かな区間はほぼ切り捨てられていたが、新実装ではプリロール分
        # (VAD_PREROLL_CHUNKS - VAD_SPEECH_CHUNKS 個)がそのまま残るはずである。
        quiet_chunks = [quiet] * (VAD_PREROLL_CHUNKS + 8)
        loud_chunks = [loud] * VAD_SPEECH_CHUNKS
        stream = _FakeStream(quiet_chunks + loud_chunks)

        frames = self.voice._wait_for_speech_onset(stream)

        self.assertIsNotNone(frames)
        self.assertEqual(len(frames), VAD_PREROLL_CHUNKS)
        # 録音本体の長さそのものが0.75秒以上であること
        self.assertGreaterEqual(len(frames) * AUDIO_CHUNK / AUDIO_RATE, VAD_PREROLL_SECONDS)
        # 末尾VAD_SPEECH_CHUNKS個は発話確定に使われた「大きい」チャンク
        self.assertTrue(all(f == loud for f in frames[-VAD_SPEECH_CHUNKS:]))
        # それより前は発話確定前の「静かな」チャンクであり、破棄されず含まれている
        self.assertTrue(all(f == quiet for f in frames[:-VAD_SPEECH_CHUNKS]))
        self.assertEqual(len(frames) - VAD_SPEECH_CHUNKS, VAD_PREROLL_CHUNKS - VAD_SPEECH_CHUNKS)

    def test_returns_none_when_disabled_before_confirmation(self):
        # _enabledがクリアされている場合、ストリームを読まずに即Noneを返す
        # (既存の「if not (self._active and self._enabled.is_set()): continue」と同じ停止動作)
        self.voice._enabled.clear()
        stream = _FakeStream([])

        self.assertIsNone(self.voice._wait_for_speech_onset(stream))


class WhisperInitialPromptTests(unittest.TestCase):
    """Whisper transcribe() 呼び出しに挨拶語彙を含むinitial_promptが
    渡されないことをソースレベルで確認する回帰ガード。"""

    def test_transcribe_call_has_no_initial_prompt_argument(self):
        source = inspect.getsource(VoiceRecognizer._loop)
        self.assertNotIn("initial_prompt", source)

    def test_no_greeting_vocabulary_remains_in_the_recognition_loop(self):
        source = inspect.getsource(VoiceRecognizer._loop)
        for phrase in ("こんにちは", "ありがとうございます", "自己紹介"):
            self.assertNotIn(phrase, source)


class TTSRateTests(unittest.TestCase):
    def test_accepts_sapi_rate_boundaries(self):
        self.assertEqual(normalize_tts_rate(-10), -10)
        self.assertEqual(normalize_tts_rate(10), 10)

    def test_invalid_rate_falls_back_to_default(self):
        for value in (-11, 11, 2.5, "2", True, None):
            with self.subTest(value=value):
                self.assertEqual(normalize_tts_rate(value), DEFAULT_TTS_RATE)


class AudioStartupSettingsTests(unittest.TestCase):
    def test_accepts_vad_boundaries(self):
        self.assertEqual(normalize_vad_threshold(1), 1)
        self.assertEqual(normalize_vad_threshold(32767), 32767)

    def test_invalid_vad_values_fall_back_to_default(self):
        for value in (0, -1, 32768, 2.5, "150", True, False, None):
            with self.subTest(value=value):
                self.assertEqual(
                    normalize_vad_threshold(value), DEFAULT_VAD_RMS)

    def test_whisper_load_depends_only_on_explicit_mic_enablement(self):
        self.assertTrue(should_load_whisper(True))
        for value in (False, None, 1, "true"):
            with self.subTest(value=value):
                self.assertFalse(should_load_whisper(value))

    def test_initial_greeting_is_queued_only_once(self):
        self.assertTrue(should_queue_initial_greeting(True, False))
        self.assertFalse(should_queue_initial_greeting(True, True))
        self.assertFalse(should_queue_initial_greeting(False, False))


class FakeSpeaker:
    def __init__(self, wait_results):
        self.Rate = None
        self.wait_results = list(wait_results)
        self.speak_calls = []
        self.wait_calls = []

    def Speak(self, text, flags):
        self.speak_calls.append((text, flags))

    def WaitUntilDone(self, timeout_ms):
        self.wait_calls.append(timeout_ms)
        return self.wait_results.pop(0)


class SAPIPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.worker = TTSWorker.__new__(TTSWorker)
        self.worker.rate = 6
        self.worker._stop_flag = False

    def test_async_playback_waits_without_purge(self):
        speaker = FakeSpeaker([0, -1])

        self.worker._execute_sapi_speak(speaker, "かしこまりました！")

        self.assertEqual(speaker.Rate, 6)
        self.assertEqual(speaker.speak_calls, [("かしこまりました！", 1)])
        self.assertEqual(speaker.wait_calls, [50, 50])

    def test_stop_uses_purge(self):
        speaker = FakeSpeaker([])
        self.worker._stop_flag = True

        self.worker._execute_sapi_speak(speaker, "なるほど。")

        self.assertEqual(
            speaker.speak_calls,
            [("なるほど。", 1), ("", 3)],
        )


class WhisperDiagnosticTests(unittest.TestCase):
    def test_segment_metric_summary_uses_expected_extreme(self):
        segments = [
            {"no_speech_prob": 0.2, "avg_logprob": -0.4},
            {"no_speech_prob": 0.8, "avg_logprob": -1.2},
        ]

        self.assertEqual(
            _segment_metric_summary(segments, "no_speech_prob"),
            ("0.500", "0.800"),
        )
        self.assertEqual(
            _segment_metric_summary(segments, "avg_logprob"),
            ("-0.800", "-1.200"),
        )

    def test_segment_metric_summary_handles_missing_values(self):
        self.assertEqual(_segment_metric_summary([], "compression_ratio"), ("n/a", "n/a"))


class PhraseLengthBoundaryTests(unittest.TestCase):
    """is_short_phrase_repetition自体の境界値。統合ゲートの変更とは無関係、無変更。"""

    def test_phrase_length_boundaries(self):
        min_len_phrase = "あ" * WHISPER_REPETITION_MIN_LEN
        max_len_phrase = "あ" * WHISPER_REPETITION_MAX_LEN
        too_short_phrase = "あ" * (WHISPER_REPETITION_MIN_LEN - 1)
        too_long_phrase = "あ" * (WHISPER_REPETITION_MAX_LEN + 1)

        self.assertTrue(
            is_short_phrase_repetition(f"{min_len_phrase}、{min_len_phrase}。")
        )
        self.assertTrue(
            is_short_phrase_repetition(f"{max_len_phrase}、{max_len_phrase}。")
        )
        self.assertFalse(
            is_short_phrase_repetition(f"{too_short_phrase}、{too_short_phrase}。")
        )
        self.assertFalse(
            is_short_phrase_repetition(f"{too_long_phrase}、{too_long_phrase}。")
        )

    def test_exclamation_and_question_marks_are_boundaries(self):
        self.assertTrue(is_short_phrase_repetition("こんにちは!こんにちは!"))
        self.assertTrue(is_short_phrase_repetition("こんにちは?こんにちは?"))

    def test_ellipsis_is_a_boundary(self):
        self.assertTrue(is_short_phrase_repetition("こんにちは…こんにちは…"))

    def test_surrounding_whitespace_does_not_prevent_detection(self):
        self.assertTrue(is_short_phrase_repetition("こんにちは、 こんにちは。"))
        self.assertTrue(is_short_phrase_repetition("こんにちは、　こんにちは。"))
        self.assertTrue(is_short_phrase_repetition("こんにちは 、こんにちは。"))

    def test_distinct_phrases_are_not_a_repetition(self):
        self.assertFalse(is_short_phrase_repetition("はい、はい、わかりました。"))


class RecognitionQualityGateTests(unittest.TestCase):
    """classify_recognition_quality: no_speech・avg_logprob・compression_ratio・
    短フレーズ反復形状を組み合わせた単一の品質ゲート。"""

    # ── 実運用ケース一覧(計画書の一覧表と対応) ──────────────

    def test_case01_normal_short_utterance_is_sent(self):
        segments = [{
            "no_speech_prob": 0.05, "avg_logprob": -0.30, "compression_ratio": 1.10,
        }]
        self.assertIsNone(
            classify_recognition_quality("今日は天気がいいですね。", segments)
        )

    def test_case02_genuinely_spoken_repetition_is_sent(self):
        segments = [{
            "no_speech_prob": 0.10, "avg_logprob": -0.30, "compression_ratio": 1.15,
        }]
        self.assertIsNone(
            classify_recognition_quality("こんにちは、こんにちは。", segments)
        )

    def test_case03_production_hallucination_1(self):
        segments = [{"no_speech_prob": 0.794, "avg_logprob": -0.872}]
        self.assertEqual(
            classify_recognition_quality("こんにちは、こんにちは。", segments),
            "discard_silence_hallucination",
        )

    def test_case04_production_hallucination_2_two_segments(self):
        segments = [
            {"no_speech_prob": 0.853, "avg_logprob": -0.673},
            {"no_speech_prob": 0.853, "avg_logprob": -0.673},
        ]
        self.assertEqual(
            classify_recognition_quality(
                "こんにちは、こんにちは。こんにちは、こんにちは。", segments
            ),
            "discard_silence_hallucination",
        )

    def test_case05_production_hallucination_3(self):
        segments = [{"no_speech_prob": 0.870, "avg_logprob": -0.743}]
        self.assertEqual(
            classify_recognition_quality("こんにちは、こんにちは。", segments),
            "discard_silence_hallucination",
        )

    def test_case06_previously_correctly_discarded_case_still_discards(self):
        segments = [{"no_speech_prob": 0.848, "avg_logprob": -0.951}]
        self.assertEqual(
            classify_recognition_quality("こんにちは、こんにちは。", segments),
            "discard_silence_hallucination",
        )

    def test_case07_high_compression_but_genuine_repetition_is_sent(self):
        segments = [{
            "no_speech_prob": 0.10, "avg_logprob": -0.30, "compression_ratio": 2.50,
        }]
        self.assertIsNone(
            classify_recognition_quality("こんにちは、こんにちは。", segments)
        )

    def test_case08_non_repetitive_silence_hallucination_is_discarded(self):
        segments = [{"no_speech_prob": 0.80, "avg_logprob": -0.70}]
        self.assertEqual(
            classify_recognition_quality("こんにちは。", segments),
            "discard_silence_hallucination",
        )

    def test_case09_catastrophic_low_confidence_non_repetitive(self):
        # no_speechが低くても、avg_logprobが壊滅的な低confidence
        # (WHISPER_CATASTROPHIC_LOGPROB_LIMIT以下)なら破棄する。
        segments = [{
            "no_speech_prob": 0.10, "avg_logprob": -1.50, "compression_ratio": 1.10,
        }]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_low_confidence",
        )

    def test_case10_low_no_speech_moderately_low_confidence_is_protected(self):
        # avg_logprob=-1.00は通常基準(-0.90)を下回るが、壊滅的な基準(-1.10)には
        # 達しないため、no_speechが低い(実発話が濃厚な)場合は保護して送信する。
        segments = [{"no_speech_prob": 0.10, "avg_logprob": -1.00}]
        self.assertIsNone(
            classify_recognition_quality("何かの発話です。", segments)
        )

    def test_no_speech_010_logprob_0955_is_sent(self):
        # 追加確認項目: no_speech=0.10、avg_logprob=-0.955 → send
        segments = [{"no_speech_prob": 0.10, "avg_logprob": -0.955}]
        self.assertIsNone(
            classify_recognition_quality("何かの発話です。", segments)
        )

    def test_case11_avg_logprob_boundary_with_no_speech_missing(self):
        segments = [{"avg_logprob": -0.90}]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_low_confidence",
        )

    def test_case12_no_speech_missing_low_confidence(self):
        segments = [{"avg_logprob": -0.95}]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_low_confidence",
        )

    def test_case13_high_compression_non_repetitive_moderately_low_confidence_is_protected(self):
        # no_speechが低い帯では、ステップ1の保護がステップ3(compression)より
        # 先に確定するため、高compressionでも壊滅的な低confidenceでなければ送信する。
        segments = [{
            "no_speech_prob": 0.10, "avg_logprob": -0.95, "compression_ratio": 2.50,
        }]
        self.assertIsNone(
            classify_recognition_quality("何かの発話です。", segments)
        )

    # ── 実発話保護(ステップ1)の境界値 ──────────────

    def test_protection_boundary_no_speech_exactly_at_max(self):
        segments = [{"no_speech_prob": 0.30, "avg_logprob": -0.30}]
        self.assertIsNone(
            classify_recognition_quality("こんにちは、こんにちは。", segments)
        )

    def test_protection_just_past_no_speech_max_is_not_protected(self):
        # no_speechが保護境界をわずかに超えると、反復+低confidenceなら破棄されうる
        # (no_speech>0.30の帯では従来の較正ゲート/反復ゲートがそのまま働く)
        segments = [{"no_speech_prob": 0.31, "avg_logprob": -0.95}]
        self.assertEqual(
            classify_recognition_quality("こんにちは、こんにちは。", segments),
            "discard_repetition_hallucination",
        )

    def test_protection_tolerates_moderately_low_confidence_even_with_repetition(self):
        # no_speech<=0.30の帯では、avg_logprobが壊滅的でない限り、反復があっても
        # ステップ1が最優先で送信を確定する(ステップ4の反復ゲートより先に判定される)。
        segments = [{"no_speech_prob": 0.10, "avg_logprob": -0.90}]
        self.assertIsNone(
            classify_recognition_quality("こんにちは、こんにちは。", segments)
        )

    def test_protection_catastrophic_floor_boundary_is_not_protected(self):
        # avg_logprob=-1.10ちょうどは保護されない(>であって>=ではない)
        segments = [{"no_speech_prob": 0.10, "avg_logprob": -1.10}]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_low_confidence",
        )

    def test_protection_just_above_catastrophic_floor_boundary_is_protected(self):
        segments = [{"no_speech_prob": 0.10, "avg_logprob": -1.099}]
        self.assertIsNone(
            classify_recognition_quality("何かの発話です。", segments)
        )

    # ── no_speech較正ゲート(ステップ2)の境界値 ──────────────

    def test_silence_gate_boundary_no_speech_060_logprob_090(self):
        segments = [{"no_speech_prob": 0.60, "avg_logprob": -0.90}]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_silence_hallucination",
        )

    def test_silence_gate_just_below_no_speech_060_falls_through_to_confidence_floor(self):
        # no_speech<0.60はステップ2(無音較正)を素通りするが、avg_logprob<=-0.90は
        # no_speechの値に関係なくステップ5(独立confidenceフロア)が捕捉する。
        segments = [{"no_speech_prob": 0.59, "avg_logprob": -0.90}]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_low_confidence",
        )

    def test_silence_gate_high_no_speech_uses_relaxed_logprob_boundary(self):
        segments = [{"no_speech_prob": 0.75, "avg_logprob": -0.60}]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_silence_hallucination",
        )

    def test_silence_gate_just_below_high_no_speech_requires_strict_logprob(self):
        # no_speech=0.749は「高水準」未満なので、-0.60では発火せず-0.90が必要
        segments = [{"no_speech_prob": 0.749, "avg_logprob": -0.60}]
        self.assertIsNone(
            classify_recognition_quality("何かの発話です。", segments)
        )

    def test_combines_no_speech_and_logprob_across_segments(self):
        # 単一セグメントでは発火しない2値が、平均すると発火する
        segments = [
            {"no_speech_prob": 0.50, "avg_logprob": -1.30},
            {"no_speech_prob": 0.80, "avg_logprob": -0.70},
        ]
        # avg no_speech=0.65(>=0.60), avg logprob=-1.00(<=-0.90の通常基準)
        self.assertEqual(
            classify_recognition_quality("何かの発話", segments),
            "discard_silence_hallucination",
        )

    # ── compression異常(ステップ3)のラベル分岐 ──────────────

    def test_compression_with_repetition_labels_repetition_hallucination(self):
        segments = [{
            "no_speech_prob": 0.40, "avg_logprob": -0.95, "compression_ratio": 2.40,
        }]
        self.assertEqual(
            classify_recognition_quality("こんにちは、こんにちは。", segments),
            "discard_repetition_hallucination",
        )

    def test_compression_non_repetitive_high_no_speech_labels_silence(self):
        segments = [{
            "no_speech_prob": 0.65, "avg_logprob": -0.30, "compression_ratio": 2.40,
        }]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_silence_hallucination",
        )

    def test_runtime_high_compression_silence_candidates_are_discarded(self):
        for no_speech in (0.550, 0.586, 0.599):
            with self.subTest(no_speech=no_speech):
                segments = [{
                    "no_speech_prob": no_speech,
                    "avg_logprob": -0.30,
                    "compression_ratio": 3.00,
                }]
                self.assertEqual(
                    classify_recognition_quality("何かの発話です。", segments),
                    "discard_silence_hallucination",
                )

    def test_compression_silence_boundary_at_050_is_discarded(self):
        segments = [{
            "no_speech_prob": 0.50,
            "avg_logprob": -0.30,
            "compression_ratio": 2.40,
        }]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_silence_hallucination",
        )

    def test_compression_silence_just_below_boundary_is_sent(self):
        segments = [{
            "no_speech_prob": 0.499,
            "avg_logprob": -0.30,
            "compression_ratio": 2.40,
        }]
        self.assertIsNone(
            classify_recognition_quality("何かの発話です。", segments)
        )

    def test_compression_non_repetitive_low_logprob_labels_low_confidence(self):
        # no_speech=0.40(>0.30)はステップ1の保護対象外なので、ステップ3の
        # compression判定がそのまま働く。
        segments = [{
            "no_speech_prob": 0.40, "avg_logprob": -0.90, "compression_ratio": 2.40,
        }]
        self.assertEqual(
            classify_recognition_quality("何かの発話です。", segments),
            "discard_low_confidence",
        )

    def test_compression_just_below_boundary_does_not_gate_here(self):
        segments = [{
            "no_speech_prob": 0.40, "avg_logprob": -0.95, "compression_ratio": 2.399,
        }]
        # compressionゲート不発。反復+logprob<=-0.90でステップ4が発火する。
        self.assertEqual(
            classify_recognition_quality("こんにちは、こんにちは。", segments),
            "discard_repetition_hallucination",
        )

    # ── 実発話保護(ステップ1)と反復ゲート(ステップ4)の組み合わせ確認 ──

    def test_keeps_high_confidence_intentional_repetition(self):
        segments = [{"no_speech_prob": 0.10, "avg_logprob": -0.3}]
        self.assertIsNone(
            classify_recognition_quality("こんにちは、こんにちは。", segments)
        )

    def test_keeps_short_acknowledgement_repetition(self):
        # 「はい」は2文字でWHISPER_REPETITION_MIN_LEN未満のため反復と見なされない。
        # no_speechが低く(実発話が濃厚)、avg_logprob=-0.955は壊滅的基準(-1.10)には
        # 達しないため、ステップ1により保護され送信される。
        segments = [{"no_speech_prob": 0.05, "avg_logprob": -0.955}]
        self.assertIsNone(
            classify_recognition_quality("はい、はい。", segments)
        )

    def test_keeps_non_repetitive_speech_with_moderately_low_confidence(self):
        # no_speechが低い実発話は、avg_logprobが多少低くても
        # (壊滅的な基準に達しない限り)保護して送信する。
        segments = [{"no_speech_prob": 0.05, "avg_logprob": -0.955}]
        self.assertIsNone(
            classify_recognition_quality("今日は天気がいいですね。", segments)
        )

    def test_keeps_distinct_phrases_repeated_before_different_final_phrase(self):
        segments = [{"no_speech_prob": 0.05, "avg_logprob": -0.955}]
        self.assertIsNone(
            classify_recognition_quality("はい、はい、わかりました。", segments)
        )

    def test_uses_segment_average_not_single_low_confidence_segment(self):
        segments = [
            {"no_speech_prob": 0.05, "avg_logprob": -0.1},
            {"no_speech_prob": 0.05, "avg_logprob": -0.1},
            {"no_speech_prob": 0.05, "avg_logprob": -2.0},
        ]
        self.assertIsNone(
            classify_recognition_quality("こんにちは、こんにちは。", segments)
        )

    # ── 独立confidenceフロア(ステップ5): no_speech欠落時、または>0.30の帯 ──

    def test_missing_metrics_fail_open(self):
        self.assertIsNone(classify_recognition_quality("x", [{}]))


if __name__ == "__main__":
    unittest.main()
