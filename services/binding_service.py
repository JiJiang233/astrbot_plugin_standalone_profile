"""绑定业务模块：绑定管理员、群权限、基线快照与比较后 bind/unbind。

设计文档第 7 节与第 8.1/8.2 节。核心原则：

- 一个群最多只能有一个 STPRO 绑定记录；一个 Profile 可以绑定多个群；
- 首次绑定者成为绑定管理员，之后只有他能替换或解绑；
- 任何写入都是"比较后写入"，绝不覆盖 AstrBot 管理员的显式配置；
- 让位时绝不恢复绑定前的历史 Provider，也不把新的默认 Provider 写成会话级规则。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from astrbot.api import logger

from ..storage.ownership_store import BindingRecord, OwnershipStore, utc_now_iso
from ..utils.errors import CapabilityUnavailable, PermissionDenied
from ..utils.locks import LockManager
from .astrbot_profile_bridge import AstrBotProfileBridge
from .provider_bridge import ProviderBridge

STATE_ACTIVE = "active"
STATE_PAUSED = "paused"
STATE_ADMIN_OVERRIDDEN = "admin_overridden"


class MembershipResult:
    """群成员校验结果。"""

    def __init__(
        self,
        supported: bool,
        user_in_group: bool,
        bot_in_group: bool,
    ) -> None:
        self.supported = supported
        self.user_in_group = user_in_group
        self.bot_in_group = bot_in_group


class MembershipChecker(Protocol):
    """平台能力探测接口。无法确认时 `supported=False`，调用方必须拒绝执行。"""

    async def check(self, group_umo: str, user_key: str) -> MembershipResult: ...


@dataclass
class BindOutcome:
    ok: bool
    message: str
    binding: BindingRecord | None = None


class BindingService:
    """群绑定的全部业务事务。"""

    def __init__(
        self,
        store: OwnershipStore,
        bridge: ProviderBridge,
        config_bridge: AstrBotProfileBridge,
        locks: LockManager,
        membership: MembershipChecker,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.config_bridge = config_bridge
        self.locks = locks
        self.membership = membership

    # ---------- 绑定 ----------

    async def bind(
        self,
        operator_key: str,
        profile: Any,
        group_umo: str,
    ) -> BindOutcome:
        """把群 UMO 路由到该 Profile 对应的 AstrBot 原生配置文件。

        分三段执行（第 11.2 节：网络请求不应长期占用群锁）：

        1. 加锁取快照（只读、无网络请求）并做前置检查；
        2. **释放锁**后完成网络请求（群成员校验）；
        3. 重新加锁，验证快照未变化后再提交写入。

        本方法不获取用户流程锁：命令路径由调用方按"用户 → Profile → 群"的顺序在最
        外层持有用户锁；后台对账没有用户锁时按"Profile → 群"获取。
        """
        # ---- 阶段 1：锁内取快照与前置检查 ----
        async with self.locks.ordered(
            profile_id=profile.profile_id,
            group_umo=group_umo,
        ):
            snapshot = self.bridge.inspect(profile.provider_id)
            if not snapshot.usable:
                return BindOutcome(
                    False,
                    "该档案当前不可用，请先选择模型或检查档案状态。",
                )
            native = self.config_bridge.inspect(profile.astrbot_config_id)
            if not native.exists:
                return BindOutcome(False, "该档案的 AstrBot 配置文件不存在，无法绑定。")
            if native.default_provider_id != profile.provider_id:
                return BindOutcome(
                    False,
                    "该档案已由 AstrBot 管理员接管，当前不能绑定。",
                )
            route = self.config_bridge.inspect_route(group_umo)
            session_rule = await self.bridge.read_group_rule(group_umo)
            existing = await self.store.get_binding(group_umo)
            allowed_legacy_rule = (
                existing.written_provider_id
                if existing is not None and not existing.written_config_id
                else None
            )
            if session_rule is not None and session_rule != allowed_legacy_rule:
                return BindOutcome(
                    False,
                    "该群已有管理员指定的会话模型，无法覆盖。",
                )

            if existing is not None and existing.manager_key == operator_key:
                if (
                    existing.written_config_id
                    and route.exact_config_id != existing.written_config_id
                ):
                    return BindOutcome(
                        False,
                        "管理员已修改本群的会话配置，当前不能覆盖。",
                    )
            elif existing is not None:
                return BindOutcome(
                    False,
                    "该群已由其他用户绑定，只有原绑定人可以更换。",
                )
            elif route.exact_config_id is not None:
                return BindOutcome(
                    False,
                    "该群已有管理员指定的会话配置，无法覆盖。",
                )

        # ---- 阶段 2：锁外完成网络请求 ----
        membership = await self.membership.check(group_umo, operator_key)
        if not membership.supported:
            raise CapabilityUnavailable(
                "当前适配器不支持可靠群校验，无法执行绑定。",
                f"umo={group_umo}",
            )
        if not membership.user_in_group or not membership.bot_in_group:
            return BindOutcome(False, "你或机器人不在该群内，无法绑定。")

        # ---- 阶段 3：重新加锁，验证快照未变化后提交 ----
        async with self.locks.ordered(
            profile_id=profile.profile_id,
            group_umo=group_umo,
        ):
            route_now = self.config_bridge.inspect_route(group_umo)
            session_rule_now = await self.bridge.read_group_rule(group_umo)
            existing_now = await self.store.get_binding(group_umo)
            if (
                route_now != route
                or session_rule_now != session_rule
                or existing_now != existing
            ):
                # 网络请求期间事实已变化：放弃本次写入，不做任何修改
                return BindOutcome(
                    False,
                    "校验期间群配置发生变化，本次绑定已取消。",
                )

            result = await self.config_bridge.bind_group(
                group_umo,
                profile.astrbot_config_id,
                expected_exact_config_id=route.exact_config_id,
            )
            if not result.applied:
                if result.reason == "shadowed_by_route":
                    return BindOutcome(
                        False,
                        "该群被更高优先级的非默认通配会话配置覆盖，请让 AstrBot 管理员调整路由顺序。",
                    )
                return BindOutcome(
                    False,
                    "检测到管理员正在修改群配置，本次绑定已取消。",
                )

            manager_key = existing.manager_key if existing else operator_key
            record = BindingRecord(
                group_umo=group_umo,
                profile_id=profile.profile_id,
                manager_key=manager_key,
                written_provider_id=profile.provider_id,
                previous_provider_id=(
                    existing.previous_provider_id if existing else None
                ),
                previous_rule_existed=(
                    existing.previous_rule_existed if existing else False
                ),
                base_config_id=(
                    existing.base_config_id if existing else route.effective_config_id
                ),
                base_chat_provider_id=(
                    existing.base_chat_provider_id if existing else None
                ),
                base_runner_type=existing.base_runner_type if existing else None,
                route_source=existing.route_source
                if existing
                else (
                    "explicit_route" if route.matched_pattern else "fallback_default"
                ),
                matched_route_pattern=(
                    existing.matched_route_pattern
                    if existing
                    else route.matched_pattern
                ),
                matched_route_config_id=(
                    existing.matched_route_config_id
                    if existing
                    else route.matched_config_id
                ),
                baseline_fingerprint=snapshot.fingerprint,
                last_reconciled_at=utc_now_iso(),
                state=STATE_ACTIVE,
                created_at=existing.created_at if existing else utc_now_iso(),
                written_config_id=profile.astrbot_config_id,
                previous_exact_route_existed=(
                    existing.previous_exact_route_existed
                    if existing
                    else route.exact_config_id is not None
                ),
                previous_exact_config_id=(
                    existing.previous_exact_config_id
                    if existing
                    else route.exact_config_id
                ),
                previous_matched_route_pattern=(
                    existing.previous_matched_route_pattern
                    if existing
                    else route.matched_pattern
                ),
                previous_matched_route_config_id=(
                    existing.previous_matched_route_config_id
                    if existing
                    else route.matched_config_id
                ),
            )

            try:
                await self._save_binding(record)
            except Exception as exc:
                # 仅在当前规则仍是刚写入的 STPRO Provider 时恢复
                logger.error(f"[stpro] 绑定记录保存失败，尝试回滚: {exc}")
                await self.config_bridge.unbind_group(
                    group_umo,
                    profile.astrbot_config_id,
                    previous_exact_route_existed=route.exact_config_id is not None,
                    previous_exact_config_id=route.exact_config_id,
                )
                await self.store.add_pending_reconciliation(
                    {
                        "type": "binding_rollback",
                        "group_umo": group_umo,
                        "profile_id": profile.profile_id,
                        "reason": str(exc),
                        "created_at": utc_now_iso(),
                    },
                )
                raise

            return BindOutcome(True, f"已将本群绑定到档案「{profile.name}」。", record)

    # ---------- 解绑 ----------

    async def unbind(self, operator_key: str, group_umo: str) -> BindOutcome:
        """解绑。验证通过后直接执行，不二次确认。

        与 `bind` 一样只取群锁（Profile 锁由 Profile 侧获取），用户流程锁由调用方持有。
        """
        async with self.locks.ordered(group_umo=group_umo):
            binding = await self.store.get_binding(group_umo)
            if binding is None:
                return BindOutcome(False, "该群目前没有绑定档案。")
            if binding.manager_key != operator_key:
                raise PermissionDenied(
                    "只有绑定管理员可以解绑该群。",
                    f"umo={group_umo}",
                )

            if not binding.written_config_id:
                # 尚未迁移的旧绑定仍沿用旧版安全解绑。
                result = await self.bridge.compare_and_unbind(
                    binding.written_provider_id,
                    group_umo,
                    restore_provider_id=binding.previous_provider_id,
                    restore_rule_existed=binding.previous_rule_existed,
                    expected_config_id=binding.base_config_id,
                    expected_default_provider=binding.base_chat_provider_id,
                    expected_route_source=binding.route_source,
                    expected_route_pattern=binding.matched_route_pattern,
                    expected_route_config_id=binding.matched_route_config_id,
                )
            else:
                result = await self.config_bridge.unbind_group(
                    group_umo,
                    binding.written_config_id,
                    previous_exact_route_existed=binding.previous_exact_route_existed,
                    previous_exact_config_id=binding.previous_exact_config_id,
                )

            if result.reason in ("rule_changed", "route_changed"):
                # 明确 unbind：不碰管理员规则，但必须释放插件绑定记录。
                await self._remove_binding(group_umo)
                return BindOutcome(
                    True,
                    "已解除本群绑定，并保留管理员当前设置。",
                    binding,
                )

            await self._remove_binding(group_umo)
            return BindOutcome(True, "已解除本群绑定。", binding)

    # ---------- 状态迁移 ----------

    async def pause_bindings(self, profile_id: str) -> list[str]:
        """Provider 不可用时，相关绑定进入 paused。"""
        paused: list[str] = []
        for binding in await self.store.list_bindings(profile_id):
            if binding.state == STATE_ADMIN_OVERRIDDEN:
                continue
            await self._set_state(binding.group_umo, STATE_PAUSED)
            paused.append(binding.group_umo)
        return paused

    async def resume_paused(self, profile_id: str) -> list[str]:
        """Provider 恢复可用后，恢复安全的暂停绑定。

        只恢复 `paused`；`admin_overridden` 必须由绑定管理员重新 `bind`。
        """
        resumed: list[str] = []
        for binding in await self.store.list_bindings(profile_id):
            if binding.state != STATE_PAUSED:
                continue
            if binding.written_config_id:
                route = self.config_bridge.inspect_route(binding.group_umo)
                if route.exact_config_id != binding.written_config_id:
                    continue
            else:
                route = await self.bridge.inspect_group_route(binding.group_umo)
                if route.rule_provider_id != binding.written_provider_id:
                    continue
            await self._set_state(binding.group_umo, STATE_ACTIVE)
            resumed.append(binding.group_umo)
        return resumed

    async def mark_admin_overridden(self, group_umo: str) -> None:
        await self._set_state(group_umo, STATE_ADMIN_OVERRIDDEN)

    # ---------- 内部 ----------

    async def _save_binding(self, record: BindingRecord) -> None:
        payload = record.to_dict()

        def _mutate(data: dict[str, Any]) -> None:
            data["bindings"][record.group_umo] = payload

        await self.store.transaction(_mutate)

    async def _set_state(self, group_umo: str, state: str) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            raw = data["bindings"].get(group_umo)
            if raw is not None:
                raw["state"] = state
                raw["last_reconciled_at"] = utc_now_iso()

        await self.store.transaction(_mutate)

    async def _remove_binding(self, group_umo: str) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            data["bindings"].pop(group_umo, None)

        await self.store.transaction(_mutate)
