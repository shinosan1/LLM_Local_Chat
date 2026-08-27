import datetime
import hashlib
import json
import os
import re
import stat
import threading
import uuid
from contextlib import contextmanager

from attachment_store import (
    AttachmentStore,
    AttachmentStoreError,
    CleanupResult,
    validate_attachment_metadata_list,
    validate_uuid_hex,
)
from atomic_io import atomic_write_bytes
from history_crypto import (
    DPAPIProtector,
    HistoryCryptoError,
    decode_document,
    encode_envelope,
    validate_session,
)
from portable_history import (
    portable_session_snapshot,
    session_digest,
    validate_portable_session,
)


ALLOWED_RETENTION_DAYS = (0, 30, 90, 180, 365)
_HISTORY_PENDING_RE = re.compile(
    r"^\.pending-session-delete-([0-9a-f]{32})-([0-9a-f]{32})\.bin$"
)


def normalize_retention_days(value) -> int:
    if isinstance(value, bool):
        return 0
    try:
        days = int(value)
    except (TypeError, ValueError):
        return 0
    return days if days in ALLOWED_RETENTION_DAYS else 0


class HistoryMigrationError(HistoryCryptoError):
    def __init__(self, paths: list[str], diagnostics=None):
        self.paths = paths
        self.diagnostics = tuple(diagnostics or ())
        super().__init__(
            f"{len(paths)}件の会話履歴を暗号化できませんでした。"
        )


