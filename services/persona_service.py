"""STPRO 人格的归属、原生保存与配置切换事务。"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..storage.ownership_store import (
    OwnershipStore,
    PersonaRecord,
    ProfileRecord,
    utc_now_iso,
)
from ..utils.errors import ConflictError, PermissionDenied, StproError
from ..utils.locks import LockManager
from .astrbot_profile_bridge import AstrBotProfileBridge
from .profile_service import ProfileService

_NAME_RE = re.compile(r"^[\w\-]{1,32}$", re.UNICODE)
_MAX_NAME_LEN = 32


@dataclass(frozen=True)
class PersonaListItem:
    name: str
    persona_id: str
    source: str
    selected: bool


class PersonaService:
    """只暴露当前 STPRO Profile 自己的人格和 default 当前人格。"""

    def __init__(
        self,
        context: Any,
        store: OwnershipStore,
        profile_service: ProfileService,
        config_bridge: AstrBotProfileBridge,
        locks: LockManager,
    ) -> None:
        self.persona_manager = context.persona_manager
        self.store = store
        self.profile_service = profile_service
        self.config_bridge = config_bridge
        self.locks = locks
        if self.persona_manager is None:
            raise RuntimeError("AstrBot PersonaManager 不可用")

    @staticmethod
    def normalize_name(name: str) -> str:
        return (name or "").strip().casefold()

    @classmethod
    def validate_name(cls, name: str) -> str:
        value = (name or "").strip()
        if not value:
            raise StproError("local", "人格名不能为空。")
        if len(value) > _MAX_NAME_LEN:
            raise StproError("local", f"人格名最长 {_MAX_NAME_LEN} 个字符。")
        if not _NAME_RE.match(value):
            raise StproError(
                "local",
                "人格名只能包含中文、字母、数字、`-` 和 `_`，不能包含空格。",
            )
        return value

    async def _owned_by_name(
        self,
        profile_id: str,
        name: str,
    ) -> PersonaRecord | None:
        key = self.normalize_name(name)
        for record in await self.store.list_personas(profile_id):
            if record.name_key == key:
                return record
        return None

    async def find_owned(
        self,
        profile_id: str,
        name: str,
    ) -> PersonaRecord | None:
        return await self._owned_by_name(profile_id, name)

    async def assert_name_available(self, profile: ProfileRecord, name: str) -> str:
        value = self.validate_name(name)
        if await self._owned_by_name(profile.profile_id, value):
            raise ConflictError(
                f"档案「{profile.name}」中已经有名为「{value}」的人格。"
            )
        if self.normalize_name(value) == self.normalize_name(
            self._alignment_persona_id(profile)
        ):
            raise ConflictError("该名称与档案的对齐配置人格重名，请换一个名称。")
        return value

    def _alignment_persona_id(self, profile: ProfileRecord) -> str:
        if profile.alignment_persona_id:
            return profile.alignment_persona_id
        return self.config_bridge.default_persona_id(
            profile.alignment_config_id or "default"
        )

    async def _mark_admin_managed(self, profile_id: str) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            raw = data["profiles"].get(profile_id)
            if raw is not None:
                raw["admin_managed"] = True

        await self.store.transaction(_mutate)

    async def _assert_persona_writable(
        self,
        profile_id: str,
    ) -> tuple[ProfileRecord, str]:
        profile, _ = await self.profile_service.assert_writable(profile_id)
        native = self.config_bridge.inspect(profile.astrbot_config_id)
        if not native.exists:
            raise StproError("local", "该档案对应的 AstrBot 配置文件已不存在。")

        current = native.persona_id or "default"
        expected = profile.selected_persona_id
        if not expected:
            # v0.5.0 以前的档案没有 Persona 基线。首次使用时接受原生配置当前事实，
            # 随后写入插件记录；从下一次开始即可识别 WebUI 外部改写。
            await self._save_selected(profile_id, current)
            profile.selected_persona_id = current
            expected = current
        if current != expected:
            await self._mark_admin_managed(profile_id)
            raise PermissionDenied(
                "该档案的人格已由 AstrBot 管理员修改，插件不会覆盖管理员配置。",
                f"profile={profile_id}",
            )
        return profile, current

    async def _save_selected(self, profile_id: str, persona_id: str) -> None:
        def _mutate(data: dict[str, Any]) -> None:
            raw = data["profiles"].get(profile_id)
            if raw is None:
                raise StproError("local", "该档案已不存在。")
            raw["selected_persona_id"] = persona_id

        await self.store.transaction(_mutate)

    async def list_personas(self, profile: ProfileRecord) -> list[PersonaListItem]:
        refreshed = await self.store.get_profile(profile.profile_id) or profile
        native = self.config_bridge.inspect(refreshed.astrbot_config_id)
        selected = native.persona_id or refreshed.selected_persona_id or "default"
        default_id = self._alignment_persona_id(refreshed)
        result = [
            PersonaListItem(
                name=default_id,
                persona_id=default_id,
                source="对齐配置",
                selected=selected == default_id,
            )
        ]
        for item in sorted(
            await self.store.list_personas(profile.profile_id),
            key=lambda value: value.name_key,
        ):
            result.append(
                PersonaListItem(
                    name=item.name,
                    persona_id=item.astrbot_persona_id,
                    source="当前档案",
                    selected=selected == item.astrbot_persona_id,
                )
            )
        return result

    async def create_persona(
        self,
        profile: ProfileRecord,
        owner_key: str,
        name: str,
        system_prompt: str,
    ) -> PersonaRecord:
        ProfileService.assert_owner(profile, owner_key)
        prompt = (system_prompt or "").strip()
        if not prompt:
            raise StproError("local", "人格提示词不能为空。")
        async with self.locks.profile_scope(profile.profile_id):
            current, _ = await self._assert_persona_writable(profile.profile_id)
            value = await self.assert_name_available(current, name)
            persona_id = f"stpro_persona_{uuid.uuid4().hex}"
            await self.persona_manager.create_persona(persona_id, prompt)
            record = PersonaRecord(
                astrbot_persona_id=persona_id,
                profile_id=current.profile_id,
                owner_key=owner_key,
                name=value,
                name_key=self.normalize_name(value),
                created_at=utc_now_iso(),
            )
            try:
                await self.store.save_persona(record)
            except Exception:
                try:
                    await self.persona_manager.delete_persona(persona_id)
                except Exception as rollback_exc:
                    await self.store.add_pending_reconciliation(
                        {
                            "type": "orphan_persona",
                            "persona_id": persona_id,
                            "profile_id": current.profile_id,
                            "reason": str(rollback_exc),
                            "created_at": utc_now_iso(),
                        }
                    )
                raise
            return record

    async def update_persona(
        self,
        profile: ProfileRecord,
        owner_key: str,
        name: str,
        system_prompt: str,
    ) -> PersonaRecord:
        ProfileService.assert_owner(profile, owner_key)
        prompt = (system_prompt or "").strip()
        if not prompt:
            raise StproError("local", "人格提示词不能为空。")
        async with self.locks.profile_scope(profile.profile_id):
            current, _ = await self._assert_persona_writable(profile.profile_id)
            record = await self._owned_by_name(current.profile_id, name)
            if record is None:
                raise StproError("local", f"当前档案没有名为「{name}」的自建人格。")
            await self.persona_manager.update_persona(
                record.astrbot_persona_id,
                system_prompt=prompt,
            )
            return record

    async def select_persona(
        self,
        profile: ProfileRecord,
        owner_key: str,
        name: str,
    ) -> str:
        ProfileService.assert_owner(profile, owner_key)
        value = self.validate_name(name)
        async with self.locks.profile_scope(profile.profile_id):
            current, previous_persona_id = await self._assert_persona_writable(
                profile.profile_id
            )
            owned = await self._owned_by_name(current.profile_id, value)
            default_id = self._alignment_persona_id(current)
            if owned is not None:
                persona_id = owned.astrbot_persona_id
                visible_name = owned.name
            elif self.normalize_name(value) == self.normalize_name(default_id):
                persona_id = default_id
                visible_name = default_id
            else:
                raise StproError(
                    "local",
                    "只能选择当前档案保存的人格，或 default 配置当前使用的人格。",
                )
            await self.config_bridge.set_persona(current.astrbot_config_id, persona_id)
            try:
                await self._save_selected(current.profile_id, persona_id)
            except Exception as exc:
                try:
                    await self.config_bridge.set_persona(
                        current.astrbot_config_id, previous_persona_id
                    )
                except Exception as rollback_exc:
                    await self.store.add_pending_reconciliation(
                        {
                            "type": "persona_selection_record_failed",
                            "profile_id": current.profile_id,
                            "persona_id": persona_id,
                            "reason": f"{exc}; rollback_failed: {rollback_exc}",
                            "created_at": utc_now_iso(),
                        }
                    )
                raise
            return visible_name

    async def delete_persona(
        self,
        profile: ProfileRecord,
        owner_key: str,
        name: str,
    ) -> PersonaRecord:
        ProfileService.assert_owner(profile, owner_key)
        async with self.locks.profile_scope(profile.profile_id):
            current, selected = await self._assert_persona_writable(profile.profile_id)
            record = await self._owned_by_name(current.profile_id, name)
            if record is None:
                raise StproError("local", f"当前档案没有名为「{name}」的自建人格。")
            references = [
                config_id
                for config_id in self.config_bridge.persona_references(
                    record.astrbot_persona_id
                )
                if config_id != current.astrbot_config_id
            ]
            if references:
                raise ConflictError(
                    "其他 AstrBot 配置当前正在使用这个人格。请先让管理员切换这些"
                    "配置的人格，再删除。"
                )
            default_id = self._alignment_persona_id(current)
            if selected == record.astrbot_persona_id:
                fallback = default_id
                await self.config_bridge.set_persona(
                    current.astrbot_config_id, fallback
                )
                try:
                    await self._save_selected(current.profile_id, fallback)
                except Exception:
                    await self.config_bridge.set_persona(
                        current.astrbot_config_id, selected
                    )
                    raise
            await self.persona_manager.delete_persona(record.astrbot_persona_id)
            try:
                await self.store.delete_persona(record.astrbot_persona_id)
            except Exception as exc:
                await self.store.add_pending_reconciliation(
                    {
                        "type": "deleted_persona_mapping",
                        "persona_id": record.astrbot_persona_id,
                        "profile_id": current.profile_id,
                        "reason": str(exc),
                        "created_at": utc_now_iso(),
                    }
                )
                raise
            return record

    async def delete_all_for_profile(self, profile_id: str) -> list[str]:
        """Profile 已删除后的清理；失败项保留映射供后续对账。"""
        failed: list[str] = []
        for record in await self.store.list_personas(profile_id):
            if self.config_bridge.persona_references(record.astrbot_persona_id):
                failed.append(record.name)
                continue
            try:
                await self.persona_manager.delete_persona(record.astrbot_persona_id)
            except Exception as exc:
                logger.error(
                    f"[stpro] 清理人格失败 persona={record.astrbot_persona_id}: {exc}"
                )
                await self.store.add_pending_reconciliation(
                    {
                        "type": "persona_cleanup_failed",
                        "persona_id": record.astrbot_persona_id,
                        "profile_id": profile_id,
                        "reason": str(exc),
                        "created_at": utc_now_iso(),
                    }
                )
                failed.append(record.name)
                continue
            await self.store.delete_persona(record.astrbot_persona_id)
        return failed

    async def assert_profile_personas_deletable(
        self,
        profile: ProfileRecord,
    ) -> None:
        """删除 Profile 前确认没有其他原生配置引用其人格。"""
        blocked: list[str] = []
        for record in await self.store.list_personas(profile.profile_id):
            references = [
                config_id
                for config_id in self.config_bridge.persona_references(
                    record.astrbot_persona_id
                )
                if config_id != profile.astrbot_config_id
            ]
            if references:
                blocked.append(record.name)
        if blocked:
            raise ConflictError(
                "以下人格仍被其他 AstrBot 配置引用："
                + "、".join(blocked)
                + "。请先让管理员切换相关配置的人格。"
            )
