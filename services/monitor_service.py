"""后台监控与健康状态机。

设计文档第 10 节：

- 每个 Profile 每周期只检查一次，再通知其绑定群；
- 首次失败只记录；连续达到阈值后通知异常；恢复后立即通知；状态不变不刷屏；
- 认证失败立即私聊所有者，群里只发脱敏提示；
- 未绑定群时只通知所有者；
- 不得把完整异常响应或 Key 存入监控状态。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

from ..storage.ownership_store import OwnershipStore, ProfileRecord, utc_now_iso
from ..utils.errors import ErrorCategory, StproError
from ..utils.locks import LockManager
from .model_client import ModelClient
from .provider_bridge import ProviderBridge

SendFn = Callable[[str, str], Awaitable[None]]

# 群内只发的脱敏提示，不含 Key、不含具体错误响应
_GROUP_ERROR_HINT = "当前绑定的档案连接异常，已通知档案所有者。"
_GROUP_AUTH_HINT = "当前绑定的档案认证失败，已通知档案所有者。"
_GROUP_RECOVER_HINT = "已恢复正常。"


@dataclass
class MonitorConfig:
    enable: bool = True
    interval_sec: int = 300
    failure_threshold: int = 3
    timeout_sec: int = 30
    max_concurrency: int = 3


class MonitorService:
    """去重调度、健康状态机与通知。"""

    def __init__(
        self,
        store: OwnershipStore,
        bridge: ProviderBridge,
        client: ModelClient,
        locks: LockManager,
        send: SendFn,
        config: MonitorConfig,
    ) -> None:
        self.store = store
        self.bridge = bridge
        self.client = client
        self.locks = locks
        self.send = send
        self.config = config
        self._task: asyncio.Task | None = None
        self._semaphore = asyncio.Semaphore(max(1, config.max_concurrency))
        self._inflight: set[str] = set()

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        if not self.config.enable or self._task is not None:
            return
        self._task = asyncio.create_task(self._loop(), name="stpro:monitor")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(f"[stpro] 监控任务退出异常: {exc}")
        finally:
            self._task = None
            self._inflight.clear()

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(max(30, self.config.interval_sec))
                await self.check_all()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # 一个 Profile 的异常不能终止整个监控循环
                logger.error(f"[stpro] 监控循环异常: {exc}")

    # ---------- 检查 ----------

    async def check_all(self) -> None:
        for record in await self.store.list_profiles():
            if record.profile_id in self._inflight:
                continue
            try:
                await self.check_profile(record)
            except Exception as exc:
                logger.error(
                    f"[stpro] 检查 Profile {record.profile_id} 失败: {exc}",
                )

    async def check_profile(self, record: ProfileRecord) -> None:
        """检查一个 Profile。同一 Profile 不重叠执行。"""
        if record.profile_id in self._inflight:
            return
        self._inflight.add(record.profile_id)
        try:
            async with self._semaphore:
                async with self.locks.profile_scope(record.profile_id):
                    await self._do_check(record)
        finally:
            self._inflight.discard(record.profile_id)

    async def _do_check(self, record: ProfileRecord) -> None:
        snapshot = self.bridge.inspect(record.provider_id)
        if not snapshot.exists:
            await self._apply_result(
                record, False, ErrorCategory.LOCAL, 0, missing=True
            )
            return
        if not snapshot.model or not snapshot.enable:
            # 未配置/被禁用不发起远程检查
            await self._apply_result(
                record, False, ErrorCategory.LOCAL, 0, unconfigured=True
            )
            return

        try:
            endpoint, api_key = await self._credentials(record)
            latency = await self.client.probe(endpoint, api_key, snapshot.model)
        except StproError as exc:
            await self._apply_result(record, False, exc.category, 0, error=exc)
            return
        except Exception as exc:
            wrapped = StproError(
                ErrorCategory.UNKNOWN,
                "发生未知错误，请稍后重试。",
                f"profile={record.profile_id}",
            )
            logger.warning(f"[stpro] 监控未知异常 profile={record.profile_id}: {exc}")
            await self._apply_result(record, False, wrapped.category, 0, error=wrapped)
            return

        await self._apply_result(record, True, None, latency)

    async def _credentials(self, record: ProfileRecord) -> tuple[str, str]:
        config = self.bridge.get_provider_config(record.provider_id) or {}
        keys = config.get("key") or []
        snapshot = self.bridge.inspect(record.provider_id)
        if not snapshot.endpoint or not keys:
            raise StproError(ErrorCategory.LOCAL, "档案配置不完整")
        return snapshot.endpoint, str(keys[0])

    # ---------- 状态机 ----------

    async def _apply_result(
        self,
        record: ProfileRecord,
        success: bool,
        category: str | None,
        latency_ms: int,
        *,
        error: StproError | None = None,
        missing: bool = False,
        unconfigured: bool = False,
    ) -> None:
        current = await self.store.get_profile(record.profile_id) or record
        previous = current.health_status
        previous_notification_kind = current.last_notification_kind

        if success:
            failures = 0
            new_state = "healthy"
        elif missing or unconfigured or category == ErrorCategory.LOCAL:
            # 配置缺失/未配置不是健康度问题，不改变健康状态
            failures = current.consecutive_failures
            new_state = previous
        else:
            failures = current.consecutive_failures + 1
            if previous == "unknown":
                new_state = "degraded"  # 首次失败仅记录
            elif failures >= self.config.failure_threshold:
                new_state = "unhealthy"
            else:
                new_state = "degraded"

        if success:
            notification_kind = None
        elif missing:
            notification_kind = "missing"
        elif unconfigured:
            notification_kind = "unconfigured"
        elif category == ErrorCategory.AUTH:
            notification_kind = "auth"
        elif new_state == "unhealthy":
            notification_kind = "unhealthy"
        else:
            notification_kind = "degraded"

        def _mutate(data: dict[str, Any]) -> None:
            raw = data["profiles"].get(record.profile_id)
            if raw is None:
                return
            monitor = raw.setdefault("monitor", {})
            monitor["last_checked_at"] = utc_now_iso()
            monitor["consecutive_failures"] = failures
            monitor["last_error_category"] = category
            monitor["last_latency_ms"] = latency_ms or None
            monitor["last_notification_kind"] = notification_kind
            raw.setdefault("status", {})["health"] = new_state
            if success:
                monitor["last_success_at"] = utc_now_iso()
                monitor["anomaly_notified"] = False

        await self.store.transaction(_mutate)

        await self._notify(
            record,
            success=success,
            previous=previous,
            new_state=new_state,
            failures=failures,
            category=category,
            error=error,
            missing=missing,
            previous_notification_kind=previous_notification_kind,
        )

    async def _notify(
        self,
        record: ProfileRecord,
        *,
        success: bool,
        previous: str,
        new_state: str,
        failures: int,
        category: str | None,
        error: StproError | None,
        missing: bool,
        previous_notification_kind: str | None,
    ) -> None:
        bindings = await self.store.list_bindings(record.profile_id)
        group_umos = [b.group_umo for b in bindings if b.state == "active"]

        if success:
            if previous in ("degraded", "unhealthy"):
                text = f"配置「{record.name}」{_GROUP_RECOVER_HINT}"
                await self._safe_send(record.owner_private_umo, text)
                for umo in group_umos:
                    await self._safe_send(umo, f"当前绑定的档案已恢复：{record.name}")
            return

        if category == ErrorCategory.AUTH:
            # 401/403 立即私聊所有者，不等待失败阈值
            if previous_notification_kind == "auth":
                return
            detail = f"配置「{record.name}」认证失败，请尽快更新 API Key。"
            await self._safe_send(record.owner_private_umo, detail)
            if group_umos:
                for umo in group_umos:
                    await self._safe_send(umo, _GROUP_AUTH_HINT)
            return

        if new_state == "unhealthy" and previous != "unhealthy":
            detail = (
                f"配置「{record.name}」连续 {failures} 次检查失败"
                + (f"（错误编号 {error.error_id}）" if error else "")
                + "。"
            )
            if group_umos:
                for umo in group_umos:
                    await self._safe_send(umo, _GROUP_ERROR_HINT)
            else:
                await self._safe_send(record.owner_private_umo, detail)
            if not group_umos:
                return
            await self._safe_send(record.owner_private_umo, detail)
            return

        if missing:
            if previous_notification_kind == "missing":
                return
            await self._safe_send(
                record.owner_private_umo,
                f"档案「{record.name}」对应的模型配置已不存在，可能已被管理员删除。"
                "插件不会自动重建。",
            )

    async def _safe_send(self, umo: str, text: str) -> None:
        """通知失败只记录通知错误，不改变 Provider 的健康判断。"""
        if not umo or not text:
            return
        try:
            await self.send(umo, text)
        except Exception as exc:
            logger.warning(f"[stpro] 通知发送失败 umo={umo}: {exc}")

    # ---------- 并发保护 ----------

    def is_checking(self, profile_id: str) -> bool:
        """手动 `update` 不得与后台检查并发请求同一 Profile。"""
        return profile_id in self._inflight
