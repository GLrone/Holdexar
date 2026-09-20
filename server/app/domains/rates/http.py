"""rates 域共享 HTTP 客户端：策略引擎优先（代理优先），回落环境变量代理。

Steam 域/汇率源直连在国内网络下基本不可用（成功属侥幸），代理优先。
httpx 是本域统一 HTTP 栈（urllib 的 TLS 指纹会被部分 Provider 拒绝）。
"""
from __future__ import annotations

import httpx


async def open_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """打开共享语义的 httpx 客户端（调用方负责关闭，建议 async with）。"""
    proxy = None
    try:
        from app.domains.proxies import service as proxies_service

        proxy = await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001
        proxy = None
    if proxy is None:
        from app.crawler.proxy import resolve_proxy_url

        proxy = resolve_proxy_url()
    if proxy:
        return httpx.AsyncClient(timeout=timeout, proxy=proxy)
    return httpx.AsyncClient(timeout=timeout)
