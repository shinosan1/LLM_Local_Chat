"""1入力から複数取引候補を作る分割・検証ロジックのテスト。

安全境界(amount/dateをLLM値から確定しない、1取引=1確認=1POST、
上限超過は全体拒否)が保たれていることを確認する。
"""
import datetime
import unittest

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
