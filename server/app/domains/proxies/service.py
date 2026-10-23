"""proxies 域服务：代理池 CRUD / 健康检查 / 策略引擎 / 走线日志。

策略（存 app_settings: proxy.strategy）：
- proxy_first   代理优先（默认）：Clash 在跑走 Clash → 代理池轮询 → 本地
                混合端口 → 直连
- direct_only   直连：作业托管到用户本机网络环境——TUN 型加速器裸连即走，
                系统代理（注册表/环境变量）探活通过则显式挂上；价格作业
                在此形态下也走本机，频率由 crawler 全局限流闸（200 发/5
                分钟）统一约束
- direct_first  直连优先，失败换代理重试
- proxy_only    从启用代理轮询取一个用于整个任务
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import socket
import time
import weakref
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import delete, func, select

from app.core import secretbox
from app.core.database import WritePriority, get_session_factory, write_session
from app.core.logging import log_event
from app.crawler.browse_store import StoreBrowseAPI
from app.crawler.utils import get_beijing_time_obj
from . import clash_manager, credentials
from .models import (
    ADMISSION_ACTIVE,
    ADMISSION_CANDIDATE,
    ClashNode,
    Proxy,
    ProxyEvent,
    ProxySubscription,
)
from .subscription_secret import UNREADABLE_MESSAGE, open_url, seal_url


# ─── Clash 检测串行锁（按事件循环缓存）──────────────────────
# test_clash_nodes 逐个切 selector 探测——手动检测/启动首检/定时体检三源
# 并发会互踩 selector 与延迟读数。锁按事件循环缓存：uvicorn 单 loop 常态
# 下等效模块级单例；测试里每用例新 loop 则各拿各的锁，互不卡死。
_clash_test_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _clash_test_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _clash_test_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _clash_test_locks[loop] = lock
    return lock

logger = logging.getLogger(__name__)

# 健康检查目标：**生产抓取端点**（IStoreBrowseService/GetItems/v1），不是 ping
# ——须与生产主链路（api.steampowered.com）同主机/同路径/同编码：探测别的目标
# （如 store.steampowered.com/api/appdetails）时链路可达性与生产并不一致，
# 体检通过不代表生产可用。探针取单 appid + 不带 extras（最小负载），
# 编码约定由生产侧单一来源提供。
TEST_URL = StoreBrowseAPI.probe_url()
TEST_PARAMS = None  # 编码已内嵌在 TEST_URL（input_json 查询参数）
# 走线日志的目标标签：TEST_URL 带 input_json 会超长（proxy_events.target 列 255）
TEST_TARGET = "IStoreBrowseService/GetItems"
TEST_TIMEOUT = 12.0


def _steam_payload_ok(resp: httpx.Response) -> bool:
    """生产端点响应是否「业务有效」：200 且 body 是 JSON 对象。

    只判 200 会把风控 / Cloudflare 返回的 HTML 拦截页当成「通」——那正是
    「体检通、生产不通」的一种形态。判 JSON 是最小成本的区分：内容结构再变
    也是 dict（不误杀），HTML / 空体 / 非 JSON 一律判否。
    """
    if resp.status_code != 200:
        return False
    try:
        return isinstance(resp.json(), dict)
    except Exception:  # noqa: BLE001 —— 非 JSON 响应（拦截页 / 空体）
        return False
MAX_CONSECUTIVE_FAILURES = 5  # 连续失败自动禁用

# ── 代理池加权选择（_weighted_pick）的参数 ──
SPEED_REF_MS = int(TEST_TIMEOUT * 1000)  # 速度参照 = 体检超时；比这更慢的一律按最慢算
SPEED_FLOOR = 0.5  # 最慢出口仍保留的速度分（不归零：否则慢节点永不复测、永远翻不了身）
UNTESTED_SPEED = 0.7  # 未测速节点的速度分：中游，够它分到流量被测出来
WEIGHT_SCALE = 100_000  # 权重分辨率，不影响各出口的相对份额

# ── Clash 节点兼容投影与探测节流 ───────────────────────────────
HEALTH_INTERVAL_HOURS = 6  # 体检间隔（本地软件不常驻，跨重启靠库门槛）
SUBSCRIPTION_REFRESH_HOURS = 6  # 订阅重拉间隔（与节点体检同拍，跨重启靠 KV 门槛）
FIRST_CHECK_REUSE_MINUTES = 5  # 启动/切换触发的首检复用窗口：窗口内检测过就沿用账本，不重测
# 失败冷却递增（分钟）：30min → 1h → 2h → 4h → 8h → 24h 封顶
_FAIL_COOLDOWN_MINUTES = [30, 60, 120, 240, 480, 1440]


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


async def clash_nodes_fresh(sub_id: int) -> bool:
    """该订阅的节点账本是否在 FIRST_CHECK_REUSE_MINUTES 内有过检测。

    判据唯一来源是 clash_nodes.last_checked_at（跨重启有效），与 6h 体检
    门槛同一把尺；窗口内账本就是「这条订阅现在能不能用」的现成结论，
    首检不再全量重测。账本为空或结论过期返回 False，首检照跑。
    """
    async with get_session_factory()() as session:
        row = await session.execute(
            select(func.max(ClashNode.last_checked_at)).where(
                ClashNode.subscription_id == sub_id
            )
        )
        latest = row.scalar()
    if latest is None:
        return False
    elapsed = _naive(get_beijing_time_obj()) - latest
    return elapsed < timedelta(minutes=FIRST_CHECK_REUSE_MINUTES)


# 用户最近一次显式选中的 Clash 订阅（点选行 / 切换 / 启动成功都落这里）：
# 内核未运行时跨页面与重启保留点选，启动（含自启）缺省用它，前端回显同源
SELECTED_CLASH_SUB_KEY = "proxy.selected_clash_sub_id"


async def remember_selected_clash_sub(sub_id: int) -> None:
    from app.domains.settings.service import set_value

    await set_value(SELECTED_CLASH_SUB_KEY, int(sub_id))


async def remembered_clash_sub_id() -> int | None:
    from app.domains.settings.service import get_value

    raw = await get_value(SELECTED_CLASH_SUB_KEY, None)
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def proxy_label(p: Proxy | None) -> str:
    return p.label or (f"{p.scheme}://{p.host}:{p.port}") if p else "direct"


# ─── 策略 ────────────────────────────────────────────────────

STRATEGIES = ("proxy_first", "direct_only", "direct_first", "proxy_only")

# 用户显式选过策略的标记：自动切换（订阅导入成功 → 代理）只在「默认直连、
# 用户没有表达过策略偏好」时生效，用户手动选过的策略永不被自动行为覆盖
STRATEGY_EXPLICIT_KEY = "proxy.strategy_explicit"


async def get_strategy() -> dict:
    from app.domains.settings.service import get_value, set_value

    strategy = await get_value("proxy.strategy", None)
    if strategy is None:
        # 默认策略：初次打开即直连模式——作业托管到
        # 本机网络环境（系统代理 / 加速器透明生效，限速闸保底）；导入有效
        # 订阅时自动切代理优先，用户显式选过的策略不被自动行为覆盖
        strategy = "direct_only"
        await set_value("proxy.strategy", strategy)
    if strategy in ("pinned", "clash"):
        # 策略下架：pinned/clash 移除（自启 Verge 由代理优先回落探测
        # 覆盖），存量值一次性迁移，避免静默退化为 proxy_only 语义（无代理即报错）。
        strategy = "proxy_first"
        await set_value("proxy.strategy", strategy)
        from app.domains.settings.service import delete_value

        await delete_value("proxy.pinned_id")

    port_mode, configured_port = await kernel_port_config()
    runtime_status = clash_manager.runtime.status()
    return {
        "strategy": strategy,
        "clashPortMode": port_mode,
        "clashPort": (
            configured_port if port_mode == "fixed"
            else runtime_status.get("port") if runtime_status.get("running") else None
        ),
        "runtimeRunning": bool(runtime_status.get("running")),
        "runtimeEntryReady": bool(runtime_status.get("entryReady")),
        # 内核自启 / 自动节点体检开关（默认开；体检的 force 手动路径不受闸）
        "autostart": bool(await get_value("proxy.autostart", True)),
        "healthAuto": bool(await get_value("proxy.health_auto", True)),
    }


async def set_strategy(
    strategy: str | None = None,
    clash_port: int | None = None,
    autostart: bool | None = None,
    health_auto: bool | None = None,
    clash_port_mode: str | None = None,
) -> None:
    from app.domains.settings.service import get_value, set_value

    if strategy is not None:
        if strategy not in STRATEGIES:
            raise ValueError(f"未知策略: {strategy}")
        await set_value("proxy.strategy", strategy)
        # 显式选择标记：与自动切换（订阅导入）区分——用户亲手选的策略，
        # 自动行为不得覆盖
        if (await get_value(STRATEGY_EXPLICIT_KEY, False)) is not True:
            await set_value(STRATEGY_EXPLICIT_KEY, True)
    old_mode, old_port = await kernel_port_config()
    target_mode = clash_port_mode or ("fixed" if clash_port is not None else old_mode)
    if target_mode not in {"auto", "fixed"}:
        raise ValueError("代理端口模式必须是 auto 或 fixed")
    if target_mode == "fixed":
        target_port = clash_port if clash_port is not None else old_port
        if target_port is None or not 1 <= int(target_port) <= 65535:
            raise ValueError("固定端口必须在 1 到 65535 之间")
        target_port = int(target_port)
        if old_mode != "fixed" or old_port != target_port:
            await _restart_kernel_for_port(target_port)
        await set_value("proxy.clash_port", target_port)
        await set_value("proxy.clash_port_mode", "fixed")
    else:
        await set_value("proxy.clash_port_mode", "auto")
    if autostart is not None:
        await set_value("proxy.autostart", bool(autostart))
    if health_auto is not None:
        await set_value("proxy.health_auto", bool(health_auto))


async def _restart_kernel_for_port(new_port: int) -> None:
    """工作端口变化即生效：校验新配置后再重启现役内核。"""
    from app.core.config import get_settings

    detect = clash_manager.detect_kernel(get_settings().data_dir)
    status = clash_manager.runtime.status()
    if not (detect["found"] and status["running"]):
        return
    if clash_manager.runtime.port == new_port:
        return
    config_path = clash_manager.runtime.config_path
    sub_url = clash_manager.runtime.subscription_url
    if not config_path:
        return
    try:
        outcome = await clash_manager.runtime.ensure_running(
            detect["path"], config_path, subscription_url=sub_url,
            mixed_port=new_port, force_restart=True,
        )
        if not outcome.get("entryReady"):
            raise RuntimeError("代理入口没有成功监听")
        log_event(logger, f"内核工作端口已切换为 {new_port}，已按新端口重启", tag="成功")
    except Exception as exc:  # noqa: BLE001 —— restore the previous working port on failure
        if not clash_manager.runtime.status()["running"]:
            try:
                await clash_manager.runtime.ensure_running(
                    detect["path"], config_path, subscription_url=sub_url,
                    mixed_port=status.get("port"),
                )
            except Exception:
                log_event(
                    logger, "端口切换失败后原代理入口也未能恢复",
                    level=logging.ERROR, exc_info=True,
                )
        log_event(
            logger,
            "内核按新工作端口重启失败",
            level=logging.ERROR,
            detail={"端口": new_port},
        )
        raise ValueError(f"代理端口切换失败：{type(exc).__name__}") from exc


async def maybe_autoswitch_on_subscription(nodes: int | None) -> bool:
    """导入有效订阅后自动切代理优先。

    生效条件三缺一不可：当前是直连（direct_only）+ 节点数确认为有效（>0）+
    用户没有显式选过策略（STRATEGY_EXPLICIT_KEY 未打标）。切换成功返回 True
    （调用方带进订阅保存结果，前端提示）；其余情况静默保持现状。
    """
    if not nodes or int(nodes) <= 0:
        return False
    from app.domains.settings.service import get_value, set_value

    if (await get_value(STRATEGY_EXPLICIT_KEY, False)) is True:
        return False
    if (await get_value("proxy.strategy", "direct_only")) != "direct_only":
        return False
    await set_value("proxy.strategy", "proxy_first")
    return True


async def maybe_revert_to_direct() -> bool:
    """无可用出口时回退默认直连——自动切换的反面对称动作。

    与自动切换同一把尺：只回退依赖代理池的策略（proxy_first/proxy_only），
    用户显式选过的策略不动。出口只看稳定事实（未废弃订阅 / 手动池启用
    节点），不看节点瞬时存活——体检抖动不该把策略永久打回直连。
    """
    from app.domains.settings.service import get_value, set_value

    if (await get_value(STRATEGY_EXPLICIT_KEY, False)) is True:
        return False
    if (await get_value("proxy.strategy", "direct_only")) not in (
        "proxy_first",
        "proxy_only",
    ):
        return False
    async with get_session_factory()() as session:
        live_subs = await session.scalar(
            select(func.count())
            .select_from(ProxySubscription)
            .where(ProxySubscription.deprecated.is_(False))
        )
        if live_subs:
            return False
        enabled_pool = await session.scalar(
            select(func.count()).select_from(Proxy).where(Proxy.enabled.is_(True))
        )
    if enabled_pool:
        return False
    await set_value("proxy.strategy", "direct_only")
    log_event(logger, "无可用代理出口，策略已回退直连", tag="降级")
    return True


async def _tcp_alive(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
        writer.close()
        await writer.wait_closed()
        return True
    except OSError:
        return False


async def kernel_port_config() -> tuple[str, int | None]:
    """Return the migrated runtime port mode and its saved fixed port."""
    from app.domains.settings.service import get_value, set_value
    mode = await get_value("proxy.clash_port_mode", None)
    saved_port = await get_value("proxy.clash_port", None)
    if mode not in {"auto", "fixed"}:
        mode = "fixed" if saved_port is not None else "auto"
        await set_value("proxy.clash_port_mode", mode)
    if saved_port is not None:
        saved_port = int(saved_port)
        if not 1 <= saved_port <= 65535:
            saved_port = None
    if mode == "fixed" and saved_port is None:
        mode = "auto"
        await set_value("proxy.clash_port_mode", mode)
    return mode, saved_port


async def kernel_port_options() -> dict:
    mode, saved_port = await kernel_port_config()
    status = clash_manager.runtime.status()
    if mode == "fixed":
        return {"mixed_port": saved_port}
    if status.get("running") and status.get("port"):
        return {"mixed_port": int(status["port"])}
    return {"mixed_port": None, "allocate_mixed_port": True}


async def pinned_kernel_port() -> int | None:
    mode, saved_port = await kernel_port_config()
    if mode == "fixed":
        return saved_port
    status = clash_manager.runtime.status()
    return int(status["port"]) if status.get("running") and status.get("port") else None


async def _local_fallback_port() -> int | None:
    """Return an external local proxy only after its listener and egress respond."""
    for port in clash_manager.LOCAL_MIXED_PORTS:
        proxy_url = f"http://127.0.0.1:{port}"
        if (
            await _tcp_alive("127.0.0.1", port)
            and await asyncio.to_thread(clash_manager.local_channel_egress_ok, proxy_url)
        ):
            return port
    return None


# ─── 直连形态的系统代理解析 ─────────────────────────────────
#
# aiohttp 不读 Windows 注册表系统代理（trust_env 默认关），纯系统代理型
# 本地工具（Clash Verge 等）的直连模式原本只有 TUN 接管才生效。这里用
# urllib.getproxies()（环境变量优先 → 注册表 ProxyEnable/ProxyServer，与
# 浏览器同源）把系统代理找出来，TCP 探活通过才算数。


_system_proxy_cache: tuple[float, str | None] | None = None


def _system_proxy_candidates() -> list[str]:
    """系统代理候选（https 优先，仅 http/https 形态——socks 需额外依赖不支持）。"""
    import urllib.request

    try:
        proxies = urllib.request.getproxies()
    except Exception:  # noqa: BLE001 —— 读取失败等同无系统代理
        return []
    out: list[str] = []
    for key in ("https", "http"):
        raw = proxies.get(key)
        if not raw:
            continue
        url = raw if "://" in raw else f"http://{raw}"
        if url.startswith(("http://", "https://")) and url not in out:
            out.append(url)
    return out


async def resolve_system_proxy_url() -> str | None:
    """直连形态的本机出口补全：可用的 Windows 系统代理 URL，无则 None（裸直连）。

    探活只验证代理端口在监听；能否通 Steam 由调用方超时/重试兜底。
    30s 缓存——钱包/提醒等高频调用方不反复探活。
    """
    global _system_proxy_cache

    now = time.monotonic()
    if _system_proxy_cache and now - _system_proxy_cache[0] < 30:
        return _system_proxy_cache[1]

    resolved: str | None = None
    from urllib.parse import urlsplit

    for url in _system_proxy_candidates():
        split = urlsplit(url)
        port = split.port
        if split.hostname is None or port is None:
            continue
        if await _tcp_alive(split.hostname, port):
            resolved = url
            break

    _system_proxy_cache = (now, resolved)
    return resolved


async def strategy_proxy_or_direct() -> str | None:
    """供业务域取用的策略代理：引擎不可用时按直连处理。

    与 `resolve_proxy_url` 的差别只在失败语义：本入口吞掉解析异常返回 None，
    用于「拿不到代理也要把这次同步跑完」的后台链路。
    """
    try:
        return await resolve_proxy_url()
    except Exception:  # noqa: BLE001 — 后台同步失败不改写为直连以外的语义
        return None


async def resolve_proxy_url() -> str | None:
    """策略引擎：为本次任务解析代理 URL。None = 直连。

    若策略要求代理但无可用代理，抛 RuntimeError（调用方转 400）。
    """
    from app.domains.settings.service import get_value

    strategy = await get_value("proxy.strategy", "direct_only")

    if strategy == "direct_only":
        # 直连 = 托管用户本机网络环境：TUN 型加速器裸连即走；系统代理型
        # （Clash Verge 等）由 resolve_system_proxy_url 找到并显式挂上
        return await resolve_system_proxy_url()

    if strategy == "direct_first":
        return None

    if strategy == "proxy_first":
        # 代理优先（2026-09 起默认）：Clash 内核在跑 → 内核端口（工作端口，
        # 网络页配置的那个）；代理池有启用节点 → 轮询；都没有 → 探用户自启
        # Verge 的兜底口（7897，与工作端口设置无关）；仍无 → 直连。
        status = clash_manager.runtime.status()
        # 入口硬闸：进程活着但混合端口没在听（订阅没写端口/端口被占）时
        # 交出去的地址必然 ConnectError，直接落到后续通道
        if status["running"] and status.get("port") and clash_manager.runtime.entry_listening():
            return f"http://127.0.0.1:{status['port']}"
        enabled = await list_proxies(enabled_only=True)
        if enabled:
            return await _weighted_pick(enabled)
        fallback = await _local_fallback_port()
        if fallback:
            return f"http://127.0.0.1:{fallback}"
        return None

    # proxy_only: 加权随机（延迟+健康反馈到流量分配；rr_index 顺序轮询兜底）
    enabled = await list_proxies(enabled_only=True)
    if not enabled:
        raise RuntimeError(f"策略 {strategy} 需要至少一条启用的代理")
    return await _weighted_pick(enabled)



async def _round_robin(enabled: list[Proxy]) -> str:
    """代理池轮询取一条（rr_index 存 app_settings）。"""
    from app.domains.settings.service import get_value, set_value

    idx = int(await get_value("proxy.rr_index", 0) or 0)
    chosen = enabled[idx % len(enabled)]
    await set_value("proxy.rr_index", idx + 1)
    return _runtime_url(chosen)


async def _weighted_pick(enabled: list[Proxy]) -> str:
    """按健康度加权随机取一条出口（策略引擎每批任务调用一次）。

    加权而非朴素轮询：轮询把已失败的出口和健康出口当等价，流量会继续平摊
    到死节点上，直到它撞满禁用线——每次都先白烧几个请求才发现。加权让健康度
    直接决定流量分配：快节点多干活，正在降级的节点被迅速挤出。

    权重 = **速度分 × 健康分**，两个分都是 [0,1]，相乘后放大成整数权重。
    拆成两个因子而不是拼成一个式子，因为两者性质不同：速度是「能多快」的连续
    量，健康是「还值不值得信」的判断——一个刚连败 3 次的节点再快也不该按快节点
    分流量。所以健康分是主控项（可归零、指数衰减），速度分只在健康分内部调节。

    - **速度分**：以体检超时 `TEST_TIMEOUT` 为参照，越慢越低，但保留 `SPEED_FLOOR`
      的底线。不归零是有意的：拿不到流量的节点永远不会被复测，也就永远翻不了身。
      未测速的新节点给 `UNTESTED_SPEED`（中游），同样是为了让它持续分到流量、
      尽快被测出来，而不是在取样之前就被判死。
    - **健康分**：连续失败 ≥ `MAX_CONSECUTIVE_FAILURES` 直接 0——禁用线与体检的
      自动禁用取同一个常量，一处判定两处一致，不会出现「权重说不能用、enable
      说能用」。未到线的按 2^-失败次数 衰减，留翻身机会。

    权重全为 0（整池皆死）时退回顺序轮询：策略引擎宁可拿一个可能失败的出口
    去试，也不要在这里抛异常——「没有可用出口」的判断留给上层。

    proxies 表的 latency/status/consecutive_failures 由体检（手动/定时）持续
    维护，本函数只消费、不修改。
    """
    import random

    def _speed(p: Proxy) -> float:
        """速度分 ∈ [SPEED_FLOOR, 1]：以体检超时为参照，越慢越低。"""
        if not p.latency_ms:
            return UNTESTED_SPEED
        slow = min(p.latency_ms, SPEED_REF_MS) / SPEED_REF_MS
        return SPEED_FLOOR + (1.0 - SPEED_FLOOR) * (1.0 - slow)

    def _health(p: Proxy) -> float:
        """健康分 ∈ [0, 1]：0 = 不可用，指数衰减 = 降级中，1 = 正常。"""
        fails = p.consecutive_failures or 0
        if fails >= MAX_CONSECUTIVE_FAILURES:
            return 0.0
        if fails == 0 and p.status != "failed":
            return 1.0
        # 有失败计数、或被人工标成 failed 的，按 2^-失败次数 降权（1/2, 1/4, 1/8…）。
        # 手动标 failed 但计数为 0 时按 1 次失败算，否则人工拉黑的出口权重照旧。
        return 2.0 ** -max(fails, 1)

    scores = [_speed(p) * _health(p) for p in enabled]
    if sum(scores) <= 0:
        return await _round_robin(enabled)
    # 健康分为 0 的出口权重精确为 0（不被下限抬起来），其余最低也有
    # SPEED_FLOOR × 2^-(MAX-1) × WEIGHT_SCALE，不会因取整掉到 0
    weights = [round(s * WEIGHT_SCALE) for s in scores]
    chosen = random.choices(enabled, weights=weights, k=1)[0]
    return _runtime_url(chosen)


def _runtime_url(proxy: Proxy) -> str:
    """库内对象 → 出网用 URL（账号口令在此处解密）。

    唯一解密点：任何需要真出网的地方都从这里取 URL，库内与响应体保持密文
    或掩码。代理无账号口令时不会触碰密钥层（避免无谓的解封开销）。
    """
    if not proxy.username:
        return f"{proxy.scheme}://{proxy.host}:{proxy.port}"
    from app.domains.proxies.credentials import open_value

    user = open_value(proxy.username)
    secret = open_value(proxy.password)
    if not user:
        # 密钥不可用（换机/拷库）：退化成无认证 URL，让请求自然失败，
        # 而不是拿密文当口令发出去。
        return f"{proxy.scheme}://{proxy.host}:{proxy.port}"
    auth = f"{quote(user, safe='')}:{quote(secret, safe='')}@"
    return f"{proxy.scheme}://{auth}{proxy.host}:{proxy.port}"


async def _saved_proxy_candidates(limit: int = 3) -> list[str]:
    """订阅拉取的借道出口：项目自己保存的可用代理（proxies 表启用项）。

    只在**直连失败后**才轮到它们（通道链里排在直连之后，见
    clash_manager._download_attempts）。取多条而不是一条：池里的单条代理
    随时可能自己失效，一次只试一条等于把「借道」变成掷骰子；加权随机
    天然倾向健康节点，重复抽到同一条不影响正确性（去重后可能少于 limit）。

    这里**只**返回代理池——内核端口 / 本地混合端口由通道链自行探测补位，
    不在本函数重复解析（也避免拉取路径多读一次设置库）。
    池空返回空表（调用方即「直连之外没有可借道的池代理」）。
    """
    out: list[str] = []
    seen: set[str] = set()
    try:
        enabled = await list_proxies(enabled_only=True)
    except Exception:  # noqa: BLE001 —— 池不可用按空池处理，不拖垮拉取
        enabled = []
    for _ in range(limit):
        if not enabled:
            break
        try:
            pick = await _weighted_pick(enabled)
        except Exception:  # noqa: BLE001
            break
        if pick not in seen:
            seen.add(pick)
            out.append(pick)
    return out


# ─── 名称三档链（用户改名 > 机场品牌名 > 缓存上次值）────────────
#
# 事实源唯一性：`label` 列始终是「当前生效名称」，`last_stats.labelSource`
# 只记这个名字**从哪来**（user=用户真改过 / panel=机场给的），不另存一份名字。
# 这样「用户改过」这个事实有唯一记录者，且不需要新增列。

LABEL_SOURCE_USER = "user"
LABEL_SOURCE_PANEL = "panel"


def _label_source(sub: ProxySubscription) -> str | None:
    """本行名字的来源（user / panel / None=还没有名字）。"""
    stats = sub.last_stats if isinstance(sub.last_stats, dict) else {}
    source = stats.get("labelSource")
    return source if source in (LABEL_SOURCE_USER, LABEL_SOURCE_PANEL) else None


async def apply_subscription_name_chain(sub_id: int, panel_name: str | None) -> str | None:
    """按三档链决定订阅名（返回最终生效名称，无可用来源返回 None）。

    优先级（高 → 低）：
      ① 用户真手改过的名字 —— 永不覆盖（机场改名也不动它）；
      ② 本次拉到的机场品牌名 —— 用户没改过时用它，且**可随机场改名而更新**；
      ③ 缓存/库中上次的名字 —— 前两档都拿不到时兜底。

    「机场名可更新」是关键：用户没改过时，机场把自己改名了，界面应跟着变；
    只有用户亲手动过，这个名字才被锁住。来源标记落 `last_stats.labelSource`，
    与名字本体同在一条账上，不新增平行记录。

    无标记的历史行（老版本自动取的名）按「名字是否等于机场当前给的值」归位：
    相等即视为机场名（可更新），不等即视为用户动过（锁死），无需迁移脚本。
    """
    name = (panel_name or "").strip()[:100]
    async with write_session(WritePriority.INTERACTIVE) as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return None

        current = sub.label
        source = _label_source(sub)

        # ① 用户命名：锁死，任何机场名都不许覆盖。判定分两种情形——
        #    a) 有 user 标记：用户手动改过名（含改成别的再改回来）；
        #    b) 无任何标记但有名字：老版本自动取的名（旧代码没留来源）或
        #       添加订阅时用户自己填的。这两种靠名字是否等于「机场当前给的名字」
        #       区分：相等 → 就是机场给的，不算用户命名（可跟随机场改名更新）；
        #       不等 → 用户动过，锁死。旧数据因此自动归位，无需迁移脚本。
        if current and source == LABEL_SOURCE_USER:
            return current
        if current and source is None and (not name or name != current):
            return current

        # ② 本次机场名：用户没改过就用它（含机场自己改名的情况）
        if name and name != current:
            sub.label = name
            stats = dict(sub.last_stats or {})
            stats["labelSource"] = LABEL_SOURCE_PANEL
            sub.last_stats = stats
            await session.commit()
            return name

        # ②' 名字与本次机场名一致：补上 panel 标记（历史行归位）。
        # 不补的话这些行永远是「无标记」，下次机场改名时又会被当成用户命名而锁死。
        if name and name == current and source != LABEL_SOURCE_PANEL:
            stats = dict(sub.last_stats or {})
            stats["labelSource"] = LABEL_SOURCE_PANEL
            sub.last_stats = stats
            await session.commit()
        return current


async def update_subscription_label(sub_id: int, label: str) -> dict:
    """订阅手动改名。

    **只有真正改动才算用户命名**：新旧值相同（打开窗口没碰名字就保存）
    不打 user 来源标记——否则任何一次「路过保存」都会把机场名永久冻死，
    此后再不随机场改名而更新。判据是「值变了没有」，不是「保存按了没有」。
    """
    label = (label or "").strip()[:100]
    async with write_session(WritePriority.INTERACTIVE) as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            raise ValueError("订阅不存在")
        changed = label != (sub.label or "")
        sub.label = label or None
        if changed:
            stats = dict(sub.last_stats or {})
            stats["labelSource"] = LABEL_SOURCE_USER if label else None
            sub.last_stats = stats
        await session.commit()
        return {
            "id": sub.id, "kind": sub.kind,
            "url": mask_url(open_url(sub.url)), "label": sub.label,
        }


async def update_subscription(
    sub_id: int, label: str | None = None, url: str | None = None,
    auto_refresh: bool | None = None,
) -> dict:
    """编辑订阅：改名 + 换链接 + 自动更新开关；链接变更的 clash 订阅**保存即自动重拉**。

    链接是订阅的唯一身份——换了链接等于换了一个机场，磁盘上的 config.yaml
    与 clash_nodes 账本都还属于旧链接，不重拉就一直对不上。重拉通道同
    「重拉」按钮：直连优先，失败自动借道项目保存的可用代理（见
    _saved_proxy_candidates）。

    重拉失败**不回滚**改名换链：名字和链接是用户刚确认的输入，重拉失败
    是网络的一次抖动，把用户的输入撤掉只会让他再敲一遍。失败以 warning
    回传，由前端提示可稍后手动重拉。

    返回 {id, kind, url, label, synced, nodes?, traffic?, alive?, total?,
    restarted?, warning?}；synced=true 表示本次确实重拉过。
    """
    from app.domains.proxypool.bootstrap import subscription_sync_lock

    async with subscription_sync_lock(sub_id):
        async with write_session(WritePriority.INTERACTIVE) as session:
            sub = await session.get(ProxySubscription, sub_id)
            if sub is None:
                raise ValueError("订阅不存在")
            if label is not None:
                sub.label = (label or "").strip() or None
            if auto_refresh is not None:
                sub.auto_refresh = bool(auto_refresh)
            url_changed = False
            if url is not None:
                new_url = (url or "").strip()
                if not new_url.lower().startswith(("http://", "https://")):
                    raise ValueError("订阅链接必须是 http(s) URL")
                if new_url != open_url(sub.url):
                    if sub.kind == "clash":
                        from app.domains.proxypool.subscription import subscription_url_identity

                        previous_url = open_url(sub.url)
                        sub.cache_url_identity = (
                            subscription_url_identity(previous_url) if previous_url else ""
                        )
                    sub.url = seal_url(new_url)
                    url_changed = True
            await session.commit()
            result: dict = {
                "id": sub.id, "kind": sub.kind, "url": mask_url(open_url(sub.url)),
                "label": sub.label, "synced": False,
                "autoRefresh": bool(sub.auto_refresh)
                if sub.auto_refresh is not None else True,
            }
        if url_changed and result["kind"] == "clash":
            from app.core.config import get_settings

            clash_manager.invalidate_subscription_cache_identity(
                get_settings().data_dir, sub_id,
            )
    if url_changed and result["kind"] == "clash":
        try:
            sync = await refresh_clash_subscription(sub_id, auto_switch=True)
        except Exception as e:  # noqa: BLE001 —— 重拉失败不撤销改名换链
            result["warning"] = f"订阅链接已更新，但自动重拉失败：{e}"
            return result
        result.update({
            "synced": True,
            "label": sync.get("label", result["label"]),
            "nodes": sync.get("nodes"),
            "traffic": sync.get("traffic"),
            "alive": sync.get("alive", 0),
            "total": sync.get("total", 0),
            "restarted": sync.get("restarted", False),
        })
    return result


async def refresh_subscription_traffic(sub_id: int) -> dict:
    """流量统计实时回填：轻量拉订阅响应头 subscription-userinfo。

    面板流量随消耗实时增长，仅在内核启动时落库会滞后数十 GB（曾见
    315 GB 停滞 vs 实际 318）。只更新 lastStats.traffic，不动
    config.yaml、不重启内核；失败抛 ValueError 由路由转 400/502。
    """
    sub = await get_subscription(sub_id)
    if sub is None:
        raise ValueError("订阅不存在")
    if sub.kind != "clash":
        raise ValueError("该订阅不是 Clash 订阅（面板流量头仅机场订阅有）")
    result = await refresh_clash_subscription(
        sub_id, _require_fresh_traffic=True
    )
    return {"id": sub_id, "traffic": result["traffic"]}


async def _mark_refreshed(sub_id: int) -> None:
    """重拉成功留痕：订阅行 last_imported_at（前端「上次拉取」口径）+ KV 门槛。

    KV 门槛（proxy.sub_last_refresh_at）是定时重拉的间隔依据，手动重拉同样
    计数——刚手动拉过就不必再来一次自动拉。缓存回退（下载全败）不算成功，
    门槛不被消费，下一拍继续试。
    """
    from app.domains.settings.service import set_value

    now = _naive(get_beijing_time_obj())
    async with write_session(WritePriority.BACKGROUND) as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is not None:
            sub.last_imported_at = now
            await session.commit()
    await set_value("proxy.sub_last_refresh_at", now.isoformat())


async def refresh_clash_subscription(
    sub_id: int, *, auto_switch: bool = False, _include_sync: bool = False,
    _require_fresh_traffic: bool = False,
) -> dict:
    """独立刷新 Clash 订阅：重新下载配置 + 流量回填 + 账本收敛。

    不依赖「重启内核」——服务常驻期间订阅内容也能刷新。下载成功后：
    - 内核在跑且配置有变化 → 重启生效（节点增删立即反映）
    - 内核没跑 → 只下载不启动（下次启动自然用新配置）
    存活统计落库：lastStats.alive/total 用账本 + 当前配置节点名口径。
    成功后写「上次拉取」留痕（订阅行时间戳 + 定时重拉门槛 KV，见
    _mark_refreshed）；缓存回退（全部通道下载失败）不算成功。

    通道：**直连优先**，直连失败自动改用项目保存的可用代理（_saved_proxy_candidates）。
    名称走三档链（用户真改过 > 本次机场名 > 缓存上次值），见
    `apply_subscription_name_chain`——用户没改过时机场改名会跟过来。
    """
    sub = await get_subscription(sub_id)
    if sub is None:
        raise ValueError("订阅不存在")
    if sub.kind != "clash":
        raise ValueError("该订阅不是 Clash 订阅（kind=clash）")
    sub_url = open_url(sub.url)
    if not sub_url:
        raise ValueError(UNREADABLE_MESSAGE)
    label = sub.label
    from app.core.config import get_settings

    settings = get_settings()
    try:
        meta = await clash_manager.runtime.download_subscription(
            sub_url, settings.data_dir, await _saved_proxy_candidates(),
            target_path=clash_manager.subscription_config_path(settings.data_dir, sub_id),
        )
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"订阅下载失败: {e}") from e

    if _require_fresh_traffic and (meta.get("cached") or not meta.get("userinfo")):
        raise ValueError("面板未返回在线流量头（subscription-userinfo）；已保留上次记录")

    # 流量/节点数一并回填（下载响应里就有；失败不阻塞刷新）
    fields: dict = {
        "nodes": meta.get("nodes"),
        "cached": meta.get("cached", False),
    }
    # 在线头优先；缓存回退没有新流量时沿用上次记录。
    traffic = meta.get("userinfo")
    traffic = await _resolve_traffic(sub_id, traffic)
    if traffic:
        fields["traffic"] = traffic

    # 账本收敛：以新下载内容为准，已下线/改名节点的旧行删除
    current_names = set(clash_manager.parse_node_names(
        Path(meta["path"]).read_text(encoding="utf-8", errors="ignore")
    ))
    pruned = 0
    if current_names:
        pruned = await prune_clash_node_ledger(sub_id, current_names)

        # 存活统计（账本 × 当前内容）：新下载节点无账本 → 从 0 起算
        alive = total = 0
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(ClashNode).where(ClashNode.subscription_id == sub_id)
                )
            ).scalars().all()
        for row in rows:
            if row.name in current_names:
                total += 1
                if row.status == "ok":
                    alive += 1
        fields["alive"] = alive
        fields["total"] = total
    await _merge_last_stats(sub_id, **fields)

    # 名称三档链（用户改名 > 本次机场名 > 缓存上次值）：无条件走一次，
    # 由链自己按来源决定用哪个——用户没改过时机场改名要能更新过来，
    # 只有用户亲手动过的名字才锁死。降级时 title 来自响应头 sidecar。
    label = await apply_subscription_name_chain(sub_id, meta.get("title")) or label

    # 内核在跑且跑的就是这条订阅：配置变化自动重启生效；跑的是别的订阅
    # 或没在跑 → 只下载不重启（避免共用缓存路径把内核悄悄切到别的订阅）
    # restarted 字段语义 = 「新配置已生效」：热重载（内核不动）或进程重启都算
    restarted = False
    detect = clash_manager.detect_kernel(settings.data_dir)
    if clash_manager.runtime.running_subscription_is(sub_url) and detect["found"]:
        # 生命周期变更与节点探测互斥（同手动切换订阅的持锁做法）：热重载或
        # stop/start 切断在途探测的 lane 连接，会把节点误记失败写进账本
        async with _clash_test_lock():
            try:
                # 刷新后重新渲染运行配置：交给内核的始终是生成结果，地区规则
                # 不会被这次刷新重新带回内核
                runtime_path = clash_manager.materialize_subscription_config(
                    Path(meta["path"])
                )
                port_options = await kernel_port_options()
                outcome = await clash_manager.runtime.ensure_running(
                    detect["path"], str(runtime_path), subscription_url=sub_url,
                    **port_options,
                )
                restarted = bool(outcome.get("reloaded") or outcome.get("started"))
            except Exception:  # noqa: BLE001 —— 更新失败保留旧内核运行
                log_event(
                    logger,
                    "订阅刷新时内核配置更新失败，沿用运行中的内核实例",
                    level=logging.ERROR,
                    exc_info=True,
                )
    if not meta.get("cached"):
        await _mark_refreshed(sub_id)

    result = {
        "id": sub_id,
        "label": label,
        "nodes": meta.get("nodes"),
        "traffic": fields.get("traffic"),
        "alive": fields.get("alive", 0),
        "total": fields.get("total", 0),
        "usedCache": meta.get("cached", False),
        "restarted": restarted,
        "prunedLedger": pruned,
    }
    if _include_sync:
        result["_syncResult"] = meta.get("_sync_result")
    # 用户主动重拉/换链接也算一次「订阅有效」的表示：默认直连且没表达过
    # 偏好时切代理优先。定时周期重拉不传 auto_switch——自动行为不该由
    # 后台节拍翻转用户当前的出口形态。
    if auto_switch and await maybe_autoswitch_on_subscription(meta.get("nodes")):
        result["strategySwitched"] = True
    return result


async def resolve_startable_clash(
    data_dir: Path, requested_id: int | None = None, *, snapshot_only: bool = False,
) -> dict:
    """挑一条「现在就能把内核拉起来」的 Clash 订阅，返回其配置文件路径。

    候选顺序：请求的那条在前，其余非废弃 Clash 订阅按新→旧跟后；无显式
    请求时，「用户最近一次显式选中」（落库 KV，点选/切换/启动成功都会写）
    按请求对待。每条先在线下载（download_subscription 内部自带「全部通道
    失败回退该订阅本地缓存」）。

    **在线取到的配置优先于本地缓存回退**：缓存命中只代表曾经下载成功过，不代表
    这条订阅现在活着。无指定订阅时把缓存回退的候选留作兜底继续往下找，否则内核会
    被一条已失效订阅的旧缓存粘住——在线能取到的订阅永远轮不到，定时重拉随后也
    只会反复读这份旧缓存。显式指定的订阅（requested_id）尊重用户选择，命中即返回。

    一条候选被淘汰当且仅当**既下载不了、本地又没有它的缓存**——订阅链接失效不该
    连累内核起不来，节点好不好交给体检与废弃判定。
    返回 {subscription, configPath, title, userinfo, nodes, cached, attempts}，
    attempts 记录被淘汰候选的失败原因；全军覆没抛 ValueError（文案面向
    用户，聚合各候选原因）。
    """
    subs = [s for s in await list_subscriptions("clash", reveal=True) if not s["deprecated"]]
    if not subs:
        raise ValueError(
            "没有可用的 Clash 订阅（全部已废弃或尚未添加），"
            "请在订阅区手动删除或更换订阅"
        )
    if requested_id is None:
        # 无显式请求时吃落库的「用户最近一次显式选中」（点选/切换/启动成功
        # 都会写）；那条已被删除则自然落空，按原候选序兜底
        requested_id = await remembered_clash_sub_id()
    if requested_id is not None:
        ordered = [s for s in subs if s["id"] == requested_id]
        ordered += [s for s in reversed(subs) if s["id"] != requested_id]
    else:
        ordered = list(reversed(subs))
    # 无指定订阅时允许"缓存只作兜底"；显式指定则命中即用（尊重用户选择）
    defer_cached = requested_id is None
    proxy_candidates = await _saved_proxy_candidates()
    attempts: list[dict] = []
    cached_fallback: dict | None = None
    for sub in ordered:
        cache_path = clash_manager.subscription_config_path(data_dir, sub["id"])
        try:
            download_options = {"target_path": cache_path}
            if snapshot_only:
                download_options["snapshot_only"] = True
            meta = await clash_manager.runtime.download_subscription(
                sub["url"], data_dir, proxy_candidates, **download_options,
            )
            # 交给内核的是**项目生成的运行配置**，不是机场原文：原文留作来源
            # 证据与离线缓存（cache_path），渲染失败（如依赖残缺）按本候选不可用
            # 处理，继续找下一个候选。
            runtime_path = clash_manager.materialize_subscription_config(
                Path(meta["path"])
            )
        except Exception as e:  # noqa: BLE001 —— 单条候选不可用，换下一条
            attempts.append(
                {"id": sub["id"], "label": sub["label"], "error": _brief_error(e)}
            )
            log_event(
                logger,
                f"启动候选订阅 {sub['id']} 不可用，换下一个候选",
                level=logging.WARNING,
                detail={
                    "订阅编号": sub["id"],
                    "名称": sub["label"],
                    "订阅": sub["url"],
                    "原因": str(e),
                },
            )
            continue
        entry = {
            "subscription": sub,
            "configPath": str(runtime_path),
            "rawPath": meta["path"],
            "title": meta.get("title"),
            "userinfo": meta.get("userinfo"),
            "nodes": meta.get("nodes"),
            "cached": bool(meta.get("cached", False)),
            "attempts": attempts,
        }
        if defer_cached and entry["cached"]:
            if cached_fallback is None:
                cached_fallback = entry
                log_event(
                    logger,
                    f"启动候选订阅 {sub['id']} 只能读到本地缓存，留作兜底并继续找能在线取到的候选",
                    detail={
                        "订阅编号": sub["id"],
                        "名称": sub["label"],
                        "订阅": sub["url"],
                    },
                )
            continue
        return entry
    if cached_fallback is not None:
        return cached_fallback
    detail = "；".join(
        f"{a['label'] or ('id=' + str(a['id']))}：{a['error']}" for a in attempts
    )
    raise ValueError(f"所有可用订阅都取不到配置（{detail}），请更换订阅链接后重试")


def _brief_error(exc: Exception) -> str:
    """给用户看的失败原因一句话：保留状态码/关键信息，剥掉多行 traceback 尾巴。"""
    text = " ".join(str(exc).split())
    return text if len(text) <= 160 else text[:157] + "…"


async def maybe_refresh_active_clash_subscription() -> dict:
    """定时重拉「内核正在跑的」Clash 订阅（间隔门槛 SUBSCRIPTION_REFRESH_HOURS）。

    定时重拉的必要性：机场节点列表会增删/改名/换入口，本地 config.yaml 不
    重拉就一直是旧节点集——6h 体检也只是反复测这批旧节点，新节点永远进不来。

    只重拉正在跑的那条：
    - 没在跑的订阅没有流量走它，重拉只是白耗一次外网请求（下次启动内核时
      本来就会重下）；
    - config.yaml 是各订阅共用的缓存文件，重拉非在跑订阅会把这个文件换成
      别家内容，与运行中的内核状态对不上。

    「正在跑哪条」的判据只有一条：内核启动时登记的订阅 URL
    （running_subscription_is）——共用缓存文件本身会被后续任何一次下载覆盖，
    文件名/时间戳都不能作为依据。判定不出来就跳过，不猜、不动。

    门槛（proxy.sub_last_refresh_at）由重拉成功时写（见 _mark_refreshed），
    失败不消费——下一拍继续试。

    返回 {state, subscriptionId, nodes, restarted}，state 取值：
    clash_not_running / throttled / no_subscription / unknown_subscription /
    refreshed / failed（非 refreshed 时后三者无意义）。restarted=true 表示
    新配置已生效（热重载或重启，内核进程可能没动）——新节点在账本里还是
    空行，调用方应接一次节点检测，
    否则「存活 x/y」与仪表盘可用数会停在账本口径等下个 6h 窗口。
    """
    from app.domains.settings.service import get_value

    skip = {"subscriptionId": None, "nodes": None, "restarted": False}
    status = clash_manager.runtime.status()
    if not status["running"]:
        return {"state": "clash_not_running", **skip}
    last = await get_value("proxy.sub_last_refresh_at")
    if last:
        try:
            elapsed = _naive(get_beijing_time_obj()) - datetime.fromisoformat(str(last))
        except ValueError:
            elapsed = None
        if elapsed is not None and elapsed < timedelta(hours=SUBSCRIPTION_REFRESH_HOURS):
            return {"state": "throttled", **skip}

    usable = [s for s in await list_subscriptions("clash", reveal=True) if not s["deprecated"]]
    if not usable:
        return {"state": "no_subscription", **skip}
    # 从新到旧找「URL 出现在启动文本里」的那条（与「最近一条」默认序一致）
    sub = next(
        (
            s
            for s in reversed(usable)
            if clash_manager.runtime.running_subscription_is(s["url"])
        ),
        None,
    )
    if sub is None:
        log_event(
            logger,
            "无法确认内核在跑哪条订阅，跳过本次重拉（可手动重拉）",
            tag="跳过",
        )
        return {"state": "unknown_subscription", **skip}
    try:
        result = await refresh_clash_subscription(sub["id"], _include_sync=True)
    except Exception as e:  # noqa: BLE001 —— 失败不消费门槛，下一拍重试
        log_event(
            logger,
            f"订阅 {sub['id']} 重拉失败，下一拍再试",
            level=logging.WARNING,
            detail={"订阅编号": sub["id"], "原因": str(e)},
        )
        return {"state": "failed", **skip}
    log_event(
        logger,
        f"订阅 {sub['id']} 重拉完成：{result.get('nodes')} 个节点"
        + ("，内核已重启生效" if result.get("restarted") else "，配置无变化、内核沿用"),
        tag="成功",
        detail={
            "订阅编号": sub["id"],
            "节点数": result.get("nodes"),
            "内核已生效": "是" if result.get("restarted") else "否",
        },
    )
    return {
        "state": "refreshed",
        "subscriptionId": sub["id"],
        "nodes": result.get("nodes"),
        "restarted": bool(result.get("restarted")),
        "_syncResult": result.get("_syncResult"),
    }


# ─── CRUD ────────────────────────────────────────────────────

async def list_proxies(enabled_only: bool = False) -> list[Proxy]:
    query = select(Proxy).order_by(Proxy.id)
    if enabled_only:
        query = query.where(Proxy.enabled.is_(True))
    async with get_session_factory()() as session:
        return list((await session.execute(query)).scalars())


def _proxy_dict(p: Proxy, mask_auth: bool = True) -> dict:
    """对外投影：账号口令一律掩码，只有 `hasAuth` 说明是否配置过。

    库内是密文、出网才解密；响应体永远不出现明文（连「解出来再掩码」
    都不做——没有解密就没有泄露面）。
    """
    return {
        "id": p.id,
        "label": p.label,
        "scheme": p.scheme,
        "host": p.host,
        "port": p.port,
        "hasAuth": bool(p.username),
        "url": (
            f"{p.scheme}://{p.host}:{p.port}"
            if not p.username
            else f"{p.scheme}://***@{p.host}:{p.port}"
        ),
        "enabled": p.enabled,
        "status": p.status,
        "latencyMs": p.latency_ms,
        "consecutiveFailures": p.consecutive_failures,
        "lastCheckedAt": p.last_checked_at.isoformat() if p.last_checked_at else None,
    }


async def add_proxy(
    url: str | None = None,
    *,
    scheme: str = "http",
    host: str = "",
    port: int = 0,
    username: str | None = None,
    password: str | None = None,
    label: str | None = None,
) -> dict:
    if url:
        parsed = _parse_proxy_url(url)
        scheme, host, port, username, password = parsed
    if not host or not port:
        raise ValueError("host/port 不能为空")
    if scheme not in ("http", "socks5"):
        raise ValueError(f"不支持的协议: {scheme}")
    async with write_session(WritePriority.INTERACTIVE) as session:
        proxy = Proxy(
            label=label, scheme=scheme, host=host, port=int(port),
            username=username, password=password, enabled=True,
            created_at=_naive(get_beijing_time_obj()),
        )
        credentials.seal_proxy(proxy)
        session.add(proxy)
        await session.commit()
        return _proxy_dict(proxy)


def _parse_proxy_url(url: str) -> tuple[str, str, int, str | None, str | None]:
    """解析 scheme://user:pass@host:port 或 host:port。"""
    from urllib.parse import urlparse

    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"无法解析代理地址: {url}")
    scheme = parsed.scheme.lower()
    if scheme == "https":
        scheme = "http"
    return (
        scheme,
        parsed.hostname,
        parsed.port,
        parsed.username,
        parsed.password,
    )


