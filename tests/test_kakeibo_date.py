import datetime
import unittest

from kakeibo_date import extract_date_from_text

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


if __name__ == "__main__":
    unittest.main()
