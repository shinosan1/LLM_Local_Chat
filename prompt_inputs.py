from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from io import BytesIO

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


class PromptInputError(ValueError):
    """ユーザーへ表示できるローカル入力エラー。"""


@dataclass(frozen=True)
class Attachment:
    name: str
    kind: str
    mime_type: str
    text: str | None = None
    data: bytes | None = None


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


def load_attachment(path: str) -> Attachment:
    """添付を上限内でメモリへ読み込む。実パスは返り値へ保持しない。"""
    name = _safe_name(path)
    extension = os.path.splitext(name)[1].lower()
    if extension in TEXT_ATTACHMENT_EXTENSIONS:
        data = _read_limited(path, MAX_TEXT_ATTACHMENT_BYTES, name)
        try:
            text = data.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise PromptInputError(
                f"UTF-8のテキストとして読み込めません: {name}"
            ) from exc
        if not text:
            raise PromptInputError(f"ファイルが空です: {name}")
        return Attachment(
            name=name,
            kind="text",
            mime_type="text/plain",
            text=text,
        )

    if extension in IMAGE_ATTACHMENT_EXTENSIONS:
        data = _read_limited(path, MAX_IMAGE_ATTACHMENT_BYTES, name)
        try:
            with Image.open(BytesIO(data)) as image:
                width, height = image.size
                image_format = (image.format or "").upper()
                if image_format not in ("PNG", "JPEG"):
                    raise PromptInputError(
                        f"PNGまたはJPEG画像ではありません: {name}"
                    )
                if width <= 0 or height <= 0 or width * height > MAX_IMAGE_PIXELS:
                    raise PromptInputError(
                        f"画像の画素数が上限を超えています: {name}"
                    )
                image.verify()
        except PromptInputError:
            raise
        except (UnidentifiedImageError, OSError, SyntaxError) as exc:
            raise PromptInputError(f"画像を読み込めません: {name}") from exc
        return Attachment(
            name=name,
            kind="image",
            mime_type="image/png" if image_format == "PNG" else "image/jpeg",
            data=data,
        )

    raise PromptInputError(
        f"対応していないファイル形式です: {name}"
    )


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
    for attachment in attachments:
        if attachment.kind != "text":
            continue
        blocks.append(
            "[添付ファイル]\n"
            f"ファイル名: {attachment.name}\n\n"
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
    images = [attachment for attachment in attachments if attachment.kind == "image"]
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

    external_system = external_parts("system_prompt")
    inline_system = _inline_text(settings, "system_prompt", errors)
    system_parts = external_system or [inline_system or default_prompt]

    external_personalization = external_parts("user_personalization")
    inline_personalization = _inline_text(
        settings, "user_personalization", errors)
    personalization_parts = (
        external_personalization
        if external_personalization
        else ([inline_personalization] if inline_personalization else [])
    )

    parts = list(system_parts)
    parts.extend(personalization_parts)
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
