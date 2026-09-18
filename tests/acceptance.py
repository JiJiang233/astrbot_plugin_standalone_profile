"""设计文档第 16/17/18 节的验收序列，离线可执行。

    python3 data/plugins/astrbot_plugin_standalone_profile/tests/acceptance.py

每条 `check` 对应文档里的一条验收条目（编号见注释）。无法在桩环境验证的条目在
文件末尾的 `NOT_COVERED` 中列出，需要在真实 AstrBot 上用 `probes.py` 补齐。
"""

from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "tests"))
sys.path.insert(0, str(PLUGIN_ROOT.parent))

# 注意：必须先 import run_tests —— 它会先装配 astrbot 桩并导入插件模块。
# main.py 顶层 `from ... import get_astrbot_plugin_data_path` 绑定的是那次装配的
# 函数，这里不能再 build 一个不同的数据目录，否则"清理目录"与"实际读写目录"
# 会不一致，用例之间出现数据串扰。
import run_tests  # noqa: F401  E402
from run_tests import (  # noqa: E402
    CHAT_RULE_KEY,
    DATA_DIR,
    SP,
    FakeContext,
    FakeEvent,
    FakeModelClient,
    check,
    drain,
)

LOG = run_tests._astrbot_stub.logger

from astrbot_plugin_standalone_profile.main import StproPlugin  # noqa: E402
from astrbot_plugin_standalone_profile.utils.errors import (  # noqa: E402
    ErrorCategory,
    PermissionDenied,
    StproError,
)

# --------------------------- 可控的假环境 ---------------------------


class RouteTable:
    """模拟 AstrBot 的配置路由与配置文件默认 Provider（可被"管理员"改动）。"""

    def __init__(self) -> None:
        self.umo_conf: dict[str, str] = {}
        self.conf_default: dict[str, str | None] = {
            "default": "provider_A",
            "conf2": "provider_X",
        }

    def conf_id(self, umo: str) -> str:
        return self.umo_conf.get(umo, "default")

    def conf_info(self, umo: str) -> dict:
        return {"id": self.conf_id(umo), "name": self.conf_id(umo), "path": "c.json"}

    def config(self, umo: str) -> dict:
        return {
            "agent_runner": {
                "runner_type": "local",
                "config": {
                    "model": {"provider_id": self.conf_default.get(self.conf_id(umo))},
                },
            },
        }


class RoutableContext(FakeContext):
    """FakeContext + 可变的路由表（模拟管理员改路由/改默认 Provider）。"""

    def __init__(self) -> None:
        super().__init__()
        self.routes = RouteTable()
        self.routes.umo_conf = self.config_routes
        self.routes.conf_default = {"default": "provider_A", "conf2": "provider_X"}

    def get_config(self, umo=None):
        return super().get_config(umo)


class FailingClient:
    """让远程请求稳定失败，用于验证"三次失败"与监控阈值。"""

    def __init__(self, category: str = ErrorCategory.AUTH) -> None:
        self.category = category
        self.calls = 0

    async def fetch_models(self, endpoint, api_key):
        self.calls += 1
        raise StproError(self.category, "模拟远程失败")

    async def probe(self, endpoint, api_key, model):
        self.calls += 1
        raise StproError(self.category, "模拟远程失败")


class CountingClient(FakeModelClient):
    """统计远程调用次数的正常客户端。"""

    def __init__(self) -> None:
        self.probes = 0

    async def probe(self, endpoint, api_key, model):
        self.probes += 1
        return 5


def make_plugin(ctx: FakeContext | None = None, **conf) -> StproPlugin:
    ctx = ctx or RoutableContext()
    plugin = StproPlugin(ctx, {"monitor": {"enable": False}, **conf})
    plugin.client = FakeModelClient()
    plugin.profile_service.client = plugin.client
    plugin.monitor.client = plugin.client
    return plugin


async def prepare() -> tuple[StproPlugin, RoutableContext]:
    shutil.rmtree(DATA_DIR / "plugin_data", ignore_errors=True)
    SP.store.clear()
    LOG.records.clear()
    (DATA_DIR / "plugin_data").mkdir(parents=True, exist_ok=True)
    ctx = RoutableContext()
    plugin = make_plugin(ctx)
    await plugin.initialize()
    return plugin, ctx


# --------------------------- 16.1 new ---------------------------


