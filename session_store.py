import datetime
import json
import os
from contextlib import contextmanager

from atomic_io import atomic_write_bytes
from history_crypto import (
    DPAPIProtector,
    HistoryCryptoError,
    decode_document,
    encode_envelope,
    validate_session,
)


ALLOWED_RETENTION_DAYS = (0, 30, 90, 180, 365)


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
    def __init__(self, log_dir: str, protector=None):
        self._log_dir = log_dir
        self._protector = protector if protector is not None else DPAPIProtector()
        self._index: dict[str, dict] = {}
        os.makedirs(self._log_dir, exist_ok=True)

    def new_session(self) -> dict:
        return {
            "title": "新しいチャット",
            "history": [],
            "summary": "",
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
        )
        prepared["last_activity_at"] = (
            self._utc_now_text()
            if changed
            else previous_activity or self._utc_now_text()
        )
        return prepared

    def save(self, session: dict, path: str | None) -> str | None:
        history = session.get("history", [])
        if not history:
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
        return {
            "path": path,
            "title": data.get("title", fallback_title),
            "summary": data.get("summary", ""),
        }

    def _cache_session(self, path: str, session: dict) -> None:
        stat = os.stat(path)
        self._index[path] = {
            "signature": (
                stat.st_mtime_ns,
                stat.st_size,
                stat.st_ctime_ns,
            ),
            "metadata": {
                "path": path,
                "title": session.get("title", os.path.basename(path)),
                "summary": session.get("summary", ""),
            },
        }

    def _refresh_index(self) -> None:
        if not os.path.exists(self._log_dir):
            self._index.clear()
            return

        current_paths = set()
        for entry in os.scandir(self._log_dir):
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            path = entry.path
            current_paths.add(path)
            try:
                stat = entry.stat()
                signature = (
                    stat.st_mtime_ns,
                    stat.st_size,
                    stat.st_ctime_ns,
                )
                cached = self._index.get(path)
                if cached and cached["signature"] == signature:
                    continue
                try:
                    metadata = self._read_metadata(path, entry.name)
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

    def list_sessions(self, keyword: str = "") -> list[dict]:
        self._refresh_index()
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

    def load(self, path: str) -> dict:
        data, encrypted, _raw = self._read_document(path)
        if not encrypted:
            raise HistoryCryptoError(
                "暗号化されていない会話履歴です。起動時の移行を実行してください。"
            )
        return data

    def delete(self, path: str) -> None:
        os.remove(path)
        self._index.pop(path, None)

    def rename(self, path: str, new_title: str) -> dict:
        data = self.load(path)
        data["title"] = new_title
        if not data.get("last_activity_at"):
            data["last_activity_at"] = self._timestamp_from_mtime(path)
        self._write_encrypted(path, data)
        self._cache_session(path, data)
        return data

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
            except OSError:
                failures.append(path)
        return deleted, failures
