"""1入力から複数取引候補を作る分割・検証ロジックのテスト。

安全境界(amount/dateをLLM値から確定しない、1取引=1確認=1POST、
上限超過は全体拒否)が保たれていることを確認する。
"""
import datetime
import unittest
from unittest.mock import patch

from kakeibo_split import (
    MAX_KAKEIBO_TRANSACTIONS_PER_INPUT,
    build_kakeibo_candidates,
    normalize_transactions,
)

TODAY = datetime.date.today().isoformat()

THREE_TEXT = "スーパーで2000円、コンビニで500円、薬局で1200円"
THREE_TX = [
    {"source_text": "スーパーで2000円", "type": "支出", "category": "食費",
     "store": "スーパー"},
    {"source_text": "コンビニで500円", "type": "支出", "category": "食費"},
    {"source_text": "薬局で1200円", "type": "支出", "category": "医療費"},
]
ORDERED = ["スーパーで2000円", "コンビニで500円", "薬局で1200円"]

REAL_FIVE_TEXT = (
    "業務スーパー8月20日、1,603円食料品セリア、8月20日、1,430円 日用品, "
    "ダイソー、8月20日、1,100円日用品, コーナン、8月21日、525円日用品, "
    "松源8月19日、3,963円 食料品"
)
REAL_FIVE_LLM_TX = [
    {"source_text": "業務スーパー8月20日、1,603円食料品",
     "store": "業務スーパー", "category": "食費", "type": "支出",
     "memo": None},
    {"source_text": "セリア、8月20日、1,430円日用品",
     "store": "セリア", "category": "日用品", "type": "支出",
     "memo": None},
    {"source_text": "ダイソー、8月20日、1,100円日用品",
     "store": "ダイソー", "category": "日用品", "type": "支出",
     "memo": None},
    {"source_text": "コーナン、8月21日、525円日用品",
     "store": "コーナン", "category": "日用品", "type": "支出",
     "memo": None},
    {"source_text": "松源8月19日、3,963円食料品",
     "store": "松源", "category": "食費", "type": "支出",
     "memo": None},
]
REAL_FIVE_SOURCE_TEXTS = [
    "業務スーパー8月20日、1,603円食料品",
    "セリア、8月20日、1,430円 日用品",
    "ダイソー、8月20日、1,100円日用品",
    "コーナン、8月21日、525円日用品",
    "松源8月19日、3,963円 食料品",
]

RIGHT_BOUNDARY_FIVE_TEXT = (
    "無印2170円8月18日日用品、ダイソー660円8月10日日用品、"
    "業務スーパー8月10日食料品1361円、キャンドゥー7月26日330円日用品"
    "キャンドゥー8月18日880円日用品"
)
RIGHT_BOUNDARY_FIVE_TX = [
    {"source_text": "無印2170円8月18日日用品", "store": "無印",
     "category": "日用品", "type": "支出"},
    {"source_text": "ダイソー660円8月10日日用品", "store": "ダイソー",
     "category": "日用品", "type": "支出"},
    {"source_text": "業務スーパー8月10日食料品1361円", "store": "業務スーパー",
     "category": "食費", "type": "支出"},
    {"source_text": "キャンドゥー7月26日330円日用品", "store": "キャンドゥー",
     "category": "日用品", "type": "支出"},
    {"source_text": "キャンドゥー8月18日880円日用品", "store": "キャンドゥー",
     "category": "日用品", "type": "支出"},
]


def _texts(count):
    """N件の取引を含む入力文と、対応する取引候補列を作る。"""
    fragments = [f"店{i}で{100 * (i + 1)}円" for i in range(count)]
    text = "、".join(fragments)
    tx = [{"source_text": f, "type": "支出", "category": "食費"} for f in fragments]
    return text, tx


