import unittest
import math
from unittest.mock import patch

from integrations import (
    BIOLOG_TEXT_FIELDS,
    BIOLOG_TEXT_MAX_LENGTH,
    BiologValidationError,
    IntegrationBridge,
    extract_explicit_health_fields,
    prepare_biolog_record,
    sanitize_biolog_record,
)
from json_extractors import extract_health_json, strip_health_json
from prompt_builder import PromptBuilder


class HealthPromptTests(unittest.TestCase):
    def test_measurements_are_excluded_from_activity_log(self):
        prompt = PromptBuilder("system").build_health_prompt("体脂肪率18%")

        self.assertIn("測定文を activity_log に複製しない", prompt)
        self.assertIn("測定文を memo にも複製しない", prompt)
        self.assertIn("activity_log と memo を null", prompt)

    def test_health_json_is_requested_before_short_natural_reply(self):
        prompt = PromptBuilder("system").build_health_prompt("体脂肪率18%")
        self.assertIn("必ず最初に以下のJSON形式", prompt)
        self.assertIn("自然な返答を1〜2文だけ", prompt)

    def test_retry_prompt_requests_json_only_with_field_mapping(self):
        prompt = PromptBuilder("system").build_health_extraction_prompt("血圧上98 下63")
        self.assertIn("JSONオブジェクト1個だけ", prompt)
        self.assertIn("systolic_bp=上の血圧", prompt)
        self.assertIn("diastolic_bp=下の血圧", prompt)
        self.assertIn('"memo":null', prompt)
        self.assertIn("明示された内容だけをmemo", prompt)

    def test_health_prompt_requests_explicit_memo_verbatim(self):
        prompt = PromptBuilder("system").build_health_prompt("メモ テストデータ入力")
        self.assertIn('"memo"', prompt)
        self.assertIn("メモ・備考として明示された内容", prompt)

    def test_health_prompts_treat_text_only_records_as_valid(self):
        builder = PromptBuilder("system")
        for prompt in (
            builder.build_health_prompt("食事ログ コーヒー 水 メモ テストデータ"),
            builder.build_health_extraction_prompt(
                "食事ログ コーヒー 水 メモ テストデータ"
            ),
        ):
            with self.subTest(prompt=prompt):
                self.assertIn("食事・行動・メモだけの入力も有効", prompt)
                self.assertIn('"meal_detail":"コーヒー 水"', prompt)
                self.assertIn('"memo":"テストデータ"', prompt)


