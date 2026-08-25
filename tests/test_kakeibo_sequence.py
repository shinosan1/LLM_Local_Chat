"""複数取引の逐次確認・逐次POST制御のテスト。

本番の `ChatApp._confirm_and_send_kakeibo()` を直接呼び、次を検証する。

  - 確認ダイアログとPOSTの順序
  - POSTは前の完了通知を受けてから次を送る(並列化しない)
  - ユーザーが個別にスキップしても残りは処理を続ける
  - 「残りを中止」で以降を処理しない
  - POST失敗で残りを処理しない
  - 例外が起きても進行中フラグが残らない

Tkinterの実ウィンドウは作らず、確認ダイアログ・`wait_window`・送信を差し替える。
"""
import datetime
import unittest
from unittest.mock import patch

import LLM_Local_Chat as app_module
from LLM_Local_Chat import ChatApp
from kakeibo_confirmation import validate_kakeibo_payload
from kakeibo_split import build_kakeibo_candidates


THREE_TEXT = "スーパーで2000円、コンビニで500円、薬局で1200円"
THREE_TX = [
    {"source_text": "スーパーで2000円"},
    {"source_text": "コンビニで500円"},
    {"source_text": "薬局で1200円"},
]

REAL_FIVE_TEXT = (
    "業務スーパー8月20日、1,603円食料品セリア、8月20日、1,430円 日用品, "
    "ダイソー、8月20日、1,100円日用品, コーナン、8月21日、525円日用品, "
    "松源8月19日、3,963円 食料品"
)
REAL_FIVE_LLM_TX = [
    {"source_text": "業務スーパー8月20日、1,603円食料品",
     "store": "業務スーパー", "category": "食費", "type": "支出"},
    {"source_text": "セリア、8月20日、1,430円日用品",
     "store": "セリア", "category": "日用品", "type": "支出"},
    {"source_text": "ダイソー、8月20日、1,100円日用品",
     "store": "ダイソー", "category": "日用品", "type": "支出"},
    {"source_text": "コーナン、8月21日、525円日用品",
     "store": "コーナン", "category": "日用品", "type": "支出"},
    {"source_text": "松源8月19日、3,963円食料品",
     "store": "松源", "category": "食費", "type": "支出"},
]

RIGHT_BOUNDARY_FIVE_TEXT = (
    "無印2170円8月18日日用品、ダイソー660円8月10日日用品、"
    "業務スーパー8月10日食料品1361円、キャンドゥー7月26日330円日用品"
    "キャンドゥー8月18日880円日用品"
)
RIGHT_BOUNDARY_FIVE_TX = [
    {"source_text": "無印2170円8月18日日用品", "store": "無印",
     "category": "日用品", "type": "支出"},
    {"source_text": "ダイソー660円8月10日日用品", "store": "ダイソー",
     "category": "日用品", "type": "支出"},
    {"source_text": "業務スーパー8月10日食料品1361円", "store": "業務スーパー",
     "category": "食費", "type": "支出"},
    {"source_text": "キャンドゥー7月26日330円日用品", "store": "キャンドゥー",
     "category": "日用品", "type": "支出"},
    {"source_text": "キャンドゥー8月18日880円日用品", "store": "キャンドゥー",
     "category": "日用品", "type": "支出"},
]


def three_candidates():
    result = build_kakeibo_candidates(THREE_TX, THREE_TEXT)
    assert result["status"] == "ok", result["status"]
    for candidate in result["candidates"]:
        candidate["type"] = "支出"
        candidate["category"] = "食費"
    return result["candidates"]


class _FakeDialog:
    """KakeiboConfirmDialog の代わり。生成時に次の操作を決める。"""

    script = []
    opened = []
    raise_on = None

    def __init__(self, parent, candidate, user_text, position=1, total=1):
        if _FakeDialog.raise_on == position:
            raise RuntimeError("dialog boom")
        _FakeDialog.opened.append(position)
        action = _FakeDialog.script[position - 1]
        self.aborted = action == "abort"
        self.result = dict(candidate) if action == "submit" else None


class _FakeRoot:
    def wait_window(self, _dialog):
        return None