async def update_proxy(proxy_id: int, *, enabled: bool | None = None, label: str | None = None) -> dict:
    async with write_session(WritePriority.INTERACTIVE) as session:
        proxy = await session.get(Proxy, proxy_id)
        if proxy is None:
            raise ValueError("代理不存在")
        if enabled is not None:
            proxy.enabled = enabled
            proxy.consecutive_failures = 0
        if label is not None:
            proxy.label = label
        await session.commit()
        return _proxy_dict(proxy)


async def delete_proxy(proxy_id: int) -> bool:
    async with write_session(WritePriority.INTERACTIVE) as session:
        proxy = await session.get(Proxy, proxy_id)
        if proxy is None:
            return False
        await session.delete(proxy)
        await session.commit()
        return True


# ─── 健康检查 ────────────────────────────────────────────────

async def test_proxy(proxy_id: int) -> dict:
    async with write_session(WritePriority.INTERACTIVE) as session:
        proxy = await session.get(Proxy, proxy_id)
        if proxy is None:
            raise ValueError("代理不存在")
        result = await _check(proxy)
        await session.commit()
        return {**_proxy_dict(proxy), "testError": result[2]}


async def test_all() -> list[dict]:
    proxies = await list_proxies()
    if not proxies:
        return []
    results = await asyncio.gather(
        *[_check_single(p) for p in proxies], return_exceptions=True
    )
    return [r for r in results if isinstance(r, dict)]


