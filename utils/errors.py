"""错误分类与短错误编号。

设计文档第 5 节：未知异常应返回一个短错误编号，方便管理员定位日志；日志可以
包含异常类型、HTTP 状态、Profile UUID 和请求阶段，但不得包含完整 Key。
"""

from __future__ import annotations

import itertools
import random

_counter = itertools.count(1)


class ErrorCategory:
    """决定是否计入"三次远程失败"计数器。"""

    LOCAL = "local"  # 本地格式错误，不计远程重试
    AUTH = "auth"  # 401/403，Key 错误、失效或无权限
    NOT_FOUND = "not_found"  # 404，检查 Endpoint 与 /v1 路径
    RATE_LIMIT = "rate_limit"  # 429，限流或余额，不判定配置无效
    NETWORK = "network"  # 超时、连接失败、5xx，不判定 Key 无效
    UNKNOWN = "unknown"


# 只有这些类别计入"当前多轮操作中的远程请求失败"
REMOTE_CATEGORIES = frozenset(
    {
        ErrorCategory.AUTH,
        ErrorCategory.NOT_FOUND,
        ErrorCategory.RATE_LIMIT,
        ErrorCategory.NETWORK,
        ErrorCategory.UNKNOWN,
    },
)


class StproError(Exception):
    """插件内部统一异常。

    Args:
        category: 见 `ErrorCategory`。
        user_message: 可以直接发给用户的、已脱敏的提示。
        log_detail: 仅进日志的上下文（异常类型、HTTP 状态、阶段等，不得含 Key）。
    """

    def __init__(
        self,
        category: str,
        user_message: str,
        log_detail: str = "",
    ) -> None:
        super().__init__(user_message)
        self.category = category
        self.user_message = user_message
        self.log_detail = log_detail
        self.error_id = f"E{random.randint(1000, 9999)}-{next(_counter):04d}"

    @property
    def counts_as_remote_failure(self) -> bool:
        return self.category in REMOTE_CATEGORIES

    def __str__(self) -> str:
        return f"[{self.error_id}] {self.user_message}"


class PermissionDenied(StproError):
    """越权、非所有者、非绑定管理员，或被管理员接管后禁止写入。"""

    def __init__(self, user_message: str, log_detail: str = "") -> None:
        super().__init__(ErrorCategory.LOCAL, user_message, log_detail)


class ConflictError(StproError):
    """与管理员显式配置冲突，或对象状态不允许当前操作。

    这类错误一律"零写入"：插件不得覆盖 AstrBot 管理员的显式配置。
    """

    def __init__(self, user_message: str, log_detail: str = "") -> None:
        super().__init__(ErrorCategory.LOCAL, user_message, log_detail)


class CapabilityUnavailable(StproError):
    """平台适配器能力不足（无法可靠校验群成员、无法解析唯一 UMO 等）。"""

    def __init__(self, user_message: str, log_detail: str = "") -> None:
        super().__init__(ErrorCategory.LOCAL, user_message, log_detail)


def from_openai_exception(exc: Exception, stage: str) -> StproError:
    """把 OpenAI SDK / HTTP 异常映射为插件错误。

    只依赖异常类名做匹配，避免在导入失败的环境下无法使用本模块。
    """
    name = type(exc).__name__
    detail = f"stage={stage} exc_type={name}"

    if name in ("AuthenticationError", "PermissionDeniedError"):
        return StproError(
            ErrorCategory.AUTH,
            "认证失败：API Key 错误、失效或无权限。请检查后重试。",
            detail,
        )
    if name == "NotFoundError":
        return StproError(
            ErrorCategory.NOT_FOUND,
            "请求返回 404：请检查 Endpoint 是否正确，以及是否缺少 `/v1` 路径。",
            detail,
        )
    if name == "RateLimitError":
        return StproError(
            ErrorCategory.RATE_LIMIT,
            "触发限流（429）：可能是速率限制或余额不足，配置本身未被判定为无效。",
            detail,
        )
    if name in ("APITimeoutError", "APIConnectionError"):
        return StproError(
            ErrorCategory.NETWORK,
            "连接失败或超时：服务商暂时不可用，稍后请重试。",
            detail,
        )

    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return StproError(
                ErrorCategory.AUTH,
                "认证失败：API Key 错误、失效或无权限。请检查后重试。",
                f"{detail} status={status}",
            )
        if status == 404:
            return StproError(
                ErrorCategory.NOT_FOUND,
                "请求返回 404：请检查 Endpoint 是否正确，以及是否缺少 `/v1` 路径。",
                f"{detail} status={status}",
            )
        if status == 429:
            return StproError(
                ErrorCategory.RATE_LIMIT,
                "触发限流（429）：可能是速率限制或余额不足，配置本身未被判定为无效。",
                f"{detail} status={status}",
            )
        if 500 <= status < 600:
            return StproError(
                ErrorCategory.NETWORK,
                "服务商返回 5xx：服务异常，稍后请重试。",
                f"{detail} status={status}",
            )

    return StproError(
        ErrorCategory.UNKNOWN,
        "发生未知错误，请稍后重试。",
        detail,
    )
