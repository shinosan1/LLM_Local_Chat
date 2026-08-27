import os
import tempfile
import unittest
from hashlib import sha256
from pathlib import Path

from PIL import Image

from prompt_builder import PromptBuilder, PromptInputTooLargeError
from prompt_inputs import (
    Attachment,
    IMAGE_CONTEXT_TOKEN_RESERVE,
    MAX_TEXT_ATTACHMENT_BYTES,
    PromptInputError,
    attachment_display_names,
    attachment_fingerprint,
    build_multimodal_user_content,
    format_text_attachment_input,
    load_attachment,
    load_attachment_bytes,
    resolve_system_prompt,
    validate_attachment_set,
)


class PersonalizationPromptTests(unittest.TestCase):
    def test_old_settings_keep_default_prompt(self):
        prompt, errors = resolve_system_prompt({}, "default", os.getcwd())
        self.assertEqual(prompt, "default")
        self.assertEqual(errors, [])

    def test_inline_and_external_prompts_are_combined_in_order_and_deduplicated(self):
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
        self.assertEqual(
            prompt,
            "inline system\n\n外部system\n\ninline persona\n\n"
            "外部persona\n\n日本語で回答",
        )
        self.assertEqual(prompt.count("外部persona"), 1)

    def test_identical_inline_and_external_prompt_is_inserted_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            Path(temp_dir, "system.md").write_text(
                "same prompt", encoding="utf-8")
            prompt, errors = resolve_system_prompt({
                "system_prompt": "same prompt",
                "external_prompt_files": {"system_prompt": "system.md"},
            }, "default", temp_dir)
        self.assertEqual(errors, [])
        self.assertEqual(prompt, "same prompt")

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

    def test_session_attachment_requires_full_history_at_context_boundary(self):
        builder = PromptBuilder("system")
        session = {
            "summary": "summary",
            "history": [
                {"user": "old user 1", "assistant": "old answer 1"},
                {"user": "old user 2", "assistant": "old answer 2"},
            ],
        }
        user_text = "question with attachment"
        system_text = "system\n\n[要約]: summary"
        fixed_tokens = (
            1
            + len(system_text)
            + len(user_text)
            + IMAGE_CONTEXT_TOKEN_RESERVE
        )
        history_tokens = sum(
            len(item["user"]) + len(item["assistant"]) + 12
            for item in session["history"]
        )
        exact_limit = fixed_tokens + history_tokens
        kwargs = dict(
            llm=object(),
            max_tokens=1,
            count_tokens_func=lambda _llm, text: len(text),
            system_buf_tokens=0,
            extra_reserved_tokens=IMAGE_CONTEXT_TOKEN_RESERVE,
            enforce_context_limit=True,
            require_full_history=True,
        )

        messages = builder.build(
            user_text, session, n_ctx=exact_limit, **kwargs)
        self.assertEqual(
            [message["content"] for message in messages[1:-1]],
            ["old user 1", "old answer 1", "old user 2", "old answer 2"],
        )

        with self.assertRaisesRegex(
            PromptInputTooLargeError, "会話履歴.*context上限"
        ):
            builder.build(
                user_text, session, n_ctx=exact_limit - 1, **kwargs)

    def test_fileless_chat_keeps_existing_history_budget_behavior(self):
        builder = PromptBuilder("system")
        messages = builder.build(
            "question",
            {"history": [{"user": "old user", "assistant": "old answer"}]},
            llm=object(),
            n_ctx=16,
            max_tokens=1,
            count_tokens_func=lambda _llm, _text: 1,
            history_budget_ratio=0.5,
            system_buf_tokens=0,
            enforce_context_limit=True,
        )
        self.assertEqual(
            messages,
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": "question"},
            ],
        )

    def test_single_attachment_is_not_duplicated_in_one_turn(self):
        attachment = Attachment(
            name="sample.csv",
            kind="text",
            mime_type="text/plain",
            text="date,meal\n2026-08-27,coffee",
        )
        prompt = format_text_attachment_input("続けてください", [attachment])
        self.assertEqual(prompt.count("[添付ファイル]"), 1)
        self.assertEqual(prompt.count("2026-08-27,coffee"), 1)

    def test_same_name_different_content_has_distinct_fingerprints_and_display_names(self):
        first = load_attachment_bytes("data.csv", b"date,value\n1,one\n")
        second = load_attachment_bytes("data.csv", b"date,value\n2,two\n")
        self.assertNotEqual(attachment_fingerprint(first), attachment_fingerprint(second))
        self.assertEqual(
            attachment_display_names([first, second]),
            ["data.csv", "data.csv (2)"],
        )
        prompt = format_text_attachment_input("質問", [first, second, first])
        self.assertEqual(prompt.count("[添付ファイル]"), 2)
        self.assertIn("ファイル名: data.csv\n", prompt)
        self.assertIn("ファイル名: data.csv (2)", prompt)
        self.assertEqual(prompt.count("date,value\n1,one"), 1)
        self.assertEqual(prompt.count("date,value\n2,two"), 1)

    def test_raw_bytes_metadata_and_reconstruction_are_preserved(self):
        raw = b'\xef\xbb\xbf{"answer": 42}\n'
        attachment = load_attachment_bytes(
            "payload.json", raw, attachment_id="saved-attachment")
        self.assertEqual(attachment.attachment_id, "saved-attachment")
        self.assertEqual(attachment.extension, ".json")
        self.assertEqual(attachment.size, len(raw))
        self.assertEqual(attachment.sha256, sha256(raw).hexdigest())
        self.assertEqual(attachment.data, raw)
        self.assertEqual(attachment.text, '{"answer": 42}\n')
        self.assertEqual(attachment.mime_type, "application/json")

    def test_image_extension_must_match_verified_format(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "image.png"
            Image.new("RGB", (8, 8), "white").save(path, "JPEG")
            with self.assertRaisesRegex(PromptInputError, "一致"):
                load_attachment(str(path))

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
