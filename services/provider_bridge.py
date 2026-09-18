"""ProviderBridge —— 插件与 AstrBot Provider 系统之间的 Adapter。

设计文档第 12 节：插件内部其他模块不得了解 `provider_insts`、`inst_map` 或主配置
文件格式，只能通过一个集中 Adapter 调用 ProviderManager。所有权检查、内部写入标记、
比较后写入、原生异常转换和配置脱敏都必须集中在这里。

已在 v4.28.0 源码核对的行为（详见设计文档第 21 节）：

- `update_provider` 是全量替换，必须先读回现有配置再合并；
- 会话级规则写在 `sp` 里（scope=`umo`，key=`provider_perf_chat_completion`），
  不进 AstrBot 配置文件；删除规则用 `sp.session_remove`；
- Provider 配置的增删改**不触发** Provider Hook，只有 `set_provider` 会触发；
- 默认聊天 Provider 位于 `agent_runner.config.model.provider_id`，仅当
  `runner_type == "local"` 时生效。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger, sp
from astrbot.api.provider import ProviderType

PROVIDER_ID_PREFIX = "stpro_"
PROVIDER_DISPLAY_PREFIX = "STPRO / "
CHAT_RULE_KEY = "provider_perf_chat_completion"

# 参与指纹计算的有效配置字段（第 8.3 节）：
# 纳入会影响请求行为的字段，排除字段顺序、运行时对象和纯展示字段。
_FINGERPRINT_FIELDS = (
    "api_base",
    "key",
    "model",
    "enable",
    "timeout",
    "proxy",
    "custom_headers",
    "provider_type",
    "type",
)

# 内部写入标记的默认存活时间（秒）
_MARKER_TTL = 30.0


@dataclass
class ProviderSnapshot:
    """某 `stpro_` Provider 的原生事实快照。"""

    provider_id: str
    exists: bool = False
    endpoint: str = ""
    model: str = ""
    enable: bool = False
    loaded: bool = False  # 是否已在 inst_map 中（enable=True 且实例化成功）
    fingerprint: str | None = None

    @property
    def usable(self) -> bool:
        """能否被绑定：必须存在、已启用、已选模型且已加载。"""
        return self.exists and self.enable and bool(self.model) and self.loaded


@dataclass
class GroupRouteSnapshot:
    """某群当前的路由事实快照（第 8.4 节基线要素）。"""

    group_umo: str
    rule_provider_id: str | None  # 原始会话规则值；None 表示规则不存在
    config_id: str = ""
    runner_type: str | None = None
    default_chat_provider_id: str | None = None
    route_source: str = "fallback_default"
    matched_route_pattern: str | None = None
    matched_route_config_id: str | None = None


@dataclass
class InternalWriteMarker:
    """内部写入标记（第 8.3 节）。

    仅用"最近几秒内发生"判断不安全，标记必须能表达"预期的旧值/新值"。
    """

    op_id: str
    obj_type: str  # provider / group_rule
    obj_id: str
    expected_old: str | None
    expected_new: str | None
    expires_at: float


@dataclass
class BridgeResult:
    """比较后写入的结果。"""

    applied: bool
    reason: str  # ok / rule_changed / route_changed / provider_mismatch / noop


@dataclass
class ProviderBridge:
    """集中封装所有对 AstrBot 原生 Provider 与 UMO 路由的读写。"""

    context: Any
    store: Any = None  # OwnershipStore，用于"所有权记录中存在"的校验
    markers: list[InternalWriteMarker] = field(default_factory=list)
    on_external_rule_change: Callable[[str, str], Awaitable[None]] | None = None
    _hook_registered: bool = False

    # ---------- 内部写入标记 ----------

    def _add_marker(
        self,
        obj_type: str,
        obj_id: str,
        expected_old: str | None,
        expected_new: str | None,
    ) -> str:
        op_id = uuid.uuid4().hex
        self.markers.append(
            InternalWriteMarker(
                op_id=op_id,
                obj_type=obj_type,
                obj_id=obj_id,
                expected_old=expected_old,
                expected_new=expected_new,
                expires_at=time.monotonic() + _MARKER_TTL,
            ),
        )
        return op_id

    def consume_marker(
        self,
        obj_type: str,
        obj_id: str,
        new_value: str | None,
    ) -> bool:
        """Hook 事件能否匹配到一个尚未消费的内部标记。

        匹配成功即消费（返回 True，视为插件自身写入）；不匹配或已过期则按外部
        变化处理。
        """
        now = time.monotonic()
        for marker in list(self.markers):
            if marker.expires_at < now:
                self.markers.remove(marker)
                continue
            if (
                marker.obj_type == obj_type
                and marker.obj_id == obj_id
                and marker.expected_new == new_value
            ):
                self.markers.remove(marker)
                return True
        return False

    def register_change_hook(self) -> None:
        """注册 Provider 变化 Hook。

        注意：该 Hook 只覆盖 `set_provider`（会话级规则写入）。Provider 配置的
        增删改不会触发，必须依赖对账。
        """
        if self._hook_registered:
            return
        self.context.provider_manager.register_provider_change_hook(
            self._on_provider_change
        )
        self._hook_registered = True

    def _on_provider_change(
        self,
        provider_id: str,
        provider_type: Any,
        umo: str | None,
    ) -> None:
        if not umo:
            return
        if provider_type != ProviderType.CHAT_COMPLETION:
            return
        if self.consume_marker("group_rule", umo, provider_id):
            logger.debug(f"[stpro] 会话规则变更匹配到内部写入标记: umo={umo}")
            return
        # 外部（管理员/其他插件）改写了某会话的聊天 Provider 规则
        if self.is_owned_provider_id(provider_id):
            logger.info(
                f"[stpro] 检测到外部写入 STPRO 会话规则: umo={umo} provider={provider_id}",
            )
        self._notify_external_rule_change(umo, provider_id)

    def _notify_external_rule_change(self, umo: str, provider_id: str) -> None:
        if self.on_external_rule_change is None:
            return
        import asyncio

        try:
            asyncio.get_running_loop().create_task(
                self.on_external_rule_change(umo, provider_id),
            )
        except RuntimeError:
            logger.debug("[stpro] 无事件循环，跳过外部规则变更回调")

    # ---------- 读取 ----------

    @staticmethod
    def is_owned_provider_id(provider_id: str) -> bool:
        return bool(provider_id) and provider_id.startswith(
            (PROVIDER_ID_PREFIX, PROVIDER_DISPLAY_PREFIX),
        )

    async def assert_owned(self, provider_id: str) -> None:
        """只能操作 STPRO 命名且存在于插件所有权记录中的 Provider。

        非 STPRO Provider 或没有所有权记录的孤立 Provider 一律拒绝，
        包括修改、选模型与删除。
        """
        if not self.is_owned_provider_id(provider_id):
            raise PermissionError(f"拒绝操作非 STPRO Provider: {provider_id}")
        if self.store is None:
            return
        owned = any(
            record.provider_id == provider_id
            for record in await self.store.list_profiles()
        )
        if not owned:
            raise PermissionError(
                f"Provider {provider_id} 不在插件所有权记录中，拒绝操作",
            )

    def get_provider_config(self, provider_id: str) -> dict[str, Any] | None:
        """读取原生 Provider 配置（深拷贝）。不存在返回 None。"""
        if not self.is_owned_provider_id(provider_id):
            return None
        return self.context.provider_manager.get_provider_config_by_id(provider_id)

    def inspect(self, provider_id: str) -> ProviderSnapshot:
        """读取 Provider 的当前事实。**先读事实**，再做判断。"""
        config = self.get_provider_config(provider_id)
        if config is None:
            return ProviderSnapshot(provider_id=provider_id, exists=False)

        inst = self.context.provider_manager.inst_map.get(provider_id)

        return ProviderSnapshot(
            provider_id=provider_id,
            exists=True,
            endpoint=str(config.get("api_base", "") or ""),
            model=str(config.get("model", "") or ""),
            enable=bool(config.get("enable", False)),
            loaded=inst is not None,
            fingerprint=self.fingerprint(config),
        )

    @staticmethod
    def fingerprint(config: dict[str, Any]) -> str:
        """由规范化后的有效配置生成不可逆摘要，用于比较而不是恢复数据。

        只保存摘要，不保存第二份 Key；日志不得输出摘要的原始输入。
        """
        canonical = {}
        for name in _FINGERPRINT_FIELDS:
            value = config.get(name)
            if isinstance(value, list):
                value = sorted(str(v) for v in value)
            elif isinstance(value, dict):
                value = {str(k): str(v) for k, v in sorted(value.items())}
            canonical[name] = value
        payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False).encode(
            "utf-8"
        )
        return hashlib.sha256(payload).hexdigest()

    async def read_group_rule(self, group_umo: str) -> str | None:
        """读取会话级聊天 Provider 规则的原始值。

        返回 None 表示规则不存在；返回 "" 表示规则被显式设为空（二者可区分）。
        """
        return await sp.get_async("umo", group_umo, CHAT_RULE_KEY, None)

    def inspect_group_route_sync(self, group_umo: str) -> GroupRouteSnapshot:
        """读取群的路由事实：会话规则、所属配置文件、默认聊天 Provider。"""
        route_source = "fallback_default"
        matched_pattern = None
        matched_config_id = None
        router = getattr(self.context.astrbot_config_mgr, "ucr", None)
        routing = getattr(router, "umop_to_conf_id", {})
        if isinstance(routing, dict):
            target = self._split_umo(group_umo)
            if target is not None:
                for pattern, route_config_id in routing.items():
                    parts = self._split_umo(pattern)
                    if parts is None:
                        continue
                    if all(
                        part == "" or fnmatch.fnmatchcase(value, part)
                        for part, value in zip(parts, target)
                    ):
                        route_source = "explicit_route"
                        matched_pattern = pattern
                        matched_config_id = str(route_config_id)
                        break

        conf_info = self.context.astrbot_config_mgr.get_conf_info(group_umo)
        conf_id = str((conf_info or {}).get("id", "") or "")

        conf = self.context.get_config(group_umo)
        runner_type = None
        default_provider = None
        try:
            agent_runner = conf.get("agent_runner") or {}
            runner_type = agent_runner.get("runner_type")
            if runner_type == "local":
                default_provider = (
                    agent_runner.get("config", {}).get("model", {}).get("provider_id")
                    or None
                )
        except Exception as exc:  # 配置结构异常不应中断读取
            logger.warning(f"[stpro] 读取群默认 Provider 失败: {exc}")

        return GroupRouteSnapshot(
            group_umo=group_umo,
            rule_provider_id=None,  # 由调用方 await read_group_rule 填充
            config_id=conf_id,
            runner_type=runner_type,
            default_chat_provider_id=default_provider,
            route_source=route_source,
            matched_route_pattern=matched_pattern,
            matched_route_config_id=matched_config_id,
        )

    @staticmethod
    def _split_umo(umo: str) -> tuple[str, str, str] | None:
        if not isinstance(umo, str):
            return None
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return None
        return parts[0], parts[1], parts[2]

    @staticmethod
    def route_matches_baseline(
        route: GroupRouteSnapshot,
        *,
        route_source: str,
        matched_route_pattern: str | None,
        matched_route_config_id: str | None,
        expected_config_id: str,
        expected_default_provider: str | None,
    ) -> bool:
        if route.route_source != route_source:
            return False
        if route_source == "fallback_default":
            return True
        return (
            route.matched_route_pattern == matched_route_pattern
            and route.matched_route_config_id == matched_route_config_id
            and route.config_id == expected_config_id
            and route.default_chat_provider_id == expected_default_provider
        )

    async def inspect_group_route(self, group_umo: str) -> GroupRouteSnapshot:
        snapshot = self.inspect_group_route_sync(group_umo)
        snapshot.rule_provider_id = await self.read_group_rule(group_umo)
        return snapshot

    # ---------- 写入：Provider ----------

    async def create_unconfigured(
        self,
        profile_id: str,
        display_name: str,
        endpoint: str,
        api_key: str,
    ) -> str:
        """创建尚未选模型的 Provider：`model=""`、`enable=False`。

        只有完成远程验证后才应调用本方法。
        """
        provider_id = display_name
        config = {
            "id": provider_id,
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": False,
            "key": [api_key],
            "api_base": endpoint,
            "model": "",
            "timeout": 120,
            "proxy": "",
            "custom_headers": {},
        }
        self._add_marker("provider", provider_id, None, self.fingerprint(config))
        await self.context.provider_manager.create_provider(config)
        return provider_id

    async def replace_credentials(
        self,
        provider_id: str,
        endpoint: str,
        api_key: str,
        model: str | None,
    ) -> None:
        """整体替换 Endpoint/Key（保留其他原生字段），模型由调用方决定。

        因为 `update_provider` 是全量替换，必须先读回当前配置再改字段，
        否则会清掉管理员在 WebUI 新增的合法字段。
        """
        await self.assert_owned(provider_id)
        current = self.get_provider_config(provider_id)
        if current is None:
            raise KeyError(f"Provider {provider_id} 不存在")

        new_config = dict(current)
        new_config["api_base"] = endpoint
        new_config["key"] = [api_key]
        # model 为 None 表示"原模型不在新列表中"：必须清空模型（第 4 节）
        new_config["model"] = model or ""
        new_config["enable"] = bool(model)

        self._add_marker(
            "provider",
            provider_id,
            self.fingerprint(current),
            self.fingerprint(new_config),
        )
        await self.context.provider_manager.update_provider(provider_id, new_config)

    async def select_model(self, provider_id: str, model_id: str) -> None:
        """写入模型 ID 并启用 Provider。"""
        await self.assert_owned(provider_id)
        current = self.get_provider_config(provider_id)
        if current is None:
            raise KeyError(f"Provider {provider_id} 不存在")

        new_config = dict(current)
        new_config["model"] = model_id
        new_config["enable"] = True

        self._add_marker(
            "provider",
            provider_id,
            self.fingerprint(current),
            self.fingerprint(new_config),
        )
        await self.context.provider_manager.update_provider(provider_id, new_config)

    async def set_model_and_enable(
        self,
        provider_id: str,
        model_id: str | None,
        enable: bool,
    ) -> None:
        """只改模型/启用状态，不动 Endpoint 与 Key。"""
        await self.assert_owned(provider_id)
        current = self.get_provider_config(provider_id)
        if current is None:
            raise KeyError(f"Provider {provider_id} 不存在")

        new_config = dict(current)
        # model_id 为空串表示清空模型（第 4 节：原模型不在新列表中）
        if model_id is not None:
            new_config["model"] = model_id
        new_config["enable"] = enable

        self._add_marker(
            "provider",
            provider_id,
            self.fingerprint(current),
            self.fingerprint(new_config),
        )
        await self.context.provider_manager.update_provider(provider_id, new_config)

    async def delete_owned(
        self,
        provider_id: str,
        *,
        skip_ownership_check: bool = False,
    ) -> bool:
        """删除本插件名下的 Provider。已被删除则返回 False（空操作）。

        `skip_ownership_check` 仅供创建补偿使用：此时所有权记录还没写进去，
        但必须把刚创建的原生 Provider 删掉（第 4 节要求）。
        """
        if not skip_ownership_check:
            await self.assert_owned(provider_id)
        elif not self.is_owned_provider_id(provider_id):
            raise PermissionError(f"拒绝删除非 STPRO Provider: {provider_id}")

        if self.get_provider_config(provider_id) is None:
            return False
        self._add_marker(
            "provider",
            provider_id,
            self.fingerprint(
                self.get_provider_config(provider_id) or {},
            ),
            None,
        )
        await self.context.provider_manager.delete_provider(provider_id=provider_id)
        return True

    # ---------- 兼容旧版会话 Provider 覆盖 ----------

    async def remove_legacy_group_rule(
        self,
        group_umo: str,
        expected_provider_id: str,
    ) -> bool:
        """仅当旧会话规则仍为预期值时删除，供 v1→v2 迁移使用。"""
        current = await self.read_group_rule(group_umo)
        if current != expected_provider_id:
            return False
        self._add_marker("group_rule", group_umo, expected_provider_id, None)
        await sp.session_remove(group_umo, CHAT_RULE_KEY)
        return True

    # ---------- 旧版群规则写入（仅保留兼容与迁移测试）----------

    async def bind_group(
        self,
        group_umo: str,
        provider_id: str,
        *,
        expected_rule: str | None,
        expected_config_id: str,
        expected_default_provider: str | None,
        expected_route_source: str = "fallback_default",
        expected_route_pattern: str | None = None,
        expected_route_config_id: str | None = None,
    ) -> BridgeResult:
        """比较后写入：只有当前事实仍等于预期时才写规则。

        Args:
            expected_rule: 写入前观察到的会话规则值（None 表示不存在）。
            expected_config_id: 写入前观察到的所属配置文件 ID。
            expected_default_provider: 写入前观察到的默认聊天 Provider。
        """
        # 提交前重新读取当前事实
        current_rule = await self.read_group_rule(group_umo)
        route = self.inspect_group_route_sync(group_umo)

        if current_rule != expected_rule:
            return BridgeResult(False, "rule_changed")
        if not self.route_matches_baseline(
            route,
            route_source=expected_route_source,
            matched_route_pattern=expected_route_pattern,
            matched_route_config_id=expected_route_config_id,
            expected_config_id=expected_config_id,
            expected_default_provider=expected_default_provider,
        ):
            return BridgeResult(False, "route_changed")
        if current_rule is not None and not self.is_owned_provider_id(current_rule):
            # 已存在管理员设置的非 STPRO 规则，绝不覆盖
            return BridgeResult(False, "rule_changed")

        self._add_marker("group_rule", group_umo, expected_rule, provider_id)
        await self.context.provider_manager.set_provider(
            provider_id=provider_id,
            provider_type=ProviderType.CHAT_COMPLETION,
            umo=group_umo,
        )
        return BridgeResult(True, "ok")

    async def compare_and_unbind(
        self,
        binding_written_provider_id: str,
        group_umo: str,
        *,
        restore_provider_id: str | None,
        restore_rule_existed: bool,
        expected_config_id: str,
        expected_default_provider: str | None,
        expected_route_source: str = "fallback_default",
        expected_route_pattern: str | None = None,
        expected_route_config_id: str | None = None,
    ) -> BridgeResult:
        """普通解绑/删除/替换绑定：比较后删除覆盖，并在安全时恢复绑定前的规则。

        条件（第 7.2 节）全部成立才恢复历史值：
        1. 当前规则仍等于绑定记录的 `written_provider_id`；
        2. 配置路由与相关默认 Provider 未相对基线发生管理员变化。
        """
        current_rule = await self.read_group_rule(group_umo)
        route = self.inspect_group_route_sync(group_umo)

        if current_rule != binding_written_provider_id:
            # 管理员已改成其他值：不得覆盖，也不得恢复历史值
            return BridgeResult(False, "rule_changed")
        if not self.route_matches_baseline(
            route,
            route_source=expected_route_source,
            matched_route_pattern=expected_route_pattern,
            matched_route_config_id=expected_route_config_id,
            expected_config_id=expected_config_id,
            expected_default_provider=expected_default_provider,
        ):
            return BridgeResult(False, "route_changed")

        if restore_rule_existed and restore_provider_id:
            # 绑定前确有会话级规则，且仍安全：恢复它
            self._add_marker(
                "group_rule",
                group_umo,
                binding_written_provider_id,
                restore_provider_id,
            )
            await self.context.provider_manager.set_provider(
                provider_id=restore_provider_id,
                provider_type=ProviderType.CHAT_COMPLETION,
                umo=group_umo,
            )
        else:
            # 原来没有规则或原 Provider 已删除：删除覆盖，让 AstrBot 自然回落
            self._add_marker("group_rule", group_umo, binding_written_provider_id, None)
            await sp.session_remove(group_umo, CHAT_RULE_KEY)
        return BridgeResult(True, "ok")

    async def yield_to_admin(
        self,
        binding_written_provider_id: str,
        group_umo: str,
    ) -> BridgeResult:
        """因管理员改变路由/默认 Provider 而让位。

        只删除自己的 STPRO 覆盖，绝不恢复绑定前的历史 Provider，也不把新的默认
        Provider 写成会话级规则。
        """
        current_rule = await self.read_group_rule(group_umo)
        if current_rule != binding_written_provider_id:
            # 规则已是其他值：完全不触碰，由上层标记 admin_overridden
            return BridgeResult(False, "rule_changed")

        self._add_marker("group_rule", group_umo, binding_written_provider_id, None)
        await sp.session_remove(group_umo, CHAT_RULE_KEY)
        return BridgeResult(True, "ok")
