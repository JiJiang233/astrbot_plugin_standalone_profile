"""Endpoint 归一化。

OpenAI 兼容接口的 `base_url` 应当只到版本段（通常是 `/v1`）：SDK 会自己在后面拼
`/models`、`/chat/completions`。用户手输的地址经常缺 `/v1`，或多带了具体接口路径，
这里统一归一化，避免把"404"留到请求阶段才发现（设计文档第 5 节）。

规则：

- 去掉末尾斜杠与空白；
- 剥掉尾部的具体接口路径（`/chat/completions`、`/completions`、`/models` 等）；
- 路径中已经存在版本段（`v1`、`v1beta`、`v4`…）时不追加，避免 `/v1/v1`；
- 其余情况追加 `/v1`。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

_KNOWN_TAIL_PATHS = (
    "/chat/completions",
    "/completions",
    "/responses",
    "/embeddings",
    "/models",
)

_VERSION_SEGMENT = re.compile(r"^v\d+", re.IGNORECASE)


def normalize_endpoint(raw: str) -> str:
    """把用户输入的地址归一化为 OpenAI 兼容的 base_url。"""
    endpoint = (raw or "").strip().rstrip("/")
    if not endpoint:
        return endpoint

    for tail in _KNOWN_TAIL_PATHS:
        if endpoint.lower().endswith(tail):
            endpoint = endpoint[: -len(tail)].rstrip("/")
            break

    path = urlparse(endpoint).path
    segments = [seg for seg in path.split("/") if seg]
    if any(_VERSION_SEGMENT.match(seg) for seg in segments):
        return endpoint

    return f"{endpoint}/v1"


def did_normalize(raw: str, normalized: str) -> bool:
    """是否发生了补全/改写，用于提示用户。"""
    return (raw or "").strip().rstrip("/") != normalized
