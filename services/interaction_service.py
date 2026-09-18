"""多轮输入状态机。

设计文档第 11 节：

- 同一用户同时只能进行一个流程，状态按"平台实例 ID + 用户 ID"隔离；
- 新 `/stpro` 命令先取消旧流程，再继续执行新命令；
- 取消时清除临时 Endpoint、Key、模型列表和重试次数；
- 5 分钟未完成自动取消；
- API Key 只在必要流程中短暂保存在内存；成功、失败、取消、超时和插件停止时都要
  删除引用；
- 普通私聊消息只有在存在活动流程且当前步骤需要文本输入时才消费。
- 状态对象不得写入磁盘。

不使用 `session_waiter`：它会优先截获后续消息，不适合"取消旧流程后继续执行新命令"。
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

STEP_IDLE = "idle"
STEP_ENDPOINT = "awaiting_endpoint"
STEP_API_KEY = "awaiting_api_key"
STEP_REMOVE_CONFIRM = "awaiting_remove_confirmation"
STEP_PERSONA_NAME = "awaiting_persona_name"
STEP_PERSONA_PROMPT = "awaiting_persona_prompt"

# 需要用户以普通文本输入的步骤
TEXT_STEPS = (
    STEP_ENDPOINT,
    STEP_API_KEY,
    STEP_REMOVE_CONFIRM,
    STEP_PERSONA_NAME,
    STEP_PERSONA_PROMPT,
)

# 最多保留的候选模型编号（编号只对最近一次列表有效）
MODEL_INDEX_BASE = 1


@dataclass
class FlowState:
    """单个用户的活动流程。仅存在于内存。"""

    user_key: str
    command: str  # new / set_endpoint / set_apikey / set_all / remove
    step: str = STEP_IDLE
    timeout_sec: int = 300
    profile_id: str | None = None
    candidate_name: str | None = None
    candidate_endpoint: str | None = None
    candidate_api_key: str | None = None
    candidate_persona_name: str | None = None
    models: list[str] = field(default_factory=list)
    models_generated_at: float = 0.0
    remote_failures: int = 0
    confirm_token: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    last_active: float = field(default_factory=time.monotonic)

    def touch(self) -> None:
        self.last_active = time.monotonic()

    def clear_secret(self) -> None:
        """删除内存中的 Key 与一次性 token 引用。"""
        self.candidate_api_key = None
        self.confirm_token = None

    @property
    def active(self) -> bool:
        return self.step != STEP_IDLE

    def expects_text(self) -> bool:
        return self.step in TEXT_STEPS


class InteractionService:
    """轻量状态机。所有状态由锁保护，且不落盘。"""

    def __init__(
        self,
        *,
        timeout_sec: int = 300,
        confirm_timeout_sec: int = 120,
        model_cache_ttl_sec: int = 300,
        max_remote_retries: int = 3,
        clock: Any = time.monotonic,
    ) -> None:
        self.timeout_sec = timeout_sec
        # 删除确认是一次性操作，超时必须比普通多轮流程更短（第 6 节"短超时"）
        self.confirm_timeout_sec = confirm_timeout_sec
        self.model_cache_ttl_sec = model_cache_ttl_sec
        self.max_remote_retries = max_remote_retries
        self._clock = clock
        self._flows: dict[str, FlowState] = {}
        self._lock = asyncio.Lock()

    # ---------- 生命周期 ----------

    async def start(
        self,
        user_key: str,
        command: str,
        *,
        profile_id: str | None = None,
        candidate_name: str | None = None,
        candidate_persona_name: str | None = None,
    ) -> FlowState:
        """开始新流程。已有活动流程会先被取消（返回前已清理）。"""
        async with self._lock:
            old = self._flows.get(user_key)
            if old is not None and old.active:
                logger.debug(f"[stpro] 新命令 {command} 取消旧流程 {old.command}")
                old.clear_secret()
            state = FlowState(
                user_key=user_key,
                command=command,
                profile_id=profile_id,
                candidate_name=candidate_name,
                candidate_persona_name=candidate_persona_name,
                step=self._initial_step(command),
                timeout_sec=(
                    self.confirm_timeout_sec
                    if command == "remove"
                    else self.timeout_sec
                ),
                created_at=self._clock(),
                last_active=self._clock(),
            )
            if command == "remove":
                state.confirm_token = secrets.token_urlsafe(16)
            self._flows[user_key] = state
            return state

    async def get(self, user_key: str) -> FlowState | None:
        async with self._lock:
            state = self._flows.get(user_key)
            if state is None:
                return None
            if self._is_expired(state):
                state.clear_secret()
                self._flows.pop(user_key, None)
                return None
            return state

    async def finish(self, user_key: str) -> None:
        """流程正常结束。

        第 11 节只要求"取消时清除模型列表"，成功结束时必须**保留模型列表缓存**：
        `new` 的提示是"请另发一条命令用序号或名称选择"，如果这里把缓存清掉，
        用户照着提示发 `/stpro model <名> 1` 会被判为"缓存不存在"而拒绝。
        保留的缓存仍受模型编号有效期与 Profile 匹配限制。

        敏感数据（Key、确认 token）无论如何都要清除。
        """
        async with self._lock:
            state = self._flows.get(user_key)
            if state is None:
                return
            state.clear_secret()
            state.step = STEP_IDLE
            state.touch()  # 重新计时，避免刚结束就被判超时

    async def cancel(self, user_key: str) -> FlowState | None:
        """用户取消或超时：清除全部临时状态，含 Endpoint、Key、模型列表和重试次数。"""
        async with self._lock:
            state = self._flows.pop(user_key, None)
            if state is not None:
                state.clear_secret()
                state.models = []
                state.models_generated_at = 0.0
                state.candidate_endpoint = None
                state.candidate_persona_name = None
                state.remote_failures = 0
                state.step = STEP_IDLE
            return state

    async def sweep_expired(self) -> None:
        """清理超时流程。由后台任务周期性调用。"""
        async with self._lock:
            expired = [k for k, v in self._flows.items() if self._is_expired(v)]
            for key in expired:
                state = self._flows.pop(key, None)
                if state is not None:
                    state.clear_secret()
                    logger.debug(f"[stpro] 流程超时自动取消: user={key}")

    async def clear_all(self) -> None:
        """插件停止时调用：清除全部内存状态与 Key 引用。"""
        async with self._lock:
            for state in self._flows.values():
                state.clear_secret()
            self._flows.clear()

    # ---------- 步骤推进 ----------

    @staticmethod
    def _initial_step(command: str) -> str:
        """只有需要文本输入的命令才进入等待步骤。

        `model` 只是借用状态机缓存模型编号，不等待任何用户输入。
        """
        if command in ("new", "set_endpoint", "set_all"):
            return STEP_ENDPOINT
        if command == "set_apikey":
            return STEP_API_KEY
        if command == "remove":
            return STEP_REMOVE_CONFIRM
        if command in ("persona_new_name", "persona_set_name", "persona_del_name"):
            return STEP_PERSONA_NAME
        if command in ("persona_new", "persona_edit"):
            return STEP_PERSONA_PROMPT
        return STEP_IDLE

    def _is_expired(self, state: FlowState) -> bool:
        return (self._clock() - state.last_active) > state.timeout_sec

    async def advance_to_key(
        self,
        user_key: str,
        endpoint: str,
    ) -> FlowState | None:
        state = await self.get(user_key)
        if state is None or state.step != STEP_ENDPOINT:
            return None
        state.candidate_endpoint = endpoint
        state.step = STEP_API_KEY
        state.touch()
        return state

    async def set_key(self, user_key: str, api_key: str) -> FlowState | None:
        state = await self.get(user_key)
        if state is None or state.step != STEP_API_KEY:
            return None
        state.candidate_api_key = api_key
        state.touch()
        return state

    async def set_persona_name(
        self,
        user_key: str,
        name: str,
        *,
        prompt_next: bool,
    ) -> FlowState | None:
        state = await self.get(user_key)
        if state is None or state.step != STEP_PERSONA_NAME:
            return None
        state.candidate_persona_name = name
        state.step = STEP_PERSONA_PROMPT if prompt_next else STEP_IDLE
        state.touch()
        return state

    async def remember_models(
        self,
        user_key: str,
        models: list[str],
        profile_id: str | None = None,
    ) -> None:
        """缓存最近一次成功取得的模型列表，并关联 Profile UUID（第 4 节）。"""
        state = await self.get(user_key)
        if state is None:
            return
        state.models = list(models)
        state.models_generated_at = self._clock()
        if profile_id:
            state.profile_id = profile_id
        state.touch()

    async def resolve_model_index(
        self, user_key: str, profile_id: str, index: int
    ) -> str | None:
        """把模型编号解析为模型 ID。

        缓存不存在、Profile 不匹配、已过期或序号越界时返回 None。
        """
        state = await self.get(user_key)
        if state is None:
            return None
        if state.profile_id != profile_id or not state.models:
            return None
        # 模型编号有独立的有效期，且不能跨插件重载使用（第 4 节）
        if (self._clock() - state.models_generated_at) >= self.model_cache_ttl_sec:
            return None
        offset = index - MODEL_INDEX_BASE
        if offset < 0 or offset >= len(state.models):
            return None
        return state.models[offset]

    async def resolve_model_choice(
        self,
        user_key: str,
        profile_id: str,
        choice: str,
    ) -> str | None:
        """从最近一次模型列表中按序号或名称解析模型。"""
        choice = (choice or "").strip()
        if not choice:
            return None
        if choice.isdigit():
            return await self.resolve_model_index(user_key, profile_id, int(choice))

        state = await self.get(user_key)
        if state is None or state.profile_id != profile_id or not state.models:
            return None
        if (self._clock() - state.models_generated_at) >= self.model_cache_ttl_sec:
            return None

        for model in state.models:
            if model == choice:
                return model
        lowered = choice.casefold()
        for model in state.models:
            if model.casefold() == lowered:
                return model
        return None

    async def count_remote_failure(self, user_key: str) -> tuple[int, bool]:
        """记一次远程失败，返回 (累计次数, 是否已达上限)。"""
        state = await self.get(user_key)
        if state is None:
            return 0, False
        state.remote_failures += 1
        state.touch()
        return state.remote_failures, state.remote_failures >= self.max_remote_retries

    def verify_token(self, state: FlowState | None, token: str) -> bool:
        """删除确认 token 校验。token 不匹配一律拒绝。"""
        if state is None or not state.confirm_token:
            return False
        # compare_digest 不支持非 ASCII str，必须先编码
        return secrets.compare_digest(
            state.confirm_token.encode("utf-8"),
            token.encode("utf-8"),
        )