async def accept_new() -> None:
    print("\n[16.1] new")
    plugin, ctx = await prepare()

    out = await drain(plugin.cmd_new(FakeEvent("/stpro new 公司接口"), "公司接口"))
    check("16.1 进入 Endpoint 输入", bool(out) and "Endpoint" in out[0], str(out))

    # 名称重复 / 非法必须在询问 Endpoint 前拒绝
    await plugin.interaction.cancel("aiocqhttp:10001")
    long_name = "名" * 33
    out = await drain(plugin.cmd_new(FakeEvent(f"/stpro new {long_name}"), long_name))
    check("16.1 超长名称被拒", bool(out) and "最长" in out[0], str(out))
    out = await drain(
        plugin.cmd_new(FakeEvent("/stpro new 名字 带空格"), "名字 带空格")
    )
    check(
        "16.1 非法名称在询问 Endpoint 前拒绝",
        bool(out) and "不能包含空格" in out[0],
        str(out),
    )
    check(
        "16.1 未进入多轮流程", await plugin.interaction.get("aiocqhttp:10001") is None
    )

    # 成功创建：未选模型、禁用
    await drain(plugin.cmd_new(FakeEvent("/stpro new 公司接口"), "公司接口"))
    await drain(plugin.on_private_message(FakeEvent("https://api.example.com/v1")))
    await drain(plugin.on_private_message(FakeEvent("sk-abcdefgh12345678")))
    await asyncio.sleep(0)
    for t in list(plugin._remote_tasks.values()):
        await t
    cfg = ctx.provider_manager.providers_config[0]
    check(
        "16.1 创建后 model 为空且禁用",
        cfg["model"] == "" and cfg["enable"] is False,
        str(cfg),
    )
    check(
        "16.1 Provider ID 使用新版展示格式",
        cfg["id"] == "STPRO / 10001 / 公司接口",
        cfg["id"],
    )
    notified = " ".join(text for _, text in ctx.sent)
    check("16.1 返回模型编号并提示另发 model", "/stpro model" in notified, notified)

    # 三次远程失败：不留下 Provider、记录、内存 Key
    plugin.store._data = None
    await plugin.terminate()
    shutil.rmtree(DATA_DIR / "plugin_data", ignore_errors=True)
    SP.store.clear()
    ctx2 = RoutableContext()
    plugin2 = make_plugin(ctx2)
    plugin2.client = FailingClient()
    plugin2.profile_service.client = plugin2.client
    await plugin2.initialize()
    await drain(plugin2.cmd_new(FakeEvent("/stpro new 失败档案"), "失败档案"))
    await drain(plugin2.on_private_message(FakeEvent("https://api.example.com/v1")))
    for _ in range(3):
        await drain(plugin2.on_private_message(FakeEvent("sk-abcdefgh12345678")))
        for t in list(plugin2._remote_tasks.values()):
            await t
    check(
        "16.1 三次远程失败后无 Provider", ctx2.provider_manager.providers_config == []
    )
    check(
        "16.1 三次远程失败后无所有权记录", (await plugin2.store.list_profiles()) == []
    )
    check(
        "16.1 三次远程失败后流程已结束",
        await plugin2.interaction.get("aiocqhttp:10001") is None,
    )

    # 新命令打断旧流程
    await drain(plugin.cmd_new(FakeEvent("/stpro new 甲"), "甲"))
    out = await drain(plugin.cmd_list(FakeEvent("/stpro list"), ""))
    check("16.1 新命令先提示旧操作已取消", bool(out) and "已取消" in out[0], str(out))
    plugin.store._data = None
    await plugin.terminate()


# --------------------------- 16.2 set ---------------------------


