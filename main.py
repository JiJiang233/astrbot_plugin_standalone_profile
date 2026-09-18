"""STPRO 插件入口。

命令入口使用 `@filter.command_group("stpro")`（设计文档第 14.2 节）。命令 Handler
只负责解析、调用模块接口和渲染消息；权限判断、比较后写入、回滚与脱敏都在深模块里。
"""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Image, Plain
from astrbot.api.star import Context, Star
from astrbot.core.platform.message_session import MessageSession
from astrbot.core.platform.message_type import MessageType
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .services.astrbot_profile_bridge import AstrBotProfileBridge
from .services.binding_service import BindingService, MembershipResult
from .services.interaction_service import (
    STEP_API_KEY,
    STEP_ENDPOINT,
    STEP_PERSONA_NAME,
    STEP_PERSONA_PROMPT,
    STEP_REMOVE_CONFIRM,
    InteractionService,
)
from .services.model_client import ModelClient
from .services.monitor_service import MonitorConfig, MonitorService
from .services.persona_service import PersonaService
from .services.profile_service import ProfileService
from .services.provider_bridge import ProviderBridge
from .services.reconciliation_service import ReconciliationService
from .storage.ownership_store import OwnershipStore
from .utils.errors import CapabilityUnavailable, StproError
from .utils.locks import LockManager
from .utils.masking import scrub_text

PLUGIN_NAME = "astrbot_plugin_standalone_profile"
GROUP_PREFIX = "GroupMessage"
HELP_FALLBACK_TEXT = """STPRO 指令导航

常用操作
1. 创建档案：/stpro new <配置名>
2. 查看/选择模型：/stpro model <配置名> [序号或名称]
3. 绑定群聊：/stpro bind <配置名> [群号]
4. 创建人格：/stpro persona <配置名> new [人格名]
5. 切换人格：/stpro persona <配置名> set [人格名]
6. 修改人格：/stpro persona <配置名> [人格名]
7. 删除人格：/stpro persona <配置名> del [人格名]
8. 查看档案：/stpro list [配置名]

管理操作
9. 修改接口：/stpro set <配置名> {endpoint|apikey|all}
10. 检查状态：/stpro update [配置名]
11. 解除绑定：/stpro unbind [群号]
12. 删除档案：/stpro remove <配置名>

< > 为必填参数，[ ] 为可选参数。"""


def _user_flow_lock(fn):
    """用户流程锁：串行处理同一用户的命令和普通消息（第 11.2 节）。

    只用于不含网络请求的 handler —— 含远程请求的路径已后台化，`cmd_bind` 的成员
    校验则显式放在锁外执行。
    """

    @functools.wraps(fn)
    async def wrapper(self, event, *args, **kwargs):
        async with self.locks.user_scope(self._user_key(event)):
            async for result in fn(self, event, *args, **kwargs):
                yield result

    return wrapper


class OneBotMembershipChecker:
    """aiocqhttp(OneBot v11) 的成员校验。

    设计文档第 9 节：平台能力必须显式探测。无法确认调用者在群内、机器人在群内时，
    返回 `supported=False`，调用方必须拒绝执行，不能跳过验证。
    """

    def __init__(self, context: Context) -> None:
        self.context = context

    async def check(
        self,
        group_umo: str,
        user_key: str,
        self_id: str | None = None,
    ) -> MembershipResult:
        try:
            platform_id, _, session_id = group_umo.split(":", 2)
        except ValueError:
            return MembershipResult(False, False, False)

        platform = self.context.get_platform_inst(platform_id)
        bot = getattr(platform, "bot", None)
        if bot is None or not session_id.isdigit():
            return MembershipResult(False, False, False)

        try:
            members = await bot.call_action(
                "get_group_member_list",
                group_id=int(session_id),
            )
        except Exception as exc:
            logger.info(f"[stpro] 群成员查询失败，视为能力不足: {exc}")
            return MembershipResult(False, False, False)

        if not isinstance(members, list) or not members:
            # get_group 系列的静默降级同样可能给出空列表，不能当成"不在群里"放行
            return MembershipResult(False, False, False)

        member_ids = {
            str(m.get("user_id"))
            for m in members
            if isinstance(m, dict) and m.get("user_id") is not None
        }
        user_id = user_key.split(":", 1)[-1]
        user_in_group = user_id in member_ids
        bot_in_group = bool(self_id) and str(self_id) in member_ids
        return MembershipResult(True, user_in_group, bot_in_group)


