import collections
import math
import queue
import re
import struct
import threading
import time

import numpy as np

try:
    import pyaudio
    _PYAUDIO_AVAILABLE = True
except ImportError:
    pyaudio = None
    _PYAUDIO_AVAILABLE = False
    print("[VoiceRecognizer] pyaudio 未インストール: 音声認識を無効化します")

try:
    import win32com.client
    import pythoncom
    _WIN32_AVAILABLE = True
except ImportError:
    _WIN32_AVAILABLE = False
    print("[TTS] pywin32 未インストール: Windows SAPI5 TTS を無効化します")


# ── 音声 (VAD) ──────────────────────────────
AUDIO_RATE         = 16000
AUDIO_CHUNK        = 1024
AUDIO_FORMAT       = pyaudio.paInt16 if _PYAUDIO_AVAILABLE else 8
DEFAULT_VAD_RMS    = 150
MIN_VAD_RMS        = 1
MAX_VAD_RMS        = 32767
DEFAULT_TTS_RATE   = 0
MIC_READ_ERROR_LOG_INTERVAL_SECONDS = 30.0
MIN_TTS_RATE       = -10
MAX_TTS_RATE       = 10
VAD_SPEECH_CHUNKS  = 6
VAD_SILENCE_CHUNKS = 30
VAD_MAX_SECONDS    = 6
VAD_PREROLL_SECONDS = 0.75           # 発話確定前に保持する最小プリロール秒数。
                                      # VAD_SPEECH_CHUNKS(発話確定の連続チャンク数)とは
                                      # 独立して固定する(確定条件を変えても目減りしない)。
VAD_PREROLL_CHUNKS = math.ceil(VAD_PREROLL_SECONDS * AUDIO_RATE / AUDIO_CHUNK)
# ── Whisper認識品質ゲート: 閾値は本ブロックへ集約する ──────
WHISPER_NO_SPEECH_LIMIT = 0.60              # 通常時のno_speech下限
WHISPER_AVG_LOGPROB_LIMIT = -0.90            # 通常基準。保護・低confidence・反復・compression判定すべてで
                                              # この1つの値を共有する(専用フロアは新設しない)
WHISPER_NO_SPEECH_HIGH = 0.75                # no_speechを「強い自己申告」とみなす水準
WHISPER_SILENCE_AVG_LOGPROB_LIMIT = -0.60    # no_speechが高水準の場合に要求するlogprob基準。
                                              # 通常基準より広い範囲を無音由来の裏付けとして受理する。
WHISPER_COMPRESSION_LIMIT = 2.40             # compression異常の基準(単独では破棄しない)
WHISPER_COMPRESSION_NO_SPEECH_LIMIT = 0.50   # 高compression時に無音疑いとする実測由来の下限
WHISPER_PROTECT_NO_SPEECH_MAX = 0.30         # 実発話保護: no_speech平均がこれ以下
WHISPER_CATASTROPHIC_LOGPROB_LIMIT = -1.10   # no_speech平均が実発話保護の対象内でも、
                                              # これ以下は壊滅的な低confidenceとして破棄する
WHISPER_REPETITION_MIN_LEN = 3
WHISPER_REPETITION_MAX_LEN = 12

# 句読点(全角/半角)・三点リーダー・改行を短時間反復の区切りとして扱う
_PHRASE_SPLIT_PATTERN = re.compile(r"[。、！？!?…\n]+")

WHISPER_NOISE = {
    "字幕", "翻訳",
    "。", "、", "…", " ", "　",
}
# 動画系 hallucination 専用（部分一致フィルター）
WHISPER_NOISE_PARTIAL = {
    "ご視聴ありがとうございました",
    "チャンネル登録をお願い",
    "チャンネル登録よろしく",
    "高評価よろしくお願い",
    "いいねとチャンネル登録",
    "概要欄をご確認",
    "私のビデオを見て",
    "動画を見てください",
    "チャンネルに登録",
}


def _segment_metric_summary(segments: list[dict], key: str) -> tuple[str, str]:
    """Whisperセグメント指標の平均値と、安全側の極値をログ用に返す。"""
    values = [
        float(segment[key])
        for segment in segments
        if isinstance(segment, dict)
        and isinstance(segment.get(key), (int, float))
        and not isinstance(segment.get(key), bool)
    ]
    if not values:
        return "n/a", "n/a"
    average = sum(values) / len(values)
    extreme = min(values) if key == "avg_logprob" else max(values)
    return f"{average:.3f}", f"{extreme:.3f}"