class _FakeIntegrations:
    def __init__(self, post_results):
        self._results = list(post_results)
        self.posted = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.pending = None

    def send_kakeibo(self, record, on_complete=None):
        self.posted.append(record)
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        success = self._results.pop(0) if self._results else True
        self.in_flight -= 1
        if on_complete is not None:
            on_complete(success)


class _FakeButton:
    def __init__(self):
        self.state = "normal"

    def config(self, **kwargs):
        if "state" in kwargs:
            self.state = kwargs["state"]


class _FakeController:
    """is_busy だけを持つ最小コントローラ。"""

    def __init__(self, app):
        self._app = app
        self.extra_busy = False

    def is_busy(self):
        return self.extra_busy or bool(
            getattr(self._app, "_kakeibo_sequence_active", False))


class _FakeApp:
    """ChatApp のうち、逐次制御が触る部分だけを持つ最小オブジェクト。"""

    def __init__(self, post_results):
        self.root = _FakeRoot()
        self._integrations = _FakeIntegrations(post_results)
        self._kakeibo_sequence_active = False
        self.messages = []
        self._btn_send = _FakeButton()
        self._ctrl = _FakeController(self)
        self.status_updates = 0

    def _chat_write(self, text, tag=None):
        self.messages.append(text)

    def _update_status(self):
        self.status_updates += 1

    def _restore_ui_after_kakeibo_sequence(self):
        # UI復旧も本番の実装を通す。
        ChatApp._restore_ui_after_kakeibo_sequence(self)

    def run(self, candidates):
        # 本番のメソッドをそのまま呼ぶ(テスト側で制御フローを再実装しない)。
        ChatApp._confirm_and_send_kakeibo(self, candidates)


