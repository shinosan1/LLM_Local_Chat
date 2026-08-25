import json
import math
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from tkinter import messagebox

from kakeibo_confirmation import validate_kakeibo_payload


KAKEIBO_BRIDGE_PORT = int(os.getenv("KAKEIBO_BRIDGE_PORT", "8767"))
if not 1 <= KAKEIBO_BRIDGE_PORT <= 65535:
    raise RuntimeError("KAKEIBO_BRIDGE_PORTは1～65535で指定してください")
KAKEIBO_API_URL = os.getenv(
    "KAKEIBO_API_URL", f"http://127.0.0.1:{KAKEIBO_BRIDGE_PORT}"
) + "/api/kakeibo/record"
BIOLOG_API_URL = os.getenv("BIOLOG_URL", "http://localhost:8766") + "/api/health/record"  # v1.1.0
LOCAL_API_HOSTS = {"localhost", "127.0.0.1", "::1"}
# 既定のローカルブリッジのポートのみ許可する。環境変数でホストを差し替えられても、
# localhost上の無関係なサービスへJSONを送らないための多層防御。
# bridgeの変更時は接続先と同じKAKEIBO_BRIDGE_PORTを明示する。
LOCAL_API_PORTS = {KAKEIBO_BRIDGE_PORT, 8766}
BIOLOG_RECORD_KEYS = (
    "date", "weight", "body_fat", "muscle_mass", "bmr",
    "temperature", "pulse", "systolic_bp", "diastolic_bp",
    "meal_detail", "activity_log", "memo",
)
BIOLOG_VALUE_KEYS = tuple(k for k in BIOLOG_RECORD_KEYS if k != "date")
BIOLOG_FLOAT_RANGES = {
    "temperature": (34.0, 42.0, True, True),
    "weight": (0.0, 300.0, False, False),
    "body_fat": (0.0, 100.0, True, True),
    "muscle_mass": (0.0, 200.0, False, False),
}
BIOLOG_INTEGER_RANGES = {
    "pulse": (30, 200, True, True),
    "systolic_bp": (50, 250, True, True),
    "diastolic_bp": (30, 150, True, True),
    "bmr": (0, 5000, False, False),
}
BIOLOG_TEXT_FIELDS = ("meal_detail", "activity_log", "memo")
# LLMが異常に長い文字列を返した場合に、そのままBiolog DBへ流し込まないための上限。
BIOLOG_TEXT_MAX_LENGTH = 2000
_BIOLOG_JST = timezone(timedelta(hours=9))
MEASUREMENT_FIELDS = {
    "体脂肪率": ("body_fat",),
    "収縮期血圧": ("systolic_bp",),
    "拡張期血圧": ("diastolic_bp",),
    "基礎代謝": ("bmr",),
    "筋肉量": ("muscle_mass",),
    "体温": ("temperature",),
    "脈拍": ("pulse",),
    "血圧": ("systolic_bp", "diastolic_bp"),
    "体重": ("weight",),
}
_MEASUREMENT_FRAGMENT_RE = re.compile(
    r"(体脂肪率|収縮期血圧|拡張期血圧|基礎代謝|筋肉量|体温|脈拍|血圧|体重)"
    r"\s*(?:は|が|:|：|=)?\s*"
    r"\d+(?:\.\d+)?(?:\s*/\s*\d+(?:\.\d+)?)?\s*"
    r"(?:%|％|kg|℃|mmHg|bpm|kcal)?\s*(?:です|でした)?"
)
_MEASUREMENT_SEPARATOR_RE = re.compile(r"[\s、,，]+")
_EXPLICIT_HEALTH_LABELS = {
    "食事ログ追加": "meal_detail",
    "食事ログ": "meal_detail",
    "行動ログ追加": "activity_log",
    "行動ログ": "activity_log",
    "メモ追加": "memo",
    "メモ": "memo",
}
_EXPLICIT_HEALTH_LABEL_RE = re.compile(
    r"(?P<prefix>^|[\s、,，])"
    r"(?P<label>食事ログ追加|行動ログ追加|メモ追加|食事ログ|行動ログ|メモ)"
    r"(?=$|[\s:：、,，])"
)
_EXPLICIT_BOUNDARY_CHARS = " \u3000\t\r\n:：、,，"
_LABEL_PARTICLES = frozenset("をはがにでともへのや")


