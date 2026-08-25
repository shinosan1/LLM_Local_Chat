import datetime
import unittest
from unittest.mock import patch

from kakeibo_confirmation import (
    build_kakeibo_candidate,
    can_submit_kakeibo_candidate,
    is_valid_calendar_date,
    validate_kakeibo_payload,
)


TODAY = datetime.date.today().isoformat()


class BuildKakeiboCandidateTests(unittest.TestCase):
    def test_llm_record_none_leaves_type_and_category_unselected(self):
        candidate = build_kakeibo_candidate(None, "1000円使った")
        self.assertEqual(candidate["status"], "ok")
        self.assertEqual(candidate["amount"], 1000)
        self.assertIsNone(candidate["type"])
        self.assertIsNone(candidate["category"])
        self.assertEqual(candidate["store"], "")
        self.assertEqual(candidate["memo"], "")
        self.assertEqual(candidate["date"], TODAY)

    def test_llm_record_missing_is_treated_like_none(self):
        # extract_kakeibo_json が None を返すケースを想定
        candidate = build_kakeibo_candidate(None, "セリアで雑貨を1870円買った")
        self.assertEqual(candidate["amount"], 1870)
        self.assertIsNone(candidate["type"])

    def test_valid_llm_fields_are_adopted(self):
        llm_record = {
            "type": "支出",
            "category": "日用品",
            "store": "セリア",
            "memo": "雑貨",
        }
        candidate = build_kakeibo_candidate(llm_record, "セリアで雑貨を1870円買った")
        self.assertEqual(candidate["type"], "支出")
        self.assertEqual(candidate["category"], "日用品")
        self.assertEqual(candidate["store"], "セリア")
        self.assertEqual(candidate["memo"], "雑貨")

    def test_invalid_type_becomes_unselected(self):
        llm_record = {"type": "その他", "category": "日用品"}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertIsNone(candidate["type"])
        # typeが未確定ならcategoryも選択不能
        self.assertIsNone(candidate["category"])

    def test_category_outside_list_falls_back_to_sono_ta(self):
        llm_record = {"type": "支出", "category": "存在しないカテゴリ"}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertEqual(candidate["category"], "その他支出")

        llm_record_income = {"type": "収入", "category": "存在しないカテゴリ"}
        candidate_income = build_kakeibo_candidate(llm_record_income, "1000円もらった")
        self.assertEqual(candidate_income["category"], "その他収入")

    def test_store_not_in_original_text_is_blanked(self):
        llm_record = {"store": "架空の店名"}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertEqual(candidate["store"], "")

    def test_memo_with_surrounding_whitespace_matches_after_strip(self):
        llm_record = {"memo": "  テストデータ  "}
        candidate = build_kakeibo_candidate(llm_record, "1000円 テストデータ")
        self.assertEqual(candidate["memo"], "テストデータ")

    def test_memo_non_string_is_blanked(self):
        llm_record = {"memo": 12345}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertEqual(candidate["memo"], "")

    def test_no_amount_status_leaves_amount_none(self):
        candidate = build_kakeibo_candidate({"type": "支出"}, "パンを買った")
        self.assertEqual(candidate["status"], "no_amount")
        self.assertIsNone(candidate["amount"])

    def test_invalid_amount_format_status(self):
        candidate = build_kakeibo_candidate(None, "1万2000円使った")
        self.assertEqual(candidate["status"], "invalid_amount_format")
        self.assertIsNone(candidate["amount"])

    def test_multiple_amounts_status(self):
        candidate = build_kakeibo_candidate(None, "500円と1200円使った")
        self.assertEqual(candidate["status"], "multiple_amounts")
        self.assertIsNone(candidate["amount"])

    def test_date_uses_today_when_text_has_no_date_even_if_llm_has_one(self):
        # 原文に日付表現がなければ、LLM出力のdateは無視して実行日を使う
        llm_record = {"date": "2000-01-01"}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertEqual(candidate["date"], TODAY)

    def test_date_uses_original_text_date_expression(self):
        # 原文中に明示的な日付表現(7/17等)があれば、実行年を補って採用する
        candidate = build_kakeibo_candidate(
            None, "ここら歯科　クリーニング 3020円 7/17")
        expected = f"{datetime.date.today().year}-07-17"
        self.assertEqual(candidate["date"], expected)

    def test_date_prefers_original_text_over_llm_value_when_both_present(self):
        llm_record = {"date": "2000-01-01", "type": "支出"}
        candidate = build_kakeibo_candidate(
            llm_record, "ここら歯科　クリーニング 3020円 7/17")
        expected = f"{datetime.date.today().year}-07-17"
        self.assertEqual(candidate["date"], expected)

    def test_japanese_month_day_reaches_confirmation_candidate(self):
        real_date = datetime.date
        fixed_today = real_date(2026, 8, 24)
        with patch("kakeibo_date.datetime.date") as mocked_date:
            mocked_date.today.return_value = fixed_today
            mocked_date.side_effect = lambda *args, **kwargs: real_date(
                *args, **kwargs)
            candidate = build_kakeibo_candidate(
                {"type": "支出", "category": "日用品", "store": "セリア"},
                "8月22日セリア1130円",
            )
        self.assertEqual(candidate["date"], "2026-08-22")
        self.assertEqual(candidate["amount"], 1130)

    def test_natural_date_and_amount_inputs_reach_confirmation_candidate(self):
        real_date = datetime.date
        fixed_today = real_date(2026, 8, 24)
        cases = {
            "業務スーパー8/20 1603円 食料品": ("2026-08-20", 1603),
            "8/20 業務スーパー 1603円 食料品": ("2026-08-20", 1603),
            "業務スーパー 1603円 8/20": ("2026-08-20", 1603),
            "業務スーパー2026/8/20 1603円 食料品": ("2026-08-20", 1603),
            "業務スーパー2026-08-20 1603円 食料品": ("2026-08-20", 1603),
            "業務スーパー8月20日 1603円 食料品": ("2026-08-20", 1603),
            "セリア8月22日1130円 日用品": ("2026-08-22", 1130),
            "2026/8/20 セリア 1130円 日用品": ("2026-08-20", 1130),
            "2026年8月20日 セリア 1130円 日用品": ("2026-08-20", 1130),
        }
        with patch("kakeibo_date.datetime.date") as mocked_date:
            mocked_date.today.return_value = fixed_today
            mocked_date.side_effect = lambda *args, **kwargs: real_date(
                *args, **kwargs)
            for text, (expected_date, expected_amount) in cases.items():
                with self.subTest(text=text):
                    candidate = build_kakeibo_candidate(None, text)
                    self.assertEqual(candidate["status"], "ok")
                    self.assertEqual(candidate["date"], expected_date)
                    self.assertEqual(candidate["amount"], expected_amount)

    def test_date_prefixed_invalid_and_multiple_amounts_stay_rejected(self):
        cases = {
            "1 603円": "invalid_amount_format",
            "20 1603円": "invalid_amount_format",
            "8/20 1 603円": "invalid_amount_format",
            "2026-08-20 1, 603円": "invalid_amount_format",
            "8/20 1603円と1 500円": "invalid_amount_format",
            "8/20 1603円と500円": "multiple_amounts",
        }
        for text, expected_status in cases.items():
            with self.subTest(text=text):
                candidate = build_kakeibo_candidate(None, text)
                self.assertEqual(candidate["status"], expected_status)
                self.assertIsNone(candidate["amount"])

    def test_type_expense_with_missing_category_key_gets_sono_ta(self):
        # category キー自体が存在しない場合も、typeがtruthyなら「その他支出」を設定する
        llm_record = {"type": "支出"}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertEqual(candidate["type"], "支出")
        self.assertEqual(candidate["category"], "その他支出")

    def test_type_expense_with_none_category_gets_sono_ta(self):
        llm_record = {"type": "支出", "category": None}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertEqual(candidate["category"], "その他支出")

    def test_type_income_with_missing_category_key_gets_sono_ta(self):
        llm_record = {"type": "収入"}
        candidate = build_kakeibo_candidate(llm_record, "1000円もらった")
        self.assertEqual(candidate["category"], "その他収入")

    def test_amount_ignores_llm_value_entirely(self):
        # 原文は1000円だがLLMがamount=10000を出しても、原文由来の値だけを採用する
        llm_record = {"amount": 10000, "type": "支出"}
        candidate = build_kakeibo_candidate(llm_record, "1000円使った")
        self.assertEqual(candidate["amount"], 1000)


