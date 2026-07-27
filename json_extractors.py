import json
import re


HEALTH_KEYS = frozenset({
    "weight", "body_fat", "muscle_mass", "bmr",
    "temperature", "pulse", "systolic_bp", "diastolic_bp",
    "meal_detail", "activity_log", "memo",
})
_HEALTH_KEY_PATTERN = "|".join(
    re.escape(key) for key in sorted(HEALTH_KEYS)
)
_BARE_HEALTH_JSON_RE = rf'\{{[^{{}}]*"(?:{_HEALTH_KEY_PATTERN})"[^{{}}]*\}}'


_KAKEIBO_STRUCTURE_KEYS = frozenset({"type", "category", "store"})


def _reject_nonstandard_constant(value: str):
    raise ValueError(f"non-standard JSON constant: {value}")


def _strict_json_loads(raw: str):
    return json.loads(raw, parse_constant=_reject_nonstandard_constant)


def _looks_like_kakeibo_record(data) -> bool:
    """dictであり、amountキーを持ち、type/category/storeのいずれかを持つ場合だけ
    家計簿レコード候補とみなす。amount値の型・妥当性はここでは判定しない
    (最終amountは原文由来の値を使うため、ここではLLM出力の値を一切見ない)。"""
    return (
        isinstance(data, dict)
        and "amount" in data
        and bool(_KAKEIBO_STRUCTURE_KEYS & data.keys())
    )


def extract_kakeibo_json(reply: str) -> dict | None:
    patterns = [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'\{[^{}]*"amount"\s*:[^{}]*\}',
    ]
    for pat in patterns:
        for m in re.finditer(pat, reply, re.DOTALL):
            try:
                raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                data = json.loads(raw)
                if _looks_like_kakeibo_record(data):
                    return data
            except (json.JSONDecodeError, KeyError, AttributeError):
                pass
    return None


def extract_health_json(reply: str) -> dict | None:
    print(f"[Health] reply received ({len(reply)} chars)")
    patterns = [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        _BARE_HEALTH_JSON_RE,
    ]
    candidates = {}
    for pat in patterns:
        for m in re.finditer(pat, reply, re.DOTALL):
            try:
                raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                raw_start = m.start(1) if m.lastindex and m.lastindex >= 1 else m.start(0)
                data = _strict_json_loads(raw)
                if isinstance(data, dict) and HEALTH_KEYS & data.keys():
                    candidates[(raw_start, raw)] = data
            except (json.JSONDecodeError, KeyError, AttributeError, ValueError):
                pass
    if not candidates:
        return None
    return max(candidates.items(), key=lambda item: item[0][0])[1]


def strip_health_json(reply: str) -> str:
    """完全な健康JSONだけをユーザー向け履歴から除外する。"""
    def remove_if_health(match: re.Match) -> str:
        raw = match.group(1) if match.lastindex else match.group(0)
        try:
            data = _strict_json_loads(raw)
        except (json.JSONDecodeError, ValueError):
            return match.group(0)
        return "" if isinstance(data, dict) and HEALTH_KEYS & data.keys() else match.group(0)

    cleaned = re.sub(
        r'```(?:json)?\s*(\{.*?\})\s*```',
        remove_if_health,
        reply,
        flags=re.DOTALL | re.IGNORECASE,
    )
    cleaned = re.sub(
        _BARE_HEALTH_JSON_RE,
        remove_if_health,
        cleaned,
        flags=re.DOTALL,
    )
    return cleaned.strip()
