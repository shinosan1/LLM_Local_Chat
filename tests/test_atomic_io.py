import json
import os
import tempfile
import unittest
from unittest.mock import patch

from atomic_io import atomic_write_json


class AtomicJsonWriteTests(unittest.TestCase):
    def test_failed_write_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"before": True}, handle)

            with patch("atomic_io.json.dump", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    atomic_write_json(path, {"after": True})

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"before": True})
            self.assertEqual(os.listdir(directory), ["settings.json"])

    def test_success_replaces_file_and_leaves_no_temp_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "settings.json")
            atomic_write_json(path, {"value": "更新"})

            with open(path, encoding="utf-8") as handle:
                self.assertEqual(json.load(handle), {"value": "更新"})
            self.assertEqual(os.listdir(directory), ["settings.json"])


if __name__ == "__main__":
    unittest.main()
