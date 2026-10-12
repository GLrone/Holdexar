"""共享 SSL 上下文缓存：httpx 每次新建客户端都会重建证书上下文（Windows 上
读系统证书库 + 加载 CA 包，单次数百毫秒），逐请求建客户端的模块会把事件循环
卡成碎片。上下文按校验模式进程级复用，单 event loop 下共享安全。"""
from __future__ import annotations

import ssl

_CONTEXTS: dict[bool, ssl.SSLContext] = {}


def ssl_context(verify: bool) -> ssl.SSLContext:
    """按校验模式取缓存上下文；verify=False 关闭证书校验（本机自选代理折中）。"""
    ctx = _CONTEXTS.get(verify)
    if ctx is None:
        ctx = ssl.create_default_context()
        if not verify:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        _CONTEXTS[verify] = ctx
    return ctx
