"""ユーザー原文からの日付抽出。

Section C確定仕様(2026-07-20訂正版): 金額(kakeibo_amount.py)と同様、
日付もLLM生成のdateを最終値として採用しない。原文に明示的な日付表現が
あればそれを実行年で補って採用し、なければ実行日をそのまま使う。
"""
import datetime
import re

from kakeibo_amount import _normalize_digits

# 月/日形式(例: "7/17")。年は原文に含まれない前提で実行年を補う。
# 直前直後に数字やスラッシュが続く場合は除外し、金額表記(3020円等)や
# より長い/複合的な数値表現の一部分を誤って月日として切り出さないようにする。
_DATE_MONTH_DAY_PATTERN = re.compile(
    r'(?<![0-9/])(?P<month>1[0-2]|0?[1-9])/(?P<day>3[01]|[12][0-9]|0?[1-9])(?![0-9/])'
)


def extract_date_from_text(text: str, today: datetime.date | None = None) -> str:
    """原文から月/日形式の日付表現を抽出し、実行年を補ってISO形式(YYYY-MM-DD)を返す。

    見つからない場合、または実在しない暦日(例: 2/30)の場合は実行日を返す。
    `today`はテスト用に実行日を固定するための任意引数(省略時は実際の今日)。
    """
    if today is None:
        today = datetime.date.today()
    normalized = _normalize_digits(text)
    match = _DATE_MONTH_DAY_PATTERN.search(normalized)
    if match:
        month = int(match.group("month"))
        day = int(match.group("day"))
        try:
            return datetime.date(today.year, month, day).isoformat()
        except ValueError:
            pass
    return today.isoformat()
