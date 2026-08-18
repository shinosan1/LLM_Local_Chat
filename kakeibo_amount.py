"""ユーザー原文からの金額抽出・正規化。

LLM生成のamountは最終値として採用しない(Section C確定仕様)。
このモジュールはユーザー原文だけを解析し、通貨単位を伴う数値表現から
正の整数円を機械的に抽出する。UI・LLM・DBには一切依存しない。
"""
import re

_FULLWIDTH_TRANSLATION = str.maketrans("０１２３４５６７８９，．", "0123456789,.")


def _normalize_digits(text: str) -> str:
    """全角数字・全角カンマ・全角ピリオドだけを半角へ変換する。符号や単位は変更しない。"""
    return text.translate(_FULLWIDTH_TRANSLATION)


# 数字の前置符号として扱う文字。半角/全角の各種ハイフン・マイナス記号、
# en/emダッシュ(音声認識や貼り付けで混入しやすい)を含む。
_SIGN_CHARS = r'\-−+＋－﹣–—'

# 正常な金額表現: 数字列(カンマ可)+空白(0文字以上)+単位。
# 直前が数字・カンマ・ピリオド・万・千・符号・英字(指数表記等の一部)の場合は
# 複合表現/小数/符号付き/指数表記の一部分なので除外し、直後が数字の場合も除外する。
_AMOUNT_UNIT_PATTERN = re.compile(
    rf'(?<![0-9,.万千a-zA-Z{_SIGN_CHARS}])'
    r'(?P<number>[0-9,]+)'
    r'[ 　]*'
    r'(?P<unit>万円|千円|円)'
    r'(?![0-9])'
)

# 以下はすべて「不正な通貨表現」として、正常候補より優先して検出するパターン。
# 記号による符号だけでなく、音声入力で出やすい「マイナス」「プラス」も対象にする。
_SIGNED_AMOUNT_PATTERN = re.compile(
    rf'(?:[{_SIGN_CHARS}]|マイナス|プラス)\s*[0-9,]+\s*(?:万円|千円|円)'
)
_DECIMAL_AMOUNT_PATTERN = re.compile(r'[0-9]+\.[0-9]+\s*(?:万円|千円|円)')
_COMPOUND_UNIT_AMOUNT_PATTERN = re.compile(
    r'[0-9]+\s*(?:万|千)\s*[0-9]+\s*(?:万円|千円|円)'
)
# 数字列の途中に空白が入った表記(例: "1 500円")。後半だけを正常抽出させない。
_SPACED_DIGITS_AMOUNT_PATTERN = re.compile(
    r'[0-9,]+[ 　]+[0-9,]+[ 　]*(?:万円|千円|円)'
)
# 指数表記(例: "1e3円"/"1E3円"/"1e+3円"/"1e-3円")。
# lookbehindへの英字追加だけでは同一原文中の他の正常金額を採用してしまうため、
# 指数表記自体を不正表現として明示的に検出し、正常候補抽出より先に判定する。
_EXPONENT_AMOUNT_PATTERN = re.compile(
    r'[0-9]+(?:\.[0-9]+)?[eE][+\-]?[0-9]+[ 　]*(?:万円|千円|円)'
)

_UNIT_MULTIPLIERS = {"万円": 10000, "千円": 1000, "円": 1}


def _parse_amount_match(number_str: str, unit: str) -> int | None:
    """カンマ区切りが正しい場合だけ整数へ変換し、不正なら None を返す。"""
    if number_str.startswith(",") or number_str.endswith(","):
        return None
    if "," in number_str:
        if not re.fullmatch(r"\d{1,3}(,\d{3})*", number_str):
            return None
        digits = number_str.replace(",", "")
    else:
        digits = number_str
    if not digits:
        return None
    return int(digits) * _UNIT_MULTIPLIERS[unit]


