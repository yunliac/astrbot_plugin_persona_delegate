"""Runtime blocklist tests; standard library only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "persona_delegate_moderation_under_test", Path(__file__).resolve().parents[1] / "moderation.py"
)
assert _SPEC is not None and _SPEC.loader is not None
moderation = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(moderation)


class BlocklistTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.blocklist = moderation.Blocklist(Path(self.temp.name) / "blocklist.sqlite3")
        self.blocklist.initialize()
        self.addCleanup(self.blocklist.close)

    def test_add_and_is_blocked(self):
        self.assertFalse(self.blocklist.is_blocked("qq", "123"))
        self.blocklist.add("qq", "123", "qq:admin")
        self.assertTrue(self.blocklist.is_blocked("qq", "123"))
        self.assertFalse(self.blocklist.is_blocked("qq", "456"))

    def test_wildcard_blocks_every_platform(self):
        self.blocklist.add("*", "123", "admin")
        self.assertTrue(self.blocklist.is_blocked("qq", "123"))
        self.assertTrue(self.blocklist.is_blocked("telegram", "123"))

    def test_platform_specific_does_not_block_other_platforms(self):
        self.blocklist.add("qq", "123", "admin")
        self.assertTrue(self.blocklist.is_blocked("qq", "123"))
        self.assertFalse(self.blocklist.is_blocked("telegram", "123"))

    def test_remove_reports_whether_row_existed(self):
        self.blocklist.add("qq", "123")
        self.assertTrue(self.blocklist.remove("qq", "123"))
        self.assertFalse(self.blocklist.is_blocked("qq", "123"))
        self.assertFalse(self.blocklist.remove("qq", "123"))

    def test_duplicate_add_updates_adder(self):
        self.blocklist.add("qq", "123", "first")
        self.blocklist.add("qq", "123", "second")
        entries = self.blocklist.entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["added_by"], "second")

    def test_entries_are_newest_first_with_fields(self):
        self.blocklist.add("qq", "1", "a")
        self.blocklist.add("qq", "2", "b")
        entries = self.blocklist.entries()
        self.assertEqual([row["account"] for row in entries], ["2", "1"])
        self.assertEqual(set(entries[0]), {"platform", "account", "added_by", "added_at"})

    def test_empty_account_is_rejected(self):
        with self.assertRaises(ValueError):
            self.blocklist.add("qq", "   ")


if __name__ == "__main__":
    unittest.main()