async def accept_set() -> None:
    print("\n[16.2] set")
    plugin, ctx = await prepare()
    record = await _create_profile(plugin, "档案一")
    await plugin.profile_service.select_model(record, "gpt-4o")
    plugin._pending_self_id = "99999"
    await plugin.binding_service.bind(
        "aiocqhttp:10001", record, "aiocqhttp:GroupMessage:1"
    )

    # 新端点含原模型 → 保留模型和启用，绑定继续
    plugin.client.models = ["gpt-4o", "other"]
    await plugin.profile_service.replace_credentials(
        record, "https://new.example.com/v1", "sk-newkey1234567890"
    )
    cfg = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    check(
        "16.2 含原模型：保留模型并启用",
        cfg["model"] == "gpt-4o" and cfg["enable"] is True,
        str(cfg),
    )
    binding = await plugin.store.get_binding("aiocqhttp:GroupMessage:1")
    check("16.2 绑定仍 active", binding is not None and binding.state == "active")

    # 新端点不含原模型 → 清空模型、禁用，绑定 paused
    plugin.client.models = ["only-other"]
    await plugin.profile_service.replace_credentials(
        record, "https://new2.example.com/v1", "sk-newkey1234567890"
    )
    cfg = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    check(
        "16.2 不含原模型：清空模型并禁用",
        cfg["model"] == "" and cfg["enable"] is False,
        str(cfg),
    )
    binding = await plugin.store.get_binding("aiocqhttp:GroupMessage:1")
    check(
        "16.2 绑定进入 paused",
        binding is not None and binding.state == "paused",
        str(binding),
    )

    # 候选验证失败 → 原配置不变
    before = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    failing = FailingClient()
    plugin.profile_service.client = failing
    try:
        await plugin.profile_service.replace_credentials(
            record, "https://bad.example.com/v1", "sk-bad"
        )
    except StproError:
        pass
    after = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    check("16.2 候选验证失败：原配置不变", before == after, f"{before} != {after}")
    plugin.profile_service.client = plugin.client

    # admin_managed → 拒绝且零写入
    await plugin.store.transaction(
        lambda d: d["profiles"][record.profile_id].__setitem__("admin_managed", True),
    )
    snapshot_before = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    try:
        await plugin.profile_service.replace_credentials(
            record, "https://x.example.com/v1", "sk-x"
        )
        rejected = False
    except PermissionDenied:
        rejected = True
    check("16.2 admin_managed 拒绝 set", rejected)
    check(
        "16.2 admin_managed 零写入",
        ctx.provider_manager.get_provider_config_by_id(record.provider_id)
        == snapshot_before,
    )
    plugin.store._data = None
    await plugin.terminate()


# --------------------------- 16.3 model ---------------------------


async def accept_model() -> None:
    print("\n[16.3] model")
    plugin, ctx = await prepare()
    record = await _create_profile(plugin, "档案二")

    await plugin.interaction.start(
        "aiocqhttp:10001", "model", profile_id=record.profile_id
    )
    await plugin.interaction.remember_models(
        "aiocqhttp:10001", ["gpt-4o", "gpt-4o-mini"]
    )

    model_id = await plugin.interaction.resolve_model_index(
        "aiocqhttp:10001",
        record.profile_id,
        1,
    )
    check("16.3 有效序号解析为模型 ID", model_id == "gpt-4o", str(model_id))

    check(
        "16.3 越界序号被拒",
        await plugin.interaction.resolve_model_index(
            "aiocqhttp:10001", record.profile_id, 9
        )
        is None,
    )
    check(
        "16.3 跨 Profile 序号被拒",
        await plugin.interaction.resolve_model_index(
            "aiocqhttp:10001", "other-profile", 1
        )
        is None,
    )
    # 缓存过期
    plugin.interaction.model_cache_ttl_sec = 0
    check(
        "16.3 过期序号被拒",
        await plugin.interaction.resolve_model_index(
            "aiocqhttp:10001", record.profile_id, 1
        )
        is None,
    )
    plugin.interaction.model_cache_ttl_sec = 300

    # 支持列表中的模型名称，但拒绝列表之外的任意模型 ID 文本
    out = await drain(
        plugin.cmd_model(
            FakeEvent("/stpro model 档案二 not-in-list-model"),
            "档案二",
            "not-in-list-model",
        )
    )
    check(
        "16.3 不接受列表外的模型 ID 文本", bool(out) and "没有找到" in out[0], str(out)
    )
    out = await drain(
        plugin.cmd_model(
            FakeEvent("/stpro model 档案二 gpt-4o-mini"), "档案二", "gpt-4o-mini"
        )
    )
    check("16.3 列表内的模型名称可用", bool(out) and "gpt-4o-mini" in out[0], str(out))

    # 选模型后启用，暂停的绑定恢复
    await plugin.profile_service.select_model(record, "gpt-4o")
    cfg = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    check(
        "16.3 选择后写入模型并启用",
        cfg["model"] == "gpt-4o" and cfg["enable"] is True,
        str(cfg),
    )
    plugin.store._data = None
    await plugin.terminate()


