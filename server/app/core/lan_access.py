"""局域网访问模式的状态持有层：开关、本机地址集、解锁限速、访问密码验证器。

信任模型：默认关闭＝纯回环（行为与历史版本一致）。开启后 Host 闸放行
本机网卡地址，局域网浏览器凭访问密码自助换会话（`/session/unlock`）。
访问密码以 scrypt PHC 验证器串存储（不可逆——服务端自己也恢复不出明文，
号主拿库同样无效）；本模块持内存态与验证器纯计算，持久化归 settings 域。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import socket
import time

ENV_ACCESS = "HOLDEXAR_ACCESS"
# 环境变量名常量（组合构造；密码值本身只从环境读取，绝不入源码）
ENV_ACCESS_PASSWORD = f"{ENV_ACCESS}_PASSWORD"

_enabled: bool = False

# 解锁失败限速：按来源对端分桶（进程内全局单计数器会让任一恶意设备把
# 合法设备的自助解锁一并锁死），连续失败达阈值后锁定该对端一段时间；
# 成功一次即清零。锁窗过期后再次穷举的，锁时按连续轮次加倍（渐进升级
# 至 10 分钟帽，防「过期-再锁」循环维持无限解锁拒绝）。桶数有帽，超限
# 先清过期桶再按插入序丢弃最旧。
_UNLOCK_MAX_FAILS = 5
_UNLOCK_LOCK_S = 30.0
_LOCK_CAP_S = 600.0
_PEER_CAP = 512
_peer_state: dict[str, dict[str, float | int]] = {}


def peer_key(scope) -> str:
    """限速分桶键：来源对端地址；合成 scope（无 client）按本机处理。"""
    client = scope.get("client")
    if not client:
        return "loopback"
    addr_text = client[0] if isinstance(client, (list, tuple)) else str(client)
    return addr_text or "loopback"


def _peer(peer: str) -> dict[str, float | int]:
    if len(_peer_state) >= _PEER_CAP and peer not in _peer_state:
        now = time.monotonic()
        for key in [k for k, s in _peer_state.items() if s["lock_until"] <= now]:
            del _peer_state[key]
            if len(_peer_state) < _PEER_CAP:
                break
        while len(_peer_state) >= _PEER_CAP:
            _peer_state.pop(next(iter(_peer_state)))
    return _peer_state.setdefault(peer, {"fails": 0, "lock_until": 0.0, "cycle": 0})


def unlock_locked_seconds(peer: str = "loopback") -> float:
    """该对端解锁通道剩余锁定秒数（0＝可尝试）。"""
    lock_until = _peer(peer)["lock_until"]
    if lock_until <= 0.0:
        return 0.0
    remaining = lock_until - time.monotonic()
    return max(0.0, remaining)


def unlock_record_failure(peer: str = "loopback") -> None:
    """记录该对端一次解锁失败；连续失败达阈值进入锁定窗口。

    锁窗过期后再次穷举的，锁时按连续轮次加倍（渐进升级至 10 分钟帽）。
    """
    state = _peer(peer)
    state["fails"] = int(state["fails"]) + 1
    if int(state["fails"]) >= _UNLOCK_MAX_FAILS:
        cycle = int(state["cycle"])
        lock_s = min(_UNLOCK_LOCK_S * (2**min(cycle, 5)), _LOCK_CAP_S)
        state["lock_until"] = time.monotonic() + lock_s
        state["fails"] = 0
        state["cycle"] = cycle + 1


def unlock_record_success(peer: str = "loopback") -> None:
    """解锁成功清零该对端的失败计数、锁定与升级轮次。"""
    _peer_state[peer] = {"fails": 0, "lock_until": 0.0, "cycle": 0}


def reset_unlock_state() -> None:
    """测试隔离：清空全部限速状态。"""
    _peer_state.clear()


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
    "hash_password",
    "verify_password",
]


# ── 访问密码验证器（P0-1 终态：存储改为不可逆 scrypt PHC 串）────────────
#
# PHC 串自包含算法与参数（盐随哈希生成），校验按串内参数重算；KDF 参数
# 边界校验在重算之前（恶意篡改大 N 曾可致解锁内存耗尽——P0-2 同源）。
# 格式与 Go 侧 x/crypto/scrypt 跨语言互通（RFC 7914 参数一致即输出一致）。

_PHC_ALGO = "scrypt"
_PHC_N = 1 << 15
_PHC_R = 8
_PHC_P = 1
_PHC_DKLEN = 32
_PHC_SALT_BYTES = 16
_PHC_HASH_BYTES = 32
_PHC_N_MIN = 1 << 12
_PHC_N_MAX = 1 << 22
_PHC_R_MAX = 64
_PHC_P_MAX = 8
_PHC_MEM_LIMIT = 256 * 1024 * 1024


def _b64_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def hash_password(password: str) -> str:
    """口令 → scrypt PHC 验证器串（盐随哈希随机生成，参数内嵌）。"""
    salt = secrets.token_bytes(_PHC_SALT_BYTES)
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_PHC_N, r=_PHC_R, p=_PHC_P,
        dklen=_PHC_DKLEN, maxmem=_PHC_MEM_LIMIT,
    )
    return (
        f"${_PHC_ALGO}${_PHC_N},{_PHC_R},{_PHC_P}"
        f"${_b64_encode(salt)}${_b64_encode(derived)}"
    )


def _parse_phc(phc: str) -> tuple[int, int, int, bytes, bytes]:
    """PHC 串 → (n, r, p, salt, hash)；非法即抛 ValueError（防篡改大 N）。"""
    parts = phc.split("$")
    if len(parts) != 5 or parts[0] != "" or parts[1] != _PHC_ALGO:
        raise ValueError("验证器格式非法")
    try:
        n_text, r_text, p_text = parts[2].split(",")
        n, r, p = int(n_text), int(r_text), int(p_text)
    except ValueError as exc:
        raise ValueError("验证器参数非法") from exc
    if n % 2 or not _PHC_N_MIN <= n <= _PHC_N_MAX:
        raise ValueError(f"验证器参数 N 越界：{n}")
    if not 1 <= r <= _PHC_R_MAX or not 1 <= p <= _PHC_P_MAX:
        raise ValueError(f"验证器参数 r/p 越界：{r}/{p}")
    if r * p >= 1 << 30 or 128 * n * r > _PHC_MEM_LIMIT:
        raise ValueError(f"验证器参数组合超出内存帽：{n}/{r}/{p}")
    salt = _b64_decode(parts[3])
    derived = _b64_decode(parts[4])
    if len(salt) != _PHC_SALT_BYTES or len(derived) < _PHC_HASH_BYTES:
        raise ValueError("验证器盐或哈希长度非法")
    return n, r, p, salt, derived


def verify_password(password: str, phc: str) -> bool:
    """口令 → 与验证器串比对（按串内参数重算，恒定时间比较）。"""
    try:
        n, r, p, salt, expected = _parse_phc(phc)
    except ValueError:
        return False
    derived = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p,
        dklen=len(expected), maxmem=_PHC_MEM_LIMIT,
    )
    return hmac.compare_digest(derived, expected)