def _split_into_phrase_fragments(text: str) -> list[str]:
    """句読点・感嘆符・疑問符・三点リーダー・改行で分割し、前後の半角/全角空白を
    除去したうえで空となる断片を除いたフレーズ一覧を返す。"""
    return [
        fragment.strip(" 　")
        for fragment in _PHRASE_SPLIT_PATTERN.split(text)
        if fragment.strip(" 　")
    ]


def is_short_phrase_repetition(text: str) -> bool:
    """発話全体が同一の短いフレーズだけの反復であるかを判定する。"""
    fragments = _split_into_phrase_fragments(text)
    if len(fragments) < 2:
        return False
    unique_fragments = set(fragments)
    if len(unique_fragments) != 1:
        return False
    phrase_len = len(fragments[0])
    return WHISPER_REPETITION_MIN_LEN <= phrase_len <= WHISPER_REPETITION_MAX_LEN


def _average_metric(segments: list[dict], key: str) -> float | None:
    """有効な数値のみを対象に平均値を返す(bool型は除外)。

    no_speech_prob・avg_logprob・compression_ratioのいずれも、局所的な
    外れ値による誤判定を避けるため、単一セグメントの極値ではなく
    有効セグメント全体の単純平均で統一して評価する。1セグメントだけが
    深刻に異常でも、他の正常なセグメントに希釈されて見逃されうる
    トレードオフを受容している。
    """
    values = [
        float(segment[key])
        for segment in segments
        if isinstance(segment, dict)
        and isinstance(segment.get(key), (int, float))
        and not isinstance(segment.get(key), bool)
    ]
    if not values:
        return None
    return sum(values) / len(values)


def classify_recognition_quality(text: str, segments: list[dict]) -> str | None:
    """Whisper認識結果の採否を判定する単一の品質ゲート。

    no_speech_prob・avg_logprob・compression_ratio(いずれもセグメント平均)、
    短フレーズ反復形状を組み合わせて判定する。単一指標のOR条件だけでは
    破棄しない。戻り値はNone(送信)、または次のいずれかの破棄理由。
      - "discard_silence_hallucination": 無音由来の幻覚
      - "discard_repetition_hallucination": 反復由来の幻覚
      - "discard_low_confidence": no_speech/反復に関係しない低confidence
    """
    no_speech_avg = _average_metric(segments, "no_speech_prob")
    avg_logprob_avg = _average_metric(segments, "avg_logprob")
    compression_avg = _average_metric(segments, "compression_ratio")
    repeated_phrase = is_short_phrase_repetition(text)

    # 1. 実発話保護: no_speechが十分低ければ、confidenceが通常基準(-0.90)を
    #    下回っていても、反復・高compressionの有無を問わず実発話として保護する。
    #    ただし壊滅的な低confidence(WHISPER_CATASTROPHIC_LOGPROB_LIMIT以下)は
    #    実発話であっても内容が信頼できないため、ここで判定を確定して破棄する。
    if (
        no_speech_avg is not None
        and avg_logprob_avg is not None
        and no_speech_avg <= WHISPER_PROTECT_NO_SPEECH_MAX
    ):
        if avg_logprob_avg > WHISPER_CATASTROPHIC_LOGPROB_LIMIT:
            return None
        return "discard_low_confidence"

    # 2. no_speech較正ゲート(反復の有無を問わない、無音由来の判定)。
    #    no_speechの自己申告が強いほど、要求するconfidenceの基準を広げる。
    if (
        no_speech_avg is not None
        and avg_logprob_avg is not None
        and no_speech_avg >= WHISPER_NO_SPEECH_LIMIT
    ):
        required_logprob = (
            WHISPER_SILENCE_AVG_LOGPROB_LIMIT
            if no_speech_avg >= WHISPER_NO_SPEECH_HIGH
            else WHISPER_AVG_LOGPROB_LIMIT
        )
        if avg_logprob_avg <= required_logprob:
            return "discard_silence_hallucination"

    # 3. compression異常: 高compression単独では破棄せず、根拠に応じて
    #    理由を分岐する。実機で no_speech=0.55〜0.60 / compression=3.0 の
    #    無音誤認識が高confidence扱いで通過したため、この組み合わせだけは
    #    無音由来として破棄する。no_speech<0.50の正常発話候補は維持する。
    if compression_avg is not None and compression_avg >= WHISPER_COMPRESSION_LIMIT:
        if repeated_phrase:
            return "discard_repetition_hallucination"
        if (
            no_speech_avg is not None
            and no_speech_avg >= WHISPER_COMPRESSION_NO_SPEECH_LIMIT
        ):
            return "discard_silence_hallucination"
        if avg_logprob_avg is not None and avg_logprob_avg <= WHISPER_AVG_LOGPROB_LIMIT:
            return "discard_low_confidence"

    # 4. 反復ゲート: 短フレーズ反復かつconfidence低下との組み合わせ。
    if (
        repeated_phrase
        and avg_logprob_avg is not None
        and avg_logprob_avg <= WHISPER_AVG_LOGPROB_LIMIT
    ):
        return "discard_repetition_hallucination"

    # 5. 独立confidenceフロア: no_speech平均が欠落している場合、または
    #    WHISPER_PROTECT_NO_SPEECH_MAXを超える場合に、avg_logprobだけで
    #    低confidenceを判定する(no_speech平均がこれ以下の帯はステップ1で
    #    判定済みのため、ここには到達しない)。
    if avg_logprob_avg is not None and avg_logprob_avg <= WHISPER_AVG_LOGPROB_LIMIT:
        return "discard_low_confidence"

    return None