class CanSubmitKakeiboCandidateTests(unittest.TestCase):
    def _valid_record(self):
        return {
            "date": TODAY,
            "amount": 1000,
            "type": "支出",
            "category": "日用品",
            "store": "",
            "memo": "",
        }

    def test_all_conditions_met(self):
        self.assertTrue(can_submit_kakeibo_candidate(self._valid_record(), True))

    def test_expense_type_without_llm_category_can_still_submit(self):
        # LLMがcategoryを出さなかった場合でも、build_kakeibo_candidateが
        # 「その他支出」へ自動補完するため、カテゴリ選択済みとして登録可能になる。
        candidate = build_kakeibo_candidate({"type": "支出"}, "1000円使った")
        self.assertTrue(can_submit_kakeibo_candidate(candidate, True))

    def test_not_confirmed_disables_submit(self):
        self.assertFalse(can_submit_kakeibo_candidate(self._valid_record(), False))

    def test_unselected_type_disables_submit(self):
        record = self._valid_record()
        record["type"] = None
        self.assertFalse(can_submit_kakeibo_candidate(record, True))

    def test_bool_amount_disables_submit(self):
        record = self._valid_record()
        record["amount"] = True
        self.assertFalse(can_submit_kakeibo_candidate(record, True))

    def test_zero_amount_disables_submit(self):
        record = self._valid_record()
        record["amount"] = 0
        self.assertFalse(can_submit_kakeibo_candidate(record, True))

    def test_invalid_date_disables_submit(self):
        record = self._valid_record()
        record["date"] = "2024-02-30"
        self.assertFalse(can_submit_kakeibo_candidate(record, True))

    def test_category_outside_type_list_disables_submit(self):
        record = self._valid_record()
        record["category"] = "給与"  # 収入カテゴリを支出typeに割り当てる
        self.assertFalse(can_submit_kakeibo_candidate(record, True))


