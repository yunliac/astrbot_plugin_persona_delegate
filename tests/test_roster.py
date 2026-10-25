"""Roster (group member directory) tests; standard library only."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import tempfile
import unittest

_SPEC = importlib.util.spec_from_file_location(
    "persona_delegate_roster_under_test", Path(__file__).resolve().parents[1] / "roster.py"
)
assert _SPEC is not None and _SPEC.loader is not None
roster_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(roster_mod)


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.roster = roster_mod.Roster(Path(self.temp.name) / "roster.sqlite3")
        self.roster.initialize()
        self.addCleanup(self.roster.close)

    def test_replace_group_stores_group_and_members(self):
        count = self.roster.replace_group("qq", "100", "测试群", [
            {"qq": "42", "nickname": "小明", "card": "小明同学"},
            {"qq": "43", "nickname": "小红", "card": ""},
        ])
        self.assertEqual(count, 2)
        stats = self.roster.stats()
        self.assertEqual(stats["groups"], 1)
        self.assertEqual(stats["members"], 2)
        self.assertEqual(self.roster.group_name("qq", "100"), "测试群")

    def test_replace_group_deduplicates_and_skips_empty_qq(self):
        count = self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "A"},
            {"qq": "42", "nickname": "A2"},
            {"qq": "", "nickname": "empty"},
            {},
        ])
        self.assertEqual(count, 1)
        self.assertEqual(self.roster.find_member("qq", "100", "42")["nickname"], "A")

    def test_replace_group_removes_members_no_longer_present(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42", "nickname": "A"}])
        self.roster.replace_group("qq", "100", "群", [{"qq": "43", "nickname": "B"}])
        self.assertIsNone(self.roster.find_member("qq", "100", "42"))
        self.assertIsNotNone(self.roster.find_member("qq", "100", "43"))

    def test_find_member_matches_nickname_card_or_qq_and_strips_at(self):
        self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "小明", "card": "小明同学"},
        ])
        for name in ("小明", "小明同学", "42", "@小明"):
            with self.subTest(name=name):
                row = self.roster.find_member("qq", "100", name)
                self.assertIsNotNone(row)
                self.assertEqual(row["qq"], "42")

    def test_find_member_any_requires_unique_qq(self):
        self.roster.replace_group("qq", "100", "群A", [{"qq": "42", "nickname": "小明"}])
        self.roster.replace_group("qq", "200", "群B", [{"qq": "43", "nickname": "小明"}])
        self.assertIsNone(self.roster.find_member_any("qq", "小明"))
        self.roster.replace_group("qq", "200", "群B", [{"qq": "42", "nickname": "小明"}])
        row = self.roster.find_member_any("qq", "小明")
        self.assertIsNotNone(row)
        self.assertEqual(row["qq"], "42")

    def test_remember_mention_adds_member_and_group_without_overwriting_names(self):
        self.roster.remember_mention("qq", "100", "42", "群名片")
        self.assertEqual(self.roster.find_member("qq", "100", "42")["nickname"], "群名片")
        self.roster.replace_group("qq", "100", "群", [{"qq": "42", "nickname": "正式昵称"}])
        self.roster.remember_mention("qq", "100", "42", "临时称呼")
        self.assertEqual(self.roster.find_member("qq", "100", "42")["nickname"], "正式昵称")

    def test_remember_mention_without_group_row_creates_placeholder(self):
        self.roster.remember_mention("qq", "999", "42", "路人")
        self.assertEqual(self.roster.group_name("qq", "999"), "")
        self.assertEqual(self.roster.find_member("qq", "999", "路人")["qq"], "42")

    def test_prune_groups_removes_stale_groups_and_members(self):
        self.roster.replace_group("qq", "100", "保留", [{"qq": "42", "nickname": "A"}])
        self.roster.replace_group("qq", "200", "退群", [{"qq": "43", "nickname": "B"}])
        self.roster.prune_groups("qq", ["100"])
        self.assertEqual(self.roster.stats()["groups"], 1)
        self.assertIsNotNone(self.roster.find_member("qq", "100", "42"))
        self.assertIsNone(self.roster.find_member("qq", "200", "43"))

    def test_group_for_returns_most_recent_group_of_qq(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42", "nickname": "A"}])
        self.assertEqual(self.roster.group_for("qq", "42"), "100")
        self.assertEqual(self.roster.group_for("qq", "404"), "")

    def test_platforms_are_isolated(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42", "nickname": "A"}])
        self.assertIsNone(self.roster.find_member("telegram", "100", "42"))
        self.assertIsNone(self.roster.find_member("qq", "100", "telegram"))


if __name__ == "__main__":
    unittest.main()
