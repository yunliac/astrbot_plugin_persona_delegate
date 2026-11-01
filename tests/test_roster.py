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

    def test_exact_group_authorization_and_alias_owner_scope(self):
        for group in ("g1", "g2"):
            self.roster.replace_group("qq", group, group, [{"qq": "owner"}, {"qq": "target", "nickname": "官方昵称"}])
        self.roster.add_alias("qq", "g1", "qq:owner", "专属称呼", "target")
        self.assertTrue(self.roster.is_shared_group("qq", "g1", "owner", "target"))
        self.assertFalse(self.roster.is_shared_group("qq", "g3", "owner", "target"))
        self.assertFalse(self.roster.is_shared_group("other", "g1", "owner", "target"))
        self.assertEqual(len(self.roster.find_candidates("qq", "g1", "专属称呼", "qq:owner")), 1)
        for group, owner in [("g1", "qq:other"), ("g2", "qq:owner"), ("", "qq:owner")]:
            self.assertEqual(self.roster.find_candidates("qq", group, "专属称呼", owner), [])
        self.roster.replace_group("qq", "g1", "g1", [{"qq": "owner"}])
        self.assertFalse(self.roster.is_shared_group("qq", "g1", "owner", "target"))
        self.assertEqual(self.roster.find_candidates("qq", "g1", "专属称呼", "qq:owner"), [])

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

    def test_find_candidates_exact_matches_and_required_fields(self):
        self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "小明", "card": "小明同学"},
        ])
        for name in ("小明", "小明同学", "42", "@小明"):
            with self.subTest(name=name):
                rows = self.roster.find_candidates("qq", "100", name)
                self.assertEqual(len(rows), 1)
                self.assertEqual(
                    {key: rows[0][key] for key in ("qq", "nickname", "card", "group_id", "platform")},
                    {"qq": "42", "nickname": "小明", "card": "小明同学",
                     "group_id": "100", "platform": "qq"},
                )
        for name in ("小", "明", "4", "", "@"):
            self.assertEqual(self.roster.find_candidates("qq", "100", name), [])
        self.assertEqual(self.roster.find_candidates("qq", "200", "小明"), [])
        self.assertEqual(self.roster.find_candidates("telegram", "100", "小明"), [])

    def test_same_name_keeps_all_candidates_and_legacy_member_is_ambiguous(self):
        self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "同名"},
            {"qq": "43", "nickname": "同名"},
            {"qq": "44", "nickname": "其他", "card": "同名"},
        ])
        rows = self.roster.find_candidates("qq", "100", "同名")
        self.assertEqual({row["qq"] for row in rows}, {"42", "43", "44"})
        self.assertIsNone(self.roster.find_member("qq", "100", "同名"))
        self.assertIsNone(self.roster.find_member_any("qq", "同名"))

    def test_private_candidates_deduplicate_same_qq_across_groups(self):
        self.roster.replace_group("qq", "100", "群A", [{"qq": "42", "nickname": "同名"}])
        self.roster.replace_group("qq", "200", "群B", [
            {"qq": "42", "nickname": "同名"}, {"qq": "43", "nickname": "同名"},
        ])
        rows = self.roster.find_candidates("qq", "", "同名")
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["qq"] for row in rows}, {"42", "43"})
        self.assertEqual(len(self.roster.find_candidates("qq", "", "42")), 1)
        self.assertEqual(self.roster.find_member_any("qq", "42")["qq"], "42")

    def test_alias_persists_refresh_and_reopen_without_changing_official_names(self):
        self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "正式昵称", "card": "正式名片"},
        ])
        self.roster.add_alias("qq", "100", "owner", "老朋友", "42")
        self.roster.add_alias("qq", "100", "owner", "老朋友", "42")
        self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "新昵称", "card": "新名片"},
        ])
        self.roster.close()
        self.roster.initialize()
        rows = self.roster.find_candidates("qq", "100", "老朋友", "owner")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["qq"], "42")
        self.assertEqual(rows[0]["nickname"], "新昵称")
        self.assertEqual(rows[0]["card"], "新名片")
        self.assertEqual(self.roster.find_candidates("qq", "100", "老朋", "owner"), [])

    def test_alias_isolated_by_owner_platform_and_exact_group_scope(self):
        for platform in ("qq", "telegram"):
            for group_id in ("100", "200"):
                self.roster.replace_group(platform, group_id, "群", [{"qq": "42", "nickname": "A"}])
        self.roster.add_alias("qq", "100", "owner", "群称呼", "42")
        self.roster.add_alias("qq", "", "owner", "私聊称呼", "42")
        self.assertEqual(len(self.roster.find_candidates("qq", "100", "群称呼", "owner")), 1)
        self.assertEqual(len(self.roster.find_candidates("qq", "", "私聊称呼", "owner")), 1)
        for platform, group_id, owner, alias in (
            ("qq", "100", "other", "群称呼"),
            ("qq", "100", "", "群称呼"),
            ("telegram", "100", "owner", "群称呼"),
            ("qq", "200", "owner", "群称呼"),
            ("qq", "", "owner", "群称呼"),
            ("qq", "100", "owner", "私聊称呼"),
            ("qq", "200", "owner", "私聊称呼"),
        ):
            with self.subTest(platform=platform, group_id=group_id, owner=owner, alias=alias):
                self.assertEqual(self.roster.find_candidates(platform, group_id, alias, owner), [])

    def test_alias_allows_multiple_qq_and_merges_with_official_candidates(self):
        self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "A"},
            {"qq": "43", "nickname": "B"},
            {"qq": "44", "nickname": "称呼"},
        ])
        for qq in ("42", "43", "44"):
            self.roster.add_alias("qq", "100", "owner", "称呼", qq)
        rows = self.roster.find_candidates("qq", "100", "称呼", "owner")
        self.assertEqual(len(rows), 3)
        self.assertEqual({row["qq"] for row in rows}, {"42", "43", "44"})
        self.roster.add_alias("qq", "100", "", "旧接口称呼", "42")
        self.roster.add_alias("qq", "100", "", "旧接口称呼", "43")
        self.assertIsNone(self.roster.find_member("qq", "100", "旧接口称呼"))

    def test_alias_matches_only_current_members_in_its_scope(self):
        self.roster.replace_group("qq", "100", "群A", [{"qq": "42", "nickname": "A"}])
        self.roster.replace_group("qq", "200", "群B", [{"qq": "42", "nickname": "A"}])
        self.roster.add_alias("qq", "100", "owner", "群称呼", "42")
        self.roster.add_alias("qq", "", "owner", "私聊称呼", "42")
        self.roster.replace_group("qq", "100", "群A", [])
        self.assertEqual(self.roster.find_candidates("qq", "100", "群称呼", "owner"), [])
        self.assertEqual(len(self.roster.find_candidates("qq", "", "私聊称呼", "owner")), 1)
        self.roster.prune_groups("qq", ["100"])
        self.assertEqual(self.roster.find_candidates("qq", "", "私聊称呼", "owner"), [])
        self.roster.add_alias("qq", "100", "owner", "不存在", "404")
        self.assertEqual(self.roster.find_candidates("qq", "100", "不存在", "owner"), [])

    def test_legacy_lookups_resolve_unique_default_owner_alias(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42", "nickname": "A"}])
        self.roster.add_alias("qq", "100", "", "群称呼", "42")
        self.roster.add_alias("qq", "", "", "私聊称呼", "42")
        self.assertEqual(self.roster.find_member("qq", "100", "群称呼")["qq"], "42")
        self.assertEqual(self.roster.find_member_any("qq", "私聊称呼")["qq"], "42")
        self.roster.replace_group("qq", "200", "群", [{"qq": "43", "nickname": "B"}])
        self.roster.add_alias("qq", "", "", "私聊称呼", "43")
        self.assertIsNone(self.roster.find_member_any("qq", "私聊称呼"))

    def test_find_member_any_requires_unique_qq(self):
        self.roster.replace_group("qq", "100", "群A", [{"qq": "42", "nickname": "小明"}])
        self.roster.replace_group("qq", "200", "群B", [{"qq": "43", "nickname": "小明"}])
        self.assertIsNone(self.roster.find_member_any("qq", "小明"))
        self.roster.replace_group("qq", "200", "群B", [{"qq": "42", "nickname": "小明"}])
        row = self.roster.find_member_any("qq", "小明")
        self.assertIsNotNone(row)
        self.assertEqual(row["qq"], "42")

    def test_remember_mention_updates_nonempty_nickname_and_preserves_card(self):
        self.roster.remember_mention("qq", "100", "42", "群名片")
        self.assertEqual(self.roster.find_member("qq", "100", "42")["nickname"], "群名片")
        self.roster.replace_group("qq", "100", "群", [
            {"qq": "42", "nickname": "正式昵称", "card": "已有名片"},
        ])
        self.roster.remember_mention("qq", "100", "42", " 最新昵称 ")
        row = self.roster.find_member("qq", "100", "42")
        self.assertEqual(row["nickname"], "最新昵称")
        self.assertEqual(row["card"], "已有名片")
        self.assertIsNone(self.roster.find_member("qq", "100", "正式昵称"))
        self.roster.remember_mention("qq", "100", "42", "  ")
        row = self.roster.find_member("qq", "100", "42")
        self.assertEqual(row["nickname"], "最新昵称")
        self.assertEqual(row["card"], "已有名片")

    def test_remember_mention_fills_empty_nickname(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42", "card": "名片"}])
        self.roster.remember_mention("qq", "100", "42", "新昵称")
        row = self.roster.find_member("qq", "100", "42")
        self.assertEqual(row["nickname"], "新昵称")
        self.assertEqual(row["card"], "名片")

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

    def test_common_groups_allows_same_synced_group(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42"}, {"qq": "43"}])
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), ["100"])
        self.assertEqual(self.roster.common_groups("qq", "43", "42"), ["100"])

    def test_common_groups_rejects_different_groups(self):
        self.roster.replace_group("qq", "100", "群A", [{"qq": "42"}])
        self.roster.replace_group("qq", "200", "群B", [{"qq": "43"}])
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), [])

    def test_common_groups_rejects_cross_platform_members(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42"}])
        self.roster.replace_group("telegram", "100", "群", [{"qq": "43"}])
        for platform in ("qq", "telegram"):
            with self.subTest(platform=platform):
                self.assertEqual(self.roster.common_groups(platform, "42", "43"), [])
        self.roster.replace_group("telegram", "100", "群", [{"qq": "42"}, {"qq": "43"}])
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), [])
        self.assertEqual(self.roster.common_groups("telegram", "42", "43"), ["100"])

    def test_common_groups_returns_sorted_unique_shared_groups(self):
        for group_id in ("300", "100", "200"):
            self.roster.replace_group("qq", group_id, "群", [
                {"qq": "42"}, {"qq": "43"}, {"qq": "43"},
            ])
        self.roster.replace_group("qq", "400", "非共享群", [{"qq": "42"}])
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), ["100", "200", "300"])

    def test_common_groups_excludes_empty_group_id(self):
        self.roster.replace_group("qq", "", "无群号", [{"qq": "42"}, {"qq": "43"}])
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), [])

    def test_common_groups_mentions_cannot_authorize_either_member(self):
        for synced_members in ([], [{"qq": "42"}], [{"qq": "43"}]):
            with self.subTest(synced_members=synced_members):
                self.roster.replace_group("qq", "100", "群", synced_members)
                self.roster.remember_mention("qq", "100", "42", "发起人")
                self.roster.remember_mention("qq", "100", "43", "目标")
                self.assertEqual(self.roster.common_groups("qq", "42", "43"), [])
        self.roster.replace_group("qq", "100", "群", [{"qq": "42"}, {"qq": "43"}])
        self.roster.remember_mention("qq", "100", "42", "新昵称")
        self.roster.remember_mention("qq", "100", "43", "新昵称")
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), ["100"])

    def test_common_groups_rejects_member_after_refresh_removes_them(self):
        for remaining_qq, departed_qq in (("42", "43"), ("43", "42")):
            with self.subTest(departed_qq=departed_qq):
                self.roster.replace_group("qq", "100", "群", [{"qq": "42"}, {"qq": "43"}])
                self.assertEqual(self.roster.common_groups("qq", "42", "43"), ["100"])
                self.roster.replace_group("qq", "100", "群", [{"qq": remaining_qq}])
                self.assertEqual(self.roster.common_groups("qq", "42", "43"), [])
                self.roster.remember_mention("qq", "100", departed_qq, "退群成员")
                self.assertEqual(self.roster.common_groups("qq", "42", "43"), [])

    def test_common_groups_rejects_group_after_pruning(self):
        self.roster.replace_group("qq", "100", "群", [{"qq": "42"}, {"qq": "43"}])
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), ["100"])
        self.roster.prune_groups("qq", [])
        self.assertEqual(self.roster.common_groups("qq", "42", "43"), [])

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
