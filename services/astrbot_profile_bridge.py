"""AstrBot 原生配置文件与会话配置路由 Adapter。

该模块隐藏 AstrBot 配置文件复制、进程内 Dashboard 服务和 UMO 精确路由。
调用方只需要围绕一个 STPRO Profile 创建/检查配置，并执行比较后绑定或解绑。
"""

from __future__ import annotations

import copy
import fnmatch
import hashlib
import json
from dataclasses import dataclass
from typing import Any

from astrbot.api import logger


@dataclass(frozen=True)
class NativeProfileSnapshot:
    config_id: str | None
    exists: bool
    name: str | None
    runner_type: str | None
    default_provider_id: str | None
    persona_id: str | None
    fingerprint: str | None


@dataclass(frozen=True)
class ConfigRouteSnapshot:
    group_umo: str
    exact_config_id: str | None
    matched_pattern: str | None
    matched_config_id: str | None
    effective_config_id: str


@dataclass(frozen=True)
class ConfigRouteResult:
    applied: bool
    reason: str


class AstrBotProfileBridge:
    """STPRO 与 AstrBot 原生配置文件系统之间的唯一配置 Adapter。"""

    DISPLAY_PREFIX = "STPRO / "

    def __init__(
        self,
        context: Any,
    ) -> None:
        self.context = context
        self.manager = context.astrbot_config_mgr

    def _dashboard_services(self) -> Any:
        """取得当前进程已经创建的 Dashboard 服务容器。

        AstrBot 4.28 尚未把“创建配置并热加载流水线”公开给插件 Context，
        但 Dashboard 自身已经将这组事务封装成服务。这里延迟解析该服务，
        避免插件加载顺序早于 Dashboard 初始化，也避免任何 HTTP/API Key。
        """
        try:
            from astrbot.dashboard import server as dashboard_server

            adapter = getattr(dashboard_server, "APP", None)
            app = getattr(adapter, "_app", None)
            state = getattr(app, "state", None)
            services = getattr(state, "services", None)
        except Exception as exc:
            raise RuntimeError("无法访问 AstrBot 进程内配置服务") from exc
        if services is None:
            raise RuntimeError(
                "AstrBot Dashboard 尚未初始化，暂时无法创建或修改配置文件"
            )
        return services

    async def _create_config_profile(self, *, name: str, config: dict[str, Any]) -> str:
        # 保留 Context 方法探测，便于未来 AstrBot 正式公开同名插件 API。
        method = getattr(self.context, "create_config_profile", None)
        if callable(method):
            return str(await method(name=name, config=config) or "")
        result = await self._dashboard_services().config_profiles.create_profile(
            name,
            config,
        )
        return str((result or {}).get("conf_id") or "")

    async def _delete_config_profile(self, config_id: str) -> bool:
        method = getattr(self.context, "delete_config_profile", None)
        if callable(method):
            return bool(await method(config_id))
        await self._dashboard_services().config_profiles.delete_profile(config_id)
        return True

    async def _update_config_profile(
        self,
        config_id: str,
        config: dict[str, Any],
    ) -> None:
        method = getattr(self.context, "update_config_profile", None)
        if callable(method):
            await method(config_id, config)
            return
        await self._dashboard_services().config_profiles.update_profile(
            config_id,
            config,
        )

    async def _set_config_route(self, umo: str, config_id: str) -> None:
        method = getattr(self.context, "set_config_route", None)
        if callable(method):
            await method(umo, config_id)
            return

        route_service = self._dashboard_services().config_routes
        routing = getattr(getattr(self.manager, "ucr", None), "umop_to_conf_id", {})
        if not isinstance(routing, dict):
            await route_service.set_route(umo, config_id)
            return

        # AstrBot 4.28 按路由表插入顺序返回第一个匹配项。WebUI 常见的
        # ``:: -> default`` 是全局回退，不应阻止群精确绑定；但普通
        # ``update_route`` 会把新路由追加到末尾，导致 default 永远先命中。
        # 仅当当前第一个匹配项确实指向 default 时，将精确路由插到它前面，
        # 其余管理员路由的相对顺序保持不变。非 default 通配项仍不会被越过。
        target = self._split_umo(umo)
        first_match: tuple[str, str] | None = None
        if target is not None:
            for pattern, candidate in routing.items():
                parts = self._split_umo(pattern)
                if parts is None:
                    continue
                if all(
                    part == "" or fnmatch.fnmatchcase(value, part)
                    for part, value in zip(parts, target)
                ):
                    first_match = (pattern, str(candidate))
                    break

        if first_match is None or first_match[0] == umo or first_match[1] != "default":
            await route_service.set_route(umo, config_id)
            return

        reordered: dict[str, str] = {}
        for pattern, candidate in routing.items():
            if pattern == first_match[0]:
                reordered[umo] = config_id
            if pattern == umo:
                continue
            reordered[pattern] = candidate
        await route_service.replace_route_mapping(reordered)

    async def _delete_config_route(self, umo: str) -> None:
        method = getattr(self.context, "delete_config_route", None)
        if callable(method):
            await method(umo)
            return
        await self._dashboard_services().config_routes.delete_route_by_umo(umo)

    @classmethod
    def display_name(cls, owner_id: str, profile_name: str) -> str:
        return f"{cls.DISPLAY_PREFIX}{owner_id} / {profile_name}"

    @staticmethod
    def _model_config(config: Any) -> dict[str, Any]:
        agent_runner = config.setdefault("agent_runner", {})
        runner_config = agent_runner.setdefault("config", {})
        return runner_config.setdefault("model", {})

    @staticmethod
    def _persona_config(config: Any) -> dict[str, Any]:
        agent_runner = config.setdefault("agent_runner", {})
        runner_config = agent_runner.setdefault("config", {})
        return runner_config.setdefault("persona", {})

    @staticmethod
    def _managed_fingerprint(
        runner_type: str | None,
        provider_id: str | None,
    ) -> str:
        payload = json.dumps(
            {"runner_type": runner_type, "provider_id": provider_id},
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def _config_meta(self, config_id: str) -> dict[str, Any] | None:
        data = getattr(self.manager, "abconf_data", None)
        if isinstance(data, dict):
            meta = data.get(config_id)
            return meta if isinstance(meta, dict) else None
        for info in self.manager.get_conf_list():
            if str(info.get("id")) == config_id:
                return dict(info)
        return None

    def inspect(
        self,
        config_id: str | None,
    ) -> NativeProfileSnapshot:
        if not config_id or config_id == "default":
            return NativeProfileSnapshot(
                config_id=config_id,
                exists=False,
                name=None,
                runner_type=None,
                default_provider_id=None,
                persona_id=None,
                fingerprint=None,
            )
        confs = getattr(self.manager, "confs", {})
        config = confs.get(config_id) if isinstance(confs, dict) else None
        meta = self._config_meta(config_id)
        if config is None or meta is None:
            return NativeProfileSnapshot(
                config_id=config_id,
                exists=False,
                name=None,
                runner_type=None,
                default_provider_id=None,
                persona_id=None,
                fingerprint=None,
            )

        agent_runner = config.get("agent_runner") or {}
        runner_type = agent_runner.get("runner_type")
        provider_id = ((agent_runner.get("config") or {}).get("model") or {}).get(
            "provider_id"
        )
        persona_id = ((agent_runner.get("config") or {}).get("persona") or {}).get(
            "persona_id"
        )
        return NativeProfileSnapshot(
            config_id=config_id,
            exists=True,
            name=str(meta.get("name") or ""),
            runner_type=str(runner_type) if runner_type is not None else None,
            default_provider_id=(
                str(provider_id)
                if provider_id is not None and provider_id != ""
                else None
            ),
            persona_id=(
                str(persona_id)
                if persona_id is not None and persona_id != ""
                else "default"
            ),
            fingerprint=self._managed_fingerprint(runner_type, provider_id),
        )

    def default_persona_id(self) -> str:
        config = self.manager.default_conf
        agent_runner = config.get("agent_runner") or {}
        persona_id = ((agent_runner.get("config") or {}).get("persona") or {}).get(
            "persona_id"
        )
        return str(persona_id or "default")

    def persona_references(self, persona_id: str) -> list[str]:
        """返回所有当前引用指定人格的原生配置 ID。"""
        references: list[str] = []
        confs = getattr(self.manager, "confs", {})
        if not isinstance(confs, dict):
            return references
        for config_id, config in confs.items():
            if not isinstance(config, dict):
                continue
            agent_runner = config.get("agent_runner") or {}
            current = ((agent_runner.get("config") or {}).get("persona") or {}).get(
                "persona_id"
            )
            if str(current or "default") == persona_id:
                references.append(str(config_id))
        return references

    async def set_persona(self, config_id: str | None, persona_id: str) -> None:
        """更新一个 STPRO 原生配置的人格，并让 AstrBot 热重载该流水线。"""
        if not config_id or config_id == "default":
            raise ValueError("STPRO 配置文件不存在")
        confs = getattr(self.manager, "confs", {})
        current = confs.get(config_id) if isinstance(confs, dict) else None
        if current is None:
            raise ValueError(f"Config file {config_id} does not exist")
        config = copy.deepcopy(dict(current))
        self._persona_config(config)["persona_id"] = str(persona_id or "default")
        await self._update_config_profile(config_id, config)

    async def create_for_profile(
        self,
        owner_id: str,
        profile_name: str,
        provider_id: str,
    ) -> NativeProfileSnapshot:
        # 必须复制当前完整 default，而不是 AstrBot 的静态空白模板。
        config = copy.deepcopy(dict(self.manager.default_conf))
        self._model_config(config)["provider_id"] = provider_id
        config_id = await self._create_config_profile(
            name=self.display_name(owner_id, profile_name),
            config=config,
        )
        config_id = str(config_id or "")
        if not config_id:
            raise RuntimeError("AstrBot 未返回配置文件 ID")
        snapshot = self.inspect(config_id)
        if not snapshot.exists:
            raise RuntimeError("AstrBot 配置文件创建后无法读取")
        return snapshot

    async def delete_owned(
        self,
        config_id: str | None,
        provider_id: str,
        *,
        allow_missing: bool = True,
    ) -> bool:
        if not config_id:
            return False
        snapshot = self.inspect(config_id)
        if not snapshot.exists:
            return False if allow_missing else False
        if snapshot.default_provider_id != provider_id:
            raise PermissionError(
                "AstrBot 管理员已修改该配置文件的默认 Provider，拒绝删除配置文件"
            )
        return await self._delete_config_profile(config_id)

    @staticmethod
    def _split_umo(umo: str) -> tuple[str, str, str] | None:
        if not isinstance(umo, str):
            return None
        parts = umo.split(":", 2)
        if len(parts) != 3:
            return None
        return parts[0], parts[1], parts[2]

    def inspect_route(self, group_umo: str) -> ConfigRouteSnapshot:
        routing = getattr(getattr(self.manager, "ucr", None), "umop_to_conf_id", {})
        routing = routing if isinstance(routing, dict) else {}
        exact = routing.get(group_umo)
        target = self._split_umo(group_umo)
        matched_pattern = None
        matched_config_id = None
        if target is not None:
            for pattern, config_id in routing.items():
                parts = self._split_umo(pattern)
                if parts is None:
                    continue
                if all(
                    part == "" or fnmatch.fnmatchcase(value, part)
                    for part, value in zip(parts, target)
                ):
                    matched_pattern = pattern
                    matched_config_id = str(config_id)
                    break
        info = self.manager.get_conf_info(group_umo) or {}
        effective = str(info.get("id") or "default")
        return ConfigRouteSnapshot(
            group_umo=group_umo,
            exact_config_id=str(exact) if exact is not None else None,
            matched_pattern=matched_pattern,
            matched_config_id=matched_config_id,
            effective_config_id=effective,
        )

    async def bind_group(
        self,
        group_umo: str,
        config_id: str,
        *,
        expected_exact_config_id: str | None,
    ) -> ConfigRouteResult:
        current = self.inspect_route(group_umo)
        if current.exact_config_id != expected_exact_config_id:
            return ConfigRouteResult(False, "route_changed")
        await self._set_config_route(group_umo, config_id)
        verified = self.inspect_route(group_umo)
        if (
            verified.exact_config_id != config_id
            or verified.effective_config_id != config_id
        ):
            # AstrBot 当前按路由表顺序匹配；若更早的通配规则抢先命中，新增
            # 精确路由也不会生效。此时恢复原值，不重排管理员的整张路由表。
            if expected_exact_config_id is None:
                await self._delete_config_route(group_umo)
            else:
                await self._set_config_route(
                    group_umo,
                    expected_exact_config_id,
                )
            return ConfigRouteResult(False, "shadowed_by_route")
        return ConfigRouteResult(True, "ok")

    async def unbind_group(
        self,
        group_umo: str,
        written_config_id: str,
        *,
        previous_exact_route_existed: bool,
        previous_exact_config_id: str | None,
    ) -> ConfigRouteResult:
        current = self.inspect_route(group_umo)
        if current.exact_config_id != written_config_id:
            return ConfigRouteResult(False, "route_changed")
        if previous_exact_route_existed and previous_exact_config_id:
            await self._set_config_route(group_umo, previous_exact_config_id)
        else:
            await self._delete_config_route(group_umo)
        return ConfigRouteResult(True, "ok")

    async def migrate_legacy_binding(
        self,
        group_umo: str,
        config_id: str,
        provider_id: str,
        legacy_bridge: Any,
        *,
        expected_exact_config_id: str | None,
    ) -> ConfigRouteResult:
        """先写安全的原生配置路由，再删除旧会话 Provider 覆盖。"""
        current = self.inspect_route(group_umo)
        if current.exact_config_id != expected_exact_config_id:
            return ConfigRouteResult(False, "route_changed")
        old_rule = await legacy_bridge.read_group_rule(group_umo)
        if old_rule != provider_id:
            return ConfigRouteResult(False, "legacy_rule_changed")
        await self._set_config_route(group_umo, config_id)
        verified = self.inspect_route(group_umo)
        if verified.effective_config_id != config_id:
            if expected_exact_config_id is None:
                await self._delete_config_route(group_umo)
            else:
                await self._set_config_route(
                    group_umo,
                    expected_exact_config_id,
                )
            return ConfigRouteResult(False, "shadowed_by_route")
        try:
            await legacy_bridge.remove_legacy_group_rule(group_umo, provider_id)
        except Exception:
            # 路由已可用，保留旧覆盖会抢占新配置，必须回滚精确路由。
            await self._delete_config_route(group_umo)
            raise
        logger.info(f"[stpro] 已迁移旧群绑定到 AstrBot 配置路由: {group_umo}")
        return ConfigRouteResult(True, "ok")