async def _check_single(proxy: Proxy) -> dict:
    async with write_session(WritePriority.BACKGROUND) as session:
        fresh = await session.get(Proxy, proxy.id)
        result = await _check(fresh)
        await session.commit()
        return {**_proxy_dict(fresh), "testError": result[2]}


async def _check(proxy: Proxy) -> tuple[str, int | None, str | None]:
    """对生产抓取端点测延迟。返回 (status, latency_ms, error)。"""
    started = time.monotonic()
    status, latency, error = "failed", None, None
    try:
        async with httpx.AsyncClient(
            timeout=TEST_TIMEOUT, proxy=_runtime_url(proxy)
        ) as client:
            resp = await client.get(TEST_URL, params=TEST_PARAMS)
            latency = int((time.monotonic() - started) * 1000)
            if _steam_payload_ok(resp):
                status = "ok"
                proxy.consecutive_failures = 0
            else:
                error = (
                    f"HTTP {resp.status_code}" if resp.status_code != 200
                    else "非 JSON 响应（疑似风控拦截页）"
                )
                proxy.consecutive_failures += 1
    except Exception as e:  # noqa: BLE001
        latency = int((time.monotonic() - started) * 1000)
        error = f"{type(e).__name__}: {e}" if not str(e) else str(e)
        proxy.consecutive_failures += 1

    if proxy.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and proxy.enabled:
        proxy.enabled = False
        log_event(
            logger,
            f"代理 {proxy_label(proxy)} 连续失败 {proxy.consecutive_failures} 次，已自动禁用",
            tag="降级",
            level=logging.WARNING,
            detail={"代理": proxy_label(proxy), "连续失败次数": proxy.consecutive_failures},
        )

    proxy.status = status
    proxy.latency_ms = latency
    proxy.last_checked_at = _naive(get_beijing_time_obj())

    await record_event(
        kind="test", target=TEST_TARGET, proxy_label=proxy_label(proxy),
        status_code=None, duration_ms=latency, error=error,
    )
    return status, latency, error


