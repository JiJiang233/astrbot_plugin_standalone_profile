"""STPRO 插件的离线自测。

不需要 AstrBot 运行时，也不需要网络。运行方式：

    python3 data/plugins/astrbot_plugin_standalone_profile/tests/run_tests.py

覆盖两类路径：
- 命令层（`cmd_*` / 事件 handler）：这里最容易出 await/生成器用错的问题；
- 服务层（bind/unbind/对账/让位）：验证"管理员优先"与"比较后写入"。
"""

from __future__ import annotations

import asyncio
import copy
import fnmatch
import inspect
import json
import os
import shutil
import sys
import uuid
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PLUGIN_ROOT / "tests"))
sys.path.insert(0, str(PLUGIN_ROOT.parent))

DATA_DIR = Path(
    os.environ.get(
        "STPRO_TEST_DATA_DIR",
        r"D:\Codes\python\astrbot\cache\stpro_test_data",
    ),
)

import _astrbot_stub  # noqa: E402

_astrbot_stub.build(str(DATA_DIR / "plugin_data"))

from astrbot.core.platform.message_type import MessageType  # noqa: E402
from astrbot_plugin_standalone_profile.main import StproPlugin  # noqa: E402
from astrbot_plugin_standalone_profile.utils.errors import StproError  # noqa: E402

SP = _astrbot_stub.sp
CHAT_RULE_KEY = "provider_perf_chat_completion"


# --------------------------- 假对象 ---------------------------


class Result:
    def __init__(self, text):
        self.text = text


class FakeEvent:
    def __init__(
        self,
        text,
        *,
        private=True,
        group_id=None,
        from_self=False,
        sent_event=False,
    ):
        self._text = text
        self._private = private
        self._group_id = group_id
        self._from_self = from_self
        self.stopped = False
        self.unified_msg_origin = (
            "aiocqhttp:FriendMessage:10001"
            if private
            else f"aiocqhttp:GroupMessage:{group_id}"
        )
        raw = {"post_type": "message_sent"} if sent_event else {}
        if sent_event:
            raw["self_id"] = 99999
            raw["user_id"] = 10001  # message_sent 里 user_id 是接收方
            raw["sender"] = {"user_id": 99999}  # sender 才是机器人自己
        self.message_obj = type("M", (), {"raw_message": raw})()

    def plain_result(self, text):
        return Result(text)

    def stop_event(self):
        self.stopped = True

    def get_message_str(self):
        return self._text

    def get_platform_id(self):
        return "aiocqhttp"

    def get_sender_id(self):
        return "99999" if self._from_self else "10001"

    def get_self_id(self):
        return "99999"

    def get_group_id(self):
        return self._group_id

    def get_message_type(self):
        return (
            MessageType.FRIEND_MESSAGE if self._private else MessageType.GROUP_MESSAGE
        )


class FakeBot:
    async def call_action(self, action, **kwargs):
        if action == "get_group_member_list":
            return [{"user_id": 10001}, {"user_id": 99999}]
        raise ValueError(action)


class FakePlatform:
    def __init__(self):
        self.bot = FakeBot()


class FakePersonaManager:
    def __init__(self):
        self.personas: dict[str, object] = {}

    async def create_persona(self, persona_id, system_prompt, **kwargs):
        if persona_id in self.personas:
            raise ValueError("persona exists")
        persona = type(
            "Persona",
            (),
            {"persona_id": persona_id, "system_prompt": system_prompt},
        )()
        self.personas[persona_id] = persona
        return persona

    async def update_persona(self, persona_id, system_prompt=None, **kwargs):
        persona = self.personas[persona_id]
        if system_prompt is not None:
            persona.system_prompt = system_prompt
        return persona

    async def delete_persona(self, persona_id):
        if persona_id not in self.personas:
            raise ValueError("persona missing")
        self.personas.pop(persona_id)

    async def get_persona(self, persona_id):
        return self.personas[persona_id]

    async def get_all_personas(self):
        return list(self.personas.values())


class FakeProviderManager:
    def __init__(self):
        self.providers_config: list[dict] = []
        self.inst_map: dict = {}
        self.hooks: list = []

    async def create_provider(self, config):
        self.providers_config.append(dict(config))
        if config.get("enable"):
            self.inst_map[config["id"]] = object()

    async def update_provider(self, origin_id, new_config):
        self.providers_config = [
            new_config if p["id"] == origin_id else p for p in self.providers_config
        ]
        self.inst_map.pop(origin_id, None)
        if new_config.get("enable") and new_config.get("model"):
            self.inst_map[new_config["id"]] = object()

    async def delete_provider(self, provider_id=None, provider_source_id=None):
        self.providers_config = [
            p for p in self.providers_config if p["id"] != provider_id
        ]
        self.inst_map.pop(provider_id, None)

    def get_provider_config_by_id(self, provider_id, merged=False):
        for p in self.providers_config:
            if p["id"] == provider_id:
                return json.loads(json.dumps(p))
        return None

    async def set_provider(self, provider_id, provider_type, umo=None):
        if provider_id not in self.inst_map:
            raise ValueError("Provider does not exist")
        await SP.session_put(umo, f"provider_perf_{provider_type.value}", provider_id)
        for hook in self.hooks:
            hook(provider_id, provider_type, umo)

    def register_provider_change_hook(self, hook):
        self.hooks.append(hook)


