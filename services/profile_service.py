"""Profile 业务模块：名称解析、所有权、new/set/model/list/remove。

设计文档第 3、4、6 节。所有命令只能作用于调用者自己的 Profile；查找名称时使用
规范化后的 `name_key`，回复时使用用户创建时的原始名称。
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..storage.ownership_store import OwnershipStore, ProfileRecord, utc_now_iso
from ..utils.errors import ConflictError, PermissionDenied, StproError
from ..utils.locks import LockManager
from ..utils.masking import mask_endpoint, mask_secret, scrub_text
from .astrbot_profile_bridge import AstrBotProfileBridge
from .model_client import ModelClient, validate_endpoint
from .provider_bridge import ProviderBridge

_NAME_RE = re.compile(r"^[\w\-]{1,32}$", re.UNICODE)
_MAX_NAME_LEN = 32


@dataclass
class CredentialResult:
    """`set` 的执行结果。"""

    models: list[str]
    model_kept: bool
    paused: bool  # 是否因原模型不在新列表中而禁用 Provider


class ProfileService:
    """Profile 相关的全部业务事务。"""

    def __init__(
        self,
        store: OwnershipStore,
        bridge: ProviderBridge,
        config_bridge: AstrBotProfileBridge,
        client: ModelClient,
        locks: LockManager,
        *,
        on_profile_usable: Callable[[str], Any] | None = None,
        on_profile_unusable: Callable[[str], Any] | None = None,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.config_bridge = config_bridge
        self.client = client
        self.locks = locks
        self.on_profile_usable = on_profile_usable
        self.on_profile_unusable = on_profile_unusable

    # ---------- 名称 ----------

    @staticmethod
    def normalize_name(name: str) -> str:
        """规范化名称用于查找：不区分英文大小写。"""
        return (name or "").strip().casefold()

    @classmethod
    def validate_name(cls, name: str) -> str:
        name = (name or "").strip()
        if not name:
            raise StproError("local", "配置名不能为空。")
        if len(name) > _MAX_NAME_LEN:
            raise StproError("local", f"配置名最长 {_MAX_NAME_LEN} 个字符。")
        if not _NAME_RE.match(name):
            raise StproError(
                "local",
                "配置名只能包含中文、字母、数字、`-` 和 `_`，不能包含空格。",
            )
        return name

    async def find_by_name(self, owner_key: str, name: str) -> ProfileRecord | None:
        """按名称查找调用者自己的 Profile。"""
        name_key = self.normalize_name(name)
        for record in await self.store.list_profiles(owner_key):
            if record.name_key == name_key:
                return record
        return None

    async def assert_not_duplicated(self, owner_key: str, name: str) -> None:
        if await self.find_by_name(owner_key, name) is not None:
            raise StproError("local", f"你已经有一个名为「{name}」的配置了。")

    @staticmethod
    def owner_display_id(owner_key: str) -> str:
        """从平台用户主键提取 WebUI 展示用账号；QQ 场景即 QQ 号。"""
        value = str(owner_key or "").rsplit(":", 1)[-1].strip()
        if not value:
            raise StproError("local", "无法识别当前用户账号，不能创建配置。")
        return value

    # ---------- 创建 ----------

    async def create_profile(
        self,
        owner_key: str,
        owner_private_umo: str,
        name: str,
        endpoint: str,
        api_key: str,
    ) -> tuple[ProfileRecord, list[str]]:
        """远程验证通过后才创建 Provider；创建结果必须是 `model=""`、`enable=false`。

        若原生 Provider 创建成功但所有权记录写入失败，必须尝试删除刚创建的
        Provider；补偿也失败时写入待对账记录并报告错误，不能谎报成功。
        """
        name = self.validate_name(name)
        endpoint = validate_endpoint(endpoint)
        await self.assert_not_duplicated(owner_key, name)
        try:
            alignment_config_id, _, alignment_persona_id = (
                self.config_bridge.alignment_baseline()
            )
        except ValueError as exc:
            raise StproError(
                "local",
                f"插件设置中的对齐默认配置不可用：{exc}",
            ) from exc

        models = await self.client.fetch_models(endpoint, api_key)

        profile_id = uuid.uuid4().hex
        owner_id = self.owner_display_id(owner_key)
        display_name = self.config_bridge.display_name(owner_id, name)
        provider_id = await self.bridge.create_unconfigured(
            profile_id,
            display_name,
            endpoint,
            api_key,
        )
        native_config = None
        try:
            native_config = await self.config_bridge.create_for_profile(
                owner_id,
                name,
                provider_id,
                alignment_config_id,
            )
        except Exception as exc:
            logger.error(f"[stpro] 创建 AstrBot 配置文件失败，回滚 Provider: {exc}")
            await self.bridge.delete_owned(provider_id, skip_ownership_check=True)
            raise StproError(
                "local",
                "创建失败，AstrBot 配置文件未能建立，已回滚。",
                f"profile={profile_id}",
            ) from exc

        record = ProfileRecord(
            profile_id=profile_id,
            owner_key=owner_key,
            name=name,
            name_key=self.normalize_name(name),
            provider_id=provider_id,
            astrbot_config_id=native_config.config_id,
            astrbot_config_name=native_config.name,
            astrbot_config_fingerprint=native_config.fingerprint,
            owner_private_umo=owner_private_umo,
            models=models,
            models_fetched_at=utc_now_iso(),
            config_status="unconfigured",
            created_at=utc_now_iso(),
            selected_persona_id=native_config.persona_id or "default",
            alignment_config_id=alignment_config_id,
            alignment_persona_id=alignment_persona_id,
        )

        try:
            await self._save_profile(record)
        except Exception as exc:
            # 补偿：删除刚创建的 Provider
            logger.error(
                scrub_text(
                    f"[stpro] 所有权记录写入失败，尝试回滚 Provider "
                    f"{provider_id}: {exc}",
                    [api_key],
                ),
            )
            try:
                await self.config_bridge.delete_owned(
                    native_config.config_id,
                    provider_id,
                )
                # 补偿删除：此时所有权记录还没写进去，跳过所有权校验
                await self.bridge.delete_owned(provider_id, skip_ownership_check=True)
            except Exception as rollback_exc:
                await self.store.add_pending_reconciliation(
                    {
                        "type": "orphan_provider",
                        "provider_id": provider_id,
                        "profile_id": profile_id,
                        "reason": f"rollback_failed: {rollback_exc}",
                        "created_at": utc_now_iso(),
                    },
                )
            raise StproError(
                "local",
                "创建失败，已回滚，请稍后重试。",
                f"profile={profile_id}",
            ) from exc

        # 创建后立即用原生事实刷新指纹，供后续对账识别外部修改
        await self.refresh_fingerprint(record)
        return record, models

    # ---------- 修改 Endpoint / Key ----------

    async def replace_credentials(
        self,
        record: ProfileRecord,
        endpoint: str | None,
        api_key: str | None,
    ) -> CredentialResult:
        """先验证候选值，再替换正式值。任一次失败都不能污染旧配置。

        第 3 节：任一修改都应先读取当前事实，不能只相信传入的内存对象。
        """
        async with self.locks.profile_scope(record.profile_id):
            current_record, current_snapshot = await self._assert_native_writable(
                record.profile_id
            )
            current_endpoint, current_api_key = await self._credentials_for(
                current_record, current_snapshot
            )
        endpoint = validate_endpoint(endpoint or current_endpoint)
        api_key = api_key or current_api_key
        models = await self.client.fetch_models(endpoint, api_key)

        async with self.locks.profile_scope(record.profile_id):
            current_record, snapshot = await self._assert_native_writable(
                record.profile_id,
            )
            if not snapshot.exists:
                raise StproError(
                    "local",
                    "该档案对应的配置已不存在，无法更新。",
                    f"profile={record.profile_id}",
                )

            model_kept = bool(snapshot.model) and snapshot.model in models
            new_model: str | None = snapshot.model if model_kept else None

            # 候选值验证完成，才写入正式 Provider
            await self.bridge.replace_credentials(
                current_record.provider_id,
                endpoint,
                api_key,
                new_model,
            )

            updated = await self.store.get_profile(record.profile_id) or current_record
            updated.models = models
            updated.models_fetched_at = utc_now_iso()
            updated.config_status = "configured" if model_kept else "unconfigured"

            if model_kept:
                updated.consecutive_failures = 0
                updated.health_status = "healthy"
            else:
                updated.health_status = "unknown"
                updated.anomaly_notified = False

            await self._save_profile(updated)
            await self.refresh_fingerprint(updated)

        # 第 4 节：原模型不在新列表中 → 已有绑定进入"暂停"。
        # 由服务层保证，而不是交给命令 Handler，避免其他调用路径漏掉。
        if not model_kept and self.on_profile_unusable is not None:
            result = self.on_profile_unusable(updated.profile_id)
            if hasattr(result, "__await__"):
                await result

        return CredentialResult(
            models=models, model_kept=model_kept, paused=not model_kept
        )

    # ---------- 模型 ----------

    async def refresh_models(self, record: ProfileRecord) -> list[str]:
        """重新获取模型列表（不修改任何配置）。"""
        async with self.locks.profile_scope(record.profile_id):
            current = await self.store.get_profile(record.profile_id) or record
            snapshot = self.bridge.inspect(current.provider_id)
            if not snapshot.exists:
                raise StproError(
                    "local",
                    "该档案对应的配置已不存在，无法获取模型列表。",
                    f"profile={record.profile_id}",
                )
            endpoint, api_key = await self._credentials_for(current, snapshot)
            models = await self.client.fetch_models(endpoint, api_key)

            updated = await self.store.get_profile(record.profile_id) or current
            updated.models = models
            updated.models_fetched_at = utc_now_iso()
            await self._save_profile(updated)
            return models

    async def select_model(self, record: ProfileRecord, model_id: str) -> None:
        """写入模型 ID 并启用 Provider；安全的暂停绑定随后恢复。

        第 3 节：先读取当前事实，不能用可能过期的内存对象判断 admin_managed。
        """
        async with self.locks.profile_scope(record.profile_id):
            current_record, _ = await self._assert_native_writable(record.profile_id)
            await self.bridge.select_model(current_record.provider_id, model_id)

            updated = await self.store.get_profile(record.profile_id) or current_record
            updated.config_status = "configured"
            updated.consecutive_failures = 0
            updated.health_status = "healthy"
            updated.anomaly_notified = False
            await self._save_profile(updated)
            await self.refresh_fingerprint(updated)

        if self.on_profile_usable is not None:
            result = self.on_profile_usable(updated.profile_id)
            if hasattr(result, "__await__"):
                await result

    async def disable_for_missing_model(self, profile_id: str) -> None:
        """新端点不含原模型时：清空模型、禁用 Provider。"""
        async with self.locks.profile_scope(profile_id):
            record, _ = await self._assert_native_writable(profile_id)
            await self.bridge.set_model_and_enable(record.provider_id, "", False)
            updated = await self.store.get_profile(profile_id) or record
            updated.config_status = "unconfigured"
            await self._save_profile(updated)
            await self.refresh_fingerprint(updated)

    # ---------- 删除 ----------

    async def remove_profile(self, record: ProfileRecord) -> None:
        """删除顺序：解除群路由 → Provider → AstrBot 配置 → 所有权记录。

        群的解绑由 BindingService 在调用前完成，这里只处理 Provider 与记录，
        并保证不出现"Provider 尚在但所有权已无"的静默孤儿。
        """
        async with self.locks.profile_scope(record.profile_id):
            current = await self.store.get_profile(record.profile_id) or record
            snapshot = self.bridge.inspect(current.provider_id)
            native = self.config_bridge.inspect(current.astrbot_config_id)
            if snapshot.exists:
                current, snapshot = await self._assert_native_writable(
                    record.profile_id
                )
            if native.exists and native.default_provider_id != current.provider_id:
                raise PermissionDenied(
                    "该档案的 AstrBot 配置文件已由管理员接管，当前不能删除。",
                    f"profile={record.profile_id}",
                )
            if snapshot.exists:
                await self.bridge.delete_owned(current.provider_id)
            if native.exists:
                try:
                    await self.config_bridge.delete_owned(
                        current.astrbot_config_id,
                        current.provider_id,
                    )
                except Exception as exc:
                    await self.store.add_pending_reconciliation(
                        {
                            "type": "partial_profile_delete",
                            "profile_id": current.profile_id,
                            "provider_id": current.provider_id,
                            "config_id": current.astrbot_config_id,
                            "reason": str(exc),
                            "created_at": utc_now_iso(),
                        }
                    )
                    raise

            # 最后删除所有权记录。调用方必须已经成功清空绑定。
            def _mutate(data: dict[str, Any]) -> None:
                remaining = [
                    raw
                    for raw in data["bindings"].values()
                    if raw.get("profile_id") == record.profile_id
                ]
                if remaining:
                    raise ConflictError(
                        "仍有群绑定未解除，删除已停止。",
                        f"profile={record.profile_id}",
                    )
                data["profiles"].pop(record.profile_id, None)

            await self.store.transaction(_mutate)

    # ---------- 查看 ----------

    async def summarize(self, record: ProfileRecord) -> dict[str, Any]:
        """返回脱敏摘要；Key 掩码保留可识别的少量首尾字符。

        第 6 节要求 `list <配置名>` 至少显示：原始配置名、Endpoint、脱敏 Key、
        当前模型、Provider 启用/健康/管理状态、**绑定群及各绑定状态**。
        """
        snapshot = self.bridge.inspect(record.provider_id)
        api_key = ""
        if snapshot.exists:
            config = self.bridge.get_provider_config(record.provider_id) or {}
            keys = config.get("key") or []
            api_key = str(keys[0]) if keys else ""

        bindings = await self.store.list_bindings(record.profile_id)
        native = self.config_bridge.inspect(record.astrbot_config_id)

        return {
            "name": record.name,
            "endpoint": mask_endpoint(snapshot.endpoint),
            "api_key": mask_secret(api_key),
            "model": snapshot.model or "（未选择）",
            "enable": snapshot.enable,
            "config_status": record.config_status,
            "health_status": record.health_status,
            "admin_managed": record.admin_managed,
            "exists": snapshot.exists,
            "astrbot_config_id": record.astrbot_config_id,
            "astrbot_config_name": native.name or record.astrbot_config_name,
            "astrbot_config_exists": native.exists,
            "bindings": [
                {"group_umo": b.group_umo, "state": b.state} for b in bindings
            ],
        }

    # ---------- 内部 ----------

    async def _credentials_for(
        self,
        record: ProfileRecord,
        snapshot: Any = None,
    ) -> tuple[str, str]:
        """从原生 Provider 配置取 Endpoint 与 Key（插件 JSON 不复制这些值）。"""
        snapshot = snapshot or self.bridge.inspect(record.provider_id)
        config = self.bridge.get_provider_config(record.provider_id) or {}
        keys = config.get("key") or []
        if not snapshot.endpoint or not keys:
            raise StproError(
                "local",
                "该档案缺少 Endpoint 或 API Key，请使用 "
                "`/stpro set <配置名> all` 重新设置。",
                f"profile={record.profile_id}",
            )
        return snapshot.endpoint, str(keys[0])

    async def refresh_fingerprint(self, record: ProfileRecord) -> None:
        """用原生当前事实刷新插件记录中的指纹与状态。"""
        snapshot = self.bridge.inspect(record.provider_id)
        native = self.config_bridge.inspect(record.astrbot_config_id)

        def _mutate(data: dict[str, Any]) -> None:
            raw = data["profiles"].get(record.profile_id)
            if raw is None:
                return
            raw["provider_fingerprint"] = snapshot.fingerprint
            cfg = raw.setdefault("astrbot_config", {})
            cfg["name"] = native.name or cfg.get("name")
            cfg["fingerprint"] = native.fingerprint
            if not snapshot.exists:
                raw.setdefault("status", {})["config"] = "missing"
            elif snapshot.model and snapshot.enable:
                raw.setdefault("status", {})["config"] = "configured"

        await self.store.transaction(_mutate)

    async def _save_profile(self, record: ProfileRecord) -> None:
        payload = record.to_dict()

        def _mutate(data: dict[str, Any]) -> None:
            data["profiles"][record.profile_id] = payload

        await self.store.transaction(_mutate)

    async def _assert_native_writable(
        self,
        profile_id: str,
    ) -> tuple[ProfileRecord, Any]:
        """锁内确认档案仍由插件管理，并返回最新记录与原生快照。"""
        record = await self.store.get_profile(profile_id)
        if record is None:
            raise StproError("local", "该档案已不存在。", f"profile={profile_id}")
        snapshot = self.bridge.inspect(record.provider_id)
        native = self.config_bridge.inspect(record.astrbot_config_id)
        changed = bool(
            snapshot.exists
            and record.provider_fingerprint
            and snapshot.fingerprint
            and record.provider_fingerprint != snapshot.fingerprint
        )
        if changed:

            def _mark_admin(data: dict[str, Any]) -> None:
                raw = data["profiles"].get(profile_id)
                if raw is not None:
                    raw["admin_managed"] = True
                    raw["provider_fingerprint"] = snapshot.fingerprint

            await self.store.transaction(_mark_admin)
            record.admin_managed = True
        native_changed = bool(
            native.exists and native.default_provider_id != record.provider_id
        )
        if native_changed:

            def _mark_native_admin(data: dict[str, Any]) -> None:
                raw = data["profiles"].get(profile_id)
                if raw is not None:
                    raw["admin_managed"] = True
                    cfg = raw.setdefault("astrbot_config", {})
                    cfg["fingerprint"] = native.fingerprint

            await self.store.transaction(_mark_native_admin)
            record.admin_managed = True
        if record.admin_managed:
            raise PermissionDenied(
                "该档案已由 AstrBot 管理员接管，当前不能修改。",
                f"profile={profile_id}",
            )
        return record, snapshot

    async def assert_writable(
        self,
        profile_id: str,
    ) -> tuple[ProfileRecord, Any]:
        """公开给同插件服务使用的管理员接管检查。"""
        return await self._assert_native_writable(profile_id)

    @staticmethod
    def assert_owner(record: ProfileRecord, owner_key: str) -> None:
        if record.owner_key != owner_key:
            raise PermissionDenied(
                "这不是你的配置。",
                f"profile={record.profile_id}",
            )

    @staticmethod
    def assert_not_admin_managed(record: ProfileRecord) -> None:
        if record.admin_managed:
            raise ConflictError(
                "该档案已由 AstrBot 管理员接管，当前不能修改。",
                f"profile={record.profile_id}",
            )