# ─── 订阅链接（clash / plain 双方式，长期保存在本地库）─────────

SUB_KINDS = ("clash", "plain")


async def migrate_legacy_subscription() -> None:
    """旧设置 proxy.subscription_url → proxy_subscriptions(kind=clash)，一次性。"""
    from app.domains.settings.service import get_value, set_value

    legacy = await get_value("proxy.subscription_url")
    if not legacy:
        return
    async with write_session(WritePriority.BACKGROUND) as session:
        rows = (
            await session.execute(
                select(ProxySubscription).where(ProxySubscription.kind == "clash")
            )
        ).scalars().all()
        # 行内是密文（或存量明文），去重比对走解密口径
        exists = any(open_url(r.url) == legacy.strip() for r in rows)
        if not exists:
            session.add(
                ProxySubscription(
                    kind="clash", url=seal_url(legacy.strip()),
                    created_at=_naive(get_beijing_time_obj()),
                    admission_status=ADMISSION_ACTIVE,
                )
            )
            await session.commit()
            log_event(logger, "已迁移旧 Clash 订阅链接到订阅表", tag="成功")
    await set_value("proxy.subscription_url", None)


def mask_url(url: str) -> str:
    """订阅链接掩码：保留主机名，隐藏路径与查询串（token 都在那里）。

    展示层只需要「这是哪家机场」，恢复完整链接是显式的、单独的取用动作。
    """
    if not url:
        return ""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(url)
    except ValueError:
        return "***"
    host = parts.hostname or "***"
    return f"{parts.scheme}://{host}/***" if parts.scheme else f"{host}/***"


async def list_subscriptions(kind: str | None = None, *, reveal: bool = False) -> list[dict]:
    """订阅列表投影。

    默认 `url` 为掩码——响应体不出现明文 token，即便调用方持有合法会话，
    也不该在列表里顺手拿到全部订阅凭据。进程内部需要真实 URL 的逻辑
    （启动候选遍历、内核归属判定）用 `reveal=True` 取明文；那是库内解密，
    不经过任何响应体。
    """
    query = select(ProxySubscription).order_by(ProxySubscription.id)
    if kind:
        query = query.where(ProxySubscription.kind == kind)
    async with get_session_factory()() as session:
        rows = (await session.execute(query)).scalars().all()

    def _url(stored: str | None) -> str:
        plain = open_url(stored)
        return plain if reveal else mask_url(plain)

    return [
        {
            "id": s.id,
            "kind": s.kind,
            "url": _url(s.url),
            "label": s.label,
            "createdAt": s.created_at.isoformat() if s.created_at else None,
            "lastImportedAt": s.last_imported_at.isoformat() if s.last_imported_at else None,
            "lastStats": s.last_stats,
            "deprecated": bool(s.deprecated) or s.resource_block_reason() is not None,
            "deprecatedAt": s.deprecated_at.isoformat() if s.deprecated_at else None,
            "deprecatedReason": s.resource_block_reason() or s.deprecated_reason,
            # 生产准入（见 models.py 常量说明）：
            # - clash：行上的 admission_status，缺失按 ACTIVE 呈现（历史行兼容）；
            #   新订阅由 add_subscription 显式写成 CANDIDATE；
            # - 非 clash（plain）：没有 Candidate 生命周期，一律呈现 ACTIVE
            #   ——历史行哪怕曾被写成 CANDIDATE 也在这里归正。
            "admissionStatus": (
                (s.admission_status or ADMISSION_ACTIVE)
                if s.kind == "clash"
                else ADMISSION_ACTIVE
            ),
            # 自动更新：False 时定时刷新跳过它（手动重拉照常）
            "autoRefresh": bool(s.auto_refresh)
            if s.auto_refresh is not None
            else True,
        }
        for s in rows
    ]


