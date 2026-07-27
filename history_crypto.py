import base64
import json
from typing import Protocol


ENVELOPE_FORMAT = "llm-local-chat-dpapi"
ENVELOPE_VERSION = 1
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class HistoryCryptoError(RuntimeError):
    pass


class HistoryProtector(Protocol):
    def protect(self, data: bytes) -> bytes: ...
    def unprotect(self, data: bytes) -> bytes: ...


class DPAPIProtector:
    """Windowsの現在ユーザースコープで履歴を保護する。"""

    def __init__(self):
        try:
            import win32crypt
        except ImportError as exc:
            raise HistoryCryptoError(
                "Windows DPAPIを利用できません。pywin32を確認してください。"
            ) from exc
        self._win32crypt = win32crypt

    def protect(self, data: bytes) -> bytes:
        try:
            return self._win32crypt.CryptProtectData(
                data, None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN
            )
        except Exception as exc:
            raise HistoryCryptoError("会話履歴を暗号化できませんでした。") from exc

    def unprotect(self, data: bytes) -> bytes:
        try:
            _description, plaintext = self._win32crypt.CryptUnprotectData(
                data, None, None, None, CRYPTPROTECT_UI_FORBIDDEN
            )
            return plaintext
        except Exception as exc:
            raise HistoryCryptoError(
                "会話履歴を復号できません。Windowsユーザーを確認してください。"
            ) from exc


def validate_session(data) -> dict:
    if not isinstance(data, dict):
        raise HistoryCryptoError("会話履歴の形式が不正です。")
    if not isinstance(data.get("title", ""), str):
        raise HistoryCryptoError("会話履歴のタイトル形式が不正です。")
    if not isinstance(data.get("summary", ""), str):
        raise HistoryCryptoError("会話履歴の要約形式が不正です。")
    history = data.get("history", [])
    if not isinstance(history, list):
        raise HistoryCryptoError("会話履歴の本文形式が不正です。")
    for item in history:
        if not isinstance(item, dict):
            raise HistoryCryptoError("会話履歴の発言形式が不正です。")
        for field in ("user", "assistant"):
            if field in item and not isinstance(item[field], str):
                raise HistoryCryptoError("会話履歴の発言形式が不正です。")
    return data


def parse_session_bytes(raw: bytes) -> dict:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCryptoError("会話履歴JSONが破損しています。") from exc
    return validate_session(data)


def encode_envelope(raw: bytes, protector: HistoryProtector) -> bytes:
    ciphertext = protector.protect(raw)
    envelope = {
        "format": ENVELOPE_FORMAT,
        "version": ENVELOPE_VERSION,
        "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
    }
    return json.dumps(
        envelope, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")


def decode_document(
    raw: bytes, protector: HistoryProtector
) -> tuple[dict, bool, bytes]:
    try:
        outer = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HistoryCryptoError("会話履歴ファイルが破損しています。") from exc

    if isinstance(outer, dict) and "format" in outer:
        if outer.get("format") != ENVELOPE_FORMAT:
            raise HistoryCryptoError("未対応の会話履歴形式です。")
        if outer.get("version") != ENVELOPE_VERSION:
            raise HistoryCryptoError("未対応の会話履歴バージョンです。")
        ciphertext_text = outer.get("ciphertext")
        if not isinstance(ciphertext_text, str):
            raise HistoryCryptoError("暗号化会話履歴の形式が不正です。")
        try:
            ciphertext = base64.b64decode(ciphertext_text, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise HistoryCryptoError("暗号化会話履歴が破損しています。") from exc
        plaintext = protector.unprotect(ciphertext)
        return parse_session_bytes(plaintext), True, plaintext

    return validate_session(outer), False, raw
