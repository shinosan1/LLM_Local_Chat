from __future__ import annotations

import base64
import hashlib
import os
from dataclasses import dataclass
from io import BytesIO
from uuid import uuid4

from PIL import Image, UnidentifiedImageError


TEXT_ATTACHMENT_EXTENSIONS = frozenset({".txt", ".md", ".json", ".csv"})
IMAGE_ATTACHMENT_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg"})
EXTERNAL_PROMPT_EXTENSIONS = frozenset({".txt", ".md"})

MAX_ATTACHMENTS = 8
MAX_IMAGE_ATTACHMENTS = 1
MAX_TEXT_ATTACHMENT_BYTES = 1024 * 1024
MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 4096 * 4096
MAX_EXTERNAL_PROMPT_BYTES = 1024 * 1024
# MTMDへ渡す画像トークン上限と、PromptBuilderが事前に確保する同一予算。
IMAGE_CONTEXT_TOKEN_RESERVE = 4096

_ATTACHMENT_SPECS = {
    ".txt": ("text", "text/plain", MAX_TEXT_ATTACHMENT_BYTES),
    ".md": ("text", "text/markdown", MAX_TEXT_ATTACHMENT_BYTES),
    ".json": ("text", "application/json", MAX_TEXT_ATTACHMENT_BYTES),
    ".csv": ("text", "text/csv", MAX_TEXT_ATTACHMENT_BYTES),
    ".png": ("image", "image/png", MAX_IMAGE_ATTACHMENT_BYTES),
    ".jpg": ("image", "image/jpeg", MAX_IMAGE_ATTACHMENT_BYTES),
    ".jpeg": ("image", "image/jpeg", MAX_IMAGE_ATTACHMENT_BYTES),
}


class PromptInputError(ValueError):
    """ユーザーへ表示できるローカル入力エラー。"""


@dataclass(frozen=True)
class Attachment:
    name: str
    kind: str
    mime_type: str
    text: str | None = None
    data: bytes | None = None
    attachment_id: str | None = None
    extension: str | None = None
    size: int | None = None
    sha256: str | None = None


def _safe_name(path: str) -> str:
    name = os.path.basename(path)
    cleaned = "".join(ch for ch in name if ch >= " " and ch not in "\r\n")
    return cleaned.strip() or "添付ファイル"


def _read_limited(path: str, limit: int, name: str) -> bytes:
    try:
        if not os.path.isfile(path):
            raise PromptInputError(f"ファイルが見つかりません: {name}")
        if os.path.getsize(path) > limit:
            raise PromptInputError(f"ファイルサイズが上限を超えています: {name}")
        with open(path, "rb") as file:
            data = file.read(limit + 1)
    except PromptInputError:
        raise
    except OSError as exc:
        raise PromptInputError(f"ファイルを読み込めません: {name}") from exc
    if len(data) > limit:
        raise PromptInputError(f"ファイルサイズが上限を超えています: {name}")
    return data


def _attachment_spec(name: str) -> tuple[str, str, int, str]:
    extension = os.path.splitext(name)[1].lower()
    spec = _ATTACHMENT_SPECS.get(extension)
    if spec is None:
        raise PromptInputError(f"対応していないファイル形式です: {name}")
    kind, mime_type, limit = spec
    return kind, mime_type, limit, extension


def _validated_attachment_id(attachment_id: str | None) -> str:
    if attachment_id is None:
        return uuid4().hex
    if not isinstance(attachment_id, str) or not attachment_id:
        raise PromptInputError("添付IDの形式が正しくありません。")
    return attachment_id


