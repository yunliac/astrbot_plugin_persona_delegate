"""Adapter-only tests: no AstrBot installation or real engine is required.

Run with: python -m unittest discover -s tests -p test_adapter.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import json
from pathlib import Path
import sys
import time
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
import uuid


ROOT = Path(__file__).resolve().parents[1]


class StubMessageType:
    FRIEND_MESSAGE = "FriendMessage"
    GROUP_MESSAGE = "GroupMessage"


class StubMessageSession:
    def __init__(self, platform_id, message_type, session_id):
        self.platform_id = platform_id
        self.message_type = message_type
        self.session_id = session_id

    @classmethod
    def from_str(cls, value):
        parts = value.split(":")
        if len(parts) != 3 or not parts[0] or parts[1] not in {
            StubMessageType.FRIEND_MESSAGE, StubMessageType.GROUP_MESSAGE,
        }:
            raise ValueError("invalid session")
        return cls(*parts)

    def __str__(self):
        return f"{self.platform_id}:{self.message_type}:{self.session_id}"


class StubStar:
    def __init__(self, context):
        self.context = context


class StubPlain:
    def __init__(self, text=""):
        self.text = text


class StubAt:
    def __init__(self, qq="", name=None):
        self.qq = qq
        self.name = name


class StubOptOut:
    def __init__(self, path):
        self.path = path
        self.initialize = Mock()
        self.close = Mock()
        self.set = Mock(return_value=0.0)
        self.clear = Mock(return_value=True)
        self.is_opted_out = Mock(return_value=False)
        self.status = Mock(return_value=None)
        self.prune_expired = Mock()
        self.entries = Mock(return_value=[])


class StubBlocklist:
    def __init__(self, path):
        self.path = path
        self.initialize = Mock()
        self.close = Mock()
        self.add = Mock()
        self.remove = Mock(return_value=True)
        self.is_blocked = Mock(return_value=False)
        self.entries = Mock(return_value=[])


class StubRoster:
    def __init__(self, path):
        self.path = path
        self.initialize = Mock()
        self.close = Mock()
        self.find_member = Mock(return_value=None)
        self.find_member_any = Mock(return_value=None)
        self.group_for = Mock(return_value="")
        self.remember_mention = Mock()
        self.replace_group = Mock(return_value=0)
        self.prune_groups = Mock()
        self.stats = Mock(return_value={"groups": 0, "members": 0, "last_sync": None})


def identity_decorator(*args, **kwargs):
    return lambda function: function


def load_adapter():
    """Execute main as a unique package child and restore *all* module entries.

    patch.dict snapshots sys.modules, including pre-existing AstrBot modules;
    restoration happens on import failure too. The loaded module retains its
    stub references, but neither the fake package nor any stub stays installed.
    """
    package_name = "_persona_adapter_test_" + uuid.uuid4().hex
    package_spec = importlib.machinery.ModuleSpec(package_name, loader=None, is_package=True)
    package = importlib.util.module_from_spec(package_spec)
    package.__path__ = [str(ROOT)]

    names = (
        "astrbot", "astrbot.api", "astrbot.api.event",
        "astrbot.api.message_components", "astrbot.api.star",
        "astrbot.core", "astrbot.core.platform",
        "astrbot.core.platform.message_session", "astrbot.core.platform.message_type",
    )
    modules = {name: types.ModuleType(name) for name in names}
    for name, module in modules.items():
        module.__path__ = []
        parent_name, _, child_name = name.rpartition(".")
        if parent_name in modules:
            setattr(modules[parent_name], child_name, module)
    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api"].logger = Mock()
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].MessageChain = Mock()

    class StubResult:
        def __init__(self, chain=None):
            self.chain = chain or []

    modules["astrbot.api.event"].MessageEventResult = StubResult
    modules["astrbot.api.event"].filter = types.SimpleNamespace(
        llm_tool=identity_decorator,
        command=identity_decorator,
        event_message_type=identity_decorator,
        on_decorating_result=identity_decorator,
        EventMessageType=types.SimpleNamespace(PRIVATE_MESSAGE="private", GROUP_MESSAGE="group"),
    )
    modules["astrbot.api.message_components"].Plain = StubPlain
    modules["astrbot.api.message_components"].At = StubAt
    star = modules["astrbot.api.star"]
    star.Context = object
    star.Star = StubStar
    star.StarTools = types.SimpleNamespace(get_data_dir=lambda name=None: str(ROOT))
    star.register = identity_decorator
    modules["astrbot.core.platform.message_session"].MessageSession = StubMessageSession
    modules["astrbot.core.platform.message_type"].MessageType = StubMessageType
    engine_module = types.ModuleType(package_name + ".engine")
    engine_module.Engine = Mock(name="Engine", return_value=types.SimpleNamespace(
        start=AsyncMock(return_value={"id": "task-1", "status": "waiting", "mode": "single"}),
        status=AsyncMock(return_value=[]),
        receive=AsyncMock(return_value=False),
        cancel_target=AsyncMock(return_value=0),
        tasks_for_target=AsyncMock(return_value=[]),
    ))
    roster_module = types.ModuleType(package_name + ".roster")
    roster_module.Roster = StubRoster
    moderation_module = types.ModuleType(package_name + ".moderation")
    moderation_module.Blocklist = StubBlocklist
    optout_module = types.ModuleType(package_name + ".optout")
    optout_module.OptOut = StubOptOut
    modules[package_name] = package
    modules[engine_module.__name__] = engine_module
    modules[roster_module.__name__] = roster_module
    modules[moderation_module.__name__] = moderation_module
    modules[optout_module.__name__] = optout_module
    spec = importlib.util.spec_from_file_location(package_name + ".main", ROOT / "main.py")
    adapter = importlib.util.module_from_spec(spec)
    modules[spec.name] = adapter
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(adapter)
    return adapter


class FakeEvent:
    def __init__(self, sender="owner", platform="qq", origin="qq:GroupMessage:group-1",
                 text="帮我私聊一下，问明天几点到", self_id="bot", components=None,
                 is_at_or_wake_command=False, sender_name="云理"):
        self.sender = sender
        self.platform = platform
        self.unified_msg_origin = origin
        self.text = text
        self.self_id = self_id
        self.components = components or []
        self.is_at_or_wake_command = is_at_or_wake_command
        self.sender_name = sender_name
        self.stop_event = Mock()
        self.plain_result = Mock(side_effect=lambda text: text)
        self.extras = {}
        self.result = None

    def get_sender_name(self):
        return self.sender_name

    def set_extra(self, key, value):
        self.extras[key] = value

    def get_extra(self, key=None, default=None):
        if key is None:
            return self.extras
        return self.extras.get(key, default)

    def get_result(self):
        return self.result

    def set_result(self, result):
        self.result = result

    def get_messages(self):
        return self.components

    def get_sender_id(self):
        return self.sender

    def get_platform_id(self):
        return self.platform

    def get_self_id(self):
        return self.self_id

    def get_message_str(self):
        return self.text


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module = load_adapter()
        self.context = types.SimpleNamespace(
            get_config=Mock(return_value={}),
            get_current_chat_provider_id=AsyncMock(return_value="active-provider"),
        )
        self.config = {}
        self.adapter = self.module.PersonaDelegate(self.context, self.config)
        self.engine = self.adapter.engine
        self.event = FakeEvent()
        self.adapter._persona = AsyncMock(return_value=("chosen-persona", "persona prompt"))

    def authorize(self):
        self.config["allowed_users"] = ["qq:owner"]
        self.config["contacts"] = ["Alice=qq:FriendMessage:friend"]

    async def test_default_allows_everyone_to_use_tools(self):
        self.assertTrue(self.adapter._allowed(self.event))
        self.adapter._require(self.event)
        result = await self.adapter.private_chat(self.event, "123456", "hello")
        self.assertNotIn("未执行", result)
        self.engine.start.assert_awaited_once()
        self.adapter._persona.assert_awaited_once()

    async def test_blocked_user_is_refused(self):
        self.adapter.blocklist.is_blocked.return_value = True
        self.assertFalse(self.adapter._allowed(self.event))
        with self.assertRaisesRegex(ValueError, "禁止使用代聊"):
            self.adapter._require(self.event)
        self.assertIn("禁止使用代聊", await self.adapter.private_chat(self.event, "123456", "hello"))
        self.assertIn("禁止使用代聊", await self.adapter.control(self.event, "status"))
        self.engine.start.assert_not_awaited()

    async def test_config_blocklist_matches_sender_or_platform_qualified(self):
        self.config["blocked_users"] = ["other:owner"]
        self.assertTrue(self.adapter._allowed(self.event))
        self.config["blocked_users"] = ["qq:owner"]
        self.assertFalse(self.adapter._allowed(self.event))
        self.config["blocked_users"] = ["owner"]
        self.assertFalse(self.adapter._allowed(self.event))

    async def test_admin_bypasses_blocklist(self):
        self.context.get_config.return_value = {"admins_id": ["owner"]}
        self.adapter.blocklist.is_blocked.return_value = True
        self.assertTrue(self.adapter._allowed(self.event))
        self.adapter._require(self.event)

    async def test_non_admin_cannot_manage_blocklist(self):
        result = await self.adapter.control(self.event, "ban", target="123")
        self.assertIn("只有管理员", result)
        self.adapter.blocklist.add.assert_not_called()

    async def test_admin_can_ban_and_unban(self):
        self.context.get_config.return_value = {"admins_id": ["owner"]}
        result = await self.adapter.control(self.event, "ban", target="123")
        self.assertIn("已拉黑", result)
        self.adapter.blocklist.add.assert_called_once_with("qq", "123", "qq:owner")
        self.adapter.blocklist.remove.return_value = True
        result = await self.adapter.control(self.event, "unban", target="123")
        self.assertIn("已解除拉黑", result)
        self.adapter.blocklist.remove.assert_called_once_with("qq", "123")

    async def test_admin_cannot_ban_self_or_other_admin(self):
        self.context.get_config.return_value = {"admins_id": ["owner", "999"]}
        self.assertIn("不能拉黑管理员", await self.adapter.control(self.event, "ban", target="owner"))
        self.assertIn("不能拉黑管理员", await self.adapter.control(self.event, "ban", target="999"))
        self.adapter.blocklist.add.assert_not_called()

    async def test_admin_can_list_blocklist(self):
        self.context.get_config.return_value = {"admins_id": ["owner"]}
        self.adapter.blocklist.entries.return_value = [
            {"platform": "qq", "account": "123", "added_by": "qq:owner", "added_at": 1.0},
        ]
        result = await self.adapter.control(self.event, "blocked")
        self.assertEqual(json.loads(result), [{"platform": "qq", "account": "123", "by": "qq:owner"}])

    async def test_blocklist_can_be_managed_with_chat_command(self):
        self.context.get_config.return_value = {"admins_id": ["owner"]}
        event = FakeEvent(text="/代聊 拉黑 456")
        results = [item async for item in self.adapter.chat_command(event)]
        self.assertIn("已拉黑", results[-1])
        self.adapter.blocklist.add.assert_called_once_with("qq", "456", "qq:owner")

    async def test_permission_accepts_admin_ids_and_normalizes_numeric_ids(self):
        self.context.get_config.return_value = {"admins_id": [123]}
        event = FakeEvent(sender=123)
        self.assertTrue(self.adapter._is_admin(event))
        self.assertTrue(self.adapter._allowed(event))
        self.adapter._require(event)
        self.context.get_config.assert_called_with(event.unified_msg_origin)

    async def test_contacts_keep_only_explicit_nonempty_private_sessions(self):
        self.config["contacts"] = [
            " Alice = qq:FriendMessage:friend ",
            "Group=qq:GroupMessage:group", "Empty=qq:FriendMessage:",
            "MissingSeparator", " =qq:FriendMessage:x", "Bad=invalid",
        ]
        self.assertEqual(self.adapter._contacts(), {"Alice": "qq:FriendMessage:friend"})

    async def test_configured_alias_and_exact_target_allowed_with_raw_disabled(self):
        self.authorize()
        for target in (" Alice ", "qq:FriendMessage:friend"):
            with self.subTest(target=target):
                self.assertEqual(self.adapter._target(self.event, target),
                                 ("qq:FriendMessage:friend", "Alice"))

    async def test_unconfigured_raw_targets_are_denied_by_default_and_when_disabled(self):
        for enabled in (None, False):
            if enabled is not None:
                self.config["allow_raw_target"] = enabled
            for target in ("friend", "qq:FriendMessage:friend", "Someone"):
                with self.subTest(enabled=enabled, target=target):
                    with self.assertRaisesRegex(ValueError, "联系人未配置"):
                        self.adapter._target(self.event, target)

    async def test_enabled_raw_target_accepts_account_and_explicit_private_session(self):
        self.config["allow_raw_target"] = True
        self.assertEqual(self.adapter._target(self.event, " friend "),
                         ("qq:FriendMessage:friend", "friend"))
        self.assertEqual(self.adapter._target(self.event, "other:FriendMessage:42"),
                         ("other:FriendMessage:42", "other:FriendMessage:42"))

    async def test_raw_group_empty_and_ambiguous_targets_are_rejected(self):
        self.config["allow_raw_target"] = True
        for target in ("qq:GroupMessage:group", "qq:FriendMessage:", "", "two words"):
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    self.adapter._target(self.event, target)

    async def test_group_contact_cannot_bypass_private_only_rule(self):
        self.config.update(contacts=["Group=qq:GroupMessage:group"], allow_raw_target=False)
        with self.assertRaises(ValueError):
            self.adapter._target(self.event, "Group")
        self.config["allow_raw_target"] = True
        with self.assertRaises(ValueError):
            self.adapter._target(self.event, "qq:GroupMessage:group")

    async def test_report_always_targets_initiator_private_session(self):
        self.authorize()
        for origin in ("qq:GroupMessage:group-1", "qq:FriendMessage:unrelated"):
            with self.subTest(origin=origin):
                event = FakeEvent(origin=origin)
                result = json.loads(await self.adapter.private_chat(event, "Alice", "ask time"))
                kwargs = self.engine.start.await_args.kwargs
                self.assertEqual(kwargs["report_to"], "qq:FriendMessage:owner")
                self.assertEqual(kwargs["origin"], origin)
                self.assertEqual(kwargs["owner_key"], "qq:owner")
                self.assertEqual(kwargs["target"], "qq:FriendMessage:friend")
                self.assertEqual(kwargs["target_name"], "Alice")
                self.assertEqual(kwargs["goal"], "ask time")
                self.assertEqual(result["id"], "task-1")

    async def test_self_target_is_rejected_before_persona_and_engine(self):
        self.authorize()
        self.config["contacts"] = ["Me=qq:FriendMessage:owner"]
        result = await self.adapter.private_chat(self.event, "Me", "hello")
        self.assertIn("不能向发起人或机器人自己", result)
        self.adapter._persona.assert_not_awaited()
        self.engine.start.assert_not_awaited()

    async def test_current_persona_and_provider_are_forwarded(self):
        self.authorize()
        await self.adapter.private_chat(self.event, "Alice", "hello")
        self.adapter._persona.assert_awaited_once_with(self.event, "")
        kwargs = self.engine.start.await_args.kwargs
        self.assertEqual(kwargs["persona_id"], "chosen-persona")
        self.assertEqual(kwargs["persona_prompt"], "persona prompt")
        self.assertEqual(kwargs["provider_id"], "active-provider")
        self.context.get_current_chat_provider_id.assert_awaited_once_with(self.event.unified_msg_origin)

    async def test_explicit_persona_and_configured_provider_are_forwarded(self):
        self.authorize()
        self.config["provider_id"] = " configured-provider "
        self.adapter._persona.return_value = ("saved-persona", "saved prompt")
        await self.adapter.private_chat(self.event, "Alice", "hello", " saved-id ")
        self.adapter._persona.assert_awaited_once_with(self.event, "saved-id")
        kwargs = self.engine.start.await_args.kwargs
        self.assertEqual(kwargs["persona_id"], "saved-persona")
        self.assertEqual(kwargs["persona_prompt"], "saved prompt")
        self.assertEqual(kwargs["provider_id"], "configured-provider")
        self.context.get_current_chat_provider_id.assert_not_awaited()

    async def test_status_is_a_metadata_allowlist_without_any_body(self):
        self.authorize()
        metadata = dict(id="task-1", status="waiting",
                        target_name="Alice", turns=1, deadline=123)
        self.engine.status.return_value = [{
            **metadata, "goal": "SECRET_GOAL", "persona_prompt": "SECRET_PERSONA",
            "messages": [{"content": "SECRET_BODY"}], "reply": "SECRET_REPLY",
            "report_to": "SECRET_REPORT", "unknown_future_field": "SECRET_FUTURE",
        }]
        result = await self.adapter.control(self.event, "status")
        self.assertEqual(json.loads(result), [metadata])
        self.assertNotIn("SECRET", result)
        self.engine.status.assert_awaited_once_with("qq:owner")

    async def test_private_reply_ignores_self_even_if_sender_ids_have_different_types(self):
        event = FakeEvent(sender=123, self_id="123")
        await self.adapter.on_private_reply(event)
        self.engine.receive.assert_not_awaited()
        event.stop_event.assert_not_called()

    async def test_private_reply_ignores_empty_or_media_only_text(self):
        for text in ("", " \n\t "):
            with self.subTest(text=text):
                event = FakeEvent(text=text)
                await self.adapter.on_private_reply(event)
                event.stop_event.assert_not_called()
        self.engine.receive.assert_not_awaited()

    async def test_private_reply_is_recorded_but_still_flows_to_normal_chat(self):
        self.engine.receive.return_value = True
        event = FakeEvent(text="  " + "x" * 5000 + "  ")
        await self.adapter.on_private_reply(event)
        self.engine.receive.assert_awaited_once_with(event.unified_msg_origin, "x" * 4000)
        # 对方回复后相当于对方发起对话，不能被插件拦截。
        event.stop_event.assert_not_called()

    async def test_unknown_private_reply_also_flows_to_normal_chat(self):
        self.engine.receive.return_value = False
        event = FakeEvent(text="  hello  ")
        await self.adapter.on_private_reply(event)
        self.engine.receive.assert_awaited_once_with(event.unified_msg_origin, "hello")
        event.stop_event.assert_not_called()

    async def test_followup_settings_are_passed_to_engine(self):
        module = load_adapter()
        context = types.SimpleNamespace(
            get_config=Mock(return_value={}),
            get_current_chat_provider_id=AsyncMock(return_value="provider"),
        )
        module.PersonaDelegate(context, {"followup_window_seconds": 60, "followup_max_relays": 3})
        kwargs = module.Engine.call_args.kwargs
        self.assertEqual(kwargs["followup_window"], 60)
        self.assertEqual(kwargs["followup_max"], 3)
        self.assertIs(kwargs["persona_status_reports"], True)
        self.assertEqual(kwargs["timeout_seconds"], 300)

    async def test_loading_restores_preexisting_modules_and_leaves_no_fake_package(self):
        sentinel = types.ModuleType("astrbot.api")
        with patch.dict(sys.modules, {"astrbot.api": sentinel}):
            before = dict(sys.modules)
            adapter = load_adapter()
            self.assertIs(sys.modules["astrbot.api"], sentinel)
            self.assertEqual(set(sys.modules), set(before))
            for name, original in before.items():
                self.assertIs(sys.modules[name], original, name)
            self.assertNotIn(adapter.__name__, sys.modules)
            self.assertNotIn(adapter.__package__ + ".engine", sys.modules)
            self.assertNotIn(adapter.__package__ + ".roster", sys.modules)
            self.assertNotIn(adapter.__package__ + ".moderation", sys.modules)
            self.assertNotIn(adapter.__package__ + ".optout", sys.modules)


class PersonaAndTransportTests(unittest.IsolatedAsyncioTestCase):
    """Exercise the real adapter methods with only external services mocked."""

    def setUp(self):
        self.module = load_adapter()
        self.manager = types.SimpleNamespace(
            get_persona_v3_by_id=Mock(return_value={"name": "Saved", "prompt": " saved prompt "}),
            resolve_selected_persona=AsyncMock(return_value=(
                None, {"name": "Default", "prompt": " default prompt "}, None, None,
            )),
        )
        self.conversations = types.SimpleNamespace(
            get_curr_conversation_id=AsyncMock(return_value="conversation-1"),
            get_conversation=AsyncMock(return_value=types.SimpleNamespace(persona_id="selected-id")),
        )
        self.context = types.SimpleNamespace(
            persona_manager=self.manager,
            conversation_manager=self.conversations,
            send_message=AsyncMock(return_value=True),
            llm_generate=AsyncMock(return_value=types.SimpleNamespace(completion_text=" generated ")),
        )
        self.adapter = self.module.PersonaDelegate(self.context, {})
        self.event = FakeEvent()
        self.event.get_platform_name = Mock(return_value="aiocqhttp")

    async def test_explicit_persona_id_loads_saved_persona_without_resolving_conversation(self):
        result = await self.adapter._persona(self.event, "saved-id")
        self.assertEqual(result, ("Saved", "saved prompt"))
        self.manager.get_persona_v3_by_id.assert_called_once_with("saved-id")
        self.manager.resolve_selected_persona.assert_not_awaited()
        self.conversations.get_curr_conversation_id.assert_not_awaited()
        self.conversations.get_conversation.assert_not_awaited()

    async def test_nonexistent_explicit_persona_is_rejected(self):
        self.manager.get_persona_v3_by_id.return_value = None
        with self.assertRaisesRegex(ValueError, "指定的人格不存在"):
            await self.adapter._persona(self.event, "missing-id")
        self.manager.get_persona_v3_by_id.assert_called_once_with("missing-id")
        self.manager.resolve_selected_persona.assert_not_awaited()

    async def test_current_conversation_persona_is_passed_to_platform_resolution(self):
        self.assertEqual(await self.adapter._persona(self.event, ""), ("Default", "default prompt"))
        self.conversations.get_curr_conversation_id.assert_awaited_once_with(self.event.unified_msg_origin)
        self.conversations.get_conversation.assert_awaited_once_with(
            self.event.unified_msg_origin, "conversation-1",
        )
        self.manager.resolve_selected_persona.assert_awaited_once_with(
            umo=self.event.unified_msg_origin,
            conversation_persona_id="selected-id", platform_name="aiocqhttp",
        )
        self.manager.get_persona_v3_by_id.assert_not_called()

    async def test_no_current_conversation_resolves_default_without_fetching_conversation(self):
        self.conversations.get_curr_conversation_id.return_value = None
        self.assertEqual(await self.adapter._persona(self.event, ""), ("Default", "default prompt"))
        self.conversations.get_conversation.assert_not_awaited()
        self.manager.resolve_selected_persona.assert_awaited_once_with(
            umo=self.event.unified_msg_origin,
            conversation_persona_id=None, platform_name="aiocqhttp",
        )

    async def test_missing_conversation_or_unset_persona_resolves_default(self):
        for conversation in (None, types.SimpleNamespace(), types.SimpleNamespace(persona_id=None)):
            with self.subTest(conversation=conversation):
                self.conversations.get_conversation.return_value = conversation
                self.manager.resolve_selected_persona.reset_mock()
                result = await self.adapter._persona(self.event, "")
                self.assertEqual(result, ("Default", "default prompt"))
                self.manager.resolve_selected_persona.assert_awaited_once_with(
                    umo=self.event.unified_msg_origin,
                    conversation_persona_id=None, platform_name="aiocqhttp",
                )

    async def test_disabled_resolved_persona_none_is_rejected(self):
        self.conversations.get_conversation.return_value = types.SimpleNamespace(persona_id="[none]")
        self.manager.resolve_selected_persona.return_value = (None, None, None, None)
        with self.assertRaisesRegex(ValueError, "当前会话未启用可读取的保存人格"):
            await self.adapter._persona(self.event, "")
        self.manager.resolve_selected_persona.assert_awaited_once_with(
            umo=self.event.unified_msg_origin,
            conversation_persona_id="[none]", platform_name="aiocqhttp",
        )

    async def test_empty_persona_prompt_is_rejected_for_explicit_and_current_personas(self):
        for prompt in ("", " \n\t "):
            for persona_id in ("saved-id", ""):
                with self.subTest(prompt=prompt, persona_id=persona_id):
                    data = {"name": "Empty", "prompt": prompt}
                    self.manager.get_persona_v3_by_id.return_value = data
                    self.manager.resolve_selected_persona.return_value = (None, data, None, None)
                    with self.assertRaisesRegex(ValueError, "人格提示词为空"):
                        await self.adapter._persona(self.event, persona_id)

    async def test_persona_style_extra_is_appended_to_the_prompt(self):
        self.adapter.config["persona_style_extra"] = "称呼我主人，句尾带～"
        self.manager.get_persona_v3_by_id.return_value = {"name": "S", "prompt": " base "}
        _, prompt = await self.adapter._persona(self.event, "saved-id")
        self.assertIn("额外风格要求", prompt)
        self.assertIn("称呼我主人，句尾带～", prompt)

    async def test_persona_style_examples_are_appended_and_capped(self):
        for examples in ("example dialogue", "x" * 9000):
            with self.subTest(length=len(examples)):
                self.manager.get_persona_v3_by_id.return_value = {
                    "name": "Styled", "prompt": " base prompt ",
                    "_mood_imitation_dialogs_processed": examples,
                }
                self.assertEqual(
                    await self.adapter._persona(self.event, "saved-id"),
                    ("Styled", "base prompt\n人格风格示例：\n" + examples[:8000]),
                )

    async def test_explicit_persona_without_name_falls_back_to_requested_id(self):
        self.manager.get_persona_v3_by_id.return_value = {"prompt": "prompt"}
        self.assertEqual(await self.adapter._persona(self.event, "saved-id"), ("saved-id", "prompt"))

    async def test_send_false_raises_instead_of_claiming_delivery(self):
        self.context.send_message.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未发送消息"):
            await self.adapter._send("qq:FriendMessage:friend", "hello")
        self.context.send_message.assert_awaited_once()
        chain = self.module.MessageChain.call_args.kwargs["chain"]
        self.assertEqual(chain[0].text, "hello")

    async def test_generate_rejects_empty_none_and_whitespace_completion(self):
        for completion in (None, "", " \n\t "):
            with self.subTest(completion=completion):
                self.context.llm_generate.return_value = types.SimpleNamespace(completion_text=completion)
                with self.assertRaisesRegex(ValueError, "模型未返回文本"):
                    await self.adapter._generate("provider", "system", "prompt")
                self.context.llm_generate.assert_awaited_with(
                    chat_provider_id="provider", system_prompt="system", prompt="prompt",
                )

    async def test_generate_returns_trimmed_nonempty_completion(self):
        self.assertEqual(await self.adapter._generate("provider", "system", "prompt"), "generated")
        self.context.llm_generate.assert_awaited_once_with(
            chat_provider_id="provider", system_prompt="system", prompt="prompt",
        )


class ChannelAndGroupTests(unittest.IsolatedAsyncioTestCase):
    """Private requires an explicit private word; otherwise the bot @s in-group."""

    def setUp(self):
        self.module = load_adapter()
        self.context = types.SimpleNamespace(
            get_config=Mock(return_value={}),
            get_current_chat_provider_id=AsyncMock(return_value="provider-1"),
            send_message=AsyncMock(return_value=True),
            get_platform_inst=Mock(return_value=None),
        )
        self.config = {"allowed_users": ["qq:owner"],
                       "contacts": ["Alice=qq:FriendMessage:friend"]}
        self.adapter = self.module.PersonaDelegate(self.context, self.config)
        self.adapter._persona = AsyncMock(return_value=("人格", "人格提示词"))
        self.adapter._generate = AsyncMock(return_value="明天下午三点集合，别迟到。")
        self.engine = self.adapter.engine

    def group_event(self, text, components=None):
        return FakeEvent(origin="qq:GroupMessage:g1", text=text, components=components or [])

    def dm_event(self, text, components=None):
        return FakeEvent(origin="qq:FriendMessage:owner", text=text, components=components or [])

    def sent_chain(self):
        return self.module.MessageChain.call_args.kwargs["chain"]

    async def test_group_message_without_private_word_at_mentions_in_group(self):
        event = self.group_event("帮我跟 @小明 说一声，明天下午三点集合",
                                 [self.module.At(qq="42", name="小明")])
        result = json.loads(await self.adapter.private_chat(event, "@小明", "明天下午三点集合"))
        self.assertEqual(result["channel"], "group")
        self.assertTrue(result["sent"])
        self.engine.start.assert_not_awaited()
        chain = self.sent_chain()
        self.assertIsInstance(chain[0], self.module.At)
        self.assertEqual(chain[0].qq, "42")
        self.assertIsNone(chain[0].name)
        self.assertEqual(chain[1].text, " 明天下午三点集合，别迟到。")
        self.context.send_message.assert_awaited_once()
        self.assertEqual(self.context.send_message.await_args.args[0], "qq:GroupMessage:g1")

    async def test_private_word_in_group_uses_private_session_and_report_to_owner(self):
        event = self.group_event("帮我私聊一下 @小明，问明天几点到",
                                 [self.module.At(qq="42", name="小明")])
        result = json.loads(await self.adapter.private_chat(event, "@小明", "问明天几点到"))
        self.assertEqual(result["channel"], "private")
        self.assertEqual(result["target"], "小明")
        kwargs = self.engine.start.await_args.kwargs
        self.assertEqual(kwargs["target"], "qq:FriendMessage:42")
        self.assertEqual(kwargs["report_to"], "qq:FriendMessage:owner")
        self.assertEqual(kwargs["origin"], "qq:GroupMessage:g1")
        self.context.send_message.assert_not_awaited()

    async def test_explicit_private_without_private_word_is_refused_not_posted(self):
        event = self.group_event("帮我跟 @小明 说一声", [self.module.At(qq="42", name="小明")])
        result = await self.adapter.private_chat(event, "@小明", "说一声", channel="private")
        self.assertIn("未执行代聊", result)
        self.assertIn("私聊", result)
        self.engine.start.assert_not_awaited()
        self.context.send_message.assert_not_awaited()

    async def test_private_chat_initiator_can_speak_in_dm(self):
        event = self.dm_event("帮我私聊一下张三，问明天几点到")
        result = json.loads(await self.adapter.private_chat(event, "123456", "问明天几点到"))
        self.assertEqual(result["channel"], "private")
        kwargs = self.engine.start.await_args.kwargs
        self.assertEqual(kwargs["target"], "qq:FriendMessage:123456")
        self.assertEqual(kwargs["owner_key"], "qq:owner")
        self.assertEqual(kwargs["owner_name"], "云理")
        self.assertEqual(kwargs["report_to"], "qq:FriendMessage:owner")

    async def test_dm_without_private_word_asks_for_clarification(self):
        event = self.dm_event("帮我跟张三说一声")
        result = await self.adapter.private_chat(event, "123456", "说一声")
        self.assertIn("未执行代聊", result)
        self.assertIn("私聊", result)
        self.engine.start.assert_not_awaited()
        self.context.send_message.assert_not_awaited()

    async def test_group_channel_outside_group_is_refused(self):
        event = self.dm_event("在群里@他一下")
        result = await self.adapter.private_chat(event, "123456", "说一声", channel="group")
        self.assertIn("未执行代聊", result)
        self.engine.start.assert_not_awaited()

    async def test_multiple_mentions_are_ambiguous(self):
        event = self.group_event("@小明 @小红 明天见", [
            self.module.At(qq="42", name="小明"), self.module.At(qq="43", name="小红"),
        ])
        result = await self.adapter.private_chat(event, "", "明天见")
        self.assertIn("多个 @", result)
        self.engine.start.assert_not_awaited()
        self.context.send_message.assert_not_awaited()

    async def test_bot_and_all_mentions_are_ignored(self):
        event = self.group_event("@机器人 @所有人 @小明 明天见", [
            self.module.At(qq="bot", name="机器人"), self.module.At(qq="all", name="所有人"),
            self.module.At(qq="42", name="小明"),
        ])
        result = json.loads(await self.adapter.private_chat(event, "", "明天见"))
        self.assertEqual(result["channel"], "group")
        self.assertEqual(self.sent_chain()[0].qq, "42")

    async def test_group_send_failure_is_reported_without_private_task(self):
        self.context.send_message.return_value = False
        event = self.group_event("@小明 明天见", [self.module.At(qq="42", name="小明")])
        result = await self.adapter.private_chat(event, "", "明天见")
        self.assertIn("未发送", result)
        self.engine.start.assert_not_awaited()

    async def test_group_send_exception_is_reported_as_not_sent(self):
        self.context.send_message.side_effect = RuntimeError("platform boom")
        event = self.group_event("@小明 明天见", [self.module.At(qq="42", name="小明")])
        result = await self.adapter.private_chat(event, "", "明天见")
        self.assertIn("未发送", result)
        self.engine.start.assert_not_awaited()

    async def test_group_at_uses_only_the_account_id(self):
        event = self.group_event("@42 明天见", [self.module.At(qq="42", name="小明")])
        await self.adapter.private_chat(event, "", "明天见")
        chain = self.sent_chain()
        self.assertEqual(chain[0].qq, "42")
        self.assertIsNone(chain[0].name)

    async def test_numeric_account_is_a_valid_target_without_contacts(self):
        event = self.dm_event("帮我私聊一下 123456，问明天几点到")
        await self.adapter.private_chat(event, "123456", "问明天几点到")
        self.assertEqual(self.engine.start.await_args.kwargs["target"],
                         "qq:FriendMessage:123456")

    async def test_contact_alias_resolves_to_its_private_account_in_group(self):
        event = self.group_event("@Alice 明天见", [self.module.At(qq="friend", name="Alice")])
        await self.adapter.private_chat(event, "Alice", "明天见")
        self.assertEqual(self.sent_chain()[0].qq, "friend")

    async def test_targeting_the_initiator_is_refused(self):
        event = self.group_event("@x 明天见", [self.module.At(qq="owner", name="x")])
        result = await self.adapter.private_chat(event, "", "明天见")
        self.assertIn("不能向发起人或机器人自己", result)
        self.engine.start.assert_not_awaited()
        self.context.send_message.assert_not_awaited()

    async def test_negated_group_phrase_does_not_override_private_word(self):
        event = self.group_event("私聊他，别在群里说，问明天几点到",
                                 [self.module.At(qq="42", name="小明")])
        await self.adapter.private_chat(event, "@小明", "问明天几点到")
        self.assertEqual(self.engine.start.await_args.kwargs["target"],
                         "qq:FriendMessage:42")

    async def test_explicit_group_phrase_selects_group_even_without_mention_text(self):
        event = self.group_event("在群里@他一下，问明天几点到",
                                 [self.module.At(qq="42", name="小明")])
        result = json.loads(await self.adapter.private_chat(event, "", "问明天几点到"))
        self.assertEqual(result["channel"], "group")

    async def test_initialize_sets_up_roster_synchronously_and_starts_engine(self):
        self.engine.initialize = AsyncMock()
        self.engine.tick = AsyncMock()
        self.engine.close = AsyncMock()
        await self.adapter.initialize()
        self.adapter.roster.initialize.assert_called_once_with()
        self.engine.initialize.assert_awaited_once()
        await self.adapter.terminate()
        self.engine.close.assert_awaited_once()

    async def test_group_nickname_resolves_to_member_account(self):
        self.adapter.roster.find_member.return_value = {
            "qq": "777", "nickname": "小明", "card": "", "group_id": "g1",
        }
        event = self.group_event("帮我私聊一下小明，问明天几点到")
        result = json.loads(await self.adapter.private_chat(event, "小明", "问明天几点到"))
        self.assertEqual(result["channel"], "private")
        kwargs = self.engine.start.await_args.kwargs
        self.assertEqual(kwargs["target"], "qq:FriendMessage:777")
        self.adapter.roster.find_member.assert_called_once_with("qq", "g1", "小明")
        self.assertEqual(self.adapter._group_hints["qq:FriendMessage:777"], "g1")

    async def test_dm_unique_nickname_resolves_across_groups(self):
        self.adapter.roster.find_member_any.return_value = {
            "qq": "888", "nickname": "小红", "card": "", "group_id": "g2",
        }
        event = self.dm_event("帮我私聊一下小红，问几点到")
        await self.adapter.private_chat(event, "小红", "问几点到")
        self.assertEqual(self.engine.start.await_args.kwargs["target"], "qq:FriendMessage:888")
        self.adapter.roster.find_member_any.assert_called_once_with("qq", "小红")

    async def test_group_at_bot_records_mentioned_members(self):
        event = self.group_event("私聊一下 @小明", [self.module.At(qq="42", name="小明")])
        event.is_at_or_wake_command = True
        await self.adapter.on_group_mention(event)
        self.adapter.roster.remember_mention.assert_called_once_with("qq", "g1", "42", "小明")

    async def test_group_message_without_at_bot_does_not_record(self):
        event = self.group_event("私聊一下 @小明", [self.module.At(qq="42", name="小明")])
        await self.adapter.on_group_mention(event)
        self.adapter.roster.remember_mention.assert_not_called()

    async def test_private_send_falls_back_to_group_temp_session(self):
        self.context.send_message.side_effect = RuntimeError("not friend")
        client = types.SimpleNamespace(call_action=AsyncMock())
        self.context.get_platform_inst = Mock(
            return_value=types.SimpleNamespace(get_client=Mock(return_value=client)),
        )
        self.adapter._group_hints["qq:FriendMessage:777"] = "100"
        await self.adapter._send("qq:FriendMessage:777", "hi")
        client.call_action.assert_awaited_once_with(
            "send_private_msg", user_id=777, group_id=100,
            message=[{"type": "text", "data": {"text": "hi"}}],
        )

    async def test_private_send_success_does_not_use_temp_session(self):
        self.adapter._group_hints["qq:FriendMessage:777"] = "g1"
        await self.adapter._send("qq:FriendMessage:777", "hi")
        self.context.get_platform_inst.assert_not_called()

    async def test_private_send_without_any_group_hint_raises(self):
        self.context.send_message.side_effect = RuntimeError("not friend")
        with self.assertRaisesRegex(RuntimeError, "未发送消息"):
            await self.adapter._send("qq:FriendMessage:777", "hi")

    async def test_roster_sync_reads_groups_and_members(self):
        client = types.SimpleNamespace(call_action=AsyncMock(side_effect=[
            [{"group_id": 100, "group_name": "测试群"}],
            [{"user_id": 42, "nickname": "小明", "card": ""}],
        ]))
        platform = types.SimpleNamespace(
            meta=Mock(return_value=types.SimpleNamespace(id="qq")),
            get_client=Mock(return_value=client),
        )
        self.context.platform_manager = types.SimpleNamespace(platform_insts=[platform])
        self.adapter.roster.replace_group.return_value = 1
        result = await self.adapter.sync_roster()
        self.assertEqual(result, {"groups": 1, "members": 1, "platforms": 1})
        self.adapter.roster.replace_group.assert_called_once_with(
            "qq", "100", "测试群", [{"qq": "42", "nickname": "小明", "card": ""}],
        )
        self.adapter.roster.prune_groups.assert_called_once_with("qq", ["100"])

    async def test_daily_roster_sync_runs_once_at_configured_hour(self):
        self.config["roster_sync_hour"] = 4
        self.adapter.sync_roster = AsyncMock(
            return_value={"groups": 1, "members": 1, "platforms": 1},
        )

        class FakeNow:
            hour = 4

            def date(self):
                return types.SimpleNamespace(isoformat=lambda: "2026-10-26")

        self.module.datetime = types.SimpleNamespace(now=lambda tz=None: FakeNow())
        await self.adapter._maybe_sync_roster()
        await self.adapter._maybe_sync_roster()
        self.adapter.sync_roster.assert_awaited_once()
        self.assertEqual(self.adapter._last_sync_day, "2026-10-26")

    async def test_roster_sync_retries_when_platform_not_ready(self):
        self.config["roster_sync_hour"] = 4
        self.adapter.sync_roster = AsyncMock(
            return_value={"groups": 0, "members": 0, "platforms": 0},
        )
        self.adapter._last_sync_attempt = 0

        class FakeNow:
            hour = 4

            def date(self):
                return types.SimpleNamespace(isoformat=lambda: "2026-10-26")

        self.module.datetime = types.SimpleNamespace(now=lambda tz=None: FakeNow())
        await self.adapter._maybe_sync_roster()
        self.assertIsNone(self.adapter._last_sync_day)
        await self.adapter._maybe_sync_roster()  # 60 秒节流内不重复尝试
        self.adapter.sync_roster.assert_awaited_once()

    async def test_daily_roster_sync_skips_other_hours(self):
        self.adapter.sync_roster = AsyncMock()

        class FakeNow:
            hour = 5

            def date(self):
                return types.SimpleNamespace(isoformat=lambda: "2026-10-26")

        self.module.datetime = types.SimpleNamespace(now=lambda tz=None: FakeNow())
        await self.adapter._maybe_sync_roster()
        self.adapter.sync_roster.assert_not_awaited()

    async def test_td_temporary_optout_notifies_initiator_in_persona_style(self):
        self.engine.tasks_for_target.return_value = [{
            "id": "t1", "report_to": "qq:FriendMessage:owner", "target_name": "小明",
            "persona_prompt": "人格提示", "provider_id": "provider-1",
        }]
        self.adapter._generate = AsyncMock(return_value="小明说接下来两小时不想被打扰～")
        event = self.dm_event("/td 2小时")
        results = [item async for item in self.adapter.td_command(event)]
        self.assertIn("已退订私聊（2小时）", results[-1])
        self.adapter.optout.set.assert_called_once_with("qq", "owner", 7200.0)
        self.engine.cancel_target.assert_awaited_once_with(
            event.unified_msg_origin, "对方已退订私聊，任务停止。", notify=False,
        )
        self.context.send_message.assert_awaited_once()
        self.assertEqual(self.context.send_message.await_args.args[0], "qq:FriendMessage:owner")
        self.assertEqual(self.module.MessageChain.call_args.kwargs["chain"][0].text,
                         "小明说接下来两小时不想被打扰～")
        event.stop_event.assert_called_once_with()

    async def test_td_permanent_and_cancel(self):
        event = self.dm_event("/td 永久")
        results = [item async for item in self.adapter.td_command(event)]
        self.assertIn("永久", results[-1])
        self.adapter.optout.set.assert_called_once_with("qq", "owner", 0.0)
        self.adapter.optout.clear.return_value = True
        event = self.dm_event("/td 取消")
        results = [item async for item in self.adapter.td_command(event)]
        self.assertIn("已恢复私聊", results[-1])
        self.adapter.optout.clear.assert_called_once_with("qq", "owner")

    async def test_td_invalid_and_group_usage(self):
        event = self.dm_event("/td 5分钟")
        results = [item async for item in self.adapter.td_command(event)]
        self.assertIn("用法", results[-1])
        self.adapter.optout.set.assert_not_called()
        event = self.group_event("/td 2小时")
        results = [item async for item in self.adapter.td_command(event)]
        self.assertIn("请在私聊里", results[-1])
        self.adapter.optout.set.assert_not_called()

    async def test_private_task_to_opted_out_target_is_refused(self):
        self.adapter.optout.status.return_value = {
            "platform": "qq", "account": "123456", "until": time.time() + 7200, "created": 0,
        }
        event = self.group_event("帮我私聊一下 123456，问明天几点到")
        result = await self.adapter.private_chat(event, "123456", "问明天几点到")
        self.assertIn("对方已退订私聊", result)
        self.assertIn("还剩", result)
        self.assertIn("小时", result)
        self.engine.start.assert_not_awaited()

    async def test_group_at_to_opted_out_target_still_allowed(self):
        self.adapter.optout.is_opted_out.return_value = True
        event = self.group_event("帮我跟 @小明 说一声明天见",
                                 [self.module.At(qq="42", name="小明")])
        result = json.loads(await self.adapter.private_chat(event, "", "明天见"))
        self.assertEqual(result["channel"], "group")
        self.context.send_message.assert_awaited_once()

    async def test_opted_out_initiator_cannot_start_private_task(self):
        self.adapter.optout.is_opted_out.side_effect = lambda platform, account: account == "owner"
        event = self.dm_event("帮我私聊一下 123456，问几点到")
        result = await self.adapter.private_chat(event, "123456", "问几点到")
        self.assertIn("你已退订私聊", result)
        self.engine.start.assert_not_awaited()

    async def test_send_is_blocked_for_opted_out_target(self):
        self.adapter.optout.is_opted_out.return_value = True
        with self.assertRaisesRegex(RuntimeError, "已退订私聊"):
            await self.adapter._send("qq:FriendMessage:777", "hi")
        self.context.send_message.assert_not_awaited()

    async def test_private_reply_ignores_td_command(self):
        event = self.dm_event("/td 2小时")
        await self.adapter.on_private_reply(event)
        self.engine.receive.assert_not_awaited()

    async def test_remaining_label_formats_days_hours_minutes_and_permanent(self):
        now = time.time()
        self.assertEqual(self.adapter._remaining_label(0), "永久")
        self.assertIn("天", self.adapter._remaining_label(now + 2 * 86400))
        self.assertIn("小时", self.adapter._remaining_label(now + 7200))
        self.assertIn("分钟", self.adapter._remaining_label(now + 90))

    async def test_group_prompt_names_the_caller_as_subject(self):
        event = self.group_event("@小明 明天见", [self.module.At(qq="42", name="小明")])
        await self.adapter.private_chat(event, "", "明天见")
        first = self.adapter._generate.await_args_list[0]
        system, prompt = first.args[1], first.args[2]
        self.assertIn("云理", system)
        self.assertIn("第一人称", system)
        self.assertIn("绝不能指你", system)
        self.assertIn("云理", prompt)

    async def test_private_chat_sets_persona_confirmation_for_the_reply(self):
        self.adapter._generate = AsyncMock(return_value="主人～已经私聊小明，让他三小时后找你啦～")
        event = self.group_event("帮我私聊一下 123456，告诉他三小时后找我")
        await self.adapter.private_chat(event, "123456", "告诉他三小时后找我")
        self.assertEqual(event.get_extra("persona_delegate_reply"),
                         "主人～已经私聊小明，让他三小时后找你啦～")

    async def test_group_chat_sets_persona_confirmation_for_the_reply(self):
        self.adapter._generate = AsyncMock(return_value="已经在群里 @ 过小明啦～")
        event = self.group_event("@小明 明天见", [self.module.At(qq="42", name="小明")])
        await self.adapter.private_chat(event, "", "明天见")
        self.assertEqual(event.get_extra("persona_delegate_reply"), "已经在群里 @ 过小明啦～")

    async def test_persona_decorate_replaces_the_main_model_reply(self):
        event = self.group_event("帮我私聊一下 123456")
        event.set_extra("persona_delegate_reply", "主人～已经私聊小明啦")
        event.result = types.SimpleNamespace(chain=[self.module.Plain("已经私聊转达了")])
        await self.adapter.persona_decorate(event)
        self.assertEqual(event.result.chain[0].text, "主人～已经私聊小明啦")
        self.assertIsNone(event.get_extra("persona_delegate_reply"))

    async def test_persona_decorate_creates_result_when_model_reply_is_empty(self):
        event = self.group_event("帮我私聊一下 123456")
        event.set_extra("persona_delegate_reply", "主人～已经私聊小明啦")
        await self.adapter.persona_decorate(event)
        self.assertEqual(event.result.chain[0].text, "主人～已经私聊小明啦")

    async def test_confirmation_falls_back_when_persona_generation_fails(self):
        self.adapter._generate = AsyncMock(side_effect=RuntimeError("boom"))
        event = self.group_event("帮我私聊一下 123456，告诉他三小时后找我")
        await self.adapter.private_chat(event, "123456", "告诉他三小时后找我")
        extra = event.get_extra("persona_delegate_reply")
        self.assertIn("已经私聊", extra)

    async def test_unknown_channel_value_is_refused(self):
        event = self.group_event("@小明 明天见", [self.module.At(qq="42", name="小明")])
        result = await self.adapter.private_chat(event, "", "明天见", channel="bogus")
        self.assertIn("channel 只能是", result)

    async def test_mention_wins_over_a_mismatched_model_target(self):
        event = self.group_event("帮我 @小明 说一声明天见",
                                 [self.module.At(qq="42", name="小明")])
        result = json.loads(await self.adapter.private_chat(event, "小红", "明天见"))
        self.assertEqual(result["channel"], "group")
        self.assertEqual(self.sent_chain()[0].qq, "42")

    async def test_mentions_can_be_disabled_by_configuration(self):
        self.config["allow_mentions"] = False
        event = self.group_event("帮我私聊一下 @小明", [self.module.At(qq="42", name="小明")])
        result = await self.adapter.private_chat(event, "@小明", "问明天几点到")
        self.assertIn("未执行代聊", result)
        self.engine.start.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
