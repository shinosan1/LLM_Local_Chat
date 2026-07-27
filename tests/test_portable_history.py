import json
import hashlib
import os
import tempfile
import unittest
from unittest.mock import patch

from history_crypto import HistoryCryptoError
from portable_history import (
    KDF_N,
    MAX_ARCHIVE_BYTES,
    PortableHistoryError,
    export_archive,
    import_archive,
    session_digest,
)
from session_store import SessionStore


class FakeProtector:
    def protect(self, data):
        return b"protected:" + data[::-1]

    def unprotect(self, data):
        if not data.startswith(b"protected:"):
            raise HistoryCryptoError("invalid ciphertext")
        return data[len(b"protected:"):][::-1]


def sample_session(title="秘密"):
    return {
        "title": title,
        "summary": "要約",
        "history": [{"user": "質問", "assistant": "回答"}],
        "last_activity_at": "2026-07-27T00:00:00+00:00",
        "future_compatible": {"flag": True, "items": [1, 2]},
    }


class PortableArchiveTests(unittest.TestCase):
    def test_roundtrip_preserves_valid_additional_fields(self):
        session = sample_session()
        raw = export_archive(
            [session],
            "correct horse battery staple",
            created_at="2026-07-27T00:00:00+00:00",
            random_bytes=lambda size: b"x" * size,
        )
        self.assertEqual(
            import_archive(raw, "correct horse battery staple"),
            [session],
        )
        self.assertNotIn("質問".encode("utf-8"), raw)
        self.assertNotIn(b"future_compatible", raw)

    def test_random_salt_and_nonce_change_ciphertext(self):
        with patch(
            "portable_history.uuid.uuid4",
            return_value=__import__("uuid").UUID(int=1),
        ):
            first = export_archive([sample_session()], "long passphrase")
            second = export_archive([sample_session()], "long passphrase")
        self.assertNotEqual(first, second)

    def test_fixed_inputs_produce_deterministic_vector(self):
        fixed_uuid = __import__("uuid").UUID(int=1)
        with patch("portable_history.uuid.uuid4", return_value=fixed_uuid):
            first = export_archive(
                [sample_session()],
                "long passphrase",
                created_at="2026-07-27T00:00:00+00:00",
                random_bytes=lambda size: bytes(range(size)),
            )
            second = export_archive(
                [sample_session()],
                "long passphrase",
                created_at="2026-07-27T00:00:00+00:00",
                random_bytes=lambda size: bytes(range(size)),
            )
        self.assertEqual(first, second)
        self.assertEqual(
            hashlib.sha256(first).hexdigest(),
            "192bc9404a6dfce66f50cca1915940632286b1900a54ba3185c7138bec959ac6",
        )
        self.assertEqual(import_archive(first, "long passphrase"), [
            sample_session()
        ])

    def test_wrong_passphrase_and_ciphertext_tamper_are_rejected(self):
        raw = export_archive([sample_session()], "correct passphrase")
        with self.assertRaisesRegex(
            PortableHistoryError, "パスフレーズが違うか"
        ):
            import_archive(raw, "wrong passphrase")
        envelope = json.loads(raw)
        text = envelope["ciphertext"]
        envelope["ciphertext"] = ("A" if text[0] != "A" else "B") + text[1:]
        with self.assertRaisesRegex(
            PortableHistoryError, "パスフレーズが違うか"
        ):
            import_archive(
                json.dumps(envelope, separators=(",", ":")).encode(),
                "correct passphrase",
            )

    def test_aad_header_tamper_is_rejected_before_or_during_auth(self):
        raw = export_archive([sample_session()], "correct passphrase")
        envelope = json.loads(raw)
        envelope["kdf"]["n"] = KDF_N * 2
        with self.assertRaisesRegex(PortableHistoryError, "KDF"):
            import_archive(
                json.dumps(envelope, separators=(",", ":")).encode(),
                "correct passphrase",
            )
        envelope = json.loads(raw)
        envelope["cipher"]["nonce"] = "AAAA"
        with self.assertRaises(PortableHistoryError):
            import_archive(
                json.dumps(envelope, separators=(",", ":")).encode(),
                "correct passphrase",
            )

    def test_exact_outer_schema_and_size_limit(self):
        raw = export_archive([sample_session()], "correct passphrase")
        envelope = json.loads(raw)
        envelope["extra"] = 1
        with self.assertRaises(PortableHistoryError):
            import_archive(json.dumps(envelope).encode(), "correct passphrase")
        with self.assertRaises(PortableHistoryError):
            import_archive(b"x" * (MAX_ARCHIVE_BYTES + 1), "passphrase")

    def test_duplicate_digest_is_deterministic(self):
        first = sample_session()
        second = json.loads(json.dumps(first, ensure_ascii=False))
        self.assertEqual(session_digest(first), session_digest(second))
        second["last_activity_at"] = "2026-07-28T00:00:00+00:00"
        self.assertNotEqual(session_digest(first), session_digest(second))


class PortableSessionStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="portable_store_")
        self.store = SessionStore(
            self.temp.name, protector=FakeProtector())

    def tearDown(self):
        self.temp.cleanup()

    def test_import_preserves_activity_and_additional_fields(self):
        session = sample_session()
        paths, skipped = self.store.import_sessions([session])
        self.assertEqual(skipped, 0)
        self.assertEqual(len(paths), 1)
        self.assertEqual(self.store.load(paths[0]), session)

    def test_duplicate_default_skip_and_explicit_copy(self):
        session = sample_session()
        first, _ = self.store.import_sessions([session])
        skipped_paths, skipped = self.store.import_sessions([session])
        copied_paths, copied_skipped = self.store.import_sessions(
            [session], skip_duplicates=False)
        self.assertEqual(len(first), 1)
        self.assertEqual(skipped_paths, [])
        self.assertEqual(skipped, 1)
        self.assertEqual(len(copied_paths), 1)
        self.assertEqual(copied_skipped, 0)

    def test_failure_rolls_back_new_files_and_preserves_existing(self):
        existing, _ = self.store.import_sessions([sample_session("existing")])
        with open(existing[0], "rb") as handle:
            before = handle.read()
        original = self.store._write_encrypted
        calls = 0

        def fail_second(path, session):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk failure")
            return original(path, session)

        self.store._write_encrypted = fail_second
        with self.assertRaises(OSError):
            self.store.import_sessions([
                sample_session("one"),
                sample_session("two"),
            ])
        with open(existing[0], "rb") as handle:
            self.assertEqual(handle.read(), before)
        self.assertFalse(any(
            name.startswith(".import.")
            for name in os.listdir(self.temp.name)
        ))

    def test_readback_failure_removes_encrypted_staging_file(self):
        original = self.store._read_document

        def fail_import_stage(path):
            if os.path.basename(path).startswith(".import."):
                raise HistoryCryptoError("readback failure")
            return original(path)

        self.store._read_document = fail_import_stage
        with self.assertRaises(HistoryCryptoError):
            self.store.import_sessions([sample_session("new")])
        self.assertFalse(any(
            name.startswith(".import.")
            for name in os.listdir(self.temp.name)
        ))

    def test_commit_failure_removes_all_staging_files(self):
        with patch("session_store.os.replace", side_effect=OSError("commit")):
            with self.assertRaises(OSError):
                self.store.import_sessions([sample_session("new")])
        self.assertEqual(os.listdir(self.temp.name), [])


if __name__ == "__main__":
    unittest.main()