class FakeContext:
    def __init__(self):
        self.provider_manager = FakeProviderManager()
        self.persona_manager = FakePersonaManager()
        self.default_config = {
            "admins_id": [],
            "persona": {"default": "persona_keep"},
            "knowledge_base": {"enabled": True},
            "plugins": {"disabled": ["keep_me"]},
            "agent_runner": {
                "runner_type": "local",
                "config": {
                    "model": {"provider_id": "provider_A"},
                    "persona": {"persona_id": "default"},
                },
            },
        }
        self.native_configs = {"default": copy.deepcopy(self.default_config)}
        self.config_names: dict[str, str] = {}
        self.config_routes: dict[str, str] = {}
        self.runtime_schedulers: set[str] = {"default"}
        self.astrbot_config_mgr = type("M", (), {})()
        self.astrbot_config_mgr.default_conf = self.native_configs["default"]
        self.astrbot_config_mgr.confs = self.native_configs
        self.astrbot_config_mgr.abconf_data = {}
        self.astrbot_config_mgr.ucr = type(
            "Ucr",
            (),
            {"umop_to_conf_id": self.config_routes},
        )()
        self.astrbot_config_mgr.get_conf_info = self._get_conf_info
        self.astrbot_config_mgr.get_conf_list = self._get_conf_list
        self.astrbot_config_mgr.create_conf = self._create_conf
        self.astrbot_config_mgr.delete_conf = self._delete_conf
        self.astrbot_config_mgr.ucr.update_route = self._update_route
        self.astrbot_config_mgr.ucr.delete_route = self._delete_route
        self.create_config_profile = self._create_config_profile
        self.delete_config_profile = self._delete_config_profile
        self.update_config_profile = self._update_config_profile
        self.set_config_route = self._set_config_route
        self.delete_config_route = self._delete_route
        self.sent: list[tuple[str, str]] = []

    def _get_conf_info(self, umo):
        config_id = "default"
        target = umo.split(":", 2)
        if len(target) == 3:
            for pattern, candidate in self.config_routes.items():
                parts = pattern.split(":", 2)
                if len(parts) == 3 and all(
                    part == "" or fnmatch.fnmatchcase(value, part)
                    for part, value in zip(parts, target)
                ):
                    config_id = candidate
                    break
        if config_id not in self.native_configs:
            config_id = "default"
        return {"id": config_id, "name": self.config_names.get(config_id, config_id)}

    def _get_conf_list(self):
        return [
            {"id": config_id, "name": self.config_names.get(config_id, config_id)}
            for config_id in self.native_configs
        ]

    async def _create_conf(self, *, name, config):
        config_id = str(uuid.uuid4())
        self.native_configs[config_id] = copy.deepcopy(config)
        self.config_names[config_id] = name or config_id
        self.astrbot_config_mgr.abconf_data[config_id] = {
            "name": self.config_names[config_id],
            "path": f"abconf_{config_id}.json",
        }
        return config_id

    async def _delete_conf(self, config_id):
        if config_id not in self.native_configs or config_id == "default":
            return False
        self.native_configs.pop(config_id, None)
        self.config_names.pop(config_id, None)
        self.astrbot_config_mgr.abconf_data.pop(config_id, None)
        return True

    async def _create_config_profile(self, *, name, config):
        config_id = await self._create_conf(name=name, config=config)
        self.runtime_schedulers.add(config_id)
        return config_id

    async def _delete_config_profile(self, config_id):
        deleted = await self._delete_conf(config_id)
        if deleted:
            self.runtime_schedulers.discard(config_id)
        return deleted

    async def _update_config_profile(self, config_id, config):
        if config_id not in self.native_configs:
            raise ValueError("config missing")
        self.native_configs[config_id] = copy.deepcopy(config)

    async def _update_route(self, umo, config_id):
        self.config_routes[umo] = config_id

    async def _set_config_route(self, umo, config_id):
        if config_id == "default":
            await self._delete_route(umo)
            return
        if config_id not in self.native_configs:
            raise ValueError(f"配置文件 {config_id} 不存在")
        await self._update_route(umo, config_id)

    async def _delete_route(self, umo):
        self.config_routes.pop(umo, None)

    def get_platform_inst(self, platform_id):
        return FakePlatform()

    def get_config(self, umo=None):
        return self.native_configs[self._get_conf_info(umo or "")["id"]]

    async def send_message(self, session, chain):
        self.sent.append((str(session), chain.components[0].text))


class FakeModelClient:
    """假客户端。`models` 可在用例中修改，用于模拟"新端点不含原模型"。"""

    def __init__(self, models: list[str] | None = None) -> None:
        self.models = models if models is not None else ["gpt-4o", "gpt-4o-mini"]

    async def fetch_models(self, endpoint, api_key):
        return list(self.models)

    async def probe(self, endpoint, api_key, model):
        return 8


# --------------------------- 测试工具 ---------------------------

PASSED: list[str] = []
FAILED: list[str] = []


async def drain(agen) -> list[str]:
    out = []
    async for item in agen:
        out.append(item.text)
    return out


async def settle(plugin: StproPlugin, ctx: FakeContext) -> list[str]:
    """等待所有后台远程任务结束，返回期间主动发出的通知文本。"""
    before = len(ctx.sent)
    for _ in range(20):
        tasks = [t for t in plugin._remote_tasks.values() if not t.done()]
        if not tasks:
            break
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.sleep(0)
    return [text for _, text in ctx.sent[before:]]


