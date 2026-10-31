"""本机 TCP 端口原语：探活与空闲分配的单一实现（内核/下载/lane 共用）。"""
from __future__ import annotations

import socket


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """端口有进程在听（connect 探测）。"""
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def free_local_port() -> int:
    """让系统分配一个空闲的本机 TCP 端口。

    刻意不做端口租约：真撞上时内核会暴露失败，到真实使用中出现证据再做端口管理。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def is_ssl_error(exc: "Exception | str") -> bool:
    """SSL 证书类错误判定（账户/账单/家庭/成就/兑换五域共用单实现）。

    线索表为原五份拷贝的并集；本机加速器回环场景（hosts 劫持 +
    127.0.0.1 自签证书）依赖此判定做证书校验降级重试。
    """
    text = f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else str(exc or "")
    lowered = text.lower()
    return any(clue in lowered for clue in (
        "ssl", "certificate_verify_failed", "certificate verify failed",
        "self-signed", "unable to get local issuer", "certifi",
    ))