class ValidateKakeiboPayloadTests(unittest.TestCase):
    def _valid_payload(self):
        return {
            "date": TODAY,
            "amount": 1000,
            "type": "支出",
            "category": "日用品",
            "store": "セリア",
            "memo": "",
        }

    def test_valid_payload_passes_through(self):
        payload = validate_kakeibo_payload(self._valid_payload())
        self.assertIsNotNone(payload)
        self.assertEqual(payload["amount"], 1000)

    def test_unknown_keys_are_removed(self):
        record = self._valid_payload()
        record["unexpected_key"] = "danger"
        payload = validate_kakeibo_payload(record)
        self.assertNotIn("unexpected_key", payload)

    def test_non_dict_is_rejected(self):
        self.assertIsNone(validate_kakeibo_payload("not a dict"))

    def test_invalid_iso_date_is_rejected(self):
        record = self._valid_payload()
        record["date"] = "2024-02-30"  # 存在しない暦日
        self.assertIsNone(validate_kakeibo_payload(record))

    def test_non_iso_date_format_is_rejected(self):
        record = self._valid_payload()
        record["date"] = "2024/01/01"
        self.assertIsNone(validate_kakeibo_payload(record))

    def test_bool_amount_is_rejected(self):
        record = self._valid_payload()
        record["amount"] = True
        self.assertIsNone(validate_kakeibo_payload(record))

    def test_non_positive_amount_is_rejected(self):
        record = self._valid_payload()
        record["amount"] = 0
        self.assertIsNone(validate_kakeibo_payload(record))

    def test_invalid_type_is_rejected_not_coerced(self):
        record = self._valid_payload()
        record["type"] = "不正な種別"
        self.assertIsNone(validate_kakeibo_payload(record))

    def test_category_mismatched_with_type_is_rejected(self):
        record = self._valid_payload()
        record["category"] = "給与"  # 収入カテゴリを支出typeに割り当てる
        self.assertIsNone(validate_kakeibo_payload(record))

    def test_non_string_store_is_blanked_not_rejected(self):
        record = self._valid_payload()
        record["store"] = 12345
        payload = validate_kakeibo_payload(record)
        self.assertEqual(payload["store"], "")

    def test_non_string_memo_is_blanked_not_rejected(self):
        record = self._valid_payload()
        record["memo"] = 12345
        payload = validate_kakeibo_payload(record)
        self.assertEqual(payload["memo"], "")

    def test_missing_required_field_is_rejected(self):
        record = self._valid_payload()
        del record["amount"]
        self.assertIsNone(validate_kakeibo_payload(record))

    def test_ui_bypassed_invalid_value_is_still_rejected(self):
        # UIの登録ボタン判定を経由しない、直接構築された不正ペイロードでも拒否されること
        bypassed_record = {
            "date": TODAY,
            "amount": -500,
            "type": "その他",
            "category": "存在しないカテゴリ",
            "store": "店",
            "memo": "",
        }
        self.assertIsNone(validate_kakeibo_payload(bypassed_record))


class IsValidCalendarDateTests(unittest.TestCase):
    def test_valid_date(self):
        self.assertTrue(is_valid_calendar_date("2026-07-20"))

    def test_nonexistent_date_is_invalid(self):
        self.assertFalse(is_valid_calendar_date("2024-02-30"))

    def test_non_string_is_invalid(self):
        self.assertFalse(is_valid_calendar_date(None))

    def test_wrong_format_is_invalid(self):
        self.assertFalse(is_valid_calendar_date("2024/01/01"))

    def test_digits_only_without_hyphens_is_invalid(self):
        self.assertFalse(is_valid_calendar_date("20260720"))

    def test_iso_week_format_is_invalid(self):
        self.assertFalse(is_valid_calendar_date("2026-W30-1"))


if __name__ == "__main__":
    unittest.main()