async def run_cmd(
    plugin: StproPlugin, ctx: FakeContext, agen
) -> tuple[list[str], list[str]]:
    """执行一条命令：拿到即时回复，并等待后台任务给出主动通知。"""
    out = await drain(agen)
    notified = await settle(plugin, ctx)
    return out, notified


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  ✓ {name}")
    else:
        FAILED.append(f"{name}: {detail}")
        print(f"  ✗ {name} —— {detail}")


# --------------------------- 用例 ---------------------------


async def new_plugin() -> tuple[StproPlugin, FakeContext]:
    ctx = FakeContext()
    plugin = StproPlugin(ctx, {"monitor": {"enable": False}})
    plugin.client = FakeModelClient()
    plugin.profile_service.client = plugin.client
    plugin.monitor.client = plugin.client
    await plugin.initialize()
    return plugin, ctx


async def test_self_message_ignored() -> None:
    """机器人自己的消息不能再被当成用户输入。"""
    print("\n[1] 机器人自身消息")
    plugin, _ = await new_plugin()

    await drain(plugin.cmd_new(FakeEvent("/stpro new 公司接口"), "公司接口"))
    state = await plugin.interaction.get("aiocqhttp:10001")
    check(
        "cmd_new 进入 awaiting_endpoint",
        state is not None and state.step == "awaiting_endpoint",
    )

    # 机器人自己的回复被平台回显（sender_id == self_id）
    out = await drain(
        plugin.on_private_message(
            FakeEvent("开始创建「公司接口」。请发送 Endpoint：", from_self=True),
        ),
    )
    check("自身消息（sender=self）被忽略", out == [], f"不应有输出，实际 {out}")
    state = await plugin.interaction.get("aiocqhttp:10001")
    check("流程未被推进", state is not None and state.step == "awaiting_endpoint")

    # OneBot message_sent 事件（user_id 是接收方，只能靠 post_type 判断）
    out = await drain(
        plugin.on_private_message(
            FakeEvent("https://evil.example.com/v1", sent_event=True)
        ),
    )
    check("message_sent 事件被忽略", out == [], f"不应有输出，实际 {out}")
    state = await plugin.interaction.get("aiocqhttp:10001")
    check("流程仍未推进", state is not None and state.step == "awaiting_endpoint")

    # 回声未被 self 判断拦住时，格式门卫必须挡住它
    echo = FakeEvent("Endpoint 已记录，请发送 API Key：")
    out = await drain(plugin.on_private_message(echo))
    check("提示语回声不触发格式报错", out == [], f"不应有输出，实际 {out}")
    echo2 = FakeEvent("好的，我已经帮你记下了，请继续。")
    out = await drain(plugin.on_private_message(echo2))
    check("闲聊回声不触发格式报错", out == [], f"不应有输出，实际 {out}")

    # 真正输错格式（少了 https://）仍然要给提示
    out = await drain(plugin.on_private_message(FakeEvent("api.example.com/v1")))
    check(
        "漏写 https 时给出格式提示",
        bool(out) and "Endpoint 格式不正确" in out[0],
        str(out),
    )

    # 也不会触发命令
    out = await drain(
        plugin.cmd_new(FakeEvent("/stpro new 另一个", from_self=True), "另一个")
    )
    check("自身消息不触发命令", out == [])

    # 非标准实现：post_type 仍是 message，但 raw sender 是机器人自己
    spoof = FakeEvent("https://evil.example.com/v1")
    spoof.message_obj = type(
        "M",
        (),
        {
            "raw_message": {
                "post_type": "message",
                "self_id": 99999,
                "sender": {"user_id": 99999},
            }
        },
    )()
    out = await drain(plugin.on_private_message(spoof))
    check("raw sender 为自身的消息被忽略", out == [], f"不应有输出，实际 {out}")

    # 真实用户输入仍然有效
    out = await drain(
        plugin.on_private_message(FakeEvent("https://api.example.com/v1")),
    )
    check("真实 Endpoint 被接受", bool(out) and "API Key" in out[0], str(out))

    plugin.store._data = None
    await plugin.terminate()


async def test_command_signatures() -> None:
    """命令参数必须留在函数签名中，供 AstrBot 生成帮助并解析参数。"""
    print("\n[0] 命令参数签名")
    expected = {
        "cmd_new": ("event", "name"),
        "cmd_set": ("event", "name", "field"),
        "cmd_model": ("event", "name", "choice"),
        "cmd_list": ("event", "name"),
        "cmd_remove": ("event", "name"),
        "cmd_bind": ("event", "name", "group_id"),
        "cmd_unbind": ("event", "group_id"),
        "cmd_update": ("event", "name"),
    }
    actual = {
        name: tuple(inspect.signature(getattr(StproPlugin, name)).parameters)[1:]
        for name in expected
    }
    check("八个子命令使用 AstrBot 原生参数签名", actual == expected, str(actual))


