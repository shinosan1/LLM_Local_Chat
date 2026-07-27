import json
import re
import unittest

from json_extractors import (
    HEALTH_KEYS,
    extract_health_json,
    extract_kakeibo_json,
    strip_health_json,
)
from prompt_builder import PromptBuilder


class JsonExtractorRegressionTests(unittest.TestCase):
    def test_every_health_key_is_supported_in_bare_json(self):
        for key in HEALTH_KEYS:
            with self.subTest(key=key):
                value = '"記録"' if key in {
                    "meal_detail", "activity_log", "memo"
                } else "1"
                reply = f'{{"{key}":{value}}}'
                self.assertEqual(extract_health_json(reply)[key], "記録" if value.startswith('"') else 1)
                self.assertEqual(strip_health_json(reply), "")

    def test_kakeibo_skips_record_without_structure_keys(self):
        # 1つ目はamountキーのみでtype/category/storeを持たないため構造条件で除外され、
        # type/category/storeのいずれかを持つ2つ目が採用される。
        reply = (
            '```json\n{"amount": true}\n```\n'
            '```json\n{"amount": 1500, "store": "店"}\n```'
        )
        self.assertEqual(extract_kakeibo_json(reply)["amount"], 1500)

    def test_kakeibo_accepts_null_amount_with_structure_key(self):
        reply = '```json\n{"amount": null, "type": "支出"}\n```'
        record = extract_kakeibo_json(reply)
        self.assertIsNotNone(record)
        self.assertIsNone(record["amount"])

    def test_kakeibo_does_not_match_health_json(self):
        reply = '```json\n{"weight": 70, "body_fat": 20}\n```'
        self.assertIsNone(extract_kakeibo_json(reply))

    def test_kakeibo_does_not_match_memo_only_json(self):
        reply = '```json\n{"memo": "test"}\n```'
        self.assertIsNone(extract_kakeibo_json(reply))

    def test_kakeibo_does_not_match_arbitrary_code_example_json(self):
        reply = '```json\n{"foo": 1, "bar": 2}\n```'
        self.assertIsNone(extract_kakeibo_json(reply))

    def test_kakeibo_returns_none_for_broken_json(self):
        reply = '```json\n{"amount": 1000, "type": "支出"\n```'  # 閉じ括弧欠落
        self.assertIsNone(extract_kakeibo_json(reply))

    def test_health_uses_last_valid_candidate(self):
        reply = (
            '```json\n{"memo":"旧形式の例"}\n```\n'
            '```json\n{"weight":60,"memo":"本当の内容"}\n```'
        )
        self.assertEqual(
            extract_health_json(reply),
            {"weight": 60, "memo": "本当の内容"},
        )

    def test_health_keeps_memo_only_candidate(self):
        self.assertEqual(
            extract_health_json('{"memo":"記録"}'),
            {"memo": "記録"},
        )

    def test_non_finite_health_json_is_not_extracted_or_stripped(self):
        reply = '```json\n{"weight":NaN}\n```'
        self.assertIsNone(extract_health_json(reply))
        self.assertEqual(strip_health_json(reply), reply)


class KakeiboPromptTemplateTests(unittest.TestCase):
    def _prompt(self) -> str:
        return PromptBuilder("system").build_kakeibo_prompt("テスト入力")

    def test_json_template_is_valid_json(self):
        prompt = self._prompt()
        match = re.search(r'```json\s*(\{.*?\})\s*```', prompt, re.DOTALL)
        self.assertIsNotNone(match)
        data = json.loads(match.group(1))
        self.assertIsNone(data["type"])
        self.assertIsNone(data["amount"])
        self.assertIsNone(data["store"])
        self.assertIsNone(data["category"])
        self.assertIsNone(data["memo"])

    def test_no_fixed_expense_type_in_prompt(self):
        prompt = self._prompt()
        self.assertNotIn('"type": "支出"', prompt)

    def test_no_explanatory_text_as_type_value(self):
        prompt = self._prompt()
        self.assertNotIn("支出または収入", prompt)


if __name__ == "__main__":
    unittest.main()