async def add_subscription(kind: str, url: str, label: str | None = None) -> dict:
    """保存订阅。kind=clash 时一步到位：
    ① 内核缺失 → 自动安装（随包资产复制，缺失时才网络下载，进度可轮询）；
    ② 下载订阅验证内容可用（节点数/流量头回填，失败不删行——返回 warning）。
    """
    url = url.strip()
    if kind not in SUB_KINDS:
        raise ValueError(f"未知订阅方式: {kind}")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("订阅链接必须是 http(s) URL")
    from app.core.config import get_settings

    settings = get_settings()
    kernel_installed = None  # None=原本就有 / dict=本次自动安装结果
    if kind == "clash" and not clash_manager.detect_kernel(settings.data_dir)["found"]:
        import asyncio

        kernel_installed = await asyncio.to_thread(
            clash_manager.install_kernel, settings.data_dir
        )
        if not kernel_installed.get("ok"):
            raise ValueError(
                f"内核安装失败：{kernel_installed.get('error')}；"
                "可稍后重试，或手动放置 mihomo 内核到 data/clash/"
            )

    async with write_session(WritePriority.INTERACTIVE) as session:
        sub = ProxySubscription(
            kind=kind, url=seal_url(url), label=label, created_at=_naive(get_beijing_time_obj()),
            # clash 走候选准入（默认 CANDIDATE，显式写清意图）；明文订阅没有候选
            # 观察期，直接 ACTIVE。
            admission_status=(
                ADMISSION_CANDIDATE if kind == "clash" else ADMISSION_ACTIVE
            ),
        )
        session.add(sub)
        await session.commit()
        sub_id = sub.id
    result = {"id": sub_id, "kind": kind, "url": url, "label": label}

    # 订阅验证下载（clash：拉配置拿节点数/流量头；失败保留行，warning 带回）
    if kind == "clash":
        try:
            meta = await clash_manager.runtime.download_subscription(
                url, settings.data_dir, await _saved_proxy_candidates(),
                target_path=clash_manager.subscription_config_path(settings.data_dir, sub_id),
            )
            result["nodes"] = meta.get("nodes")
            result["cached"] = meta.get("cached", False)
            # 在线头优先，缓存回退沿用上次已记录的值。
            traffic = await _resolve_traffic(sub_id, meta.get("userinfo"))
            result["traffic"] = traffic
            await _merge_last_stats(
                sub_id, nodes=meta.get("nodes"), traffic=meta.get("userinfo")
            )
            # 名称三档链（用户改名 > 本次机场名 > 缓存上次值）：无条件走一次，
            # 用户没改过时机场改名能更新过来，只有用户亲手动过的名字才锁死。
            named = await apply_subscription_name_chain(sub_id, meta.get("title"))
            if named:
                result["label"] = named
            # 订阅有效（节点数>0）且用户处在默认直连 → 自动切代理优先
            # （导入有效订阅即代理模式）
            if await maybe_autoswitch_on_subscription(meta.get("nodes")):
                result["strategySwitched"] = True
        except Exception as e:  # noqa: BLE001 —— 验证失败不撤销保存
            result["warning"] = f"订阅已保存，但下载验证失败：{e}（可稍后重拉）"
    if kernel_installed is not None:
        result["kernelInstalled"] = True
        result["kernelVersion"] = clash_manager.kernel_version(kernel_installed["path"])
    return result


async def delete_subscription(sub_id: int) -> bool:
    """删除订阅 + 级联清理两本账：clash_nodes（本域）整批删；clash 订阅同时
    退出生产池——该订阅提供的节点行随退出一并删除（节点跟着订阅走，订阅没了
    节点不该留），快照行与磁盘产物同批回收。"""
    from app.core.config import get_settings
    from app.domains.proxypool.subscription import snapshot_dir

    async with write_session(WritePriority.INTERACTIVE) as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return False
        await session.execute(
            delete(ClashNode).where(ClashNode.subscription_id == sub_id)
        )
        exit_result = None
        if sub.kind == "clash":
            from app.domains.proxypool.admission import exit_from_production
            from app.domains.proxypool.models import SubscriptionSnapshot

            exit_result = await exit_from_production(
                session, subscription_id=sub_id, now=_naive(get_beijing_time_obj())
            )
            await session.execute(
                delete(SubscriptionSnapshot)
                .where(SubscriptionSnapshot.subscription_id == sub_id)
            )
        await session.delete(sub)
        await session.commit()
    # 行已删，快照原始字节按订阅目录整体回收（内容寻址，行没了字节即孤儿）
    shutil.rmtree(
        snapshot_dir(get_settings().data_dir) / str(sub_id), ignore_errors=True
    )
    # 缓存正文与响应头元数据同批回收：只删行不删文件会在 clash 目录留下无人
    # 引用的孤儿（旧订阅内容继续占盘，且「已删除」在文件系统层面不成立）。
    removed_files = clash_manager.remove_subscription_artifacts(
        get_settings().data_dir, sub_id
    )
    # 节点随订阅删除必须留痕（与退出端点同一理由）：否则事后只见节点消失、找不到原因
    if exit_result is not None and exit_result.exited:
        from app.core import orchestration as events

        await events.record(
            events.KIND_SUBSCRIPTION_EXITED,
            f"订阅 {sub_id} 删除，随删退出生产池：删除节点 {exit_result.removed_sources} 行",
            level=events.LEVEL_WARN,
            payload={
                "subscriptionId": sub_id,
                "deletedNodes": exit_result.removed_sources,
                "deleted": True,
                "removedCacheFiles": removed_files,
            },
        )
    # 删到一条不剩即无出口：回退直连（事务外，避免与写闸重入）
    await maybe_revert_to_direct()
    return True


async def get_subscription(sub_id: int) -> ProxySubscription | None:
    async with get_session_factory()() as session:
        return await session.get(ProxySubscription, sub_id)


async def mark_imported(sub_id: int, stats: dict) -> None:
    async with write_session(WritePriority.BACKGROUND) as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return
        sub.last_imported_at = _naive(get_beijing_time_obj())
        sub.last_stats = stats
        await session.commit()


def parse_plain_proxy_line(line: str) -> dict | None:
    """解析明文代理行，支持常见商业代理池格式：
    scheme://user:pass@host:port | host:port | host:port:user:pass | user:pass@host:port"""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" in raw:
        scheme, host, port, username, password = _parse_proxy_url(raw)
        return {"scheme": scheme, "host": host, "port": port, "username": username, "password": password}
    # user:pass@host:port
    if "@" in raw:
        auth, _, addr = raw.rpartition("@")
        user, _, pwd = auth.partition(":")
        host, _, port = addr.partition(":")
        if host and port.isdigit():
            return {"scheme": "http", "host": host, "port": int(port), "username": user or None, "password": pwd or None}
        return None
    parts = raw.split(":")
    if len(parts) == 2 and parts[1].isdigit():
        return {"scheme": "http", "host": parts[0], "port": int(parts[1]), "username": None, "password": None}
    if len(parts) == 4 and parts[1].isdigit():
        return {"scheme": "http", "host": parts[0], "port": int(parts[1]), "username": parts[2], "password": parts[3]}
    return None


def _parse_plain_proxy_content(raw: bytes, _fmt: str) -> list[dict]:
    parsed = []
    for line in raw.decode("utf-8").splitlines():
        try:
            item = parse_plain_proxy_line(line)
        except (TypeError, ValueError):
            continue
        if item:
            parsed.append(item)
    return parsed


async def import_plain_subscription(sub_id: int, timeout: float = 30.0) -> dict:
    """拉取明文代理订阅并导入节点池（按 host:port:user 去重）。"""
    sub = await get_subscription(sub_id)
    if sub is None:
        raise ValueError("订阅不存在")
    if sub.kind != "plain":
        raise ValueError("该订阅不是明文代理订阅（kind=plain）")
    sub_url = open_url(sub.url)
    if not sub_url:
        raise ValueError(UNREADABLE_MESSAGE)
    from app.core.config import get_settings
    from app.domains.proxypool.bootstrap import _healthy_pool_channels, default_kernel_proxy
    from app.domains.proxypool.subscription import (
        DEFAULT_FETCH_TIMEOUT,
        SnapshotError,
        build_channels,
        build_snapshot,
        fetch_egress_success_scores,
        fetch_subscription,
        persist_fetch_failure,
        persist_snapshot,
        saved_proxy_channels,
        subscription_url_identity,
    )

    settings = get_settings()
    fetched_at = _naive(get_beijing_time_obj())
    async with get_session_factory()() as session:
        pool_channels = await _healthy_pool_channels(
            session, data_dir=settings.data_dir, now=fetched_at,
        )
        egress_scores = await fetch_egress_success_scores(session)
    channels = build_channels(
        kernel_proxy=default_kernel_proxy(), pool_channels=pool_channels,
    )
    existing_channels = {channel.proxy for channel in channels}
    channels.extend(
        channel for channel in saved_proxy_channels(await _saved_proxy_candidates())
        if channel.proxy not in existing_channels
    )
    try:
        fetched = await fetch_subscription(
            sub_url,
            channels,
            request_timeout=min(DEFAULT_FETCH_TIMEOUT, max(0.1, float(timeout))),
            egress_scores=egress_scores,
            response_parser=_parse_plain_proxy_content,
        )
    except SnapshotError as exc:
        fetched_at = _naive(get_beijing_time_obj())
        async with write_session(WritePriority.BACKGROUND) as session:
            current = await session.get(ProxySubscription, sub_id)
            if current is not None and open_url(current.url) == sub_url:
                current.last_fetch_at = fetched_at
                current.last_fetch_status = "FAILED"
                current.last_error = str(exc)[:500]
                await persist_fetch_failure(
                    session, subscription_id=sub_id, url=sub_url,
                    attempts=getattr(exc, "attempts", []), error=str(exc),
                    duration_ms=getattr(exc, "duration_ms", 0), now=fetched_at,
                )
                await session.commit()
        await record_event(
            kind="import", target=f"plain:{sub_id}",
            proxy_label=f"plain:{sub_id}",
            status_code=next((a.http_status for a in reversed(getattr(exc, "attempts", []))
                              if a.http_status is not None), None),
            duration_ms=getattr(exc, "duration_ms", 0), error=str(exc),
        )
        raise ValueError(str(exc)) from exc

    parsed = [
        {
            **item,
            "scheme": item.get("scheme") or "http",
            "username": item.get("username"),
            "password": item.get("password"),
        }
        for item in fetched.resolved_nodes
    ]
    if not parsed:
        raise ValueError("订阅内容没有可解析的代理行")

    snapshot = build_snapshot(sub_id, sub_url, fetched, parsed, now=fetched_at)
    async with write_session(WritePriority.BACKGROUND) as session:
        current = await session.get(ProxySubscription, sub_id)
        if current is None or open_url(current.url) != sub_url:
            raise ValueError("订阅地址在刷新期间发生变化，本次结果未应用")
        await persist_snapshot(session, snapshot, data_dir=settings.data_dir)
        current.cache_url_identity = subscription_url_identity(sub_url)
        current.last_fetch_at = fetched_at
        current.last_fetch_status = "OK"
        current.last_error = None
        await session.commit()

    # 去重键用**解密后**的用户名：库内是随机 nonce 的密文，同一条目每次
    # 加密结果都不同，拿密文当键会失去去重能力（重复导入同一个订阅会翻倍）。
    existing = {
        (p.host, p.port, credentials.open_value(p.username) or "")
        for p in await list_proxies()
    }
    existing_before_add = set(existing)
    added = skipped = 0
    async with write_session(WritePriority.BACKGROUND) as session:
        for item in parsed:
            key = (item["host"], item["port"], item["username"] or "")
            if key in existing:
                skipped += 1
                continue
            existing.add(key)
            proxy = Proxy(
                label=item["username"] and f"{item['host']}:{item['port']}({item['username']})" or None,
                scheme=item["scheme"], host=item["host"], port=item["port"],
                username=item["username"], password=item["password"],
                enabled=True, created_at=_naive(get_beijing_time_obj()),
            )
            credentials.seal_proxy(proxy)
            session.add(proxy)
            added += 1
        await session.commit()

    # ── 入库即体检（导入后自动校验标记可用节点）──
    # 只测本次新增行（存量行沿用既有 status，重测留给 6h 体检）；全失败也
    # 保留数据——enabled 由 _check 连败计数器自然处置，不在导入里删行。
    stats: dict = {"fetched": len(parsed), "added": added, "skipped": skipped}
    if added:
        new_keys = existing - existing_before_add
        fresh = [
            p for p in await list_proxies()
            if (p.host, p.port, credentials.open_value(p.username) or "") in new_keys
        ]
        results = await asyncio.gather(
            *[_check_single(p) for p in fresh], return_exceptions=True
        )
        checked = [r for r in results if isinstance(r, dict)]
        stats["checked"] = len(checked)
        stats["alive"] = sum(1 for r in checked if r.get("status") == "ok")
        log_event(
            logger,
            f"明文代理导入体检完成：新增 {added} 条，{stats['alive']}/{stats['checked']} 条通过",
            detail={
                "订阅编号": sub_id,
                "新增条数": added,
                "通过条数": stats["alive"],
                "体检条数": stats["checked"],
            },
        )
    await mark_imported(sub_id, stats)
    await record_event(
        kind="import", target=f"plain:{sub_id}", proxy_label=f"plain:{sub_id}",
        status_code=fetched.http_status, duration_ms=fetched.duration_ms, error=None,
    )
    # 订阅有效（体检有存活节点）且用户处在默认直连 → 自动切代理优先
    if stats.get("alive") and await maybe_autoswitch_on_subscription(stats["alive"]):
        stats["strategySwitched"] = True
    return stats


