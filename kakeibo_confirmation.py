"""家計簿の確認画面候補構築と、送信直前の最終検証。

Section C確定仕様(2026-07-20訂正版): LLM生成JSONは編集可能な初期候補にすぎない。
amount・dateはいずれもLLM値を一切参照せず、ユーザー原文からの機械抽出
(kakeibo_amount / kakeibo_date)だけで決定する。原文に明示的な日付表現が
なければ実行日を使う。UI(Tkinter)には一切依存しない純粋関数のみで構成する。
"""
import datetime
import re

from kakeibo_amount import extract_amount_result
from kakeibo_date import extract_date_from_text
from prompt_builder import KAKEIBO_EXPENSE_CATS, KAKEIBO_INCOME_CATS

KAKEIBO_RECORD_KEYS = ("date", "store", "amount", "category", "type", "memo")

# datetime.date.fromisoformat単体は "20260720" や "2026-W30-1" のような
# ISO 8601拡張形式も受理してしまうため、先にYYYY-MM-DDの形へ完全一致させる。
_ISO_DATE_FORMAT_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def is_valid_calendar_date(date_str) -> bool:
    """YYYY-MM-DD形式で、かつ実在する暦日かどうかを判定する。"""
    if not isinstance(date_str, str):
        return False
    if not _ISO_DATE_FORMAT_PATTERN.fullmatch(date_str):
        return False
    try:
        datetime.date.fromisoformat(date_str)
    except ValueError:
        return False
    return True


def _matches_verbatim(candidate_value, user_text: str) -> str:
    """空白除去後の候補文字列が原文に同一表記で含まれる場合だけその値を返す。"""
    if not isinstance(candidate_value, str):
        return ""
    stripped = candidate_value.strip()
    if not stripped or stripped not in user_text:
        return ""
    return stripped


def _normalize_type(llm_type) -> str | None:
    """支出・収入以外は未選択(None)として扱う。キーワード判定は行わない。"""
    return llm_type if llm_type in ("支出", "収入") else None


def _normalize_category(llm_category, type_value: str | None) -> str | None:
    """typeが未確定ならcategoryも選択不能(None)。確定していれば一覧外は「その他」。"""
    if type_value is None:
        return None
    allowed = KAKEIBO_EXPENSE_CATS if type_value == "支出" else KAKEIBO_INCOME_CATS
    if llm_category in allowed:
        return llm_category
    return "その他支出" if type_value == "支出" else "その他収入"


def build_kakeibo_candidate(llm_record: dict | None, user_text: str) -> dict:
    """LLM候補(Noneでもよい)とユーザー原文から確認画面の初期候補を構築する。

    "status" は amount 抽出結果("ok"/"no_amount"/"invalid_amount_format"/
    "multiple_amounts")。"ok" 以外は amount が None のまま返り、
    呼び出し側(Controller)はそれぞれ専用の案内を出して確認画面を開かない。
    """
    amount_result = extract_amount_result(user_text)
    record = llm_record if isinstance(llm_record, dict) else {}

    type_value = _normalize_type(record.get("type"))
    category_value = _normalize_category(record.get("category"), type_value)
    store_value = _matches_verbatim(record.get("store"), user_text)
    memo_value = _matches_verbatim(record.get("memo"), user_text)

    return {
        "status": amount_result["status"],
        "date": extract_date_from_text(user_text),
        "amount": amount_result.get("amount"),
        "type": type_value,
        "category": category_value,
        "store": store_value,
        "memo": memo_value,
    }


def can_submit_kakeibo_candidate(record: dict, confirmed: bool) -> bool:
    """確認画面の登録ボタン有効条件(UI用)。UI迂回時の最終防衛は validate_kakeibo_payload が別途担う。"""
    if not confirmed:
        return False
    type_value = record.get("type")
    if type_value not in ("支出", "収入"):
        return False
    amount_value = record.get("amount")
    if (
        isinstance(amount_value, bool)
        or not isinstance(amount_value, int)
        or amount_value <= 0
    ):
        return False
    if not is_valid_calendar_date(record.get("date")):
        return False
    allowed = KAKEIBO_EXPENSE_CATS if type_value == "支出" else KAKEIBO_INCOME_CATS
    return record.get("category") in allowed


def validate_kakeibo_payload(record: dict) -> dict | None:
    """API送信直前の最終検証。UIの登録ボタン判定とは独立したフェイルクローズ検証。

    UIを迂回して不正な値が渡された場合でも、ここですべて拒否できることを担保する。
    """
    if not isinstance(record, dict):
        return None

    payload = {k: record[k] for k in KAKEIBO_RECORD_KEYS if k in record}

    if not is_valid_calendar_date(payload.get("date")):
        return None

    amount_value = payload.get("amount")
    if (
        isinstance(amount_value, bool)
        or not isinstance(amount_value, int)
        or amount_value <= 0
    ):
        return None

    type_value = payload.get("type")
    if type_value not in ("支出", "収入"):
        return None

    allowed = KAKEIBO_EXPENSE_CATS if type_value == "支出" else KAKEIBO_INCOME_CATS
    if payload.get("category") not in allowed:
        return None

    store_value = payload.get("store", "")
    payload["store"] = store_value if isinstance(store_value, str) else ""

    memo_value = payload.get("memo", "")
    payload["memo"] = memo_value if isinstance(memo_value, str) else ""

    return payload
