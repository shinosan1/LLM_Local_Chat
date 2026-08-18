"""ユーザー原文からの日付抽出。

Section C確定仕様(2026-07-20訂正版): 金額(kakeibo_amount.py)と同様、
日付もLLM生成のdateを最終値として採用しない。原文に明示的な日付表現が
あればそれを採用し、なければ実行日をそのまま使う。年が原文に無い月/日
形式の場合だけ実行年を補う。
"""
import datetime
import re

from kakeibo_amount import _normalize_digits

# 年付き日付形式(例: "2026/08/16", "2026-08-16")。原文に年があるので実行年は補わない。
# 区切りは / と - の両方を許容するが、後方参照で同一文字に限定し、
# 混在表記("2026/08-16"等)は日付表現として扱わない。
# 直前直後に数字や区切り文字が続く場合は、より長い数値表現(電話番号等)の
# 一部分を誤って日付として切り出さないよう除外する。
_DATE_FULL_PATTERN = re.compile(
    r'(?<![0-9/\-])'
    r'(?P<year>[0-9]{4})(?P<sep>[/\-])'
    r'(?P<month>1[0-2]|0?[1-9])(?P=sep)'
    r'(?P<day>3[01]|[12][0-9]|0?[1-9])'
    r'(?![0-9/\-])'
)

# 月/日形式(例: "7/17")。年は原文に含まれない前提で実行年を補う。
# 直前直後に数字やスラッシュが続く場合は除外し、金額表記(3020円等)や
# より長い/複合的な数値表現の一部分を誤って月日として切り出さないようにする。
_DATE_MONTH_DAY_PATTERN = re.compile(
    r'(?<![0-9/])(?P<month>1[0-2]|0?[1-9])/(?P<day>3[01]|[12][0-9]|0?[1-9])(?![0-9/])'
)


def find_explicit_date(text: str, today: datetime.date | None = None) -> str | None:
    """原文に明示的な日付表現がある場合だけISO形式(YYYY-MM-DD)で返す。

    見つからない場合、または実在しない暦日(例: 2/30)しか無い場合は None を返す。
    実行日へのフォールバックは行わない。複数取引を扱う際に「この断片自身が日付を
    持っているか」を判定する必要があるため、extract_date_from_text から分離した。
    対応形式は extract_date_from_text と同一で、新しい形式は追加していない。
    """
    if today is None:
        today = datetime.date.today()
    normalized = _normalize_digits(text)

    full_match = _DATE_FULL_PATTERN.search(normalized)
    if full_match:
        try:
            return datetime.date(
                int(full_match.group("year")),
                int(full_match.group("month")),
                int(full_match.group("day")),
            ).isoformat()
        except ValueError:
            pass

    match = _DATE_MONTH_DAY_PATTERN.search(normalized)
    if match:
        month = int(match.group("month"))
        day = int(match.group("day"))
        try:
            return datetime.date(today.year, month, day).isoformat()
        except ValueError:
            pass
    return None


def extract_date_from_text(text: str, today: datetime.date | None = None) -> str:
    """原文の日付表現をISO形式(YYYY-MM-DD)で返す。

    年付き形式("2026/08/16"・"2026-08-16")を優先して採用し、無ければ月/日形式
    ("7/17")を実行年で補って採用する。どちらも見つからない場合、または実在しない
    暦日(例: 2/30)の場合は実行日を返す。
    `today`はテスト用に実行日を固定するための任意引数(省略時は実際の今日)。
    """
    if today is None:
        today = datetime.date.today()
    found = find_explicit_date(text, today)
    return found if found is not None else today.isoformat()


def find_explicit_dates(text: str, today: datetime.date | None = None) -> list[str]:
    """原文中の明示的な日付表現を、出現位置順にISO形式(YYYY-MM-DD)で返す。

    複数取引の入力で「入力全体の日付を、日付を持たない取引へ適用してよいか」を
    判断するために使う。日付が一意に定まらない入力を安全側で拒否できるよう、
    find_explicit_date のように1件だけ返すのではなく全件を返す。

    対応形式は find_explicit_date と同一で、新しい形式は追加していない。
    実在しない暦日は候補に含めない。年付き形式が一致した範囲は月/日形式の
    lookbehind により二重に数えられない。
    """
    if today is None:
        today = datetime.date.today()
    normalized = _normalize_digits(text)

    found: list[tuple[int, str]] = []
    for match in _DATE_FULL_PATTERN.finditer(normalized):
        try:
            iso = datetime.date(
                int(match.group("year")),
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            continue
        found.append((match.start(), iso))

    for match in _DATE_MONTH_DAY_PATTERN.finditer(normalized):
        try:
            iso = datetime.date(
                today.year,
                int(match.group("month")),
                int(match.group("day")),
            ).isoformat()
        except ValueError:
            continue
        found.append((match.start(), iso))

    found.sort()
    return [iso for _, iso in found]
