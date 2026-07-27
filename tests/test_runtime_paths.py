import os
import unittest
from unittest.mock import patch

import LLM_Local_Chat as app


class RuntimePathTests(unittest.TestCase):
    def test_runtime_paths_are_based_on_application_directory(self):
        with patch("os.getcwd", return_value=r"C:\unrelated"):
            self.assertEqual(app.SETTINGS_FILE, app.app_path("chat_settings.json"))
            self.assertEqual(app.LOG_DIR, app.app_path("chat_logs"))
            self.assertEqual(
                app.AVATAR_DEFAULT,
                app.app_path("avatars", "default_avatar.png"),
            )

    def test_relative_model_path_is_based_on_application_directory(self):
        relative = os.path.join("models", "model.gguf")
        self.assertEqual(app.resolve_model_path(relative), app.app_path(relative))

    def test_absolute_model_path_is_preserved(self):
        absolute = os.path.abspath(os.path.join("X:\\", "models", "model.gguf"))
        self.assertEqual(app.resolve_model_path(absolute), absolute)


if __name__ == "__main__":
    unittest.main()