# --------------------------- 16.4 bind / unbind ---------------------------


async def accept_bind() -> None:
    print("\n[16.4] bind / unbind")
    plugin, ctx = await prepare()
    record = await _create_profile(plugin, "档案三")
    await plugin.profile_service.select_model(record, "gpt-4o")
    plugin._pending_self_id = "99999"

    umo_a = "aiocqhttp:GroupMessage:111"
    umo_b = "aiocqhttp:GroupMessage:222"
    await plugin.binding_service.bind("aiocqhttp:10001", record, umo_a)
    check(
        "16.4 目标群配置路由被写入",
        ctx.config_routes.get(umo_a) == record.astrbot_config_id,
    )
    check(
        "16.4 其他群不受影响",
        umo_b not in ctx.config_routes,
    )

    # 已有管理员非 STPRO 规则 → 零写入拒绝
    await SP.session_put(umo_b, CHAT_RULE_KEY, "provider_admin_B")
    outcome = await plugin.binding_service.bind("aiocqhttp:10001", record, umo_b)
    check("16.4 管理员规则下拒绝绑定", not outcome.ok, outcome.message)
    check(
        "16.4 拒绝时管理员规则不变",
        await SP.get_async("umo", umo_b, CHAT_RULE_KEY, None) == "provider_admin_B",
    )

    # 非绑定管理员不能解绑
    try:
        await plugin.binding_service.unbind("aiocqhttp:20002", umo_a)
        denied = False
    except PermissionDenied:
        denied = True
    check("16.4 非绑定管理员不能解绑", denied)

    # 管理员在绑定后改写规则 → 解绑不得删除管理员新规则
    await SP.session_put(umo_a, CHAT_RULE_KEY, "provider_admin_C")
    await plugin.binding_service.unbind("aiocqhttp:10001", umo_a)
    check(
        "16.4 解绑不删除管理员新规则",
        await SP.get_async("umo", umo_a, CHAT_RULE_KEY, None) == "provider_admin_C",
    )

    # 绑定前无会话规则 → 解绑删除覆盖并自然回落
    umo_c = "aiocqhttp:GroupMessage:333"
    await plugin.binding_service.bind("aiocqhttp:10001", record, umo_c)
    await plugin.binding_service.unbind("aiocqhttp:10001", umo_c)
    check(
        "16.4 无历史路由时解绑自然回落",
        umo_c not in ctx.config_routes,
    )
    plugin.store._data = None
    await plugin.terminate()


# --------------------------- 16.5 remove ---------------------------


async def accept_remove() -> None:
    print("\n[16.5] remove")
    plugin, ctx = await prepare()
    record = await _create_profile(plugin, "档案四")
    plugin._pending_self_id = "99999"
    await plugin.binding_service.bind(
        "aiocqhttp:10001", record, "aiocqhttp:GroupMessage:1"
    )

    # 未确认 / token 不匹配不删除
    out = await drain(plugin.cmd_remove(FakeEvent("/stpro remove 档案四"), "档案四"))
    token = out[0].strip().splitlines()[-1]
    out = await drain(plugin.on_private_message(FakeEvent("xxxxxxxxxxxxxxxx")))
    check("16.5 token 不匹配不删除", bool(out) and "取消" in out[0], str(out))
    check("16.5 Provider 仍在", len(ctx.provider_manager.providers_config) == 1)

    # 发起新命令使确认失效
    await drain(plugin.cmd_remove(FakeEvent("/stpro remove 档案四"), "档案四"))
    out = await drain(plugin.cmd_list(FakeEvent("/stpro list"), ""))
    check("16.5 新命令使确认失效", bool(out) and "已取消" in out[0], str(out))

    # 确认超时
    await drain(plugin.cmd_remove(FakeEvent("/stpro remove 档案四"), "档案四"))
    token = (await plugin.interaction.get("aiocqhttp:10001")).confirm_token
    base = plugin.interaction._clock
    plugin.interaction._clock = lambda: base() + 10_000
    out = await drain(plugin.on_private_message(FakeEvent(token)))
    check("16.5 确认超时后不删除", out == [], str(out))
    plugin.interaction._clock = base

    # 正常删除：先解绑、再删 Provider、最后删记录
    await drain(plugin.cmd_remove(FakeEvent("/stpro remove 档案四"), "档案四"))
    token = (await plugin.interaction.get("aiocqhttp:10001")).confirm_token
    out = await drain(plugin.on_private_message(FakeEvent(token)))
    check("16.5 确认后删除完成", bool(out) and "已删除" in out[0], str(out))
    check("16.5 Provider 已删除", ctx.provider_manager.providers_config == [])
    check("16.5 无孤儿：所有权记录已清", (await plugin.store.list_profiles()) == [])
    check(
        "16.5 群规则已解除",
        await SP.get_async("umo", "aiocqhttp:GroupMessage:1", CHAT_RULE_KEY, None)
        is None,
    )

    # admin_managed 且 Provider 尚在 → 拒绝
    record2 = await _create_profile(plugin, "档案五")
    await plugin.store.transaction(
        lambda d: d["profiles"][record2.profile_id].__setitem__("admin_managed", True),
    )
    out = await drain(plugin.cmd_remove(FakeEvent("/stpro remove 档案五"), "档案五"))
    check(
        "16.5 admin_managed 时拒绝 remove", bool(out) and "管理员" in out[0], str(out)
    )
    plugin.store._data = None
    await plugin.terminate()


