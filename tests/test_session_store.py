import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from session_store import SessionStore


class CountingSessionStore(SessionStore):
    def __init__(self, log_dir):
        super().__init__(log_dir)
        self.metadata_reads = 0

    def _read_metadata(self, path, fallback_title):
        self.metadata_reads += 1
        return super()._read_metadata(path, fallback_title)


class SessionStoreIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(
            prefix="test_session_store_")
        self.store = CountingSessionStore(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write(self, filename, title, summary=""):
        path = os.path.join(self.temp_dir.name, filename)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {"title": title, "summary": summary, "history": []},
                handle,
                ensure_ascii=False,
            )
        return path

    def test_repeated_search_reuses_parsed_metadata(self):
        self._write("chat_1.json", "Alpha", "first")
        self._write("chat_2.json", "Beta", "second")

        self.assertEqual(len(self.store.list_sessions()), 2)
        self.assertEqual(self.store.metadata_reads, 2)
        self.assertEqual(
            [item["title"] for item in self.store.list_sessions("sec")],
            ["Beta"],
        )
        self.assertEqual(self.store.metadata_reads, 2)

    def test_save_rename_and_delete_update_index_immediately(self):
        session = {"title": "Saved", "summary": "memo", "history": [{"user": "u"}]}
        path = self.store.save(session, None)
        self.assertEqual(self.store.list_sessions()[0]["title"], "Saved")
        self.assertEqual(self.store.metadata_reads, 0)

        self.store.rename(path, "Renamed")
        self.assertEqual(self.store.list_sessions()[0]["title"], "Renamed")
        self.assertEqual(self.store.metadata_reads, 0)

        self.store.delete(path)
        self.assertEqual(self.store.list_sessions(), [])

    def test_external_change_and_new_file_are_detected(self):
        path = self._write("chat_1.json", "Before")
        self.store.list_sessions()
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"title": "After with longer title", "history": []}, handle)
        self._write("chat_2.json", "New")

        titles = [item["title"] for item in self.store.list_sessions()]
        self.assertEqual(titles, ["New", "After with longer title"])
        self.assertEqual(self.store.metadata_reads, 3)

    def test_broken_json_is_ignored_until_it_changes(self):
        broken = os.path.join(self.temp_dir.name, "chat_2.json")
        with open(broken, "w", encoding="utf-8") as handle:
            handle.write("{")
        self._write("chat_1.json", "Valid")

        self.assertEqual(
            [item["title"] for item in self.store.list_sessions()], ["Valid"])
        reads_after_first_scan = self.store.metadata_reads
        self.store.list_sessions("anything")
        self.assertEqual(self.store.metadata_reads, reads_after_first_scan)

    def test_new_session_paths_are_unique_when_timestamp_collides(self):
        fixed = datetime.datetime(2026, 7, 19, 12, 34, 56, 123456)
        session = {"title": "Saved", "history": [{"user": "u"}]}
        with patch("session_store.datetime.datetime") as mocked_datetime:
            mocked_datetime.now.return_value = fixed
            first = self.store.save(session, None)
            second = self.store.save(session, None)

        self.assertNotEqual(first, second)
        self.assertTrue(os.path.exists(first))
        self.assertTrue(os.path.exists(second))


if __name__ == "__main__":
    unittest.main()
