import tempfile
import unittest
from pathlib import Path

from tgearn.bots import make_controller
from tgearn.buyback import BuybackRepository
from tgearn.config import BotConfig
from tgearn.store import StoreRepository
from tgearn.telegram_api import TelegramError


class FakeTelegram:
    def __init__(self):
        self.calls = []
        self.fail_edit = None

    def call(self, method, payload=None):
        self.calls.append((method, payload))
        if method == "editMessageText" and self.fail_edit:
            raise self.fail_edit
        return {"message_id": len(self.calls)}

    @property
    def screen(self):
        return next(payload for method, payload in reversed(self.calls)
                    if method in {"sendMessage", "editMessageText"})

    def callback(self, prefix):
        return next(button["callback_data"] for row in self.screen["reply_markup"]["inline_keyboard"]
                    for button in row if button["callback_data"].startswith(prefix))


class BotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = StoreRepository(root / "store.sqlite3")
        self.buyer = BuybackRepository(root / "buyback.sqlite3")
        self.shop_api, self.buyer_api = FakeTelegram(), FakeTelegram()
        self.shop = make_controller(BotConfig("store", "unused", frozenset({11, 22}), root / "store.sqlite3"),
                                    self.shop_api, self.store)
        self.buyback = make_controller(BotConfig("buyback", "unused", frozenset({11, 22}), root / "buyback.sqlite3"),
                                       self.buyer_api, self.buyer)
        self.update_id = 0

    def message(self, controller, text, user=11, chat_type="private"):
        self.update_id += 1
        update = {"update_id": self.update_id, "message": {
            "message_id": self.update_id, "from": {"id": user, "is_bot": False},
            "chat": {"id": user, "type": chat_type}, "text": text,
        }}
        controller.handle(update)
        return update

    def click(self, controller, data, user=11, chat_id=None):
        self.update_id += 1
        controller.handle({"update_id": self.update_id, "callback_query": {
            "id": str(self.update_id), "from": {"id": user}, "data": data,
            "message": {"message_id": 1, "chat": {"id": user if chat_id is None else chat_id,
                                                     "type": "private"}},
        }})

    def test_store_purchase_and_replayed_buttons_issue_once(self):
        self.message(self.shop, "/start")
        self.click(self.shop, "item:A50")
        purchase = self.shop_api.callback("buy:")
        self.click(self.shop, purchase)
        self.assertNotIn("LAB_RAW_", self.shop_api.screen["text"])
        payment = self.shop_api.callback("event:paid:")
        self.click(self.shop, payment)
        token = self.store.orders(11)[0]["token"]
        self.assertIn(token, self.shop_api.screen["text"])
        self.click(self.shop, payment)
        self.click(self.shop, purchase)
        self.assertIn(token, self.shop_api.screen["text"])
        self.assertEqual(len(self.store.orders(11)), 1)
        self.assertEqual(self.store.catalog()[0]["stock"], 499)

    def test_mixed_payment_requires_visible_minimum_consent(self):
        self.message(self.shop, "/topup 1000")
        self.click(self.shop, self.shop_api.callback("event:paid:"))
        self.click(self.shop, "item:A50")
        self.click(self.shop, self.shop_api.callback("buy:"))
        text = self.shop_api.screen["text"]
        self.assertIn("200 ₽", text)
        self.assertIn("800 ₽", text)
        self.assertEqual(self.store.orders(11), [])
        self.click(self.shop, self.shop_api.callback("minimum:"))
        self.assertEqual(self.store.balance(11), {"available": 0, "held": 1000})
        self.click(self.shop, self.shop_api.callback("event:paid:"))
        self.assertEqual(self.store.balance(11), {"available": 800, "held": 0})
        self.assertIn("LAB_RAW_", self.shop_api.screen["text"])

    def test_topup_message_replay_uses_same_request(self):
        update = self.message(self.shop, "/topup 1500")
        self.shop.handle(update)
        self.assertEqual(len(self.store.invoices(11)), 1)
        self.click(self.shop, self.shop_api.callback("event:paid:"))
        self.shop.handle(update)
        self.assertEqual(self.store.balance(11)["available"], 1500)
        self.assertEqual(len(self.store.invoices(11)), 1)

    def test_late_payment_does_not_issue_cancelled_order(self):
        self.click(self.shop, "item:A50")
        self.click(self.shop, self.shop_api.callback("buy:"))
        self.click(self.shop, self.shop_api.callback("event:cancelled:"))
        self.click(self.shop, self.shop_api.callback("event:paid:"))
        self.assertIn("Нужен разбор", self.shop_api.screen["text"])
        self.assertIsNone(self.store.orders(11)[0]["token"])
        self.assertEqual(self.store.catalog()[0]["stock"], 500)

    def test_buyer_accepts_raw_sample_without_order(self):
        self.click(self.buyback, "registry:seed")
        self.message(self.buyback, self.buyer.fixtures()[0]["token"])
        self.assertIn("Пример найден", self.buyer_api.screen["text"])
        accept = self.buyer_api.callback("accept:")
        self.click(self.buyback, accept)
        self.click(self.buyback, accept)
        self.assertEqual(self.buyer.balance(11), 1800)
        self.assertEqual(self.store.orders(11), [])
        self.assertEqual(self.store.balance(11)["available"], 0)

    def test_empty_registry_is_not_external_invalidity_claim(self):
        self.message(self.buyback, self.buyer.fixtures()[0]["token"])
        self.assertIn("В базе этого бота", self.buyer_api.screen["text"])
        self.assertEqual(self.buyer.balance(11), 0)
        self.assertFalse(any(b["callback_data"].startswith("accept:")
                             for row in self.buyer_api.screen["reply_markup"]["inline_keyboard"] for b in row))

    def test_raw_secret_rejected_without_echo_or_storage(self):
        raw = "sk-" + "not-a-real-key-but-sensitive-input" * 2
        self.message(self.buyback, raw)
        self.assertIn("только учебный токен", self.buyer_api.screen["text"])
        self.assertNotIn(raw, str(self.buyer_api.calls))
        self.assertNotIn(raw.encode(), self.buyer.db.path.read_bytes())
        self.assertEqual(self.buyer.claims(11), [])

    def test_unknown_user_cannot_seed_or_create_payments(self):
        self.click(self.buyback, "registry:seed", user=99)
        self.message(self.shop, "/topup 1000", user=99)
        self.assertEqual(self.buyer.registry_state()["count"], 0)
        self.assertEqual(self.store.invoices(99), [])
        self.assertIn("закрытая демонстрация", self.shop_api.screen["text"])

    def test_id_bootstrap_discloses_only_requesters_own_id(self):
        self.message(self.shop, "/id", user=99)
        self.assertIn("<code>99</code>", self.shop_api.screen["text"])
        self.assertEqual(self.shop_api.screen["reply_markup"]["inline_keyboard"], [])
        self.assertEqual(self.store.invoices(99), [])

    def test_groups_and_mismatched_callback_chat_are_ignored(self):
        self.message(self.shop, "/topup 1000", chat_type="group")
        self.click(self.buyback, "registry:seed", user=11, chat_id=22)
        self.assertEqual(self.shop_api.calls, [])
        self.assertEqual(self.buyer_api.calls, [])
        self.assertEqual(self.buyer.registry_state()["count"], 0)

    def test_other_tester_cannot_read_invoice_or_accept_quote(self):
        self.message(self.shop, "/topup 1500")
        invoice = self.store.invoices(11)[0]
        self.click(self.shop, f"invoice:{invoice['id']}", user=22)
        self.assertNotIn(invoice["id"][:8], self.shop_api.screen["text"])
        self.buyer.seed_registry()
        self.message(self.buyback, self.buyer.fixtures()[0]["token"])
        self.click(self.buyback, self.buyer_api.callback("accept:"), user=22)
        self.assertEqual(self.buyer.balance(22), 0)

    def test_unknown_callbacks_and_malformed_updates_have_no_effect(self):
        for update in [None, [], {}, {"message": None}, {"callback_query": {"message": None}}]:
            self.shop.handle(update)
        self.click(self.shop, "event:paid:bad-id")
        self.click(self.buyback, "registry:unexpected")
        self.assertEqual(self.store.invoices(11), [])
        self.assertEqual(self.buyer.registry_state()["count"], 0)

    def test_edit_failure_falls_back_but_not_modified_does_not_duplicate(self):
        self.shop_api.fail_edit = TelegramError(400)
        self.click(self.shop, "home")
        self.assertEqual(self.shop_api.calls[-1][0], "sendMessage")
        self.shop_api.calls.clear()
        self.shop_api.fail_edit = TelegramError(400, not_modified=True)
        self.click(self.shop, "home")
        self.assertFalse(any(method == "sendMessage" for method, _ in self.shop_api.calls))

    def test_menus_fit_telegram_limits_and_use_no_payment_links(self):
        for controller, api, actions in [
            (self.shop, self.shop_api, ["home", "catalog", "wallet", "orders", "help", "invoices", "item:A50"]),
            (self.buyback, self.buyer_api, ["home", "check", "wallet", "claims", "fixtures", "help", "payout"]),
        ]:
            for action in actions:
                self.click(controller, action)
                self.assertLess(len(api.screen["text"]), 4096)
                for row in api.screen["reply_markup"]["inline_keyboard"]:
                    for item in row:
                        self.assertLessEqual(len(item["callback_data"].encode()), 64)
                        self.assertNotIn("url", item)


if __name__ == "__main__":
    unittest.main()
