import base64
import binascii
import copy
import datetime
import hashlib
import json
import os
import uuid

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from history_crypto import HistoryCryptoError, validate_session


OUTER_FORMAT = "shiro-portable-export"
OUTER_VERSION = 1
INNER_SCHEMA = "shiro-portable-history"
INNER_VERSION = 1
KDF_NAME = "scrypt"
KDF_N = 32768
KDF_R = 8
KDF_P = 1
SALT_BYTES = 16
NONCE_BYTES = 12
KEY_BYTES = 32
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_PLAINTEXT_BYTES = 128 * 1024 * 1024
MAX_SESSIONS = 10_000
MAX_TURNS = 100_000
MAX_TEXT_CHARS = 4_000_000
MAX_JSON_DEPTH = 32


class PortableHistoryError(RuntimeError):
    pass


def portable_session_snapshot(session: dict) -> dict:
    """Return an export-safe copy without local-only attachment references."""
    validate_portable_session(session)
    snapshot = copy.deepcopy(session)
    snapshot.pop("session_id", None)
    snapshot.pop("attachments", None)
    return snapshot


def _canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _b64decode(value, expected_bytes=None, max_bytes=None) -> bytes:
    if not isinstance(value, str):
        raise PortableHistoryError("暗号化ファイルの形式が不正です。")
    if max_bytes is not None and len(value) > ((max_bytes + 2) // 3) * 4 + 4:
        raise PortableHistoryError("暗号化ファイルのサイズが上限を超えています。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise PortableHistoryError("暗号化ファイルの形式が不正です。") from exc
    if expected_bytes is not None and len(decoded) != expected_bytes:
        raise PortableHistoryError("暗号化ファイルの形式が不正です。")
    if max_bytes is not None and len(decoded) > max_bytes:
        raise PortableHistoryError("暗号化ファイルのサイズが上限を超えています。")
    return decoded


def _check_json_tree(value, depth=0) -> None:
    if depth > MAX_JSON_DEPTH:
        raise PortableHistoryError("会話履歴の入れ子が深すぎます。")
    if value is None or isinstance(value, (str, int, float, bool)):
        return
    if isinstance(value, list):
        for item in value:
            _check_json_tree(item, depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise PortableHistoryError("会話履歴のキー形式が不正です。")
            _check_json_tree(item, depth + 1)
        return
    raise PortableHistoryError("会話履歴に保存できない値が含まれています。")


def validate_portable_session(session) -> dict:
    try:
        validate_session(session)
    except HistoryCryptoError as exc:
        raise PortableHistoryError(str(exc)) from exc
    _check_json_tree(session)
    for field in ("title", "summary"):
        if len(session.get(field, "")) > MAX_TEXT_CHARS:
            raise PortableHistoryError("会話履歴のテキストが長すぎます。")
    activity = session.get("last_activity_at")
    if activity is not None and not isinstance(activity, str):
        raise PortableHistoryError("会話履歴の日時形式が不正です。")
    if activity:
        try:
            parsed_activity = datetime.datetime.fromisoformat(activity)
        except ValueError as exc:
            raise PortableHistoryError("会話履歴の日時形式が不正です。") from exc
        if parsed_activity.tzinfo is None:
            raise PortableHistoryError("会話履歴の日時にはタイムゾーンが必要です。")
    history = session.get("history", [])
    if len(history) > MAX_TURNS:
        raise PortableHistoryError("会話履歴の件数が上限を超えています。")
    for turn in history:
        for field in ("user", "assistant"):
            if len(turn.get(field, "")) > MAX_TEXT_CHARS:
                raise PortableHistoryError("会話履歴の発言が長すぎます。")
    try:
        raw = _canonical(session)
    except (TypeError, ValueError) as exc:
        raise PortableHistoryError("会話履歴の形式が不正です。") from exc
    if len(raw) > MAX_PLAINTEXT_BYTES:
        raise PortableHistoryError("会話履歴のサイズが上限を超えています。")
    return session


def session_digest(session: dict) -> str:
    return hashlib.sha256(_canonical(portable_session_snapshot(session))).hexdigest()


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise PortableHistoryError("パスフレーズを入力してください。")
    return Scrypt(
        salt=salt,
        length=KEY_BYTES,
        n=KDF_N,
        r=KDF_R,
        p=KDF_P,
    ).derive(passphrase.encode("utf-8"))


def export_archive(
    sessions: list[dict],
    passphrase: str,
    *,
    created_at: str | None = None,
    random_bytes=os.urandom,
) -> bytes:
    if not isinstance(passphrase, str) or len(passphrase) < 12:
        raise PortableHistoryError(
            "パスフレーズは12文字以上にしてください。")
    if not isinstance(sessions, list) or not sessions:
        raise PortableHistoryError("エクスポートする会話履歴がありません。")
    if len(sessions) > MAX_SESSIONS:
        raise PortableHistoryError("会話履歴の件数が上限を超えています。")
    records = []
    for session in sessions:
        portable_session = portable_session_snapshot(session)
        records.append({
            "record_id": str(uuid.uuid4()),
            "session": portable_session,
        })
    payload = {
        "schema": INNER_SCHEMA,
        "version": INNER_VERSION,
        "created_at": created_at or datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat(),
        "sessions": records,
    }
    plaintext = _canonical(payload)
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise PortableHistoryError("エクスポート内容が上限を超えています。")
    salt = random_bytes(SALT_BYTES)
    nonce = random_bytes(NONCE_BYTES)
    if len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES:
        raise PortableHistoryError("安全な乱数を生成できませんでした。")
    header = {
        "format": OUTER_FORMAT,
        "version": OUTER_VERSION,
        "kdf": {
            "name": KDF_NAME,
            "n": KDF_N,
            "r": KDF_R,
            "p": KDF_P,
            "salt": _b64encode(salt),
        },
        "cipher": {
            "name": "AES-256-GCM",
            "nonce": _b64encode(nonce),
        },
    }
    key = _derive_key(passphrase, salt)
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, _canonical(header))
    envelope = {**header, "ciphertext": _b64encode(ciphertext)}
    raw = _canonical(envelope)
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise PortableHistoryError("暗号化ファイルのサイズが上限を超えています。")
    return raw


def import_archive(raw: bytes, passphrase: str) -> list[dict]:
    if not isinstance(raw, bytes) or len(raw) > MAX_ARCHIVE_BYTES:
        raise PortableHistoryError("暗号化ファイルのサイズが上限を超えています。")
    try:
        envelope = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise PortableHistoryError("暗号化ファイルの形式が不正です。") from exc
    if not isinstance(envelope, dict) or set(envelope) != {
        "format", "version", "kdf", "cipher", "ciphertext"
    }:
        raise PortableHistoryError("暗号化ファイルの形式が不正です。")
    if (
        envelope["format"] != OUTER_FORMAT
        or type(envelope["version"]) is not int
        or envelope["version"] != OUTER_VERSION
    ):
        raise PortableHistoryError("未対応の暗号化ファイルです。")
    kdf = envelope["kdf"]
    cipher = envelope["cipher"]
    if not isinstance(kdf, dict) or set(kdf) != {
        "name", "n", "r", "p", "salt"
    }:
        raise PortableHistoryError("暗号化ファイルのKDF設定が不正です。")
    if (
        kdf.get("name") != KDF_NAME
        or type(kdf.get("n")) is not int or kdf["n"] != KDF_N
        or type(kdf.get("r")) is not int or kdf["r"] != KDF_R
        or type(kdf.get("p")) is not int or kdf["p"] != KDF_P
    ):
        raise PortableHistoryError("未対応のKDF設定です。")
    if not isinstance(cipher, dict) or set(cipher) != {"name", "nonce"}:
        raise PortableHistoryError("暗号化ファイルの暗号設定が不正です。")
    if cipher.get("name") != "AES-256-GCM":
        raise PortableHistoryError("未対応の暗号方式です。")
    salt = _b64decode(kdf["salt"], expected_bytes=SALT_BYTES)
    nonce = _b64decode(cipher["nonce"], expected_bytes=NONCE_BYTES)
    ciphertext = _b64decode(
        envelope["ciphertext"], max_bytes=MAX_PLAINTEXT_BYTES + 16
    )
    header = {
        "format": envelope["format"],
        "version": envelope["version"],
        "kdf": kdf,
        "cipher": cipher,
    }
    key = _derive_key(passphrase, salt)
    try:
        plaintext = AESGCM(key).decrypt(
            nonce, ciphertext, _canonical(header)
        )
    except InvalidTag as exc:
        raise PortableHistoryError(
            "パスフレーズが違うか、暗号化ファイルが破損しています。"
        ) from exc
    if len(plaintext) > MAX_PLAINTEXT_BYTES:
        raise PortableHistoryError("復号内容のサイズが上限を超えています。")
    try:
        payload = json.loads(plaintext.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PortableHistoryError("復号した履歴の形式が不正です。") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schema", "version", "created_at", "sessions"
    }:
        raise PortableHistoryError("復号した履歴の形式が不正です。")
    if (
        payload["schema"] != INNER_SCHEMA
        or type(payload["version"]) is not int
        or payload["version"] != INNER_VERSION
        or not isinstance(payload["created_at"], str)
        or not isinstance(payload["sessions"], list)
        or len(payload["sessions"]) > MAX_SESSIONS
    ):
        raise PortableHistoryError("未対応の履歴データ形式です。")
    try:
        created_at = datetime.datetime.fromisoformat(payload["created_at"])
    except ValueError as exc:
        raise PortableHistoryError("履歴データの作成日時が不正です。") from exc
    if created_at.tzinfo is None:
        raise PortableHistoryError("履歴データの作成日時が不正です。")
    sessions = []
    record_ids = set()
    for record in payload["sessions"]:
        if not isinstance(record, dict) or set(record) != {
            "record_id", "session"
        }:
            raise PortableHistoryError("履歴レコードの形式が不正です。")
        try:
            record_id = str(uuid.UUID(record["record_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise PortableHistoryError("履歴レコードIDが不正です。") from exc
        if record_id in record_ids:
            raise PortableHistoryError("履歴レコードIDが重複しています。")
        record_ids.add(record_id)
        sessions.append(portable_session_snapshot(record["session"]))
    if not sessions:
        raise PortableHistoryError("暗号化ファイルに会話履歴がありません。")
    return sessions
