"""外部时间权威模块：太平洋夏令时判定 + 价格爬取锚点网格推算。

池价格爬取对齐 Steam 每日折扣刷新时刻——
太平洋 10:00 = 北京 01:00（夏令时/PDT）/ 02:00（冬令时/PST）锚点，
锚点 + 6h 步进网格（1/7/13/19 或 2/8/14/20），DST 切换日锚点漂移
网格自动重算。

DST 判定以「请求外部时间」为权威（用户明确要求）：timeapi.io 的
America/Los_Angeles 端点直接返回 dstActive 布尔，不依赖本机 tzdata
新鲜度。请求走 rates 同款策略代理优先（Steam 域直连被墙的默认姿势），
代理失败再直连（timeapi.io 非 Steam 域，国内直连实测可达），全链
失败回落本地 zoneinfo（tzdata 2025.3；打包产物已实证含
zoneinfo/america/Los_Angeles）。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import httpx

from app.crawler.utils import BEIJING_TZ

logger = logging.getLogger(__name__)

PACIFIC_TZ = ZoneInfo("America/Los_Angeles")
TIMEAPI_URL = "https://timeapi.io/api/Time/current/zone?timeZone=America/Los_Angeles"
TIMEAPI_ZONE = "America/Los_Angeles"

GRID_STEP = timedelta(hours=6)


def anchor_hour(is_dst: bool) -> int:
    """Steam 每日折扣刷新对应的北京时间锚点小时：夏令时 01:00 / 冬令时 02:00。"""
    return 1 if is_dst else 2


def next_grid_time(now: datetime, anchor: int) -> datetime:
    """锚点 + 6h 步进网格中严格晚于 now 的下一时刻（now 须为北京 aware）。

    DST 切换日锚点 1↔2 漂移时网格整体重算：切换前的最后一格与
    切换后新网格的首格间隔不再是精确 6h（秋季 1h/春季 5h，每年各一次），
    之后恢复 6h 步进。
    """
    base = now.replace(hour=anchor, minute=0, second=0, microsecond=0)
    while base <= now:
        base += GRID_STEP
    return base


async def _resolve_proxy() -> str | None:
    """策略引擎优先（rates service 同款），回落环境变量。"""
    proxy = None
    try:
        from app.domains.proxies import service as proxies_service

        proxy = await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001 —— 策略引擎不可用不阻断时间判定
        proxy = None
    if proxy is None:
        from app.crawler.proxy import resolve_proxy_url

        proxy = resolve_proxy_url()
    return proxy


async def _http_get_json(url: str, proxy: str | None) -> dict | None:
    """单次请求；直连通道显式 trust_env=False（防系统/环境代理死节点
    劫持"直连"——Verge 系统代理开着但节点死时，继承环境就不是真直连）。"""
    try:
        client = (
            httpx.AsyncClient(timeout=6, proxy=proxy)
            if proxy
            else httpx.AsyncClient(timeout=6, trust_env=False)
        )
        async with client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()
    except Exception as e:  # noqa: BLE001
        logger.info("时间源请求失败（%s 通道）: %s", "代理" if proxy else "直连", e)
        return None


def _warn_clock_drift(data: dict) -> None:
    """外部墙钟 vs 本机钟偏差诊断（>5min 告警；网格触发仍按本机钟）。"""
    try:
        raw = str(data.get("dateTime", ""))
        # timeapi.io 分数位 7 位数字，fromisoformat 只认 6 位——截尾
        ext = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", raw))
        drift = abs((ext.replace(tzinfo=PACIFIC_TZ) - datetime.now(PACIFIC_TZ)).total_seconds())
        if drift > 300:
            logger.warning("外部时间与本机时钟偏差 %.0f 分钟——本机时钟可能不准", drift / 60)
    except Exception:  # noqa: BLE001 —— 诊断性质，失败静默
        pass


async def fetch_pacific_dst() -> tuple[bool, str]:
    """判定当前太平洋时间是否夏令时。永不抛异常。

    优先级：timeapi.io（代理通道 → 直连通道）→ 本地 zoneinfo。
    返回 (is_dst, 来源)。
    """
    proxy = await _resolve_proxy()
    attempts: list[str | None] = [proxy, None] if proxy else [None]
    for attempt_proxy in attempts:
        data = await _http_get_json(TIMEAPI_URL, attempt_proxy)
        if not isinstance(data, dict):
            continue
        zone = data.get("timeZone")
        if zone is not None and zone != TIMEAPI_ZONE:
            logger.warning("时间源响应时区异常（%s），不采信", zone)
            continue
        dst = data.get("dstActive")
        if isinstance(dst, bool):
            _warn_clock_drift(data)
            return dst, "timeapi.io"
    is_dst = datetime.now(PACIFIC_TZ).dst() != timedelta(0)
    return is_dst, "zoneinfo"


async def probe_next_grid() -> tuple[datetime, int, bool, str]:
    """外部时间判 DST → 锚点 → 下一网格时刻。调度器自续约的单一入口。

    返回 (下一网格时刻[北京 aware], 锚点小时, 是否夏令时, 判定来源)。
    """
    is_dst, source = await fetch_pacific_dst()
    hour = anchor_hour(is_dst)
    return next_grid_time(datetime.now(BEIJING_TZ), hour), hour, is_dst, source


def local_next_grid() -> tuple[datetime, int, bool, str]:
    """启动初锚（同步零网络）：本地 zoneinfo 判 DST → 下一网格时刻。

    start_scheduler 同步上下文无法 await 外部请求，先用本地推算立即
    挂上锚点网格；随后 _anchor_probe_after_start 异步请求外部时间纠偏
    （权威源；非切换日两者恒一致，切换日探针保证网格换轨）。
    """
    is_dst = datetime.now(PACIFIC_TZ).dst() != timedelta(0)
    hour = anchor_hour(is_dst)
    return next_grid_time(datetime.now(BEIJING_TZ), hour), hour, is_dst, "zoneinfo(启动初锚)"