class SequentialConfirmAndPostTests(unittest.TestCase):
    def setUp(self):
        self._original_dialog = app_module.KakeiboConfirmDialog
        app_module.KakeiboConfirmDialog = _FakeDialog
        _FakeDialog.opened = []
        _FakeDialog.raise_on = None
        self.addCleanup(self._restore)

    def _restore(self):
        app_module.KakeiboConfirmDialog = self._original_dialog
        _FakeDialog.opened = []
        _FakeDialog.raise_on = None

    def _run(self, script, post_results):
        _FakeDialog.script = script
        app = _FakeApp(post_results)
        app.run(three_candidates())
        return app

    def test_three_transactions_open_and_post_in_order(self):
        app = self._run(["submit"] * 3, [True] * 3)
        self.assertEqual(_FakeDialog.opened, [1, 2, 3])
        self.assertEqual(
            [r["amount"] for r in app._integrations.posted], [2000, 500, 1200])

    def test_posts_are_never_parallel(self):
        app = self._run(["submit"] * 3, [True] * 3)
        self.assertEqual(app._integrations.max_in_flight, 1)

    def test_sequence_flag_is_cleared_after_completion(self):
        app = self._run(["submit"] * 3, [True] * 3)
        self.assertFalse(app._kakeibo_sequence_active)

    def test_second_transaction_skipped(self):
        app = self._run(["submit", "skip", "submit"], [True, True])
        self.assertEqual(_FakeDialog.opened, [1, 2, 3])
        self.assertEqual(
            [r["amount"] for r in app._integrations.posted], [2000, 1200])
        self.assertFalse(app._kakeibo_sequence_active)

    def test_abort_does_not_open_remaining_dialogs(self):
        app = self._run(["submit", "abort", "submit"], [True])
        self.assertEqual(_FakeDialog.opened, [1, 2])
        self.assertEqual([r["amount"] for r in app._integrations.posted], [2000])
        self.assertFalse(app._kakeibo_sequence_active)

    def test_post_failure_stops_remaining(self):
        app = self._run(["submit"] * 3, [True, False])
        self.assertEqual(_FakeDialog.opened, [1, 2])
        self.assertEqual(
            [r["amount"] for r in app._integrations.posted], [2000, 500])
        self.assertFalse(app._kakeibo_sequence_active)

    def test_window_close_is_treated_as_abort(self):
        # ×ボタン相当(aborted=True)で残りを処理しない。
        app = self._run(["abort", "submit", "submit"], [])
        self.assertEqual(_FakeDialog.opened, [1])
        self.assertEqual(app._integrations.posted, [])
        self.assertFalse(app._kakeibo_sequence_active)

    def test_second_sequence_is_refused_while_active(self):
        _FakeDialog.script = ["submit"] * 3
        app = _FakeApp([True] * 3)
        app._kakeibo_sequence_active = True
        app.run(three_candidates())
        self.assertEqual(_FakeDialog.opened, [])
        self.assertEqual(app._integrations.posted, [])
        self.assertTrue(
            any("進行中" in m for m in app.messages), app.messages)

    def test_dialog_exception_clears_sequence_flag(self):
        _FakeDialog.script = ["submit"] * 3
        _FakeDialog.raise_on = 2
        app = _FakeApp([True] * 3)
        app.run(three_candidates())
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual([r["amount"] for r in app._integrations.posted], [2000])

    def test_send_exception_clears_sequence_flag(self):
        def _boom(record, on_complete=None):
            raise RuntimeError("send boom")

        _FakeDialog.script = ["submit"] * 3
        app = _FakeApp([True] * 3)
        app._integrations.send_kakeibo = _boom
        app.run(three_candidates())
        self.assertFalse(app._kakeibo_sequence_active)

    def test_empty_candidates_does_not_start_sequence(self):
        app = _FakeApp([])
        app.run([])
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual(_FakeDialog.opened, [])

    def test_japanese_date_reaches_validated_post_payload(self):
        real_date = datetime.date
        fixed_today = real_date(2026, 8, 24)
        text = "8月22日セリア1130円"
        with patch("kakeibo_date.datetime.date") as mocked_date:
            mocked_date.today.return_value = fixed_today
            mocked_date.side_effect = lambda *args, **kwargs: real_date(
                *args, **kwargs)
            result = build_kakeibo_candidates(
                [{"source_text": text, "store": "セリア"}], text)

        self.assertEqual(result["status"], "ok")
        candidate = result["candidates"][0]
        candidate["type"] = "支出"
        candidate["category"] = "日用品"
        self.assertEqual(candidate["date"], "2026-08-22")

        _FakeDialog.script = ["submit"]
        app = _FakeApp([True])
        app.run([candidate])
        self.assertEqual(len(app._integrations.posted), 1)
        payload = validate_kakeibo_payload(app._integrations.posted[0])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["date"], "2026-08-22")

    def test_date_adjacent_amount_reaches_confirmation_and_post_payload(self):
        real_date = datetime.date
        fixed_today = real_date(2026, 8, 24)
        text = "業務スーパー8/20 1603円 食料品"
        with patch("kakeibo_date.datetime.date") as mocked_date:
            mocked_date.today.return_value = fixed_today
            mocked_date.side_effect = lambda *args, **kwargs: real_date(
                *args, **kwargs)
            result = build_kakeibo_candidates(
                [{
                    "source_text": text,
                    "type": "支出",
                    "category": "食費",
                    "store": "業務スーパー",
                }],
                text,
            )

        self.assertEqual(result["status"], "ok")
        candidate = result["candidates"][0]
        self.assertEqual(candidate["date"], "2026-08-20")
        self.assertEqual(candidate["amount"], 1603)

        _FakeDialog.script = ["submit"]
        app = _FakeApp([True])
        app.run([candidate])
        self.assertEqual(len(app._integrations.posted), 1)
        payload = validate_kakeibo_payload(app._integrations.posted[0])
        self.assertIsNotNone(payload)
        self.assertEqual(payload["date"], "2026-08-20")
        self.assertEqual(payload["amount"], 1603)


class _DeferredIntegrations:
    """完了通知を自動で呼ばない送信フェイク。

    本番のPOSTは別スレッドで走り、完了通知はUIスレッドへ後から戻る。
    その非同期性を再現するため、テスト側が明示的に callback を呼ぶまで
    シーケンスが先へ進まないことを確認できるようにする。
    """

    def __init__(self):
        self.posted = []
        self.pending = []

    def send_kakeibo(self, record, on_complete=None):
        self.posted.append(record)
        self.pending.append(on_complete)

    def complete(self, index, success=True):
        self.pending[index](success)