def load_attachment_bytes(
    name: str,
    data: bytes,
    *,
    attachment_id: str | None = None,
) -> Attachment:
    """検証済みsidecar等の生bytesから、実パスを持たない添付を復元する。"""
    safe_name = _safe_name(name)
    kind, mime_type, limit, extension = _attachment_spec(safe_name)
    if not isinstance(data, bytes):
        raise PromptInputError(f"ファイルを読み込めません: {safe_name}")
    if len(data) > limit:
        raise PromptInputError(f"ファイルサイズが上限を超えています: {safe_name}")
    content_hash = hashlib.sha256(data).hexdigest()

    if kind == "text":
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PromptInputError(
                f"UTF-8のテキストとして読み込めません: {safe_name}"
            ) from exc
        if not text:
            raise PromptInputError(f"ファイルが空です: {safe_name}")
        return Attachment(
            name=safe_name,
            kind="text",
            mime_type=mime_type,
            text=text,
            data=data,
            attachment_id=_validated_attachment_id(attachment_id),
            extension=extension,
            size=len(data),
            sha256=content_hash,
        )

    try:
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            image_format = (image.format or "").upper()
            expected_format = "PNG" if extension == ".png" else "JPEG"
            if image_format != expected_format:
                raise PromptInputError(
                    f"拡張子と画像形式が一致しません: {safe_name}"
                )
            if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                raise PromptInputError(
                    f"画像の画素数が上限を超えています: {safe_name}"
                )
            image.verify()
    except PromptInputError:
        raise
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise PromptInputError(f"画像を読み込めません: {safe_name}") from exc
    return Attachment(
        name=safe_name,
        kind="image",
        mime_type=mime_type,
        data=data,
        attachment_id=_validated_attachment_id(attachment_id),
        extension=extension,
        size=len(data),
        sha256=content_hash,
    )


def load_attachment(path: str) -> Attachment:
    """添付を上限内でメモリへ読み込む。実パスは返り値へ保持しない。"""
    name = _safe_name(path)
    _kind, _mime_type, limit, _extension = _attachment_spec(name)
    return load_attachment_bytes(name, _read_limited(path, limit, name))


def attachment_fingerprint(attachment: Attachment) -> str:
    """実パスを使わず、同じpayloadだけを重複判定できる値を返す。"""
    content_hash = attachment.sha256
    if not content_hash:
        if attachment.data is not None:
            content_hash = hashlib.sha256(attachment.data).hexdigest()
        else:
            content_hash = hashlib.sha256(
                (attachment.text or "").encode("utf-8")
            ).hexdigest()
    extension = attachment.extension or os.path.splitext(attachment.name)[1].lower()
    size = attachment.size
    if size is None:
        size = len(attachment.data) if attachment.data is not None else len(
            (attachment.text or "").encode("utf-8")
        )
    return f"{attachment.kind}:{extension}:{size}:{content_hash.lower()}"


def attachment_display_names(
    attachments: tuple[Attachment, ...] | list[Attachment],
) -> list[str]:
    """同名添付を入力順に data.csv / data.csv (2) と区別する。"""
    totals: dict[str, int] = {}
    for attachment in attachments:
        key = attachment.name.casefold()
        totals[key] = totals.get(key, 0) + 1
    seen: dict[str, int] = {}
    names = []
    for attachment in attachments:
        key = attachment.name.casefold()
        index = seen.get(key, 0) + 1
        seen[key] = index
        names.append(
            attachment.name if totals[key] == 1 or index == 1
            else f"{attachment.name} ({index})"
        )
    return names


def _unique_attachments(
    attachments: tuple[Attachment, ...] | list[Attachment],
) -> list[Attachment]:
    unique = []
    seen = set()
    for attachment in attachments:
        fingerprint = attachment_fingerprint(attachment)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(attachment)
    return unique


def validate_attachment_set(attachments: tuple[Attachment, ...] | list[Attachment]) -> None:
    if len(attachments) > MAX_ATTACHMENTS:
        raise PromptInputError(
            f"添付できるファイルは最大{MAX_ATTACHMENTS}件です。"
        )
    image_count = sum(attachment.kind == "image" for attachment in attachments)
    if image_count > MAX_IMAGE_ATTACHMENTS:
        raise PromptInputError("画像は1回の送信につき1枚まで添付できます。")


