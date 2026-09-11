"""Meaningful checks for the isolated synthetic acceptance repository."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from tgearn.buyback import BuybackRepository


class BuybackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "buyback.sqlite3"
        self.repo = BuybackRepository(self.db_path)
        self.token = self.repo.fixtures()[0]["token"]

    def _quote(self, user_id=1, token=None):
        self.repo.seed_registry()
        result = self.repo.check(user_id, token or self.token)
        self.assertEqual(result["kind"], "matched")
        return result["quote_id"]

    def _row_count(self, table):
        # Names only come from test literals, never external inputs.
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_starts_empty_and_independent_without_shop(self):
        self.assertEqual(self.repo.registry_state(), {"count": 0, "available": True})
        self.assertEqual(self.repo.balance(1), 0)
        self.assertEqual(self.repo.claims(1), [])
        self.assertEqual(self.repo.check(1, self.token), {"kind": "notfound"})
        self.assertEqual(self.repo.offers()[0]["amount"], 1800)
        self.assertEqual(self.repo.offers()[1]["amount"], 3000)
        self.assertFalse((Path(self.tmp.name) / "shop.sqlite3").exists())

    def test_external_sample_acceptance_needs_no_purchase_or_order(self):
        quote_id = self._quote(user_id=4821)
        self.assertRegex(quote_id, r"^[a-f0-9]{32}$")
        accepted = self.repo.accept(4821, quote_id)
        self.assertEqual(accepted["kind"], "accepted")
        self.assertEqual(accepted["claim"]["amount"], 1800)
        self.assertEqual(accepted["claim"]["status"], "accepted")
        self.assertEqual(self.repo.balance(4821), 1800)
        self.assertEqual(self.repo.balance(1), 0)

    def test_registry_seed_is_idempotent_and_does_not_store_raw(self):
        self.assertEqual(self.repo.seed_registry(), 3)
        self.assertEqual(self.repo.seed_registry(), 0)
        self.assertEqual(self.repo.registry_state()["count"], 3)
        self.repo.accept(1, self._quote())
        for path in Path(self.tmp.name).iterdir():
            if path.is_file():
                contents = path.read_bytes()
                for fixture in self.repo.fixtures():
                    self.assertNotIn(fixture["token"].encode(), contents)
        stored = self.repo.claims(1)[0]
        self.assertNotIn("token", stored)
        self.assertEqual(stored["masked_token"], "LAB_RAW_…5Bv8")

    def test_lookup_trims_edges_but_is_case_sensitive(self):
        self.repo.seed_registry()
        self.assertEqual(self.repo.check(1, " \n" + self.token + "\t")["kind"], "matched")
        other_case = "LAB_RAW_" + self.token[8:].swapcase()
        self.assertEqual(self.repo.check(1, other_case), {"kind": "notfound"})
        self.assertEqual(self.repo.check(1, self.token.lower()), {"kind": "format"})

    def test_unknown_synthetic_input_never_creates_quote_or_claim(self):
        self.repo.seed_registry()
        unknown = "LAB_RAW_unknown_99999999"
        self.assertEqual(self.repo.check(1, unknown), {"kind": "notfound"})
        self.assertEqual(self._row_count("buyback_quotes"), 0)
        self.assertEqual(self._row_count("buyback_claims"), 0)
        self.assertNotIn(unknown.encode(), self.db_path.read_bytes())

    def test_empty_and_real_key_shaped_inputs_rejected_without_storage(self):
        self.repo.seed_registry()
        self.assertEqual(self.repo.check(1, " \n\t"), {"kind": "empty"})
        inputs = (
            "sk-proj-this-is-not-a-real-key-do-not-accept",
            "sk-" + "x" * 48,
            "LAB_RAW_sk-proj-this-is-not-a-real-key",
            self.token + "\nextra",
            self.token + "\x00",
            "LAB_RAW_" + "x" * 300,
            None,
        )
        for invalid in inputs:
            with self.subTest(input_type=type(invalid).__name__):
                self.assertEqual(self.repo.check(1, invalid), {"kind": "format"})
        self.assertEqual(self._row_count("buyback_quotes"), 0)
        self.assertEqual(self._row_count("buyback_claims"), 0)
        for invalid in inputs:
            if isinstance(invalid, str):
                self.assertNotIn(invalid.encode(), self.db_path.read_bytes())

    def test_quote_belongs_to_exact_user(self):
        quote_id = self._quote(user_id=42)
        self.assertEqual(self.repo.accept(43, quote_id), {"kind": "notfound"})
        self.assertEqual(self.repo.balance(42), 0)
        self.assertEqual(self.repo.accept(42, quote_id)["kind"], "accepted")
        self.assertEqual(self.repo.claims(43), [])
        for invalid in ("", "0" * 32, "../quote", None):
            self.assertEqual(self.repo.accept(42, invalid), {"kind": "notfound"})

    def test_duplicate_protection_between_users_and_after_registry_clear(self):
        quote_one = self._quote(1)
        quote_two = self._quote(2)
        self.assertEqual(self.repo.accept(1, quote_one)["kind"], "accepted")
        self.assertEqual(self.repo.accept(2, quote_two), {"kind": "duplicate"})
        self.assertEqual(self.repo.check(3, self.token), {"kind": "duplicate"})
        self.repo.clear_registry()
        self.assertEqual(self.repo.registry_state()["count"], 0)
        self.assertEqual(self.repo.accept(1, quote_one), {"kind": "duplicate"})
        self.assertEqual(self.repo.check(3, self.token), {"kind": "duplicate"})
        self.repo.seed_registry()
        self.assertEqual(self.repo.check(3, self.token), {"kind": "duplicate"})
        self.assertEqual(self.repo.balance(1), 1800)
        self.assertEqual(self.repo.balance(2), 0)
        self.assertEqual(len(self.repo.claims(1)), 1)

    def test_quote_expires_exactly_after_five_minutes(self):
        with patch("tgearn.buyback.time.time", return_value=1000):
            quote_id = self._quote()
        with patch("tgearn.buyback.time.time", return_value=1300):
            self.assertEqual(self.repo.accept(1, quote_id), {"kind": "expired"})
        self.assertEqual(self.repo.balance(1), 0)

    def test_quote_is_usable_until_expiry(self):
        with patch("tgearn.buyback.time.time", return_value=1000):
            quote_id = self._quote()
        with patch("tgearn.buyback.time.time", return_value=1299.99):
            self.assertEqual(self.repo.accept(1, quote_id)["kind"], "accepted")

    def test_registry_removal_rechecked_on_confirmation(self):
        quote_id = self._quote()
        self.repo.clear_registry()
        self.assertEqual(self.repo.accept(1, quote_id), {"kind": "notfound"})
        self.assertEqual(self.repo.balance(1), 0)

    def test_old_quote_does_not_survive_registry_reseed(self):
        quote_id = self._quote()
        self.repo.clear_registry()
        self.repo.seed_registry()
        self.assertEqual(self.repo.accept(1, quote_id), {"kind": "expired"})
        fresh = self.repo.check(1, self.token)
        self.assertEqual(self.repo.accept(1, fresh["quote_id"])["kind"], "accepted")

    def test_changed_sku_is_rechecked_before_credit(self):
        quote_id = self._quote()
        token_hash = hashlib.sha256(self.token.encode()).hexdigest()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE buyback_registry SET sku = 'A100' WHERE token_hash = ?",
                (token_hash,),
            )
        self.assertEqual(self.repo.accept(1, quote_id), {"kind": "expired"})
        self.assertEqual(self.repo.balance(1), 0)

    def test_unknown_registered_sku_does_not_create_quote(self):
        self.repo.seed_registry()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("UPDATE buyback_registry SET sku = 'UNKNOWN'")
        self.assertEqual(self.repo.check(1, self.token), {"kind": "unsupported"})
        self.assertEqual(self._row_count("buyback_quotes"), 0)

    def test_unavailable_is_separate_from_notfound_and_disables_confirmation(self):
        quote_id = self._quote()
        self.repo.set_available(False)
        self.assertFalse(self.repo.registry_state()["available"])
        self.assertEqual(self.repo.check(1, self.token), {"kind": "unavailable"})
        self.assertEqual(self.repo.accept(1, quote_id), {"kind": "unavailable"})
        self.assertEqual(self.repo.balance(1), 0)
        self.repo.set_available(True)
        self.assertEqual(self.repo.accept(1, quote_id)["kind"], "accepted")

    def test_persistence_history_and_balance_after_reopening(self):
        for fixture in self.repo.fixtures()[:2]:
            quote_id = self._quote(user_id=7, token=fixture["token"])
            self.assertEqual(self.repo.accept(7, quote_id)["kind"], "accepted")
        reopened = BuybackRepository(self.db_path)
        self.assertEqual(reopened.balance(7), 4800)
        self.assertEqual(len(reopened.claims(7)), 2)
        self.assertEqual(len(reopened.claims(7, limit=1)), 1)
        self.assertEqual(reopened.claims(8), [])
        self.assertEqual(reopened.check(8, self.token), {"kind": "duplicate"})
        self.assertEqual(reopened.claims(7)[0]["sku"], "A100")

    def test_competing_connections_can_credit_a_token_only_once(self):
        quote_one = self._quote(1)
        quote_two = self._quote(2)
        other = BuybackRepository(self.db_path)
        start = threading.Barrier(2)

        def confirm(repo, user_id, quote_id):
            start.wait(timeout=5)
            return repo.accept(user_id, quote_id)["kind"]

        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(confirm, self.repo, 1, quote_one)
            second = pool.submit(confirm, other, 2, quote_two)
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertCountEqual(results, ["accepted", "duplicate"])
        self.assertEqual(self.repo.balance(1) + self.repo.balance(2), 1800)
        self.assertEqual(self._row_count("buyback_claims"), 1)


if __name__ == "__main__":
    unittest.main()