class _DeferredApp(_FakeApp):
    def __init__(self):
        super().__init__([])
        self._integrations = _DeferredIntegrations()


class DeferredCompletionTests(unittest.TestCase):
    """完了通知を受けるまで次の確認・送信へ進まないことを検証する。"""

    def setUp(self):
        self._original_dialog = app_module.KakeiboConfirmDialog
        app_module.KakeiboConfirmDialog = _FakeDialog
        _FakeDialog.opened = []
        _FakeDialog.raise_on = None
        _FakeDialog.script = ["submit"] * 3
        self.addCleanup(self._restore)
        self.app = _DeferredApp()
        self.bridge = self.app._integrations

    def _restore(self):
        app_module.KakeiboConfirmDialog = self._original_dialog
        _FakeDialog.opened = []
        _FakeDialog.raise_on = None

    def test_progress_waits_for_each_completion(self):
        self.app.run(three_candidates())

        # 開始直後: 1件目だけが開かれ、送信されている。
        self.assertEqual(_FakeDialog.opened, [1])
        self.assertEqual([r["amount"] for r in self.bridge.posted], [2000])
        self.assertEqual(len(self.bridge.pending), 1)
        self.assertTrue(self.app._kakeibo_sequence_active)

        # POST #1 の完了通知を受けて初めて 2件目へ進む。
        self.bridge.complete(0, True)
        self.assertEqual(_FakeDialog.opened, [1, 2])
        self.assertEqual([r["amount"] for r in self.bridge.posted], [2000, 500])
        self.assertEqual(len(self.bridge.pending), 2)
        self.assertTrue(self.app._kakeibo_sequence_active)

        # POST #2 の完了通知を受けて 3件目へ進む。
        self.bridge.complete(1, True)
        self.assertEqual(_FakeDialog.opened, [1, 2, 3])
        self.assertEqual(
            [r["amount"] for r in self.bridge.posted], [2000, 500, 1200])
        self.assertTrue(self.app._kakeibo_sequence_active)

        # POST #3 完了でシーケンス終了。
        self.bridge.complete(2, True)
        self.assertEqual(_FakeDialog.opened, [1, 2, 3])
        self.assertEqual(len(self.bridge.posted), 3)
        self.assertFalse(self.app._kakeibo_sequence_active)

    def test_observed_five_transactions_wait_for_each_post_callback(self):
        result = build_kakeibo_candidates(
            REAL_FIVE_LLM_TX,
            REAL_FIVE_TEXT,
            today=datetime.date(2026, 8, 25),
        )
        self.assertEqual(result["status"], "ok")
        candidates = result["candidates"]
        self.assertEqual(len(candidates), 5)
        _FakeDialog.script = ["submit"] * 5

        self.app.run(candidates)
        expected_amounts = [1603, 1430, 1100, 525, 3963]
        for index in range(5):
            self.assertEqual(_FakeDialog.opened, list(range(1, index + 2)))
            self.assertEqual(
                [record["amount"] for record in self.bridge.posted],
                expected_amounts[:index + 1],
            )
            self.assertEqual(len(self.bridge.pending), index + 1)
            self.assertTrue(self.app._kakeibo_sequence_active)
            self.bridge.complete(index, True)

        self.assertEqual(_FakeDialog.opened, [1, 2, 3, 4, 5])
        self.assertEqual(len(self.bridge.posted), 5)
        self.assertFalse(self.app._kakeibo_sequence_active)

    def test_amount_before_date_five_transactions_wait_for_each_callback(self):
        result = build_kakeibo_candidates(
            RIGHT_BOUNDARY_FIVE_TX,
            RIGHT_BOUNDARY_FIVE_TEXT,
            today=datetime.date(2026, 8, 25),
        )
        self.assertEqual(result["status"], "ok")
        candidates = result["candidates"]
        self.assertEqual(len(candidates), 5)
        _FakeDialog.script = ["submit"] * 5

        self.app.run(candidates)
        expected_amounts = [2170, 660, 1361, 330, 880]
        for index in range(5):
            self.assertEqual(_FakeDialog.opened, list(range(1, index + 2)))
            self.assertEqual(
                [record["amount"] for record in self.bridge.posted],
                expected_amounts[:index + 1],
            )
            self.assertEqual(len(self.bridge.pending), index + 1)
            payload = validate_kakeibo_payload(self.bridge.posted[index])
            self.assertIsNotNone(payload)
            self.assertEqual(payload["amount"], expected_amounts[index])
            self.assertTrue(self.app._kakeibo_sequence_active)
            self.bridge.complete(index, True)

        self.assertEqual(_FakeDialog.opened, [1, 2, 3, 4, 5])
        self.assertEqual(len(self.bridge.posted), 5)
        self.assertFalse(self.app._kakeibo_sequence_active)

    def test_second_dialog_not_opened_before_first_completion(self):
        self.app.run(three_candidates())
        self.assertNotIn(2, _FakeDialog.opened)
        self.assertEqual(len(self.bridge.posted), 1)

    def test_failed_completion_stops_remaining(self):
        self.app.run(three_candidates())
        self.bridge.complete(0, True)
        self.assertEqual(_FakeDialog.opened, [1, 2])

        self.bridge.complete(1, False)

        self.assertEqual(_FakeDialog.opened, [1, 2])
        self.assertEqual(len(self.bridge.posted), 2)
        self.assertFalse(self.app._kakeibo_sequence_active)

    def test_sequence_stays_active_until_last_completion(self):
        self.app.run(three_candidates())
        self.bridge.complete(0, True)
        self.bridge.complete(1, True)
        self.assertTrue(self.app._kakeibo_sequence_active)
        self.bridge.complete(2, True)
        self.assertFalse(self.app._kakeibo_sequence_active)


