from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

from tgearn.config import ConfigError, load_dotenv, read_config
from tgearn.telegram_api import TelegramAPI, TelegramError


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.token = "12345:" + "x" * 35
        self.env = patch.dict(os.environ, {"STORE_BOT_TOKEN": self.token}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_empty_testers_fail_closed_and_token_is_not_in_repr(self):
        config = read_config("store", self.root)
        self.assertEqual(config.allowed_ids, frozenset())
        self.assertNotIn(self.token, repr(config))

    def test_two_bots_cannot_share_token_or_resolved_database(self):
        os.environ["BUYBACK_BOT_TOKEN"] = self.token
        with self.assertRaises(ConfigError):
            read_config("store", self.root)
        del os.environ["BUYBACK_BOT_TOKEN"]
        os.environ["STORE_DB"] = ".data/../same.sqlite3"
        os.environ["BUYBACK_DB"] = "same.sqlite3"
        with self.assertRaises(ConfigError):
            read_config("store", self.root)

    def test_bad_ids_and_missing_token_are_safe_errors(self):
        os.environ["STORE_TESTER_IDS"] = "11,not-a-user"
        with self.assertRaises(ConfigError) as error:
            read_config("store", self.root)
        self.assertNotIn(self.token, str(error.exception))
        self.assertNotIn("not-a-user", str(error.exception))
        os.environ["STORE_BOT_TOKEN"] = "private-bad-value"
        with self.assertRaises(ConfigError) as error:
            read_config("store", self.root)
        self.assertNotIn("private-bad-value", str(error.exception))

    def test_dotenv_is_literal_and_environment_takes_precedence(self):
        path = self.root / ".env"
        path.write_text("STORE_BOT_TOKEN=ignored\nDEMO_VALUE='$(do-not-execute)'\n", encoding="utf-8")
        load_dotenv(path)
        self.assertEqual(os.environ["STORE_BOT_TOKEN"], self.token)
        self.assertEqual(os.environ["DEMO_VALUE"], "$(do-not-execute)")


class TransportTests(unittest.TestCase):
    def test_network_exception_does_not_expose_token_or_url(self):
        api = TelegramAPI("secret-test-value")
        output = io.StringIO()
        with patch("tgearn.telegram_api.urlopen", side_effect=URLError("secret-test-value")):
            with redirect_stdout(output), self.assertRaises(TelegramError) as error:
                api.call("getMe")
        self.assertNotIn("secret-test-value", str(error.exception) + output.getvalue())

    def test_malformed_error_and_retry_delay_are_sanitized(self):
        bodies = [
            {"ok": False, "error_code": "secret-test-value", "parameters": []},
            {"ok": False, "error_code": 429, "parameters": {"retry_after": 10000}},
        ]
        for body in bodies:
            with patch("tgearn.telegram_api.urlopen", return_value=io.BytesIO(json.dumps(body).encode())):
                with self.assertRaises(TelegramError) as error:
                    TelegramAPI("unused").call("getMe")
            self.assertNotIn("secret-test-value", str(error.exception))
            self.assertLessEqual(error.exception.retry_after, 300)

    def test_transport_has_no_payment_method(self):
        with self.assertRaises(ValueError):
            TelegramAPI("unused").call("sendInvoice")


if __name__ == "__main__":
    unittest.main()