def extract_amount_result(text: str) -> dict:
    """原文から金額を抽出し、判定結果を返す。

    戻り値の "status":
      - "ok": 有効な金額候補が1件だけ確定できた("amount"に整数を格納)
      - "no_amount": 通貨表現自体が存在しなかった
      - "invalid_amount_format": 通貨表現はあるが不正(小数・符号付き・複合単位・
        桁区切り不正・数字列途中の空白等)。不正表現が1件でもあれば、
        同じ原文中に正常な金額があっても優先して全体を拒否する
      - "multiple_amounts": 有効な金額候補が複数件ある
    """
    normalized = _normalize_digits(text)

    if (
        _SIGNED_AMOUNT_PATTERN.search(normalized)
        or _DECIMAL_AMOUNT_PATTERN.search(normalized)
        or _COMPOUND_UNIT_AMOUNT_PATTERN.search(normalized)
        or _SPACED_DIGITS_AMOUNT_PATTERN.search(normalized)
        or _EXPONENT_AMOUNT_PATTERN.search(normalized)
    ):
        return {"status": "invalid_amount_format", "amounts": []}

    valid_amounts = []
    saw_invalid_match = False
    for match in _AMOUNT_UNIT_PATTERN.finditer(normalized):
        value = _parse_amount_match(match.group("number"), match.group("unit"))
        if value is None or value <= 0:
            saw_invalid_match = True
            continue
        valid_amounts.append(value)

    if saw_invalid_match:
        return {"status": "invalid_amount_format", "amounts": []}
    if not valid_amounts:
        return {"status": "no_amount", "amounts": []}
    if len(valid_amounts) > 1:
        return {"status": "multiple_amounts", "amounts": valid_amounts}
    return {"status": "ok", "amount": valid_amounts[0], "amounts": valid_amounts}


def normalize_manual_amount_input(raw: str) -> int | None:
    """確認画面でユーザーが入力したamount文字列を正の整数円へ正規化する。

    小数・符号・単位・ゼロ・空欄・不正な桁区切りカンマは None を返す。
    """
    normalized = _normalize_digits(raw).strip()
    if not normalized or not re.fullmatch(r"[0-9,]+", normalized):
        return None
    if "," in normalized:
        if not re.fullmatch(r"\d{1,3}(,\d{3})*", normalized):
            return None
        digits = normalized.replace(",", "")
    else:
        digits = normalized
    if not digits:
        return None
    value = int(digits)
    return value if value > 0 else None


def find_amount_spans(text: str) -> list[tuple[int, int, int]]:
    """有効な金額表現を (開始位置, 終了位置, 円換算値) の一覧で返す。

    位置は元の `text` に対する添字。`_normalize_digits` は1文字を1文字へ
    置換するだけなので、正規化後の位置はそのまま元文字列の位置として使える。

    extract_amount_result と同じパターン・同じ換算規則を使い、判定の二重実装を
    避ける。不正な通貨表現(小数・符号付き・複合単位等)が含まれる場合は、
    どこまでを1件と数えてよいか決められないため空リストを返す。有効な金額が
    無い場合も空リストになるため、呼び出し側は必要に応じて
    extract_amount_result の status と併せて判断する。
    """
    normalized = _normalize_digits(text)

    if (
        _SIGNED_AMOUNT_PATTERN.search(normalized)
        or _DECIMAL_AMOUNT_PATTERN.search(normalized)
        or _COMPOUND_UNIT_AMOUNT_PATTERN.search(normalized)
        or _SPACED_DIGITS_AMOUNT_PATTERN.search(normalized)
        or _EXPONENT_AMOUNT_PATTERN.search(normalized)
    ):
        return []

    spans: list[tuple[int, int, int]] = []
    for match in _AMOUNT_UNIT_PATTERN.finditer(normalized):
        value = _parse_amount_match(match.group("number"), match.group("unit"))
        if value is None or value <= 0:
            return []
        spans.append((match.start(), match.end(), value))
    return spans