class BiologValidationError(ValueError):
    """Biologへ送信できない型・値を検出したことを表す。"""


def _biolog_jst_today() -> str:
    """Biolog APIの日付省略時と同じJST当日を返す。"""
    return datetime.now(_BIOLOG_JST).date().isoformat()


def _valid_biolog_date(value) -> str | None:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return None
    try:
        date.fromisoformat(value)
    except ValueError:
        return None
    return value


def _finalize_biolog_record_date(record: dict) -> dict:
    """送信直前のrecordへ、実際に登録する確定日付を保持する。"""
    finalized = dict(record)
    raw_date = finalized.get("date")
    if raw_date in (None, ""):
        finalized["date"] = _biolog_jst_today()
    elif _valid_biolog_date(raw_date) is None:
        raise BiologValidationError("date must be a real YYYY-MM-DD date")
    return finalized


def _biolog_completion_date(response_payload, post_payload: dict) -> str | None:
    """API応答日付を優先し、なければ実際のPOST日付を返す。"""
    if isinstance(response_payload, dict):
        response_date = _valid_biolog_date(response_payload.get("date"))
        if response_date is not None:
            return response_date
    return _valid_biolog_date(post_payload.get("date"))


def _in_range(value, limits) -> bool:
    lower, upper, lower_inclusive, upper_inclusive = limits
    lower_ok = value >= lower if lower_inclusive else value > lower
    upper_ok = value <= upper if upper_inclusive else value < upper
    return lower_ok and upper_ok


def _is_finite_number(value) -> bool:
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _validate_biolog_payload(payload: dict) -> dict:
    validated = dict(payload)

    raw_date = validated.get("date")
    if raw_date is None:
        validated.pop("date", None)
    elif not isinstance(raw_date, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", raw_date
    ):
        raise BiologValidationError("date must be YYYY-MM-DD")
    else:
        try:
            date.fromisoformat(raw_date)
        except ValueError as exc:
            raise BiologValidationError("date is not a real calendar date") from exc

    for field, limits in BIOLOG_FLOAT_RANGES.items():
        value = validated.get(field)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite_number(value)
            or not _in_range(value, limits)
        ):
            raise BiologValidationError(f"invalid {field}")

    for field, limits in BIOLOG_INTEGER_RANGES.items():
        value = validated.get(field)
        if value is None:
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not _is_finite_number(value)
            or not float(value).is_integer()
        ):
            raise BiologValidationError(f"invalid {field}")
        normalized = int(value)
        if not _in_range(normalized, limits):
            raise BiologValidationError(f"invalid {field}")
        validated[field] = normalized

    for field in BIOLOG_TEXT_FIELDS:
        value = validated.get(field)
        if value is None:
            continue
        if not isinstance(value, str):
            raise BiologValidationError(f"invalid {field}")
        if len(value) > BIOLOG_TEXT_MAX_LENGTH:
            raise BiologValidationError(
                f"{field} exceeds {BIOLOG_TEXT_MAX_LENGTH} characters"
            )

    return validated


def is_allowed_local_api_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "http"
        and parsed.username is None
        and parsed.password is None
        and host in LOCAL_API_HOSTS
        and port in LOCAL_API_PORTS
    )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_local_api(request, timeout):
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout)


def _read_json_response(response, max_bytes=1024 * 1024):
    raw = response.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise ValueError("ローカルAPIの応答サイズが上限を超えました。")
    return json.loads(raw.decode("utf-8"))


def extract_explicit_health_fields(user_text: str) -> tuple[dict, frozenset[str]]:
    """ユーザー原文から明示された健康ログ欄だけを決定的に抽出する。"""
    if not isinstance(user_text, str) or not user_text.strip():
        return {}, frozenset()

    candidates = []
    for match in _EXPLICIT_HEALTH_LABEL_RE.finditer(user_text):
        value_start = match.end()
        while (
            value_start < len(user_text)
            and user_text[value_start] in _EXPLICIT_BOUNDARY_CHARS
        ):
            value_start += 1
        if (
            value_start < len(user_text)
            and user_text[value_start] in _LABEL_PARTICLES
        ):
            continue
        candidates.append((match, value_start))

    field_values: dict[str, list[str]] = {}
    for index, (match, value_start) in enumerate(candidates):
        value_end = (
            candidates[index + 1][0].start()
            if index + 1 < len(candidates)
            else len(user_text)
        )
        value = user_text[value_start:value_end].strip(_EXPLICIT_BOUNDARY_CHARS)
        if not value:
            continue
        field = _EXPLICIT_HEALTH_LABELS[match.group("label")]
        values = field_values.setdefault(field, [])
        if value not in values:
            values.append(value)

    fields = {field: "\n".join(values) for field, values in field_values.items()}
    return fields, frozenset(fields)


