"""Standard-library Engine regression tests; run unittest discover -s tests -v."""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

# Load this exact checkout, independently of adapter imports and package names.
_SPEC = importlib.util.spec_from_file_location(
    "persona_delegate_engine_under_test", Path(__file__).resolve().parents[1] / "engine.py"
)
assert _SPEC is not None and _SPEC.loader is not None
engine = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(engine)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lock_patch = patch.object(engine, "_LOCK", asyncio.Lock())
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.sent = []
        self.calls = []
        self.fail_target = False
        self.fail_report = False
        self.fail_relay = False
        self.fail_judge = False
        self.related = True
        self.persona_output = "你好，请确认事项。"
        self.relay_output = "主人，小明说已经收到啦～"
        self.waiting_output = "我已经传达过去了，等他回复就告诉你～"
        self.no_reply_output = "小明还没回我，等有消息再告诉你～"
        self.e = engine.Engine(Path(self.temp.name) / "tasks.sqlite", self.send, self.generate)
        await self.e.initialize()
        self.addAsyncCleanup(self.e.close)

    async def send(self, target, text):
        self.sent.append((target, text))
        if (target.startswith("target") and self.fail_target) or (target == "report" and self.fail_report):
            raise RuntimeError("sensitive callback error")

    async def generate(self, provider, system, prompt):
        self.calls.append((provider, system, prompt))
        if "相关性判定器" in system:
            if self.fail_judge:
                raise RuntimeError("judge boom")
            return '{"related": %s}' % ("true" if self.related else "false")
        if "正在等对方回复" in system:
            return self.waiting_output
        if "联系人还没有回复" in system:
            return self.no_reply_output
        if "转述给委托人" in system:
            if self.fail_relay:
                raise RuntimeError("relay boom")
            return self.relay_output
        return self.persona_output

    async def start(self, **overrides):
        values = dict(owner_key="owner", origin="origin-secret", report_to="report", target="target",
                      target_name="目标", goal="确认事项", persona_id="persona",
                      persona_prompt="人格提示", provider_id="provider", owner_name="主人")
        values.update(overrides)
        return await self.e.start(**values)

    def target_messages(self):
        return [text for target, text in self.sent if target.startswith("target")]

    def outbox(self):
        return self.e._conn().execute("SELECT * FROM outbox ORDER BY id").fetchall()

    async def latest(self, owner="owner"):
        return (await self.e.status(owner))[0]

    async def test_first_send_uses_caller_subject_without_robot_disclosure(self):
        task = await self.start()
        self.assertEqual((task["status"], task["turns"]), ("waiting", 1))
        message = self.target_messages()[0]
        self.assertNotIn("机器人", message)
        self.assertEqual(message, self.persona_output)
        system, prompt = self.calls[0][1], self.calls[0][2]
        self.assertIn("主人", system)
        self.assertIn("第一人称", system)
        self.assertIn("绝不能指你", system)
        self.assertIn("主人", prompt)
        self.assertEqual(len(self.sent), 2, "发送后应有一条人格化的“已传达”进度回报")
        self.assertEqual(self.sent[-1][1], self.waiting_output)
        self.assertNotIn("【代聊回报】", self.sent[-1][1])
        self.assertTrue(set("id status mode target target_name goal owner_key origin report_to turns created updated deadline transcript persona_id persona_prompt provider_id".split()) <= task.keys())
        self.assertEqual(task["transcript"][0]["delivery"], "sent")

    def test_default_wait_timeout_is_300_seconds(self):
        e = engine.Engine(Path(self.temp.name) / "default.sqlite", self.send, self.generate)
        self.assertEqual(e.timeout_seconds, 300)

    def test_split_text_uses_sentence_boundaries_and_limit(self):
        text = "第一句。" * 40
        chunks = engine.split_text(text, 60)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 60 for chunk in chunks))
        self.assertEqual("".join(chunks), text)

    def test_split_text_hard_cuts_without_punctuation(self):
        text = "长" * 200
        chunks = engine.split_text(text, 50)
        self.assertEqual([len(chunk) for chunk in chunks], [50, 50, 50, 50])
        self.assertEqual("".join(chunks), text)
        self.assertEqual(engine.split_text("", 50), [])

    async def test_long_first_message_is_split_into_multiple_sends(self):
        self.e.max_message_chars = 60
        self.persona_output = "句子。" * 40
        await self.start()
        targets = self.target_messages()
        self.assertGreater(len(targets), 1)
        self.assertTrue(all(len(text) <= 60 for text in targets))
        self.assertEqual("".join(targets), self.persona_output)

    async def test_long_relay_is_split_into_multiple_reports(self):
        self.e.max_message_chars = 60
        self.e.followup_window = 0
        self.relay_output = "转述。" * 40
        await self.start()
        before = len(self.sent)
        await self.e.receive("target", "原文")
        reports = [text for target, text in self.sent[before:] if target == "report"]
        self.assertGreater(len(reports), 1)
        self.assertTrue(all(len(text) <= 60 for text in reports))
        self.assertEqual("".join(reports), self.relay_output)

    async def test_first_reply_is_reported_in_persona_style(self):
        self.e.followup_window = 0  # isolate the first-reply relay from follow-ups
        await self.start()
        self.assertTrue(await self.e.receive("target", "原文证据：已收到。"))
        task = await self.latest()
        self.assertEqual(task["status"], "completed")
        report = self.sent[-1][1]
        self.assertEqual(report, self.relay_output, "完成回报应直接是人格化转述，不带机械模板头")
        self.assertEqual(len(self.calls), 3, "首条 + 等回复通知 + 一次人格转述，不再续写对话")
        self.assertEqual([m["role"] for m in task["transcript"]], ["assistant", "user"])
        self.assertEqual(task["transcript"][1]["text"], "原文证据：已收到。")
        self.assertFalse(await self.e.receive("target", "第二条"))

    async def test_relay_style_failure_falls_back_to_verbatim(self):
        self.fail_relay = True
        await self.start()
        await self.e.receive("target", "原文证据：已收到。")
        report = self.sent[-1][1]
        self.assertIn("人格转述失败", report)
        self.assertIn("原文证据：已收到。", report)
        self.assertEqual((await self.latest())["status"], "completed")

    async def test_followup_related_message_is_relayed_in_persona_style(self):
        await self.start()
        await self.e.receive("target", "好的")
        target_count = len(self.target_messages())
        self.assertTrue(await self.e.receive("target", "补充一下：改成四点"))
        report = self.sent[-1][1]
        self.assertEqual(report, self.relay_output)
        self.assertEqual(len(self.target_messages()), target_count, "跟转不应再向联系人发送")
        row = self.e._conn().execute("SELECT count FROM followups WHERE target='target'").fetchone()
        self.assertEqual(row["count"], 1)

    async def test_followup_unrelated_message_is_not_relayed(self):
        await self.start()
        await self.e.receive("target", "好的")
        self.related = False
        before = len(self.sent)
        self.assertFalse(await self.e.receive("target", "今天天气不错"))
        self.assertEqual(len(self.sent), before)

    async def test_followup_judge_failure_is_treated_as_unrelated(self):
        await self.start()
        await self.e.receive("target", "好的")
        self.fail_judge = True
        before = len(self.sent)
        self.assertFalse(await self.e.receive("target", "补充"))
        self.assertEqual(len(self.sent), before)

    async def test_followup_expired_window_is_ignored(self):
        await self.start()
        await self.e.receive("target", "好的")
        with self.e._conn():
            self.e._conn().execute("UPDATE followups SET until=?", (time.time() - 1,))
        self.assertFalse(await self.e.receive("target", "补充"))

    async def test_followup_max_relays_cap(self):
        self.e.followup_max = 1
        await self.start()
        await self.e.receive("target", "好的")
        self.assertTrue(await self.e.receive("target", "一"))
        self.assertFalse(await self.e.receive("target", "二"))

    async def test_followup_disabled_by_zero_window(self):
        self.e.followup_window = 0
        await self.start()
        await self.e.receive("target", "好的")
        self.assertFalse(await self.e.receive("target", "补充"))

    async def test_new_task_clears_previous_followup(self):
        await self.start()
        await self.e.receive("target", "好的")
        self.assertIsNotNone(self.e._conn().execute("SELECT 1 FROM followups WHERE target='target'").fetchone())
        await self.start()
        self.assertIsNone(self.e._conn().execute("SELECT 1 FROM followups WHERE target='target'").fetchone())

    async def test_followup_style_failure_falls_back_to_verbatim(self):
        await self.start()
        self.fail_relay = True
        await self.e.receive("target", "好的")
        await self.e.receive("target", "补充：改成四点")
        report = self.sent[-1][1]
        self.assertIn("原话跟转", report)
        self.assertIn("补充：改成四点", report)

    async def test_blank_reply_is_not_recorded_as_completion(self):
        await self.start()
        self.assertFalse(await self.e.receive("target", "   "))
        self.assertEqual((await self.latest())["status"], "waiting")
        self.assertFalse(self.e._task((await self.latest())["id"])["transcript"][-1]["role"] == "user")

    async def test_reply_from_another_session_is_ignored(self):
        await self.start()
        self.assertFalse(await self.e.receive("target-other", "路过"))
        self.assertEqual((await self.latest())["status"], "waiting")

    async def test_target_failure_never_retries(self):
        self.fail_target = True
        task = await self.start()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(task["transcript"][-1]["delivery"], "uncertain")
        self.fail_target = False
        await self.e.tick()
        await self.e.close()
        await self.e.initialize()
        await self.e.tick()
        self.assertEqual(len(self.target_messages()), 1)

    async def test_empty_llm_output_fails_without_target_send(self):
        self.persona_output = " \n "
        task = await self.start()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.target_messages(), [])

    async def test_generate_exception_is_failed(self):
        async def broken(*args):
            raise RuntimeError("secret")
        self.e.generate = broken
        task = await self.start()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.target_messages(), [])

    async def test_llm_timeout_is_failed_before_task_deadline(self):
        self.e.llm_timeout = 0.01
        async def pending(*args):
            await asyncio.Event().wait()
        self.e.generate = pending
        task = await self.start()
        self.assertEqual(task["status"], "failed")
        self.assertEqual(self.target_messages(), [])

    async def test_send_timeout_is_failed_not_retried(self):
        async def pending(target, text):
            self.sent.append((target, text))
            if target == "target":
                await asyncio.Event().wait()
        self.e.send = pending
        with patch.object(engine, "_SEND_TIMEOUT", 0.01):
            task = await self.start()
        self.assertEqual(task["status"], "failed")
        await self.e.tick()
        self.assertEqual(len(self.target_messages()), 1)

    async def test_restart_interrupts_without_sending_on_initialize(self):
        task = await self.start()
        await self.e.close()
        before = len(self.sent)
        await self.e.initialize()
        self.assertEqual(len(self.sent), before)
        self.assertEqual((await self.latest())["status"], "interrupted")
        self.assertEqual(self.outbox()[0]["task_id"], task["id"])
        await self.e.tick()
        self.assertEqual(len(self.target_messages()), 1)

    async def test_timeout_tick_ends_task(self):
        task = await self.start()
        with self.e._conn():
            self.e._conn().execute("UPDATE tasks SET deadline=? WHERE id=?", (time.time() - 1, task["id"]))
        await self.e.tick()
        self.assertEqual((await self.latest())["status"], "timeout")
        self.assertEqual(self.sent[-1][1], self.no_reply_output)
        self.assertFalse(await self.e.receive("target", "太晚了"))

    async def test_persona_status_reports_can_be_disabled(self):
        self.e.persona_status_reports = False
        await self.start()
        self.assertIn("【代聊回报】", self.sent[-1][1])
        self.assertIn("等待对方回复", self.sent[-1][1])
        task = (await self.latest())
        with self.e._conn():
            self.e._conn().execute("UPDATE tasks SET deadline=? WHERE id=?", (time.time() - 1, task["id"]))
        await self.e.tick()
        self.assertIn("任务超时终止", self.sent[-1][1])

    async def test_retention_prunes_old_terminal_not_active_or_outbox(self):
        self.fail_report = True
        old = await self.start()
        await self.e.cancel("owner", old["id"])
        active = await self.start(target="target-active")
        with self.e._conn():
            self.e._conn().execute("UPDATE tasks SET updated=?", (time.time() - 8 * 86400,))
        await self.e.tick()
        ids = [t["id"] for t in await self.e.status("owner")]
        self.assertNotIn(old["id"], ids)
        self.assertIn(active["id"], ids)
        self.assertTrue(any(r["task_id"] == old["id"] for r in self.outbox()))

    async def test_cancel_owner_isolation_specific_and_all(self):
        mine = await self.start()
        theirs = await self.start(owner_key="other", target="target-other")
        self.assertIn("没有找到", await self.e.cancel("other", mine["id"]))
        self.assertEqual((await self.latest())["status"], "waiting")
        self.assertIn("1", await self.e.cancel("owner", mine["id"]))
        self.assertEqual((await self.latest())["status"], "cancelled")
        self.assertEqual((await self.latest("other"))["id"], theirs["id"])
        self.assertEqual((await self.latest("other"))["status"], "waiting")
        await self.start(target="target-two")
        await self.start(target="target-three")
        self.assertIn("2", await self.e.cancel("owner"))
        self.assertEqual((await self.latest("other"))["status"], "waiting")

    async def test_concurrent_same_target_only_one_task(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        async def slow(provider, system, prompt):
            entered.set()
            await release.wait()
            return "你好"
        self.e.generate = slow
        first = asyncio.create_task(self.start())
        await entered.wait()
        second = asyncio.create_task(self.start())
        release.set()
        results = await asyncio.gather(first, second, return_exceptions=True)
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ValueError) for r in results), 1)
        self.assertEqual(len(self.target_messages()), 1)

    async def test_global_twenty_active_limit(self):
        for i in range(20):
            await self.start(target=f"target-{i}", owner_key=f"owner-{i}")
        with self.assertRaises(ValueError):
            await self.start(target="target-overflow")

    async def test_report_failure_keeps_completed_status_and_backoff_caps(self):
        await self.start()
        self.fail_report = True
        await self.e.receive("target", "好的")
        self.assertEqual((await self.latest())["status"], "completed")
        first = self.outbox()[0]
        self.assertEqual(first["attempts"], 1)
        self.assertGreater(first["next_attempt"], time.time())
        await self.e.tick()
        self.assertEqual(self.outbox()[0]["attempts"], 1)
        for attempt in range(2, 13):
            with self.e._conn():
                self.e._conn().execute("UPDATE outbox SET next_attempt=0")
            before = time.time()
            await self.e.tick()
            row = self.outbox()[0]
            self.assertEqual(row["attempts"], attempt)
            self.assertAlmostEqual(row["next_attempt"] - before, min(3600, 5 * 2 ** min(attempt - 1, 10)), delta=1)
        self.fail_report = False
        with self.e._conn():
            self.e._conn().execute("UPDATE outbox SET next_attempt=0")
        await self.e.tick()
        self.assertEqual(self.outbox(), [])
        self.assertEqual((await self.latest())["status"], "completed")

    async def test_output_length_and_prompt_history_allowlist(self):
        self.persona_output = "长" * 5000
        await self.start()
        self.assertTrue(all(len(text) <= 1500 for _, text in self.sent))
        provider, system, prompt = self.calls[0]
        self.assertEqual(provider, "provider")
        self.assertIn("人格提示", system)
        self.assertIn("只输出正文", system)
        self.assertNotIn("origin-secret", prompt)
        self.assertNotIn("owner_key", prompt)
        self.assertNotIn("report_to", prompt)
        self.assertIn("确认事项", prompt)

    async def test_validation_and_status_recent_ten_owner_only(self):
        with self.assertRaises(ValueError):
            await self.start(goal="字" * 2001)
        with self.assertRaises(ValueError):
            await self.start(owner_key="")
        for _ in range(12):
            task = await self.start()
            await self.e.cancel("owner", task["id"])
        await self.start(owner_key="other", target="target-other")
        rows = await self.e.status("owner")
        self.assertEqual(len(rows), 10)
        self.assertTrue(all(t["owner_key"] == "owner" for t in rows))
        self.assertEqual([t["created"] for t in rows], sorted((t["created"] for t in rows), reverse=True))

    async def test_tasks_for_target_and_silent_cancel(self):
        task = await self.start()
        self.assertEqual([t["id"] for t in await self.e.tasks_for_target("target")], [task["id"]])
        before = len(self.sent)
        self.assertEqual(await self.e.cancel_target("target", "对方已退订私聊。", notify=False), 1)
        self.assertEqual(len(self.sent), before, "notify=False 不应排队回报")
        self.assertEqual(await self.e.tasks_for_target("target"), [])
        self.assertEqual((await self.latest())["status"], "cancelled")

    async def test_cancel_target_cancels_waiting_task_and_reports(self):
        await self.start()
        self.assertEqual(await self.e.cancel_target("target", "对方已退订私聊，任务停止。"), 1)
        self.assertEqual((await self.latest())["status"], "cancelled")
        self.assertIn("退订", self.sent[-1][1])
        self.assertEqual(await self.e.cancel_target("target", "again"), 0)

    async def test_external_cancellation_persists_failure(self):
        entered = asyncio.Event()
        async def pending(*args):
            entered.set()
            await asyncio.Event().wait()
        self.e.generate = pending
        task = asyncio.create_task(self.start())
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await self.latest())["status"], "failed")
        self.assertEqual(self.target_messages(), [])


if __name__ == "__main__":
    unittest.main()