class StproPlugin(Star):
    """让用户创建 WebUI 可管理的独立模型配置，并按群选择配置。"""

    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.conf = config if config is not None else {}

        self.locks = LockManager()
        self.store = OwnershipStore(
            _plugin_data_dir() / "ownership.json",
        )
        self.bridge = ProviderBridge(context, store=self.store)
        alignment_config_id = str(
            self.conf.get("alignment_config_id") or "default"
        ).strip()
        self.config_bridge = AstrBotProfileBridge(
            context,
            alignment_config_id=alignment_config_id or "default",
        )
        self._refresh_alignment_config_options()
        self.membership = OneBotMembershipChecker(context)

        monitor_conf = self.conf.get("monitor") or {}
        self.client = ModelClient(
            timeout=float(monitor_conf.get("timeout_sec", 30) or 30),
        )

        self.binding_service = BindingService(
            self.store,
            self.bridge,
            self.config_bridge,
            self.locks,
            self._adapt_membership(),
        )
        self.profile_service = ProfileService(
            self.store,
            self.bridge,
            self.config_bridge,
            self.client,
            self.locks,
            on_profile_usable=self._on_profile_usable,
            on_profile_unusable=self._on_profile_unusable,
        )
        self.persona_service = PersonaService(
            context,
            self.store,
            self.profile_service,
            self.config_bridge,
            self.locks,
        )
        self.help_image_path = _plugin_data_dir() / "stpro_help.png"
        self.monitor = MonitorService(
            self.store,
            self.bridge,
            self.client,
            self.locks,
            send=self._send_text,
            config=MonitorConfig(
                enable=bool(monitor_conf.get("enable", True)),
                interval_sec=int(monitor_conf.get("interval_sec", 300) or 300),
                failure_threshold=int(monitor_conf.get("failure_threshold", 3) or 3),
                timeout_sec=int(monitor_conf.get("timeout_sec", 30) or 30),
                max_concurrency=int(monitor_conf.get("max_concurrency", 3) or 3),
            ),
        )
        self.reconciler = ReconciliationService(
            self.store,
            self.bridge,
            self.config_bridge,
            self.binding_service,
        )
        interaction_conf = self.conf.get("interaction") or {}
        self.interaction = InteractionService(
            timeout_sec=int(interaction_conf.get("timeout_sec", 300) or 300),
            confirm_timeout_sec=int(
                interaction_conf.get("confirm_timeout_sec", 120) or 120,
            ),
            model_cache_ttl_sec=int(self.conf.get("model_cache_ttl_sec", 300) or 300),
            max_remote_retries=int(interaction_conf.get("max_remote_retries", 3) or 3),
        )

        self._tasks: set[asyncio.Task] = set()
        # 用户主键 -> 正在跑的远程任务；保证同一用户不会并发发起两个远程请求
        self._remote_tasks: dict[str, asyncio.Task] = {}
        self._reconcile_interval = int(
            (self.conf.get("reconcile") or {}).get("interval_sec", 600) or 600,
        )

    # ---------- 装配辅助 ----------

    def _adapt_membership(self) -> Any:
        """把成员校验包装成 BindingService 需要的接口。

        绑定命令的 self_id 由调用方通过 `_pending_self_id` 提供；无法提供时
        机器人侧校验不可用，返回 `supported=False` 让调用方拒绝。
        """
        outer = self

        class _Adapter:
            async def check(self, group_umo: str, user_key: str) -> MembershipResult:
                self_id = getattr(outer, "_pending_self_id", None)
                return await outer.membership.check(group_umo, user_key, self_id)

        return _Adapter()

    async def _on_profile_usable(self, profile_id: str) -> None:
        resumed = await self.binding_service.resume_paused(profile_id)
        if resumed:
            logger.info(f"[stpro] Profile {profile_id} 恢复，已恢复绑定 {resumed}")

    async def _on_profile_unusable(self, profile_id: str) -> None:
        """Provider 变为不可用（如 set 后原模型消失）：相关绑定进入暂停。"""
        paused = await self.binding_service.pause_bindings(profile_id)
        if paused:
            logger.info(f"[stpro] Profile {profile_id} 不可用，已暂停绑定 {paused}")

    async def _send_text(self, umo: str, text: str) -> None:
        await self.context.send_message(umo, MessageChain([Plain(text)]))

    async def _on_external_rule_change(self, umo: str, provider_id: str) -> None:
        """外部改写会话规则：登记后交给对账处理，不自动改回。"""
        logger.info(f"[stpro] 外部改写会话规则 umo={umo} provider={provider_id}")
        try:
            await self.reconciler.reconcile()
            self._refresh_alignment_config_options()
        except Exception as exc:
            logger.warning(f"[stpro] 外部规则变更后对账失败: {exc}")

    # ---------- 生命周期 ----------

    async def initialize(self) -> None:
        try:
            await self.store.load()
        except Exception as exc:
            # 损坏或版本过高：允许只读诊断，不覆盖、不删除任何 Provider
            logger.error(f"[stpro] 数据加载失败，进入只读诊断模式: {exc}")
            return

        self.bridge.on_external_rule_change = self._on_external_rule_change
        self.bridge.register_change_hook()

        try:
            report = await self.reconciler.reconcile()
            self._refresh_alignment_config_options()
            logger.info(f"[stpro] 启动对账完成: {report.as_text()}")
        except Exception as exc:
            logger.error(f"[stpro] 启动对账失败: {exc}")

        try:
            await self._ensure_help_image(force=True)
        except Exception as exc:
            logger.warning(f"[stpro] 帮助图片生成失败，将使用文字回退: {exc}")

        # 只有完整对账结束后才启动定时监控
        await self.monitor.start()
        self._spawn(self._sweep_loop())
        self._spawn(self._config_options_loop())
        self._spawn(self._reconcile_loop())

    async def terminate(self) -> None:
        """停止时只取消任务与清理临时状态，不删除 Provider、配置或管理员规则。"""
        await self.monitor.stop()
        for task in list(self._remote_tasks.values()):
            task.cancel()
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        for task in list(self._remote_tasks.values()):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        self._remote_tasks.clear()
        await self.interaction.clear_all()

    def _spawn(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(60)
                await self.interaction.sweep_expired()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[stpro] 清理超时流程失败: {exc}")

    async def _reconcile_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(60, self._reconcile_interval))
                await self.reconciler.reconcile()
                self._refresh_alignment_config_options()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[stpro] 周期对账失败: {exc}")

    async def _config_options_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(30)
                self._refresh_alignment_config_options()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(f"[stpro] 刷新对齐配置列表失败: {exc}")

    # ---------- 命令组 ----------

    @filter.command_group("stpro")
    def stpro(self) -> None:
        """管理独立模型配置与群绑定"""
        pass

    @stpro.command("help")
    async def cmd_help(self, event: AstrMessageEvent) -> Any:
        """查看 STPRO 指令导航图"""
        if self._is_self_message(event):
            return
        try:
            path = await self._ensure_help_image()
        except Exception as exc:
            logger.warning(f"[stpro] 帮助图片生成失败: {exc}")
            yield event.plain_result(HELP_FALLBACK_TEXT)
            return
        yield event.chain_result([Image.fromFileSystem(str(path))])

    @stpro.command("new")
    @_user_flow_lock
    async def cmd_new(self, event: AstrMessageEvent, name: str) -> Any:
        """创建一个新档案"""
        if self._is_self_message(event):
            return
        if not self._require_private(event):
            yield event.plain_result("请私聊机器人创建档案。")
            return
        notice = await self._begin_command(event)
        try:
            self.profile_service.validate_name(name)
            await self.profile_service.assert_not_duplicated(
                self._user_key(event),
                name,
            )
        except StproError as exc:
            yield event.plain_result(f"{notice}{exc}")
            return

        await self.interaction.start(
            self._user_key(event),
            "new",
            candidate_name=name,
        )
        yield event.plain_result(
            f"{notice}正在创建「{name}」。请发送 Endpoint，"
            "例如：https://api.openai.com/v1",
        )

    @stpro.command("set")
    @_user_flow_lock
    async def cmd_set(
        self,
        event: AstrMessageEvent,
        name: str,
        field: str,
    ) -> Any:
        """更新档案的 Endpoint、API Key 或两者"""
        if self._is_self_message(event):
            return
        if not self._require_private(event):
            yield event.plain_result("请私聊机器人更新档案。")
            return
        notice = await self._begin_command(event)
        mode = field.lower()
        if mode not in {"endpoint", "apikey", "all"}:
            yield event.plain_result(
                f"{notice}用法：/stpro set <配置名> {{endpoint|apikey|all}}"
            )
            return
        record = await self.profile_service.find_by_name(self._user_key(event), name)
        if record is None:
            yield event.plain_result(f"{notice}没有找到名为「{name}」的配置。")
            return
        if record.admin_managed:
            yield event.plain_result(
                f"{notice}该档案已由 AstrBot 管理员接管，当前不能修改。"
            )
            return

        await self.interaction.start(
            self._user_key(event),
            f"set_{mode}",
            profile_id=record.profile_id,
            candidate_name=record.name,
        )
        prompt = "请发送新的 API Key。" if mode == "apikey" else "请发送新的 Endpoint。"
        yield event.plain_result(f"{notice}{prompt}")

    @stpro.command("model")
    @_user_flow_lock
    async def cmd_model(
        self,
        event: AstrMessageEvent,
        name: str,
        choice: str = "",
    ) -> Any:
        """查看可用模型或选择模型"""
        if self._is_self_message(event):
            return
        if not self._require_private(event):
            yield event.plain_result("请私聊机器人查看或选择模型。")
            return
        notice = await self._begin_command(event)
        user_key = self._user_key(event)
        record = await self.profile_service.find_by_name(user_key, name)
        if record is None:
            yield event.plain_result(f"{notice}没有找到名为「{name}」的配置。")
            return

        try:
            if not choice:
                # 获取模型列表是远程请求：交给后台任务，命令立即返回
                await self.interaction.start(
                    user_key, "model", profile_id=record.profile_id
                )
                started = self._start_background(
                    user_key,
                    event.unified_msg_origin,
                    lambda: self._models_work(user_key, record),
                )
                yield event.plain_result(
                    f"{notice}正在获取「{record.name}」的模型列表，稍后把结果发给你。"
                    if started
                    else f"{notice}上一次请求仍在进行中，请稍后再试。",
                )
                return
        except StproError as exc:
            yield event.plain_result(f"{notice}{exc}")
            return

        model_id = await self.interaction.resolve_model_choice(
            user_key,
            record.profile_id,
            choice,
        )
        if model_id is None:
            yield event.plain_result(
                f"{notice}模型列表已过期，或没有找到这个序号/名称。请重新执行 "
                f"`/stpro model {record.name}` 后再选择。",
            )
            return
        try:
            await self.profile_service.select_model(record, model_id)
        except StproError as exc:
            yield event.plain_result(f"{notice}{exc}")
            return
        await self.interaction.finish(user_key)
        yield event.plain_result(f"{notice}已为「{record.name}」选择模型：{model_id}")

    @stpro.command("list")
    @_user_flow_lock
    async def cmd_list(self, event: AstrMessageEvent, name: str = "") -> Any:
        """查看档案列表或档案详情"""
        if self._is_self_message(event):
            return
        if not self._require_private(event):
            yield event.plain_result("请私聊机器人查看档案。")
            return
        notice = await self._begin_command(event)
        user_key = self._user_key(event)
        records = await self.store.list_profiles(user_key)
        if name:
            record = await self.profile_service.find_by_name(user_key, name)
            if record is None:
                yield event.plain_result(f"{notice}没有找到名为「{name}」的配置。")
                return
            yield event.plain_result(
                f"{notice}{self._render_summary(await self.profile_service.summarize(record))}"
            )
            return

        if not records:
            yield event.plain_result(
                f"{notice}你还没有档案。使用 `/stpro new <配置名>` 创建。"
            )
            return

        lines = [f"{i}. {r.name}" for i, r in enumerate(records, start=1)]
        yield event.plain_result(f"{notice}你的档案：\n" + "\n".join(lines))

    @stpro.command("persona")
    @_user_flow_lock
    async def cmd_persona(
        self,
        event: AstrMessageEvent,
        profile_name: str,
        action: str = "",
        persona_name: str = "",
    ) -> Any:
        """列出、创建、切换、修改或删除当前档案的人格。"""
        if self._is_self_message(event):
            return
        if not self._require_private(event):
            yield event.plain_result("请私聊机器人管理人格。")
            return
        notice = await self._begin_command(event)
        user_key = self._user_key(event)
        profile = await self.profile_service.find_by_name(user_key, profile_name)
        if profile is None:
            yield event.plain_result(f"{notice}没有找到名为「{profile_name}」的配置。")
            return

        try:
            if not action:
                items = await self.persona_service.list_personas(profile)
                lines = []
                for index, item in enumerate(items, start=1):
                    mark = " ← 当前" if item.selected else ""
                    lines.append(f"{index}. {item.name}（{item.source}）{mark}")
                yield event.plain_result(
                    f"{notice}档案「{profile.name}」可用人格：\n" + "\n".join(lines)
                )
                return

            lowered = action.casefold()
            if lowered == "new":
                if persona_name:
                    name = await self.persona_service.assert_name_available(
                        profile, persona_name
                    )
                    await self.interaction.start(
                        user_key,
                        "persona_new",
                        profile_id=profile.profile_id,
                        candidate_persona_name=name,
                    )
                    yield event.plain_result(
                        f"{notice}正在创建人格「{name}」。请发送人格提示词。"
                    )
                else:
                    await self.interaction.start(
                        user_key,
                        "persona_new_name",
                        profile_id=profile.profile_id,
                    )
                    yield event.plain_result(f"{notice}请发送新的人格名。")
                return

            if lowered == "set":
                if persona_name:
                    selected = await self.persona_service.select_persona(
                        profile, user_key, persona_name
                    )
                    yield event.plain_result(
                        f"{notice}已将档案「{profile.name}」的人格切换为「{selected}」。"
                    )
                else:
                    await self.interaction.start(
                        user_key,
                        "persona_set_name",
                        profile_id=profile.profile_id,
                    )
                    yield event.plain_result(
                        f"{notice}请发送要切换的人格名。可先用 `/stpro persona {profile.name}` 查看。"
                    )
                return

            if lowered == "del":
                if persona_name:
                    deleted = await self.persona_service.delete_persona(
                        profile, user_key, persona_name
                    )
                    yield event.plain_result(f"{notice}已删除人格「{deleted.name}」。")
                else:
                    await self.interaction.start(
                        user_key,
                        "persona_del_name",
                        profile_id=profile.profile_id,
                    )
                    yield event.plain_result(f"{notice}请发送要删除的人格名。")
                return

            if persona_name:
                yield event.plain_result(
                    f"{notice}用法：/stpro persona <配置名> [人格名]"
                )
                return
            record = await self.persona_service.find_owned(profile.profile_id, action)
            if record is None:
                yield event.plain_result(
                    f"{notice}当前档案没有名为「{action}」的自建人格。"
                )
                return
            await self.interaction.start(
                user_key,
                "persona_edit",
                profile_id=profile.profile_id,
                candidate_persona_name=record.name,
            )
            yield event.plain_result(
                f"{notice}正在修改人格「{record.name}」。请发送新的人格提示词。"
            )
        except StproError as exc:
            yield event.plain_result(f"{notice}{exc}")

    @stpro.command("remove")
    @_user_flow_lock
    async def cmd_remove(self, event: AstrMessageEvent, name: str) -> Any:
        """删除档案，需要二次确认"""
        if self._is_self_message(event):
            return
        if not self._require_private(event):
            yield event.plain_result("请私聊机器人删除档案。")
            return
        notice = await self._begin_command(event)
        user_key = self._user_key(event)
        record = await self.profile_service.find_by_name(user_key, name)
        if record is None:
            yield event.plain_result(f"{notice}没有找到名为「{name}」的配置。")
            return
        if record.admin_managed and self.bridge.inspect(record.provider_id).exists:
            # 删除会覆盖管理员接管结果：发起阶段就拒绝（第 6 节）
            yield event.plain_result(
                f"{notice}该档案已由 AstrBot 管理员接管，当前不能删除。\n"
                "如需删除，请先让管理员在 WebUI 中移除对应配置。",
            )
            return

        state = await self.interaction.start(
            user_key, "remove", profile_id=record.profile_id
        )
        yield event.plain_result(
            f"{notice}确认删除「{record.name}」吗？关联的所有群绑定也会解除。\n"
            f"请发送确认码：\n{state.confirm_token}",
        )

    @stpro.command("bind")
    async def cmd_bind(
        self,
        event: AstrMessageEvent,
        name: str,
        group_id: str = "",
    ) -> Any:
        """为群绑定一个档案"""
        if self._is_self_message(event):
            return
        user_key = self._user_key(event)

        async with self.locks.user_scope(user_key):
            notice = await self._begin_command(event)
            record = await self.profile_service.find_by_name(user_key, name)
            if record is None:
                yield event.plain_result(f"{notice}没有找到名为「{name}」的配置。")
                return

            try:
                group_umo = self._resolve_group_umo(
                    event,
                    group_id or None,
                )
            except StproError as exc:
                yield event.plain_result(f"{notice}{exc}")
                return

        # 成员校验包含网络请求，实际绑定在用户流程锁外执行。
        self._pending_self_id = event.get_self_id()
        try:
            outcome = await self.binding_service.bind(user_key, record, group_umo)
        except (CapabilityUnavailable, StproError) as exc:
            yield event.plain_result(f"{notice}{exc}")
            return
        finally:
            self._pending_self_id = None
        yield event.plain_result(f"{notice}{outcome.message}")

    @stpro.command("unbind")
    @_user_flow_lock
    async def cmd_unbind(
        self,
        event: AstrMessageEvent,
        group_id: str = "",
    ) -> Any:
        """解除群绑定"""
        if self._is_self_message(event):
            return
        notice = await self._begin_command(event)
        try:
            group_umo = self._resolve_group_umo(event, group_id or None)
        except StproError as exc:
            yield event.plain_result(f"{notice}{exc}")
            return
        try:
            outcome = await self.binding_service.unbind(
                self._user_key(event), group_umo
            )
        except StproError as exc:
            yield event.plain_result(f"{notice}{exc}")
            return
        yield event.plain_result(f"{notice}{outcome.message}")

    @stpro.command("update")
    @_user_flow_lock
    async def cmd_update(self, event: AstrMessageEvent, name: str = "") -> Any:
        """检查档案是否可用"""
        if self._is_self_message(event):
            return
        if not self._require_private(event):
            yield event.plain_result("请私聊机器人检查档案。")
            return
        notice = await self._begin_command(event)
        user_key = self._user_key(event)

        if name:
            record = await self.profile_service.find_by_name(user_key, name)
            targets = [record] if record else []
            if record is None:
                yield event.plain_result(f"{notice}没有找到名为「{name}」的配置。")
                return
        else:
            targets = await self.store.list_profiles(user_key)

        if not targets:
            yield event.plain_result(f"{notice}没有可检查的配置。")
            return

        # 检查是远程请求：交给后台任务，命令立即返回
        started = self._start_background(
            user_key,
            event.unified_msg_origin,
            lambda: self._update_work(targets),
        )
        yield event.plain_result(
            f"{notice}正在检查 {len(targets)} 个配置，稍后把结果发给你。"
            if started
            else f"{notice}上一次请求仍在进行中，请稍后再试。",
        )

    # ---------- 普通私聊消息：多轮输入 ----------

    @filter.event_message_type(EventMessageType.PRIVATE_MESSAGE)
    @_user_flow_lock
    async def on_private_message(self, event: AstrMessageEvent) -> Any:
        """只消费活动流程中当前步骤需要的文本，其余交还 AstrBot 后续处理。"""
        if self._is_self_message(event):
            return
        user_key = self._user_key(event)
        state = await self.interaction.get(user_key)
        if state is None or not state.expects_text():
            return

        text = (event.get_message_str() or "").strip()
        if not text:
            return
        # 同一条消息可能同时命中命令处理器和普通私聊处理器。命令处理器会先
        # 建立交互状态，因此这里必须挡住 STPRO 命令本身，避免它被新状态再次
        # 当作 Endpoint、确认码、Persona 名称或提示词消费。AstrBot 会在唤醒
        # 阶段移除已匹配的 wake prefix，所以同时兼容 `/stpro` 与 `stpro`。
        if self._is_stpro_command_text(text):
            return

        try:
            if state.step == STEP_ENDPOINT:
                # 门卫：不像 Endpoint 的消息（机器人回声、LLM 闲聊回复）直接不消费，
                # 否则每次提示"请发送 Endpoint"都会立刻被自己的回声触发一次格式报错。
                if not self._looks_like_endpoint(text):
                    return
                event.stop_event()
                from .services.model_client import validate_endpoint

                endpoint = validate_endpoint(text)
                hint = (
                    f"（已自动补全为 {endpoint}）\n"
                    if endpoint != text.strip().rstrip("/")
                    else ""
                )
                if state.command == "set_endpoint":
                    state.candidate_endpoint = endpoint
                    state.touch()
                    started = self._start_background(
                        user_key,
                        event.unified_msg_origin,
                        lambda: self._credential_work(
                            user_key,
                            state,
                            event.unified_msg_origin,
                        ),
                    )
                    yield event.plain_result(
                        f"{hint}正在验证新的 Endpoint，稍后把结果发给你。"
                        if started
                        else f"{hint}上一次请求仍在进行中，请稍后再试。",
                    )
                    return
                await self.interaction.advance_to_key(user_key, endpoint)
                yield event.plain_result(f"{hint}Endpoint 已记录，请发送 API Key：")
                return

            if state.step == STEP_API_KEY:
                if not self._looks_like_api_key(text):
                    return
                event.stop_event()
                await self.interaction.set_key(user_key, text)
                # 远程验证与创建是耗时请求：交给后台任务，这里立即返回
                started = self._start_background(
                    user_key,
                    event.unified_msg_origin,
                    lambda: self._credential_work(
                        user_key,
                        state,
                        event.unified_msg_origin,
                    ),
                    known_secrets=[text],
                )
                yield event.plain_result(
                    (
                        "正在验证新的 API Key，稍后把结果发给你。"
                        if state.command == "set_apikey"
                        else "正在验证 Endpoint 与 API Key，稍后把结果发给你。"
                    )
                    if started
                    else "上一次请求仍在进行中，请稍后再试。",
                )
                return

            if state.step == STEP_REMOVE_CONFIRM:
                # 普通聊天消息不能被当成确认：只有形态像确认码时才回应
                if not self._looks_like_token(text):
                    return
                event.stop_event()
                async for result in self._handle_remove_confirm(event, state, text):
                    yield result
                return

            if state.step == STEP_PERSONA_NAME:
                event.stop_event()
                async for result in self._handle_persona_name(event, state, text):
                    yield result
                return

            if state.step == STEP_PERSONA_PROMPT:
                event.stop_event()
                async for result in self._handle_persona_prompt(event, state, text):
                    yield result
                return
        except StproError as exc:
            yield event.plain_result(str(exc))
        except Exception as exc:
            logger.exception(f"[stpro] 多轮输入处理失败: {exc}")
            await self.interaction.cancel(user_key)
            yield event.plain_result("当前操作处理失败，已取消，请稍后重试。")
            return

    # ---------- 后台任务：所有远程请求都不占用命令处理链路 ----------

    def _start_background(
        self,
        user_key: str,
        reply_umo: str,
        work: Any,
        known_secrets: list[str] | None = None,
    ) -> bool:
        """把耗时的远程请求放到后台任务，命令 handler 立即返回。

        同一用户同时只允许一个后台远程任务（`_remote_tasks` 去重）。完成后用
        `context.send_message` 主动通知，符合文档第 10 节的主动通知方式。

        `known_secrets`：本次流程涉及的 Key。写日志前会先抹除，避免未知异常的
        消息里回显 Key（第 18 节）。
        """
        pending = self._remote_tasks.get(user_key)
        if pending is not None and not pending.done():
            return False

        async def runner() -> None:
            try:
                text = await work()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error(
                    scrub_text(f"[stpro] 后台任务失败: {exc}", known_secrets or []),
                )
                text = "处理时发生未知错误，请稍后重试。"
            if text:
                await self._send_text(reply_umo, text)

        task = asyncio.create_task(runner())
        self._remote_tasks[user_key] = task

        def _remove_finished(done: asyncio.Task) -> None:
            if self._remote_tasks.get(user_key) is done:
                self._remote_tasks.pop(user_key, None)

        task.add_done_callback(_remove_finished)
        return True

    async def _credential_work(
        self,
        user_key: str,
        state: Any,
        owner_umo: str,
    ) -> str:
        """`new` / `set` 的远程验证与写入，返回要发给用户的文本。"""
        endpoint = state.candidate_endpoint or ""
        api_key = state.candidate_api_key or ""
        try:
            if state.command == "new":
                record, models = await self.profile_service.create_profile(
                    user_key,
                    owner_umo,
                    state.candidate_name or "",
                    endpoint,
                    api_key,
                )
                self._refresh_alignment_config_options()
                await self.interaction.remember_models(
                    user_key,
                    models,
                    record.profile_id,
                )
                await self.interaction.finish(user_key)
                return (
                    f"已创建档案「{record.name}」，并同步到 AstrBot 配置中心。"
                    "请继续选择模型。\n可用模型：\n"
                    + self._render_models(models, record)
                    + f"\n请选择：/stpro model {record.name} <模型序号或名称>"
                )

            record = await self.store.get_profile(state.profile_id)
            if record is None:
                await self.interaction.finish(user_key)
                return "配置已不存在，操作取消。"

            result = await self.profile_service.replace_credentials(
                record,
                endpoint if state.command in ("set_endpoint", "set_all") else None,
                api_key if state.command in ("set_apikey", "set_all") else None,
            )
            await self.interaction.remember_models(
                user_key, result.models, record.profile_id
            )
            await self.interaction.finish(user_key)
            if result.model_kept:
                return f"已更新档案「{record.name}」，当前模型和群绑定保持不变。"
            await self.binding_service.pause_bindings(record.profile_id)
            return (
                f"已更新档案「{record.name}」，但原模型已不可用，相关群绑定已暂停。"
                "\n可用模型：\n"
                + self._render_models(result.models, record)
                + f"\n请重新选择：/stpro model {record.name} <模型序号或名称>"
            )
        except StproError as exc:
            return await self._remote_failure_text(user_key, exc)
        finally:
            # 无论成功失败都清掉内存里的 Key 引用
            current = await self.interaction.get(user_key)
            if current is not None:
                current.clear_secret()

    async def _models_work(self, user_key: str, record: Any) -> str:
        """刷新模型列表（远程请求），返回带编号的列表文本。"""
        try:
            models = await self.profile_service.refresh_models(record)
        except StproError as exc:
            return str(exc)
        await self.interaction.remember_models(user_key, models, record.profile_id)
        snapshot = self.bridge.inspect(record.provider_id)
        return (
            f"档案「{record.name}」的可用模型：\n"
            + self._render_models(models, record, snapshot.model)
            + f"\n请选择：/stpro model {record.name} <模型序号或名称>"
        )

    async def _update_work(self, records: list[Any]) -> str:
        """只读健康检查（远程请求），返回汇总文本。"""
        lines = []
        for record in records:
            if self.monitor.is_checking(record.profile_id):
                lines.append(f"· {record.name}：检查进行中")
                continue
            try:
                await self.monitor.check_profile(record)
            except Exception as exc:
                lines.append(f"· {record.name}：检查失败（{exc}）")
                continue
            refreshed = await self.store.get_profile(record.profile_id) or record
            lines.append(
                f"· {record.name}：{self._health_text(refreshed.health_status)}；"
                f"连续失败 {refreshed.consecutive_failures} 次；"
                f"耗时 {refreshed.last_latency_ms or '-'} ms",
            )
        return "检查结果：\n" + "\n".join(lines)

    async def _remote_failure_text(self, user_key: str, exc: StproError) -> str:
        """远程失败文案：本地格式错误不计次数，远程失败累计三次取消操作。

        第 5 节：明确错误累计三次后"取消操作并清除临时输入"，因此这里用 `cancel`
        （清除 Endpoint、Key、模型列表与重试次数），而不是保留缓存的 `finish`。
        """
        if not exc.counts_as_remote_failure:
            return str(exc)
        _, exceeded = await self.interaction.count_remote_failure(user_key)
        if exceeded:
            await self.interaction.cancel(user_key)
            return f"{exc}\n连续失败已达上限，本次操作已取消，临时输入已清除。"
        return f"{exc}\n请重新输入 API Key（或发送新的 Endpoint）："

    async def _handle_remove_confirm(
        self,
        event: AstrMessageEvent,
        state: Any,
        text: str,
    ) -> None:
        user_key = self._user_key(event)
        if not self.interaction.verify_token(state, text.strip()):
            await self.interaction.cancel(user_key)
            yield event.plain_result("确认码不匹配，删除已取消。")
            return

        record = await self.store.get_profile(state.profile_id)
        if record is None:
            await self.interaction.finish(user_key)
            yield event.plain_result("配置已不存在。")
            return

        # 删除顺序：解除群绑定 → 删除 Provider 与原生配置 → 删除所有权记录。
        # 部分步骤失败时保留记录供对账继续处理，并如实报告"未完全完成"（第 6 节）。
        failed: list[str] = []
        for binding in await self.store.list_bindings(record.profile_id):
            try:
                outcome = await self.binding_service.unbind(
                    binding.manager_key,
                    binding.group_umo,
                )
                if not outcome.ok:
                    failed.append(binding.group_umo)
            except Exception as exc:
                logger.error(f"[stpro] 删除前解绑失败 umo={binding.group_umo}: {exc}")
                failed.append(binding.group_umo)

        if failed:
            yield event.plain_result(
                f"有 {len(failed)} 个群暂时无法解除绑定，删除已停止。"
                "档案和剩余记录均已保留，请稍后重试。",
            )
            return

        try:
            await self.persona_service.assert_profile_personas_deletable(record)
        except StproError as exc:
            yield event.plain_result(f"档案删除已停止：{exc}")
            return

        try:
            await self.profile_service.remove_profile(record)
            self._refresh_alignment_config_options()
        except Exception as exc:
            logger.error(f"[stpro] 删除 Profile 失败: {exc}")
            yield event.plain_result(
                f"删除未完全完成：{exc}\n"
                "档案记录仍然保留，稍后可用 `/stpro remove` 重试。",
            )
            return

        persona_failures = await self.persona_service.delete_all_for_profile(
            record.profile_id
        )

        await self.interaction.finish(user_key)
        if persona_failures:
            yield event.plain_result(
                f"已删除档案「{record.name}」，但有 {len(persona_failures)} 个人格"
                "暂时无法清理，映射已保留供后续对账。"
            )
            return
        yield event.plain_result(f"已删除档案「{record.name}」。")

    async def _handle_persona_name(
        self,
        event: AstrMessageEvent,
        state: Any,
        text: str,
    ) -> None:
        user_key = self._user_key(event)
        profile = await self.store.get_profile(state.profile_id)
        if profile is None:
            await self.interaction.cancel(user_key)
            yield event.plain_result("配置已不存在，操作取消。")
            return
        try:
            if state.command == "persona_new_name":
                name = await self.persona_service.assert_name_available(profile, text)
                state.command = "persona_new"
                await self.interaction.set_persona_name(
                    user_key, name, prompt_next=True
                )
                yield event.plain_result(f"正在创建人格「{name}」。请发送人格提示词。")
                return
            if state.command == "persona_set_name":
                selected = await self.persona_service.select_persona(
                    profile, user_key, text
                )
                await self.interaction.finish(user_key)
                yield event.plain_result(
                    f"已将档案「{profile.name}」的人格切换为「{selected}」。"
                )
                return
            if state.command == "persona_del_name":
                deleted = await self.persona_service.delete_persona(
                    profile, user_key, text
                )
                await self.interaction.finish(user_key)
                yield event.plain_result(f"已删除人格「{deleted.name}」。")
        except StproError as exc:
            yield event.plain_result(str(exc))

    async def _handle_persona_prompt(
        self,
        event: AstrMessageEvent,
        state: Any,
        text: str,
    ) -> None:
        user_key = self._user_key(event)
        profile = await self.store.get_profile(state.profile_id)
        if profile is None:
            await self.interaction.cancel(user_key)
            yield event.plain_result("配置已不存在，操作取消。")
            return
        name = state.candidate_persona_name or ""
        try:
            if state.command == "persona_new":
                record = await self.persona_service.create_persona(
                    profile, user_key, name, text
                )
                message = (
                    f"已创建人格「{record.name}」。需要启用时请执行："
                    f"/stpro persona {profile.name} set {record.name}"
                )
            elif state.command == "persona_edit":
                record = await self.persona_service.update_persona(
                    profile, user_key, name, text
                )
                message = f"已更新人格「{record.name}」。"
            else:
                await self.interaction.cancel(user_key)
                return
        except StproError as exc:
            yield event.plain_result(str(exc))
            return
        except Exception as exc:
            logger.exception(f"[stpro] Persona 保存失败: {exc}")
            await self.interaction.cancel(user_key)
            yield event.plain_result("人格保存失败，请稍后重试。")
            return
        await self.interaction.finish(user_key)
        yield event.plain_result(message)

    # ---------- 辅助 ----------

    def _refresh_alignment_config_options(self) -> None:
        """把当前 AstrBot 原生配置列表注入插件设置下拉框。"""
        schema = getattr(self.conf, "schema", None)
        if not isinstance(schema, dict):
            return
        item = schema.get("alignment_config_id")
        if not isinstance(item, dict):
            return

        options: list[str] = []
        labels: list[str] = []
        try:
            profiles = self.context.astrbot_config_mgr.get_conf_list()
        except Exception as exc:
            logger.warning(f"[stpro] 获取对齐配置下拉列表失败: {exc}")
            return

        for profile in profiles or []:
            if isinstance(profile, dict):
                config_id = str(profile.get("id") or "").strip()
                name = str(profile.get("name") or config_id).strip()
            else:
                config_id = str(getattr(profile, "id", "") or "").strip()
                name = str(getattr(profile, "name", "") or config_id).strip()
            if not config_id or config_id in options:
                continue
            options.append(config_id)
            labels.append(f"{name}（{config_id}）" if name != config_id else config_id)

        if "default" not in options:
            options.insert(0, "default")
            labels.insert(0, "default（全局默认配置）")
        item["options"] = options
        item["labels"] = labels

    async def _ensure_help_image(self, *, force: bool = False) -> Path:
        if force or not self.help_image_path.is_file():
            from .tools.render_help import render

            await asyncio.to_thread(render, self.help_image_path)
        return self.help_image_path

    async def _begin_command(self, event: AstrMessageEvent) -> str:
        """新 /stpro 命令先取消旧流程，返回需要前置展示的提示。"""
        user_key = self._user_key(event)
        old = await self.interaction.get(user_key)
        pending = self._remote_tasks.get(user_key)
        cancelled_remote = pending is not None and not pending.done()
        if cancelled_remote:
            pending.cancel()
            try:
                await pending
            except (asyncio.CancelledError, Exception):
                pass
            if self._remote_tasks.get(user_key) is pending:
                self._remote_tasks.pop(user_key, None)
        if old is None or not old.active:
            return "（已取消上一项操作）\n" if cancelled_remote else ""
        old_command = old.command
        await self.interaction.cancel(user_key)
        return f"（已取消未完成的「{old_command}」操作）\n"

    def _user_key(self, event: AstrMessageEvent) -> str:
        return f"{event.get_platform_id()}:{event.get_sender_id()}"

    @staticmethod
    def _is_self_message(event: AstrMessageEvent) -> bool:
        """判断是否是机器人自己发出的消息。

        平台（如开启"上报自身消息"的 NapCat）会把机器人自己发出的消息再上报一次。
        若不忽略，插件自己的提示语会被当成用户的多轮输入，导致刚提示"请输入
        Endpoint"就立刻报 Endpoint 格式错误。

        两种特征都要判断：
        - 发送者就是机器人自己（sender_id == self_id）；
        - OneBot 的 `message_sent` 事件（此时 `user_id` 是接收方，不能靠 self_id 判断）。
        """
        try:
            self_id = str(event.get_self_id() or "")
            if self_id and str(event.get_sender_id() or "") == self_id:
                return True
        except Exception:
            pass

        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if isinstance(raw, dict) and raw.get("post_type") == "message_sent":
            return True

        # 部分实现上报自身消息时 post_type 仍是 message，但 sender 是机器人自己
        if isinstance(raw, dict) and raw.get("self_id"):
            raw_sender = raw.get("sender")
            if isinstance(raw_sender, dict) and str(
                raw_sender.get("user_id") or "",
            ) == str(raw.get("self_id")):
                return True
        return False

    @staticmethod
    def _is_stpro_command_text(text: str) -> bool:
        """是否为会由 AstrBot 命令处理器接管的 STPRO 命令文本。"""
        normalized = text.strip().casefold()
        if normalized.startswith("/"):
            normalized = normalized[1:].lstrip()
        return bool(normalized) and normalized.split(maxsplit=1)[0] == "stpro"

    @staticmethod
    def _looks_like_endpoint(text: str) -> bool:
        """是否像一条 Endpoint 输入。

        含空白的消息（提示语、闲聊、LLM 回复）一律不算；其余带 `.`/`:`/`/`
        的连续字符串才交给正式校验，这样既挡住回声，又能给真正输错格式的用户
        （如少了 `https://`）保留格式提示。
        """
        if any(ch.isspace() for ch in text):
            return False
        if text.lower().startswith(("http://", "https://")):
            return True
        return any(sep in text for sep in (".", ":", "/"))

    @staticmethod
    def _looks_like_api_key(text: str) -> bool:
        """API Key 是不含空白的连续字符串。"""
        return bool(text) and not any(ch.isspace() for ch in text)

    @staticmethod
    def _looks_like_token(text: str) -> bool:
        """确认码是不含空白、长度足够的字符串。"""
        return bool(text) and not any(ch.isspace() for ch in text) and len(text) >= 8

    def _require_private(self, event: AstrMessageEvent) -> bool:
        return event.get_message_type() == MessageType.FRIEND_MESSAGE

    def _build_group_umo(self, platform_id: str, group_id: str) -> str:
        return str(MessageSession(platform_id, MessageType.GROUP_MESSAGE, group_id))

    def _resolve_group_umo(
        self,
        event: AstrMessageEvent,
        group_id: str | None,
    ) -> str:
        """群聊只操作当前群；私聊必须给出群号。"""
        if event.get_message_type() == MessageType.GROUP_MESSAGE:
            gid = event.get_group_id()
            if not gid:
                raise CapabilityUnavailable("无法解析当前群的会话标识。")
            return self._build_group_umo(event.get_platform_id(), gid)

        if not group_id:
            raise StproError("local", "私聊中请给出群号：/stpro bind <配置名> <群号>")
        if not group_id.isdigit():
            raise StproError("local", "群号必须是数字。")
        return self._build_group_umo(event.get_platform_id(), group_id)

    @staticmethod
    def _render_models(models: list[str], record: Any, current_model: str = "") -> str:
        if not models:
            return "没有获取到可用模型。"
        lines = []
        for i, model in enumerate(models, start=1):
            mark = " ← 当前" if current_model and model == current_model else ""
            lines.append(f"{i}. {model}{mark}")
        return "\n".join(lines)

    @staticmethod
    def _render_summary(summary: dict[str, Any]) -> str:
        status_map = {
            "unconfigured": "未配置",
            "configured": "已配置",
            "missing": "配置缺失",
        }
        health_map = {
            "unknown": "未知",
            "healthy": "正常",
            "degraded": "降级",
            "unhealthy": "异常",
        }
        binding_map = {
            "active": "生效中",
            "paused": "已暂停",
            "admin_overridden": "管理员已接管",
        }
        bindings = summary.get("bindings") or []
        if bindings:
            binding_text = "\n".join(
                "  · {group}：{state}".format(
                    group=b["group_umo"],
                    state=binding_map.get(b["state"], b["state"]),
                )
                for b in bindings
            )
        else:
            binding_text = "  （无）"

        return (
            f"档案名：{summary['name']}\n"
            f"Endpoint：{summary['endpoint'] or '-'}\n"
            f"API Key：{summary['api_key'] or '-'}\n"
            f"模型：{summary['model']}\n"
            f"启用：{'是' if summary['enable'] else '否'}\n"
            f"WebUI 配置：{summary.get('astrbot_config_name') or '-'}\n"
            f"配置文件 ID：{summary.get('astrbot_config_id') or '-'}\n"
            f"配置状态：{status_map.get(summary['config_status'], summary['config_status'])}\n"
            f"健康状态：{health_map.get(summary['health_status'], summary['health_status'])}\n"
            f"AstrBot 管理员接管：{'是' if summary['admin_managed'] else '否'}\n"
            f"绑定群：\n{binding_text}"
        )

    @staticmethod
    def _health_text(status: str) -> str:
        return {
            "unknown": "尚未检查",
            "healthy": "可用",
            "degraded": "暂时异常",
            "unhealthy": "不可用",
        }.get(status, status)


def _plugin_data_dir() -> Any:
    from pathlib import Path

    path = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path