async def test_full_flow() -> None:
    """new → model → bind → list → remove 的主流程。"""
    print("\n[2] 主流程")
    plugin, ctx = await new_plugin()

    await run_cmd(
        plugin, ctx, plugin.cmd_new(FakeEvent("/stpro new 公司接口"), "公司接口")
    )
    out, _ = await run_cmd(
        plugin,
        ctx,
        plugin.on_private_message(FakeEvent("https://api.example.com/v1")),
    )
    out, notified = await run_cmd(
        plugin,
        ctx,
        plugin.on_private_message(FakeEvent("sk-abcdefgh12345678")),
    )
    check("命令立即返回、不等待网络", bool(out) and "正在验证" in out[0], str(out))
    result = " ".join(notified)
    check("后台完成后通知模型列表", "gpt-4o" in result, str(notified))
    check("输出不含完整 Key", "abcdefgh12345678" not in result)
    record = (await plugin.store.list_profiles("aiocqhttp:10001"))[0]
    native = ctx.native_configs[record.astrbot_config_id]
    conf_list = ctx.astrbot_config_mgr.get_conf_list()
    check(
        "新建配置在 WebUI 配置列表可见",
        any(
            item["id"] == record.astrbot_config_id
            and item["name"] == "STPRO / 10001 / 公司接口"
            for item in conf_list
        ),
        str(conf_list),
    )
    check("新配置保留 default 人格", native["persona"] == ctx.default_config["persona"])
    check(
        "新配置保留 default 知识库",
        native["knowledge_base"] == ctx.default_config["knowledge_base"],
    )
    check(
        "新配置保留 default 插件设置",
        native["plugins"] == ctx.default_config["plugins"],
    )
    check(
        "新配置默认 Provider 指向 STPRO Provider",
        native["agent_runner"]["config"]["model"]["provider_id"] == record.provider_id,
    )
    check(
        "创建接口已热装配调度器",
        record.astrbot_config_id in ctx.runtime_schedulers,
    )

    _, notified = await run_cmd(
        plugin, ctx, plugin.cmd_model(FakeEvent("/stpro model 公司接口"), "公司接口")
    )
    check("模型列表带编号", "1. gpt-4o" in " ".join(notified), str(notified))

    out, _ = await run_cmd(
        plugin,
        ctx,
        plugin.cmd_model(FakeEvent("/stpro model 公司接口 1"), "公司接口", "1"),
    )
    check("选择模型并启用", bool(out) and "gpt-4o" in out[0], str(out))

    # 也可用模型名称选择
    out, _ = await run_cmd(
        plugin,
        ctx,
        plugin.cmd_model(
            FakeEvent("/stpro model 公司接口 gpt-4o-mini"), "公司接口", "gpt-4o-mini"
        ),
    )
    check("按模型名称选择成功", bool(out) and "gpt-4o-mini" in out[0], str(out))

    # 名称必须来自最近一次列表，不能塞入任意模型 ID
    out, _ = await run_cmd(
        plugin,
        ctx,
        plugin.cmd_model(
            FakeEvent("/stpro model 公司接口 evil-model"), "公司接口", "evil-model"
        ),
    )
    check("列表外的模型名称被拒绝", bool(out) and "没有找到" in out[0], str(out))

    out, _ = await run_cmd(
        plugin, ctx, plugin.cmd_list(FakeEvent("/stpro list 公司接口"), "公司接口")
    )
    check("摘要已脱敏", bool(out) and "abcdefgh12345678" not in out[0], str(out))

    plugin._pending_self_id = "99999"
    out, _ = await run_cmd(
        plugin,
        ctx,
        plugin.cmd_bind(
            FakeEvent("/stpro bind 公司接口", private=False, group_id="123456"),
            "公司接口",
        ),
    )
    check("群内绑定成功", bool(out) and "已将本群绑定" in out[0], str(out))

    out, _ = await run_cmd(
        plugin, ctx, plugin.cmd_remove(FakeEvent("/stpro remove 公司接口"), "公司接口")
    )
    check("remove 要求确认码", bool(out) and "确认码" in out[0], str(out))
    token = out[0].strip().splitlines()[-1]

    out, _ = await run_cmd(
        plugin, ctx, plugin.on_private_message(FakeEvent("随便说点什么"))
    )
    check("普通聊天不被当成确认", out == [], f"不应有输出，实际 {out}")
    check("Provider 仍然存在", len(ctx.provider_manager.providers_config) == 1)

    out, _ = await run_cmd(
        plugin,
        ctx,
        plugin.on_private_message(FakeEvent("1234567890abcdef")),
    )
    check("错误的确认码被拒绝", bool(out) and "取消" in out[0], str(out))
    check("Provider 仍存在", len(ctx.provider_manager.providers_config) == 1)

    out, _ = await run_cmd(
        plugin, ctx, plugin.cmd_remove(FakeEvent("/stpro remove 公司接口"), "公司接口")
    )
    token = out[0].strip().splitlines()[-1]
    out, _ = await run_cmd(plugin, ctx, plugin.on_private_message(FakeEvent(token)))
    check("正确确认码完成删除", bool(out) and "已删除" in out[0], str(out))
    check("Provider 已被删除", ctx.provider_manager.providers_config == [])

    plugin.store._data = None
    await plugin.terminate()


