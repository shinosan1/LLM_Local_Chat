import contextlib
import io
import unittest

from audio_workers import TTSWorker, _format_whisper_result_log


class _FakeSpeaker:
    def __init__(self):
        self.Rate = None
        self.speak_calls = []

    def Speak(self, text, flags):
        self.speak_calls.append((text, flags))

    def WaitUntilDone(self, timeout_ms):
        return -1


class AudioLogPrivacyTests(unittest.TestCase):
    def test_tts_log_omits_text_but_keeps_diagnostics(self):
        worker = TTSWorker.__new__(TTSWorker)
        worker.rate = 0
        worker._stop_flag = False
        speaker = _FakeSpeaker()
        secret = "ログへ出してはいけない会話本文"
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            worker._execute_sapi_speak(speaker, secret)

        log = output.getvalue()
        self.assertNotIn(secret, log)
        self.assertIn(f"chars={len(secret)}", log)
        self.assertEqual(speaker.speak_calls, [(secret, 1)])

    def test_whisper_log_omits_text_but_keeps_diagnostics(self):
        secret = "認識された機密性のある会話本文"

        log = _format_whisper_result_log(1.25, False, secret)

        self.assertNotIn(secret, log)
        self.assertEqual(log, f"[Whisper] 1.2秒 tts_active=False chars={len(secret)}")


if __name__ == "__main__":
    unittest.main()
