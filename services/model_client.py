"""OpenAI 兼容接口的远程客户端。

设计文档第 5 节：验证使用异步 OpenAI SDK，按异常类型分类；Endpoint 本地格式错误
不计入远程重试。这里是对外发起请求的唯一出口，测试时可整体替换为 Adapter。
"""

from __future__ import annotations

import re
from typing import Any

from ..utils.endpoint import normalize_endpoint
from ..utils.errors import ErrorCategory, StproError, from_openai_exception
from ..utils.masking import scrub_text

_ENDPOINT_RE = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)


def validate_endpoint(endpoint: str) -> str:
    """本地格式校验 + 归一化（自动补 `/v1`）。

    失败抛 `StproError(LOCAL)`，不计入远程重试；成功返回归一化后的地址。
    """
    endpoint = (endpoint or "").strip().rstrip("/")
    if not endpoint:
        raise StproError(ErrorCategory.LOCAL, "Endpoint 不能为空。")
    if not _ENDPOINT_RE.match(endpoint):
        raise StproError(
            ErrorCategory.LOCAL,
            "Endpoint 格式不正确。请填写完整的 http(s) 地址，"
            "例如 `https://api.openai.com/v1`。",
        )
    if not endpoint.lower().startswith(("http://", "https://")):
        raise StproError(
            ErrorCategory.LOCAL, "Endpoint 必须以 http:// 或 https:// 开头。"
        )
    return normalize_endpoint(endpoint)


class ModelClient:
    """封装模型列表获取与可用性探测。"""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    def _client(self, endpoint: str, api_key: str) -> Any:
        from openai import AsyncOpenAI  # 延迟导入，避免未安装时影响插件加载

        return AsyncOpenAI(
            base_url=endpoint,
            api_key=api_key,
            timeout=self.timeout,
            max_retries=0,
        )

    @staticmethod
    def _scrub(exc: Exception, api_key: str) -> Exception:
        """抹掉异常消息里可能回显的 API Key（第 5/18 节）。

        这里是唯一同时拿到 Key 和异常的地方：异常向上抛后会被多处写进日志，
        必须先把 Key 换掉，否则"未知异常"路径会泄露 Key。
        """
        if not api_key:
            return exc
        try:
            message = str(exc)
            if api_key not in message:
                return exc
            cleaned = scrub_text(message, [api_key])
            exc.args = (cleaned,) + tuple(exc.args[1:])
        except Exception:
            return exc
        return exc

    async def fetch_models(self, endpoint: str, api_key: str) -> list[str]:
        """获取模型 ID 列表。失败抛 `StproError`。"""
        endpoint = validate_endpoint(endpoint)
        client = self._client(endpoint, api_key)
        try:
            resp = await client.models.list()
            # 兼容返回分页对象或直接是列表的情况
            items = getattr(resp, "data", None)
            if items is None:
                items = resp if isinstance(resp, list) else []
            models: list[str] = []
            for model in items:
                model_id = getattr(model, "id", None)
                if model_id:
                    models.append(str(model_id))
            return models
        except Exception as exc:
            raise from_openai_exception(
                self._scrub(exc, api_key),
                stage="list_models",
            ) from exc
        finally:
            await client.close()

    async def probe(
        self,
        endpoint: str,
        api_key: str,
        model: str,
    ) -> int:
        """做一次最小请求，返回耗时（毫秒）。失败抛 `StproError`。"""
        endpoint = validate_endpoint(endpoint)
        client = self._client(endpoint, api_key)
        try:
            import time

            started = time.monotonic()
            await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=1,
            )
            return int((time.monotonic() - started) * 1000)
        except Exception as exc:
            raise from_openai_exception(
                self._scrub(exc, api_key),
                stage="probe",
            ) from exc
        finally:
            await client.close()
