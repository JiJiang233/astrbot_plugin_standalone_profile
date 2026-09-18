"""最小 astrbot API 桩，用于在没有 AstrBot 运行时的环境下跑插件测试。

只实现插件实际用到的接口，不参与插件运行。
"""

import enum
import os
import sys
import types
from dataclasses import dataclass, field


class _Logger:
    def __init__(self):
        self.records: list[tuple[str, str]] = []

    def _log(self, level, msg, *a, **k):
        self.records.append((level, str(msg)))

    info = lambda self, msg, *a, **k: self._log("info", msg)  # noqa: E731
    debug = lambda self, msg, *a, **k: self._log("debug", msg)  # noqa: E731
    warning = lambda self, msg, *a, **k: self._log("warning", msg)  # noqa: E731
    error = lambda self, msg, *a, **k: self._log("error", msg)  # noqa: E731
    exception = lambda self, msg, *a, **k: self._log("exception", msg)  # noqa: E731


logger = _Logger()


class SharedPreferences:
    def __init__(self):
        self.store: dict = {}

    async def get_async(self, scope, scope_id, key, default=None):
        return self.store.get((scope, scope_id, key), default)

    async def session_put(self, umo, key, value):
        self.store[("umo", umo, key)] = value

    async def session_remove(self, umo, key):
        self.store.pop(("umo", umo, key), None)

    async def session_get(self, umo, key, default=None):
        return self.store.get(("umo", umo, key), default)


sp = SharedPreferences()


class MessageType(enum.Enum):
    GROUP_MESSAGE = "GroupMessage"
    FRIEND_MESSAGE = "FriendMessage"
    OTHER_MESSAGE = "OtherMessage"


@dataclass
class MessageSession:
    platform_name: str
    message_type: MessageType
    session_id: str
    platform_id: str = field(init=False)

    def __post_init__(self):
        self.platform_id = self.platform_name

    def __str__(self):
        return f"{self.platform_id}:{self.message_type.value}:{self.session_id}"


class ProviderType(enum.Enum):
    CHAT_COMPLETION = "chat_completion"
    SPEECH_TO_TEXT = "speech_to_text"
    TEXT_TO_SPEECH = "text_to_speech"


class EventMessageType(enum.Flag):
    GROUP_MESSAGE = enum.auto()
    PRIVATE_MESSAGE = enum.auto()
    OTHER_MESSAGE = enum.auto()
    ALL = GROUP_MESSAGE | PRIVATE_MESSAGE | OTHER_MESSAGE


class _RegisteringCommandable:
    def __init__(self, name):
        self.name = name
        self.children = []

    def command(self, name):
        def deco(fn):
            fn.__stpro_command__ = f"{self.name} {name}"
            self.children.append(fn)
            return fn

        return deco

    def group(self, name):
        return _RegisteringCommandable(f"{self.name} {name}")


class _Filter:
    @staticmethod
    def command_group(name):
        def deco(fn):
            return _RegisteringCommandable(name)

        return deco

    @staticmethod
    def command(name):
        def deco(fn):
            fn.__stpro_command__ = name
            return fn

        return deco

    @staticmethod
    def event_message_type(t):
        def deco(fn):
            fn.__stpro_event__ = t
            return fn

        return deco


filter = _Filter()


class AstrMessageEvent:
    pass


class MessageChain:
    def __init__(self, components=None):
        self.components = components or []


class Plain:
    def __init__(self, text):
        self.text = text


class Image:
    @staticmethod
    def fromFileSystem(path):
        return ("image", path)


class Context:
    pass


class Star:
    def __init__(self, context, config=None):
        self.context = context
        self.config = config

    async def initialize(self):
        pass

    async def terminate(self):
        pass


def get_astrbot_plugin_data_path():
    return str(DATA_ROOT)


DATA_ROOT = "/tmp/stpro_test_data/plugin_data"


def build(data_root: str = DATA_ROOT) -> None:
    """把桩模块注册到 sys.modules。"""
    global DATA_ROOT
    DATA_ROOT = data_root
    os.makedirs(data_root, exist_ok=True)

    def mod(name, **attrs):
        m = types.ModuleType(name)
        for k, v in attrs.items():
            setattr(m, k, v)
        sys.modules[name] = m
        return m

    mod("astrbot")
    mod("astrbot.core")
    mod("astrbot.core.platform")
    mod("astrbot.core.utils")

    mod("astrbot.api", logger=logger, sp=sp)
    mod("astrbot.api.star", Context=Context, Star=Star)
    mod(
        "astrbot.api.event",
        AstrMessageEvent=AstrMessageEvent,
        MessageChain=MessageChain,
        filter=filter,
    )
    mod("astrbot.api.event.filter", EventMessageType=EventMessageType, filter=filter)
    mod("astrbot.api.message_components", Plain=Plain, Image=Image)
    mod("astrbot.api.provider", ProviderType=ProviderType)
    mod(
        "astrbot.core.platform.message_session",
        MessageSession=MessageSession,
        MessageSesion=MessageSession,
    )
    mod("astrbot.core.platform.message_type", MessageType=MessageType)

    path_mod = mod(
        "astrbot.core.utils.astrbot_path",
        get_astrbot_plugin_data_path=lambda: DATA_ROOT,
    )
    path_mod.get_astrbot_plugin_data_path = lambda: DATA_ROOT
