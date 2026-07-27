import unittest

from kakeibo_amount import extract_amount_result, normalize_manual_amount_input


def _status(text: str) -> str:
    return extract_amount_result(text)["status"]


class KakeiboAmountExtractionRegressionTests(unittest.TestCase):
    """Section C確定版の22件の入力例のうち、amount抽出結果を検証する。"""

    def test_case_1_simple_amount(self):
        self.assertEqual(extract_amount_result("セリアで雑貨を1870円買った")["amount"], 1870)

    def test_case_2_amount_present_despite_misheard_item(self):
        self.assertEqual(extract_amount_result("セイヤで1870円のザッカー4人")["amount"], 1870)

    def test_case_3_manen_conversion(self):
        self.assertEqual(extract_amount_result("給料20万円が入った")["amount"], 200000)

    def test_case_4_and_5_same_amount_different_type(self):
        self.assertEqual(
            extract_amount_result("メルカリでゲームを3000円で買った")["amount"], 3000)
        self.assertEqual(
            extract_amount_result("メルカリでゲームを3000円で売った")["amount"], 3000)

    def test_case_6_and_7_okozukai(self):
        self.assertEqual(
            extract_amount_result("子どもにお小遣い5000円を渡した")["amount"], 5000)
        self.assertEqual(
            extract_amount_result("お小遣い5000円をもらった")["amount"], 5000)

    def test_case_8_no_amount_mentioned(self):
        self.assertEqual(_status("パンを買った"), "no_amount")

    def test_case_9_amount_present_despite_category_confusion_risk(self):
        self.assertEqual(extract_amount_result("パンツを買った1000円")["amount"], 1000)

    def test_case_10_refund(self):
        self.assertEqual(extract_amount_result("病院から2000円返金された")["amount"], 2000)

    def test_case_11_multi_transaction_text_has_no_amount(self):
        # 原文に具体的な金額表記がないため、複数取引固有の挙動には到達しない
        self.assertEqual(_status("給料が入ったので洗剤を買った"), "no_amount")

    def test_case_12_simple_purchase(self):
        self.assertEqual(extract_amount_result("洗剤を1000円で買った")["amount"], 1000)

    def test_case_13_manen_only(self):
        self.assertEqual(extract_amount_result("給料20万円")["amount"], 200000)

    def test_case_14_and_15_bare_amount(self):
        self.assertEqual(extract_amount_result("1000円使った")["amount"], 1000)
        self.assertEqual(extract_amount_result("1000円")["amount"], 1000)

    def test_case_16_and_17_no_amount_mentioned(self):
        self.assertEqual(_status("給与が振り込まれた"), "no_amount")
        self.assertEqual(_status("ボーナスで旅行に行った"), "no_amount")

    def test_case_18_and_19_flea_market(self):
        self.assertEqual(extract_amount_result("フリマで売れた3000円")["amount"], 3000)
        self.assertEqual(extract_amount_result("フリマで買った3000円")["amount"], 3000)

    def test_case_20_utility_bill(self):
        self.assertEqual(extract_amount_result("電気代を支払った3000円")["amount"], 3000)

    def test_case_21_refund_income(self):
        self.assertEqual(extract_amount_result("還付金が入った5000円")["amount"], 5000)

    def test_case_22_transportation_reimbursement(self):
        self.assertEqual(extract_amount_result("交通費精算で2000円もらった")["amount"], 2000)


class KakeiboAmountNormalizationTests(unittest.TestCase):
    """全角数字・桁区切りカンマ・千円/万円換算の正規化例。"""

    def test_halfwidth_comma_separated(self):
        self.assertEqual(extract_amount_result("セリアで1,870円使った")["amount"], 1870)

    def test_fullwidth_digits(self):
        self.assertEqual(extract_amount_result("セリアで１８７０円使った")["amount"], 1870)

    def test_manen_conversion_example(self):
        self.assertEqual(extract_amount_result("給料20万円が入った")["amount"], 200000)

    def test_people_count_is_not_an_amount(self):
        self.assertEqual(_status("4人で食事した"), "no_amount")

    def test_multiple_amounts_are_rejected(self):
        result = extract_amount_result("500円と1200円使った")
        self.assertEqual(result["status"], "multiple_amounts")
        self.assertEqual(result["amounts"], [500, 1200])


