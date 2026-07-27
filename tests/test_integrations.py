import os
import unittest
import urllib.request
from unittest.mock import Mock, patch

import integrations


class LocalApiProxyTests(unittest.TestCase):
    def test_open_local_api_ignores_environment_proxies(self):
        request = urllib.request.Request("http://localhost:8765/api/test")
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


class LocalApiUrlAllowlistTests(unittest.TestCase):
    def test_allows_default_bridge_ports_on_local_hosts(self):
        for url in (
            "http://localhost:8765/api/kakeibo/record",
            "http://localhost:8766/api/health/record",
            "http://127.0.0.1:8765/api/kakeibo/record",
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
            "https://localhost:8765/api/kakeibo/record",
            "http://example.invalid:8765/api/kakeibo/record",
            "http://192.168.1.10:8765/api/kakeibo/record",
            "http://localhost:8765@example.invalid/api/kakeibo/record",
            "http://localhost:notaport/api/kakeibo/record",
        ):
            with self.subTest(url=url):
                self.assertFalse(integrations.is_allowed_local_api_url(url))


if __name__ == "__main__":
    unittest.main()
