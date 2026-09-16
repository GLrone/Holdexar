"""CDK 第三方平台价格查询（专用脚本）。

跨域查价通道：由本模块统一代理请求。

数据源与解析：
- SteamPY  GET /xboot/common/plugIn/getGame?subId={sub}&appId={app}&type=subid
           → success && result.keyPrice 为最低挂牌价, result.id 用于详情跳转
- SteamCICI GET /prod-api/user/system/shopGame/list?parentId={app}
           → code==200 && rows[].gameId==subId → lastLowSellPrice

独立运行: python -m app.crawler.cdk_fetcher <appid> [sub_id]
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx

from .proxy import resolve_proxy_url

logger = logging.getLogger(__name__)

STEAMPY_URL = "https://steampy.com/xboot/common/plugIn/getGame"
CICI_URL = "https://steamcici.com/prod-api/user/system/shopGame/list"
STEAMPY_DETAIL_URL = "https://steampy.com/cdkDetail?name=cn&gameId={id}"
TIMEOUT = 15.0


async def fetch_steampy(appid: int, sub_id: int | None, proxy_url: str | None = None) -> dict[str, Any]:
    """SteamPY 查询。返回 {listed, price, url}。"""
    empty = {"listed": False, "price": None, "url": f"https://steampy.com/game/{appid}"}
    if not sub_id:
        return empty
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy_url) as client:
            resp = await client.get(
                STEAMPY_URL,
                params={"subId": sub_id, "appId": appid, "type": "subid"},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://steampy.com/"},
            )
            data = resp.json()
        if data.get("success") and data.get("result"):
            result = data["result"]
            key_price = result.get("keyPrice")
            game_id = result.get("id")
            return {
                "listed": bool(key_price),
                "price": f"¥{key_price}" if key_price else None,
                "url": STEAMPY_DETAIL_URL.format(id=game_id) if game_id else empty["url"],
            }
        return empty
    except Exception as e:  # noqa: BLE001
        logger.warning("SteamPY 查询失败 appid=%s: %s", appid, e)
        return {**empty, "error": str(e)}


async def fetch_steamcici(appid: int, sub_id: int | None, proxy_url: str | None = None) -> dict[str, Any]:
    """SteamCICI 查询。返回 {listed, price, url}。"""
    url = f"https://www.steamcici.com/"
    if not sub_id:
        return {"listed": False, "price": None, "url": url}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, proxy=proxy_url) as client:
            resp = await client.get(
                CICI_URL,
                params={"parentId": appid},
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.steamcici.com/"},
            )
            data = resp.json()
        if data.get("code") == 200 and data.get("rows"):
            row = next(
                (r for r in data["rows"] if str(r.get("gameId")) == str(sub_id)),
                None,
            )
            price = row.get("lastLowSellPrice") if row else None
            if row and price is not None:
                return {"listed": True, "price": f"¥{price}", "url": url}
            return {"listed": False, "price": None, "url": url}
        return {"listed": False, "price": None, "url": url}
    except Exception as e:  # noqa: BLE001
        logger.warning("SteamCICI 查询失败 appid=%s: %s", appid, e)
        return {"listed": False, "price": None, "url": url, "error": str(e)}


async def resolve_sub_id(appid: int) -> int | None:
    """从库内 current 表取标准版 sub_id（CN 优先，回退任意区）。

    sub_id 是包元数据，不依赖当前价格状态（missing 行同样有效）。
    """
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.domains.games.models import GameCurrentPrice

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(GameCurrentPrice.sub_id)
                .where(
                    GameCurrentPrice.appid == appid,
                    GameCurrentPrice.sub_id.is_not(None),
                    GameCurrentPrice.sub_id > 0,
                )
                .order_by(
                    # CN 优先
                    (GameCurrentPrice.region_code == "CN").desc(),
                    GameCurrentPrice.sub_id.asc(),
                )
                .limit(1)
            )
        ).scalar()
    return int(row) if row else None


async def resolve_proxy_for_request() -> str | None:
    """本次查价代理：策略引擎优先（代理优先为默认），回落环境变量。"""
    try:
        from app.domains.proxies import service as proxies_service

        proxy = await proxies_service.resolve_proxy_url()
        if proxy:
            return proxy
    except Exception:  # noqa: BLE001
        pass
    return resolve_proxy_url()


# ── 进程内 TTL 缓存（stale-while-revalidate，范式同 family service）────────
# 详情页每次进页都会打 SteamPY/SteamCICI 两个外网接口，而挂牌价变化以分钟计。
# 时间窗内的重复请求（详情页往返、切板块回跳）直接复用；过期后**先回旧值再后台
# 刷新**，调用方不再等一次最长 15s 的 HTTPS 往返。失败结果用更短 TTL：失败往往
# 是瞬时的（代理闪断），不能把坏响应钉满一个完整窗口。
_CDK_TTL_SECONDS = 300
_CDK_ERROR_TTL_SECONDS = 60
_CDK_CACHE_MAX = 256
_CDK_CACHE: dict[int | tuple[int, int | None], tuple[float, dict[str, Any]]] = {}
_CDK_REFRESHING: set[int | tuple[int, int | None]] = set()


def invalidate_cdk_cache() -> None:
    """清空 CDK 缓存（测试 / 强制刷新用）。"""
    _CDK_CACHE.clear()


def _cdk_had_error(data: dict[str, Any]) -> bool:
    return bool(data.get("steampy", {}).get("error") or data.get("steamcici", {}).get("error"))


async def _fetch_cdk_uncached(appid: int, sub_id: int | None = None, proxy_url: str | None = None) -> dict[str, Any]:
    """实时查询：sub 解析 + 双平台并发。sub_id 不传时从库内自动解析。"""
    if sub_id is None:
        sub_id = await resolve_sub_id(appid)
    proxy = proxy_url if proxy_url is not None else await resolve_proxy_for_request()
    steampy, cici = await asyncio.gather(
        fetch_steampy(appid, sub_id, proxy),
        fetch_steamcici(appid, sub_id, proxy),
    )
    return {"appid": appid, "subId": sub_id, "steampy": steampy, "steamcici": cici}


async def fetch_cdk(appid: int, sub_id: int | None = None, proxy_url: str | None = None) -> dict[str, Any]:
    """查询双平台 CDK 状态（TTL 缓存优先）。sub_id 不传时从库内自动解析。"""
    now = time.monotonic()
    # 显式 sub_id 时以 (appid, sub_id) 为缓存键，否则以 appid 为键
    cache_key = (appid, sub_id) if sub_id is not None else appid
    cached = _CDK_CACHE.get(cache_key)
    if cached is not None:
        ts, data = cached
        ttl = _CDK_ERROR_TTL_SECONDS if _cdk_had_error(data) else _CDK_TTL_SECONDS
        if now - ts < ttl:
            return data
        # 过期有旧值：立即回旧值，同键的后台刷新只挂一个
        if cache_key not in _CDK_REFRESHING:
            _CDK_REFRESHING.add(cache_key)

            async def _bg(key=appid, sid=sub_id, ck=cache_key) -> None:
                try:
                    fresh = await _fetch_cdk_uncached(key, sid)
                    _CDK_CACHE[ck] = (time.monotonic(), fresh)
                except Exception:  # noqa: BLE001 —— 刷新失败保留旧值并前移时间戳
                    if ck in _CDK_CACHE:
                        _CDK_CACHE[ck] = (time.monotonic(), _CDK_CACHE[ck][1])
                finally:
                    _CDK_REFRESHING.discard(ck)

            asyncio.create_task(_bg())
        return data
    fresh = await _fetch_cdk_uncached(appid, sub_id, proxy_url)
    if len(_CDK_CACHE) >= _CDK_CACHE_MAX:
        _CDK_CACHE.pop(next(iter(_CDK_CACHE)))
    _CDK_CACHE[cache_key] = (now, fresh)
    return fresh


if __name__ == "__main__":
    import asyncio
    import json
    import sys

    if len(sys.argv) < 2:
        print("用法: python -m app.crawler.cdk_fetcher <appid>")
        sys.exit(1)
    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(fetch_cdk(int(sys.argv[1])))
    print(json.dumps(result, ensure_ascii=False, indent=2))
