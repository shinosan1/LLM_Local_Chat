import os
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from prompt_builder import PromptBuilder, PromptInputTooLargeError
from prompt_inputs import (
    IMAGE_CONTEXT_TOKEN_RESERVE,
    MAX_TEXT_ATTACHMENT_BYTES,
    PromptInputError,
    build_multimodal_user_content,
    format_text_attachment_input,
    load_attachment,
    resolve_system_prompt,
    validate_attachment_set,
)


class PersonalizationPromptTests(unittest.TestCase):
    def test_old_settings_keep_default_prompt(self):
        prompt, errors = resolve_system_prompt({}, "default", os.getcwd())
        self.assertEqual(prompt, "default")
        self.assertEqual(errors, [])

    def test_external_system_and_personalization_take_priority_and_deduplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "system.md").write_text("外部system", encoding="utf-8")
            (root / "persona.txt").write_text("外部persona", encoding="utf-8")
            settings = {
                "system_prompt": "inline system",
                "user_personalization": "inline persona",
                "response_language": "日本語で回答",
                "external_prompt_files": {
                    "system_prompt": "system.md",
                    "user_personalization": "persona.txt",
                    "instructions": ["persona.txt"],
                },
            }

            prompt, errors = resolve_system_prompt(
                settings, "default", temp_dir)

        self.assertEqual(errors, [])
        self.assertIn("外部system", prompt)
        self.assertIn("外部persona", prompt)
        self.assertIn("日本語で回答", prompt)
        self.assertNotIn("inline system", prompt)
        self.assertNotIn("inline persona", prompt)
        self.assertEqual(prompt.count("外部persona"), 1)

    def test_missing_external_file_warns_and_falls_back_to_inline(self):
        settings = {
            "system_prompt": "inline system",
            "external_prompt_files": {"system_prompt": "missing.md"},
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            prompt, errors = resolve_system_prompt(
                settings, "default", temp_dir)
        self.assertEqual(prompt, "inline system")
        self.assertTrue(any("missing.md" in error for error in errors))


class AttachmentInputTests(unittest.TestCase):
    def test_prompt_budget_rejects_instead_of_truncating_current_input(self):
        builder = PromptBuilder("system")
        with self.assertRaises(PromptInputTooLargeError):
            builder.build(
                "very long attachment",
                {"history": []},
                llm=object(),
                n_ctx=10,
                max_tokens=5,
                count_tokens_func=lambda _llm, text: len(text),
                system_buf_tokens=1,
                enforce_context_limit=True,
            )

    def test_extra_image_reserve_is_included_in_prompt_budget(self):
        builder = PromptBuilder("system")
        with self.assertRaises(PromptInputTooLargeError):
            builder.build(
                "question",
                {"history": []},
                llm=object(),
                n_ctx=IMAGE_CONTEXT_TOKEN_RESERVE,
                max_tokens=1,
                count_tokens_func=lambda _llm, _text: 0,
                system_buf_tokens=0,
                extra_reserved_tokens=IMAGE_CONTEXT_TOKEN_RESERVE,
                enforce_context_limit=True,
            )

    def test_supported_text_files_are_distinct_from_user_text(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            for suffix in (".txt", ".md", ".json", ".csv"):
                with self.subTest(suffix=suffix):
                    path = Path(temp_dir) / f"sample{suffix}"
                    path.write_text("添付本文", encoding="utf-8")
                    attachment = load_attachment(str(path))
                    prompt = format_text_attachment_input(
                        "質問", [attachment])
                    self.assertEqual(attachment.kind, "text")
                    self.assertIn("質問", prompt)
                    self.assertIn(f"ファイル名: sample{suffix}", prompt)
                    self.assertIn("--- ファイル内容 ---", prompt)
                    self.assertIn("添付本文", prompt)
                    self.assertNotIn(temp_dir, prompt)

    def test_text_file_over_limit_is_rejected_without_truncation(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "large.txt"
            path.write_bytes(b"a" * (MAX_TEXT_ATTACHMENT_BYTES + 1))
            with self.assertRaisesRegex(PromptInputError, "上限"):
                load_attachment(str(path))

    def test_png_and_jpeg_become_local_data_uris_without_paths(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            attachments = []
            for suffix, image_format in ((".png", "PNG"), (".jpg", "JPEG")):
                path = Path(temp_dir) / f"image{suffix}"
                Image.new("RGB", (8, 8), "white").save(path, image_format)
                attachments.append(load_attachment(str(path)))

            for attachment in attachments:
                content = build_multimodal_user_content("説明して", [attachment])
                self.assertIsInstance(content, list)
                url = content[1]["image_url"]["url"]
                self.assertTrue(url.startswith(f"data:{attachment.mime_type};base64,"))
                self.assertNotIn(temp_dir, url)

    def test_more_than_one_image_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            attachments = []
            for index in range(2):
                path = Path(temp_dir) / f"image{index}.png"
                Image.new("RGB", (8, 8), "white").save(path, "PNG")
                attachments.append(load_attachment(str(path)))
            with self.assertRaisesRegex(PromptInputError, "1枚"):
                validate_attachment_set(attachments)


if __name__ == "__main__":
    unittest.main()
