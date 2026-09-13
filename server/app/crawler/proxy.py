"""代理解析（M1 最小实现：默认直连 + 可选单一代理）。

策略引擎已落在 domains/proxies/service.resolve_proxy_url（direct_only /
direct_first / proxy_only / proxy_first），本模块是其底层取值口，调用方不变。
"""
from __future__ import annotations

import os

from app.core.app_info import ENV_PREFIX


def resolve_proxy_url() -> str | None:
    """返回本次运行的固定代理 URL；未设置则直连。

    环境变量：<ENV_PREFIX>PROXY_URL，如 http://127.0.0.1:7890 或 socks5://user:pass@host:port
    """
    url = os.environ.get(f"{ENV_PREFIX}PROXY_URL", "").strip()
    return url or None