# ─── Clash 节点检测（出口 IP 去重 + 存活）─────────────────────

_EXIT_IP_URLS = [
    "http://ip-api.com/json/?fields=query",
    "https://api.ip.sb/ip",
]


async def _fetch_exit_ip(client: httpx.AsyncClient) -> str | None:
    for url in _EXIT_IP_URLS:
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                text = resp.text.strip()
                # ip-api 返回 JSON；api.ip.sb 返回纯文本
                if text.startswith("{"):
                    match = re.search(r'"query"\s*:\s*"([\d.a-fA-F:]+)"', text)
                    if match:
                        return match.group(1)
                return text.strip('" \n')
        except Exception:  # noqa: BLE001
            continue
    return None


async def _active_clash_subscription_id(status: dict) -> int | None:
    """内核正在跑的订阅 id：按启动时登记的订阅 URL 匹配 > 最近一条 clash 订阅。

    节点检测与统计（pool_stats）共用——「当前订阅」只有这一个定义。
    兜底取最近一条是内核没带归属信息（登记机制上线前启动）时的兼容口径：
    无法判定时检测结果记到最近添加的订阅账本。
    """
    async with get_session_factory()() as session:
        subs = (
            await session.execute(
                select(ProxySubscription)
                .where(ProxySubscription.kind == "clash")
                .order_by(ProxySubscription.id)
            )
        ).scalars().all()
    active_url = (status.get("subscriptionUrl") or "").strip()
    if active_url:
        for s in subs:
            # 行内是密文，与内核登记的明文归属按解密口径比对
            if open_url(s.url) == active_url:
                return s.id
    return subs[-1].id if subs else None


# ─── Clash 节点检测会话（进度快照）──────────────────────────────
# 检测是分钟级串行探测，同步等响应会让界面长时间无反馈。每次检测
# （手动/启动首检/定时体检同源）对应一份模块级会话快照：探测逐节点
# 写入，前端经 GET /proxies/clash/test/progress 轮询。写入全部发生在
# 串行锁持有期间（reset 在 impl 锁内首行，finalize 紧随 impl 返回、
# 中间无 await），单事件循环下无竞态；读取只做拷贝。
_clash_test_session: dict | None = None

_SESSION_ACTIVE_PHASES = ("queued", "running")


def _session_now() -> str:
    return _naive(get_beijing_time_obj()).isoformat()


def _clash_test_session_reset() -> None:
    global _clash_test_session
    _clash_test_session = {
        "phase": "queued",
        "total": None,
        "toProbe": None,
        "probed": 0,
        "cooldownSkipped": 0,
        "alive": 0,
        "aliveUnique": None,
        "selector": None,
        "subscriptionId": None,
        "deprecated": None,
        "nodes": [],
        "startedAt": _session_now(),
        "finishedAt": None,
        "error": None,
    }


def _clash_test_session_update(**fields) -> None:
    if _clash_test_session is not None:
        _clash_test_session.update(fields)


def _clash_test_session_node(row: dict) -> None:
    if _clash_test_session is None:
        return
    _clash_test_session["nodes"].append(row)
    _clash_test_session["probed"] += 1
    if row.get("alive"):
        _clash_test_session["alive"] += 1


def clash_test_progress() -> dict | None:
    """当前/最近一次检测会话快照；无会话返回 None。"""
    if _clash_test_session is None:
        return None
    snap = dict(_clash_test_session)
    snap["nodes"] = [dict(n) for n in snap["nodes"]]
    return snap


def clash_test_start() -> dict:
    """手动检测入口：已有进行中的会话则复用（手动/首检/体检同源），
    否则落 queued 会话并拉起后台任务，立即返回快照（不等探测）。"""
    if _clash_test_session and _clash_test_session["phase"] in _SESSION_ACTIVE_PHASES:
        return clash_test_progress()
    _clash_test_session_reset()
    asyncio.get_running_loop().create_task(_clash_test_session_task())
    return clash_test_progress()


async def _clash_test_session_task() -> None:
    try:
        await test_clash_nodes(probe_all=True)  # 手动检测 = 要全量结论
    except ValueError:
        pass  # 失败原因（用户语言）已落会话，由进度端点带回
    except Exception:  # noqa: BLE001
        log_event(
            logger,
            "Clash 节点检测后台会话执行失败",
            level=logging.ERROR,
            exc_info=True,
        )


async def test_clash_nodes(
    subscription_id: int | None = None, *, probe_all: bool = False,
) -> dict:
    """检测 Clash 订阅节点（会话入口）：探测实现在 _test_clash_nodes_impl，
    进度实时写入会话快照，成功/失败都收口到会话（phase=done/failed）。
    probe_all：全量探测（冷却期节点也测）——手动检测与启动/切换/重拉首检
    都是「用户要当前完整结论」的场景；定时体检走缺省 False，冷却期节点
    跳过不耗探测。"""
    if _clash_test_session is None:
        _clash_test_session_reset()  # 体检/首检直调路径：等锁期间即有 queued 可看
    try:
        result = await _test_clash_nodes_impl(subscription_id, probe_all=probe_all)
    except Exception as e:  # noqa: BLE001
        _clash_test_session_update(
            phase="failed", error=str(e) or "节点检测失败", finishedAt=_session_now()
        )
        raise
    _clash_test_session_update(
        phase="done",
        total=result["total"],
        probed=result["probed"],
        alive=result["alive"],
        aliveUnique=result["aliveUnique"],
        selector=result["selector"],
        subscriptionId=result["subscriptionId"],
        deprecated=result["deprecated"],
        nodes=result["nodes"],
        finishedAt=_session_now(),
    )
    return result


def _lane_port_listening(port: int) -> bool:
    """lane listener 端口在听才可用：内核启动时端口被占的 lane 绑不上，
    走它的探测会把「通道不通」误判成「节点不通」——探测前先剔除。"""
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def _probe_steam(client: httpx.AsyncClient) -> tuple[bool, int | None]:
    """生产端点业务探测；同时返回 HTTP 状态以区分目标响应与传输失败。

    判据 = 200 且 body 是 JSON 对象（见 _steam_payload_ok）——
    节点真能服务生产流量才算活，风控拦截页不算。
    """
    try:
        resp = await client.get(TEST_URL, params=TEST_PARAMS, timeout=TEST_TIMEOUT)
        return _steam_payload_ok(resp), resp.status_code
    except Exception:  # noqa: BLE001
        return False, None


async def _probe_node_via_lane(
    ctl: httpx.AsyncClient, base: str, lane: tuple[str, int], name: str,
) -> dict:
    """单节点经单条探测 lane 探测：切本 lane 组 → 组选择回读核验 → 并发跑
    Steam 存活与出口 IP。回读不对齐（选择没生效）按探不到处理——不产生该
    节点的健康证据，与 L1 的 GLOBAL 归因纪律同一条。"""
    group, port = lane
    blank = {"name": name, "alive": False, "steamOk": False, "steamStatus": None, "exitIp": None,
             "ms": None, "duplicate": False, "probed": False}
    try:
        put = await ctl.put(f"{base}/proxies/{quote(group, safe='')}", json={"name": name})
        if put.status_code >= 400:
            return blank
        now = (await ctl.get(f"{base}/proxies/{quote(group, safe='')}")).json().get("now")
        if now != name:
            return blank
    except Exception:  # noqa: BLE001
        return blank
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(
            timeout=TEST_TIMEOUT, proxy=f"http://127.0.0.1:{port}", trust_env=False
        ) as via:
            (steam_ok, steam_status), exit_ip = await asyncio.gather(
                _probe_steam(via), _fetch_exit_ip(via)
            )
    except Exception:  # noqa: BLE001
        return {**blank, "probed": True}
    ms = int((time.monotonic() - started) * 1000)
    return {**blank, "alive": steam_ok, "steamOk": steam_ok, "steamStatus": steam_status,
            "exitIp": exit_ip, "ms": ms, "probed": True}


async def _probe_nodes_via_lanes(
    ctl: httpx.AsyncClient, base: str,
    lanes: list[tuple[str, int]], names: list[str],
) -> list[dict]:
    """经探测 lane 并发探测（并发上限 = lane 数，20）：每条 lane 一个独立
    listener + include-all select 组，组选择互不干扰。节点多于 lane 数分波：
    每波先切组再整波并发，波内即进度（逐行进会话）。"""
    results: list[dict] = []
    for start in range(0, len(names), len(lanes)):
        wave = names[start:start + len(lanes)]
        rows = await asyncio.gather(*(
            _probe_node_via_lane(ctl, base, lanes[i], name)
            for i, name in enumerate(wave)
        ))
        results.extend(rows)
        for row in rows:
            _clash_test_session_node(row)
    return results


async def _probe_nodes_via_selector(
    ctl: httpx.AsyncClient, base: str, headers: dict[str, str],
    mixed_port: int, names: list[str], selector: str,
) -> list[dict]:
    """回退路径：内核配置无探测 lane 时逐节点切路由 selector 经混合端口探测。

    selector 切换是全局状态，只能串行；rule 模式下流量按规则组走（不经过
    GLOBAL），检测期间临时切 global 模式让 GLOBAL 成为总闸，测完恢复。"""
    results: list[dict] = []
    prev_mode = None
    try:
        mode_resp = await ctl.get(f"{base}/configs")
        if mode_resp.status_code == 200:
            prev_mode = (mode_resp.json() or {}).get("mode")
            if prev_mode != "global":
                await ctl.patch(f"{base}/configs", json={"mode": "global"})
    except Exception:  # noqa: BLE001 —— 模式切换失败则按当前模式尽力检测
        prev_mode = None
    try:
        for name in names:
            try:
                put = await ctl.put(
                    f"{base}/proxies/{quote(selector, safe='')}",
                    json={"name": name},
                )
                if put.status_code >= 400:
                    row = {"name": name, "alive": False, "steamOk": False, "exitIp": None, "ms": None, "duplicate": False, "probed": False}
                    results.append(row)
                    _clash_test_session_node(row)
                    continue
            except Exception:  # noqa: BLE001
                row = {"name": name, "alive": False, "steamOk": False, "exitIp": None, "ms": None, "duplicate": False, "probed": False}
                results.append(row)
                _clash_test_session_node(row)
                continue

            started = time.monotonic()
            async with httpx.AsyncClient(
                timeout=TEST_TIMEOUT, proxy=f"http://127.0.0.1:{mixed_port}", trust_env=False
            ) as via:
                # 存活判定（Steam）与出口 IP 探测并发：IP 仅用于同落地
                # 去重，不阻塞也不参与 alive 判定
                    (steam_ok, steam_status), exit_ip = await asyncio.gather(
                    _probe_steam(via), _fetch_exit_ip(via)
                )
            ms = int((time.monotonic() - started) * 1000)
            row = {"name": name, "alive": steam_ok, "steamOk": steam_ok,
                   "steamStatus": steam_status, "exitIp": exit_ip, "ms": ms,
                   "duplicate": False, "probed": True}
            results.append(row)
            _clash_test_session_node(row)
    finally:
        if prev_mode is not None and prev_mode != "global":
            try:
                await ctl.patch(f"{base}/configs", json={"mode": prev_mode})
            except Exception:  # noqa: BLE001
                log_event(
                    logger,
                    "检测后恢复内核路由模式失败，内核可能仍处于 global 模式（重启内核可恢复）",
                    tag="注意",
                    level=logging.WARNING,
                    detail={"原模式": prev_mode},
                )
    return results


def _same_exit_primary_key(row: dict, order: dict[str, int]) -> tuple:
    """同出口代表行的择优键：本次检测存活优先 → 未冷却（非账本沿用）优先 →
    本次延迟低者优先 → 配置顺序。展示位永远是该出口当前最可信的快节点——
    出口 IP 探测与 Steam 存活探测相互独立，「拿到 IP 但 Steam 判死」的节点
    不能凭出现序占住展示位。"""
    ms = row.get("ms")
    return (
        not row.get("alive"),
        bool(row.get("cooling")),
        ms if isinstance(ms, int) else float("inf"),
        order.get(row["name"], len(order)),
    )


def _mark_same_exit(results: list[dict], order: dict[str, int]) -> None:
    """按出口 IP 分组择优代表行，其余同 IP 行标 duplicate（就地改写）。"""
    groups: dict[str, list[dict]] = {}
    for row in results:
        ip = row.get("exitIp")
        if ip:
            groups.setdefault(ip, []).append(row)
    for members in groups.values():
        primary = min(members, key=lambda r: _same_exit_primary_key(r, order))
        for row in members:
            row["duplicate"] = row is not primary


