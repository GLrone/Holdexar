"""代理凭据的静态加密口径：落库密封、使用时解密。

`proxies.username` / `proxies.password` 是长期有效的代理服务凭据，与 Steam
Cookie 同级：落库一律封装为 `enc1:` 密文，只有真正要出网时才解密。

存量明文按 `enc1:` 前缀分流原样可用（启动封存步骤收敛）；解密失败返回空串
（密钥属主机绑定，库文件被单独拷走 / 整机迁移后不可解）——行内密文保留，
调用方按「代理不可用」处理。

**端口约束**：本模块只负责库内值与内存对象之间的转换。任何响应体、日志、
导出文件都不得直接输出这里的明文（导出走 `data_export` 的显式解密路径，
且受本地会话准入保护）。
"""
from __future__ import annotations

import logging

from app.core import secretbox
from app.core.logging import log_event
from app.core.secretbox import SecretBoxError

logger = logging.getLogger(__name__)

PURPOSE = "proxy-credential"


def seal(value: str | None) -> str:
    """明文 → 密文（空值原样返回空串）。"""
    if not value:
        return ""
    return secretbox.encrypt_secret(value, PURPOSE)


def open_value(value: str | None) -> str:
    """密文 → 明文；非密文原样返回（存量明文兼容）。"""
    text = value or ""
    if not secretbox.is_encrypted(text):
        return text
    try:
        return secretbox.decrypt_secret(text, PURPOSE)
    except SecretBoxError:
        log_event(logger, "代理凭据无法在本机解密，按不可用处理", level=logging.WARNING)
        return ""


def seal_proxy(proxy) -> None:
    """把代理对象上的明文账号口令就地替换为密文（落库前调用）。"""
    if proxy.username and not secretbox.is_encrypted(proxy.username):
        proxy.username = seal(proxy.username)
    if proxy.password and not secretbox.is_encrypted(proxy.password):
        proxy.password = seal(proxy.password)


def open_proxy(proxy) -> None:
    """把代理对象上的密文就地替换为明文（出网前调用）。"""
    proxy.username = open_value(proxy.username) or None
    proxy.password = open_value(proxy.password) or None


__all__ = ["PURPOSE", "open_proxy", "open_value", "seal", "seal_proxy"]