# --------------------------- 16.6 update 与监控 ---------------------------


async def accept_monitor() -> None:
    print("\n[16.6] update 与后台监控")
    plugin, ctx = await prepare()
    record = await _create_profile(plugin, "档案六")
    await plugin.profile_service.select_model(record, "gpt-4o")
    counting = CountingClient()
    plugin.monitor.client = counting
    plugin.profile_service.client = counting

    # 一个 Profile 绑定多群，每周期只检查一次
    plugin._pending_self_id = "99999"
    for gid in ("1", "2", "3"):
        await plugin.binding_service.bind(
            "aiocqhttp:10001",
            record,
            f"aiocqhttp:GroupMessage:{gid}",
        )
    await plugin.monitor.start()
    await plugin.monitor.check_all()
    check("16.6 每周期只检查一次", counting.probes == 1, f"probes={counting.probes}")
    await plugin.monitor.stop()

    # 达到阈值通知一次，持续失败不刷屏
    plugin.monitor.client = FailingClient(ErrorCategory.NETWORK)
    await plugin.monitor.start()
    ctx.sent.clear()
    for _ in range(plugin.monitor.config.failure_threshold):
        await plugin.monitor.check_profile(record)
    notified_once = len(ctx.sent)
    for _ in range(3):
        await plugin.monitor.check_profile(record)
    check("16.6 达到阈值通知", notified_once >= 1, f"sent={ctx.sent}")
    check(
        "16.6 持续失败不刷屏",
        len(ctx.sent) == notified_once,
        f"{notified_once} -> {len(ctx.sent)}",
    )

    # 恢复后通知一次
    plugin.monitor.client = counting
    await plugin.monitor.check_profile(record)
    check(
        "16.6 恢复后通知",
        len(ctx.sent) > notified_once,
        f"{notified_once} -> {len(ctx.sent)}",
    )
    await plugin.monitor.stop()

    # 认证失败立即私聊所有者
    ctx.sent.clear()
    plugin.monitor.client = FailingClient(ErrorCategory.AUTH)
    await plugin.monitor.start()
    await plugin.monitor.check_profile(record)
    targets = [umo for umo, _ in ctx.sent]
    check(
        "16.6 认证失败私聊所有者",
        any("FriendMessage" in u for u in targets),
        str(targets),
    )
    await plugin.monitor.stop()

    # update 只读：不改 Endpoint/Key/模型/路由
    before = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    await plugin.monitor.check_profile(record)
    after = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    check("16.6 检查不修改 Provider 配置", before == after, f"{before} != {after}")
    plugin.store._data = None
    await plugin.terminate()


# --------------------------- 17 管理员优先专项 ---------------------------