def format_text_attachment_input(
    user_text: str, attachments: tuple[Attachment, ...] | list[Attachment]
) -> str:
    blocks = []
    unique_attachments = _unique_attachments(attachments)
    for attachment, display_name in zip(
        unique_attachments, attachment_display_names(unique_attachments)
    ):
        if attachment.kind != "text":
            continue
        blocks.append(
            "[添付ファイル]\n"
            f"ファイル名: {display_name}\n\n"
            "--- ファイル内容 ---\n"
            f"{attachment.text or ''}\n"
            "--- ファイル内容ここまで ---"
        )
    if not blocks:
        return user_text
    return user_text + "\n\n" + "\n\n".join(blocks)


def build_multimodal_user_content(
    text: str, attachments: tuple[Attachment, ...] | list[Attachment]
) -> str | list[dict]:
    images = [
        attachment for attachment in _unique_attachments(attachments)
        if attachment.kind == "image"
    ]
    if not images:
        return text
    content: list[dict] = [{"type": "text", "text": text}]
    for attachment in images:
        encoded = base64.b64encode(attachment.data or b"").decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:{attachment.mime_type};base64,{encoded}",
            },
        })
    return content


def _inline_text(settings: dict, key: str, errors: list[str]) -> str:
    value = settings.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        errors.append(f"{key} は文字列で指定してください。")
        return ""
    return value.strip()


def _file_specs(value, key: str, errors: list[str]) -> list[str]:
    if value in (None, "", []):
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return value
    errors.append(f"external_prompt_files.{key} の形式が正しくありません。")
    return []


def _read_external_prompt(
    configured_path: str,
    app_dir: str,
    seen_paths: set[str],
    errors: list[str],
) -> str | None:
    display_name = _safe_name(configured_path)
    extension = os.path.splitext(display_name)[1].lower()
    if extension not in EXTERNAL_PROMPT_EXTENSIONS:
        errors.append(f"外部プロンプトは.txtまたは.mdのみ対応です: {display_name}")
        return None
    resolved = (
        configured_path
        if os.path.isabs(configured_path)
        else os.path.join(app_dir, configured_path)
    )
    resolved = os.path.abspath(os.path.normpath(resolved))
    path_key = os.path.normcase(resolved)
    if path_key in seen_paths:
        return None
    try:
        data = _read_limited(resolved, MAX_EXTERNAL_PROMPT_BYTES, display_name)
        text = data.decode("utf-8-sig").strip()
    except UnicodeDecodeError:
        errors.append(f"外部プロンプトをUTF-8で読み込めません: {display_name}")
        return None
    except PromptInputError as exc:
        errors.append(str(exc))
        return None
    seen_paths.add(path_key)
    if not text:
        errors.append(f"外部プロンプトが空です: {display_name}")
        return None
    return text


def resolve_system_prompt(
    settings: dict, default_prompt: str, app_dir: str
) -> tuple[str, list[str]]:
    """設定と外部ファイルを1つのsystem promptへ安全に解決する。"""
    errors: list[str] = []
    external = settings.get("external_prompt_files", {})
    if external is None:
        external = {}
    if not isinstance(external, dict):
        errors.append("external_prompt_files はJSONオブジェクトで指定してください。")
        external = {}

    seen_paths: set[str] = set()
    seen_content: set[str] = set()

    def external_parts(key: str) -> list[str]:
        result = []
        for configured_path in _file_specs(external.get(key), key, errors):
            text = _read_external_prompt(
                configured_path, app_dir, seen_paths, errors)
            if text is not None:
                result.append(text)
        return result

    inline_system = _inline_text(settings, "system_prompt", errors)
    inline_personalization = _inline_text(
        settings, "user_personalization", errors)

    parts = [inline_system or default_prompt]
    parts.extend(external_parts("system_prompt"))
    if inline_personalization:
        parts.append(inline_personalization)
    parts.extend(external_parts("user_personalization"))
    for key in ("response_language", "reasoning_visibility_instruction"):
        value = _inline_text(settings, key, errors)
        if value:
            parts.append(value)
    parts.extend(external_parts("user_profile"))
    parts.extend(external_parts("instructions"))

    unique_parts = []
    for part in parts:
        normalized = part.replace("\r\n", "\n").strip()
        if not normalized or normalized in seen_content:
            continue
        seen_content.add(normalized)
        unique_parts.append(normalized)
    return "\n\n".join(unique_parts), errors
