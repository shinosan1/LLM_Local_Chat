import datetime
import json
import os


class SessionStore:
    def __init__(self, log_dir: str):
        self._log_dir = log_dir
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
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = os.path.join(self._log_dir, f"chat_{ts}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(session, f, ensure_ascii=False, indent=2)
        return path

    def list_sessions(self, keyword: str = "") -> list[dict]:
        sessions = []
        kw = keyword.strip().lower()
        if not os.path.exists(self._log_dir):
            return sessions
        for fn in sorted(os.listdir(self._log_dir), reverse=True):
            if not fn.endswith(".json"):
                continue
            fp = os.path.join(self._log_dir, fn)
            try:
                with open(fp, encoding="utf-8") as f:
                    data = json.load(f)
                title = data.get("title", fn)
                summary = data.get("summary", "")
                if kw and kw not in (title + summary).lower():
                    continue
                sessions.append({"path": fp, "title": title, "summary": summary})
            except Exception:
                pass
        return sessions

    def load(self, path: str) -> dict:
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def delete(self, path: str) -> None:
        os.remove(path)

    def rename(self, path: str, new_title: str) -> dict:
        data = self.load(path)
        data["title"] = new_title
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        return data
