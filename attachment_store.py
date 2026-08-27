from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass

from atomic_io import atomic_write_bytes


MAX_ATTACHMENTS = 8
MAX_IMAGE_ATTACHMENTS = 1
MAX_TEXT_ATTACHMENT_BYTES = 1024 * 1024
MAX_IMAGE_ATTACHMENT_BYTES = 10 * 1024 * 1024

_UUID_RE = re.compile(r"^[0-9a-f]{32}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SESSION_PENDING_RE = re.compile(
    r"^\.pending-session-delete-([0-9a-f]{32})-([0-9a-f]{32})$"
)
_FILE_PATTERNS = (
    ("final", re.compile(r"^([0-9a-f]{32})(\.[a-z0-9]+)$")),
    ("add", re.compile(r"^\.pending-add-([0-9a-f]{32})(\.[a-z0-9]+)$")),
    ("delete", re.compile(
        r"^\.pending-delete-([0-9a-f]{32})(\.[a-z0-9]+)$")),
)

_TYPE_BY_EXTENSION = {
    ".txt": ("text", "text/plain", MAX_TEXT_ATTACHMENT_BYTES),
    ".md": ("text", "text/markdown", MAX_TEXT_ATTACHMENT_BYTES),
    ".json": ("text", "application/json", MAX_TEXT_ATTACHMENT_BYTES),
    ".csv": ("text", "text/csv", MAX_TEXT_ATTACHMENT_BYTES),
    ".png": ("image", "image/png", MAX_IMAGE_ATTACHMENT_BYTES),
    ".jpg": ("image", "image/jpeg", MAX_IMAGE_ATTACHMENT_BYTES),
    ".jpeg": ("image", "image/jpeg", MAX_IMAGE_ATTACHMENT_BYTES),
}
_METADATA_KEYS = frozenset({
    "id", "name", "kind", "mime_type", "extension", "size", "sha256",
})


class AttachmentStoreError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class CleanupResult:
    succeeded: int = 0
    failed: int = 0
    skipped: int = 0
    cleanup_pending: int = 0

    def as_dict(self) -> dict:
        return {
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "cleanup_pending": self.cleanup_pending,
        }


def validate_uuid_hex(value, field: str = "ID") -> str:
    if not isinstance(value, str) or _UUID_RE.fullmatch(value) is None:
        raise AttachmentStoreError("invalid_id", f"添付の{field}形式が不正です。")
    return value


def _safe_display_name(value) -> str:
    if not isinstance(value, str):
        raise AttachmentStoreError("invalid_metadata", "添付名の形式が不正です。")
    name = value.strip()
    if (
        not name
        or len(name) > 255
        or name in (".", "..")
        or "/" in name
        or "\\" in name
        or any(ord(char) < 32 for char in name)
    ):
        raise AttachmentStoreError("invalid_metadata", "添付名の形式が不正です。")
    return name


def validate_attachment_metadata(value: dict) -> dict:
    if not isinstance(value, dict) or set(value) != _METADATA_KEYS:
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付メタデータの形式が不正です。")
    attachment_id = validate_uuid_hex(value.get("id"), "内部ID")
    name = _safe_display_name(value.get("name"))
    extension = value.get("extension")
    if not isinstance(extension, str):
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付の拡張子形式が不正です。")
    extension = extension.lower()
    expected = _TYPE_BY_EXTENSION.get(extension)
    if expected is None:
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付の拡張子に対応していません。")
    kind, mime_type, size_limit = expected
    if value.get("kind") != kind or value.get("mime_type") != mime_type:
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付の種類が一致しません。")
    size = value.get("size")
    if isinstance(size, bool) or not isinstance(size, int) or not 0 < size <= size_limit:
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付のサイズ形式が不正です。")
    digest = value.get("sha256")
    if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付のSHA-256形式が不正です。")
    return {
        "id": attachment_id,
        "name": name,
        "kind": kind,
        "mime_type": mime_type,
        "extension": extension,
        "size": size,
        "sha256": digest,
    }


def validate_attachment_metadata_list(value) -> list[dict]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ATTACHMENTS:
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付一覧の形式が不正です。")
    result = [validate_attachment_metadata(item) for item in value]
    ids = [item["id"] for item in result]
    if len(ids) != len(set(ids)):
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み添付の内部IDが重複しています。")
    if sum(item["kind"] == "image" for item in result) > MAX_IMAGE_ATTACHMENTS:
        raise AttachmentStoreError(
            "invalid_metadata", "保存済み画像は1チャットにつき1枚までです。")
    return result