class SequenceUiStateTests(unittest.TestCase):
    """POST待ち中は送信ボタンを戻さず、全終了経路で復旧することを検証する。"""

    def setUp(self):
        self._original_dialog = app_module.KakeiboConfirmDialog
        app_module.KakeiboConfirmDialog = _FakeDialog
        _FakeDialog.opened = []
        _FakeDialog.raise_on = None
        _FakeDialog.script = ["submit"] * 3
        self.addCleanup(self._restore)

    def _restore(self):
        app_module.KakeiboConfirmDialog = self._original_dialog
        _FakeDialog.opened = []
        _FakeDialog.raise_on = None

    def _app(self):
        app = _DeferredApp()
        app._btn_send.state = "disabled"
        return app

    def test_send_button_stays_disabled_while_waiting_for_post(self):
        app = self._app()
        app.run(three_candidates())
        self.assertTrue(app._kakeibo_sequence_active)
        self.assertEqual(app._btn_send.state, "disabled")

        app._integrations.complete(0, True)
        self.assertEqual(app._btn_send.state, "disabled")

    def test_send_button_restored_after_all_completions(self):
        app = self._app()
        app.run(three_candidates())
        for index in range(3):
            app._integrations.complete(index, True)
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual(app._btn_send.state, "normal")
        self.assertGreaterEqual(app.status_updates, 1)

    def test_send_button_restored_after_abort(self):
        _FakeDialog.script = ["abort", "submit", "submit"]
        app = self._app()
        app.run(three_candidates())
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual(app._btn_send.state, "normal")

    def test_send_button_restored_after_all_skipped(self):
        _FakeDialog.script = ["skip"] * 3
        app = self._app()
        app.run(three_candidates())
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual(app._btn_send.state, "normal")
        self.assertEqual(app._integrations.posted, [])

    def test_send_button_restored_after_post_failure(self):
        app = self._app()
        app.run(three_candidates())
        app._integrations.complete(0, False)
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual(app._btn_send.state, "normal")

    def test_send_button_restored_after_dialog_exception(self):
        _FakeDialog.raise_on = 1
        app = self._app()
        app.run(three_candidates())
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual(app._btn_send.state, "normal")

    def test_send_button_not_restored_while_other_llm_work_runs(self):
        app = self._app()
        app._ctrl.extra_busy = True
        app.run(three_candidates())
        for index in range(3):
            app._integrations.complete(index, True)
        # シーケンスは終わっているが、別のLLM処理が続いているので戻さない。
        self.assertFalse(app._kakeibo_sequence_active)
        self.assertEqual(app._btn_send.state, "disabled")


if __name__ == "__main__":
    unittest.main()
