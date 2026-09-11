"""Behavioral tests for the closed shop; no Telegram or provider traffic."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sqlite3
import tempfile
import unittest

from tgearn.db import DomainError
from tgearn.store import StoreRepository


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "shop.sqlite3"
        self.shop = StoreRepository(str(self.path))
        self.counter = 0

    def request(self):
        self.counter += 1
        return f"request-{self.counter}"

    def credit(self, amount, user=1):
        result = self.shop.create_topup(user, amount, self.request())
        self.shop.settle_invoice(user, result["invoice"]["id"], "paid")

    def stock(self, sku="A50"):
        return next(product["stock"] for product in self.shop.catalog() if product["sku"] == sku)

    def test_catalog_contains_six_products_and_3000_synthetic_goods(self):
        products = self.shop.catalog()
        self.assertEqual(len(products), 6)
        self.assertEqual(sum(product["stock"] for product in products), 3_000)
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 0})
        self.assertEqual(self.shop.orders(1), [])

    def test_full_balance_issues_raw_good_once_without_invoice(self):
        self.credit(2_000)
        request = self.request()
        first = self.shop.checkout(1, "A50", request)
        self.assertEqual(first["order"]["status"], "issued")
        self.assertTrue(first["order"]["token"].startswith("LAB_RAW_A50_"))
        self.assertNotEqual(first["order"]["id"], first["order"]["token"])
        self.assertIsNone(first["invoice"])
        self.assertEqual(self.shop.balance(1), {"available": 800, "held": 0})
        self.assertEqual(self.shop.checkout(1, "A50", request), first)
        self.assertEqual(self.stock(), 499)
        self.assertEqual(len(self.shop.orders(1)), 1)

    def test_zero_balance_purchase_waits_for_manual_payment(self):
        result = self.shop.checkout(1, "A50", self.request())
        self.assertEqual(result["invoice"]["amount"], 1_200)
        self.assertEqual(result["order"]["status"], "awaiting")
        self.assertIsNone(result["order"]["token"])
        self.assertEqual(self.stock(), 499)
        paid = self.shop.settle_invoice(1, result["invoice"]["id"], "paid")
        self.assertEqual(paid["kind"], "issued")
        self.assertIsNotNone(paid["order"]["token"])
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 0})

    def test_minimum_topup_requires_consent_and_returns_change(self):
        self.credit(1_000)
        request = self.request()
        quote = self.shop.checkout(1, "A50", request)
        self.assertEqual(quote, {
            "kind": "minimum", "sku": "A50", "delta": 200, "amount": 1_000, "after": 800,
        })
        self.assertEqual(self.stock(), 500)
        self.assertEqual(self.shop.orders(1), [])
        self.assertEqual(self.shop.balance(1), {"available": 1_000, "held": 0})
        result = self.shop.checkout(1, "A50", request, allow_minimum_topup=True)
        self.assertEqual(result["order"]["held"], 1_000)
        self.assertEqual(result["order"]["delta"], 200)
        self.assertEqual(result["invoice"]["amount"], 1_000)
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 1_000})
        self.shop.settle_invoice(1, result["invoice"]["id"], "paid")
        self.assertEqual(self.shop.balance(1), {"available": 800, "held": 0})
        again = self.shop.checkout(1, "A50", request, allow_minimum_topup=True)
        self.assertEqual(again["order"]["id"], result["order"]["id"])
        self.assertEqual(again["order"]["status"], "issued")

    def test_changed_balance_cannot_silently_raise_confirmed_topup(self):
        self.credit(1_000)
        request = self.request()
        self.assertEqual(self.shop.checkout(1, "A50", request)["kind"], "minimum")
        self.shop.checkout(1, "T30", self.request())
        with self.assertRaises(DomainError):
            self.shop.checkout(1, "A50", request, allow_minimum_topup=True)
        self.assertEqual(self.stock(), 500)
        self.assertEqual(len(self.shop.orders(1)), 1)
        fresh = self.shop.checkout(1, "A50", self.request())
        self.assertEqual(fresh["invoice"]["amount"], 1_200)

    def test_exact_minimum_difference_needs_no_extra_consent(self):
        self.credit(1_000)
        result = self.shop.checkout(1, "A100", self.request())
        self.assertEqual(result["kind"], "order")
        self.assertEqual(result["invoice"]["amount"], 1_000)
        self.shop.settle_invoice(1, result["invoice"]["id"], "paid")
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 0})

    def test_cancel_and_expire_restore_reserved_balance_and_stock_once(self):
        for terminal in ("cancelled", "expired"):
            with self.subTest(terminal=terminal):
                user = 1 if terminal == "cancelled" else 2
                self.credit(1_000, user=user)
                before = self.stock()
                result = self.shop.checkout(user, "A50", self.request(), allow_minimum_topup=True)
                settled = self.shop.settle_invoice(user, result["invoice"]["id"], terminal)
                self.assertEqual(settled["kind"], terminal)
                self.assertEqual(settled["order"]["status"], terminal)
                self.assertIsNone(settled["order"]["token"])
                self.assertEqual(self.shop.balance(user), {"available": 1_000, "held": 0})
                self.assertEqual(self.stock(), before)
                repeated = self.shop.settle_invoice(user, result["invoice"]["id"], terminal)
                self.assertEqual(repeated["kind"], "duplicate")
                self.assertEqual(self.shop.balance(user), {"available": 1_000, "held": 0})
                self.assertEqual(self.stock(), before)

    def test_late_paid_event_goes_to_review_without_credit_or_delivery(self):
        self.credit(1_000)
        purchase = self.shop.checkout(1, "A50", self.request(), allow_minimum_topup=True)
        invoice_id = purchase["invoice"]["id"]
        self.shop.settle_invoice(1, invoice_id, "cancelled")
        # Another user's order may already own the released unit.
        other = self.shop.checkout(2, "A50", self.request())
        other_paid = self.shop.settle_invoice(2, other["invoice"]["id"], "paid")
        late = self.shop.settle_invoice(1, invoice_id, "paid")
        self.assertEqual(late["kind"], "review")
        self.assertEqual(late["invoice"]["status"], "review")
        self.assertTrue(late["order"]["review"])
        self.assertEqual(late["order"]["status"], "cancelled")
        self.assertIsNone(late["order"]["token"])
        self.assertEqual(self.shop.balance(1), {"available": 1_000, "held": 0})
        self.assertEqual(self.shop.orders(2)[0]["token"], other_paid["order"]["token"])
        self.assertEqual(self.stock(), 499)
        self.assertEqual(self.shop.settle_invoice(1, invoice_id, "paid")["kind"], "duplicate")

    def test_late_topup_is_reviewed_not_credited(self):
        result = self.shop.create_topup(1, 2_000, self.request())
        invoice_id = result["invoice"]["id"]
        self.shop.settle_invoice(1, invoice_id, "expired")
        late = self.shop.settle_invoice(1, invoice_id, "paid")
        self.assertEqual(late["kind"], "review")
        self.assertIsNone(late["order"])
        self.assertEqual(self.shop.settle_invoice(1, invoice_id, "paid")["kind"], "duplicate")
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 0})

    def test_expired_purchase_stays_expired_after_repeated_late_payment(self):
        purchase = self.shop.checkout(1, "A50", self.request())
        invoice_id = purchase["invoice"]["id"]
        self.shop.settle_invoice(1, invoice_id, "expired")
        self.assertEqual(self.shop.settle_invoice(1, invoice_id, "paid")["kind"], "review")
        repeated = self.shop.settle_invoice(1, invoice_id, "paid")
        self.assertEqual(repeated["kind"], "duplicate")
        self.assertEqual(repeated["invoice"]["status"], "review")
        self.assertEqual(repeated["order"]["status"], "expired")
        self.assertTrue(repeated["order"]["review"])
        self.assertIsNone(repeated["order"]["token"])
        self.assertEqual(self.stock(), 500)
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 0})

    def test_paid_invoice_cannot_be_reversed_or_credited_twice(self):
        result = self.shop.create_topup(1, 2_000, self.request())
        invoice_id = result["invoice"]["id"]
        self.assertEqual(self.shop.settle_invoice(1, invoice_id, "paid")["kind"], "credited")
        for event in ("paid", "cancelled", "expired"):
            self.assertEqual(self.shop.settle_invoice(1, invoice_id, event)["kind"], "duplicate")
        self.assertEqual(self.shop.invoice(1, invoice_id)["status"], "paid")
        self.assertEqual(self.shop.balance(1), {"available": 2_000, "held": 0})

    def test_only_one_pending_invoice_and_failed_action_has_no_side_effects(self):
        first = self.shop.checkout(1, "A50", self.request())
        with self.assertRaises(DomainError):
            self.shop.checkout(1, "A100", self.request())
        with self.assertRaises(DomainError):
            self.shop.create_topup(1, 1_000, self.request())
        self.assertEqual(self.stock("A100"), 500)
        self.assertEqual(len(self.shop.orders(1)), 1)
        self.assertEqual(len(self.shop.invoices(1)), 1)
        self.shop.settle_invoice(1, first["invoice"]["id"], "cancelled")
        second = self.shop.create_topup(1, 1_000, self.request())
        self.assertEqual(second["invoice"]["status"], "pending")

    def test_invoice_and_request_ownership_are_enforced(self):
        request = self.request()
        purchase = self.shop.checkout(1, "A50", request)
        invoice_id = purchase["invoice"]["id"]
        with self.assertRaises(DomainError):
            self.shop.invoice(2, invoice_id)
        with self.assertRaises(DomainError):
            self.shop.settle_invoice(2, invoice_id, "paid")
        with self.assertRaises(DomainError):
            self.shop.checkout(2, "A50", request)
        with self.assertRaises(DomainError):
            self.shop.checkout(1, "A100", request)
        with self.assertRaises(DomainError):
            self.shop.create_topup(1, 1_200, request)
        self.assertEqual(self.shop.orders(2), [])
        self.assertEqual(self.shop.invoices(2), [])
        self.assertEqual(self.shop.invoice(1, invoice_id)["status"], "pending")

    def test_topup_request_reuse_checks_amount_and_user(self):
        request = self.request()
        first = self.shop.create_topup(1, 1_000, request)
        self.assertEqual(self.shop.create_topup(1, 1_000, request), first)
        with self.assertRaises(DomainError):
            self.shop.create_topup(1, 2_000, request)
        with self.assertRaises(DomainError):
            self.shop.create_topup(2, 1_000, request)
        self.assertEqual(len(self.shop.invoices(1)), 1)

    def test_restart_preserves_issued_and_reserved_inventory(self):
        self.credit(2_000)
        issued = self.shop.checkout(1, "A50", self.request())
        awaiting = self.shop.checkout(2, "A50", self.request())
        restarted = StoreRepository(str(self.path))
        self.assertEqual(next(p["stock"] for p in restarted.catalog() if p["sku"] == "A50"), 498)
        self.assertEqual(restarted.orders(1)[0]["token"], issued["order"]["token"])
        self.assertEqual(restarted.balance(1), {"available": 800, "held": 0})
        self.assertIsNone(restarted.orders(2)[0]["token"])
        paid = restarted.settle_invoice(2, awaiting["invoice"]["id"], "paid")
        self.assertNotEqual(paid["order"]["token"], issued["order"]["token"])

    def test_parallel_repeated_buttons_create_only_one_order(self):
        request = self.request()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.shop.checkout(1, "A50", request), range(8)))
        self.assertEqual(len({result["order"]["id"] for result in results}), 1)
        self.assertEqual(len({result["invoice"]["id"] for result in results}), 1)
        self.assertEqual(self.stock(), 499)
        self.assertEqual(len(self.shop.orders(1)), 1)

    def test_parallel_paid_events_issue_only_once(self):
        self.credit(1_000)
        purchase = self.shop.checkout(1, "A50", self.request(), allow_minimum_topup=True)
        invoice_id = purchase["invoice"]["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.shop.settle_invoice(1, invoice_id, "paid"), range(8)))
        self.assertEqual(sum(result["kind"] == "issued" for result in results), 1)
        self.assertEqual(sum(result["kind"] == "duplicate" for result in results), 7)
        self.assertEqual(len({result["order"]["token"] for result in results}), 1)
        self.assertEqual(self.shop.balance(1), {"available": 800, "held": 0})
        self.assertEqual(self.stock(), 499)

    def test_parallel_users_receive_different_goods(self):
        def buy(user):
            result = self.shop.checkout(user, "A50", f"parallel-{user}")
            return self.shop.settle_invoice(user, result["invoice"]["id"], "paid")["order"]

        with ThreadPoolExecutor(max_workers=8) as pool:
            orders = list(pool.map(buy, range(1, 9)))
        self.assertEqual(len({order["token"] for order in orders}), 8)
        self.assertEqual(self.stock(), 492)

    def test_storage_failure_rolls_back_delivery_and_balance_together(self):
        self.credit(1_000)
        purchase = self.shop.checkout(1, "A50", self.request(), allow_minimum_topup=True)
        invoice_id = purchase["invoice"]["id"]
        # Simulate a disk-level write rejection at the final invoice update.
        with self.shop.db.transaction() as connection:
            connection.execute(
                "CREATE TRIGGER reject_invoice_write BEFORE UPDATE ON store_invoices "
                "BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.shop.settle_invoice(1, invoice_id, "paid")
        self.assertEqual(self.shop.invoice(1, invoice_id)["status"], "pending")
        self.assertEqual(self.shop.orders(1)[0]["status"], "awaiting")
        self.assertIsNone(self.shop.orders(1)[0]["token"])
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 1_000})
        self.assertEqual(self.stock(), 499)
        with self.shop.db.transaction() as connection:
            connection.execute("DROP TRIGGER reject_invoice_write")
        retried = self.shop.settle_invoice(1, invoice_id, "paid")
        self.assertEqual(retried["kind"], "issued")
        self.assertEqual(self.shop.balance(1), {"available": 800, "held": 0})

    def test_out_of_stock_does_not_spend_or_create_an_invoice(self):
        self.credit(502_000)
        for index in range(500):
            self.shop.checkout(1, "T30", f"exhaust-{index}")
        self.assertEqual(self.stock("T30"), 0)
        before = self.shop.balance(1)
        previous_invoices = self.shop.invoices(1)
        with self.assertRaises(DomainError):
            self.shop.checkout(1, "T30", "exhausted-next")
        self.assertEqual(self.shop.balance(1), before)
        self.assertEqual(self.shop.invoices(1), previous_invoices)
        # Replaying an existing order must still work after stock runs out.
        replay = self.shop.checkout(1, "T30", "exhaust-0")
        self.assertEqual(replay["order"]["status"], "issued")

    def test_pagination_is_stable_and_separates_users(self):
        self.credit(4_000)
        ids = [self.shop.checkout(1, "T30", self.request())["order"]["id"] for _ in range(4)]
        self.assertEqual([row["id"] for row in self.shop.orders(1, 2, 0)], ids[::-1][:2])
        self.assertEqual([row["id"] for row in self.shop.orders(1, 2, 2)], ids[::-1][2:])
        self.assertEqual(self.shop.orders(1, 2, 4), [])
        self.assertEqual(self.shop.orders(2), [])

    def test_invalid_inputs_cannot_mutate_money_or_stock(self):
        for amount in (0, -1, 999, 1_000.0, True, "1000"):
            with self.subTest(amount=amount), self.assertRaises(DomainError):
                self.shop.create_topup(1, amount, self.request())
        with self.assertRaises(DomainError):
            self.shop.checkout(1, "' OR 1=1 --", self.request())
        with self.assertRaises(DomainError):
            self.shop.checkout(True, "A50", self.request())
        with self.assertRaises(DomainError):
            self.shop.checkout(1, "A50", "")
        with self.assertRaises(DomainError):
            self.shop.orders(1, -1)
        self.assertEqual(self.shop.balance(1), {"available": 0, "held": 0})
        self.assertEqual(sum(item["stock"] for item in self.shop.catalog()), 3_000)
        self.assertEqual(self.shop.orders(1), [])


if __name__ == "__main__":
    unittest.main()