def prepare_biolog_record(
    record: dict | None, user_text: str
) -> tuple[dict | None, frozenset[str]]:
    """LLM抽出結果へユーザーの明示ラベル値を優先マージする。"""
    explicit_values, explicit_fields = extract_explicit_health_fields(user_text)
    merged = dict(record) if isinstance(record, dict) else {}
    merged.update(explicit_values)
    return sanitize_biolog_record(merged, explicit_fields), explicit_fields


def sanitize_biolog_record(
    record: dict, explicit_fields=None
) -> dict | None:
    if not isinstance(record, dict):
        return None
    explicit = frozenset(explicit_fields or ())
    payload = {k: record[k] for k in BIOLOG_RECORD_KEYS if k in record}
    payload = _validate_biolog_payload(payload)
    for key in BIOLOG_TEXT_FIELDS:
        value = payload.get(key)
        if isinstance(value, str) and not value.strip():
            payload[key] = None
    if payload.get("memo") is None:
        payload.pop("memo", None)
    if (
        "activity_log" not in explicit
        and _is_measurement_only_text(payload, "activity_log")
    ):
        payload["activity_log"] = None
    if (
        "memo" not in explicit
        and _is_measurement_only_text(payload, "memo")
    ):
        payload.pop("memo", None)
    if not any(payload.get(k) not in (None, "") for k in BIOLOG_VALUE_KEYS):
        return None
    return payload


def _has_numeric_measurement(payload: dict, fields: tuple[str, ...]) -> bool:
    return all(
        isinstance(payload.get(field), (int, float))
        and not isinstance(payload.get(field), bool)
        for field in fields
    )


def _is_measurement_only_text(payload: dict, field: str) -> bool:
    """指定欄の全体が、payloadに対応値を持つ測定文だけか判定する。"""
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        return False

    text = value.strip()
    position = 0
    matched = False
    for match in _MEASUREMENT_FRAGMENT_RE.finditer(text):
        gap = text[position:match.start()]
        if gap and _MEASUREMENT_SEPARATOR_RE.fullmatch(gap) is None:
            return False
        fields = MEASUREMENT_FIELDS[match.group(1)]
        if not _has_numeric_measurement(payload, fields):
            return False
        matched = True
        position = match.end()

    tail = text[position:]
    if tail and _MEASUREMENT_SEPARATOR_RE.fullmatch(tail) is None:
        return False
    return matched and position > 0