class SessionStore:
    @staticmethod
    def _stat_signature(stat_result) -> tuple:
        return (
            stat_result.st_mtime_ns,
            stat_result.st_size,
            stat_result.st_ctime_ns,
        )

    @staticmethod
    def _file_signature(path: str, stat_result=None) -> tuple:
        stat_result = stat_result or os.stat(path)
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return (*SessionStore._stat_signature(stat_result), digest.digest())

    def __init__(self, log_dir: str, protector=None):
        self._log_dir = log_dir
        self._protector = protector if protector is not None else DPAPIProtector()
        self._index: dict[str, dict] = {}
        self._lock = threading.RLock()
        self._attachment_store = AttachmentStore(
            os.path.join(self._log_dir, "attachments")
        )
        os.makedirs(self._log_dir, exist_ok=True)

    @staticmethod
    def _merge_cleanup_results(*results: CleanupResult) -> CleanupResult:
        return CleanupResult(
            succeeded=sum(item.succeeded for item in results),
            failed=sum(item.failed for item in results),
            skipped=sum(item.skipped for item in results),
            cleanup_pending=sum(item.cleanup_pending for item in results),
        )

    def _managed_path(self, path: str) -> str:
        if not isinstance(path, str) or not path:
            raise HistoryCryptoError("保存済み会話のパスが無効です。")
        log_dir = os.path.normcase(os.path.realpath(self._log_dir))
        candidate = os.path.normcase(os.path.realpath(path))
        try:
            managed = os.path.commonpath((log_dir, candidate)) == log_dir
        except ValueError:
            managed = False
        if (
            not managed
            or os.path.dirname(candidate) != log_dir
            or not candidate.endswith(".json")
            or not os.path.isfile(candidate)
        ):
            raise HistoryCryptoError(
                "現在の保存済み会話を確認できません。"
                "会話一覧を更新してから、もう一度実行してください。"
            )
        return candidate

    def _live_session_id_counts(self) -> dict[str, int]:
        counts, _documents = self._live_session_snapshot()
        return counts

    def _live_session_snapshot(self) -> tuple[dict[str, int], list[tuple]]:
        """Read each live history at most once for one recovery operation."""
        counts: dict[str, int] = {}
        documents: list[tuple] = []
        for entry in os.scandir(self._log_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                data, encrypted, _raw = self._read_document(entry.path)
            except Exception:
                continue
            documents.append((entry.path, data, encrypted))
            session_id = data.get("session_id")
            if encrypted and isinstance(session_id, str):
                counts[session_id] = counts.get(session_id, 0) + 1
        return counts, documents

    @staticmethod
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

    def _history_pending_path(
        self, session_id: str, transaction_id: str
    ) -> str:
        session_id = validate_uuid_hex(session_id, "セッションID")
        transaction_id = validate_uuid_hex(transaction_id, "処理ID")
        root = os.path.abspath(self._log_dir)
        candidate = os.path.abspath(os.path.join(
            root,
            f".pending-session-delete-{session_id}-{transaction_id}.bin",
        ))
        try:
            contained = os.path.commonpath((root, candidate)) == root
        except ValueError:
            contained = False
        if not contained or os.path.dirname(candidate) != root:
            raise HistoryCryptoError("会話履歴の削除保留パスが不正です。")
        return candidate

    def _pending_history_deletes(self) -> list[tuple[str, str, str]]:
        result = []
        for entry in os.scandir(self._log_dir):
            match = _HISTORY_PENDING_RE.fullmatch(entry.name)
            if (
                match
                and not entry.is_symlink()
                and entry.is_file(follow_symlinks=False)
            ):
                result.append((match.group(1), match.group(2), entry.path))
        return result

    def _commit_pending_history_delete(
        self, path: str, session_id: str
    ) -> bool:
        try:
            if self._is_reparse(path) or not os.path.isfile(path):
                return False
            data, encrypted, _raw = self._read_document(path)
            if not encrypted or data.get("session_id") != session_id:
                return False
            os.remove(path)
            return True
        except Exception:
            return False

    def _recover_attachment_namespaces(
        self, live_snapshot: tuple[dict[str, int], list[tuple]] | None = None
    ) -> list[dict]:
        """Resolve interrupted chat deletions from the live encrypted sessions."""
        errors = []
        counts, documents = live_snapshot or self._live_session_snapshot()
        processed_deletes = set()
        for session_id, transaction_id, path in self._pending_history_deletes():
            if counts.get(session_id, 0):
                errors.append({
                    "code": "recovery_failed",
                    "message": "会話履歴の削除保留状態が競合しています。",
                })
                continue
            if not self._commit_pending_history_delete(path, session_id):
                errors.append({
                    "code": "cleanup_pending",
                    "message": "会話履歴の物理削除に未完了項目があります。",
                })
                continue
            processed_deletes.add((session_id, transaction_id))
            result = self._attachment_store.commit_session_delete(
                session_id, transaction_id
            )
            if result.failed or result.skipped or result.cleanup_pending:
                errors.append({
                    "code": "cleanup_pending",
                    "message": "保存済み添付の物理削除に未完了項目があります。",
                })
        for session_id, transaction_id, _path in (
            self._attachment_store.pending_session_namespaces()
        ):
            if (session_id, transaction_id) in processed_deletes:
                continue
            if counts.get(session_id, 0):
                if not self._attachment_store.restore_session_delete(
                    session_id, transaction_id
                ):
                    errors.append({
                        "code": "recovery_failed",
                        "message": "保存済み添付の削除保留状態を復元できませんでした。",
                    })
                continue
            result = self._attachment_store.commit_session_delete(
                session_id, transaction_id
            )
            if result.failed or result.skipped or result.cleanup_pending:
                errors.append({
                    "code": "cleanup_pending",
                    "message": "保存済み添付の物理削除に未完了項目があります。",
                })
        # Per-file add/delete transactions are recovered from the authoritative
        # encrypted metadata, including sessions whose attachment list is now
        # empty after a committed delete.
        for _path, data, encrypted in documents:
            try:
                session_id = data.get("session_id")
                if (
                    not encrypted
                    or not isinstance(session_id, str)
                    or counts.get(session_id, 0) != 1
                ):
                    continue
                validate_uuid_hex(session_id, "セッションID")
                metadata = validate_attachment_metadata_list(
                    data.get("attachments", []))
                errors.extend(self._attachment_store.recover_session(
                    session_id, metadata))
            except Exception:
                continue
        # A process may stop after writing a pending add but before the first
        # encrypted chat JSON is committed.  Only strict UUID directories and
        # strict pending filenames are considered; final/unknown files remain.
        for session_id in self._attachment_store.session_namespaces():
            if counts.get(session_id, 0) == 0:
                try:
                    errors.extend(self._attachment_store.recover_session(
                        session_id, []))
                except (AttachmentStoreError, OSError):
                    errors.append({
                        "code": "skipped",
                        "message": "安全に確認できない添付領域をスキップしました。",
                    })
        return errors

    def new_session(self) -> dict:
        return {
            "session_id": uuid.uuid4().hex,
            "title": "新しいチャット",
            "history": [],
            "summary": "",
            "attachments": [],
        }

    @staticmethod
    def _utc_now_text() -> str:
        return datetime.datetime.now(datetime.timezone.utc).isoformat()

    @staticmethod
    def _timestamp_from_mtime(path: str) -> str:
        return datetime.datetime.fromtimestamp(
            os.path.getmtime(path), datetime.timezone.utc
        ).isoformat()

    def _read_document(self, path: str) -> tuple[dict, bool, bytes]:
        with open(path, "rb") as handle:
            raw = handle.read()
        return decode_document(raw, self._protector)

    def _write_encrypted(self, path: str, session: dict) -> None:
        validate_session(session)
        raw = json.dumps(
            session, ensure_ascii=False, indent=2
        ).encode("utf-8")
        envelope = encode_envelope(raw, self._protector)
        _data, encrypted, verified = decode_document(
            envelope, self._protector
        )
        if not encrypted or verified != raw:
            raise HistoryCryptoError("暗号化会話履歴の検証に失敗しました。")
        atomic_write_bytes(path, envelope)

    def _prepare_for_save(self, session: dict, path: str | None) -> dict:
        prepared = dict(session)
        previous = None
        previous_activity = None
        if path and os.path.exists(path):
            previous, _encrypted, _raw = self._read_document(path)
            previous_activity = previous.get("last_activity_at")
            if not previous_activity:
                previous_activity = self._timestamp_from_mtime(path)
        changed = (
            previous is None
            or previous.get("history", []) != prepared.get("history", [])
            or previous.get("summary", "") != prepared.get("summary", "")
            or previous.get("attachments", [])
            != prepared.get("attachments", [])
        )
        prepared["last_activity_at"] = (
            self._utc_now_text()
            if changed
            else previous_activity or self._utc_now_text()
        )
        return prepared

    def save(self, session: dict, path: str | None) -> str | None:
        with self._lock:
            history = session.get("history", [])
            attachments = session.get("attachments", [])
            if not path and not history and not attachments:
                return None
            prepared = self._prepare_for_save(session, path)
            if not path:
                ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
                base_path = os.path.join(self._log_dir, f"chat_{ts}.json")
                path = base_path
                suffix = 1
                while os.path.exists(path):
                    path = os.path.splitext(base_path)[0] + f"_{suffix}.json"
                    suffix += 1
            self._write_encrypted(path, prepared)
            session["last_activity_at"] = prepared["last_activity_at"]
            self._cache_session(path, prepared)
            return path

    def _read_metadata(self, path: str, fallback_title: str) -> dict:
        data, _encrypted, _raw = self._read_document(path)
        return self._metadata_from_document(path, fallback_title, data)

    @staticmethod
    def _metadata_from_document(
        path: str, fallback_title: str, data: dict
    ) -> dict:
        return {
            "path": path,
            "title": data.get("title", fallback_title),
            "summary": data.get("summary", ""),
        }

    def _cache_session(self, path: str, session: dict) -> None:
        stat = os.stat(path)
        self._index[path] = {
            "signature": self._file_signature(path, stat),
            "metadata": {
                "path": path,
                "title": session.get("title", os.path.basename(path)),
                "summary": session.get("summary", ""),
            },
        }

    def _refresh_index(self, preloaded: dict[str, dict] | None = None) -> None:
        if not os.path.exists(self._log_dir):
            self._index.clear()
            return

        preloaded = preloaded or {}
        current_paths = set()
        for entry in os.scandir(self._log_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            path = entry.path
            current_paths.add(path)
            try:
                stat = entry.stat()
                cached = self._index.get(path)
                stat_signature = self._stat_signature(stat)
                if (
                    cached
                    and cached.get("signature", ())[:3] == stat_signature
                ):
                    continue
                signature = self._file_signature(path, stat)
                if (
                    cached
                    and len(cached.get("signature", ())) == 4
                    and cached["signature"][3] == signature[3]
                ):
                    cached["signature"] = signature
                    continue
                try:
                    document = preloaded.get(
                        os.path.normcase(os.path.realpath(path)))
                    metadata = (
                        self._metadata_from_document(path, entry.name, document)
                        if document is not None
                        else self._read_metadata(path, entry.name)
                    )
                except Exception:
                    metadata = None
                self._index[path] = {
                    "signature": signature,
                    "metadata": metadata,
                }
            except OSError:
                self._index.pop(path, None)

        for path in set(self._index) - current_paths:
            self._index.pop(path, None)

    def _list_sessions_locked(self, keyword: str = "") -> list[dict]:
        kw = keyword.strip().lower()
        sessions = [
            cached["metadata"]
            for path, cached in sorted(
                self._index.items(),
                key=lambda item: os.path.basename(item[0]),
                reverse=True,
            )
            if cached["metadata"] is not None
        ]
        if not kw:
            return sessions
        return [
            session for session in sessions
            if kw in (session["title"] + session["summary"]).lower()
        ]

    @staticmethod
    def _snapshot_documents(live_snapshot) -> dict[str, dict]:
        return {
            os.path.normcase(os.path.realpath(path)): data
            for path, data, encrypted in live_snapshot[1]
            if encrypted
        }

    def _prepare_history_operation_locked(self):
        live_snapshot = self._live_session_snapshot()
        self._recover_attachment_namespaces(live_snapshot)
        documents = self._snapshot_documents(live_snapshot)
        self._refresh_index(documents)
        return live_snapshot, documents

    def list_sessions(self, keyword: str = "") -> list[dict]:
        with self._lock:
            self._prepare_history_operation_locked()
            return self._list_sessions_locked(keyword)

    def _load_encrypted(self, path: str) -> dict:
        data, encrypted, _raw = self._read_document(path)
        if not encrypted:
            raise HistoryCryptoError(
                "暗号化されていない会話履歴です。"
                "起動時の移行を実行してください。"
            )
        return data

    def load(self, path: str) -> dict:
        with self._lock:
            self._recover_attachment_namespaces()
            return self._load_encrypted(path)

    def load_managed_session(self, path: str) -> dict:
        """Load one saved session only when it is still managed by this store."""
        return self.load(self._managed_path(path))

    def delete(self, path: str) -> dict:
        with self._lock:
            live_snapshot, documents = self._prepare_history_operation_locked()
            managed_path, data = self._document_for_path_locked(
                path, documents)
            metadata = validate_attachment_metadata_list(
                data.get("attachments", []))
            session_id = data.get("session_id")
            if not isinstance(session_id, str):
                session_id = uuid.uuid4().hex
                data["session_id"] = session_id
                self._write_encrypted(managed_path, data)
                self._cache_session(managed_path, data)
                live_snapshot[0][session_id] = 1
            validate_uuid_hex(session_id, "セッションID")
            transaction_id = uuid.uuid4().hex
            staged = False
            if live_snapshot[0].get(session_id, 0) != 1:
                raise HistoryCryptoError(
                    "同じセッションIDの会話履歴が複数あるため、"
                    "添付を安全に削除できません。"
                )
            if metadata or os.path.lexists(os.path.join(
                self._attachment_store.root, session_id
            )):
                staged = self._attachment_store.stage_session_delete(
                    session_id, transaction_id) is not None
            pending_history = self._history_pending_path(
                session_id, transaction_id)
            if os.path.lexists(pending_history):
                if staged:
                    self._attachment_store.restore_session_delete(
                        session_id, transaction_id)
                raise HistoryCryptoError("会話履歴の削除状態が競合しています。")
            try:
                os.replace(managed_path, pending_history)
            except Exception:
                if staged:
                    self._attachment_store.restore_session_delete(
                        session_id, transaction_id)
                raise
            self._index.pop(managed_path, None)
            if not self._commit_pending_history_delete(
                pending_history, session_id
            ):
                return CleanupResult(
                    failed=1, cleanup_pending=1).as_dict()
            result = CleanupResult()
            if staged:
                result = self._attachment_store.commit_session_delete(
                    session_id, transaction_id)
            return result.as_dict()

    def rename(self, path: str, new_title: str) -> dict:
        with self._lock:
            data = self.load(path)
            data["title"] = new_title
            if not data.get("last_activity_at"):
                data["last_activity_at"] = self._timestamp_from_mtime(path)
            self._write_encrypted(path, data)
            self._cache_session(path, data)
            return data

    @staticmethod
    def _attachment_metadata(attachment) -> tuple[dict, bytes]:
        data = getattr(attachment, "data", None)
        if not isinstance(data, bytes) or not data:
            raise AttachmentStoreError(
                "invalid_data", "添付データを保存できません。")
        attachment_id = getattr(attachment, "attachment_id", None)
        if not isinstance(attachment_id, str) or not attachment_id:
            attachment_id = uuid.uuid4().hex
        extension = getattr(attachment, "extension", None)
        if not isinstance(extension, str) or not extension:
            extension = os.path.splitext(getattr(attachment, "name", ""))[1]
        metadata = {
            "id": attachment_id,
            "name": getattr(attachment, "name", ""),
            "kind": getattr(attachment, "kind", ""),
            "mime_type": getattr(attachment, "mime_type", ""),
            "extension": extension.lower(),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        return validate_attachment_metadata_list([metadata])[0], data

    def add_attachments(
        self, session: dict, path: str | None, attachments
    ) -> tuple[str, list[dict], list[str]]:
        """Copy validated attachments into this chat and persist metadata."""
        with self._lock:
            self._recover_attachment_namespaces()
            existing = validate_attachment_metadata_list(
                session.get("attachments", []))
            fingerprints = {
                (item["kind"], item["size"], item["sha256"])
                for item in existing
            }
            additions: list[tuple[dict, bytes]] = []
            warnings: list[str] = []
            for attachment in attachments:
                metadata, data = self._attachment_metadata(attachment)
                fingerprint = (
                    metadata["kind"], metadata["size"], metadata["sha256"])
                if fingerprint in fingerprints:
                    warnings.append(
                        f"同じ内容の添付は既に追加されています: {metadata['name']}")
                    continue
                fingerprints.add(fingerprint)
                additions.append((metadata, data))
            if not additions:
                return path, [], warnings

            combined = validate_attachment_metadata_list(
                [*existing, *(item[0] for item in additions)])
            old_session_id = session.get("session_id")
            session_id = old_session_id
            if not isinstance(session_id, str):
                session_id = uuid.uuid4().hex
            validate_uuid_hex(session_id, "セッションID")
            written = []
            try:
                for metadata, data in additions:
                    self._attachment_store.write_pending_add(
                        session_id, metadata, data)
                    written.append(metadata)
                session["session_id"] = session_id
                session["attachments"] = combined
                saved_path = self.save(session, path)
                if not saved_path:
                    raise HistoryCryptoError("添付の会話履歴を保存できませんでした。")
            except Exception as exc:
                session["attachments"] = existing
                if old_session_id is None:
                    session.pop("session_id", None)
                else:
                    session["session_id"] = old_session_id
                cleanup_failed = False
                for metadata in written:
                    if not self._attachment_store.abort_add(
                        session_id, metadata
                    ):
                        cleanup_failed = True
                if cleanup_failed:
                    recovery = self._attachment_store.recover_session(
                        session_id, existing)
                    if any(
                        item.get("code") == "recovery_failed"
                        for item in recovery
                    ):
                        raise HistoryCryptoError(
                            "添付の会話履歴を保存できず、"
                            "一時添付の削除も完了していません。"
                        ) from exc
                raise

            for metadata in written:
                try:
                    self._attachment_store.finalize_add(session_id, metadata)
                except (AttachmentStoreError, OSError):
                    warnings.append(
                        f"添付の保存完了処理を再試行します: {metadata['name']}")
            return saved_path, written, warnings

    def load_attachments(self, session: dict) -> tuple[list[tuple[dict, bytes]], list[str]]:
        """Load only intact sidecars; damaged content is never returned."""
        with self._lock:
            metadata = validate_attachment_metadata_list(
                session.get("attachments", []))
            if not metadata:
                return [], []
            session_id = validate_uuid_hex(
                session.get("session_id"), "セッションID")
            warnings = [
                item["message"]
                for item in self._attachment_store.recover_session(
                    session_id, metadata)
            ]
            loaded = []
            for item in metadata:
                try:
                    loaded.append((item, self._attachment_store.read(
                        session_id, item)))
                except AttachmentStoreError as exc:
                    warnings.append(f"{item['name']}: {exc}")
            return loaded, list(dict.fromkeys(warnings))

    @staticmethod
    def _document_key(path: str) -> str:
        return os.path.normcase(os.path.realpath(path))

    def _document_for_path_locked(
        self, path: str, documents: dict[str, dict]
    ) -> tuple[str, dict]:
        managed_path = self._managed_path(path)
        data = documents.get(self._document_key(managed_path))
        if data is None:
            data = self._load_encrypted(managed_path)
            documents[self._document_key(managed_path)] = data
        return managed_path, data

    def _read_attachment_once_locked(
        self,
        session_id: str,
        metadata: dict,
        verified: dict[tuple[str, str], tuple[bytes | None, Exception | None]],
    ) -> tuple[bytes | None, Exception | None]:
        key = (session_id, metadata["id"])
        if key not in verified:
            try:
                verified[key] = (
                    self._attachment_store.read(session_id, metadata), None)
            except (AttachmentStoreError, OSError) as exc:
                verified[key] = (None, exc)
        return verified[key]

    def _rollback_staged_locked(
        self, session_id: str, staged: list[dict]
    ) -> int:
        pending = 0
        for metadata in reversed(staged):
            try:
                restored = self._attachment_store.rollback_delete(
                    session_id, metadata)
            except (AttachmentStoreError, OSError):
                restored = False
            if not restored:
                pending += 1
        return pending

    def _save_attachment_metadata_locked(
        self, data: dict, managed_path: str
    ) -> None:
        """Persist a known attachment metadata change without rereading history."""
        prepared = dict(data)
        prepared["last_activity_at"] = self._utc_now_text()
        self._write_encrypted(managed_path, prepared)
        data["last_activity_at"] = prepared["last_activity_at"]
        self._cache_session(managed_path, prepared)

    def _delete_attachment_batch_locked(
        self,
        path: str,
        attachment_ids: list[str],
        documents: dict[str, dict],
        session_counts: dict[str, int],
        verified: dict[tuple[str, str], tuple[bytes | None, Exception | None]]
        | None = None,
    ) -> tuple[dict, CleanupResult]:
        """Delete one session's attachments atomically at metadata level."""
        managed_path, data = self._document_for_path_locked(path, documents)
        metadata = validate_attachment_metadata_list(
            data.get("attachments", []))
        ids = list(dict.fromkeys(attachment_ids))
        by_id = {item["id"]: item for item in metadata}
        targets = [by_id[item_id] for item_id in ids if item_id in by_id]
        skipped = len(ids) - len(targets)
        if not targets:
            return data, CleanupResult(skipped=skipped)

        session_id = validate_uuid_hex(
            data.get("session_id"), "セッションID")
        if session_counts.get(session_id, 0) != 1:
            return data, CleanupResult(
                failed=len(targets), skipped=skipped)
        verified = verified if verified is not None else {}
        staged: list[dict] = []
        for target in targets:
            _raw, error = self._read_attachment_once_locked(
                session_id, target, verified)
            if error is not None:
                pending = self._rollback_staged_locked(session_id, staged)
                for item in staged:
                    verified.pop((session_id, item["id"]), None)
                return data, CleanupResult(
                    failed=len(targets), skipped=skipped,
                    cleanup_pending=pending,
                )
            try:
                did_stage = self._attachment_store.stage_delete(
                    session_id, target)
            except (AttachmentStoreError, OSError):
                did_stage = False
            if not did_stage:
                pending = self._rollback_staged_locked(session_id, staged)
                for item in staged:
                    verified.pop((session_id, item["id"]), None)
                return data, CleanupResult(
                    failed=len(targets), skipped=skipped,
                    cleanup_pending=pending,
                )
            staged.append(target)

        target_ids = {item["id"] for item in targets}
        data["attachments"] = [
            item for item in metadata if item["id"] not in target_ids]
        try:
            self._save_attachment_metadata_locked(data, managed_path)
        except Exception:
            data["attachments"] = metadata
            pending = self._rollback_staged_locked(session_id, staged)
            for item in staged:
                verified.pop((session_id, item["id"]), None)
            return data, CleanupResult(
                failed=len(targets), skipped=skipped,
                cleanup_pending=pending,
            )

        pending = 0
        for target in targets:
            try:
                committed = self._attachment_store.commit_delete(
                    session_id, target)
            except (AttachmentStoreError, OSError):
                committed = False
            if not committed:
                pending += 1
        documents[self._document_key(managed_path)] = data
        return data, CleanupResult(
            succeeded=len(targets), skipped=skipped,
            cleanup_pending=pending,
        )

    def _saved_attachment_rows_locked(
        self,
        documents: dict[str, dict],
        verified: dict[tuple[str, str], tuple[bytes | None, Exception | None]]
        | None = None,
    ) -> tuple[list[dict], dict]:
        verified = verified if verified is not None else {}
        rows = []
        for session_info in self._list_sessions_locked():
            data = documents.get(self._document_key(session_info["path"]))
            if data is None:
                continue
            try:
                metadata = validate_attachment_metadata_list(
                    data.get("attachments", []))
                session_id = data.get("session_id")
                if metadata:
                    validate_uuid_hex(session_id, "セッションID")
            except Exception:
                continue
            name_counts: dict[str, int] = {}
            for item in metadata:
                key = item["name"].casefold()
                name_counts[key] = name_counts.get(key, 0) + 1
                suffix = name_counts[key]
                display_name = (
                    item["name"] if suffix == 1
                    else f"{item['name']} ({suffix})"
                )
                _raw, error = self._read_attachment_once_locked(
                    session_id, item, verified)
                if error is None:
                    status = "利用可能"
                else:
                    status = {
                        "missing": "実体が見つかりません",
                        "unreadable": "読み取り不能",
                        "size_mismatch": "サイズ不一致",
                        "hash_mismatch": "SHA-256不一致",
                    }.get(getattr(error, "code", ""), "利用不可")
                rows.append({
                    "attachment_id": item["id"],
                    "session_id": session_id,
                    "session_path": session_info["path"],
                    "name": item["name"],
                    "display_name": display_name,
                    "kind": item["kind"],
                    "kind_label": (
                        "画像" if item["kind"] == "image" else "テキスト"),
                    "mime_type": item["mime_type"],
                    "size": item["size"],
                    "chat_title": data.get("title", ""),
                    "status": status,
                })
        return rows, verified

    def _current_attachment_snapshot_locked(
        self,
        current_path: str | None,
        documents: dict[str, dict],
        verified: dict[tuple[str, str], tuple[bytes | None, Exception | None]],
    ) -> dict:
        result = {
            "current_path": None,
            "current_session": None,
            "current_attachments": [],
            "current_warnings": [],
        }
        if not current_path:
            return result
        try:
            managed_path, data = self._document_for_path_locked(
                current_path, documents)
            metadata = validate_attachment_metadata_list(
                data.get("attachments", []))
            session_id = data.get("session_id")
            if metadata:
                validate_uuid_hex(session_id, "セッションID")
            loaded = []
            warnings = []
            for item in metadata:
                raw, error = self._read_attachment_once_locked(
                    session_id, item, verified)
                if error is None:
                    loaded.append((item, raw))
                else:
                    warnings.append(f"{item['name']}: {error}")
            result.update({
                "current_path": managed_path,
                "current_session": data,
                "current_attachments": loaded,
                "current_warnings": list(dict.fromkeys(warnings)),
            })
        except Exception as exc:
            result["current_warnings"] = [
                f"現在のチャット表示を更新できませんでした: {exc}"]
        return result

    def _attachment_manager_snapshot_locked(
        self,
        documents: dict[str, dict],
        current_path: str | None,
        verified: dict[tuple[str, str], tuple[bytes | None, Exception | None]]
        | None = None,
    ) -> dict:
        rows, verified = self._saved_attachment_rows_locked(
            documents, verified)
        current = self._current_attachment_snapshot_locked(
            current_path, documents, verified)
        return {
            "items": rows,
            "total_size": sum(max(0, int(item["size"])) for item in rows),
            **current,
        }

    def delete_saved_attachment(self, item: dict) -> dict:
        with self._lock:
            if not isinstance(item, dict):
                return CleanupResult(skipped=1).as_dict()
            live_snapshot, documents = self._prepare_history_operation_locked()
            _data, result = self._delete_attachment_batch_locked(
                item.get("session_path"),
                [item.get("attachment_id")],
                documents,
                live_snapshot[0],
            )
            return result.as_dict()

    def delete_saved_attachments(
        self, path: str, attachment_ids: list[str] | None = None
    ) -> tuple[dict, dict]:
        with self._lock:
            live_snapshot, documents = self._prepare_history_operation_locked()
            managed_path, data = self._document_for_path_locked(
                path, documents)
            ids = (
                [item["id"] for item in validate_attachment_metadata_list(
                    data.get("attachments", []))]
                if attachment_ids is None else list(attachment_ids)
            )
            data, result = self._delete_attachment_batch_locked(
                managed_path, ids, documents, live_snapshot[0])
            return data, result.as_dict()

    def list_saved_attachments(self) -> list[dict]:
        with self._lock:
            live_snapshot, documents = self._prepare_history_operation_locked()
            rows, _verified = self._saved_attachment_rows_locked(documents)
            return rows

    def saved_attachment_manager_snapshot(
        self, current_path: str | None = None
    ) -> dict:
        with self._lock:
            _snapshot, documents = self._prepare_history_operation_locked()
            return self._attachment_manager_snapshot_locked(
                documents, current_path)

    def delete_saved_attachment_snapshot(
        self, item: dict, current_path: str | None = None
    ) -> dict:
        with self._lock:
            live_snapshot, documents = self._prepare_history_operation_locked()
            if not isinstance(item, dict):
                result = CleanupResult(skipped=1)
            else:
                _data, result = self._delete_attachment_batch_locked(
                    item.get("session_path"),
                    [item.get("attachment_id")],
                    documents,
                    live_snapshot[0],
                )
            snapshot = self._attachment_manager_snapshot_locked(
                documents, current_path)
            snapshot["result"] = result.as_dict()
            return snapshot

    @staticmethod
    def _valid_session_ids(documents: dict[str, dict]) -> set[str]:
        valid = set()
        for data in documents.values():
            session_id = data.get("session_id")
            if isinstance(session_id, str):
                try:
                    valid.add(validate_uuid_hex(session_id, "セッションID"))
                except AttachmentStoreError:
                    continue
        return valid

    def _delete_all_saved_attachments_locked(
        self,
        documents: dict[str, dict],
        session_counts: dict[str, int],
    ) -> tuple[CleanupResult, dict]:
        rows, verified = self._saved_attachment_rows_locked(documents)
        results = [CleanupResult(skipped=(
            self._attachment_store.audit_entries(
                self._valid_session_ids(documents))
        ))]
        grouped: dict[str, list[str]] = {}
        for row in rows:
            grouped.setdefault(row["session_path"], []).append(
                row["attachment_id"])
        for path, attachment_ids in grouped.items():
            _data, result = self._delete_attachment_batch_locked(
                path, attachment_ids, documents, session_counts, verified)
            results.append(result)
        return self._merge_cleanup_results(*results), verified

    def delete_all_saved_attachments(self) -> dict:
        with self._lock:
            live_snapshot, documents = self._prepare_history_operation_locked()
            result, _verified = self._delete_all_saved_attachments_locked(
                documents, live_snapshot[0])
            return result.as_dict()

    def delete_all_saved_attachments_snapshot(
        self, current_path: str | None = None
    ) -> dict:
        with self._lock:
            live_snapshot, documents = self._prepare_history_operation_locked()
            result, verified = self._delete_all_saved_attachments_locked(
                documents, live_snapshot[0])
            snapshot = self._attachment_manager_snapshot_locked(
                documents, current_path, verified)
            snapshot["result"] = result.as_dict()
            return snapshot

    def exportable_sessions(self) -> list[dict]:
        with self._lock:
            _snapshot, documents = self._prepare_history_operation_locked()
            return [
                portable_session_snapshot(
                    documents[self._document_key(item["path"])])
                for item in self._list_sessions_locked()
                if self._document_key(item["path"]) in documents
            ]

    def existing_session_digests(self) -> set[str]:
        with self._lock:
            return {
                session_digest(session)
                for session in self.exportable_sessions()
            }

    def import_sessions(
        self, sessions: list[dict], *, skip_duplicates: bool = True
    ) -> tuple[list[str], int]:
        with self._lock:
            validated = []
            existing = self.existing_session_digests()
            seen = set(existing)
            duplicates = 0
            for session in sessions:
                portable = portable_session_snapshot(session)
                validate_portable_session(portable)
                digest = session_digest(portable)
                if skip_duplicates and digest in seen:
                    duplicates += 1
                    continue
                seen.add(digest)
                # Portable archives intentionally have no local attachment
                # identity.  A session ID is assigned lazily if a new local
                # attachment is added after import.
                validated.append(dict(portable))
            staged = []
            stage_paths = []
            committed = []
            try:
                for session in validated:
                    stage = os.path.join(
                        self._log_dir, f".import.{uuid.uuid4().hex}.tmp"
                    )
                    stage_paths.append(stage)
                    self._write_encrypted(stage, session)
                    verified, encrypted, _raw = self._read_document(stage)
                    if not encrypted or session_digest(verified) != session_digest(
                        session
                    ):
                        raise HistoryCryptoError(
                            "インポート履歴のDPAPI照合に失敗しました。"
                        )
                    staged.append((stage, session))
                for stage, session in staged:
                    while True:
                        target = os.path.join(
                            self._log_dir,
                            f"chat_import_{uuid.uuid4().hex}.json",
                        )
                        if not os.path.exists(target):
                            break
                    os.replace(stage, target)
                    committed.append(target)
                    self._cache_session(target, session)
                return committed, duplicates
            except Exception as original_error:
                rollback_failures = []
                for target in committed:
                    try:
                        os.remove(target)
                    except FileNotFoundError:
                        continue
                    except OSError:
                        rollback_failures.append(target)
                    self._index.pop(target, None)
                if rollback_failures:
                    raise HistoryCryptoError(
                        "インポートの巻き戻しに失敗しました。"
                        "追加された履歴を確認してください。"
                    ) from original_error
                raise
            finally:
                for stage in stage_paths:
                    try:
                        os.remove(stage)
                    except FileNotFoundError:
                        pass

    def _scan_legacy_with_diagnostics(self):
        legacy = []
        errors = []
        diagnostics = []
        for entry in os.scandir(self._log_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            try:
                _data, encrypted, _raw = self._read_document(entry.path)
                if not encrypted:
                    legacy.append(entry.path)
            except Exception as exc:
                errors.append(entry.path)
                diagnostics.append({
                    "name": entry.name,
                    "phase": "scan",
                    "error_type": type(exc).__name__,
                })
        return legacy, errors, diagnostics

    def scan_legacy(self) -> tuple[list[str], list[str]]:
        legacy, errors, _diagnostics = self._scan_legacy_with_diagnostics()
        return legacy, errors

    @contextmanager
    def _migration_lock(self):
        lock_path = os.path.join(self._log_dir, ".history_migration.lock")
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError as exc:
            raise HistoryCryptoError(
                "履歴移行が別のプロセスで実行中か、前回の異常終了で"
                "ロックが残っています。\n"
                "すべてのShiroが終了していることを確認した場合に限り、"
                "次のファイルを手動で削除して再起動してください。\n"
                f"{lock_path}"
            ) from exc
        try:
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            yield
        finally:
            try:
                os.remove(lock_path)
            except FileNotFoundError:
                pass

    def migrate_legacy(self, paths: list[str]) -> int:
        probe = b"llm-local-chat-dpapi-preflight"
        if self._protector.unprotect(
            self._protector.protect(probe)
        ) != probe:
            raise HistoryCryptoError("Windows DPAPIの事前検証に失敗しました。")

        failures = []
        diagnostics = []
        migrated = 0
        with self._migration_lock():
            # 起動時scan後に追加された平文履歴も、置換開始前に取り込む。
            current_legacy, scan_errors, scan_diagnostics = (
                self._scan_legacy_with_diagnostics()
            )
            if scan_errors:
                raise HistoryMigrationError(
                    scan_errors, diagnostics=scan_diagnostics)
            targets = list(dict.fromkeys([*paths, *current_legacy]))
            for path in targets:
                try:
                    before = os.stat(path)
                    with open(path, "rb") as handle:
                        raw = handle.read()
                    _data, encrypted, plaintext = decode_document(
                        raw, self._protector
                    )
                    if encrypted:
                        continue
                    envelope = encode_envelope(plaintext, self._protector)
                    _check, check_encrypted, verified = decode_document(
                        envelope, self._protector
                    )
                    if not check_encrypted or verified != raw:
                        raise HistoryCryptoError(
                            "暗号化会話履歴の照合に失敗しました。"
                        )
                    current = os.stat(path)
                    if (
                        current.st_mtime_ns != before.st_mtime_ns
                        or current.st_size != before.st_size
                    ):
                        raise HistoryCryptoError(
                            "移行中に会話履歴が更新されました。"
                        )
                    with open(path, "rb") as handle:
                        if handle.read() != raw:
                            raise HistoryCryptoError(
                                "移行中に会話履歴の内容が更新されました。"
                            )
                    atomic_write_bytes(path, envelope)
                    os.utime(
                        path,
                        ns=(before.st_atime_ns, before.st_mtime_ns),
                    )
                    migrated += 1
                except Exception as exc:
                    failures.append(path)
                    diagnostics.append({
                        "name": os.path.basename(path),
                        "phase": "encrypt",
                        "error_type": type(exc).__name__,
                    })
            remaining_legacy, remaining_errors, remaining_diagnostics = (
                self._scan_legacy_with_diagnostics()
            )
            failures.extend(remaining_legacy)
            failures.extend(remaining_errors)
            diagnostics.extend({
                "name": os.path.basename(path),
                "phase": "post_scan",
                "error_type": "LegacyHistoryRemaining",
            } for path in remaining_legacy)
            diagnostics.extend({
                **detail, "phase": "post_scan"
            } for detail in remaining_diagnostics)
        self._index.clear()
        if failures:
            raise HistoryMigrationError(
                list(dict.fromkeys(failures)),
                diagnostics=diagnostics,
            )
        return migrated

    def expired_paths(
        self, retention_days: int, current_path: str | None = None, now=None
    ) -> list[str]:
        days = normalize_retention_days(retention_days)
        if days == 0:
            return []
        now = now or datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(days=days)
        current_norm = (
            os.path.normcase(os.path.abspath(current_path))
            if current_path else None
        )
        expired = []
        for entry in os.scandir(self._log_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            path_norm = os.path.normcase(os.path.abspath(entry.path))
            if current_norm and path_norm == current_norm:
                continue
            try:
                data, encrypted, _raw = self._read_document(entry.path)
                if not encrypted:
                    continue
                activity_text = data.get("last_activity_at")
                if activity_text:
                    activity = datetime.datetime.fromisoformat(activity_text)
                    if activity.tzinfo is None:
                        raise ValueError("timezone is required")
                    activity = activity.astimezone(datetime.timezone.utc)
                else:
                    activity = datetime.datetime.fromtimestamp(
                        entry.stat().st_mtime, datetime.timezone.utc
                    )
                if activity < cutoff:
                    expired.append(entry.path)
            except Exception:
                continue
        return expired

    def prune_expired(
        self, retention_days: int, current_path: str | None = None, now=None
    ) -> tuple[int, list[str]]:
        failures = []
        deleted = 0
        for path in self.expired_paths(retention_days, current_path, now):
            try:
                self.delete(path)
                deleted += 1
            except Exception:
                failures.append(path)
        return deleted, failures