async def _test_clash_nodes_impl(
    subscription_id: int | None = None, *, probe_all: bool = False,
) -> dict:
    """检测 Clash 订阅节点：逐个切换 selector，经混合端口探测。

    - 存活判定 = Steam 端点 HTTP 200（与手动代理池同款探测目标；
      ip-api 通 ≠ Steam 通，Steam 侧风控/路由差异只有真实端点能暴露）
    - 出口 IP 仅用于去重：Steam API 不回显出口 IP（无 Set-Cookie/IP 头），
      与存活探测并发执行不增耗时；出口 IP 相同 = 同落地（机场多入口
      同落地很常见），duplicate 标记
    - 节点状态来自 ProxyNode；兼容表只记录最近体检时间与节流期限
    - 冷却期内跳过检测（不算死也不耗探测时间）
    - 订阅资源失效或没有可用节点时停用，恢复后自动解除
    - 探测通道：内核配置带探测 lane（`inject_probe_lanes` 注入的 20 组
      select + listener）→ 按 lane 并发探测（并发上限 20，节点多于 lane 分波）；
      lane 缺席（配置注入前启动的内核）回退逐节点切 selector 的串行路径
    - selector 自愈：检测后若当前选中节点已死 → 自动切最快健康节点
    - 串行锁 _clash_test_lock：手动检测/启动首检/定时体检三源互斥——
      并发跑两轮完整检测会互踩 lane 组选择与账本写入
    - 返回 {total, alive, aliveUnique, nodes, subscriptionId, deprecated}
      （ms 为 Steam 请求延迟，不再混入 IP 探测耗时）
    """
    async with _clash_test_lock():
        _clash_test_session_reset()
        status = clash_manager.runtime.status()
        if not status["running"]:
            raise ValueError("Clash 未运行：请先在 Clash 接入选择订阅并启动")
        base = status["controllerUrl"]
        secret = clash_manager.runtime.secret
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        mixed_port = status["port"]

        # 本次检测归属的订阅：显式指定 > 内核配置文件对应 > 最近一条 clash 订阅
        sub_id = subscription_id
        if sub_id is None:
            sub_id = await _active_clash_subscription_id(status)
            if sub_id is None:
                raise ValueError("无 Clash 订阅记录：请先在订阅区保存订阅")

        async with httpx.AsyncClient(timeout=10, headers=headers, trust_env=False) as ctl:
            resp = await ctl.get(f"{base}/proxies")
            resp.raise_for_status()
            all_proxies = resp.json().get("proxies", {})

        # 真实节点：来自配置文件的 proxies 段（排除内置与策略组）
        config_text = ""
        if status.get("configPath"):
            from pathlib import Path

            config_text = Path(status["configPath"]).read_text(encoding="utf-8", errors="ignore")
        node_names = clash_manager.parse_node_names(config_text)
        if not node_names:
            # 订阅用 proxy-providers 时退回控制器类型过滤
            node_names = [
                name for name, info in all_proxies.items()
                if str(info.get("type", "")).lower() not in clash_manager._NON_NODE_TYPES
                and name not in ("DIRECT", "REJECT", "GLOBAL", "COMPATIBLE", "Pass")
            ]

        # 节点状态账本：定时体检按冷却跳过（结果沿用账本 status）；
        # probe_all（手动检测/首检）全量探测——20 并发下全量成本很低，
        # 且用户要的是「当前完整结论」，恢复的节点应当场发现
        ledger = await _load_clash_node_ledger(sub_id, node_names)
        to_probe = (
            list(node_names) if probe_all
            else [n for n in node_names if not _in_cooldown(ledger.get(n))]
        )
        now = _naive(get_beijing_time_obj())

        # 路由组（selector 自愈用；探测 lane 的组是检测专用通道，不作路由组）
        groups = [
            info for info in all_proxies.values()
            if str(info.get("type", "")).lower() in ("selector", "fallback", "urltest")
            and not str(info.get("name", "")).startswith(clash_manager.PROBE_LANE_PREFIX)
        ]

        def _group_score(info: dict) -> int:
            name = str(info.get("name", ""))
            for i, kw in enumerate(("GLOBAL", "PROXY", "节点", "选择", "手动")):
                if kw in name:
                    return i
            return 99

        groups.sort(key=_group_score)
        selector = (str(groups[0].get("name") or "") or "GLOBAL") if groups else "GLOBAL"

        # 会话进入 running：总量/待测量/冷却跳过数一次落定，之后探测逐节点追加
        _clash_test_session_update(
            phase="running",
            total=len(node_names),
            toProbe=len(to_probe),
            cooldownSkipped=len(node_names) - len(to_probe),
            subscriptionId=sub_id,
            selector=selector,
        )

        # 探测通道：内核本次启动注入的 lane（含可用端口预检）→ 20 并发分波；
        # lane 缺席（注入前启动的内核）回退逐节点切 selector 的串行路径。
        # 事实源是运行时状态不是磁盘配置——订阅下载会在启动后把 config.yaml
        # 覆写回原始文本，内核内存里仍跑着注入后的配置。
        lanes = [
            lane for lane in clash_manager.runtime.probe_lanes
            if _lane_port_listening(lane[1])
        ]

        async with httpx.AsyncClient(timeout=8, headers=headers, trust_env=False) as ctl:
            if lanes:
                results = await _probe_nodes_via_lanes(ctl, base, lanes, to_probe)
            else:
                # selector 回退路径的探测流量走混合端口：入口没在听时全部节点
                # 会误判失败污染账本（并连带触发订阅误废弃），如实报错代替
                if not clash_manager.runtime.entry_listening():
                    raise ValueError(
                        "内核入口不可用（混合端口未监听）且探测 lane 缺席，无法检测；请重启内核后重试"
                    )
                results = await _probe_nodes_via_selector(
                    ctl, base, headers, mixed_port, to_probe, selector
                )

        # 同出口代表行择优 + 其余标 duplicate（前端按出口 IP 分组收敛，
        # 代表行即该出口的展示位——取本次检测中最可信的快节点）
        order = {name: i for i, name in enumerate(node_names)}
        results.sort(key=lambda r: order.get(r["name"], len(order)))
        _mark_same_exit(results, order)

        # ── 体检结果写入权威节点状态，再刷新兼容投影 ──
        try:
            from app.domains.proxypool.bridge import ingest_ledger, ingest_node_check
            from app.domains.proxypool.models import HealthRun
            from app.domains.proxypool.scheduling import request_rebuild

            async with write_session(WritePriority.BACKGROUND) as session:
                # 手动体检的运行台账：独立计时 + 节点级汇总（前端「本次体检耗时」
                # 的唯一事实源），逐节点观测经 run_id 归属
                run = HealthRun(
                    channel="manual", subscription_id=sub_id,
                    started_at=now, total=len(results),
                    steam_ok=sum(1 for r in results if r.get("alive")),
                    failed=sum(1 for r in results if not r.get("alive")),
                    ip_known=len({r["exitIp"] for r in results
                                  if r.get("alive") and r.get("exitIp")}),
                )
                session.add(run)
                await session.flush()
                fresh = await ingest_node_check(
                    session, subscription_id=sub_id, results=results, now=now,
                    run_id=run.id,
                )
                ledger_hit = await ingest_ledger(session, now=now)
                run.finished_at = datetime.now()
                run.duration_ms = int(
                    (run.finished_at - run.started_at).total_seconds() * 1000
                )
                await session.commit()
            if fresh.changed or ledger_hit.changed:
                request_rebuild()
            log_event(
                logger,
                f"体检结果对齐池账本：本轮命中 {fresh.matched} 个、账本命中 {ledger_hit.matched} 个",
                detail={
                    "本轮命中": fresh.matched,
                    "本轮复活": fresh.activated,
                    "本轮新增出口": fresh.exit_ips_added,
                    "账本命中": ledger_hit.matched,
                    "账本复活": ledger_hit.activated,
                    "账本新增出口": ledger_hit.exit_ips_added,
                    "本轮未匹配": len(fresh.unmatched),
                },
            )
        except Exception:  # noqa: BLE001 —— 对齐失败不改体检结论
            log_event(
                logger,
                "体检结果对齐池账本失败，不影响节点账本",
                level=logging.ERROR,
                exc_info=True,
            )

        # ── 生成订阅体检结果和节流投影，不维护第二套健康状态 ──
        alive_count, unique_alive, deprecated = await _apply_node_results(
            sub_id, ledger, results, node_names, now
        )

        # ── selector 自愈：当前选中节点已死 → 切最快健康节点 ──
        try:
            healthy = sorted(
                (r for r in results if r["alive"] and r["ms"] is not None),
                key=lambda r: r["ms"],
            )
            current = (all_proxies.get(selector) or {}).get("now")
            current_alive = any(r["name"] == current and r["alive"] for r in results) or (
                ledger.get(current) and ledger[current].get("status") == "ok" and not _in_cooldown(ledger.get(current))
            )
            if healthy and not current_alive:
                self_heal_target = healthy[0]["name"]
                async with httpx.AsyncClient(timeout=8, headers=headers, trust_env=False) as ctl:
                    await ctl.put(
                        f"{base}/proxies/{quote(selector, safe='')}",
                        json={"name": self_heal_target},
                    )
                log_event(
                    logger,
                    f"选中节点 {current} 已死，已切换到最快健康节点 {self_heal_target}",
                    tag="已修复",
                    detail={"原节点": current, "新节点": self_heal_target},
                )
        except Exception:  # noqa: BLE001 —— 自愈失败不影响检测结果
            log_event(
                logger,
                "切换健康节点失败，不阻塞检测",
                tag="忽略",
                level=logging.WARNING,
            )

        return {
            "total": len(node_names),
            "probed": len(results),
            "alive": alive_count,
            "aliveUnique": unique_alive,
            "selector": selector,
            "subscriptionId": sub_id,
            "deprecated": deprecated,
            "nodes": [r for r in results] + _cooldown_nodes_payload(ledger, node_names, {r["name"] for r in results}),
        }


async def prune_clash_node_ledger(sub_id: int, current_names: set[str] | None = None) -> int:
    """账本收敛：删除已不在订阅当前内容里的节点行（改名/下架的历史痕迹）。

    之前「保留不删」的设计在改名即新行的现实下会永久累积；改为每次
    订阅刷新/检测时按当前内容收敛——改名节点 = 旧行删除 + 新行从零
    开始（诚实反映「这是新节点」，绕过状态机的收益只剩一次性未知态）。
    current_names 缺省时从内核当前配置文件解析；空集防呆不删
    （解析不到节点宁可不裁，全删等于销账）。
    """
    if current_names is None:
        status = clash_manager.runtime.status()
        if not status.get("configPath") or not Path(status["configPath"]).is_file():
            return 0
        text = Path(status["configPath"]).read_text(encoding="utf-8", errors="ignore")
        current_names = set(clash_manager.parse_node_names(text))
    if not current_names:
        return 0
    async with write_session(WritePriority.BACKGROUND) as session:
        result = await session.execute(
            delete(ClashNode).where(
                ClashNode.subscription_id == sub_id,
                ClashNode.name.notin_(current_names),
            )
        )
        await session.commit()
        return result.rowcount or 0


