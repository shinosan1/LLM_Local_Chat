import math
import queue
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
    import pyttsx3
    _PYTTSX3_AVAILABLE = True
except ImportError:
    pyttsx3 = None
    _PYTTSX3_AVAILABLE = False
    print("[TTS] pyttsx3 未インストール: TTS を無効化します")

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
VAD_SPEECH_CHUNKS  = 6
VAD_SILENCE_CHUNKS = 30
VAD_MAX_SECONDS    = 6

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


class TTSWorker:
    def __init__(self, avatar, root):
        self.avatar = avatar
        self.root = root
        self._q = queue.Queue()
        self._stop_flag = False
        self._is_running = True
        self._ready = threading.Event()
        self.enabled = False  # 起動時はOFF・設定ファイルから後で反映される
        self.on_start = None
        self.on_stop  = None

        # 起動時の挨拶を一度だけ行うためのフラグ
        self._initial_greeting_done = False

        self._thread = threading.Thread(target=self._play_loop, daemon=True)
        self._thread.start()

    def speak(self, text):
        """ChatApp側から呼ばれる入り口"""
        if not self.enabled:
            return
        if text and text.strip():
            if not self._ready.is_set():
                self._ready.wait(timeout=2.0)
            self._q.put(text)
            if self.on_start:
                self.root.after(0, self.on_start)

    def _play_loop(self):
        """再生メインループ"""
        if _WIN32_AVAILABLE:
            pythoncom.CoInitialize()

        try:
            self._ready.set()

            while self._is_running:
                try:
                    text = self._q.get(timeout=0.1)

                    if not self.enabled:
                        try:
                            self._q.task_done()
                        except ValueError:
                            pass
                        continue

                    self._stop_flag = False

                    # 口パク開始
                    self.root.after(0, self.avatar.start_speaking)

                    # SAPI5再生実行
                    self._execute_sapi_speak(text)

                    # 口パク停止
                    self.root.after(0, self.avatar.stop_speaking)
                    if self.on_stop:
                        self.root.after(0, self.on_stop)

                    try:
                        self._q.task_done()
                    except ValueError:
                        pass

                except queue.Empty:
                    continue
                except Exception as e:
                    print(f"[TTS Loop Error] {e}")
        finally:
            if _WIN32_AVAILABLE:
                pythoncom.CoUninitialize()

    def _execute_sapi_speak(self, text):
        """SAPI5専用再生ロジック（中断対応版）"""
        if not _WIN32_AVAILABLE:
            return
        try:
            speaker = win32com.client.Dispatch("SAPI.SpVoice")

            # 1:Async, 2:PurgeBeforeSpeak
            speaker.Speak(text, 1 | 2)

            while speaker.Status.RunningState != 1: # 1:Finished
                if self._stop_flag:
                    speaker.Speak("", 3) # 強制停止
                    return

                pythoncom.PumpWaitingMessages()
                time.sleep(0.01)
        except Exception as e:
            print(f"[SAPI5 Error] {e}")

    def stop_all(self):
        """停止ボタン"""
        while not self._q.empty():
            try:
                self._q.get_nowait()
                self._q.task_done()
            except: break
        self._stop_flag = True

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
        self._flush_request = False

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
            self._enabled.clear()

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

    def _loop(self) -> None:
        # Bluetoothデバイスが既定の場合、接続確立を待つ
        time.sleep(2.0)
        if not _PYAUDIO_AVAILABLE:
            print("[マイク初期化エラー] pyaudio 未インストール  (音声認識は無効になります)")
            return
        pa = pyaudio.PyAudio()
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
                    except Exception:
                        pass

                # ── 発話開始待ち ─────────────────────
                consecutive = 0
                pre_buf: list[bytes] = []
                while self._active and self._enabled.is_set():
                    try:
                        chunk = stream.read(
                            AUDIO_CHUNK, exception_on_overflow=False)
                    except Exception:
                        time.sleep(0.05)
                        continue
                    # TTS発話中はバッファを蓄積せず即スキップ
                    # （閾値10倍では開放型イヤホン環境でTTS音声を拾ってしまうため）
                    if self._tts_active:
                        consecutive = 0
                        pre_buf.clear()
                        continue
                    pre_buf.append(chunk)
                    if len(pre_buf) > VAD_SPEECH_CHUNKS + 2:
                        pre_buf.pop(0)
                    rms = self._rms(chunk)
                    if rms > self.vad_threshold:
                        consecutive += 1
                        if consecutive >= VAD_SPEECH_CHUNKS:
                            print(f"[VAD] 発話検知 RMS={rms:.1f} threshold={self.vad_threshold}")
                            break
                    else:
                        consecutive = 0

                if not (self._active and self._enabled.is_set()):
                    continue

                self._fire(self.on_listening)

                # ── 発話録音 ─────────────────────────
                frames        = list(pre_buf)
                silence_count = 0
                max_chunks    = VAD_MAX_SECONDS * AUDIO_RATE // AUDIO_CHUNK

                while self._active and len(frames) < max_chunks:
                    try:
                        chunk = stream.read(
                            AUDIO_CHUNK, exception_on_overflow=False)
                    except Exception:
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

                self._fire(self.on_processing)

                # ── Whisper 認識 ──────────────────────
                if self.whisper_model is None:
                    continue

                audio_np = (
                    np.frombuffer(b"".join(frames), dtype=np.int16)
                    .astype(np.float32) / 32768.0
                )
                try:
                    _t = time.time()
                    # MODIFIED: WhisperPool がヒステリシス付きで GPU/CPU を切替
                    _model, _on_gpu = self.whisper_model.get_model(self.res_monitor)
                    res = _model.transcribe(
                        audio_np,
                        language="ja",
                        fp16=_on_gpu,
                        no_speech_threshold=0.8,
                        logprob_threshold=-1.5,
                        condition_on_previous_text=False,
                        initial_prompt="自己紹介、こんにちは、ありがとうございます。",
                        beam_size=1,
                        best_of=1,
                        temperature=0.0,
                    )
                    text = res.get("text", "").strip()
                    print(f"[Whisper] {time.time()-_t:.1f}秒 tts_active={self._tts_active} 結果='{text}'")
                    _noise = (
                        not text
                        or len(text) < 2
                        or len(text) > 100   # 100文字超はhallucination
                        or text in WHISPER_NOISE
                        or all(c in "。、…　 " for c in text)
                        or any(p in text for p in WHISPER_NOISE_PARTIAL)
                    )
                    if not _noise:
                        now = time.time()
                        # 同一テキストでも一定時間経過後は有効とする
                        if (text != _last_text
                                or now - _last_text_time > _SAME_TEXT_EXPIRE):
                            _last_text      = text
                            _last_text_time = now
                            self.on_text(text)
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
        self._active = False