class NormalizeTransactionsTests(unittest.TestCase):
    def test_three_fragments_keep_source_order(self):
        result = normalize_transactions(THREE_TX, THREE_TEXT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([f for f, _ in result["items"]], ORDERED)

    def test_reversed_order_is_normalized_to_source_order(self):
        result = normalize_transactions(list(reversed(THREE_TX)), THREE_TEXT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual([f for f, _ in result["items"]], ORDERED)

    def test_fabricated_source_text_is_rejected(self):
        transactions = [
            {"source_text": "スーパーで2000円"},
            {"source_text": "薬局で1200円"},
        ]
        result = normalize_transactions(
            transactions, "スーパーで2000円、コンビニで500円")
        self.assertEqual(result["status"], "invalid_split")
        self.assertEqual(result["items"], [])

    def test_duplicated_span_is_rejected(self):
        transactions = [
            {"source_text": "スーパーで2000円"},
            {"source_text": "スーパーで2000円"},
        ]
        self.assertEqual(
            normalize_transactions(transactions, THREE_TEXT)["status"],
            "invalid_split")

    def test_overlapping_span_is_rejected(self):
        transactions = [
            {"source_text": "スーパーで2000円、コンビニで500円"},
            {"source_text": "コンビニで500円"},
        ]
        self.assertEqual(
            normalize_transactions(transactions, THREE_TEXT)["status"],
            "invalid_split")

    def test_empty_source_text_is_rejected(self):
        self.assertEqual(
            normalize_transactions([{"source_text": "  "}], THREE_TEXT)["status"],
            "invalid_split")

    def test_unknown_key_is_rejected(self):
        transactions = [{"source_text": "スーパーで2000円", "amount": 999999}]
        self.assertEqual(
            normalize_transactions(transactions, THREE_TEXT)["status"],
            "invalid_split")

    def test_non_list_input_is_rejected(self):
        self.assertEqual(
            normalize_transactions({"source_text": "x"}, THREE_TEXT)["status"],
            "invalid_split")
        self.assertEqual(
            normalize_transactions([], THREE_TEXT)["status"], "invalid_split")

    def test_observed_five_transactions_recover_original_spaces(self):
        result = normalize_transactions(REAL_FIVE_LLM_TX, REAL_FIVE_TEXT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [fragment for fragment, _record in result["items"]],
            REAL_FIVE_SOURCE_TEXTS,
        )
        self.assertEqual(
            result["spans"], [(0, 21), (21, 41), (43, 63), (65, 83), (85, 103)])

    def test_exact_match_path_does_not_call_space_recovery(self):
        with patch(
            "kakeibo_split._recover_horizontal_space_fragments",
            side_effect=AssertionError("space fallback must not run"),
        ):
            result = normalize_transactions(THREE_TX, THREE_TEXT)
        self.assertEqual(result["status"], "ok")

    def test_horizontal_space_variants_recover_exact_original_slice(self):
        cases = [
            ("店A 100円", "店A100円"),
            ("店A　100円", "店A100円"),
            ("店A100円", "店A 100円"),
            ("店A100円", "店A　100円"),
            ("店A 100円", "店A　100円"),
            ("店A　100円", "店A 100円"),
        ]
        for user_text, llm_text in cases:
            with self.subTest(user_text=user_text, llm_text=llm_text):
                result = normalize_transactions(
                    [{"source_text": llm_text}], user_text)
                self.assertEqual(result["status"], "ok")
                self.assertEqual(result["items"][0][0], user_text)
                self.assertEqual(result["spans"], [(0, len(user_text))])

    def test_non_space_changes_are_not_recovered(self):
        user_text = "セリア、8月20日、1,430円 日用品"
        llm_texts = [
            "セリア 8月20日、1,430円 日用品",
            "セリア、8月20日、1430円 日用品",
            "セリア、8月20日、1,130円 日用品",
            "セリア、8月21日、1,430円 日用品",
            "ダイソー、8月20日、1,430円 日用品",
            "セリア、8月20日、1,430円 日用品を購入",
        ]
        for llm_text in llm_texts:
            with self.subTest(llm_text=llm_text):
                result = normalize_transactions(
                    [{"source_text": llm_text}], user_text)
                self.assertEqual(result["status"], "invalid_split")

    def test_newline_changes_are_not_recovered(self):
        cases = [
            ("店A\n100円", "店A100円"),
            ("店A100円", "店A\n100円"),
        ]
        for user_text, llm_text in cases:
            with self.subTest(user_text=user_text, llm_text=llm_text):
                result = normalize_transactions(
                    [{"source_text": llm_text}], user_text)
                self.assertEqual(result["status"], "invalid_split")

    def test_ambiguous_space_normalized_match_is_rejected(self):
        user_text = "店A 100円、店A　100円"
        result = normalize_transactions(
            [{"source_text": "店A100円"}], user_text)
        self.assertEqual(result["status"], "invalid_split")

    def test_space_recovery_rejects_overlapping_spans(self):
        user_text = "店A 100円"
        transactions = [
            {"source_text": "店A100円"},
            {"source_text": "店A100円"},
        ]
        result = normalize_transactions(transactions, user_text)
        self.assertEqual(result["status"], "invalid_split")


class TransactionLimitTests(unittest.TestCase):
    def test_limit_is_ten(self):
        self.assertEqual(MAX_KAKEIBO_TRANSACTIONS_PER_INPUT, 10)

    def test_exactly_ten_is_accepted(self):
        text, transactions = _texts(MAX_KAKEIBO_TRANSACTIONS_PER_INPUT)
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            len(result["candidates"]), MAX_KAKEIBO_TRANSACTIONS_PER_INPUT)

    def test_eleven_rejects_whole_input(self):
        text, transactions = _texts(MAX_KAKEIBO_TRANSACTIONS_PER_INPUT + 1)
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "too_many")
        self.assertEqual(result["candidates"], [])


