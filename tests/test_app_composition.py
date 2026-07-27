import unittest
from unittest.mock import patch

from history_crypto import HistoryCryptoError
from app_composition import create_app_deps


class AppCompositionTests(unittest.TestCase):
    def test_session_store_failure_stops_resource_monitor(self):
        monitor = type("Monitor", (), {
            "stop": lambda self: setattr(self, "stopped", True),
        })()
        with (
            patch("app_composition.ResourceMonitor", return_value=monitor),
            patch("app_composition.WhisperPool"),
            patch(
                "app_composition.SessionStore",
                side_effect=HistoryCryptoError("DPAPI unavailable"),
            ),
        ):
            with self.assertRaises(HistoryCryptoError):
                create_app_deps("unused")
        self.assertTrue(monitor.stopped)


if __name__ == "__main__":
    unittest.main()