async def test_alignment_config() -> None:
    """新档案复制插件设置指定的原生配置，而不是固定复制 default。"""
    print("\n[2.1] 对齐默认配置")
    ctx = FakeContext()
    aligned = copy.deepcopy(ctx.default_config)
    aligned["persona"] = {"default": "aligned_persona_marker"}
    aligned["knowledge_base"] = {"enabled": False, "ids": ["aligned-kb"]}
    aligned["plugins"] = {"disabled": ["aligned_plugin"]}
    aligned["agent_runner"]["config"]["persona"]["persona_id"] = "aligned-persona"
    ctx.native_configs["aligned"] = aligned
    ctx.config_names["aligned"] = "用于新档案的基准"
    ctx.astrbot_config_mgr.abconf_data["aligned"] = {
        "name": "用于新档案的基准",
        "path": "abconf_aligned.json",
    }

    plugin = StproPlugin(
        ctx,
        {
            "monitor": {"enable": False},
            "alignment_config_id": "aligned",
        },
    )
    plugin.client = FakeModelClient()
    plugin.profile_service.client = plugin.client
    plugin.monitor.client = plugin.client
    await plugin.initialize()
    record, _ = await plugin.profile_service.create_profile(
        "aiocqhttp:10001",
        "aiocqhttp:FriendMessage:10001",
        "对齐测试",
        "https://api.example.com/v1",
        "sk-alignment-test",
    )
    native = ctx.native_configs[record.astrbot_config_id]
    check(
        "新配置复制指定配置的人格字段",
        native["persona"] == aligned["persona"],
    )
    check(
        "新配置复制指定配置的知识库",
        native["knowledge_base"] == aligned["knowledge_base"],
    )
    check(
        "新配置复制指定配置的插件设置",
        native["plugins"] == aligned["plugins"],
    )
    check(
        "新配置仍替换为自己的 STPRO Provider",
        native["agent_runner"]["config"]["model"]["provider_id"] == record.provider_id,
    )
    check("档案记录对齐配置 ID", record.alignment_config_id == "aligned")
    check(
        "档案记录对齐配置人格",
        record.alignment_persona_id == "aligned-persona",
    )
    personas = await plugin.persona_service.list_personas(record)
    check(
        "人格回退使用档案创建时的对齐人格",
        personas[0].persona_id == "aligned-persona"
        and personas[0].source == "对齐配置",
    )
    plugin.store._data = None
    await plugin.terminate()

    bad_ctx = FakeContext()
    bad_plugin = StproPlugin(
        bad_ctx,
        {
            "monitor": {"enable": False},
            "alignment_config_id": "missing-config",
        },
    )
    bad_plugin.client = FakeModelClient()
    bad_plugin.profile_service.client = bad_plugin.client
    bad_plugin.monitor.client = bad_plugin.client
    await bad_plugin.initialize()
    error = None
    try:
        await bad_plugin.profile_service.create_profile(
            "aiocqhttp:10001",
            "aiocqhttp:FriendMessage:10001",
            "无效对齐",
            "https://api.example.com/v1",
            "sk-alignment-test",
        )
    except StproError as exc:
        error = str(exc)
    check(
        "不存在的对齐配置会明确拒绝创建",
        bool(error) and "missing-config" in error,
        str(error),
    )
    check(
        "对齐配置无效时不会创建 Provider",
        bad_ctx.provider_manager.providers_config == [],
    )
    bad_plugin.store._data = None
    await bad_plugin.terminate()


async def test_admin_priority() -> None:
    """管理员接管：插件必须让位且不改回。"""
    print("\n[3] 管理员优先")
    plugin, ctx = await new_plugin()
    user_key = "aiocqhttp:10001"
    group_umo = "aiocqhttp:GroupMessage:123456"

    record, _ = await plugin.profile_service.create_profile(
        user_key,
        "aiocqhttp:FriendMessage:10001",
        "公司接口",
        "https://api.example.com/v1",
        "sk-abcdefgh12345678",
    )
    check(
        "新建 Provider 未启用且无模型",
        ctx.provider_manager.providers_config[0]["enable"] is False,
    )

    outcome = await plugin.binding_service.bind(user_key, record, group_umo)
    check("未选模型时拒绝绑定", not outcome.ok, outcome.message)

    await plugin.profile_service.select_model(record, "gpt-4o")
    plugin._pending_self_id = "99999"
    outcome = await plugin.binding_service.bind(user_key, record, group_umo)
    check("选模型后绑定成功", outcome.ok, outcome.message)
    check(
        "群路由写入 STPRO 配置文件",
        ctx.config_routes.get(group_umo) == record.astrbot_config_id,
    )

    # 管理员把本群的精确配置路由改成其他配置文件
    ctx.native_configs["admin_conf"] = copy.deepcopy(ctx.default_config)
    ctx.config_names["admin_conf"] = "管理员配置"
    ctx.astrbot_config_mgr.abconf_data["admin_conf"] = {
        "name": "管理员配置",
        "path": "abconf_admin.json",
    }
    ctx.config_routes[group_umo] = "admin_conf"
    await plugin.reconciler.reconcile()
    binding = await plugin.store.get_binding(group_umo)
    check(
        "绑定标记为 admin_overridden",
        binding is not None and binding.state == "admin_overridden",
    )
    check(
        "管理员的值未被改回",
        ctx.config_routes.get(group_umo) == "admin_conf",
    )

    outcome = await plugin.binding_service.bind(user_key, record, group_umo)
    check("冲突存在时拒绝重绑", not outcome.ok, outcome.message)

    ctx.config_routes[group_umo] = record.astrbot_config_id
    outcome = await plugin.binding_service.bind(user_key, record, group_umo)
    check("冲突消失后可重绑", outcome.ok, outcome.message)

    outcome = await plugin.binding_service.unbind(user_key, group_umo)
    check("正常解绑成功", outcome.ok, outcome.message)
    check(
        "解绑后 STPRO 精确路由被删除",
        group_umo not in ctx.config_routes,
    )

    plugin.store._data = None
    await plugin.terminate()