class BuildKakeiboCandidatesTests(unittest.TestCase):
    def test_three_transactions_extract_amounts_from_fragments(self):
        result = build_kakeibo_candidates(THREE_TX, THREE_TEXT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [c["amount"] for c in result["candidates"]], [2000, 500, 1200])
        self.assertEqual(
            [c["source_text"] for c in result["candidates"]], ORDERED)

    def test_single_transaction_keeps_legacy_behaviour(self):
        text = "コンビニで500円使った"
        result = build_kakeibo_candidates([{"source_text": text}], text)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["amount"], 500)
        self.assertEqual(result["candidates"][0]["date"], TODAY)

    def test_real_date_adjacent_amount_input_builds_candidate(self):
        real_date = datetime.date
        fixed_today = real_date(2026, 8, 24)
        text = "業務スーパー8/20 1603円 食料品"
        transactions = [{
            "source_text": text,
            "type": "支出",
            "category": "食費",
            "store": "業務スーパー",
        }]
        with patch("kakeibo_date.datetime.date") as mocked_date:
            mocked_date.today.return_value = fixed_today
            mocked_date.side_effect = lambda *args, **kwargs: real_date(
                *args, **kwargs)
            result = build_kakeibo_candidates(transactions, text)

        self.assertEqual(result["status"], "ok")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["date"], "2026-08-20")
        self.assertEqual(candidate["amount"], 1603)

    def test_observed_five_transactions_build_from_recovered_source_text(self):
        result = build_kakeibo_candidates(
            REAL_FIVE_LLM_TX, REAL_FIVE_TEXT, today=datetime.date(2026, 8, 25))
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["candidates"]), 5)
        self.assertEqual(
            [candidate["source_text"] for candidate in result["candidates"]],
            REAL_FIVE_SOURCE_TEXTS,
        )
        self.assertEqual(
            [candidate["date"] for candidate in result["candidates"]],
            ["2026-08-20", "2026-08-20", "2026-08-20",
             "2026-08-21", "2026-08-19"],
        )
        self.assertEqual(
            [candidate["amount"] for candidate in result["candidates"]],
            [1603, 1430, 1100, 525, 3963],
        )

    def test_real_amount_before_date_input_builds_five_candidates(self):
        normalized = normalize_transactions(
            RIGHT_BOUNDARY_FIVE_TX, RIGHT_BOUNDARY_FIVE_TEXT)
        self.assertEqual(normalized["status"], "ok")
        self.assertEqual(
            normalized["spans"],
            [(0, 15), (16, 32), (33, 52), (53, 71), (71, 89)],
        )

        result = build_kakeibo_candidates(
            RIGHT_BOUNDARY_FIVE_TX,
            RIGHT_BOUNDARY_FIVE_TEXT,
            today=datetime.date(2026, 8, 25),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["candidates"]), 5)
        self.assertEqual(
            [candidate["date"] for candidate in result["candidates"]],
            ["2026-08-18", "2026-08-10", "2026-08-10",
             "2026-07-26", "2026-08-18"],
        )
        self.assertEqual(
            [candidate["amount"] for candidate in result["candidates"]],
            [2170, 660, 1361, 330, 880],
        )

    def test_date_prefixed_invalid_and_multiple_amounts_reject_input(self):
        cases = {
            "8/20 1 603円": "invalid_amount_format",
            "2026-08-20 1, 603円": "invalid_amount_format",
            "8/20 1603円と1 500円": "invalid_amount_format",
            "8/20 1603円と500円": "multiple_amounts",
        }
        for text, expected_status in cases.items():
            with self.subTest(text=text):
                result = build_kakeibo_candidates(
                    [{"source_text": text}], text)
                self.assertEqual(result["status"], expected_status)
                self.assertEqual(result["candidates"], [])

    def test_llm_amount_is_never_used(self):
        transactions = [
            {"source_text": "スーパーで2000円", "type": "支出", "category": "食費"},
        ]
        result = build_kakeibo_candidates(transactions, "スーパーで2000円")
        self.assertEqual(result["candidates"][0]["amount"], 2000)

    def test_llm_supplied_date_key_is_rejected(self):
        transactions = [
            {"source_text": "2026/08/16 スーパーで2000円", "date": "2030-01-01"},
        ]
        result = build_kakeibo_candidates(
            transactions, "2026/08/16 スーパーで2000円")
        self.assertEqual(result["status"], "invalid_split")

    def test_date_extracted_from_fragment(self):
        text = "8/16 スーパーで2000円、9/20 コンビニで500円"
        transactions = [
            {"source_text": "8/16 スーパーで2000円"},
            {"source_text": "9/20 コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        year = datetime.date.today().year
        self.assertEqual(result["candidates"][0]["date"], f"{year}-08-16")
        self.assertEqual(result["candidates"][1]["date"], f"{year}-09-20")

    def test_leading_date_applies_to_all_fragments(self):
        text = "2026/08/16 スーパーで2000円、コンビニで500円"
        transactions = [
            {"source_text": "2026/08/16 スーパーで2000円"},
            {"source_text": "コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [c["date"] for c in result["candidates"]],
            ["2026-08-16", "2026-08-16"])

    def test_fragment_with_two_amounts_rejects_whole_input(self):
        text = "スーパーで2000円と500円"
        result = build_kakeibo_candidates([{"source_text": text}], text)
        self.assertEqual(result["status"], "multiple_amounts")
        self.assertEqual(result["candidates"], [])

    def test_fragment_without_amount_rejects_whole_input(self):
        text = "スーパーで2000円、コンビニに行った"
        transactions = [
            {"source_text": "スーパーで2000円"},
            {"source_text": "コンビニに行った"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "no_amount")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["reason_index"], 1)

    def test_store_must_appear_in_its_own_fragment(self):
        transactions = [
            {"source_text": "スーパーで2000円", "store": "薬局"},
            {"source_text": "薬局で1200円", "store": "薬局"},
        ]
        result = build_kakeibo_candidates(
            transactions, "スーパーで2000円、薬局で1200円")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["candidates"][0]["store"], "")
        self.assertEqual(result["candidates"][1]["store"], "薬局")


if __name__ == "__main__":
    unittest.main()


class LeftoverAmountTests(unittest.TestCase):
    """LLMが取引を取りこぼした場合に部分登録させない。"""

    def test_missing_third_transaction_rejects_whole_input(self):
        transactions = [
            {"source_text": "スーパーで2000円"},
            {"source_text": "コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, THREE_TEXT)
        self.assertEqual(result["status"], "uncovered_amount")
        self.assertEqual(result["candidates"], [])

    def test_missing_first_transaction_rejects_whole_input(self):
        transactions = [
            {"source_text": "コンビニで500円"},
            {"source_text": "薬局で1200円"},
        ]
        result = build_kakeibo_candidates(transactions, THREE_TEXT)
        self.assertEqual(result["status"], "uncovered_amount")

    def test_eleven_in_text_but_llm_returns_ten_rejects_whole_input(self):
        text, transactions = _texts(MAX_KAKEIBO_TRANSACTIONS_PER_INPUT + 1)
        result = build_kakeibo_candidates(
            transactions[:MAX_KAKEIBO_TRANSACTIONS_PER_INPUT], text)
        self.assertEqual(result["status"], "uncovered_amount")
        self.assertEqual(result["candidates"], [])

    def test_all_amounts_covered_is_accepted(self):
        result = build_kakeibo_candidates(THREE_TX, THREE_TEXT)
        self.assertEqual(result["status"], "ok")


class AmbiguousSplitTests(unittest.TestCase):
    """同一文脈内の複数金額をLLMが人工的に切り分けた場合は拒否する。"""

    def test_amount_only_fragment_is_rejected(self):
        text = "スーパーで2000円と500円"
        transactions = [
            {"source_text": "スーパーで2000円"},
            {"source_text": "500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ambiguous_split")
        self.assertEqual(result["candidates"], [])

    def test_whole_ambiguous_text_as_one_fragment_is_rejected(self):
        text = "スーパーで2000円と500円"
        result = build_kakeibo_candidates([{"source_text": text}], text)
        self.assertEqual(result["status"], "multiple_amounts")
        self.assertEqual(result["candidates"], [])

    def test_fragments_with_store_context_are_accepted(self):
        text = "スーパーで2000円、コンビニで500円"
        transactions = [
            {"source_text": "スーパーで2000円"},
            {"source_text": "コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [c["amount"] for c in result["candidates"]], [2000, 500])


class AmbiguousDateTests(unittest.TestCase):
    """日付なし候補へ入力全体の日付を適用してよいのは一意なときだけ。"""

    def test_single_date_applies_to_all(self):
        text = "8/16 スーパーで2000円、コンビニで500円"
        transactions = [
            {"source_text": "8/16 スーパーで2000円"},
            {"source_text": "コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        year = datetime.date.today().year
        self.assertEqual(
            [c["date"] for c in result["candidates"]],
            [f"{year}-08-16", f"{year}-08-16"])

    def test_each_fragment_has_its_own_date(self):
        text = "8/16 スーパーで2000円、8/17 コンビニで500円"
        transactions = [
            {"source_text": "8/16 スーパーで2000円"},
            {"source_text": "8/17 コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        year = datetime.date.today().year
        self.assertEqual(
            [c["date"] for c in result["candidates"]],
            [f"{year}-08-16", f"{year}-08-17"])

    def test_multiple_dates_with_undated_fragment_rejects_whole_input(self):
        text = "8/16 スーパーで2000円、8/17 コンビニで500円、薬局で1200円"
        transactions = [
            {"source_text": "8/16 スーパーで2000円"},
            {"source_text": "8/17 コンビニで500円"},
            {"source_text": "薬局で1200円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ambiguous_date")
        self.assertEqual(result["candidates"], [])

    def test_no_date_anywhere_uses_today(self):
        result = build_kakeibo_candidates(THREE_TX, THREE_TEXT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(
            [c["date"] for c in result["candidates"]], [TODAY] * 3)

    def test_single_japanese_date_applies_to_all(self):
        text = "8月16日スーパーで2000円、コンビニで500円"
        transactions = [
            {"source_text": "8月16日スーパーで2000円"},
            {"source_text": "コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        year = datetime.date.today().year
        self.assertEqual(
            [c["date"] for c in result["candidates"]],
            [f"{year}-08-16", f"{year}-08-16"],
        )

    def test_each_fragment_has_its_own_japanese_date(self):
        text = "8月16日スーパーで2000円、8月17日コンビニで500円"
        transactions = [
            {"source_text": "8月16日スーパーで2000円"},
            {"source_text": "8月17日コンビニで500円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        year = datetime.date.today().year
        self.assertEqual(
            [c["date"] for c in result["candidates"]],
            [f"{year}-08-16", f"{year}-08-17"],
        )

    def test_multiple_japanese_dates_with_undated_fragment_are_ambiguous(self):
        text = (
            "8月16日スーパーで2000円、8月17日コンビニで500円、"
            "薬局で1200円"
        )
        transactions = [
            {"source_text": "8月16日スーパーで2000円"},
            {"source_text": "8月17日コンビニで500円"},
            {"source_text": "薬局で1200円"},
        ]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ambiguous_date")
        self.assertEqual(result["candidates"], [])


class SingleTransactionDisplayTests(unittest.TestCase):
    """1件入力では確認画面へ入力全文を出す(後方互換)。"""

    def test_partial_source_text_is_replaced_with_full_input(self):
        text = "今日スーパーで2000円使った"
        transactions = [{"source_text": "スーパーで2000円"}]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["amount"], 2000)
        self.assertEqual(candidate["source_text"], text)

    def test_amount_and_date_still_come_from_full_input(self):
        text = "8/16 今日スーパーで2000円使った"
        transactions = [{"source_text": "スーパーで2000円"}]
        result = build_kakeibo_candidates(transactions, text)
        self.assertEqual(result["status"], "ok")
        candidate = result["candidates"][0]
        year = datetime.date.today().year
        self.assertEqual(candidate["date"], f"{year}-08-16")
        self.assertEqual(candidate["amount"], 2000)
        self.assertEqual(candidate["source_text"], text)

    def test_multiple_transactions_keep_their_own_fragment(self):
        result = build_kakeibo_candidates(THREE_TX, THREE_TEXT)
        self.assertEqual(
            [c["source_text"] for c in result["candidates"]], ORDERED)
