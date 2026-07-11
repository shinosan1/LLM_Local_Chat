import json
import re


def extract_kakeibo_json(reply: str) -> dict | None:
    patterns = [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'\{[^{}]*"amount"\s*:\s*\d+[^{}]*\}',
    ]
    for pat in patterns:
        for m in re.finditer(pat, reply, re.DOTALL):
            try:
                raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                data = json.loads(raw)
                if isinstance(data.get("amount"), (int, float)) and data["amount"] > 0:
                    return data
            except (json.JSONDecodeError, KeyError, AttributeError):
                pass
    return None


def extract_health_json(reply: str) -> dict | None:
    print(f"[Health] reply received ({len(reply)} chars)")
    HEALTH_KEYS = {
        "weight", "body_fat", "muscle_mass", "bmr",
        "temperature", "pulse", "systolic_bp", "diastolic_bp",
        "meal_detail", "activity_log",
    }
    patterns = [
        r'```json\s*(\{.*?\})\s*```',
        r'```\s*(\{.*?\})\s*```',
        r'\{[^{}]*"(?:weight|body_fat|meal_detail|temperature|pulse|systolic_bp|diastolic_bp)"[^{}]*\}',
    ]
    for pat in patterns:
        for m in re.finditer(pat, reply, re.DOTALL):
            try:
                raw = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
                data = json.loads(raw)
                if HEALTH_KEYS & set(data.keys()):
                    return data
            except (json.JSONDecodeError, KeyError, AttributeError):
                pass
    return None