# --------------------------- 入口 ---------------------------


async def test_masking() -> None:
    """第 18 节：对常见长度、极短、无 `sk-` 前缀的 Key 做掩码测试。"""
    print("\n[4] Key 脱敏与数据最小化")
    from astrbot_plugin_standalone_profile.utils.masking import mask_secret

    cases = [
        ("sk-abcdefgh12345678", "sk-"),  # 常见长度
        ("sk-abcdefghijklmnopqrstuvwxyz0123456789", "sk-"),  # 超长
        ("sk-ab", ""),  # 极短
        ("a", ""),  # 单字符
        ("ab", ""),  # 极短无前缀
        ("", ""),  # 空
        (None, ""),  # None
        ("1234567890abcdef", ""),  # 无 sk- 前缀
    ]
    for secret, must_start in cases:
        masked = mask_secret(secret)
        ok = bool(masked) and (not must_start or masked.startswith(must_start))
        # 掩码不得暴露原始值的大部分内容
        if secret and len(secret) >= 8:
            ok = ok and secret[3:-4] not in masked
        check(
            f"掩码 {secret!r} -> {masked!r}",
            ok if secret else masked == "",
            "掩码结果异常",
        )

    # 原始 Key 不能出现在掩码结果里
    long_key = "sk-abcdefgh12345678"
    check(
        "掩码不泄露中间段",
        "abcdefgh1234" not in mask_secret(long_key),
        mask_secret(long_key),
    )

    # 数据最小化：插件 JSON 里不得出现 API Key
    plugin, ctx = await new_plugin()
    record, _ = await plugin.profile_service.create_profile(
        "aiocqhttp:10001",
        "aiocqhttp:FriendMessage:10001",
        "最小化",
        "https://api.example.com/v1",
        "sk-supersecretkey123456",
    )
    raw = json.loads((plugin.store.path).read_text(encoding="utf-8"))
    blob = json.dumps(raw, ensure_ascii=False)
    check("插件 JSON 不含 API Key", "supersecretkey" not in blob, blob[:200])
    check("插件 JSON 含 Provider ID", record.provider_id in blob)
    plugin.store._data = None
    await plugin.terminate()


async def test_endpoint_normalize() -> None:
    """Endpoint 归一化：自动补 /v1，剥离多余的具体接口路径。"""
    print("\n[5] Endpoint 自动补全")
    from astrbot_plugin_standalone_profile.utils.endpoint import normalize_endpoint

    cases = [
        ("https://api.example.com", "https://api.example.com/v1"),
        ("https://api.example.com/", "https://api.example.com/v1"),
        ("https://api.example.com/v1", "https://api.example.com/v1"),
        ("https://api.example.com/v1/", "https://api.example.com/v1"),
        ("https://api.example.com/v1/chat/completions", "https://api.example.com/v1"),
        ("https://api.example.com/chat/completions", "https://api.example.com/v1"),
        ("https://api.example.com/models", "https://api.example.com/v1"),
        ("https://api.example.com/v4", "https://api.example.com/v4"),
        ("https://api.example.com/v1beta", "https://api.example.com/v1beta"),
        ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ("https://api.example.com/openai/v1", "https://api.example.com/openai/v1"),
    ]
    for raw, expected in cases:
        got = normalize_endpoint(raw)
        check(f"补全 {raw} -> {got}", got == expected, f"期望 {expected}")

    # 归一化结果会真正用于创建 Provider
    plugin, ctx = await new_plugin()
    await drain(plugin.cmd_new(FakeEvent("/stpro new 补全测试"), "补全测试"))
    out = await drain(plugin.on_private_message(FakeEvent("https://api.example.com")))
    check("输入无 /v1 时提示已补全", bool(out) and "已自动补全" in out[0], str(out))
    state = await plugin.interaction.get("aiocqhttp:10001")
    check(
        "候选 Endpoint 已补全 /v1",
        state is not None and state.candidate_endpoint.endswith("/v1"),
        str(state and state.candidate_endpoint),
    )
    plugin.store._data = None
    await plugin.terminate()


async def test_choose_right_after_new() -> None:
    """`new` 成功后必须能照提示直接选择，不能被"缓存已清除"挡住。"""
    print("\n[6] 新建后直接选择模型")
    plugin, ctx = await new_plugin()

    await run_cmd(plugin, ctx, plugin.cmd_new(FakeEvent("/stpro new 直接选"), "直接选"))
    await run_cmd(
        plugin,
        ctx,
        plugin.on_private_message(FakeEvent("https://api.example.com/v1")),
    )
    _, notified = await run_cmd(
        plugin,
        ctx,
        plugin.on_private_message(FakeEvent("sk-abcdefgh12345678")),
    )
    check(
        "new 的提示包含序号或名称",
        any("模型序号或名称" in t for t in notified),
        str(notified),
    )

    out, _ = await run_cmd(
        plugin, ctx, plugin.cmd_model(FakeEvent("/stpro model 直接选 1"), "直接选", "1")
    )
    check("new 后可直接按序号选择", bool(out) and "gpt-4o" in out[0], str(out))

    out, _ = await run_cmd(
        plugin,
        ctx,
        plugin.cmd_model(
            FakeEvent("/stpro model 直接选 gpt-4o-mini"), "直接选", "gpt-4o-mini"
        ),
    )
    check("选择后仍可按名称改选", bool(out) and "gpt-4o-mini" in out[0], str(out))

    plugin.store._data = None
    await plugin.terminate()


