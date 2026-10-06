"""订阅链接静态加密口径：落库密封、使用时解密（secretbox 机器派生密钥）。

订阅链接是付费代理服务的凭据（内嵌 token / 专属路径，拿到即可消耗其流量），
与 Steam 登录凭据同属敏感面：`proxy_subscriptions.url` 与
`subscription_snapshots.url`（快照 provenance）落库一律封装为 AES-256-GCM
密文，消费时（下载 / 流量头 / 内核归属判定 / 面板展示）才解密。

存量明文按 `enc1:` 前缀分流原样可用（启动封存步骤收敛）；解密失败返回
空串（密钥属主机绑定，库文件被单独拷走 / 整机迁移后不可解）——行内密文
保留，调用方按「链接不可用」处理并给出重新添加订阅的指引。
"""
from __future__ import annotations

import logging

from app.core import secretbox
from app.core.logging import log_event
from app.core.secretbox import SecretBoxError

logger = logging.getLogger(__name__)

# 订阅链接的静态加密用途串（secretbox 按此派生独立密钥）
PURPOSE = "proxy-subscription"

# 解密失败的订阅行用户语言提示（用户面不出现实现词）
UNREADABLE_MESSAGE = "订阅链接无法在本机解密，请删除该订阅后重新添加"


def seal_url(url: str | None) -> str:
    """订阅链接落库前封装（AES-256-GCM；空串 / 空值原样返回）。"""
    if not url:
        return ""
    return secretbox.encrypt_secret(url, PURPOSE)


def open_url(value: str | None) -> str:
    """订阅链接密文 → 明文（使用时解密）。

    存量明文（尚未走启动封存步骤）原样返回；解密失败返回空串并留日志，
    不抛异常——调用方以「链接不可用」路径给出用户指引。
    """
    value = value or ""
    if not secretbox.is_encrypted(value):
        return value
    try:
        return secretbox.decrypt_secret(value, PURPOSE)
    except SecretBoxError:
        log_event(logger, "订阅链接无法在本机解密，按链接不可用处理", level=logging.WARNING)
        return ""


def url_matches(stored: str | None, wanted: str) -> bool:
    """快照 provenance 比对：存量值解密后与当前 URL 严格相等。"""
    return bool(wanted) and open_url(stored) == wanted