class KakeiboAmountDangerousPartialMatchTests(unittest.TestCase):
    """危険な部分一致・不正表記を安全側(拒否)に倒すことを確認する。"""

    def test_decimal_manen_is_rejected(self):
        self.assertEqual(_status("1.5万円"), "invalid_amount_format")

    def test_decimal_manen_fullwidth_period_is_rejected(self):
        self.assertEqual(_status("1．5万円"), "invalid_amount_format")

    def test_compound_unit_manen_is_rejected(self):
        self.assertEqual(_status("1万2000円"), "invalid_amount_format")

    def test_compound_unit_senen_is_rejected(self):
        self.assertEqual(_status("1千500円"), "invalid_amount_format")

    def test_double_comma_is_rejected(self):
        self.assertEqual(_status("1,,000円"), "invalid_amount_format")

    def test_leading_comma_is_rejected(self):
        self.assertEqual(_status(",500円"), "invalid_amount_format")

    def test_zero_yen_is_rejected(self):
        self.assertEqual(_status("0円"), "invalid_amount_format")

    def test_negative_amount_is_rejected(self):
        self.assertEqual(_status("-1000円"), "invalid_amount_format")

    def test_negative_amount_fullwidth_minus_is_rejected(self):
        self.assertEqual(_status("−1000円"), "invalid_amount_format")

    def test_positive_signed_amount_is_rejected(self):
        self.assertEqual(_status("+1000円"), "invalid_amount_format")

    def test_fullwidth_hyphen_minus_amount_is_rejected(self):
        self.assertEqual(_status("－1000円"), "invalid_amount_format")

    def test_small_hyphen_minus_amount_is_rejected(self):
        self.assertEqual(_status("﹣1000円"), "invalid_amount_format")

    def test_en_dash_amount_is_rejected(self):
        self.assertEqual(_status("–1000円"), "invalid_amount_format")

    def test_em_dash_amount_is_rejected(self):
        self.assertEqual(_status("—1000円"), "invalid_amount_format")

    def test_word_minus_amount_is_rejected(self):
        self.assertEqual(_status("マイナス1000円"), "invalid_amount_format")

    def test_word_plus_amount_is_rejected(self):
        self.assertEqual(_status("プラス1000円"), "invalid_amount_format")

    def test_exponential_notation_lowercase_e_is_rejected(self):
        self.assertEqual(_status("1e3円"), "invalid_amount_format")

    def test_exponential_notation_uppercase_e_is_rejected(self):
        self.assertEqual(_status("1E3円"), "invalid_amount_format")

    def test_exponential_notation_with_positive_sign_is_rejected(self):
        self.assertEqual(_status("1e+3円"), "invalid_amount_format")

    def test_exponential_notation_with_negative_sign_is_rejected(self):
        self.assertEqual(_status("1e-3円"), "invalid_amount_format")

    def test_exponential_notation_mixed_with_valid_amount_is_rejected_entirely(self):
        result = extract_amount_result("1e3円と500円")
        self.assertEqual(result["status"], "invalid_amount_format")
        self.assertNotIn(500, result.get("amounts", []))

    def test_uppercase_exponential_notation_mixed_with_valid_amount_is_rejected_entirely(self):
        result = extract_amount_result("1E3円と500円")
        self.assertEqual(result["status"], "invalid_amount_format")
        self.assertNotIn(500, result.get("amounts", []))

    def test_currency_related_word_is_not_an_amount(self):
        self.assertEqual(_status("円安なので節約した"), "no_amount")

    def test_invalid_amount_mixed_with_valid_amount_is_rejected_entirely(self):
        result = extract_amount_result("1,,000円と500円")
        self.assertEqual(result["status"], "invalid_amount_format")

    def test_negative_amount_mixed_with_valid_amount_is_rejected_entirely(self):
        result = extract_amount_result("-1000円と500円")
        self.assertEqual(result["status"], "invalid_amount_format")

    def test_valid_multiple_amounts_without_invalid_ones(self):
        result = extract_amount_result("1000円と500円")
        self.assertEqual(result["status"], "multiple_amounts")


class KakeiboAmountSpacedDigitsTests(unittest.TestCase):
    """数字と単位の間の空白は許容するが、数字列の途中の空白は不正表記として拒否する。"""

    def test_space_between_number_and_unit_is_valid(self):
        self.assertEqual(extract_amount_result("1000 円")["amount"], 1000)

    def test_fullwidth_digits_with_space_before_manen_is_valid(self):
        self.assertEqual(extract_amount_result("２０ 万円")["amount"], 200000)

    def test_space_inside_digit_sequence_is_rejected(self):
        self.assertEqual(_status("1 500円"), "invalid_amount_format")

    def test_fullwidth_space_inside_digit_sequence_is_rejected(self):
        self.assertEqual(_status("12　500円"), "invalid_amount_format")

    def test_comma_then_space_inside_digit_sequence_is_rejected(self):
        self.assertEqual(_status("1, 500円"), "invalid_amount_format")

    def test_spaced_digits_mixed_with_valid_amount_is_rejected_entirely(self):
        result = extract_amount_result("1 500円と300円")
        self.assertEqual(result["status"], "invalid_amount_format")


class KakeiboAmountLlmDisagreementTests(unittest.TestCase):
    """LLM生成値と原文抽出値が異なっても、原文由来の値だけを採用する(抽出関数自体はLLM値を一切見ない)。"""

    def test_extraction_ignores_any_llm_value_by_construction(self):
        # 原文が「1000円」であれば、LLMが別の値を出していたかどうかに関わらず
        # この関数は原文だけを見て1000を返す(LLM値は引数にすら含まれない)。
        self.assertEqual(extract_amount_result("1000円")["amount"], 1000)


class ManualAmountInputNormalizationTests(unittest.TestCase):
    """確認画面でのamount手動編集時の正規化。"""

    def test_fullwidth_digits_with_comma(self):
        self.assertEqual(normalize_manual_amount_input("１,２００"), 1200)

    def test_plain_digits(self):
        self.assertEqual(normalize_manual_amount_input("1200"), 1200)

    def test_decimal_is_rejected(self):
        self.assertIsNone(normalize_manual_amount_input("1.5"))

    def test_negative_is_rejected(self):
        self.assertIsNone(normalize_manual_amount_input("-1000"))

    def test_zero_is_rejected(self):
        self.assertIsNone(normalize_manual_amount_input("0"))

    def test_empty_is_rejected(self):
        self.assertIsNone(normalize_manual_amount_input(""))

    def test_invalid_comma_grouping_is_rejected(self):
        self.assertIsNone(normalize_manual_amount_input("1,,000"))

    def test_unit_suffix_is_rejected(self):
        self.assertIsNone(normalize_manual_amount_input("1000円"))


if __name__ == "__main__":
    unittest.main()
