import json
import os
import unittest
import urllib.error
import urllib.request
from unittest.mock import MagicMock, Mock, patch

import integrations


class LocalApiProxyTests(unittest.TestCase):
    def test_open_local_api_ignores_environment_proxies(self):
        request = urllib.request.Request("http://localhost:8767/api/test")
        response = object()
        opener = Mock()
        opener.open.return_value = response

        proxy_env = {
            "HTTP_PROXY": "http://proxy.invalid:8080",
            "HTTPS_PROXY": "http://proxy.invalid:8080",
        }
        with patch.dict(os.environ, proxy_env, clear=False), patch(
            "integrations.urllib.request.build_opener", return_value=opener
        ) as build_opener:
            actual = integrations._open_local_api(request, timeout=5)

        self.assertIs(actual, response)
        handler = build_opener.call_args.args[0]
        self.assertIsInstance(handler, urllib.request.ProxyHandler)
        self.assertEqual(handler.proxies, {})
        opener.open.assert_called_once_with(request, timeout=5)

    def test_open_local_api_disables_redirects(self):
        request = urllib.request.Request("http://localhost:8767/api/test")
        opener = Mock()
        with patch(
            "integrations.urllib.request.build_opener", return_value=opener
        ) as build_opener:
            integrations._open_local_api(request, timeout=5)
        self.assertTrue(any(
            isinstance(handler, integrations._NoRedirectHandler)
            for handler in build_opener.call_args.args
        ))


class LocalApiUrlAllowlistTests(unittest.TestCase):
    def test_allows_default_bridge_ports_on_local_hosts(self):
        for url in (
            "http://localhost:8767/api/kakeibo/record",
            "http://localhost:8766/api/health/record",
            "http://127.0.0.1:8767/api/kakeibo/record",
            "http://[::1]:8766/api/health/record",
        ):
            with self.subTest(url=url):
                self.assertTrue(integrations.is_allowed_local_api_url(url))

    def test_rejects_other_ports_on_local_hosts(self):
        """ローカルホストでも、既定ブリッジ以外のポートへは送信しない。"""
        for url in (
            "http://localhost:9999/api/health/record",
            "http://localhost/api/health/record",
            "http://localhost:80/api/health/record",
            "http://127.0.0.1:8501/api/health/record",
        ):
            with self.subTest(url=url):
                self.assertFalse(integrations.is_allowed_local_api_url(url))

    def test_rejects_non_local_hosts_and_schemes(self):
        for url in (
            "https://localhost:8767/api/kakeibo/record",
            "http://example.invalid:8767/api/kakeibo/record",
            "http://192.168.1.10:8767/api/kakeibo/record",
            "http://localhost:8767@example.invalid/api/kakeibo/record",
            "http://user@localhost:8767/api/kakeibo/record",
            "http://user:pass@localhost:8767/api/kakeibo/record",
            "http://localhost:notaport/api/kakeibo/record",
        ):
            with self.subTest(url=url):
                self.assertFalse(integrations.is_allowed_local_api_url(url))

    def test_json_response_size_is_limited(self):
        response = Mock()
        response.read.return_value = b"x" * 11
        with self.assertRaises(ValueError):
            integrations._read_json_response(response, max_bytes=10)


class BiologCompletionDateTests(unittest.TestCase):
    def _bridge(self):
        writes = []
        root = Mock()
        root.after.side_effect = lambda _delay, callback: callback()
        bridge = integrations.IntegrationBridge(
            root, lambda text, tag: writes.append((text, tag))
        )

        def run_immediately(_label, target):
            target()
            return True

        bridge._start_worker = run_immediately
        return bridge, writes

    @staticmethod
    def _response(payload):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = json.dumps(payload).encode("utf-8")
        return response

    @staticmethod
    def _posted_payload(open_api):
        request = open_api.call_args.args[0]
        return json.loads(request.data.decode("utf-8"))

    def test_missing_date_is_finalized_as_jst_date_before_single_post(self):
        bridge, writes = self._bridge()
        response = self._response({"message": "登録完了", "id": 1})
        with patch(
            "integrations._biolog_jst_today", return_value="2026-08-25"
        ), patch("integrations._open_local_api", return_value=response) as open_api:
            bridge._send_to_biolog_api({
                "memo": "体脂肪率16.2%はサンプルです。"
            })

        open_api.assert_called_once()
        self.assertEqual(self._posted_payload(open_api)["date"], "2026-08-25")
        self.assertIn(("✅ Biolog記録完了: 2026-08-25\n", "health_ok"), writes)

    def test_explicit_date_is_preserved_for_post_and_display(self):
        bridge, writes = self._bridge()
        response = self._response({"message": "登録完了", "id": 1})
        with patch("integrations._open_local_api", return_value=response) as open_api:
            bridge._send_to_biolog_api({
                "date": "2026-08-20", "memo": "テスト"
            })

        self.assertEqual(self._posted_payload(open_api)["date"], "2026-08-20")
        self.assertIn(("✅ Biolog記録完了: 2026-08-20\n", "health_ok"), writes)

    def test_valid_response_date_has_display_priority_without_rewriting_post(self):
        bridge, writes = self._bridge()
        response = self._response({
            "message": "登録完了", "id": 1, "date": "2026-08-24"
        })
        with patch("integrations._open_local_api", return_value=response) as open_api:
            bridge._send_to_biolog_api({
                "date": "2026-08-25", "memo": "テスト"
            })

        self.assertEqual(self._posted_payload(open_api)["date"], "2026-08-25")
        self.assertIn(("✅ Biolog記録完了: 2026-08-24\n", "health_ok"), writes)

    def test_response_without_date_falls_back_to_confirmed_post_date(self):
        bridge, writes = self._bridge()
        response = self._response({"message": "登録完了", "id": 1})
        with patch("integrations._open_local_api", return_value=response):
            bridge._send_to_biolog_api({
                "date": "2026-08-20", "memo": "テスト"
            })

        self.assertIn(("✅ Biolog記録完了: 2026-08-20\n", "health_ok"), writes)

    def test_api_failure_does_not_show_completion(self):
        bridge, writes = self._bridge()
        with patch(
            "integrations._open_local_api",
            side_effect=urllib.error.URLError("offline"),
        ) as open_api:
            bridge._send_to_biolog_api({
                "date": "2026-08-20", "memo": "テスト"
            })

        open_api.assert_called_once()
        self.assertFalse(any("Biolog記録完了" in text for text, _tag in writes))
        self.assertTrue(any("接続できません" in text for text, _tag in writes))


if __name__ == "__main__":
    unittest.main()
