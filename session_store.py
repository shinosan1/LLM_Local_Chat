import datetime
import json
import os

from atomic_io import atomic_write_json


class SessionStore:
    def __init__(self, log_dir: str):
        self._log_dir = log_dir
        self._index: dict[str, dict] = {}
        os.makedirs(self._log_dir, exist_ok=True)

    def new_session(self) -> dict:
        return {
            "title": "新しいチャット",
            "history": [],
            "summary": "",
        }

    def save(self, session: dict, path: str | None) -> str | None:
        history = session.get("history", [])
        if not history:
            return None
        if not path:
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            base_path = os.path.join(self._log_dir, f"chat_{ts}.json")
            path = base_path
            suffix = 1
            while os.path.exists(path):
                path = os.path.splitext(base_path)[0] + f"_{suffix}.json"
                suffix += 1
        atomic_write_json(path, session)
        self._cache_session(path, session)
        return path

    def _read_metadata(self, path: str, fallback_title: str) -> dict:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {
            "path": path,
            "title": data.get("title", fallback_title),
            "summary": data.get("summary", ""),
        }

    def _cache_session(self, path: str, session: dict) -> None:
        stat = os.stat(path)
        self._index[path] = {
            "signature": (stat.st_mtime_ns, stat.st_size),
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
                signature = (stat.st_mtime_ns, stat.st_size)
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
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def delete(self, path: str) -> None:
        os.remove(path)
        self._index.pop(path, None)

    def rename(self, path: str, new_title: str) -> dict:
        data = self.load(path)
        data["title"] = new_title
        atomic_write_json(path, data)
        self._cache_session(path, data)
        return data
