"""STPRO v0.4 真机探针。

本文件不自动运行，不创建临时 Provider，也不直接修改 AstrBot 内部状态。它读取一个
已经由 `/stpro new` 和 `/stpro bind` 创建的测试档案，验证 WebUI 原生配置与会话配置
路由的运行时事实。请只对专门的测试档案和测试群执行。
"""

from __future__ import annotations

from astrbot.api import logger


def _ok(lines: list[str], name: str, passed: bool, detail: str = "") -> None:
    mark = "PASS" if passed else "FAIL"
    lines.append(f"[{mark}] {name}" + (f" —— {detail}" if detail else ""))


async def run_probes(
    plugin,
    *,
    owner_key: str,
    profile_name: str,
    test_group_umo: str,
) -> str:
    """验证测试档案的原生配置、精确路由与运行时调度器。

    调用前先由 `owner_key` 对应用户创建档案、选择模型，并绑定 `test_group_umo`。
    `plugin` 是正在运行的 StproPlugin 实例。
    """
    lines = ["=== STPRO v0.4 真机探针 ==="]
    record = await plugin.profile_service.find_by_name(owner_key, profile_name)
    if record is None:
        _ok(lines, "找到测试档案", False, profile_name)
        return "\n".join(lines)
    owner_id = owner_key.split(":", 1)[-1]
    expected_name = f"STPRO / {owner_id} / {record.name}"

    manager = plugin.context.astrbot_config_mgr
    native = plugin.config_bridge.inspect(record.astrbot_config_id)
    _ok(lines, "原生配置存在", native.exists, str(record.astrbot_config_id))
    _ok(
        lines,
        "原生配置名称正确",
        native.name == expected_name,
        str(native.name),
    )
    _ok(
        lines,
        "原生配置默认 Provider 正确",
        native.default_provider_id == record.provider_id,
        str(native.default_provider_id),
    )

    listed = manager.get_conf_list()
    visible = any(
        str(
            getattr(info, "id", None)
            or (info.get("id") if isinstance(info, dict) else "")
        )
        == record.astrbot_config_id
        for info in listed
    )
    _ok(lines, "配置出现在 get_conf_list", visible)

    route = plugin.config_bridge.inspect_route(test_group_umo)
    _ok(
        lines,
        "目标群精确路由正确",
        route.exact_config_id == record.astrbot_config_id,
        str(route.exact_config_id),
    )
    _ok(
        lines,
        "目标群实际命中 STPRO 配置",
        route.effective_config_id == record.astrbot_config_id,
        f"pattern={route.matched_pattern} effective={route.effective_config_id}",
    )

    lifecycle = getattr(plugin.context, "core_lifecycle", None)
    if lifecycle is None:
        lifecycle = getattr(plugin.context, "_core_lifecycle", None)
    schedulers = None
    if lifecycle is not None:
        schedulers = getattr(lifecycle, "pipeline_scheduler_mapping", None)
        if schedulers is None:
            schedulers = getattr(lifecycle, "pipeline_schedulers", None)
    if isinstance(schedulers, dict):
        _ok(
            lines,
            "PipelineScheduler 已热装配",
            record.astrbot_config_id in schedulers,
            f"已装配 {len(schedulers)} 个配置",
        )
    else:
        lines.append(
            "[MANUAL] 当前版本未从插件 Context 暴露 Scheduler 映射；"
            "请在 WebUI 保存后立即向测试群发消息，确认无需重启即可使用该配置。"
        )

    lines.append(f"[MANUAL] 请在 WebUI“配置文件”确认显示：{expected_name}")
    lines.append(
        "[MANUAL] 请在 WebUI“为特定会话选择配置”确认："
        f"{test_group_umo} → {expected_name}"
    )
    report = "\n".join(lines)
    logger.info(f"\n{report}")
    return report
