"""本机网络环境形态判定。

判定面只有一个：本机的域名解析是否被别的软件接管（典型是开了虚拟网卡
+ 假地址模式的加速工具）。接管之后所有域名都被换成假地址，程序拿到的
节点地址全是假的，探测结论必然是「全部失效」——这不是节点坏了，是环境
不可信。此时继续探测与抓取只会误杀好节点并堆出大量失败记录。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
import time

logger = logging.getLogger(__name__)

# 判定用域名：必然真实存在且与业务无关，避免把「目标站点挂了」误判成环境接管
PROBE_HOST = "www.baidu.com"
# 假地址段（主流内核的默认取值）：解析结果落进这里即判定被接管
FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/16")

# 判定结果短缓存：体检每 5 分钟一拍、价格链每段一问，不必每次都解析
_CACHE_TTL = 30.0
_cache_value = False
_cache_at = 0.0

# 用户语言说明：只说结果与可执行下一步，实现词一律不进用户面
USER_MESSAGE = "本机网络被其他软件接管，为避免误判已暂停相关动作，关闭后会自动继续"


def _resolves_to_fake_ip(host: str = PROBE_HOST) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        # 解析失败不是接管的证据（可能只是本机断网），宁可不判
        return False
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if addr in FAKE_IP_NETWORK:
            return True
    return False


def tun_mode_suspected() -> bool:
    """本机是否疑似开了接管式加速（域名解析被换成假地址）。

    判定失败（域名解析报错）按「没开」处理：探测本身不可用时误停全局抓取，
    代价远大于多跑一轮探测。
    """
    global _cache_value, _cache_at
    now = time.monotonic()
    if now - _cache_at < _CACHE_TTL:
        return _cache_value
    value = _resolves_to_fake_ip()
    _cache_value = value
    _cache_at = now
    return value


def reset_cache() -> None:
    """丢弃判定缓存（测试与「用户刚改过环境」场景用）。"""
    global _cache_at
    _cache_at = 0.0
