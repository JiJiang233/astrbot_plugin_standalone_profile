"""启动与周期对账、管理员接管判定。

设计文档第 14.1 节：

对每个 Profile：
1. 所有权记录存在、Provider 存在且指纹符合插件最后一次内部写入：保持状态；
2. Provider 存在但有效配置指纹不同：标记 `admin_managed=true`，以原生值为事实，
   不写回；
3. Provider 不存在：标记 `missing`，相关绑定进入 `paused`，不得擅自重建；
4. 存在孤立的 `stpro_` Provider 但无所有权记录：只告警，不认领、不修改、不删除。

对每个绑定：
1. Profile 记录不存在：列入待人工/安全清理，但不得贸然删除群规则；
2. 群当前规则等于 `written_provider_id` 且路由/默认 Provider 基线无外部变化：
   按 Provider 可用性设为 `active` 或 `paused`；
3. 群当前规则不等于该 STPRO Provider：设为 `admin_overridden`，不写回；
4. 路由或相关默认 Provider 相对基线变化：设为 `admin_overridden`；若群规则仍是
   本绑定写入的 STPRO 值，则比较后只删除该覆盖，绝不恢复历史值；
5. 重复或无效绑定记录：保留原文件备份并按确定性规则选一条进入只读告警。

只有完整对账结束后才启动定时监控。任何不确定对象都应禁止写操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ..storage.ownership_store import OwnershipStore, utc_now_iso
from .astrbot_profile_bridge import AstrBotProfileBridge
from .binding_service import (
    STATE_ACTIVE,
    STATE_ADMIN_OVERRIDDEN,
    STATE_PAUSED,
    BindingService,
)
from .provider_bridge import ProviderBridge


@dataclass
class ReconcileReport:
    admin_managed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    orphan_providers: list[str] = field(default_factory=list)
    admin_overridden: list[str] = field(default_factory=list)
    yielded: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    reactivated: list[str] = field(default_factory=list)
    native_profiles_created: list[str] = field(default_factory=list)
    bindings_migrated: list[str] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)

    def as_text(self) -> str:
        parts = [
            f"管理员接管 {len(self.admin_managed)} 个",
            f"缺失 {len(self.missing)} 个",
            f"孤立 Provider {len(self.orphan_providers)} 个",
            f"绑定让位 {len(self.admin_overridden)} 个",
            f"暂停 {len(self.paused)} 个",
            f"恢复 {len(self.reactivated)} 个",
            f"创建 WebUI 配置 {len(self.native_profiles_created)} 个",
            f"迁移绑定 {len(self.bindings_migrated)} 个",
        ]
        return "；".join(parts)


class ReconciliationService:
    """对账：把插件记录与 AstrBot 原生事实对齐。"""

    def __init__(
        self,
        store: OwnershipStore,
        bridge: ProviderBridge,
        config_bridge: AstrBotProfileBridge,
        binding_service: BindingService,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.config_bridge = config_bridge
        self.binding_service = binding_service

    async def reconcile(self) -> ReconcileReport:
        report = ReconcileReport()
        await self._reconcile_profiles(report)
        await self._reconcile_bindings(report)
        await self._detect_orphan_providers(report)
        if report.admin_managed or report.missing or report.admin_overridden:
            logger.info(f"[stpro] 对账完成: {report.as_text()}")
        return report

    # ---------- Profile ----------

    async def _reconcile_profiles(self, report: ReconcileReport) -> None:
        for record in await self.store.list_profiles():
            snapshot = self.bridge.inspect(record.provider_id)
            if not snapshot.exists:
                report.missing.append(record.profile_id)
                await self._mark_profile(record.profile_id, config_status="missing")
                await self.binding_service.pause_bindings(record.profile_id)
                continue
            if record.admin_managed:
                report.admin_managed.append(record.profile_id)
                continue
            if (
                record.provider_fingerprint
                and snapshot.fingerprint
                and record.provider_fingerprint != snapshot.fingerprint
            ):
                report.admin_managed.append(record.profile_id)
                await self._mark_profile(
                    record.profile_id,
                    admin_managed=True,
                    fingerprint=snapshot.fingerprint,
                )
                continue

            if not record.astrbot_config_id:
                try:
                    native = await self.config_bridge.create_for_profile(
                        self._owner_display_id(record.owner_key),
                        record.name,
                        record.provider_id,
                        record.alignment_config_id or "default",
                    )
                except Exception as exc:
                    report.pending.append(record.profile_id)
                    logger.warning(
                        f"[stpro] 旧档案暂未迁移到 WebUI profile={record.profile_id}: {exc}"
                    )
                    continue
                await self._save_native_profile(
                    record.profile_id,
                    native,
                    record.alignment_config_id or "default",
                )
                report.native_profiles_created.append(record.profile_id)
                record = await self.store.get_profile(record.profile_id) or record

            native = self.config_bridge.inspect(record.astrbot_config_id)

            if not native.exists:
                # 有明确 ID 但被管理员删除：让位，不自动重建。
                report.missing.append(record.profile_id)
                await self._mark_profile(
                    record.profile_id,
                    config_status="missing",
                    admin_managed=True,
                )
                await self.binding_service.pause_bindings(record.profile_id)
                continue

            if native.default_provider_id != record.provider_id:
                report.admin_managed.append(record.profile_id)
                await self._mark_profile(
                    record.profile_id,
                    admin_managed=True,
                    config_fingerprint=native.fingerprint,
                    config_name=native.name,
                )
                continue

            config_status = (
                "configured" if snapshot.model and snapshot.enable else "unconfigured"
            )
            await self._mark_profile(
                record.profile_id,
                config_status=config_status,
                fingerprint=snapshot.fingerprint,
                config_fingerprint=native.fingerprint,
                config_name=native.name,
            )
            if not snapshot.usable:
                report.paused.extend(
                    await self.binding_service.pause_bindings(record.profile_id),
                )

    async def _mark_profile(
        self,
        profile_id: str,
        *,
        config_status: str | None = None,
        admin_managed: bool | None = None,
        fingerprint: str | None = None,
        config_fingerprint: str | None = None,
        config_name: str | None = None,
    ) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            raw = data["profiles"].get(profile_id)
            if raw is None:
                return
            if config_status is not None:
                raw.setdefault("status", {})["config"] = config_status
            if admin_managed is not None:
                raw["admin_managed"] = admin_managed
            if fingerprint is not None:
                raw["provider_fingerprint"] = fingerprint
            native = raw.setdefault("astrbot_config", {})
            if config_fingerprint is not None:
                native["fingerprint"] = config_fingerprint
            if config_name is not None:
                native["name"] = config_name
            raw["last_reconciled_at"] = utc_now_iso()

        await self.store.transaction(_mutate)

    async def _save_native_profile(
        self,
        profile_id: str,
        native: Any,
        alignment_config_id: str,
    ) -> None:
        alignment_persona_id = self.config_bridge.default_persona_id(
            alignment_config_id
        )

        def _mutate(data: dict[str, Any]) -> None:
            raw = data["profiles"].get(profile_id)
            if raw is None:
                return
            cfg = raw.setdefault("astrbot_config", {})
            cfg.update(
                {
                    "id": native.config_id,
                    "name": native.name,
                    "fingerprint": native.fingerprint,
                }
            )
            raw["alignment_config_id"] = alignment_config_id
            raw["alignment_persona_id"] = alignment_persona_id
            raw["selected_persona_id"] = native.persona_id or alignment_persona_id

        await self.store.transaction(_mutate)

    # ---------- 绑定 ----------

    async def _reconcile_bindings(self, report: ReconcileReport) -> None:
        for binding in await self.store.list_bindings():
            profile = await self.store.get_profile(binding.profile_id)
            if profile is None:
                # 不得贸然删除群规则，只登记待清理
                report.pending.append(binding.group_umo)
                await self.store.add_pending_reconciliation(
                    {
                        "type": "binding_without_profile",
                        "group_umo": binding.group_umo,
                        "profile_id": binding.profile_id,
                        "created_at": utc_now_iso(),
                    },
                )
                continue

            native = self.config_bridge.inspect(profile.astrbot_config_id)
            if not native.exists or native.default_provider_id != profile.provider_id:
                await self.binding_service.mark_admin_overridden(binding.group_umo)
                report.admin_overridden.append(binding.group_umo)
                continue

            if not binding.written_config_id:
                try:
                    migrated = await self.config_bridge.migrate_legacy_binding(
                        binding.group_umo,
                        profile.astrbot_config_id,
                        binding.written_provider_id,
                        self.bridge,
                        expected_exact_config_id=(
                            binding.matched_route_config_id
                            if binding.matched_route_pattern == binding.group_umo
                            else None
                        ),
                    )
                except Exception as exc:
                    report.pending.append(binding.group_umo)
                    logger.warning(
                        f"[stpro] 旧绑定迁移失败，保留原绑定 umo={binding.group_umo}: {exc}"
                    )
                    continue
                if migrated.applied:
                    await self._mark_binding_migrated(
                        binding.group_umo,
                        profile.astrbot_config_id,
                        previous_exact_config_id=(
                            binding.matched_route_config_id
                            if binding.matched_route_pattern == binding.group_umo
                            else None
                        ),
                    )
                    report.bindings_migrated.append(binding.group_umo)
                    binding = await self.store.get_binding(binding.group_umo) or binding
                else:
                    await self.binding_service.mark_admin_overridden(binding.group_umo)
                    report.admin_overridden.append(binding.group_umo)
                    continue

            config_route = self.config_bridge.inspect_route(binding.group_umo)
            if config_route.exact_config_id != binding.written_config_id:
                if binding.state != STATE_ADMIN_OVERRIDDEN:
                    await self.binding_service.mark_admin_overridden(binding.group_umo)
                    report.admin_overridden.append(binding.group_umo)
                continue

            session_rule = await self.bridge.read_group_rule(binding.group_umo)
            if session_rule is not None:
                # 会话 Provider 覆盖的解析优先于配置文件默认 Provider。管理员或
                # 其他插件写入后，STPRO 保留自身路由但立即让位且不自动恢复。
                if binding.state != STATE_ADMIN_OVERRIDDEN:
                    await self.binding_service.mark_admin_overridden(binding.group_umo)
                    report.admin_overridden.append(binding.group_umo)
                continue

            # 新版绑定不再依赖会话 Provider 覆盖；配置路由就是完整事实。
            snapshot = self.bridge.inspect(profile.provider_id)
            target = STATE_ACTIVE if snapshot.usable else STATE_PAUSED
            if binding.state != target and binding.state != STATE_ADMIN_OVERRIDDEN:
                await self._set_binding_state(binding.group_umo, target)
                (
                    report.reactivated if target == STATE_ACTIVE else report.paused
                ).append(
                    binding.group_umo,
                )

    async def _mark_binding_migrated(
        self,
        group_umo: str,
        config_id: str,
        previous_exact_config_id: str | None,
    ) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            raw = data["bindings"].get(group_umo)
            if raw is None:
                return
            route = raw.setdefault("config_route", {})
            route["written_config_id"] = config_id
            route["previous_exact_route_existed"] = previous_exact_config_id is not None
            route["previous_exact_config_id"] = previous_exact_config_id
            route.setdefault(
                "previous_matched_route_pattern",
                raw.get("matched_route_pattern"),
            )
            route.setdefault(
                "previous_matched_route_config_id",
                raw.get("matched_route_config_id"),
            )
            raw["last_reconciled_at"] = utc_now_iso()

        await self.store.transaction(_mutate)

    async def _set_binding_state(self, group_umo: str, state: str) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            raw = data["bindings"].get(group_umo)
            if raw is not None:
                raw["state"] = state
                raw["last_reconciled_at"] = utc_now_iso()

        await self.store.transaction(_mutate)

    # ---------- 孤立 Provider ----------

    async def _detect_orphan_providers(self, report: ReconcileReport) -> None:
        """存在 STPRO Provider 但无所有权记录：只告警，不认领、不修改、不删除。"""
        owned = {r.provider_id for r in await self.store.list_profiles()}
        try:
            configs = self.context_provider_configs()
        except Exception as exc:
            logger.debug(f"[stpro] 读取 Provider 配置列表失败: {exc}")
            return
        for provider_id in configs:
            if (
                self.bridge.is_owned_provider_id(provider_id)
                and provider_id not in owned
            ):
                report.orphan_providers.append(provider_id)
                logger.warning(
                    f"[stpro] 发现孤立 STPRO Provider {provider_id}："
                    "插件不会认领、修改或删除它。",
                )

    @staticmethod
    def _owner_display_id(owner_key: str) -> str:
        return str(owner_key or "").rsplit(":", 1)[-1].strip()

    def context_provider_configs(self) -> list[str]:
        return [
            cfg.get("id", "")
            for cfg in self.bridge.context.provider_manager.providers_config
            if isinstance(cfg, dict)
        ]
