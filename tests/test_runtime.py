"""Polling and CLI checks using only fake Telegram transports."""

from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tgearn.__main__ import main
from tgearn.buyback import BuybackRepository
from tgearn.config import BotConfig, ConfigError
from tgearn.db import UpdateCursor
from tgearn.process_lock import process_lock
from tgearn.runtime import run
from tgearn.telegram_api import TelegramError


_SECRET = "private-token-or-raw-input-that-must-not-appear"


class FakeStop:
    def __init__(self):
        self.stopped = False
        self.waits = []

    def is_set(self):
        return self.stopped

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.stopped


class FakeAPI:
    def __init__(self, updates=(), webhook="", startup_errors=None):
        self.events = list(updates)
        self.webhook = webhook
        self.calls = []
        self.startup_errors = startup_errors or {}

    def call(self, method, payload=None):
        self.calls.append((method, payload))
        errors = self.startup_errors.get(method, [])
        if errors:
            raise errors.pop(0)
        if method == "getMe":
            return {"id": 123456, "is_bot": True}
        if method == "getWebhookInfo":
            return {"url": self.webhook}
        if method == "setMyCommands":
            return True
        if method == "getUpdates":
            if not self.events:
                raise KeyboardInterrupt()
            event = self.events.pop(0)
            if isinstance(event, BaseException):
                raise event
            return event
        raise AssertionError("Unexpected fake API method")

    def polling(self):
        return [payload for method, payload in self.calls if method == "getUpdates"]


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "buyback.sqlite3"
        self.config = BotConfig("buyback", _SECRET, frozenset({1}), self.db_path)
        self.messages = []
        self.stop = FakeStop()
        self.handled = []

    def _factory(self, config, api, repository):
        controller = Mock()
        controller.handle.side_effect = lambda update: self.handled.append(update["update_id"])
        return controller

    def _run(self, api, factory=None):
        return run(
            self.config, api=api, controller_factory=factory or self._factory,
            stop_event=self.stop, emit=self.messages.append,
        )

    def _offset(self):
        return UpdateCursor(BuybackRepository(self.db_path).db).read()

    def test_startup_methods_and_long_polling_contract(self):
        api = FakeAPI([[{"update_id": 7, "message": {}}]])
        self.assertEqual(self._run(api), 0)
        self.assertEqual([name for name, _ in api.calls[:3]], ["getMe", "getWebhookInfo", "setMyCommands"])
        first = api.polling()[0]
        self.assertEqual(first["timeout"], 20)
        self.assertEqual(first["allowed_updates"], ["message", "callback_query"])
        self.assertEqual(first["offset"], 0)
        self.assertEqual(api.polling()[1]["offset"], 8)
        self.assertEqual(self.handled, [7])

    def test_resume_uses_saved_offset_and_skips_replayed_duplicate(self):
        self.assertEqual(self._run(FakeAPI([[{"update_id": 11}]])), 0)
        self.handled.clear()
        resumed = FakeAPI([[{"update_id": 11}, {"update_id": 12}]])
        self.assertEqual(self._run(resumed), 0)
        self.assertEqual(resumed.polling()[0]["offset"], 12)
        self.assertEqual(self.handled, [12])
        self.assertEqual(self._offset(), 13)

    def test_unordered_batch_is_processed_without_skipping_earlier_updates(self):
        api = FakeAPI([[{"update_id": 12}, {"update_id": 10}, {"update_id": 11}, {"update_id": 11}]])
        self.assertEqual(self._run(api), 0)
        self.assertEqual(self.handled, [10, 11, 12])
        self.assertEqual(self._offset(), 13)

    def test_bad_message_is_acknowledged_and_does_not_poison_next_update(self):
        def factory(config, api, repository):
            def handle(update):
                self.handled.append(update["update_id"])
                if update["update_id"] == 1:
                    raise ValueError(_SECRET)
            controller = Mock()
            controller.handle.side_effect = handle
            return controller

        self.assertEqual(self._run(FakeAPI([[{"update_id": 1}, {"update_id": 2}]]), factory), 0)
        self.assertEqual(self.handled, [1, 2])
        self.assertEqual(self._offset(), 3)
        self.assertNotIn(_SECRET, "\n".join(self.messages))

    def test_delivery_failure_keeps_committed_result_without_repeating_update(self):
        def factory(config, api, repository):
            repository.seed_registry()
            token = repository.fixtures()[0]["token"]

            def handle(update):
                self.handled.append(update["update_id"])
                quote = repository.check(1, token)
                repository.accept(1, quote["quote_id"])
                raise TelegramError(0)

            controller = Mock()
            controller.handle.side_effect = handle
            return controller

        self.assertEqual(self._run(FakeAPI([[{"update_id": 5}]]), factory), 0)
        self.assertEqual(self._offset(), 6)
        repo = BuybackRepository(self.db_path)
        self.assertEqual(repo.balance(1), 1800)
        self.assertEqual(len(repo.claims(1)), 1)
        self.assertEqual(self.handled, [5])
        self.handled.clear()
        self.assertEqual(self._run(FakeAPI([[{"update_id": 5}]])), 0)
        self.assertEqual(self.handled, [])
        self.assertEqual(repo.balance(1), 1800)

    def test_network_backoff_and_retry_after_are_honored(self):
        api = FakeAPI([TelegramError(0), TelegramError(503), TelegramError(429, retry_after=7), []])
        self.assertEqual(self._run(api), 0)
        self.assertEqual(self.stop.waits, [1, 2, 7])
        self.assertEqual(self.handled, [])

    def test_startup_transient_failure_is_retried(self):
        api = FakeAPI(startup_errors={"getMe": [TelegramError(503)]})
        self.assertEqual(self._run(api), 0)
        self.assertEqual(self.stop.waits, [1])
        self.assertEqual([name for name, _ in api.calls[:2]], ["getMe", "getMe"])

    def test_401_and_409_stop_instead_of_retrying_forever(self):
        for code in (401, 409):
            with self.subTest(code=code):
                api = FakeAPI([TelegramError(code)])
                self.assertEqual(self._run(api), 1)
                self.assertEqual(len(api.polling()), 1)
        self.assertEqual(self.stop.waits, [])

    def test_fatal_delivery_error_still_saves_offset_then_stops(self):
        def factory(config, api, repository):
            controller = Mock()
            controller.handle.side_effect = TelegramError(401)
            return controller
        self.assertEqual(self._run(FakeAPI([[{"update_id": 19}, {"update_id": 20}]]), factory), 1)
        self.assertEqual(self._offset(), 20)

    def test_existing_webhook_stops_without_revealing_url_or_deleting_it(self):
        api = FakeAPI(webhook="https://example.invalid/" + _SECRET)
        factory = Mock()
        self.assertEqual(self._run(api, factory), 1)
        factory.assert_not_called()
        self.assertEqual([name for name, _ in api.calls], ["getMe", "getWebhookInfo"])
        self.assertFalse(self.db_path.exists())
        self.assertNotIn(_SECRET, "\n".join(self.messages))
        self.assertNotIn("https://", "\n".join(self.messages))

    def test_malformed_update_id_stops_safely_without_a_tight_loop(self):
        for bad in ({"message": {"text": _SECRET}}, {"update_id": True}, {"update_id": -1}, {"update_id": 2**63}):
            with self.subTest(kind=type(bad.get("update_id")).__name__):
                api = FakeAPI([[bad]])
                self.assertEqual(self._run(api), 1)
                self.assertEqual(len(api.polling()), 1)
        self.assertEqual(self.handled, [])
        self.assertNotIn(_SECRET, "\n".join(self.messages))

    def test_unexpected_transport_exception_does_not_print_exception_details(self):
        api = FakeAPI([RuntimeError("https://example.invalid/" + _SECRET)])
        self.assertEqual(self._run(api), 1)
        output = "\n".join(self.messages)
        self.assertNotIn(_SECRET, output)
        self.assertNotIn("https://", output)
        self.assertNotIn("Traceback", output)

    def test_second_process_lock_prevents_any_network_call(self):
        api = FakeAPI()
        with process_lock(self.db_path):
            self.assertEqual(self._run(api), 1)
        self.assertEqual(api.calls, [])

    def test_stop_during_retry_exits_cleanly(self):
        self.stop.stopped = True
        api = FakeAPI()
        self.assertEqual(self._run(api), 0)
        self.assertEqual(api.calls, [])

    def test_check_only_never_constructs_api_or_runs_bot(self):
        output = io.StringIO()
        with patch("tgearn.__main__.read_config", return_value=self.config), \
                patch("tgearn.__main__.run") as runner, \
                patch("tgearn.runtime.TelegramAPI") as transport, \
                redirect_stdout(output):
            self.assertEqual(main(["buyback", "--check"]), 0)
        runner.assert_not_called()
        transport.assert_not_called()
        self.assertFalse(self.db_path.exists())
        self.assertNotIn(_SECRET, output.getvalue())

    def test_cli_errors_never_echo_argument_or_exception_secrets(self):
        output = io.StringIO()
        with redirect_stderr(output), self.assertRaises(SystemExit) as raised:
            main([_SECRET])
        self.assertEqual(raised.exception.code, 2)
        self.assertNotIn(_SECRET, output.getvalue())
        output = io.StringIO()
        with patch("tgearn.__main__.read_config", side_effect=ConfigError(_SECRET)), redirect_stderr(output):
            self.assertEqual(main(["store", "--check"]), 2)
        self.assertNotIn(_SECRET, output.getvalue())


if __name__ == "__main__":
    unittest.main()
