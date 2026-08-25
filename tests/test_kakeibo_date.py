import datetime
import unittest

from kakeibo_date import (
    extract_date_from_text,
    find_explicit_date,
    find_explicit_dates,
)

_FIXED_TODAY = datetime.date(2026, 7, 20)


class ExtractDateFromTextTests(unittest.TestCase):
    def test_extracts_month_day_and_fills_in_current_year(self):
        result = extract_date_from_text("3020円 7/17", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-17")

    def test_no_date_expression_falls_back_to_today(self):
        result = extract_date_from_text("1000円使った", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_invalid_calendar_date_falls_back_to_today(self):
        # 2月30日は実在しない暦日
        result = extract_date_from_text("2/30に何か買った", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_fullwidth_digits_are_normalized(self):
        result = extract_date_from_text("７/１７にクリーニング", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-17")

    def test_out_of_range_month_is_not_matched(self):
        result = extract_date_from_text("13/1に買った", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_amount_number_is_not_mistaken_for_date(self):
        result = extract_date_from_text("3020円使った", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_real_today_is_used_when_argument_omitted(self):
        result = extract_date_from_text("1000円使った")
        self.assertEqual(result, datetime.date.today().isoformat())


class ExtractFullDateFromTextTests(unittest.TestCase):
    """年付き日付("YYYY/MM/DD"・"YYYY-MM-DD")は実行日で上書きしない。"""

    def test_slash_full_date_is_used_instead_of_today(self):
        # 実運用で報告された入力。実行日(2026-08-17想定)へ置き換わってはならない。
        result = extract_date_from_text(
            "2026/08/16 テスト店にて日用品２０００円分買う",
            today=datetime.date(2026, 8, 17),
        )
        self.assertEqual(result, "2026-08-16")

    def test_hyphen_full_date_is_used_instead_of_today(self):
        result = extract_date_from_text(
            "2026-08-16 テスト店にて日用品2000円分買う",
            today=datetime.date(2026, 8, 17),
        )
        self.assertEqual(result, "2026-08-16")

    def test_slash_and_hyphen_forms_agree(self):
        today = datetime.date(2026, 8, 17)
        self.assertEqual(
            extract_date_from_text("2026/08/16 に2000円", today=today),
            extract_date_from_text("2026-08-16 に2000円", today=today),
        )

    def test_year_in_text_is_not_replaced_by_run_year(self):
        # 実行年と異なる年でも原文の年をそのまま使う(実行年で補わない)。
        result = extract_date_from_text("2025/12/31 に1000円", today=_FIXED_TODAY)
        self.assertEqual(result, "2025-12-31")

    def test_single_digit_month_and_day_are_accepted(self):
        result = extract_date_from_text("2026/8/1 に1000円", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-08-01")

    def test_fullwidth_full_date_is_normalized(self):
        result = extract_date_from_text("２０２６/０８/１６ に2000円", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-08-16")

    def test_invalid_calendar_full_date_falls_back_to_today(self):
        # 2月30日は実在しないため、年付きでも実行日へフォールバックする。
        result = extract_date_from_text("2026/02/30 に1000円", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_mixed_separators_are_not_treated_as_date(self):
        result = extract_date_from_text("2026/08-16 に1000円", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_out_of_range_month_in_full_date_is_not_matched(self):
        result = extract_date_from_text("2026/13/01 に1000円", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_phone_number_is_not_mistaken_for_date(self):
        result = extract_date_from_text("03-1234-5678 へ電話して1000円", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-20")

    def test_month_day_still_works_when_no_full_date(self):
        # 既存の月/日形式の挙動を壊していないことの確認。
        result = extract_date_from_text("7/17 に3020円", today=_FIXED_TODAY)
        self.assertEqual(result, "2026-07-17")


class ExtractJapaneseDateFromTextTests(unittest.TestCase):
    """日本語の年月日・月日形式を原文から機械抽出する。"""

    _TODAY = datetime.date(2026, 8, 24)

    def test_japanese_month_day_in_compact_purchase_input(self):
        result = extract_date_from_text(
            "8月22日セリア1130円", today=self._TODAY)
        self.assertEqual(result, "2026-08-22")

    def test_fullwidth_japanese_month_day(self):
        result = extract_date_from_text(
            "８月２２日セリア１１３０円", today=self._TODAY)
        self.assertEqual(result, "2026-08-22")

    def test_japanese_month_day_can_touch_amount_digits(self):
        result = extract_date_from_text(
            "セリア8月22日1130円", today=self._TODAY)
        self.assertEqual(result, "2026-08-22")

    def test_japanese_full_date(self):
        result = extract_date_from_text(
            "2026年8月22日セリア1130円", today=self._TODAY)
        self.assertEqual(result, "2026-08-22")

    def test_fullwidth_japanese_full_date(self):
        result = extract_date_from_text(
            "２０２６年８月２２日セリア１１３０円", today=self._TODAY)
        self.assertEqual(result, "2026-08-22")

    def test_japanese_full_date_can_touch_amount_digits(self):
        result = extract_date_from_text(
            "2026年8月20日1130円", today=self._TODAY)
        self.assertEqual(result, "2026-08-20")

    def test_invalid_japanese_month_day_falls_back_to_today(self):
        text = "2月30日セリア1130円"
        self.assertIsNone(find_explicit_date(text, today=self._TODAY))
        self.assertEqual(find_explicit_dates(text, today=self._TODAY), [])
        self.assertEqual(
            extract_date_from_text(text, today=self._TODAY), "2026-08-24")

    def test_invalid_full_date_is_not_reinterpreted_in_leap_year(self):
        leap_year_today = datetime.date(2024, 8, 24)
        text = "2025年2月29日セリア1130円"
        self.assertIsNone(find_explicit_date(text, today=leap_year_today))
        self.assertEqual(find_explicit_dates(text, today=leap_year_today), [])
        self.assertEqual(
            extract_date_from_text(text, today=leap_year_today), "2024-08-24")

    def test_invalid_full_date_does_not_hide_separate_valid_date(self):
        leap_year_today = datetime.date(2024, 8, 24)
        text = "2025年2月29日、8月22日セリア1130円"
        self.assertEqual(
            find_explicit_date(text, today=leap_year_today), "2024-08-22")
        self.assertEqual(
            find_explicit_dates(text, today=leap_year_today), ["2024-08-22"])

    def test_valid_full_date_is_not_reinterpreted_as_month_day(self):
        dates = find_explicit_dates(
            "2025年8月22日セリア1130円",
            today=datetime.date(2024, 8, 24),
        )
        self.assertEqual(dates, ["2025-08-22"])

    def test_existing_formats_still_work(self):
        expected = {
            "8/22セリア1130円": "2026-08-22",
            "2026/08/22セリア1130円": "2026-08-22",
            "2026-08-22セリア1130円": "2026-08-22",
        }
        for text, iso_date in expected.items():
            with self.subTest(text=text):
                self.assertEqual(
                    extract_date_from_text(text, today=self._TODAY), iso_date)

    def test_invalid_existing_full_dates_are_not_shortened(self):
        leap_year_today = datetime.date(2024, 8, 24)
        for text in (
            "2025/02/29セリア1130円",
            "2025-02-29セリア1130円",
        ):
            with self.subTest(text=text):
                self.assertIsNone(
                    find_explicit_date(text, today=leap_year_today))
                self.assertEqual(
                    find_explicit_dates(text, today=leap_year_today), [])
                self.assertEqual(
                    extract_date_from_text(text, today=leap_year_today),
                    "2024-08-24",
                )


if __name__ == "__main__":
    unittest.main()
