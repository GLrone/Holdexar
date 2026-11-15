"""局域网访问模式的状态持有层：开关、本机地址集、解锁限速。

信任模型：默认关闭＝纯回环（行为与历史版本一致）。开启后 Host 闸放行
本机网卡地址，局域网浏览器凭访问密码自助换会话（`/session/unlock`）。
本模块只持内存态与纯计算，持久化（设置 KV）与凭据读写归 settings 域，
避免 core 反向依赖 domains。
"""
from __future__ import annotations

import os
import secrets
import socket
import time

ENV_ACCESS = "HOLDEXAR_ACCESS"
# 环境变量名常量（组合构造；密码值本身只从环境读取，绝不入源码）
ENV_ACCESS_PASSWORD = f"{ENV_ACCESS}_PASSWORD"

_enabled: bool = False

# 解锁失败限速：连续失败达阈值后全局锁定一段时间（局域网信任假设下的
# 防穷举底线；成功一次即清零）。
_UNLOCK_MAX_FAILS = 5
_UNLOCK_LOCK_S = 30.0
_fail_count: int = 0
_lock_until: float = 0.0


def env_forced() -> bool:
    """环境变量是否强制开启（区别于设置 KV 通道）。"""
    return os.environ.get(ENV_ACCESS, "").strip().lower() == "lan"


def is_enabled() -> bool:
    """局域网模式是否生效（环境变量强制开启优先于设置 KV 的缓存结果）。"""
    if env_forced():
        return True
    return _enabled


def set_enabled(enabled: bool) -> bool:
    """更新设置 KV 通道的缓存开关；返回生效状态（环境变量开启时恒真）。"""
    global _enabled
    _enabled = bool(enabled)
    return is_enabled()


def env_password() -> str | None:
    """无窗口场景由环境变量直接指定访问密码；未设置返回 None。"""
    raw = os.environ.get(ENV_ACCESS_PASSWORD, "").strip()
    return raw or None


def generate_password() -> str:
    """生成 12 位随机访问密码（URL 安全字符，手机可输入）。"""
    return secrets.token_urlsafe(9)


def local_ips() -> list[str]:
    """本机全部网卡地址（字面量集合，供 Host 闸放行局域网直连）。"""
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        # 路由表探测：向外连接的本机源地址（多网卡时补齐默认出口地址）
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.5)
            sock.connect(("223.5.5.5", 53))
            ips.add(sock.getsockname()[0])
    except OSError:
        pass
    return [ip for ip in ips if ip and not ip.startswith("169.254.")]


def refresh_extra_hosts() -> None:
    """按当前开关同步 Host 闸白名单：开＝本机地址集，关＝回环-only。"""
    from app.core import access

    access.set_extra_hosts(local_ips() if is_enabled() else ())


def unlock_locked_seconds() -> float:
    """解锁通道剩余锁定秒数（0＝可尝试）。"""
    if _lock_until <= 0.0:
        return 0.0
    remaining = _lock_until - time.monotonic()
    return max(0.0, remaining)


def unlock_record_failure() -> None:
    """记录一次解锁失败；连续失败达阈值进入锁定窗口。"""
    global _fail_count, _lock_until
    _fail_count += 1
    if _fail_count >= _UNLOCK_MAX_FAILS:
        _lock_until = time.monotonic() + _UNLOCK_LOCK_S
        _fail_count = 0


def unlock_record_success() -> None:
    """解锁成功清零失败计数与锁定。"""
    global _fail_count, _lock_until
    _fail_count = 0
    _lock_until = 0.0


def reset_unlock_state() -> None:
    """测试隔离：清空限速状态。"""
    global _fail_count, _lock_until
    _fail_count = 0
    _lock_until = 0.0


__all__ = [
    "ENV_ACCESS",
    "ENV_ACCESS_PASSWORD",
    "env_forced",
    "env_password",
    "generate_password",
    "is_enabled",
    "local_ips",
    "refresh_extra_hosts",
    "reset_unlock_state",
    "set_enabled",
    "unlock_locked_seconds",
    "unlock_record_failure",
    "unlock_record_success",
]
