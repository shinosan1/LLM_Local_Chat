import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from tkinter import messagebox


KAKEIBO_API_URL = os.getenv("KAKEIBO_API_URL", "http://localhost:8765") + "/api/kakeibo/record"
BIOLOG_API_URL = os.getenv("BIOLOG_URL", "http://localhost:8766") + "/api/health/record"  # v1.1.0
LOCAL_API_HOSTS = {"localhost", "127.0.0.1", "::1"}
KAKEIBO_RECORD_KEYS = ("date", "store", "amount", "category", "type", "memo")
BIOLOG_RECORD_KEYS = (
    "date", "weight", "body_fat", "muscle_mass", "bmr",
    "temperature", "pulse", "systolic_bp", "diastolic_bp",
    "meal_detail", "activity_log",
)
BIOLOG_VALUE_KEYS = tuple(k for k in BIOLOG_RECORD_KEYS if k != "date")


def is_allowed_local_api_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        _ = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return parsed.scheme == "http" and host in LOCAL_API_HOSTS


def sanitize_kakeibo_record(record: dict) -> dict | None:
    if not isinstance(record, dict):
        return None
    payload = {k: record[k] for k in KAKEIBO_RECORD_KEYS if k in record}
    amount = payload.get("amount")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
        return None
    return payload


def sanitize_biolog_record(record: dict) -> dict | None:
    if not isinstance(record, dict):
        return None
    payload = {k: record[k] for k in BIOLOG_RECORD_KEYS if k in record}
    if not any(payload.get(k) not in (None, "") for k in BIOLOG_VALUE_KEYS):
        return None
    return payload


class IntegrationBridge:
    def __init__(self, root, chat_write):
        self.root = root
        self._chat_write = chat_write

    def confirm_and_send_kakeibo(self, record: dict) -> None:
        payload = sanitize_kakeibo_record(record)
        if not payload:
            self._chat_write(
                "⚠ 家計簿へ送信可能な項目がないため登録しませんでした。\n",
                "err",
            )
            return
        if not is_allowed_local_api_url(KAKEIBO_API_URL):
            self._chat_write(
                "⚠ 家計簿APIの送信先がローカルではないため登録を中止しました。\n",
                "err",
            )
            return
        msg = (
            "この内容を家計簿へ登録しますか？\n\n"
            + json.dumps(payload, ensure_ascii=False, indent=2)
        )
        if not messagebox.askyesno("家計簿へ登録", msg, icon="question"):
            self._chat_write("家計簿への登録をキャンセルしました。\n", "divider")
            return
        self._send_to_kakeibo_api(payload)

    def _send_to_kakeibo_api(self, record: dict) -> None:
        def _worker():
            try:
                body = json.dumps(record, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    KAKEIBO_API_URL,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    json.loads(resp.read().decode("utf-8"))
                store  = record.get("store") or "不明"
                amount = record.get("amount", 0)
                cat    = record.get("category", "")
                rtype  = record.get("type", "支出")
                date   = record.get("date", "")
                msg = (
                    f"✅ 家計簿に登録しました\n"
                    f"   {date}  {store}  {amount:,}円  [{cat}/{rtype}]\n"
                )
                self.root.after(
                    0, lambda m=msg: self._chat_write(m, "kakeibo_ok"))
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", None) or e
                self.root.after(
                    0,
                    lambda r=reason: self._chat_write(
                        f"⚠ 家計簿ブリッジに接続できません: {r}\n",
                        "err",
                    ),
                )
            except Exception as e:
                self.root.after(
                    0,
                    lambda err=e: self._chat_write(
                        f"⚠ 家計簿登録エラー: {err}\n", "err"),
                )

        threading.Thread(target=_worker, daemon=True).start()

    def confirm_and_send_biolog(self, record: dict) -> None:
        payload = sanitize_biolog_record(record)
        if not payload:
            self._chat_write(
                "⚠ Biologへ送信可能な健康記録項目がないため登録しませんでした。\n",
                "err",
            )
            return
        if not is_allowed_local_api_url(BIOLOG_API_URL):
            self._chat_write(
                "⚠ Biolog APIの送信先がローカルではないため登録を中止しました。\n",
                "err",
            )
            return
        payload_with_user = {"user_id": "self", **payload}
        msg = (
            "この内容をBiologへ登録しますか？\n\n"
            + json.dumps(payload_with_user, ensure_ascii=False, indent=2)
        )
        if not messagebox.askyesno("Biologへ登録", msg, icon="question"):
            self._chat_write("Biologへの登録をキャンセルしました。\n", "divider")
            return
        self._send_to_biolog_api(payload)

    def _send_to_biolog_api(self, record: dict) -> None:  # v1.2.0 IO専用
        def _worker():
            try:
                payload = {"user_id": "self", **record}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    BIOLOG_API_URL, data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=5) as resp:
                    json.loads(resp.read().decode("utf-8"))
                msg = f"✅ Biolog記録完了: {payload.get('date', '?')}\n"
                self.root.after(0, lambda m=msg: self._chat_write(m, "health_ok"))
            except urllib.error.HTTPError as e:
                s    = e.read().decode("utf-8", errors="replace")[:200]
                code = e.code
                self.root.after(0, lambda s=s, code=code: self._chat_write(
                    f"⚠ BiologAPIエラー ({code}): {s}\n", "err"))
            except urllib.error.URLError as e:
                r = getattr(e, "reason", None) or e
                self.root.after(0, lambda r=r: self._chat_write(
                    f"⚠ Biologに接続できません: {r}\n", "err"))
            except Exception as e:
                self.root.after(0, lambda err=e: self._chat_write(
                    f"⚠ Biologエラー: {err}\n", "err"))
        threading.Thread(target=_worker, daemon=True).start()
