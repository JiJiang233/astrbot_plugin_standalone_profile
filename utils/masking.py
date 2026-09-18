"""API Key 脱敏。

规则来自设计文档第 3 节：所有消息、列表、日志和异常中的 Key 必须脱敏，如
`sk-****a8F2`；对极短、无 `sk-` 前缀的 Key 也必须保证不会暴露其大部分内容。
"""

from __future__ import annotations

_MASK_CHAR = "*"
_MIN_TOTAL_LEN = 8
_KEEP_HEAD = 3
_KEEP_TAIL = 4


def mask_secret(secret: str | None) -> str:
    """返回脱敏后的 Key。

    - 空值返回空串，不泄露长度信息之外的任何内容。
    - 长度不足时只保留首字符（或完全打码），绝不保留"大部分内容"。
    - `sk-` 前缀保留，便于用户区分不同服务商的 Key。
    """
    if not secret:
        return ""

    secret = secret.strip()
    if not secret:
        return ""

    prefix = ""
    body = secret
    if secret.startswith("sk-") and len(secret) > 3:
        prefix = "sk-"
        body = secret[3:]

    if len(body) <= 2:
        return f"{prefix}{_MASK_CHAR * max(len(body), 2)}"

    if len(secret) < _MIN_TOTAL_LEN:
        # 极短 Key：只留首字符，其余全部打码
        return f"{prefix}{body[0]}{_MASK_CHAR * (len(body) - 1)}"

    return f"{prefix}{body[:_KEEP_HEAD]}{_MASK_CHAR * 4}{body[-_KEEP_TAIL:]}"


def mask_endpoint(endpoint: str | None) -> str:
    """Endpoint 不含密钥，但可能带 query/token，统一去掉查询串再展示。"""
    if not endpoint:
        return ""
    return endpoint.split("?", 1)[0].rstrip("/")


def scrub_text(text: str, secrets: list[str]) -> str:
    """把文本中出现的完整 Key 替换为脱敏形式，用于日志与异常兜底。"""
    if not text:
        return text
    for secret in secrets:
        if secret and len(secret) >= 6 and secret in text:
            text = text.replace(secret, mask_secret(secret))
    return text