def normalize_tts_rate(value, fallback: int = DEFAULT_TTS_RATE) -> int:
    """SAPI5の読み上げ速度を安全な整数範囲へ正規化する。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    if not MIN_TTS_RATE <= value <= MAX_TTS_RATE:
        return fallback
    return value


def _format_whisper_result_log(elapsed: float, tts_active: bool, text: str) -> str:
    return f"[Whisper] {elapsed:.1f}秒 tts_active={tts_active} chars={len(text)}"


def normalize_vad_threshold(value, fallback: int = DEFAULT_VAD_RMS) -> int:
    """VAD閾値を16bit PCMの有効な正整数範囲へ正規化する。"""
    if isinstance(value, bool) or not isinstance(value, int):
        return fallback
    if not MIN_VAD_RMS <= value <= MAX_VAD_RMS:
        return fallback
    return value


def should_load_whisper(mic_enabled) -> bool:
    """Whisperが必要なのは起動時マイクが明示的に有効な場合だけ。"""
    return mic_enabled is True


def should_queue_initial_greeting(tts_enabled, already_done) -> bool:
    """起動発話を一度だけキューへ入れる条件を返す。"""
    return tts_enabled is True and already_done is False


class TTSWorker:
    def __init__(self, avatar, root):
        self.avatar = avatar
        self.root = root
        self._q = queue.Queue()
        self._state_lock = threading.Lock()
        self._pending = 0
        self._batch_active = False
        self._stream_open = False
        self._stop_flag = False
        self._is_running = True
        self._ready = threading.Event()
        self.enabled = False  # 起動時はOFF・設定ファイルから後で反映される
        self.rate = DEFAULT_TTS_RATE
        self.on_start = None
        self.on_stop  = None

        # 起動時の挨拶を一度だけ行うためのフラグ
        self._initial_greeting_done = False

        self._thread = threading.Thread(target=self._play_loop, daemon=True)
        self._thread.start()

    def begin_stream(self):
        """LLMが文を追加し得る区間を開始する（発話開始通知はまだ出さない）。"""
        with self._state_lock:
            self._stream_open = True

    def end_stream(self):
        """最終文の投入完了を通知し、再生済みならバッチを終了する。"""
        with self._state_lock:
            self._stream_open = False
            should_stop = self._batch_active and self._pending == 0
            if should_stop:
                self._batch_active = False
        if should_stop:
            self.root.after(0, self.avatar.stop_speaking)
            if self.on_stop:
                self.root.after(0, self.on_stop)

    def speak(self, text):
        """ChatApp側から呼ばれる入り口"""
        if not self.enabled:
            return
        if text and text.strip():
            if not self._ready.is_set():
                self._ready.wait(timeout=2.0)
            with self._state_lock:
                should_start = not self._batch_active
                self._batch_active = True
                self._pending += 1
            self._q.put(text)
            if should_start and self.on_start:
                self.root.after(0, self.on_start)

    def _complete_item(self):
        """1件の処理完了を記録し、バッチ全体の終了時だけ通知する。"""
        with self._state_lock:
            self._pending = max(0, self._pending - 1)
            should_stop = (
                self._batch_active
                and self._pending == 0
                and not self._stream_open
            )
            if should_stop:
                self._batch_active = False
        if should_stop:
            self.root.after(0, self.avatar.stop_speaking)
            if self.on_stop:
                self.root.after(0, self.on_stop)

    def _play_loop(self):
        """再生メインループ"""
        speaker = None
        if _WIN32_AVAILABLE:
            pythoncom.CoInitialize()

        try:
            if _WIN32_AVAILABLE:
                try:
                    speaker = self._create_sapi_speaker()
                except Exception as e:
                    print(f"[SAPI5 Init Error] {e}")
            self._ready.set()

            while self._is_running:
                try:
                    text = self._q.get(timeout=0.1)

                    if not self.enabled or speaker is None:
                        self._complete_item()
                        self._q.task_done()
                        continue

                    self._stop_flag = False

                    # 口パク開始
                    self.root.after(0, self.avatar.start_speaking)

                    # SAPI5再生実行
                    self._execute_sapi_speak(speaker, text)

                    self._complete_item()
                    self._q.task_done()

                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"[TTS Loop Error] {e}")
        finally:
            speaker = None
            if _WIN32_AVAILABLE:
                pythoncom.CoUninitialize()

    @staticmethod
    def _create_sapi_speaker():
        """TTSワーカースレッド内で再利用するSAPI5音声を生成する。"""
        return win32com.client.Dispatch("SAPI.SpVoice")

    def _execute_sapi_speak(self, speaker, text):
        """SAPI5専用再生ロジック（中断対応版）"""
        try:
            rate = normalize_tts_rate(self.rate)
            speaker.Rate = rate
            print(f"[TTS] 再生開始 rate={rate} chars={len(text)}")

            # 通常再生はAsyncのみ。Purgeは停止要求時に限定する。
            speaker.Speak(text, 1)

            while True:
                if self._stop_flag:
                    speaker.Speak("", 3)  # Async | PurgeBeforeSpeak
                    print("[TTS] 停止要求により再生を中断")
                    return

                if bool(speaker.WaitUntilDone(50)):
                    print("[TTS] 再生完了")
                    return
                if _WIN32_AVAILABLE:
                    pythoncom.PumpWaitingMessages()
        except Exception as e:
            print(f"[SAPI5 Error] {e}")

    def stop_all(self):
        """停止ボタン"""
        drained = 0
        while not self._q.empty():
            try:
                self._q.get_nowait()
                self._q.task_done()
                drained += 1
            except queue.Empty:
                break
        with self._state_lock:
            self._stream_open = False
            if drained:
                self._pending = max(0, self._pending - drained)
            should_stop = self._batch_active and self._pending == 0
            if should_stop:
                self._batch_active = False
        self._stop_flag = True
        if should_stop:
            self.root.after(0, self.avatar.stop_speaking)
            if self.on_stop:
                self.root.after(0, self.on_stop)

    def terminate(self):
        self._is_running = False
        self.stop_all()


class VoiceRecognizer:
    """
    PyAudio 直接制御 + RMS-VAD による音声認識。
    whisper モデルは外部から渡す（バックグラウンドでロード済み）。
    マイクが存在しない環境でも例外を出さずに動作する。
    """
    def __init__(
        self,
        whisper_model,
        on_text,
        vad_threshold: int = DEFAULT_VAD_RMS,
        res_monitor=None,
    ) -> None:
        # 1. まず属性を初期化する
        self.whisper_model = whisper_model
        self.on_text = on_text
        self.vad_threshold = vad_threshold
        self.res_monitor = res_monitor

        # 2. スレッド制御用のフラグを定義（★ここが重要）
        self._enabled = threading.Event()
        self._active = True
        self._tts_active = False
        self._tts_generation = 0
        # マイクOFF/停止より前に開始した録音・認識結果を識別する。
        self._recognition_generation = 0
        self._flush_request = False
        self._mic_read_error_last_log = None
        self._mic_read_error_active = False

        # 3. コールバックの初期化
        self.on_idle = None
        self.on_listening = None
        self.on_processing = None

        # 初期状態は無効に設定
        self._enabled.clear()

        # 4. モデルがある場合のみスレッドを開始
        if self.whisper_model is not None:
            threading.Thread(target=self._loop, daemon=True).start()
        else:
            print("[VoiceRecognizer] Whisper未ロードのためスレッドを開始しません。")
    @property
    def enabled(self) -> bool:
        return self._enabled.is_set()

    @enabled.setter
    def enabled(self, v: bool) -> None:
        if v:
            self._flush_request = True  # 復帰時にバッファをフラッシュ
            self._enabled.set()
        else:
            self._recognition_generation += 1
            self._enabled.clear()

    def is_recognition_current(self, generation: int) -> bool:
        """指定した録音世代が、現在も送信可能かを返す。"""
        return (
            self._active
            and self._enabled.is_set()
            and generation == self._recognition_generation
        )

    @staticmethod
    def _rms(data: bytes) -> float:
        n = len(data) // 2
        if n == 0:
            return 0.0
        shorts = struct.unpack(f"{n}h", data[: n * 2])
        return math.sqrt(sum(s * s for s in shorts) / n)

    def _fire(self, cb) -> None:
        """コールバックを安全に呼ぶ"""
        if cb:
            try:
                cb()
            except Exception:
                pass

    def mark_tts_started(self) -> None:
        """TTS開始を記録し、Whisper解析との一時的な重複も検出可能にする。"""
        self._tts_generation += 1
        self._tts_active = True

    def _tts_interfered_since(self, generation: int) -> bool:
        """指定時点以降にTTSが開始したか、現在発話中ならTrueを返す。"""
        return self._tts_active or self._tts_generation != generation

    def finish_tts_if_generation(self, generation: int) -> bool:
        """新しいTTSが始まっていない場合だけマイク抑制を解除する。"""
        if self._tts_generation != generation:
            return False
        self._tts_active = False
        return True

    def _record_mic_read_error(self, error: Exception) -> None:
        """マイク読取障害を秘密情報なし・間隔制限付きで記録する。"""
        now = time.monotonic()
        last = getattr(self, "_mic_read_error_last_log", None)
        if last is None or now - last >= MIC_READ_ERROR_LOG_INTERVAL_SECONDS:
            print(f"[AudioInput] read failed: {type(error).__name__}")
            self._mic_read_error_last_log = now
        self._mic_read_error_active = True

    def _record_mic_read_success(self) -> None:
        if getattr(self, "_mic_read_error_active", False):
            print("[AudioInput] read recovered")
            self._mic_read_error_active = False
            self._mic_read_error_last_log = None

    def _wait_for_speech_onset(self, stream) -> list[bytes] | None:
        """発話確定(RMSがVAD_SPEECH_CHUNKS連続で閾値超え)まで待機し、
        プリロール(直近VAD_PREROLL_CHUNKS分、発話確定前の低RMSチャンクも含む)
        込みの初期フレーム列を返す。ループが無効化・停止された場合はNoneを返す。"""
        consecutive = 0
        pre_buf: collections.deque = collections.deque(maxlen=VAD_PREROLL_CHUNKS)
        while self._active and self._enabled.is_set():
            try:
                chunk = stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                self._record_mic_read_success()
            except Exception as exc:
                self._record_mic_read_error(exc)
                time.sleep(0.05)
                continue
            # TTS発話中はバッファを蓄積せず即スキップ
            # （閾値10倍では開放型イヤホン環境でTTS音声を拾ってしまうため）
            if self._tts_active:
                consecutive = 0
                pre_buf.clear()
                continue
            pre_buf.append(chunk)
            rms = self._rms(chunk)
            if rms > self.vad_threshold:
                consecutive += 1
                if consecutive >= VAD_SPEECH_CHUNKS:
                    print(f"[VAD] 発話検知 RMS={rms:.1f} threshold={self.vad_threshold}")
                    return list(pre_buf)
            else:
                consecutive = 0
        return None

    def _loop(self) -> None:
        # Bluetoothデバイスが既定の場合、接続確立を待つ
        time.sleep(2.0)
        if not _PYAUDIO_AVAILABLE:
            print("[マイク初期化エラー] pyaudio 未インストール  (音声認識は無効になります)")
            return
        pa = pyaudio.PyAudio()
        try:
            device = pa.get_default_input_device_info()
            print(
                "[AudioInput] "
                f"index={device.get('index', 'n/a')} "
                f"name='{device.get('name', 'unknown')}' "
                f"channels={device.get('maxInputChannels', 'n/a')} "
                f"default_rate={device.get('defaultSampleRate', 'n/a')}"
            )
        except Exception as e:
            print(f"[AudioInput] 既定入力デバイス情報を取得できません: {e}")
        try:
            stream = pa.open(
                format=AUDIO_FORMAT,
                channels=1,
                rate=AUDIO_RATE,
                input=True,
                frames_per_buffer=AUDIO_CHUNK,
            )
        except Exception as e:
            print(f"[マイク初期化エラー] {e}  (音声認識は無効になります)")
            pa.terminate()
            return

        try:
            _last_text      = ""
            _last_text_time = 0.0
            _SAME_TEXT_EXPIRE = 10.0  # 同一テキストの有効期限（秒）
            while self._active:
                if not self._enabled.is_set():
                    self._fire(self.on_idle)
                    time.sleep(0.1)
                    continue

                self._fire(self.on_idle)

                # マイク復帰直後はバッファをフラッシュして古い音を捨てる
                if self._flush_request:
                    self._flush_request = False
                    try:
                        for _ in range(8):  # 約500ms分を読み捨て
                            stream.read(AUDIO_CHUNK, exception_on_overflow=False)
                        self._record_mic_read_success()
                    except Exception as exc:
                        self._record_mic_read_error(exc)

                # ── 発話開始待ち ─────────────────────
                pre_frames = self._wait_for_speech_onset(stream)
                if pre_frames is None:
                    continue
                recognition_generation = self._recognition_generation

                self._fire(self.on_listening)

                # ── 発話録音 ─────────────────────────
                frames        = pre_frames
                silence_count = 0
                max_chunks    = VAD_MAX_SECONDS * AUDIO_RATE // AUDIO_CHUNK

                while (
                    self.is_recognition_current(recognition_generation)
                    and len(frames) < max_chunks
                ):
                    try:
                        chunk = stream.read(
                            AUDIO_CHUNK, exception_on_overflow=False)
                        self._record_mic_read_success()
                    except Exception as exc:
                        self._record_mic_read_error(exc)
                        break
                    # 録音中にTTSが始まったら録音データを破棄してやり直し
                    if self._tts_active:
                        frames = []
                        break
                    frames.append(chunk)
                    if self._rms(chunk) < self.vad_threshold:
                        silence_count += 1
                        if silence_count >= VAD_SILENCE_CHUNKS:
                            break
                    else:
                        silence_count = 0

                # TTS中に録音が破棄された場合はWhisper処理をスキップ
                if not frames:
                    continue

                if not self.is_recognition_current(recognition_generation):
                    continue

                rms_values = [self._rms(frame) for frame in frames]
                audio_seconds = len(frames) * AUDIO_CHUNK / AUDIO_RATE
                rms_average = sum(rms_values) / len(rms_values)
                rms_maximum = max(rms_values)

                self._fire(self.on_processing)

                # ── Whisper 認識 ──────────────────────
                if self.whisper_model is None:
                    continue
                tts_generation = self._tts_generation
                if self._tts_interfered_since(tts_generation):
                    print(
                        "[WhisperDiag] "
                        f"audio_sec={audio_seconds:.2f} "
                        f"rms_avg={rms_average:.1f} rms_max={rms_maximum:.1f} "
                        "decision=discard reason=tts_active_before_transcribe"
                    )
                    continue

                audio_np = (
                    np.frombuffer(b"".join(frames), dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                try:
                    _t = time.time()
                    transcribe_guarded = getattr(
                        self.whisper_model, "transcribe_guarded", None)
                    transcribe_kwargs = dict(
                        language="ja",
                        no_speech_threshold=0.8,
                        logprob_threshold=-1.5,
                        condition_on_previous_text=False,
                        beam_size=1,
                        best_of=1,
                        temperature=0.0,
                    )
                    if callable(transcribe_guarded):
                        guarded = transcribe_guarded(
                            self.res_monitor, audio_np, **transcribe_kwargs)
                        if guarded is None:
                            print(
                                "[WhisperDiag] decision=discard "
                                "reason=llm_reload_pause"
                            )
                            continue
                        res, _on_gpu = guarded
                    else:
                        _model, _on_gpu = self.whisper_model.get_model(
                            self.res_monitor)
                        res = _model.transcribe(
                            audio_np, fp16=_on_gpu, **transcribe_kwargs)
                    text = res.get("text", "").strip()
                    segments = res.get("segments") or []
                    no_speech_avg, no_speech_max = _segment_metric_summary(
                        segments, "no_speech_prob")
                    logprob_avg, logprob_min = _segment_metric_summary(
                        segments, "avg_logprob")
                    compression_avg, compression_max = _segment_metric_summary(
                        segments, "compression_ratio")
                    print(_format_whisper_result_log(
                        time.time() - _t, self._tts_active, text))
                    print(
                        "[WhisperDiag] "
                        f"audio_sec={audio_seconds:.2f} "
                        f"rms_avg={rms_average:.1f} rms_max={rms_maximum:.1f} "
                        f"model={'GPU' if _on_gpu else 'CPU'} "
                        f"whisper_model={getattr(self.whisper_model, 'active_model_name', 'unknown')} "
                        f"segments={len(segments)} "
                        f"no_speech_avg={no_speech_avg} no_speech_max={no_speech_max} "
                        f"avg_logprob_avg={logprob_avg} avg_logprob_min={logprob_min} "
                        f"compression_avg={compression_avg} "
                        f"compression_max={compression_max}"
                    )
                    if self._tts_interfered_since(tts_generation):
                        print("[WhisperDiag] decision=discard reason=tts_started_during_transcribe")
                        continue
                    if not self.is_recognition_current(recognition_generation):
                        print("[WhisperDiag] decision=discard reason=mic_disabled_during_transcribe")
                        continue
                    noise_reason = classify_recognition_quality(text, segments)
                    if noise_reason is None and not text:
                        noise_reason = "empty_text"
                    elif noise_reason is None and len(text) < 2:
                        noise_reason = "too_short"
                    elif noise_reason is None and len(text) > 100:
                        noise_reason = "too_long"
                    elif noise_reason is None and text in WHISPER_NOISE:
                        noise_reason = "exact_noise_phrase"
                    elif noise_reason is None and all(c in "。、…　 " for c in text):
                        noise_reason = "punctuation_only"
                    elif noise_reason is None and any(
                            p in text for p in WHISPER_NOISE_PARTIAL):
                        noise_reason = "partial_noise_phrase"

                    if noise_reason:
                        print(f"[WhisperDiag] decision=discard reason={noise_reason}")
                    else:
                        now = time.time()
                        # 同一テキストでも一定時間経過後は有効とする
                        if (text != _last_text
                                or now - _last_text_time > _SAME_TEXT_EXPIRE):
                            if self._tts_interfered_since(tts_generation):
                                print("[WhisperDiag] decision=discard reason=tts_started_before_send")
                                continue
                            if not self.is_recognition_current(recognition_generation):
                                print("[WhisperDiag] decision=discard reason=mic_disabled_before_send")
                                continue
                            _last_text      = text
                            _last_text_time = now
                            print("[WhisperDiag] decision=send reason=accepted")
                            callback = getattr(self, "on_text_generation", None)
                            if callback is not None:
                                callback(text, recognition_generation)
                            else:
                                self.on_text(text)
                        else:
                            print("[WhisperDiag] decision=discard reason=recent_duplicate")
                except Exception as e:
                    print(f"[Whisper エラー] {e}")
        finally:
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass
            pa.terminate()

    def stop(self) -> None:
        self._recognition_generation += 1
        self._enabled.clear()
        self._active = False