def _is_reparse(path: str) -> bool:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


class AttachmentStore:
    """Raw attachment sidecars isolated by validated session/attachment UUIDs."""

    def __init__(self, root: str):
        self.root = os.path.abspath(root)

    def _ensure_root(self) -> str:
        os.makedirs(self.root, exist_ok=True)
        if _is_reparse(self.root) or not os.path.isdir(self.root):
            raise AttachmentStoreError(
                "unsafe_root", "添付保存領域を安全に利用できません。")
        return os.path.realpath(self.root)

    def _contained(self, path: str, root: str | None = None) -> str:
        root = root or self._ensure_root()
        candidate = os.path.abspath(path)
        try:
            contained = os.path.commonpath((root, candidate)) == root
        except ValueError:
            contained = False
        if not contained:
            raise AttachmentStoreError(
                "unsafe_path", "添付保存領域外のパスは利用できません。")
        return candidate

    def _session_dir(self, session_id: str, *, create: bool = False) -> str:
        session_id = validate_uuid_hex(session_id, "セッションID")
        root = self._ensure_root()
        path = self._contained(os.path.join(root, session_id), root)
        if create:
            os.makedirs(path, exist_ok=True)
        if os.path.lexists(path):
            if _is_reparse(path) or not os.path.isdir(path):
                raise AttachmentStoreError(
                    "unsafe_path", "添付セッション領域を安全に利用できません。")
        return path

    @staticmethod
    def _file_name(metadata: dict, state: str = "final") -> str:
        meta = validate_attachment_metadata(metadata)
        prefix = {
            "final": "",
            "add": ".pending-add-",
            "delete": ".pending-delete-",
        }.get(state)
        if prefix is None:
            raise AttachmentStoreError("invalid_state", "添付処理状態が不正です。")
        return f"{prefix}{meta['id']}{meta['extension']}"

    def _file_path(self, session_id: str, metadata: dict, state: str) -> str:
        directory = self._session_dir(session_id, create=state == "add")
        return self._contained(os.path.join(
            directory, self._file_name(metadata, state)))

    @staticmethod
    def _check_regular(path: str) -> None:
        if _is_reparse(path) or not os.path.isfile(path):
            raise AttachmentStoreError(
                "unsafe_path", "保存済み添付ファイルを安全に利用できません。")

    def write_pending_add(self, session_id: str, metadata: dict, data: bytes) -> None:
        meta = validate_attachment_metadata(metadata)
        if not isinstance(data, bytes):
            raise AttachmentStoreError("invalid_data", "添付データの形式が不正です。")
        if len(data) != meta["size"]:
            raise AttachmentStoreError("size_mismatch", "添付サイズが一致しません。")
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise AttachmentStoreError("hash_mismatch", "添付のSHA-256が一致しません。")
        pending = self._file_path(session_id, meta, "add")
        final = self._file_path(session_id, meta, "final")
        if os.path.lexists(pending) or os.path.lexists(final):
            raise AttachmentStoreError("id_collision", "添付内部IDが重複しています。")
        atomic_write_bytes(pending, data)
        self._check_regular(pending)

    def finalize_add(self, session_id: str, metadata: dict) -> None:
        pending = self._file_path(session_id, metadata, "add")
        final = self._file_path(session_id, metadata, "final")
        if os.path.isfile(final):
            if os.path.isfile(pending):
                os.remove(pending)
            return
        self._check_regular(pending)
        os.replace(pending, final)

    def abort_add(self, session_id: str, metadata: dict) -> bool:
        pending = self._file_path(session_id, metadata, "add")
        try:
            if os.path.lexists(pending):
                self._check_regular(pending)
                os.remove(pending)
            self._remove_empty_session_dir(session_id)
            return True
        except OSError:
            return False

    def stage_delete(self, session_id: str, metadata: dict) -> bool:
        final = self._file_path(session_id, metadata, "final")
        pending = self._file_path(session_id, metadata, "delete")
        if os.path.isfile(pending):
            self._check_regular(pending)
            return True
        if not os.path.lexists(final):
            return False
        self._check_regular(final)
        if os.path.lexists(pending):
            raise AttachmentStoreError("id_collision", "添付削除状態が競合しています。")
        os.replace(final, pending)
        return True

    def commit_delete(self, session_id: str, metadata: dict) -> bool:
        pending = self._file_path(session_id, metadata, "delete")
        try:
            if os.path.lexists(pending):
                self._check_regular(pending)
                os.remove(pending)
            self._remove_empty_session_dir(session_id)
            return True
        except OSError:
            return False

    def rollback_delete(self, session_id: str, metadata: dict) -> bool:
        pending = self._file_path(session_id, metadata, "delete")
        final = self._file_path(session_id, metadata, "final")
        try:
            if not os.path.lexists(pending):
                return os.path.isfile(final)
            self._check_regular(pending)
            if os.path.lexists(final):
                return False
            os.replace(pending, final)
            return True
        except OSError:
            return False

    def read(self, session_id: str, metadata: dict) -> bytes:
        meta = validate_attachment_metadata(metadata)
        path = self._file_path(session_id, meta, "final")
        if not os.path.lexists(path):
            raise AttachmentStoreError(
                "missing", "保存済み添付ファイルが見つかりません。")
        self._check_regular(path)
        try:
            size = os.path.getsize(path)
            if size != meta["size"]:
                raise AttachmentStoreError(
                    "size_mismatch", "保存済み添付のサイズが一致しません。")
            with open(path, "rb") as handle:
                data = handle.read(meta["size"] + 1)
        except AttachmentStoreError:
            raise
        except OSError as exc:
            raise AttachmentStoreError(
                "unreadable", "保存済み添付ファイルを読み込めません。") from exc
        if len(data) != meta["size"]:
            raise AttachmentStoreError(
                "size_mismatch", "保存済み添付のサイズが一致しません。")
        if hashlib.sha256(data).hexdigest() != meta["sha256"]:
            raise AttachmentStoreError(
                "hash_mismatch", "保存済み添付のSHA-256が一致しません。")
        return data

    def recover_session(self, session_id: str, metadata_list: list[dict]) -> list[dict]:
        metadata = validate_attachment_metadata_list(metadata_list)
        by_id = {item["id"]: item for item in metadata}
        errors = []
        directory = self._session_dir(session_id, create=False)
        if not os.path.isdir(directory):
            return errors
        try:
            entries = list(os.scandir(directory))
        except OSError:
            return [{"code": "unreadable", "message":
                     "添付保存領域を読み込めません。"}]
        for entry in entries:
            parsed = self._parse_file_name(entry.name)
            if parsed is None or entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                errors.append({"code": "skipped", "message":
                               "確認できない添付管理ファイルをスキップしました。"})
                continue
            state, attachment_id, extension = parsed
            meta = by_id.get(attachment_id)
            try:
                if state == "add":
                    if meta is not None and meta["extension"] == extension:
                        self.finalize_add(session_id, meta)
                    else:
                        os.remove(entry.path)
                elif state == "delete":
                    if meta is not None and meta["extension"] == extension:
                        if not self.rollback_delete(session_id, meta):
                            raise OSError("rollback failed")
                    else:
                        os.remove(entry.path)
                elif meta is None or meta["extension"] != extension:
                    errors.append({"code": "orphan", "message":
                                   "関連情報のない添付実体をスキップしました。"})
            except (OSError, AttachmentStoreError):
                errors.append({"code": "recovery_failed", "message":
                               "添付の保留処理を完了できませんでした。"})
        self._remove_empty_session_dir(session_id)
        return errors

    @staticmethod
    def _parse_file_name(name: str):
        for state, pattern in _FILE_PATTERNS:
            match = pattern.fullmatch(name)
            if match and match.group(2) in _TYPE_BY_EXTENSION:
                return state, match.group(1), match.group(2)
        return None

    def _remove_empty_session_dir(self, session_id: str) -> None:
        directory = self._session_dir(session_id, create=False)
        try:
            os.rmdir(directory)
        except (FileNotFoundError, OSError):
            pass

    def stage_session_delete(self, session_id: str, transaction_id: str) -> str | None:
        session_id = validate_uuid_hex(session_id, "セッションID")
        transaction_id = validate_uuid_hex(transaction_id, "処理ID")
        source = self._session_dir(session_id, create=False)
        if not os.path.lexists(source):
            return None
        if _is_reparse(source) or not os.path.isdir(source):
            raise AttachmentStoreError(
                "unsafe_path", "添付セッション領域を安全に削除できません。")
        pending = self._contained(os.path.join(
            self._ensure_root(),
            f".pending-session-delete-{session_id}-{transaction_id}",
        ))
        if os.path.lexists(pending):
            raise AttachmentStoreError("id_collision", "添付削除処理が競合しています。")
        os.replace(source, pending)
        return pending

    def restore_session_delete(
        self, session_id: str, transaction_id: str
    ) -> bool:
        pending = self._pending_session_path(session_id, transaction_id)
        target = self._session_dir(session_id, create=False)
        try:
            if not os.path.lexists(pending):
                return not os.path.lexists(target)
            if _is_reparse(pending) or not os.path.isdir(pending) or os.path.lexists(target):
                return False
            os.replace(pending, target)
            return True
        except OSError:
            return False

    def commit_session_delete(
        self, session_id: str, transaction_id: str
    ) -> CleanupResult:
        pending = self._pending_session_path(session_id, transaction_id)
        if not os.path.lexists(pending):
            return CleanupResult()
        if _is_reparse(pending) or not os.path.isdir(pending):
            return CleanupResult(skipped=1, cleanup_pending=1)
        succeeded = failed = skipped = 0
        try:
            entries = list(os.scandir(pending))
        except OSError:
            return CleanupResult(failed=1, cleanup_pending=1)
        for entry in entries:
            if (
                self._parse_file_name(entry.name) is None
                or entry.is_symlink()
                or not entry.is_file(follow_symlinks=False)
            ):
                skipped += 1
                continue
            try:
                os.remove(entry.path)
                succeeded += 1
            except OSError:
                failed += 1
        try:
            os.rmdir(pending)
            pending_count = 0
        except OSError:
            pending_count = 1
        return CleanupResult(succeeded, failed, skipped, pending_count)

    def _pending_session_path(self, session_id: str, transaction_id: str) -> str:
        session_id = validate_uuid_hex(session_id, "セッションID")
        transaction_id = validate_uuid_hex(transaction_id, "処理ID")
        return self._contained(os.path.join(
            self._ensure_root(),
            f".pending-session-delete-{session_id}-{transaction_id}",
        ))

    def pending_session_namespaces(self) -> list[tuple[str, str, str]]:
        if not os.path.lexists(self.root):
            return []
        root = self._ensure_root()
        result = []
        try:
            entries = list(os.scandir(root))
        except OSError:
            return result
        for entry in entries:
            match = _SESSION_PENDING_RE.fullmatch(entry.name)
            if (
                match
                and not entry.is_symlink()
                and entry.is_dir(follow_symlinks=False)
            ):
                result.append((match.group(1), match.group(2), entry.path))
        return result

    def session_namespaces(self) -> list[str]:
        """Return only safe, UUID-named attachment session directories."""
        if not os.path.lexists(self.root):
            return []
        root = self._ensure_root()
        result = []
        try:
            entries = list(os.scandir(root))
        except OSError:
            return result
        for entry in entries:
            if (
                _UUID_RE.fullmatch(entry.name)
                and not entry.is_symlink()
                and not _is_reparse(entry.path)
                and entry.is_dir(follow_symlinks=False)
            ):
                result.append(entry.name)
        return result

    def audit_entries(self, valid_session_ids: set[str]) -> int:
        """Count unmanaged or unsafe entries without following or deleting them."""
        if not os.path.lexists(self.root):
            return 0
        root = self._ensure_root()
        skipped = 0
        try:
            entries = list(os.scandir(root))
        except OSError:
            return 1
        for entry in entries:
            if entry.is_symlink():
                skipped += 1
                continue
            if _SESSION_PENDING_RE.fullmatch(entry.name):
                skipped += 1
                continue
            if _UUID_RE.fullmatch(entry.name) is None:
                skipped += 1
                continue
            if entry.name not in valid_session_ids:
                skipped += 1
                continue
            if not entry.is_dir(follow_symlinks=False):
                skipped += 1
                continue
            try:
                children = list(os.scandir(entry.path))
            except OSError:
                skipped += 1
                continue
            skipped += sum(
                child.is_symlink()
                or not child.is_file(follow_symlinks=False)
                or self._parse_file_name(child.name) is None
                for child in children
            )
        return skipped