async def accept_admin_priority() -> None:
    print("\n[17] 管理员优先专项验收")
    plugin, ctx = await prepare()
    record = await _create_profile(plugin, "档案七")
    await plugin.profile_service.select_model(record, "gpt-4o")
    plugin._pending_self_id = "99999"
    umo = "aiocqhttp:GroupMessage:777"

    # 1. 群原用默认 A；绑 S；管理员显式改为 B → 最终 B
    await plugin.binding_service.bind("aiocqhttp:10001", record, umo)
    await SP.session_put(umo, CHAT_RULE_KEY, "provider_B")
    await plugin.reconciler.reconcile()
    check(
        "17-1 管理员显式改群规则后保持 B",
        await SP.get_async("umo", umo, CHAT_RULE_KEY, None) == "provider_B",
    )

    # 2. 群已有显式规则 A；普通用户绑 S → 拒绝，A 不变
    umo2 = "aiocqhttp:GroupMessage:888"
    await SP.session_put(umo2, CHAT_RULE_KEY, "provider_A")
    outcome = await plugin.binding_service.bind("aiocqhttp:10001", record, umo2)
    check("17-2 已有显式规则时拒绝绑定", not outcome.ok, outcome.message)
    check(
        "17-2 规则 A 未被改动",
        await SP.get_async("umo", umo2, CHAT_RULE_KEY, None) == "provider_A",
    )

    # 3. 管理员把群路由到另一个配置文件 → 保留管理员路由并让位
    umo3 = "aiocqhttp:GroupMessage:901"
    await plugin.binding_service.bind("aiocqhttp:10001", record, umo3)
    ctx.routes.umo_conf[umo3] = "conf2"
    await plugin.reconciler.reconcile()
    check(
        "17-3 路由变化后保留管理员配置",
        ctx.config_routes.get(umo3) == "conf2",
    )
    binding = await plugin.store.get_binding(umo3)
    check(
        "17-3 绑定标记 admin_overridden",
        binding is not None and binding.state == "admin_overridden",
    )

    # 4. 管理员修改 STPRO 原生配置文件的默认 Provider → 档案让位
    umo4 = "aiocqhttp:GroupMessage:902"
    await plugin.binding_service.bind("aiocqhttp:10001", record, umo4)
    ctx.native_configs[record.astrbot_config_id]["agent_runner"]["config"]["model"][
        "provider_id"
    ] = "provider_new_default"
    await plugin.reconciler.reconcile()
    refreshed4 = await plugin.store.get_profile(record.profile_id)
    check(
        "17-4 默认 Provider 变化后让位",
        refreshed4 is not None and refreshed4.admin_managed,
    )
    ctx.native_configs[record.astrbot_config_id]["agent_runner"]["config"]["model"][
        "provider_id"
    ] = record.provider_id
    await plugin.store.transaction(
        lambda d: d["profiles"][record.profile_id].__setitem__("admin_managed", False),
    )

    # 5. 管理员修改 S 的模型/Endpoint → admin_managed，set/model 被拒
    cfg = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    cfg["model"] = "admin-model"
    ctx.provider_manager.providers_config = [
        cfg if p["id"] == record.provider_id else p
        for p in ctx.provider_manager.providers_config
    ]
    await plugin.reconciler.reconcile()
    refreshed = await plugin.store.get_profile(record.profile_id)
    check(
        "17-5 管理员改配置后标记 admin_managed",
        refreshed is not None and refreshed.admin_managed,
    )
    try:
        await plugin.profile_service.select_model(refreshed, "gpt-4o")
        denied = False
    except PermissionDenied:
        denied = True
    check("17-5 admin_managed 禁止 model", denied)

    # 6. 管理员删除 S → missing，不重建
    umo6 = "aiocqhttp:GroupMessage:906"
    await plugin.binding_service.bind("aiocqhttp:10001", record, umo6)
    await plugin.store.transaction(
        lambda d: d["profiles"][record.profile_id].__setitem__("admin_managed", False),
    )
    await ctx.provider_manager.delete_provider(provider_id=record.provider_id)
    await plugin.reconciler.reconcile()
    refreshed = await plugin.store.get_profile(record.profile_id)
    check(
        "17-6 Provider 删除后标记 missing",
        refreshed is not None and refreshed.config_status == "missing",
    )
    check("17-6 未自动重建 Provider", ctx.provider_manager.providers_config == [])
    binding = await plugin.store.get_binding(umo6)
    check(
        "17-6 绑定暂停", binding is not None and binding.state == "paused", str(binding)
    )

    # 6b. 管理员删除 STPRO 原生配置文件 → 标记 missing/admin_managed，不自动重建
    record6b = await _create_profile(plugin, "档案配置删除")
    config6b = record6b.astrbot_config_id
    ctx.native_configs.pop(config6b, None)
    ctx.config_names.pop(config6b, None)
    ctx.astrbot_config_mgr.abconf_data.pop(config6b, None)
    await plugin.reconciler.reconcile()
    refreshed6b = await plugin.store.get_profile(record6b.profile_id)
    check(
        "17-6b 原生配置删除后标记 missing",
        refreshed6b is not None and refreshed6b.config_status == "missing",
    )
    check(
        "17-6b 原生配置删除后管理员优先",
        refreshed6b is not None and refreshed6b.admin_managed,
    )
    check("17-6b 未自动重建原生配置", config6b not in ctx.native_configs)

    # 7. 插件自身写入触发 Hook，内部标记匹配 → 不误判
    record7 = await _create_profile(plugin, "档案八")
    await plugin.profile_service.select_model(record7, "gpt-4o")
    external_hits: list[str] = []

    async def _record(umo_: str, provider_id: str) -> None:
        external_hits.append(umo_)

    plugin.bridge.on_external_rule_change = _record
    umo7 = "aiocqhttp:GroupMessage:907"
    route = await plugin.bridge.inspect_group_route(umo7)
    await plugin.bridge.bind_group(
        umo7,
        record7.provider_id,
        expected_rule=route.rule_provider_id,
        expected_config_id=route.config_id,
        expected_default_provider=route.default_chat_provider_id,
    )
    await asyncio.sleep(0)
    check("17-7 内部写入不误判为管理员操作", external_hits == [], str(external_hits))

    # 8. 窗口内但值不匹配 → 判为外部
    plugin.bridge._add_marker("group_rule", umo7, "expected-old", "expected-new")
    matched = plugin.bridge.consume_marker("group_rule", umo7, "totally-different")
    check("17-8 值不匹配判为外部变化", not matched)

    # 9. 重载时对账不覆盖管理员值
    umo9 = "aiocqhttp:GroupMessage:909"
    record9 = await _create_profile(plugin, "档案九")
    await plugin.profile_service.select_model(record9, "gpt-4o")
    await plugin.binding_service.bind("aiocqhttp:10001", record9, umo9)
    await SP.session_put(umo9, CHAT_RULE_KEY, "provider_admin_final")
    plugin.store._data = None
    await plugin.terminate()

    plugin_reload = make_plugin(ctx)
    await plugin_reload.initialize()
    check(
        "17-9 重载对账不覆盖管理员值",
        await SP.get_async("umo", umo9, CHAT_RULE_KEY, None) == "provider_admin_final",
    )

    # 10. 冲突消失后不自动恢复，必须重新 bind
    await SP.session_remove(umo9, CHAT_RULE_KEY)
    await plugin_reload.reconciler.reconcile()
    binding = await plugin_reload.store.get_binding(umo9)
    check(
        "17-10 冲突消失后仍为 admin_overridden",
        binding is not None and binding.state == "admin_overridden",
        str(binding),
    )
    check(
        "17-10 未自动写回规则",
        await SP.get_async("umo", umo9, CHAT_RULE_KEY, None) is None,
    )
    plugin_reload._pending_self_id = "99999"
    outcome = await plugin_reload.binding_service.bind("aiocqhttp:10001", record9, umo9)
    check("17-10 重新 bind 才恢复", outcome.ok, outcome.message)
    plugin_reload.store._data = None
    await plugin_reload.terminate()


