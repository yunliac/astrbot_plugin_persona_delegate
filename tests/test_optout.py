"""Private-chat opt-out (/td) store tests; standard library only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import time
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "persona_delegate_optout_under_test", Path(__file__).resolve().parents[1] / "optout.py"
)
assert _SPEC is not None and _SPEC.loader is not None
optout_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(optout_mod)


class OptOutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.optout = optout_mod.OptOut(Path(self.temp.name) / "optout.sqlite3")
        self.optout.initialize()
        self.addCleanup(self.optout.close)

    def test_temporary_optout_blocks_until_deadline(self):
        until = self.optout.set("qq", "123", 3600)
        self.assertAlmostEqual(until, time.time() + 3600, delta=5)
        self.assertTrue(self.optout.is_opted_out("qq", "123"))

    def test_permanent_optout_uses_zero_until(self):
        self.assertEqual(self.optout.set("qq", "123", 0), 0.0)
        self.assertTrue(self.optout.is_opted_out("qq", "123"))
        self.assertEqual(self.optout.status("qq", "123")["until"], 0.0)

    def test_expired_optout_is_not_active(self):
        self.optout.set("qq", "123", 3600)
        with self.optout._conn():
            self.optout._conn().execute("UPDATE optouts SET until=?", (time.time() - 1,))
        self.assertFalse(self.optout.is_opted_out("qq", "123"))
        self.assertIsNone(self.optout.status("qq", "123"))

    def test_clear_removes_only_that_account(self):
        self.optout.set("qq", "123", 3600)
        self.optout.set("qq", "456", 3600)
        self.assertTrue(self.optout.clear("qq", "123"))
        self.assertFalse(self.optout.is_opted_out("qq", "123"))
        self.assertTrue(self.optout.is_opted_out("qq", "456"))
        self.assertFalse(self.optout.clear("qq", "123"))

    def test_platforms_are_isolated(self):
        self.optout.set("qq", "123", 3600)
        self.assertTrue(self.optout.is_opted_out("qq", "123"))
        self.assertFalse(self.optout.is_opted_out("telegram", "123"))

    def test_prune_expired_removes_only_expired_rows(self):
        self.optout.set("qq", "expired", 3600)
        self.optout.set("qq", "permanent", 0)
        with self.optout._conn():
            self.optout._conn().execute(
                "UPDATE optouts SET until=? WHERE account='expired'", (time.time() - 1,),
            )
        self.assertEqual(self.optout.prune_expired(), 1)
        accounts = [row["account"] for row in self.optout.entries()]
        self.assertEqual(accounts, ["permanent"])

    def test_sender_blocks_are_persistent_and_idempotent(self):
        self.optout.block_sender("qq", " 123 ", " 456 ")
        first = self.optout.blocked_senders("qq", "123")
        self.assertEqual(len(first), 1)
        self.assertEqual(set(first[0]), {"platform", "account", "sender_account", "created"})
        self.assertEqual(first[0]["sender_account"], "456")
        self.assertGreater(first[0]["created"], 0)
        self.optout.block_sender("qq", "123", "456")
        self.assertEqual(self.optout.blocked_senders("qq", "123"), first)
        self.optout.close()
        self.optout.initialize()
        self.assertEqual(self.optout.blocked_senders("qq", "123"), first)
        self.assertTrue(self.optout.is_sender_blocked("qq", "123", "456"))

    def test_sender_blocks_are_directional_and_scope_isolated(self):
        self.optout.block_sender("qq", "123", "456")
        self.assertFalse(self.optout.is_sender_blocked("qq", "456", "123"))
        self.assertFalse(self.optout.is_sender_blocked("telegram", "123", "456"))
        self.assertFalse(self.optout.is_sender_blocked("qq", "789", "456"))
        self.assertFalse(self.optout.is_sender_blocked("qq", "123", "789"))
        self.assertEqual(self.optout.blocked_senders("telegram", "123"), [])
        self.assertEqual(self.optout.blocked_senders("qq", "789"), [])

    def test_unblock_removes_only_matching_sender_and_does_not_clear_optout(self):
        self.optout.set("qq", "123", 0)
        self.optout.block_sender("qq", "123", "456")
        self.optout.block_sender("qq", "123", "789")
        self.assertTrue(self.optout.unblock_sender("qq", " 123 ", " 456 "))
        self.assertFalse(self.optout.unblock_sender("qq", "123", "456"))
        self.assertFalse(self.optout.is_sender_blocked("qq", "123", "456"))
        self.assertTrue(self.optout.is_sender_blocked("qq", "123", "789"))
        self.assertTrue(self.optout.is_opted_out("qq", "123"))
        self.optout.clear("qq", "123")
        self.optout.prune_expired()
        self.assertTrue(self.optout.is_sender_blocked("qq", "123", "789"))

    def test_sender_blocks_do_not_create_global_optout(self):
        self.optout.block_sender("qq", "123", "456")
        self.assertFalse(self.optout.is_opted_out("qq", "123"))
        self.assertIsNone(self.optout.status("qq", "123"))
        self.assertEqual(self.optout.entries(), [])

    def test_sender_block_empty_accounts_rejected(self):
        for account, sender in (("", "456"), ("123", "  "), (None, "456"), ("123", None)):
            with self.subTest(account=account, sender=sender), self.assertRaises(ValueError):
                self.optout.block_sender("qq", account, sender)
        self.assertEqual(self.optout.blocked_senders("qq", "123"), [])

    def test_sender_blocks_order_is_stable(self):
        self.optout.block_sender("qq", "123", "b")
        self.optout.block_sender("qq", "123", "a")
        with self.optout._conn():
            self.optout._conn().execute("UPDATE sender_blocks SET created=1")
        self.assertEqual([r["sender_account"] for r in self.optout.blocked_senders("qq", "123")], ["a", "b"])

    def test_empty_account_is_rejected(self):
        with self.assertRaises(ValueError):
            self.optout.set("qq", "   ", 3600)


if __name__ == "__main__":
    unittest.main()
