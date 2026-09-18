"""所有权与绑定记录的持久化。

设计文档第 13 节：

- 只保存所有权、绑定管理员、映射、原规则和监控状态；Endpoint / API Key / 模型 /
  启用状态一律不复制，只存在 AstrBot Provider 配置里。
- 写入：序列化到同目录临时文件 → flush/fsync → 校验可重新读取 → 原子替换正式文件；
  替换前保留最近一次有效备份。
- JSON 损坏或版本高于当前实现时：不得用空数据覆盖、不得自动删除 Provider；
  停止有风险的写操作、输出明确错误并允许只读诊断。
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger

SCHEMA_VERSION = 3

BACKUP_SUFFIX = ".bak"
TMP_SUFFIX = ".tmp"


def utc_now_iso() -> str:
    """带时区的 ISO 8601 UTC 字符串。"""
    return datetime.now(UTC).isoformat()


class StoreCorrupted(Exception):
    """数据文件损坏或版本过高。此时只允许只读诊断，禁止一切写操作。"""


class StoreReadOnly(Exception):
    """存储处于只读保护状态，拒绝写入。"""


@dataclass
class ProfileRecord:
    """一个 STPRO Profile 的业务记录（对应一个 AstrBot Provider）。"""

    profile_id: str
    owner_key: str  # "<platform_id>:<user_id>"
    name: str  # 用户创建时的原始名称
    name_key: str  # 规范化后的名称，用于查找
    provider_id: str  # "stpro_<uuid>"
    owner_private_umo: str = ""
    models: list[str] = field(default_factory=list)
    models_fetched_at: str | None = None
    admin_managed: bool = False
    provider_fingerprint: str | None = None
    astrbot_config_id: str | None = None
    astrbot_config_name: str | None = None
    astrbot_config_fingerprint: str | None = None
    # 状态维度不得混为一个字符串（第 13.0 节）
    config_status: str = "unconfigured"  # unconfigured / configured / missing
    health_status: str = "unknown"  # unknown / healthy / degraded / unhealthy
    last_checked_at: str | None = None
    last_success_at: str | None = None
    consecutive_failures: int = 0
    last_error_category: str | None = None
    last_latency_ms: int | None = None
    anomaly_notified: bool = False
    last_notification_kind: str | None = None
    created_at: str = ""
    selected_persona_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "owner_key": self.owner_key,
            "name": self.name,
            "name_key": self.name_key,
            "provider_id": self.provider_id,
            "owner_private_umo": self.owner_private_umo,
            "models": list(self.models),
            "models_fetched_at": self.models_fetched_at,
            "admin_managed": self.admin_managed,
            "provider_fingerprint": self.provider_fingerprint,
            "astrbot_config": {
                "id": self.astrbot_config_id,
                "name": self.astrbot_config_name,
                "fingerprint": self.astrbot_config_fingerprint,
            },
            "status": {
                "config": self.config_status,
                "health": self.health_status,
            },
            "monitor": {
                "last_checked_at": self.last_checked_at,
                "last_success_at": self.last_success_at,
                "consecutive_failures": self.consecutive_failures,
                "last_error_category": self.last_error_category,
                "last_latency_ms": self.last_latency_ms,
                "anomaly_notified": self.anomaly_notified,
                "last_notification_kind": self.last_notification_kind,
            },
            "created_at": self.created_at,
            "selected_persona_id": self.selected_persona_id,
        }

    @classmethod
    def from_dict(cls, profile_id: str, raw: dict[str, Any]) -> ProfileRecord:
        status = raw.get("status") or {}
        monitor = raw.get("monitor") or {}
        astrbot_config = raw.get("astrbot_config") or {}
        return cls(
            profile_id=profile_id,
            owner_key=raw.get("owner_key", ""),
            name=raw.get("name", ""),
            name_key=raw.get("name_key", ""),
            provider_id=raw.get("provider_id", ""),
            owner_private_umo=raw.get("owner_private_umo", ""),
            models=list(raw.get("models") or []),
            models_fetched_at=raw.get("models_fetched_at"),
            admin_managed=bool(raw.get("admin_managed", False)),
            provider_fingerprint=raw.get("provider_fingerprint"),
            astrbot_config_id=astrbot_config.get("id"),
            astrbot_config_name=astrbot_config.get("name"),
            astrbot_config_fingerprint=astrbot_config.get("fingerprint"),
            config_status=status.get("config", "unconfigured"),
            health_status=status.get("health", "unknown"),
            last_checked_at=monitor.get("last_checked_at"),
            last_success_at=monitor.get("last_success_at"),
            consecutive_failures=int(monitor.get("consecutive_failures", 0) or 0),
            last_error_category=monitor.get("last_error_category"),
            last_latency_ms=monitor.get("last_latency_ms"),
            anomaly_notified=bool(monitor.get("anomaly_notified", False)),
            last_notification_kind=monitor.get("last_notification_kind"),
            created_at=raw.get("created_at", ""),
            selected_persona_id=raw.get("selected_persona_id"),
        )


@dataclass
class PersonaRecord:
    """STPRO 人格所有权；人格正文仍由 AstrBot PersonaManager 保存。"""

    astrbot_persona_id: str
    profile_id: str
    owner_key: str
    name: str
    name_key: str
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "astrbot_persona_id": self.astrbot_persona_id,
            "profile_id": self.profile_id,
            "owner_key": self.owner_key,
            "name": self.name,
            "name_key": self.name_key,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, persona_id: str, raw: dict[str, Any]) -> PersonaRecord:
        return cls(
            astrbot_persona_id=str(raw.get("astrbot_persona_id") or persona_id),
            profile_id=str(raw.get("profile_id") or ""),
            owner_key=str(raw.get("owner_key") or ""),
            name=str(raw.get("name") or ""),
            name_key=str(raw.get("name_key") or ""),
            created_at=str(raw.get("created_at") or ""),
        )


@dataclass
class BindingRecord:
    """一个群与一个 Profile 的绑定关系。"""

    group_umo: str
    profile_id: str
    manager_key: str  # 首次绑定者
    written_provider_id: str
    previous_provider_id: str | None = None
    previous_rule_existed: bool = False
    base_config_id: str = ""
    base_chat_provider_id: str | None = None
    base_runner_type: str | None = None
    route_source: str = "fallback_default"
    matched_route_pattern: str | None = None
    matched_route_config_id: str | None = None
    baseline_fingerprint: str | None = None
    last_reconciled_at: str | None = None
    state: str = "active"  # active / paused / admin_overridden
    created_at: str = ""
    written_config_id: str | None = None
    previous_exact_route_existed: bool = False
    previous_exact_config_id: str | None = None
    previous_matched_route_pattern: str | None = None
    previous_matched_route_config_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "group_umo": self.group_umo,
            "profile_id": self.profile_id,
            "manager_key": self.manager_key,
            "written_provider_id": self.written_provider_id,
            "previous_provider_id": self.previous_provider_id,
            "previous_rule_existed": self.previous_rule_existed,
            "base_config_id": self.base_config_id,
            "base_chat_provider_id": self.base_chat_provider_id,
            "base_runner_type": self.base_runner_type,
            "route_source": self.route_source,
            "matched_route_pattern": self.matched_route_pattern,
            "matched_route_config_id": self.matched_route_config_id,
            "baseline_fingerprint": self.baseline_fingerprint,
            "last_reconciled_at": self.last_reconciled_at,
            "state": self.state,
            "created_at": self.created_at,
            "config_route": {
                "written_config_id": self.written_config_id,
                "previous_exact_route_existed": self.previous_exact_route_existed,
                "previous_exact_config_id": self.previous_exact_config_id,
                "previous_matched_route_pattern": self.previous_matched_route_pattern,
                "previous_matched_route_config_id": self.previous_matched_route_config_id,
            },
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> BindingRecord:
        config_route = raw.get("config_route") or {}
        return cls(
            group_umo=raw.get("group_umo", ""),
            profile_id=raw.get("profile_id", ""),
            manager_key=raw.get("manager_key", ""),
            written_provider_id=raw.get("written_provider_id", ""),
            previous_provider_id=raw.get("previous_provider_id"),
            previous_rule_existed=bool(raw.get("previous_rule_existed", False)),
            base_config_id=raw.get("base_config_id", ""),
            base_chat_provider_id=raw.get("base_chat_provider_id"),
            base_runner_type=raw.get("base_runner_type"),
            route_source=raw.get("route_source")
            or (
                "fallback_default"
                if raw.get("base_config_id", "") == "default"
                else "explicit_route"
            ),
            matched_route_pattern=raw.get("matched_route_pattern"),
            matched_route_config_id=raw.get("matched_route_config_id"),
            baseline_fingerprint=raw.get("baseline_fingerprint"),
            last_reconciled_at=raw.get("last_reconciled_at"),
            state=raw.get("state", "active"),
            created_at=raw.get("created_at", ""),
            written_config_id=config_route.get("written_config_id"),
            previous_exact_route_existed=bool(
                config_route.get("previous_exact_route_existed", False)
            ),
            previous_exact_config_id=config_route.get("previous_exact_config_id"),
            previous_matched_route_pattern=config_route.get(
                "previous_matched_route_pattern"
            ),
            previous_matched_route_config_id=config_route.get(
                "previous_matched_route_config_id"
            ),
        )


class OwnershipStore:
    """插件数据的唯一读写入口。

    所有修改都通过 `transaction()` 完成：在锁内读快照 → 回调修改 → 原子落盘。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.backup_path = path.with_suffix(path.suffix + BACKUP_SUFFIX)
        self.tmp_path = path.with_suffix(path.suffix + TMP_SUFFIX)
        self._lock = asyncio.Lock()
        self._data: dict[str, Any] | None = None
        self._readonly_reason: str | None = None

    # ---------- 生命周期 ----------

    async def load(self) -> dict[str, Any]:
        """加载数据。文件不存在时返回空结构。"""
        async with self._lock:
            if self._data is None:
                self._data = self._read_from_disk()
            return self._data

    @property
    def readonly_reason(self) -> str | None:
        return self._readonly_reason

    def _read_from_disk(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_data()

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            self._readonly_reason = f"数据文件无法解析: {exc}"
            logger.error(
                f"[stpro] ownership 文件损坏，已切换到只读诊断模式，"
                f"不会覆盖也不会删除任何 Provider: {exc}",
            )
            raise StoreCorrupted(self._readonly_reason) from exc

        if not isinstance(raw, dict):
            self._readonly_reason = "数据文件顶层结构不是对象"
            raise StoreCorrupted(self._readonly_reason)

        version = raw.get("version", 0)
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            self._readonly_reason = (
                f"数据文件版本 {version} 高于当前实现支持的 {SCHEMA_VERSION}"
            )
            logger.error(f"[stpro] {self._readonly_reason}，已切换到只读诊断模式")
            raise StoreCorrupted(self._readonly_reason)

        if version < SCHEMA_VERSION:
            raw = self._migrate(raw, version)
            raw["version"] = SCHEMA_VERSION

        raw.setdefault("profiles", {})
        raw.setdefault("bindings", {})
        raw.setdefault("personas", {})
        raw.setdefault("pending_reconciliation", [])
        return raw

    @staticmethod
    def _empty_data() -> dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "profiles": {},
            "bindings": {},
            "personas": {},
            "pending_reconciliation": [],
        }

    @staticmethod
    def _migrate(raw: dict[str, Any], from_version: int) -> dict[str, Any]:
        """兼容旧 ownership.json；迁移只补结构，不猜测原生事实。"""
        if from_version < 1:
            raw.setdefault("profiles", {})
            raw.setdefault("bindings", {})
            raw.setdefault("pending_reconciliation", [])
        if from_version < 2:
            for profile in (raw.get("profiles") or {}).values():
                if isinstance(profile, dict):
                    profile.setdefault(
                        "astrbot_config",
                        {
                            "id": None,
                            "name": None,
                            "fingerprint": None,
                        },
                    )
            for binding in (raw.get("bindings") or {}).values():
                if isinstance(binding, dict):
                    binding.setdefault(
                        "config_route",
                        {
                            "written_config_id": None,
                            "previous_exact_route_existed": False,
                            "previous_exact_config_id": None,
                            "previous_matched_route_pattern": binding.get(
                                "matched_route_pattern"
                            ),
                            "previous_matched_route_config_id": binding.get(
                                "matched_route_config_id"
                            ),
                        },
                    )
        if from_version < 3:
            raw.setdefault("personas", {})
            for profile in (raw.get("profiles") or {}).values():
                if isinstance(profile, dict):
                    profile.setdefault("selected_persona_id", None)
        return raw

    # ---------- 写入 ----------

    async def transaction(
        self,
        mutator: Callable[[dict[str, Any]], Any],
    ) -> Any:
        """在锁内执行 `mutator(data)`，成功后原子落盘并返回其返回值。

        `mutator` 抛异常时不落盘。`data` 是内部字典，回调内可直接修改。
        """
        async with self._lock:
            if self._readonly_reason:
                raise StoreReadOnly(self._readonly_reason)
            if self._data is None:
                self._data = self._read_from_disk()

            working = copy.deepcopy(self._data)
            result = mutator(working)
            if asyncio.iscoroutine(result):
                result = await result
            await self._write_locked(working)
            self._data = working
            return result

    async def _write_locked(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        # 1) 同目录临时文件 + flush/fsync
        with self.tmp_path.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())

        # 2) 校验可重新读取
        try:
            reloaded = json.loads(self.tmp_path.read_text(encoding="utf-8"))
        except Exception as exc:
            self.tmp_path.unlink(missing_ok=True)
            raise StoreCorrupted(f"临时文件校验失败: {exc}") from exc
        if reloaded != data:
            self.tmp_path.unlink(missing_ok=True)
            raise StoreCorrupted("临时文件回读内容与内存数据不一致")

        # 3) 保留最近一次有效备份
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.backup_path)
            except Exception as exc:
                logger.warning(f"[stpro] 备份 ownership 失败（不阻断写入）: {exc}")

        # 4) 原子替换
        os.replace(self.tmp_path, self.path)

    # ---------- 便捷读取 ----------

    async def get_profile(self, profile_id: str) -> ProfileRecord | None:
        data = await self.load()
        raw = data["profiles"].get(profile_id)
        return ProfileRecord.from_dict(profile_id, raw) if raw else None

    async def get_binding(self, group_umo: str) -> BindingRecord | None:
        data = await self.load()
        raw = data["bindings"].get(group_umo)
        return BindingRecord.from_dict(raw) if raw else None

    async def list_profiles(self, owner_key: str | None = None) -> list[ProfileRecord]:
        data = await self.load()
        records = [
            ProfileRecord.from_dict(pid, raw)
            for pid, raw in data["profiles"].items()
            if isinstance(raw, dict)
        ]
        if owner_key:
            records = [r for r in records if r.owner_key == owner_key]
        return records

    async def list_bindings(self, profile_id: str | None = None) -> list[BindingRecord]:
        data = await self.load()
        records = [
            BindingRecord.from_dict(raw)
            for raw in data["bindings"].values()
            if isinstance(raw, dict)
        ]
        if profile_id:
            records = [r for r in records if r.profile_id == profile_id]
        return records

    async def get_persona(self, persona_id: str) -> PersonaRecord | None:
        data = await self.load()
        raw = data["personas"].get(persona_id)
        return PersonaRecord.from_dict(persona_id, raw) if raw else None

    async def list_personas(self, profile_id: str | None = None) -> list[PersonaRecord]:
        data = await self.load()
        records = [
            PersonaRecord.from_dict(pid, raw)
            for pid, raw in data["personas"].items()
            if isinstance(raw, dict)
        ]
        if profile_id:
            records = [record for record in records if record.profile_id == profile_id]
        return records

    async def save_persona(self, record: PersonaRecord) -> None:
        payload = record.to_dict()
        await self.transaction(
            lambda data: data["personas"].__setitem__(
                record.astrbot_persona_id, payload
            )
        )

    async def delete_persona(self, persona_id: str) -> None:
        await self.transaction(lambda data: data["personas"].pop(persona_id, None))

    async def add_pending_reconciliation(self, item: dict[str, Any]) -> None:
        await self.transaction(lambda data: data["pending_reconciliation"].append(item))