# --------------------------- 18 安全、日志与隐私 ---------------------------


async def accept_security() -> None:
    print("\n[18] 安全、日志与隐私")
    plugin, ctx = await prepare()

    # 异常消息里回显 Key 时，日志不得出现完整 Key
    KEY = "sk-echoback1234567890"

    class EchoClient:
        async def fetch_models(self, endpoint, api_key):
            raise RuntimeError(f"upstream says invalid key {api_key}")

        async def probe(self, endpoint, api_key, model):
            raise RuntimeError(f"upstream says invalid key {api_key}")

    plugin.profile_service.client = EchoClient()
    await drain(plugin.cmd_new(FakeEvent("/stpro new 泄露测试"), "泄露测试"))
    await drain(plugin.on_private_message(FakeEvent("https://api.example.com/v1")))
    await drain(plugin.on_private_message(FakeEvent(KEY)))
    for t in list(plugin._remote_tasks.values()):
        await t
    await asyncio.sleep(0)
    blob = "\n".join(msg for _, msg in LOG.records)
    check("18 日志不含完整 Key", KEY not in blob, blob[:300])
    check(
        "18 日志不含 Key 明文片段",
        "echoback1234567890" not in blob,
        blob[:300],
    )

    # 恢复客户端，后续用例需要正常的远程行为
    plugin.profile_service.client = plugin.client
    plugin.monitor.client = plugin.client

    # 越权：大小写 / Unicode / UUID 猜测
    record = await _create_profile(plugin, "MyProfile")
    other = "aiocqhttp:20002"
    check(
        "18 大小写越权失败",
        await plugin.profile_service.find_by_name(other, "myprofile") is None,
    )
    check(
        "18 Unicode 越权失败",
        await plugin.profile_service.find_by_name(other, "ＭｙＰｒｏｆｉｌｅ") is None,
    )
    check(
        "18 UUID 猜测越权失败", await plugin.store.get_profile("stpro_notexist") is None
    )

    # 非所有者不能操作他人 Profile
    try:
        plugin.profile_service.assert_owner(record, other)
        denied = False
    except PermissionDenied:
        denied = True
    check("18 非所有者被拒绝", denied)

    # 孤立 stpro_ Provider（不在所有权记录中）不得被操作
    await ctx.provider_manager.create_provider(
        {
            "id": "stpro_orphan",
            "type": "openai_chat_completion",
            "provider_type": "chat_completion",
            "enable": True,
            "key": ["sk-orphan"],
            "api_base": "https://orphan.example.com/v1",
            "model": "m",
        },
    )
    plugin.bridge.store = plugin.store
    try:
        await plugin.bridge.delete_owned("stpro_orphan")
        denied_orphan = False
    except PermissionError:
        denied_orphan = True
    check("18 孤立 stpro_ Provider 拒绝操作", denied_orphan)
    check(
        "18 孤立 Provider 未被删除",
        ctx.provider_manager.get_provider_config_by_id("stpro_orphan") is not None,
    )

    # 非 stpro_ Provider 不得被修改
    try:
        await plugin.bridge.delete_owned("provider_A")
        denied_foreign = False
    except PermissionError:
        denied_foreign = True
    check("18 非 stpro_ Provider 拒绝操作", denied_foreign)

    # JSON 损坏：只读诊断，不覆盖、不删 Provider
    record_keep = await _create_profile(plugin, "保留档案")
    data_file = plugin.store.path
    plugin.store._data = None
    await plugin.terminate()

    data_file.write_text("{ 这不是合法 JSON", encoding="utf-8")
    broken = make_plugin(ctx)
    await broken.initialize()
    check(
        "18 损坏时进入只读诊断",
        broken.store.readonly_reason is not None,
        str(broken.store.readonly_reason),
    )
    from astrbot_plugin_standalone_profile.storage.ownership_store import StoreReadOnly

    try:
        await broken.store.transaction(lambda d: None)
        readonly = False
    except StoreReadOnly:
        readonly = True
    check("18 损坏时拒绝写入", readonly)
    check(
        "18 损坏时不删除 Provider",
        ctx.provider_manager.get_provider_config_by_id(record_keep.provider_id)
        is not None,
    )
    check("18 损坏时备份/原文件保留", data_file.exists())

    await broken.terminate()


# --------------------------- 工具 ---------------------------


async def _create_profile(plugin: StproPlugin, name: str):
    record, _ = await plugin.profile_service.create_profile(
        "aiocqhttp:10001",
        "aiocqhttp:FriendMessage:10001",
        name,
        "https://api.example.com/v1",
        "sk-abcdefgh12345678",
    )
    return record


NOT_COVERED = [
    "真机验证 STPRO 原生配置文件在 WebUI 可见",
    "真机验证“为特定会话选择配置”显示 UMO → STPRO 配置",
    "真机验证创建接口已热装配 PipelineScheduler",
    "并发死锁压力测试（18 节）",
]


async def main() -> int:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    for case in (
        accept_new,
        accept_set,
        accept_model,
        accept_bind,
        accept_remove,
        accept_monitor,
        accept_admin_priority,
        accept_security,
    ):
        try:
            await case()
        except Exception as exc:
            import traceback

            traceback.print_exc()
            check(f"{case.__name__} 执行", False, f"{type(exc).__name__}: {exc}")

    print("\n未在离线验收覆盖、需真机探针：")
    for item in NOT_COVERED:
        print(f"  - {item}")
    return 1 if run_tests.FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
