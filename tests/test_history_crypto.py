import json
import sys
import unittest

from history_crypto import (
    CRYPTPROTECT_UI_FORBIDDEN,
    ENVELOPE_FORMAT,
    DPAPIProtector,
    HistoryCryptoError,
    decode_document,
    encode_envelope,
)


class FakeProtector:
    def protect(self, data):
        return b"protected:" + data[::-1]

    def unprotect(self, data):
        if not data.startswith(b"protected:"):
            raise HistoryCryptoError("invalid ciphertext")
        return data[len(b"protected:"):][::-1]


class HistoryCryptoTests(unittest.TestCase):
    def setUp(self):
        self.protector = FakeProtector()
        self.raw = json.dumps(
            {
                "title": "秘密のタイトル",
                "summary": "秘密の要約",
                "history": [{"user": "秘密の質問", "assistant": "秘密の回答"}],
            },
            ensure_ascii=False,
        ).encode("utf-8")

    def test_sdk_flag_value_is_fixed(self):
        self.assertEqual(CRYPTPROTECT_UI_FORBIDDEN, 0x1)

    def test_envelope_roundtrip_and_plaintext_is_not_exposed(self):
        envelope = encode_envelope(self.raw, self.protector)
        self.assertNotIn("秘密".encode("utf-8"), envelope)
        data, encrypted, plaintext = decode_document(
            envelope, self.protector)
        self.assertTrue(encrypted)
        self.assertEqual(plaintext, self.raw)
        self.assertEqual(data["title"], "秘密のタイトル")
        self.assertEqual(json.loads(envelope)["format"], ENVELOPE_FORMAT)

    def test_legacy_document_is_identified_without_rewriting(self):
        data, encrypted, plaintext = decode_document(
            self.raw, self.protector)
        self.assertFalse(encrypted)
        self.assertEqual(plaintext, self.raw)
        self.assertEqual(data["summary"], "秘密の要約")

    def test_unknown_version_is_rejected(self):
        envelope = json.loads(encode_envelope(
            self.raw, self.protector).decode("utf-8"))
        envelope["version"] = 99
        with self.assertRaises(HistoryCryptoError):
            decode_document(json.dumps(envelope).encode(), self.protector)

    def test_invalid_base64_is_rejected(self):
        envelope = {
            "format": ENVELOPE_FORMAT,
            "version": 1,
            "ciphertext": "***",
        }
        with self.assertRaises(HistoryCryptoError):
            decode_document(json.dumps(envelope).encode(), self.protector)

    def test_decrypted_non_json_is_rejected(self):
        envelope = encode_envelope(b"not-json", self.protector)
        with self.assertRaises(HistoryCryptoError):
            decode_document(envelope, self.protector)

    @unittest.skipUnless(sys.platform == "win32", "Windows DPAPI専用")
    def test_real_windows_dpapi_roundtrip(self):
        protector = DPAPIProtector()
        ciphertext = protector.protect(self.raw)
        self.assertNotEqual(ciphertext, self.raw)
        self.assertEqual(protector.unprotect(ciphertext), self.raw)


if __name__ == "__main__":
    unittest.main()