async def _load_clash_node_ledger(sub_id: int, node_names: list[str]) -> dict[str, dict]:
    """读取节点状态账本（clash_nodes 表），返回 {节点名: 状态dict}。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(ClashNode).where(
                    ClashNode.subscription_id == sub_id,
                    ClashNode.name.in_(node_names),
                )
            )
        ).scalars().all()
        return {
            n.name: {
                "status": n.status,
                "fail_count": n.fail_count,
                "revive_passes": n.revive_passes,
                "latency_ms": n.latency_ms,
                "exit_ip": n.exit_ip,
                "last_checked_at": n.last_checked_at,
                "cooldown_until": n.cooldown_until,
            }
            for n in rows
        }


def _in_cooldown(ledger_entry: dict | None) -> bool:
    """节点是否处于冷却期（冷却到点 = 该测了，返回 False）。"""
    if not ledger_entry or not ledger_entry.get("cooldown_until"):
        return False
    until = ledger_entry["cooldown_until"]
    return until > _naive(get_beijing_time_obj())


def _cooldown_nodes_payload(
    ledger: dict[str, dict], node_names: list[str], probed: set[str]
) -> list[dict]:
    """冷却跳过节点的结果行（沿用账本状态，标注 cooling）。"""
    payload = []
    for name in node_names:
        if name in probed:
            continue
        entry = ledger.get(name)
        if entry is None:
            continue
        payload.append(
            {
                "name": name,
                "alive": entry.get("status") == "ok",
                "steamOk": entry.get("status") == "ok",
                "exitIp": entry.get("exit_ip"),
                "ms": entry.get("latency_ms"),
                "duplicate": False,
                "probed": False,
                "cooling": True,
            }
        )
    return payload


async def _apply_node_results(
    sub_id: int,
    ledger: dict[str, dict],
    results: list[dict],
    node_names: list[str],
    now: datetime,
) -> tuple[int, int, bool]:
    """刷新节点健康投影与探测节流信息；健康状态和失败数只读 ProxyNode。"""
    from app.domains.proxypool.models import ProxyNode
    from app.domains.proxypool.bridge import project_node_health

    if node_names and not any(r.get("probed") for r in results):
        async with get_session_factory()() as session:
            sub = await session.get(ProxySubscription, sub_id)
            stats = dict(sub.last_stats or {}) if sub is not None else {}
            deprecated = bool(sub.deprecated) if sub is not None else False
            blocked = sub.resource_block_reason() if sub is not None else None
        alive = min(int(stats.get("alive") or 0), len(node_names))
        if blocked is not None:
            deprecated = await _evaluate_subscription_deprecation(sub_id, alive, len(node_names))
        return alive, int(stats.get("aliveUnique") or 0), deprecated

    alive_count = 0
    unique_ips: set[str] = set()

    async with write_session(WritePriority.BACKGROUND) as session:
        registry = {
            n.original_name: n
            for n in (await session.execute(
                select(ProxyNode).where(
                    (ProxyNode.subscription_id == sub_id)
                    | ProxyNode.runtime_name.like(f"{sub_id}|%"),
                    ProxyNode.original_name.in_(node_names),
                )
            )).scalars()
            if n.original_name
        }
        # 节点存在性以订阅当前内容为准：新节点 INSERT，已有节点 UPDATE
        existing = {
            n.name: n
            for n in (
                await session.execute(
                    select(ClashNode).where(ClashNode.subscription_id == sub_id)
                )
            ).scalars()
        }

        for r in results:
            if not r.get("probed"):
                continue  # selector 切换失败的节点不入账本
            name = r["name"]
            row = existing.get(name)
            if row is None:
                row = ClashNode(
                    subscription_id=sub_id, name=name, created_at=now,
                )
                session.add(row)

            row.last_checked_at = now
            row.latency_ms = r.get("ms")
            canonical = registry.get(name)
            if canonical is not None:
                project_node_health(row, canonical)
            else:
                row.status = "unknown"
                row.fail_count = 0
                row.revive_passes = 0
                row.exit_ip = None

            if r["alive"]:
                alive_count += 1
                if r.get("exitIp"):
                    unique_ips.add(r["exitIp"])
                row.cooldown_until = None
            elif r.get("steamStatus") is not None or r.get("exitIp"):
                row.cooldown_until = None
            else:
                failures = int(canonical.consecutive_failures or 0) if canonical else 1
                tier = min(max(failures, 1), len(_FAIL_COOLDOWN_MINUTES)) - 1
                row.cooldown_until = now + timedelta(minutes=_FAIL_COOLDOWN_MINUTES[tier])

        await session.commit()

    # 冷却跳过的节点按账本口径计入存活统计（上次判定为准）
    probed_names = {r["name"] for r in results if r.get("probed")}
    for name in node_names:
        if name in probed_names:
            continue
        entry = ledger.get(name)
        if entry and entry.get("status") == "ok":
            alive_count += 1
            if entry.get("exit_ip"):
                unique_ips.add(entry["exit_ip"])

    unique_alive = len(unique_ips)
    checked = [r for r in results if r.get("probed")]
    target_response_only = bool(checked) and not any(r.get("alive") for r in checked) and all(
        r.get("steamStatus") is not None or r.get("exitIp") for r in checked
    )
    if target_response_only:
        async with get_session_factory()() as session:
            sub = await session.get(ProxySubscription, sub_id)
            deprecated = bool(sub.deprecated) if sub is not None else False
    else:
        deprecated = await _evaluate_subscription_deprecation(
            sub_id, alive_count, len(node_names)
        )
    # 账本收敛：已不在订阅内容里的节点行随检测清理（检测读的就是当前配置）
    pruned = await prune_clash_node_ledger(sub_id, set(node_names))
    # 存活统计落库——订阅行「存活 x/y」标签的数据源
    if not target_response_only:
        await _merge_last_stats(sub_id, alive=alive_count, total=len(node_names))
    if pruned:
        log_event(
            logger,
            f"订阅 {sub_id} 的节点账本已收敛：删除 {pruned} 个已下线节点行",
            detail={"订阅编号": sub_id, "删除行数": pruned},
        )

    await record_event(
        kind="clash", target="node-test",
        proxy_label=f"clash:{sub_id}",
        error=None if alive_count or target_response_only else "全部节点不可用",
    )
    if deprecated and not target_response_only:
        await maybe_revert_to_direct()
    return alive_count, unique_alive, deprecated


async def _resolve_traffic(sub_id: int, fresh: str | None) -> str | None:
    """流量的取值链（与名称链**完全独立**）：本次拉到的 > 上次存下的。

    流量是「用掉多少」的实时量，只有一个事实源：`last_stats.traffic`。
    本次拿到新的就用新的；本次没拿到（降级走缓存、面板没发头）就沿用账本
    里已有的值——绝不因为「这次没抓到」而清空，也绝不让名称用谁的这件事
    影响流量（用户改了名字，照样认机场给的最新流量）。
    """
    value = (fresh or "").strip()
    if value:
        return value
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return None
        existing = (sub.last_stats or {}).get("traffic")
        return str(existing) if existing else None


async def _merge_last_stats(sub_id: int, **fields) -> None:
    """订阅 last_stats 局部合并（不动其他键）——流量/存活回填共用出口。

    `traffic` 是单调增长的实时量：传入 None/空串视为「本次没取到」，
    保留既有值而不是写空——否则一次失败拉取就会把已记录的流量清掉，
    界面上出现「流量从 318GB 退回未知」的倒退。
    """
    async with write_session(WritePriority.BACKGROUND) as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return
        stats = dict(sub.last_stats or {})
        for key, value in fields.items():
            if key == "traffic" and not value:
                continue
            stats[key] = value
        sub.last_stats = stats
        await session.commit()


async def _evaluate_subscription_deprecation(sub_id: int, alive_count: int, total: int) -> bool:
    """资源失效或没有可用节点时停用，恢复后自动解除。"""
    if total <= 0:
        return False
    async with write_session(WritePriority.BACKGROUND) as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return False
        reason = sub.resource_block_reason()
        if reason is None and alive_count <= 0:
            reason = f"没有可用节点（可用 0/{total}）"
        if reason is not None:
            if not sub.deprecated:
                sub.deprecated = True
                sub.deprecated_at = _naive(get_beijing_time_obj())
                sub.deprecated_reason = reason
                await session.commit()
                log_event(
                    logger,
                    f"订阅 {sub_id} 暂不可用：{reason}",
                    tag="降级",
                    level=logging.WARNING,
                    detail={
                        "订阅编号": sub_id,
                        "可用节点数": alive_count,
                        "总节点数": total,
                        "原因": reason,
                    },
                )
            return True
        if sub.deprecated:
            sub.deprecated = False
            sub.deprecated_at = None
            sub.deprecated_reason = None
            await session.commit()
            log_event(
                logger,
                f"订阅 {sub_id} 恢复可用（{alive_count}/{total}），已解除废弃标记",
                tag="已修复",
                detail={
                    "订阅编号": sub_id,
                    "可用节点数": alive_count,
                    "总节点数": total,
                },
            )
        return False


async def maybe_run_clash_health_check(force: bool = False) -> str:
    """体检门槛：距上次全量节点检测 ≥6h 才真跑（force 跳过门槛）。

    本地软件不常驻运行——APScheduler 间隔只是兜底，真正的节流靠
    clash_nodes 账本的 last_checked_at（跨重启有效）。返回执行状态。
    """
    from app.domains.settings.service import get_value

    if not force and not await get_value("proxy.health_auto", True):
        return "disabled"  # 「自动节点体检」开关关闭：定时路径让路，手动检测（force）照常
    if not clash_manager.runtime.status()["running"]:
        return "clash_not_running"
    async with get_session_factory()() as session:
        row = await session.execute(select(func.max(ClashNode.last_checked_at)))
        latest = row.scalar()
    if not force and latest is not None:
        elapsed = _naive(get_beijing_time_obj()) - latest
        if elapsed < timedelta(hours=HEALTH_INTERVAL_HOURS):
            return "throttled"
    try:
        # 体检覆盖订阅**全部**节点：冷却期节点同样重测。20 并发探测下全量成本很低，
        # 而「只探上一拍没冷却的那部分」会让被跳过节点的结论停留在过去，池容量随
        # 冷却轮转无谓收缩。
        await test_clash_nodes(probe_all=True)
        return "checked"
    except ValueError:
        return "skipped"
    except Exception:  # noqa: BLE001
        log_event(logger, "Clash 节点检测失败", level=logging.ERROR, exc_info=True)
        return "failed"


# ─── 统计（仪表盘口径：一个出口 IP 算一个代理）────────────────

async def pool_stats() -> dict:
    """代理可用性统计——按「一个出口 IP 算一个代理」口径。

    - 手动池：每条 host:port 就是一个代理；可用 = 启用且最近检测 ok
    - Clash：机场多入口常同落地，按出口 IP 去重（与节点检测的
      aliveUnique 同口径）；且只统计内核正在跑的当前订阅——内核没跑，
      账本再健康也没有流量走得到
    """
    async with get_session_factory()() as session:
        pool_rows = list((await session.execute(select(Proxy))).scalars())
    pool_total = len(pool_rows)
    pool_ok = sum(1 for p in pool_rows if p.enabled and p.status == "ok")

    status = clash_manager.runtime.status()
    running = bool(status["running"])
    clash: dict = {
        "running": running,
        "subscriptionId": None,
        "nodes": 0,
        "okNodes": 0,
        "exitIps": 0,
        "okExitIps": 0,
    }
    if running:
        sub_id = await _active_clash_subscription_id(status)
        clash["subscriptionId"] = sub_id
        if sub_id is not None:
            async with get_session_factory()() as session:
                subscription = await session.get(ProxySubscription, sub_id)
                subscription_ready = subscription is None or (
                    not subscription.deprecated and subscription.resource_block_reason() is None
                )
                rows = list(
                    (
                        await session.execute(
                            select(ClashNode).where(ClashNode.subscription_id == sub_id)
                        )
                    ).scalars()
                )
            # 只统计当前订阅内容里仍在的节点（换订阅后账本保留历史行不删）
            current_names: set[str] | None = None
            if status.get("configPath"):
                try:
                    from pathlib import Path

                    text = Path(status["configPath"]).read_text(
                        encoding="utf-8", errors="ignore"
                    )
                    names = clash_manager.parse_node_names(text)
                    if names:
                        current_names = set(names)
                except Exception:  # noqa: BLE001
                    current_names = None
            if current_names is not None:
                rows = [r for r in rows if r.name in current_names]

            all_idents: set[str] = set()
            ok_idents: set[str] = set()
            for r in rows:
                # 出口 IP 未知的节点（IP 探测失败）各自算一个
                ident = r.exit_ip or f"node:{r.id}"
                all_idents.add(ident)
                if r.status == "ok" and subscription_ready:
                    clash["okNodes"] += 1
                    ok_idents.add(ident)
            clash["nodes"] = len(rows)
            clash["exitIps"] = len(all_idents)
            clash["okExitIps"] = len(ok_idents)

    return {
        "pool": {"total": pool_total, "ok": pool_ok},
        "clash": clash,
        "available": pool_ok + clash["okExitIps"],
        "total": pool_total + clash["exitIps"],
    }


async def clash_nodes_overview() -> dict:
    """出口节点明细只读投影：clash_nodes 账本 × 内核当前订阅，逐节点
    名称 / 状态 / 延迟 / 出口 IP / 最近检测时间。

    只读最近一次检测会话（手动检测/首检/定时体检共用）留下的账本事实，
    不做任何探测；内核未跑或无活跃订阅返回空清单。"""
    status = clash_manager.runtime.status()
    if not bool(status["running"]):
        return {"running": False, "nodes": []}
    sub_id = await _active_clash_subscription_id(status)
    if sub_id is None:
        return {"running": True, "nodes": []}
    async with get_session_factory()() as session:
        rows = list(
            (
                await session.execute(
                    select(ClashNode).where(ClashNode.subscription_id == sub_id)
                )
            ).scalars()
        )
    current_names: set[str] | None = None
    if status.get("configPath"):
        try:
            text = Path(status["configPath"]).read_text(encoding="utf-8", errors="ignore")
            names = clash_manager.parse_node_names(text)
            if names:
                current_names = set(names)
        except Exception:  # noqa: BLE001
            current_names = None
    if current_names is not None:
        rows = [r for r in rows if r.name in current_names]
    nodes = [
        {
            "name": r.name,
            "status": r.status,
            "latency_ms": r.latency_ms,
            "exit_ip": r.exit_ip,
            "last_checked_at": r.last_checked_at,
        }
        for r in rows
    ]
    nodes.sort(key=lambda n: (
        n["status"] != "ok",
        n["latency_ms"] if n["latency_ms"] is not None else 1 << 30,
    ))
    return {"running": True, "nodes": nodes}


# ─── 走线日志 ────────────────────────────────────────────────

async def record_event(
    *, kind: str, target: str | None, proxy_label: str | None,
    status_code: int | None = None, duration_ms: int | None = None, error: str | None = None,
) -> None:
    try:
        async with write_session(WritePriority.BACKGROUND) as session:
            session.add(
                ProxyEvent(
                    ts=_naive(get_beijing_time_obj()), kind=kind, target=target,
                    proxy_label=proxy_label, status_code=status_code,
                    duration_ms=duration_ms, error=error,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        log_event(logger, "记录走线日志失败", level=logging.ERROR, exc_info=True)


async def recent_events(limit: int = 200) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(ProxyEvent).order_by(ProxyEvent.id.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": e.id,
            "ts": e.ts.isoformat() if e.ts else None,
            "kind": e.kind,
            "target": e.target,
            "proxyLabel": e.proxy_label,
            "statusCode": e.status_code,
            "durationMs": e.duration_ms,
            "error": e.error,
        }
        for e in rows
    ]


# ─── 存量明文订阅链接静态加密（启动幂等步骤）──────────────────

async def seal_subscription_urls() -> int:
    """明文订阅链接封装为密文（`enc1:` 前缀）：订阅表 + 快照 provenance 列。

    幂等：带密文前缀或空值的行跳过。读取侧对存量明文兼容（open_url 按
    前缀分流），本步骤失败不阻塞启动、不阻塞使用。返回封装条目数。
    """
    from app.domains.proxypool.models import SubscriptionSnapshot

    sealed = 0
    async with write_session(WritePriority.BACKGROUND) as session:
        subs = (await session.execute(select(ProxySubscription))).scalars().all()
        for row in subs:
            value = row.url or ""
            if not value or secretbox.is_encrypted(value):
                continue
            row.url = seal_url(value)
            sealed += 1
        snaps = (await session.execute(select(SubscriptionSnapshot))).scalars().all()
        for row in snaps:
            value = row.url or ""
            if not value or secretbox.is_encrypted(value):
                continue
            row.url = seal_url(value)
            sealed += 1
        await session.commit()
    if sealed:
        log_event(
            logger,
            f"存量明文订阅链接已加密落库 {sealed} 条",
            tag="已修复",
            detail={"条数": sealed},
        )
    return sealed


async def seal_proxy_credentials() -> int:
    """存量明文代理账号口令封装为密文（启动幂等步骤）。

    与订阅链接同一口径：带密文前缀或空值的行跳过；读取侧对存量明文兼容
    （`credentials.open_value` 按前缀分流）。返回封装条目数。
    """
    sealed = 0
    async with write_session(WritePriority.BACKGROUND) as session:
        rows = (await session.execute(select(Proxy))).scalars().all()
        for row in rows:
            touched = False
            for field in ("username", "password"):
                value = getattr(row, field) or ""
                if not value or secretbox.is_encrypted(value):
                    continue
                setattr(row, field, credentials.seal(value))
                touched = True
            if touched:
                sealed += 1
        await session.commit()
    if sealed:
        log_event(
            logger,
            f"存量明文代理凭据已加密落库 {sealed} 条",
            tag="已修复",
            detail={"条数": sealed},
        )
    return sealed


async def reveal_subscription_url(sub_id: int) -> str:
    """显式取用完整订阅链接（编辑 / 复制场景）。

    与列表投影分开：列表一律掩码，只有用户主动点「查看」才解一次密。
    这个函数是订阅链接明文的**唯一**出口，调用点必须是有会话保护的端点。
    """
    row = await get_subscription(sub_id)
    if row is None:
        raise ValueError("订阅不存在")
    return open_url(row.url)