async def test_set_modes_and_route_safety() -> None:
    """set 三种模式、通配路由抢占与旧精确路由恢复。"""
    print("\n[7] set 模式与配置路由安全")
    plugin, ctx = await new_plugin()
    record, _ = await plugin.profile_service.create_profile(
        "aiocqhttp:10001",
        "aiocqhttp:FriendMessage:10001",
        "路由测试",
        "https://old.example.com/v1",
        "sk-oldsecret12345678",
    )

    out = await drain(
        plugin.cmd_set(
            FakeEvent("/stpro set 路由测试 endpoint"), "路由测试", "endpoint"
        )
    )
    state = await plugin.interaction.get("aiocqhttp:10001")
    check("set endpoint 只询问 Endpoint", bool(out) and "Endpoint" in out[0])
    check(
        "set endpoint 进入正确流程",
        state is not None and state.command == "set_endpoint",
    )
    out, notified = await run_cmd(
        plugin,
        ctx,
        plugin.on_private_message(FakeEvent("https://endpoint-only.example.com/v1")),
    )
    check(
        "set endpoint 输入后直接验证",
        bool(out) and "验证新的 Endpoint" in out[0] and bool(notified),
        f"out={out} notified={notified}",
    )
    state = await plugin.interaction.get("aiocqhttp:10001")
    check(
        "set endpoint 不等待 API Key",
        state is None or not state.expects_text(),
        str(state and state.step),
    )
    await plugin.interaction.cancel("aiocqhttp:10001")

    out = await drain(
        plugin.cmd_set(FakeEvent("/stpro set 路由测试 apikey"), "路由测试", "apikey")
    )
    state = await plugin.interaction.get("aiocqhttp:10001")
    check("set apikey 直接询问 Key", bool(out) and "API Key" in out[0])
    check(
        "set apikey 进入正确流程", state is not None and state.command == "set_apikey"
    )
    out, notified = await run_cmd(
        plugin,
        ctx,
        plugin.on_private_message(FakeEvent("sk-onlynewsecret12345678")),
    )
    check(
        "set apikey 输入后直接验证",
        bool(out) and "验证新的 API Key" in out[0] and bool(notified),
        f"out={out} notified={notified}",
    )
    await plugin.interaction.cancel("aiocqhttp:10001")

    out = await drain(
        plugin.cmd_set(FakeEvent("/stpro set 路由测试 all"), "路由测试", "all")
    )
    state = await plugin.interaction.get("aiocqhttp:10001")
    check("set all 从 Endpoint 开始", bool(out) and "Endpoint" in out[0])
    check("set all 进入正确流程", state is not None and state.command == "set_all")
    await plugin.interaction.cancel("aiocqhttp:10001")

    before = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    await plugin.profile_service.replace_credentials(
        record, "https://new.example.com/v1", None
    )
    after_endpoint = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    check("只改 Endpoint 时保留原 Key", after_endpoint["key"] == before["key"])
    await plugin.profile_service.replace_credentials(
        record, None, "sk-newsecret12345678"
    )
    after_key = ctx.provider_manager.get_provider_config_by_id(record.provider_id)
    check(
        "只改 Key 时保留当前 Endpoint",
        after_key["api_base"] == after_endpoint["api_base"],
    )

    ctx.native_configs["wildcard"] = copy.deepcopy(ctx.default_config)
    ctx.config_names["wildcard"] = "通配配置"
    ctx.astrbot_config_mgr.abconf_data["wildcard"] = {"name": "通配配置"}
    group_umo = "aiocqhttp:GroupMessage:7788"
    ctx.config_routes["aiocqhttp:GroupMessage:*"] = "wildcard"
    await plugin.profile_service.select_model(record, "gpt-4o")
    plugin._pending_self_id = "99999"
    outcome = await plugin.binding_service.bind("aiocqhttp:10001", record, group_umo)
    check(
        "通配路由抢占时拒绝绑定",
        not outcome.ok and "通配" in outcome.message,
        outcome.message,
    )
    check("通配路由抢占时回滚精确路由", group_umo not in ctx.config_routes)

    del ctx.config_routes["aiocqhttp:GroupMessage:*"]
    ctx.config_routes[group_umo] = "wildcard"
    result = await plugin.config_bridge.bind_group(
        group_umo,
        record.astrbot_config_id,
        expected_exact_config_id="wildcard",
    )
    check("可替换既有精确路由", result.applied, result.reason)
    await plugin.config_bridge.unbind_group(
        group_umo,
        record.astrbot_config_id,
        previous_exact_route_existed=True,
        previous_exact_config_id="wildcard",
    )
    check("解绑恢复绑定前精确路由", ctx.config_routes.get(group_umo) == "wildcard")

    check(
        "进程内模式不需要 WebUI API Key", not hasattr(plugin.config_bridge, "api_key")
    )

    plugin.store._data = None
    await plugin.terminate()


