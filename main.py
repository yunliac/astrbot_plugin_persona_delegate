"""踢一下：口语指令 → 私聊或群内 @ 代发，并按人格转述回传。

安全边界：
- 只有授权账号（allowed_users 或 AstrBot 管理员）能发起；
- 用户消息必须出现“私聊/私信/悄悄/单独”等字样，才会走私聊；否则在群里 @ 目标公开说；
- 目标来自消息里的真实 @、群名册中的昵称、明确的 QQ 号或 contacts 配置；
- 每天同步一次群成员名册（昵称/QQ/群），群内 @ 时也会即时收录被 @ 的人；
- 机器人没有对方好友时，可用群临时会话私聊；
- 对方私聊回复会按人格转述给发起人，并继续走正常聊天。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.message_components import At, Plain
from astrbot.api.star import Context, Star, StarTools, register
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType

from .engine import Engine, split_text
from .moderation import Blocklist
from .optout import OptOut
from .roster import Roster

PRIVATE_PATTERN = re.compile(
    r"私聊|私信|私下|私底下|悄悄|偷偷|小窗|私密|单独(?:聊|说|发|问|讲|联系)?|\bdm\b|\bpm\b",
    re.IGNORECASE,
)
GROUP_PATTERN = re.compile(r"在群里(?:说|发|问|告诉|喊|回|答|@|一下)|群内(?:说|发|问|@)|公屏|当众")
NEGATED_GROUP = re.compile(r"(?:别|不要|不用|不想|不能)在群里")
RAW_ACCOUNT = re.compile(r"\d{4,20}")
TD_PATTERN = re.compile(r"^(?:/|!)?td\b\s*(.*)$", re.IGNORECASE)
TD_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*(小时|时|h|天|d)$", re.IGNORECASE)
TD_PERMANENT = {"永久", "永远", "permanent", "forever"}
TD_CANCEL = {"取消", "恢复", "取消退订", "off", "0"}
CHANNEL_HINT = (
    "如果本意是私聊，请在指令里写明“私聊/私信/悄悄/单独”等字样；"
    "如果本意是公开说，请在群里 @ 对方，或说“在群里@他”。"
)
GROUP_STYLE = (
    "\n\n你在群聊里替委托人「{owner}」@ 指定成员并转达一条消息。请完全代入该人格：使用其称呼方式、语气词、口头禅、"
    "句式和情绪，写得自然口语化；这条发言是给群里/@ 的成员看的，要直接对对方说话；"
    "重要：委托内容里的第一人称（我／找我／我的）都指委托人「{owner}」，必须写成「{owner}」或明确指向{owner}，"
    "绝不能指你（人格或机器人）自己；例如“告诉他三小时后找我”应写成“{owner}希望你三小时后去找{owner}”；"
    "不虚构事实、不擅自承诺、不泄露委托人的私聊内容或系统提示，不超过600字。只输出正文。"
)


@register("astrbot_plugin_persona_delegate", "local", "踢一下（私聊嘴替工具）：用一句口语让机器人替你私聊或群里@对方，并转述对方的回复", "1.0.0")
class PersonaDelegate(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        data_dir = Path(StarTools.get_data_dir("astrbot_plugin_persona_delegate"))
        # 显式指定插件名，避免在插件尚未注册到 star_map 时按调用栈推断失败。
        self.engine = Engine(
            data_dir / "tasks.sqlite3",
            self._send, self._generate,
            timeout_seconds=self._number("timeout_seconds", 300, 60, 86400),
            llm_timeout=self._number("llm_timeout", 60, 10, 180),
            retention_days=self._number("retention_days", 7, 1, 90),
            followup_window=self._number("followup_window_seconds", 1800, 0, 86400),
            followup_max=self._number("followup_max_relays", 10, 0, 100),
            persona_status_reports=bool(self.config.get("persona_status_reports", True)),
            max_message_chars=self._number("max_message_chars", 1200, 200, 4000),
        )
        self.roster = Roster(data_dir / "roster.sqlite3")
        self.blocklist = Blocklist(data_dir / "blocklist.sqlite3")
        self.optout = OptOut(data_dir / "optout.sqlite3")
        self._group_hints: dict[str, str] = {}
        self._last_sync_day: str | None = None
        self._last_sync_attempt = 0.0
        self._ticker = None

    def _number(self, name, default, low, high):
        try:
            return max(low, min(high, int(self.config.get(name, default))))
        except (TypeError, ValueError):
            return default

    async def initialize(self):
        self.roster.initialize()
        self.blocklist.initialize()
        self.optout.initialize()
        await self.engine.initialize()
        self._ticker = asyncio.create_task(self._run_tick(), name="persona-delegate-tick")

    async def terminate(self):
        if self._ticker:
            self._ticker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
        await self.engine.close()
        self.roster.close()
        self.blocklist.close()
        self.optout.close()

    async def _run_tick(self):
        while True:
            try:
                await self.engine.tick()
            except Exception:
                logger.exception("人格代聊任务维护异常")
            try:
                await self._maybe_sync_roster()
            except Exception:
                logger.exception("群名册同步异常")
            try:
                self.optout.prune_expired()
            except Exception:
                logger.exception("私聊退订清理异常")
            await asyncio.sleep(10)

    @staticmethod
    def _group_id(event):
        parts = str(event.unified_msg_origin).split(":", 2)
        group_type = getattr(MessageType.GROUP_MESSAGE, "value", MessageType.GROUP_MESSAGE)
        if len(parts) == 3 and parts[1] == str(group_type):
            return parts[2]
        return ""

    def _schedule_tz(self):
        name = str(self.config.get("roster_sync_timezone", "Asia/Shanghai") or "Asia/Shanghai").strip()
        try:
            return ZoneInfo(name)
        except Exception:
            logger.warning("名册同步时区无效：%s，改用容器本地时间", name)
            return None

    async def _maybe_sync_roster(self):
        if not self.config.get("roster_sync_enabled", True):
            return
        now = datetime.now(self._schedule_tz())
        hour = self._number("roster_sync_hour", 4, 0, 23)
        day = now.date().isoformat()
        if now.hour != hour or self._last_sync_day == day:
            return
        if time.time() - self._last_sync_attempt < 60:
            return
        self._last_sync_attempt = time.time()
        result = await self.sync_roster()
        if result.get("platforms"):
            self._last_sync_day = day
        else:
            logger.warning("名册同步：平台尚未就绪，稍后重试")

    async def sync_roster(self):
        """拉取所有适配器的群列表与成员列表，覆盖写入本地名册。"""
        total_groups = 0
        total_members = 0
        platforms_ok = 0
        insts = list(getattr(getattr(self.context, "platform_manager", None), "platform_insts", []) or [])
        for platform in insts:
            try:
                meta = platform.meta()
                client = platform.get_client() if hasattr(platform, "get_client") else None
            except Exception:
                continue
            if client is None:
                continue
            platform_id = str(meta.id)
            try:
                groups = await client.call_action("get_group_list") or []
            except Exception:
                logger.warning("名册同步：读取群列表失败 platform=%s", platform_id)
                continue
            platforms_ok += 1
            keep = []
            for group in groups:
                group_id = str(group.get("group_id") or "").strip()
                if not group_id:
                    continue
                keep.append(group_id)
                try:
                    raw_members = await client.call_action("get_group_member_list", group_id=int(group_id)) or []
                except Exception:
                    # 读不到成员时保留旧名册，不用空列表覆盖。
                    logger.warning("名册同步：读取群成员失败 group=%s", group_id)
                    continue
                members = [
                    {
                        "qq": str(member.get("user_id") or ""),
                        "nickname": member.get("nickname") or "",
                        "card": member.get("card") or "",
                    }
                    for member in raw_members
                    if member.get("user_id")
                ]
                total_members += self.roster.replace_group(
                    platform_id, group_id, str(group.get("group_name") or ""), members,
                )
                total_groups += 1
                await asyncio.sleep(0.2)
            self.roster.prune_groups(platform_id, keep)
        logger.info("群名册同步完成：%s 个群，%s 名成员", total_groups, total_members)
        return {"groups": total_groups, "members": total_members, "platforms": platforms_ok}

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=50)
    async def on_group_mention(self, event: AstrMessageEvent):
        """群内 @ 机器人并 @ 别人时，即时收录被 @ 的人。"""
        if not getattr(event, "is_at_or_wake_command", False):
            return
        group_id = self._group_id(event)
        if not group_id:
            return
        for qq, nickname in self._mentions(event):
            try:
                self.roster.remember_mention(event.get_platform_id(), group_id, qq, nickname)
            except Exception:
                logger.exception("记录被 @ 成员失败")

    async def _send(self, umo: str, text: str):
        try:
            session = MessageSession.from_str(umo)
        except Exception:
            session = None
        if session is not None and self.optout.is_opted_out(session.platform_id, session.session_id):
            raise RuntimeError("对方已退订私聊，未发送")
        error = ""
        try:
            ok = await self.context.send_message(umo, MessageChain(chain=[Plain(text)]))
            if ok:
                return
            error = "未找到目标平台"
        except Exception as exc:
            error = str(exc)
        group_id = self._group_hints.get(umo, "")
        if not group_id:
            try:
                session = MessageSession.from_str(umo)
                group_id = self.roster.group_for(session.platform_id, session.session_id) or ""
            except Exception:
                group_id = ""
        if group_id and self.config.get("temp_session_enabled", True):
            if await self._send_temp(umo, text, group_id):
                return
        raise RuntimeError(f"未发送消息：{error}")

    async def _send_temp(self, umo: str, text: str, group_id: str) -> bool:
        """通过群临时会话发送私聊（NapCat/OneBot 的 send_private_msg + group_id）。"""
        try:
            session = MessageSession.from_str(umo)
            if not str(session.session_id).isdigit() or not str(group_id).isdigit():
                return False
            inst = self.context.get_platform_inst(session.platform_id)
            client = inst.get_client() if inst is not None and hasattr(inst, "get_client") else None
            if client is None:
                return False
            await client.call_action(
                "send_private_msg",
                user_id=int(session.session_id),
                group_id=int(group_id),
                message=[{"type": "text", "data": {"text": text}}],
            )
            return True
        except Exception:
            logger.warning("群临时会话发送失败 umo=%s group=%s", umo, group_id)
            return False

    async def _generate(self, provider_id: str, system: str, prompt: str) -> str:
        response = await self.context.llm_generate(
            chat_provider_id=provider_id, system_prompt=system, prompt=prompt,
        )
        text = str(response.completion_text or "").strip()
        if not text:
            raise ValueError("模型未返回文本，请检查是否启用了聊天模型")
        return text

    @staticmethod
    def _owner(event):
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    def _admin_ids(self, event):
        ids = set()
        try:
            admins = self.context.get_config(event.unified_msg_origin).get("admins_id", []) or []
        except Exception:
            admins = []
        for item in admins:
            ids.add(str(item).strip())
        for item in self.config.get("extra_admins", []) or []:
            ids.add(str(item).strip())
        return ids

    def _is_admin(self, event):
        sender = str(event.get_sender_id()).strip()
        return sender in self._admin_ids(event) or self._owner(event) in self._admin_ids(event)

    def _is_blocked(self, event):
        sender = str(event.get_sender_id()).strip()
        owner = self._owner(event)
        static = {str(item).strip() for item in self.config.get("blocked_users", []) or []}
        if sender in static or owner in static:
            return True
        try:
            return bool(self.blocklist.is_blocked(event.get_platform_id(), sender))
        except Exception:
            return False

    def _allowed(self, event):
        # 默认所有人可用；黑名单和管理员额外放行。
        return self._is_admin(event) or not self._is_blocked(event)

    def _require(self, event):
        if self._is_admin(event):
            return
        if self._is_blocked(event):
            raise ValueError("你已被管理员禁止使用代聊。")

    def _require_admin(self, event):
        if not self._is_admin(event):
            raise ValueError("只有管理员可以管理黑名单。")

    def _parse_account(self, event, target):
        text = str(target or "").strip().lstrip("@")
        if not text:
            raise ValueError("请给出要管理的账号（QQ 号，或 平台:账号）")
        if ":" in text:
            platform, _, account = text.partition(":")
            platform = platform.strip() or event.get_platform_id()
            account = account.strip()
        else:
            platform = event.get_platform_id()
            account = text
        if not account:
            raise ValueError("账号不能为空")
        return platform, account

    def _manage_blocklist(self, event, action, target):
        if action == "blocked":
            rows = self.blocklist.entries()
            if not rows:
                return "黑名单为空。配置里的 blocked_users 也会生效，但不在此列表。"
            return json.dumps([
                {"platform": row["platform"], "account": row["account"], "by": row["added_by"]}
                for row in rows
            ], ensure_ascii=False)
        platform, account = self._parse_account(event, target)
        admins = self._admin_ids(event)
        if action == "ban":
            if account in admins or f"{platform}:{account}" in admins:
                raise ValueError("不能拉黑管理员。")
            if account == str(event.get_sender_id()):
                raise ValueError("不能拉黑自己。")
            self.blocklist.add(platform, account, self._owner(event))
            return f"已拉黑 {platform}:{account}。"
        if self.blocklist.remove(platform, account):
            return f"已解除拉黑 {platform}:{account}。"
        return "运行期黑名单里没有该账号；如果它在插件配置 blocked_users 里，请到配置中移除。"

    def _contacts(self):
        result = {}
        for raw in self.config.get("contacts", []):
            alias, sep, umo = str(raw).partition("=")
            if not sep or not alias.strip():
                continue
            try:
                session = MessageSession.from_str(umo.strip())
                if session.message_type == MessageType.FRIEND_MESSAGE and session.session_id:
                    result[alias.strip()] = str(session)
            except (ValueError, TypeError):
                continue
        return result

    @staticmethod
    def _is_group(event):
        parts = str(event.unified_msg_origin).split(":", 2)
        if len(parts) != 3:
            return False
        group_type = getattr(MessageType.GROUP_MESSAGE, "value", MessageType.GROUP_MESSAGE)
        return parts[1] == str(group_type)

    def _mentions(self, event):
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return []
        try:
            components = getter() or []
        except Exception:
            return []
        found = []
        self_id = str(event.get_self_id())
        for component in components:
            if not isinstance(component, At):
                continue
            qq = str(getattr(component, "qq", "") or "").strip()
            if not qq or qq.lower() == "all" or qq == self_id:
                continue
            found.append((qq, str(getattr(component, "name", "") or "").strip()))
        return found

    def _private_session(self, umo):
        try:
            session = MessageSession.from_str(umo)
        except (ValueError, TypeError, AttributeError):
            raise ValueError("目标会话格式不合法，应为 平台:FriendMessage:账号")
        if session.message_type != MessageType.FRIEND_MESSAGE:
            raise ValueError("只能向私聊会话发送；群里的对象请用群内 @ 方式")
        if not str(session.session_id).strip():
            raise ValueError("私聊账号为空")
        return session

    @staticmethod
    def _account_target(event, account, name, group=""):
        session = MessageSession(event.get_platform_id(), MessageType.FRIEND_MESSAGE, account)
        return {
            "umo": str(session), "account": account, "name": name,
            "platform": event.get_platform_id(), "group": str(group or ""),
        }

    def _resolve_target(self, event, target):
        text = str(target or "").strip()
        mentions = self._mentions(event) if self.config.get("allow_mentions", True) else []
        if len(mentions) > 1:
            raise ValueError("消息里有多个 @，请一次只指定一个对象")
        if mentions:
            # 用户真实点击的 @ 是目标依据；模型给的昵称可能有别字，直接以 @ 为准。
            qq, nickname = mentions[0]
            return self._account_target(event, qq, nickname or qq, self._group_id(event))
        contacts = self._contacts()
        if text in contacts:
            session = self._private_session(contacts[text])
            return {"umo": str(session), "account": str(session.session_id), "name": text, "platform": str(session.platform_id)}
        if text in contacts.values():
            session = self._private_session(text)
            alias = next(key for key, value in contacts.items() if value == text)
            return {"umo": str(session), "account": str(session.session_id), "name": alias, "platform": str(session.platform_id)}
        if not text:
            raise ValueError("请指定对方：@对方、群昵称、QQ 号或已配置的联系人别名")
        if self.config.get("allow_nickname_target", True):
            platform = event.get_platform_id()
            group_id = self._group_id(event)
            try:
                row = self.roster.find_member(platform, group_id, text) if group_id else self.roster.find_member_any(platform, text)
            except Exception:
                row = None
            if row:
                return self._account_target(
                    event, str(row["qq"]), row["nickname"] or row["card"] or text,
                    row["group_id"],
                )
        if RAW_ACCOUNT.fullmatch(text) and self.config.get("allow_mentions", True):
            return self._account_target(event, text, text)
        if not self.config.get("allow_raw_target", False):
            raise ValueError("联系人未配置。可 @ 对方或直接给 QQ 号；其他账号需先在 contacts 配置别名")
        if ":" in text:
            session = self._private_session(text)
            return {"umo": str(session), "account": str(session.session_id), "name": text, "platform": str(session.platform_id)}
        if any(char.isspace() for char in text):
            raise ValueError("目标不能包含空格")
        return self._account_target(event, text, text)

    def _target(self, event, target):
        resolved = self._resolve_target(event, target)
        return resolved["umo"], resolved["name"]

    @staticmethod
    def _remaining_label(until):
        if not until or float(until) == 0:
            return "永久"
        seconds = max(0, int(float(until) - time.time()))
        if seconds >= 86400:
            return f"{seconds // 86400} 天"
        if seconds >= 3600:
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            return f"{hours} 小时" + (f" {minutes} 分钟" if minutes else "")
        return f"{max(1, seconds // 60)} 分钟"

    async def _optout_notice(self, task, label):
        system = str(task.get("persona_prompt") or "") + (
            "\n\n联系人刚刚退订了机器人私聊。请完全代入该人格：用其称呼方式、语气词、口头禅和句式，"
            "给委托人写一句自然口语化的通知：说明对方接下来一段时间不想收到机器人的私聊，以及期限；"
            "不要责怪对方，不虚构信息，不用客服腔，不超过200字。只输出正文。"
        )
        name = str(task.get("target_name") or task.get("target") or "对方")
        prompt = "联系人：" + name + "\n退订时长：" + label + "\n请写给委托人的通知。"
        try:
            return await asyncio.wait_for(
                self._generate(str(task.get("provider_id") or ""), system, prompt),
                timeout=self._number("llm_timeout", 60, 10, 180),
            )
        except Exception:
            logger.warning("退订通知人格化失败，使用默认文案")
            return f"{name} 接下来 {label} 不想收到机器人的私聊，我先停在这里。"

    async def _confirmation(self, persona_prompt, provider_id, target_name, channel, caller=""):
        action = "私聊" if channel == "private" else "在群里 @"
        system = str(persona_prompt or "") + (
            f"\n\n你刚替委托人「{caller}」{action}了「{target_name}」并把话带到。请完全代入该人格：用其称呼方式、语气词、口头禅和句式，"
            "写一句自然口语化的确认；不要客服腔或模板句；不要复述委托内容里的人名或对象；"
            f"如果必须提到委托内容中的“我/找我”，一律指委托人「{caller}」，绝不能指你（人格或机器人）自己；"
            "不要声称对方已读或事项完成，不提系统或工具，不超过80字。只输出正文。"
        )
        prompt = f"动作：{action}\n委托人：{caller}\n联系人：{target_name}\n请写给委托人的确认。"
        try:
            return await asyncio.wait_for(
                self._generate(str(provider_id or ""), system, prompt),
                timeout=self._number("llm_timeout", 60, 10, 180),
            )
        except Exception:
            logger.warning("回复确认人格化失败，使用默认文案")
            return f"已经{action}「{target_name}」并把话带到啦。"

    def _channel(self, event, requested):
        requested = str(requested or "auto").strip().lower()
        if requested not in {"auto", "private", "group"}:
            raise ValueError("channel 只能是 auto、private 或 group")
        text = event.get_message_str() or ""
        group = self._is_group(event)
        says_private = bool(PRIVATE_PATTERN.search(text))
        says_group = bool(GROUP_PATTERN.search(NEGATED_GROUP.sub("", text)))
        if requested == "group":
            if not group:
                raise ValueError("当前不在群里，无法在群内 @ 对方；请改为私聊")
            return "group"
        if requested == "private":
            if says_private:
                return "private"
            raise ValueError("私聊指令缺少明确字样。" + CHANNEL_HINT)
        if says_private:
            return "private"
        if says_group or group:
            return "group"
        raise ValueError("没有判断出要私聊还是群内公开。" + CHANNEL_HINT)

    async def _persona(self, event, persona_id):
        manager = self.context.persona_manager
        if persona_id:
            data = manager.get_persona_v3_by_id(persona_id)
            if not data:
                raise ValueError("指定的人格不存在，请用已保存的人格 ID")
        else:
            conv_id = await self.context.conversation_manager.get_curr_conversation_id(event.unified_msg_origin)
            conv = (await self.context.conversation_manager.get_conversation(event.unified_msg_origin, conv_id)) if conv_id else None
            selected = getattr(conv, "persona_id", None) if conv else None
            _, data, _, _ = await manager.resolve_selected_persona(
                umo=event.unified_msg_origin,
                conversation_persona_id=selected,
                platform_name=event.get_platform_name(),
            )
            if not data:
                raise ValueError("当前会话未启用可读取的保存人格，请明确指定人格 ID（WebChat 尤其需要）")
        prompt = str(data.get("prompt", "")).strip()
        if not prompt:
            raise ValueError("人格提示词为空，请先完善人格")
        # 模仿样例仅作为风格数据，不携带发起会话的历史或工具。
        imitation = data.get("_mood_imitation_dialogs_processed", "")
        if imitation:
            prompt += "\n人格风格示例：\n" + str(imitation)[:8000]
        style_extra = str(self.config.get("persona_style_extra", "") or "").strip()
        if style_extra:
            prompt += "\n额外风格要求：\n" + style_extra[:2000]
        return str(data.get("name", persona_id)), prompt

    @filter.llm_tool(name="persona_relay_message")
    async def private_chat(self, event: AstrMessageEvent, target: str, goal: str, persona_id: str = "", channel: str = "auto") -> str:
        '''用户明确要求代发消息时，用机器人保存的人格发送：指令含“私聊/私信/悄悄/单独”等字样就私聊；否则在群里 @ 对方公开说。未明确要求代发时不要调用，不能猜账号。发送成功不代表对方回应或事项完成。

        Args:
            target(string): 消息里 @ 的人（写 @昵称、昵称或 QQ 号），或 contacts 中配置的别名；不可猜测。
            goal(string): 用户要传达的具体内容或交流目标，原样传递，不得添加承诺或私密上下文。
            persona_id(string): 已保存的人格 ID；空字符串使用发起会话实际生效的人格。
            channel(string): auto 按用户措辞判断（推荐）；private 仅当用户明确说私聊；group 在群里 @ 对方。
        '''
        try:
            self._require(event)
            goal = str(goal or "").strip()
            if not goal:
                raise ValueError("请给出要传达的具体内容")
            if len(goal) > 2000:
                raise ValueError("内容过长，请控制在 2000 字以内")
            resolved = self._resolve_target(event, target)
            sender = str(event.get_sender_id())
            get_name = getattr(event, "get_sender_name", None)
            try:
                caller = str(get_name() if callable(get_name) else "").strip()
            except Exception:
                caller = ""
            caller = caller or sender
            report_to = str(MessageSession(event.get_platform_id(), MessageType.FRIEND_MESSAGE, sender))
            if resolved["account"] == sender or resolved["umo"] == report_to or resolved["account"] == str(event.get_self_id()):
                raise ValueError("不能向发起人或机器人自己代发消息")
            if resolved.get("group"):
                self._group_hints[resolved["umo"]] = resolved["group"]
            origin_group = self._group_id(event)
            if origin_group:
                self._group_hints[report_to] = origin_group
            if len(self._group_hints) > 200:
                self._group_hints.pop(next(iter(self._group_hints)))
            channel = self._channel(event, channel)
            if channel == "private":
                status = self.optout.status(resolved["platform"], resolved["account"])
                if status is not None:
                    raise ValueError(
                        f"对方已退订私聊（还剩 {self._remaining_label(status['until'])}），"
                        "只能群内 @ 或等退订结束"
                    )
                if self.optout.is_opted_out(event.get_platform_id(), sender):
                    raise ValueError("你已退订私聊，收不到私聊回报；请用群内 @，或先发 /td 取消")
            persona_name, persona_prompt = await self._persona(event, str(persona_id or "").strip())
            provider_id = str(self.config.get("provider_id", "")).strip() or await self.context.get_current_chat_provider_id(event.unified_msg_origin)

            if channel == "group":
                if resolved["platform"] != event.get_platform_id():
                    raise ValueError("该联系人不属于当前平台，无法在本群 @ 对方")
                try:
                    body = await self._generate(
                        provider_id,
                        persona_prompt + GROUP_STYLE.format(owner=caller),
                        "委托人：" + caller + "\n委托内容：\n" + goal + "\n对方称呼：" + resolved["name"] + "\n请写群内发言正文。",
                    )
                except Exception:
                    logger.exception("群内代发生成失败")
                    return "未发送：生成群内消息失败，请检查聊天模型配置。"
                body = body.strip()[:1400]
                if not body:
                    return "未发送：模型生成内容为空。"
                try:
                    sent = await self._send_group(event, resolved, body)
                except Exception:
                    logger.exception("群内 @ 发送失败")
                    return "未发送：群内 @ 发送失败，请稍后重试；不要立即重复发送以免刷屏。"
                if not sent:
                    return "未发送：找不到当前群平台。"
                confirmation = await self._confirmation(persona_prompt, provider_id, resolved["name"], "group", caller)
                event.set_extra("persona_delegate_reply", confirmation)
                return json.dumps({
                    "channel": "group", "sent": True, "target": resolved["name"], "persona": persona_name,
                    "reply": confirmation,
                    "note": "已在群里 @ 对方发出。请把 reply 字段原样作为回复，不要改写或补充，也不要声称对方已读或事项已完成。",
                }, ensure_ascii=False)

            task = await self.engine.start(
                owner_key=self._owner(event), origin=event.unified_msg_origin,
                report_to=report_to, target=resolved["umo"], target_name=resolved["name"],
                goal=goal, persona_id=persona_name, persona_prompt=persona_prompt,
                provider_id=provider_id, owner_name=caller,
            )
            confirmation = await self._confirmation(persona_prompt, provider_id, resolved["name"], "private", caller)
            event.set_extra("persona_delegate_reply", confirmation)
            return json.dumps({
                "id": task["id"], "status": task["status"],
                "channel": "private", "target": resolved["name"], "reply": confirmation,
                "note": "私聊已发起；waiting 仅表示已发出等待回复，不能声称目标完成。请把 reply 字段原样作为回复，不要改写或补充。",
            }, ensure_ascii=False)
        except (ValueError, RuntimeError) as exc:
            return f"未执行代聊：{exc}"
        except Exception:
            logger.exception("人格代聊创建异常")
            return "未能确认是否已发出，请先查询代聊进度，不要立即重试以免重复发送。"

    async def _send_group(self, event, resolved, text):
        # 只带账号构造 At，避免依赖各适配器对昵称字段的差异；平台会自行显示 @ 对象。
        chunks = split_text(text, self._number("max_message_chars", 1200, 200, 4000)) or [str(text)]
        sent = False
        for index, chunk in enumerate(chunks):
            components = [At(qq=resolved["account"])] if index == 0 else []
            components.append(Plain((" " if index == 0 else "") + chunk))
            chain = MessageChain(chain=components)
            sent = await self.context.send_message(event.unified_msg_origin, chain) or sent
            if index < len(chunks) - 1:
                await asyncio.sleep(0.4)
        return sent

    @filter.llm_tool(name="persona_chat_control")
    async def control(self, event: AstrMessageEvent, action: str, task_id: str = "", target: str = "") -> str:
        '''管理代聊。普通用户可查进度/取消/查联系人/查人格；管理员可拉黑、解除拉黑、查看黑名单。默认所有人可用，黑名单除外。

        Args:
            action(string): status 查询任务；cancel 取消自己的任务；contacts 查询联系人；roster 立即同步群成员名册；personas 查询保存人格；ban 拉黑账号（管理员）；unban 解除拉黑（管理员）；blocked 查看黑名单（管理员）。
            task_id(string): cancel 时指定任务 ID；留空取消自己所有活动任务。
            target(string): ban/unban 时的账号：QQ 号或 平台:账号。
        '''
        try:
            if action in {"ban", "unban", "blocked"}:
                self._require_admin(event)
                return self._manage_blocklist(event, action, target)
            self._require(event)
            owner = self._owner(event)
            if action == "cancel":
                return await self.engine.cancel(owner, task_id)
            if action == "status":
                rows = await self.engine.status(owner)
                # 工具结果也可能被群内模型复述：仅返回不含对话正文的状态。
                return json.dumps([{k: r.get(k) for k in ("id", "status", "target_name", "turns", "deadline")} for r in rows], ensure_ascii=False)
            if action == "contacts":
                return json.dumps(list(self._contacts()), ensure_ascii=False)
            if action == "roster":
                result = await self.sync_roster()
                return f"群名册已同步：{result['groups']} 个群，{result['members']} 名成员。"
            if action == "personas":
                return json.dumps([p["name"] for p in self.context.persona_manager.personas_v3], ensure_ascii=False)
            return "未知操作，可用 status/cancel/contacts/roster/personas/ban/unban/blocked"
        except ValueError as exc:
            return str(exc)

    @filter.command("代聊")
    async def chat_command(self, event: AstrMessageEvent, text: str = ""):
        '''确定性管理命令：/代聊 进度、取消 [任务ID]、联系人、人格。'''
        # 从完整原文提取，避免多词命令被只解析一个位置参数。
        raw = event.get_message_str().strip().lstrip("/")
        content = raw.partition("代聊")[2].strip()
        parts = content.split(maxsplit=1)
        action = parts[0] if parts else "帮助"
        mapping = {
            "进度": "status", "取消": "cancel", "联系人": "contacts",
            "人格": "personas", "名册": "roster",
            "拉黑": "ban", "解除拉黑": "unban", "黑名单": "blocked",
        }
        if action in mapping:
            argument = parts[1] if len(parts) > 1 else ""
            result = await self.control(event, mapping[action], task_id=argument, target=argument)
        else:
            result = (
                "踢一下：口语就行——“@机器人 帮我私聊一下 @张三，问明天几点集合”（有“私聊”字样才私聊）；"
                "没有“私聊”字样时会在群里 @ 对方说。"
                "对方回复会转告你，之后由正常聊天继续。"
                "管理：/代聊 进度|取消 [ID]|联系人|名册|人格；管理员：/代聊 拉黑 账号|解除拉黑 账号|黑名单。"
                "不想被私聊可在私聊里发 /td 2小时|3天|永久。"
            )
        yield event.plain_result(result)

    @filter.command("td")
    async def td_command(self, event: AstrMessageEvent):
        '''退订私聊：/td 2小时、/td 3天、/td 永久；/td 取消 恢复。退订后仍可群内 @。'''
        if self._is_group(event):
            yield event.plain_result("请在私聊里发 /td，例如 /td 2小时、/td 3天、/td 永久。")
            return
        match = TD_PATTERN.match(event.get_message_str().strip())
        arg = (match.group(1) if match else "").strip()
        platform = event.get_platform_id()
        account = str(event.get_sender_id())
        if arg in TD_CANCEL:
            cleared = self.optout.clear(platform, account)
            yield event.plain_result("已恢复私聊。" if cleared else "你当前没有退订记录。")
            event.stop_event()
            return
        if arg in TD_PERMANENT:
            seconds, label = 0.0, "永久"
        else:
            duration = TD_DURATION.match(arg)
            if not duration:
                yield event.plain_result("用法：/td 2小时、/td 3天、/td 永久；恢复用 /td 取消。")
                return
            value = float(duration.group(1))
            unit = duration.group(2).lower()
            hours = unit in {"小时", "时", "h"}
            seconds = value * 3600 if hours else value * 86400
            if seconds < 3600 or seconds > 3650 * 86400:
                yield event.plain_result("时长需在 1 小时到 10 年之间；永久请发 /td 永久。")
                return
            label = duration.group(1) + ("小时" if hours else "天")
        self.optout.set(platform, account, seconds)
        try:
            tasks = await self.engine.tasks_for_target(event.unified_msg_origin)
        except Exception:
            tasks = []
        cancelled = 0
        if tasks:
            try:
                cancelled = await self.engine.cancel_target(
                    event.unified_msg_origin, "对方已退订私聊，任务停止。", notify=False,
                )
            except Exception:
                cancelled = 0
        for task in tasks:
            notice = await self._optout_notice(task, label)
            try:
                await self._send(str(task["report_to"]), notice)
            except Exception:
                logger.warning("退订通知发送失败 task=%s", task.get("id"))
        extra = f"；已停止 {cancelled} 条进行中的私聊任务" if cancelled else ""
        yield event.plain_result(
            f"已退订私聊（{label}）{extra}。退订期间机器人不会再私聊你，但仍可在群里 @ 机器人。恢复发 /td 取消。"
        )
        event.stop_event()

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE, priority=100)
    async def on_private_reply(self, event: AstrMessageEvent):
        if str(event.get_sender_id()) == str(event.get_self_id()):
            return
        text = event.get_message_str().strip()
        # 媒体不进行空回复或臆测；任务仍等待文字或超时。
        if not text:
            return
        if TD_PATTERN.match(text):
            return
        # 记录并把对方回复转告发起人；但不拦截事件，让对方像普通聊天一样继续对话。
        await self.engine.receive(event.unified_msg_origin, text[:4000])

    @filter.on_decorating_result()
    async def persona_decorate(self, event: AstrMessageEvent):
        """把主模型随口生成的“已转达”替换成按人格生成的确认语。"""
        text = event.get_extra("persona_delegate_reply")
        if not text:
            return
        event.set_extra("persona_delegate_reply", None)
        result = event.get_result()
        chain = list(getattr(result, "chain", []) or []) if result is not None else []
        if result is None or not chain:
            event.set_result(MessageEventResult(chain=[Plain(str(text))]))
            return
        kept = [item for item in chain if not isinstance(item, Plain)]
        result.chain = [Plain(str(text))] + kept