class BiologSanitizationTests(unittest.TestCase):
    def test_bridge_preserves_explicit_provenance_for_final_sanitize(self):
        bridge = IntegrationBridge(None, lambda *_args: None)
        sent = []
        bridge._send_to_biolog_api = sent.append
        with patch("integrations.messagebox.askyesno", return_value=True):
            bridge.confirm_and_send_biolog(
                {"weight": 60, "activity_log": "体重60kg"},
                {"activity_log"},
            )
        self.assertEqual(sent, [{"weight": 60, "activity_log": "体重60kg"}])

    def test_bridge_rejects_invalid_record_before_confirmation(self):
        writes = []
        bridge = IntegrationBridge(None, lambda text, tag: writes.append((text, tag)))
        sent = []
        bridge._send_to_biolog_api = sent.append
        with patch("integrations.messagebox.askyesno") as confirm:
            bridge.confirm_and_send_biolog({"weight": math.nan})

        confirm.assert_not_called()
        self.assertEqual(sent, [])
        self.assertTrue(any("不正な形式" in text for text, _tag in writes))

    def test_explicit_meal_and_memo_are_extracted_from_user_text(self):
        fields, explicit = extract_explicit_health_fields(
            "食事ログ　コーヒー　水　メモ　テストデータは食事ログを追加"
        )
        self.assertEqual(fields, {
            "meal_detail": "コーヒー　水",
            "memo": "テストデータは食事ログを追加",
        })
        self.assertEqual(explicit, frozenset({"meal_detail", "memo"}))

    def test_explicit_labels_support_aliases_separators_and_deduplication(self):
        fields, _ = extract_explicit_health_fields(
            "食事ログ追加: 朝食、食事ログ 昼食\n"
            "行動ログ追加 散歩, 行動ログ 散歩、メモ追加: 良好"
        )
        self.assertEqual(fields, {
            "meal_detail": "朝食\n昼食",
            "activity_log": "散歩",
            "memo": "良好",
        })

    def test_particle_following_label_is_not_treated_as_explicit_field(self):
        for text in (
            "食事ログを見てください",
            "今日は 食事ログ を確認した",
            "行動ログは後で入力する",
        ):
            with self.subTest(text=text):
                self.assertEqual(extract_explicit_health_fields(text), (
                    {}, frozenset()
                ))

    def test_explicit_fields_override_llm_values(self):
        result, explicit = prepare_biolog_record(
            {"meal_detail": "コーヒー", "memo": "テストデータ"},
            "食事ログ コーヒー 水 メモ テストデータは食事ログを追加",
        )
        self.assertEqual(result, {
            "meal_detail": "コーヒー 水",
            "memo": "テストデータは食事ログを追加",
        })
        self.assertEqual(explicit, frozenset({"meal_detail", "memo"}))

    def test_explicit_measurement_text_activity_is_preserved(self):
        result, explicit = prepare_biolog_record(
            {"weight": 60}, "行動ログ 体重60kg"
        )
        self.assertEqual(result, {"weight": 60, "activity_log": "体重60kg"})
        self.assertEqual(explicit, frozenset({"activity_log"}))

    def test_explicit_memo_is_preserved(self):
        result = sanitize_biolog_record({"memo": "テストデータ入力"})
        self.assertEqual(result, {"memo": "テストデータ入力"})

    def test_measurement_duplicate_is_removed_from_implicit_memo(self):
        result = sanitize_biolog_record({
            "body_fat": 17.9,
            "memo": "体脂肪率17.9%",
        })
        self.assertEqual(result, {"body_fat": 17.9})

    def test_supported_measurement_duplicates_are_removed_from_memo(self):
        cases = (
            ({"weight": 64.2}, "体重64.2kg"),
            ({"temperature": 36.5}, "体温36.5℃"),
            ({"pulse": 70}, "脈拍70bpm"),
            ({"systolic_bp": 120, "diastolic_bp": 80}, "血圧120/80mmHg"),
            ({"muscle_mass": 45}, "筋肉量45kg"),
            ({"bmr": 1400}, "基礎代謝1400kcal"),
            (
                {"weight": 64.2, "body_fat": 17.9},
                "体重64.2kg、体脂肪率17.9%",
            ),
        )
        for measurements, memo in cases:
            with self.subTest(memo=memo):
                result = sanitize_biolog_record({**measurements, "memo": memo})
                self.assertNotIn("memo", result)

    def test_explicit_measurement_memo_is_preserved(self):
        result, explicit = prepare_biolog_record(
            {"body_fat": 17.9, "memo": "体脂肪率17.9%"},
            "メモ 体脂肪率17.9%",
        )
        self.assertEqual(result, {
            "body_fat": 17.9,
            "memo": "体脂肪率17.9%",
        })
        self.assertEqual(explicit, frozenset({"memo"}))

    def test_non_measurement_only_memos_are_preserved(self):
        cases = (
            "体脂肪率17.9%を目標にする",
            "体脂肪率を測って安心した",
        )
        for memo in cases:
            with self.subTest(memo=memo):
                result = sanitize_biolog_record({
                    "body_fat": 17.9,
                    "memo": memo,
                })
                self.assertEqual(result["memo"], memo)

    def test_measurement_memo_without_corresponding_value_is_preserved(self):
        result = sanitize_biolog_record({
            "weight": 64.2,
            "memo": "体脂肪率17.9%",
        })
        self.assertEqual(result["memo"], "体脂肪率17.9%")

    def test_null_memo_is_omitted_from_activity_only_payload(self):
        result = sanitize_biolog_record({
            "activity_log": "AI生態資源動画編集",
            "memo": None,
        })
        self.assertEqual(result, {"activity_log": "AI生態資源動画編集"})

    def test_memo_only_json_is_extracted_and_removed_from_history(self):
        for reply in (
            '{"memo":"テストデータ入力"}',
            '```json\n{"memo":"テストデータ入力"}\n```',
        ):
            with self.subTest(reply=reply):
                self.assertEqual(
                    extract_health_json(reply),
                    {"memo": "テストデータ入力"},
                )
                self.assertEqual(strip_health_json(reply), "")

    def test_body_fat_duplicate_is_removed(self):
        result = sanitize_biolog_record({
            "body_fat": 18,
            "activity_log": "体脂肪率18%",
        })
        self.assertEqual(result["body_fat"], 18)
        self.assertIsNone(result["activity_log"])

    def test_supported_measurement_duplicates_are_removed(self):
        cases = (
            ({"weight": 64.2}, "体重64.2kg"),
            ({"temperature": 36.5}, "体温36.5℃"),
            ({"pulse": 70}, "脈拍70bpm"),
            ({"systolic_bp": 120, "diastolic_bp": 80}, "血圧120/80mmHg"),
            ({"muscle_mass": 45}, "筋肉量45kg"),
            ({"bmr": 1400}, "基礎代謝1400kcal"),
        )
        for measurements, activity in cases:
            with self.subTest(activity=activity):
                result = sanitize_biolog_record({
                    **measurements,
                    "activity_log": activity,
                })
                self.assertIsNone(result["activity_log"])

    def test_multiple_measurement_duplicates_are_removed(self):
        result = sanitize_biolog_record({
            "weight": 64.2,
            "body_fat": 18,
            "temperature": 36.5,
            "activity_log": "体重64.2kg、体脂肪率18% 体温36.5℃",
        })
        self.assertIsNone(result["activity_log"])

    def test_measurement_text_is_kept_without_corresponding_value(self):
        result = sanitize_biolog_record({
            "weight": 64.2,
            "activity_log": "体脂肪率18%",
        })
        self.assertEqual(result["activity_log"], "体脂肪率18%")

    def test_real_activities_are_preserved(self):
        activities = (
            "30分散歩した",
            "体脂肪率を測ってから30分散歩した",
            "体脂肪率18%を目標に運動した",
            "仕事で10kgの荷物を運んだ",
        )
        for activity in activities:
            with self.subTest(activity=activity):
                result = sanitize_biolog_record({
                    "body_fat": 18,
                    "activity_log": activity,
                })
                self.assertEqual(result["activity_log"], activity)

    def test_unknown_keys_are_removed(self):
        result = sanitize_biolog_record({
            "body_fat": 18,
            "token": "secret",
        })
        self.assertEqual(result, {"body_fat": 18})

    def test_record_with_no_values_is_rejected(self):
        self.assertIsNone(sanitize_biolog_record({
            "date": "2026-07-19",
            "activity_log": "",
        }))

    def test_whitespace_only_text_fields_are_rejected(self):
        self.assertIsNone(sanitize_biolog_record({
            "meal_detail": "  ",
            "activity_log": "\t",
            "memo": "\n",
        }))

    def test_zero_numeric_value_is_not_treated_as_blank_text(self):
        self.assertEqual(
            sanitize_biolog_record({"body_fat": 0.0, "memo": " "}),
            {"body_fat": 0.0},
        )

    def test_integer_valued_float_is_normalized_for_integer_fields(self):
        self.assertEqual(
            sanitize_biolog_record({"pulse": 72.0}),
            {"pulse": 72},
        )

    def test_invalid_field_rejects_entire_record(self):
        invalid_records = (
            {"weight": math.nan, "memo": "valid"},
            {"weight": math.inf, "memo": "valid"},
            {"weight": True, "memo": "valid"},
            {"weight": "60", "memo": "valid"},
            {"weight": 10 ** 1000, "memo": "valid"},
            {"pulse": 72.5, "memo": "valid"},
            {"temperature": 43.0, "memo": "valid"},
            {"weight": 300, "memo": "valid"},
            {"activity_log": {"token": "secret"}, "weight": 60},
            {"memo": ["text"], "weight": 60},
            {"date": "2026-02-30", "weight": 60},
            {"date": "2026-7-2", "weight": 60},
        )
        for record in invalid_records:
            with self.subTest(record=record):
                with self.assertRaises(BiologValidationError):
                    sanitize_biolog_record(record)

    def test_numeric_boundaries_match_biolog_schema(self):
        valid = {
            "temperature": 34.0,
            "pulse": 200,
            "systolic_bp": 50,
            "diastolic_bp": 150,
            "weight": 0.1,
            "body_fat": 100.0,
            "muscle_mass": 199.9,
            "bmr": 4999,
        }
        self.assertEqual(sanitize_biolog_record(valid), valid)

    def test_null_date_is_omitted_for_api_default(self):
        self.assertEqual(
            sanitize_biolog_record({"date": None, "memo": "記録"}),
            {"memo": "記録"},
        )


class BiologTextLengthTests(unittest.TestCase):
    def test_accepts_text_at_max_length(self):
        """上限ちょうどの長さは受け付ける(境界値)。"""
        for field in BIOLOG_TEXT_FIELDS:
            with self.subTest(field=field):
                text = "あ" * BIOLOG_TEXT_MAX_LENGTH
                payload = sanitize_biolog_record({field: text}, {field})
                self.assertIsNotNone(payload)
                self.assertEqual(payload[field], text)

    def test_rejects_text_over_max_length(self):
        """上限を1文字でも超えたらBiologへ送らない(境界値)。"""
        for field in BIOLOG_TEXT_FIELDS:
            with self.subTest(field=field):
                text = "あ" * (BIOLOG_TEXT_MAX_LENGTH + 1)
                with self.assertRaises(BiologValidationError):
                    sanitize_biolog_record({field: text}, {field})


if __name__ == "__main__":
    unittest.main()