async def test_interaction_consumption_guards() -> None:
    """命令不被二次消费；只有真正的交互输入才阻断后续 LLM。"""
    print("\n[8] 交互消费与事件阻断")
    plugin, ctx = await new_plugin()
    user_key = "aiocqhttp:10001"
    profile, _ = await plugin.profile_service.create_profile(
        user_key,
        "aiocqhttp:FriendMessage:10001",
        "vector",
        "https://api.example.com/v1",
        "sk-abcdefgh12345678",
    )

    await drain(
        plugin.cmd_persona(
            FakeEvent("/stpro persona vector new 测试名字"),
            "vector",
            "new",
            "测试名字",
        )
    )
    command_event = FakeEvent("/stpro persona vector new 测试名字")
    out = await drain(plugin.on_private_message(command_event))
    state = await plugin.interaction.get(user_key)
    check("Persona 命令不被当作提示词", out == [])
    check(
        "命令后仍等待 Persona 提示词",
        state is not None and state.step == "awaiting_persona_prompt",
    )
    check(
        "候选 Persona 名未被命令覆盖",
        state is not None and state.candidate_persona_name == "测试名字",
    )
    check("STPRO 命令事件不由交互分支停止", not command_event.stopped)
    check("命令文本未创建 Persona", not ctx.persona_manager.personas)
    bare_command_event = FakeEvent("stpro persona vector new 测试名字")
    out = await drain(plugin.on_private_message(bare_command_event))
    state = await plugin.interaction.get(user_key)
    check(
        "无斜杠 STPRO 命令也不被二次消费", out == [] and not bare_command_event.stopped
    )
    check(
        "无斜杠命令后 Persona 状态不变",
        state is not None and state.candidate_persona_name == "测试名字",
    )

    prompt_event = FakeEvent("你是一个测试助手。")
    out = await drain(plugin.on_private_message(prompt_event))
    check("Persona 提示词创建成功", bool(out) and "已创建人格" in out[0], str(out))
    check("Persona 提示词停止后续事件", prompt_event.stopped)
    check("Persona 已写入原生管理器", len(ctx.persona_manager.personas) == 1)

    await drain(
        plugin.cmd_persona(
            FakeEvent("/stpro persona vector 测试名字"),
            "vector",
            "测试名字",
        )
    )
    edit_event = FakeEvent("以后回答都把关键句放在最后。")
    out = await drain(plugin.on_private_message(edit_event))
    check("Persona 提示词更新成功", bool(out) and "已更新人格" in out[0], str(out))
    check("更新 Persona 时停止后续事件", edit_event.stopped)

    idle_event = FakeEvent("你好")
    out = await drain(plugin.on_private_message(idle_event))
    check("无活动流程时普通聊天透传", out == [] and not idle_event.stopped)

    await drain(plugin.cmd_new(FakeEvent("/stpro new endpoint测试"), "endpoint测试"))
    endpoint_chat = FakeEvent("你好，最近过得怎么样")
    out = await drain(plugin.on_private_message(endpoint_chat))
    check("等待 Endpoint 时普通聊天透传", out == [] and not endpoint_chat.stopped)

    endpoint_event = FakeEvent("https://api.example.com/v1")
    out = await drain(plugin.on_private_message(endpoint_event))
    check("有效 Endpoint 被流程消费", bool(out) and "API Key" in out[0], str(out))
    check("Endpoint 输入停止后续事件", endpoint_event.stopped)

    key_event = FakeEvent("sk-endpointtest12345678")
    out, _ = await run_cmd(plugin, ctx, plugin.on_private_message(key_event))
    check("API Key 被流程消费", bool(out) and "正在验证" in out[0], str(out))
    check("API Key 输入停止后续事件", key_event.stopped)

    remove_out = await drain(
        plugin.cmd_remove(FakeEvent("/stpro remove vector"), "vector")
    )
    token = remove_out[0].strip().splitlines()[-1]
    remove_chat = FakeEvent("你说的是什么")
    out = await drain(plugin.on_private_message(remove_chat))
    check("等待删除确认时普通聊天透传", out == [] and not remove_chat.stopped)

    token_event = FakeEvent(token)
    out = await drain(plugin.on_private_message(token_event))
    check("动态删除确认码被消费", bool(out) and "已删除" in out[-1], str(out))
    check("删除确认码停止后续事件", token_event.stopped)

    plugin.store._data = None
    await plugin.terminate()


async def run_all() -> int:
    shutil.rmtree(DATA_DIR, ignore_errors=True)
    for case in (
        test_command_signatures,
        test_self_message_ignored,
        test_full_flow,
        test_alignment_config,
        test_admin_priority,
        test_masking,
        test_endpoint_normalize,
        test_choose_right_after_new,
        test_set_modes_and_route_safety,
        test_interaction_consumption_guards,
    ):
        shutil.rmtree(DATA_DIR / "plugin_data", ignore_errors=True)
        SP.store.clear()
        (DATA_DIR / "plugin_data").mkdir(parents=True, exist_ok=True)
        await case()
    print(f"\n通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    for f in FAILED:
        print(f"  - {f}")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run_all()))
