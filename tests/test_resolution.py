"""Pending contact-resolution state: isolation, expiry and bounded capacity."""
from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

_SPEC = importlib.util.spec_from_file_location(
    "persona_delegate_resolution_under_test", Path(__file__).resolve().parents[1] / "resolution.py",
)
resolution = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(resolution)


class NeedsTargetTests(unittest.TestCase):
    def test_name_is_normalized_and_candidate_sequence_is_copied(self):
        candidates = [{"qq": "123456"}]
        error = resolution.NeedsTarget(42, candidates)
        candidates.clear()
        self.assertIsInstance(error, ValueError)
        self.assertEqual(error.name, "42")
        self.assertEqual(error.candidates, [{"qq": "123456"}])
        self.assertEqual(str(error), "联系人需要确认")

    def test_default_candidates_are_independent(self):
        first = resolution.NeedsTarget("甲")
        first.candidates.append({"qq": "123456"})
        self.assertEqual(resolution.NeedsTarget("乙").candidates, [])


class PendingTargetsTests(unittest.TestCase):
    def setUp(self):
        self.clock = patch.object(resolution.time, "time", return_value=1000)
        self.now = self.clock.start()
        self.addCleanup(self.clock.stop)
        self.pending = resolution.PendingTargets(ttl=300, limit=3)

    def test_default_ttl_and_capacity(self):
        pending = resolution.PendingTargets()
        self.assertEqual(pending.ttl, 300)
        self.assertEqual(pending.limit, 1000)
        self.assertEqual(pending.items, {})

    def test_owner_origin_and_platform_qualified_owner_are_isolated(self):
        keys = [("qq:owner", "qq:GroupMessage:g1"),
                ("qq:other", "qq:GroupMessage:g1"),
                ("qq:owner", "qq:GroupMessage:g2")]
        for index, key in enumerate(keys):
            self.pending.put(*key, goal=f"goal-{index}")
        for index, key in enumerate(keys):
            self.assertEqual(self.pending.get(*key)["goal"], f"goal-{index}")
        self.assertIsNone(self.pending.get("other:owner", keys[0][1]))
        self.assertIsNone(self.pending.get("qq:owner", "qq:FriendMessage:owner"))

    def test_get_does_not_consume_and_pop_consumes_only_exact_scope(self):
        self.pending.put("a", "g1", goal="first")
        self.pending.put("b", "g1", goal="other")
        first = self.pending.get("a", "g1")
        self.assertIs(self.pending.get("a", "g1"), first)
        self.assertIsNone(self.pending.pop("a", "g2"))
        self.assertEqual(self.pending.pop("a", "g1")["goal"], "first")
        self.assertIsNone(self.pending.pop("a", "g1"))
        self.assertEqual(self.pending.get("b", "g1")["goal"], "other")

    def test_request_payload_is_retained_with_expiry(self):
        request = dict(name="小明", candidates=[{"qq": "123456"}], goal="明天见", persona_id="saved", channel="private")
        self.pending.put("a", "g1", **request)
        self.assertEqual(self.pending.get("a", "g1"), dict(request, expires=1300))
        self.assertNotIn("expires", request)

    def test_ttl_boundary_expires_without_sliding_on_get(self):
        self.pending.put("a", "g1", goal="first")
        self.now.return_value = 1299.999
        self.assertEqual(self.pending.get("a", "g1")["expires"], 1300)
        self.now.return_value = 1300
        self.assertIsNone(self.pending.get("a", "g1"))
        self.assertEqual(self.pending.items, {})

    def test_pop_never_returns_expired_request(self):
        self.pending.put("a", "g1", goal="first")
        self.now.return_value = 1301
        self.assertIsNone(self.pending.pop("a", "g1"))

    def test_prune_removes_only_expired_entries(self):
        self.pending.put("a", "g1", goal="first")
        self.now.return_value = 1100
        self.pending.put("b", "g1", goal="second")
        self.now.return_value = 1300
        self.pending.prune()
        self.assertEqual(set(self.pending.items), {("b", "g1")})

    def test_replacing_same_scope_refreshes_ttl_without_evicting_other_request(self):
        for owner in ("a", "b", "c"):
            self.pending.put(owner, "g1", goal=owner)
        self.now.return_value = 1100
        self.pending.put("a", "g1", goal="replacement")
        self.assertEqual(len(self.pending.items), 3)
        self.assertEqual(self.pending.get("a", "g1"), {"goal": "replacement", "expires": 1400})
        self.assertEqual(self.pending.get("b", "g1")["goal"], "b")
        self.assertEqual(self.pending.get("c", "g1")["goal"], "c")

    def test_limit_evicts_oldest_insertion_and_replacement_becomes_newest(self):
        for owner in ("a", "b", "c", "a", "d"):
            self.pending.put(owner, "g1", goal=owner)
        self.assertEqual(len(self.pending.items), 3)
        self.assertIsNone(self.pending.get("b", "g1"))
        self.assertEqual(list(self.pending.items), [("c", "g1"), ("a", "g1"), ("d", "g1")])

    def test_put_prunes_expired_before_capacity_eviction(self):
        self.pending.put("expired", "g1", goal="expired")
        self.now.return_value = 1100
        self.pending.put("live", "g1", goal="live")
        self.now.return_value = 1300
        self.pending.put("new", "g1", goal="new")
        self.assertEqual(set(self.pending.items), {("live", "g1"), ("new", "g1")})

    def test_capacity_one_keeps_latest_scope(self):
        pending = resolution.PendingTargets(limit=1)
        pending.put("a", "g1", goal="first")
        pending.put("b", "g2", goal="second")
        self.assertEqual(len(pending.items), 1)
        self.assertIsNone(pending.get("a", "g1"))
        self.assertEqual(pending.pop("b", "g2")["goal"], "second")


if __name__ == "__main__":
    unittest.main()