class IntegrationBridge:
    def __init__(self, root, chat_write):
        self.root = root
        self._chat_write = chat_write
        self._worker_lock = threading.Lock()
        self._workers: dict[threading.Thread, str] = {}
        self._closing = False

    def _post_ui(self, callback) -> None:
        with self._worker_lock:
            if self._closing:
                return

        def _run_if_open():
            with self._worker_lock:
                if self._closing:
                    return
            callback()

        try:
            self.root.after(0, _run_if_open)
        except Exception:
            pass

    def _start_worker(self, label: str, target) -> bool:
        def _tracked_target():
            try:
                target()
            finally:
                current = threading.current_thread()
                with self._worker_lock:
                    self._workers.pop(current, None)

        thread = threading.Thread(target=_tracked_target, daemon=True)
        with self._worker_lock:
            if self._closing:
                return False
            self._workers[thread] = label
        thread.start()
        return True

    def begin_closing(self) -> None:
        with self._worker_lock:
            self._closing = True

    def pending_operations(self) -> list[str]:
        with self._worker_lock:
            return sorted(self._workers.values())

    def send_kakeibo(self, record: dict, on_complete=None) -> None:
        """確認画面(KakeiboConfirmDialog)でユーザーが確定したペイロードの
        最終検証とAPI送信だけを担当する。確認自体はダイアログ側で完結済み。

        `on_complete` を渡すと、送信の成否(bool)を引数にUIスレッドから1回だけ
        呼び出す。複数取引を1件ずつ直列に送るために、呼び出し側はこの通知を
        受けてから次の候補へ進む。送信を開始できなかった場合も失敗として通知する。
        """
        def _notify(success: bool) -> None:
            if on_complete is None:
                return
            self._post_ui(lambda s=success: on_complete(s))

        if self._closing:
            _notify(False)
            return
        payload = validate_kakeibo_payload(record)
        if not payload:
            self._chat_write(
                "⚠ 家計簿へ送信可能な項目がないため登録しませんでした。\n",
                "err",
            )
            _notify(False)
            return
        if not is_allowed_local_api_url(KAKEIBO_API_URL):
            self._chat_write(
                "⚠ 家計簿APIの送信先がローカルではないため登録を中止しました。\n",
                "err",
            )
            _notify(False)
            return
        self._send_to_kakeibo_api(payload, _notify)

    def _send_to_kakeibo_api(self, record: dict, notify=None) -> None:
        def _worker():
            succeeded = False
            try:
                body = json.dumps(record, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    KAKEIBO_API_URL,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with _open_local_api(req, timeout=5) as resp:
                    _read_json_response(resp)
                store  = record.get("store") or "不明"
                amount = record.get("amount", 0)
                cat    = record.get("category", "")
                rtype  = record.get("type", "支出")
                date   = record.get("date", "")
                msg = (
                    f"✅ 家計簿に登録しました\n"
                    f"   {date}  {store}  {amount:,}円  [{cat}/{rtype}]\n"
                )
                self._post_ui(
                    lambda m=msg: self._chat_write(m, "kakeibo_ok"))
                succeeded = True
            except urllib.error.URLError as e:
                reason = getattr(e, "reason", None) or e
                self._post_ui(
                    lambda r=reason: self._chat_write(
                        f"⚠ 家計簿ブリッジに接続できません: {r}\n",
                        "err",
                    ),
                )
            except Exception as e:
                self._post_ui(
                    lambda err=e: self._chat_write(
                        f"⚠ 家計簿登録エラー: {err}\n", "err"),
                )
            finally:
                if notify is not None:
                    notify(succeeded)

        if not self._start_worker("kakeibo_api", _worker):
            if notify is not None:
                notify(False)

    def confirm_and_send_biolog(
        self, record: dict, explicit_fields=None
    ) -> None:
        if self._closing:
            return
        try:
            payload = sanitize_biolog_record(record, explicit_fields)
        except BiologValidationError as exc:
            print(f"[Biolog] validation failed before confirmation: {exc}")
            self._chat_write(
                "⚠ 健康記録に不正な形式または範囲外の値があるため、"
                "Biologへ送信しませんでした。\n",
                "err",
            )
            return
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
        try:
            finalized_record = _finalize_biolog_record_date(record)
        except BiologValidationError as exc:
            print(f"[Biolog] validation failed before send: {exc}")
            self._chat_write(
                "⚠ 健康記録の日付を確定できないため、Biologへ送信しませんでした。\n",
                "err",
            )
            return

        def _worker():
            try:
                payload = {"user_id": "self", **finalized_record}
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                req = urllib.request.Request(
                    BIOLOG_API_URL, data=body,
                    headers={"Content-Type": "application/json"}, method="POST",
                )
                with _open_local_api(req, timeout=5) as resp:
                    response_payload = _read_json_response(resp)
                completed_date = _biolog_completion_date(response_payload, payload)
                if completed_date is None:
                    print("[Biolog] completed without a confirmed record date")
                    msg = "✅ Biolog記録完了（日付を確認できません）\n"
                else:
                    msg = f"✅ Biolog記録完了: {completed_date}\n"
                self._post_ui(lambda m=msg: self._chat_write(m, "health_ok"))
            except urllib.error.HTTPError as e:
                s    = e.read().decode("utf-8", errors="replace")[:200]
                code = e.code
                self._post_ui(lambda s=s, code=code: self._chat_write(
                    f"⚠ BiologAPIエラー ({code}): {s}\n", "err"))
            except urllib.error.URLError as e:
                r = getattr(e, "reason", None) or e
                self._post_ui(lambda r=r: self._chat_write(
                    f"⚠ Biologに接続できません: {r}\n", "err"))
            except Exception as e:
                self._post_ui(lambda err=e: self._chat_write(
                    f"⚠ Biologエラー: {err}\n", "err"))
        self._start_worker("biolog_api", _worker)
