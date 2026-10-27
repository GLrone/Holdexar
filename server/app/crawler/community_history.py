"""社区历史价采集适配器：第三方社区平台历史价格接口的按需回填。

单一采集适配器：签名、请求、解析都在本模块完成；写入走统一价格写入口
（write_gate + game_price_history 幂等去重），不新增并行数据库写入口。
签名是公开的客户端请求签名（时间戳+规整路径+nonce 映射交织摘要校验），
不含任何凭据或共享密钥；接口为匿名只读，每 (appid, region) 一次请求，
由用户查看图表触发，失败不自动重试（下次查看/手动触发即重试）。

能力边界（上游口径）：
- 一次返回该游戏该区上游全史，起止日期与天数参数均不可指定；
- 请求侧支持 package_id（Steam sub 号）按包取序列：包上下文即归属证据
  （响应不回显 sub，但请求上下文可证明序列属于该包；appid 级缺省请求
  与标准包序列重合）。回填先按现售包写已识别行（版本分类沿用本地
  观测佐证，本地无佐证则保持未知），appid 级序列降级为兜底未知通道；
- 兜底未知通道的行 product_unknown=1（独立未知序列，永不并入标准版
  或任何具体版本）；与已识别行同日同价的点视为重复观测直接跳过
  （佐证去重，包通道写入在前、appid 通道在后，天然去重）；
- 点为变价节点（含折扣变化）+ 当日现价节点，日级时间精度（北京时间零点）；
- 上游各区数据起点不同（同一游戏不同区可差数年），未覆盖区间明确不可补；
- 价格为主单位字符串（整数或一位小数），×100 入库——与库内全币种统一
  ×100 口径一致，但比官方渠道的最小单位观测精度低；
- 空 result 即上游无数据（不区分无游戏/无该区/免费），无错误码可辨；
- 限流语义未知：无自动重试风暴、无并发节流规避，单请求由查看触发。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
from sqlalchemy import or_, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import WritePriority, write_session
from app.core.logging import log_event
from app.domains.games.models import GamePriceHistory, HistoryBackfillState

logger = logging.getLogger(__name__)

# 接口与路径签名（签名只签路径不签参数）
_HOST = "https://api.xiaoheihe.cn"
HISTORY_PATH = "/game/get_game_prices/history/v2"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

# web 端公共设备参数（签名公共参数的一部分）
_ENV_PARAMS = {
    "app": "heybox",
    "os_type": "web",
    "x_app": "heybox_website",
    "x_client_type": "web",
    "x_os_type": "Windows",
    "x_client_version": "",
    "client_type": "web",
    "web_version": "3.0",
    "version": "999.0.4",
}

# 请求签名字符表与校验常量（公开客户端签名协议）
_CHARSET = "AB45STUVWZEFGJ6CH01D237IXYPQRKLMN89"

# 库内区域代码 → 上游 cc 代码（其余恒等；pt/gb 直接传上游返回空结果）
_REGION_CC_ALIASES = {"pt": "eu", "gb": "uk"}

# 库内价格口径 = 主单位 × 100（全币种统一，含无小数位货币，与官方
# 渠道写入侧同口径；上游返回主单位字符串）
_PRICE_SCALE = 100

# 历史点落库的产品身份：上游 appid 级序列商品身份不可辨 → 显式未知，
# 独立序列呈现，不冒充标准版/任何版本（判定的唯一事实源 games/identity）
_COMMUNITY_ROW_FIXED = {
    "sub_id": None,
    "is_gold": False,
    "is_bundle": False,
    "version_suffix": None,
    "original_price": None,
    "price_status": "ok",
    "cny_fen": None,
    "discount_end_ts": None,
    "discount_desc": None,
    "bundle_id": None,
    "bundle_discount_pct": None,
    "steam_event_key": None,
    "source": "community",
    "product_unknown": True,
}

# 北京时间裸日期（库内 snapshot_at 统一裸北京时间口径）
_BJ = timezone(timedelta(hours=8))


def region_cc(region_code: str) -> str:
    """库内区域代码 → 上游 cc 参数。"""
    cc = region_code.lower()
    return _REGION_CC_ALIASES.get(cc, cc)


# ── 请求签名（公开客户端协议，无凭据）─────────────────────────────────


def _map_chars(text: str, table: str) -> str:
    return "".join(table[ord(ch) % len(table)] for ch in text)


def _interleave(parts: list[str], length: int) -> str:
    out = ""
    for idx in range(max(len(p) for p in parts)):
        for p in parts:
            if idx < len(p):
                out += p[idx]
    return out[:length]


def _mix_column(block: list[int]) -> list[int]:
    def x2(v: int) -> int:
        v &= 0xFF
        return ((v << 1) ^ 0x1B) & 0xFF if v & 0x80 else (v << 1) & 0xFF

    def c3(v):
        return x2(v) ^ v

    def c2(v):
        return c3(x2(v))

    def c1(v):
        return c2(c3(x2(v)))

    def c0(v):
        return c1(v) ^ c2(v) ^ c3(v)

    a, b, c, d = block
    return [
        c0(a) ^ c1(b) ^ c2(c) ^ c3(d),
        c3(a) ^ c0(b) ^ c1(c) ^ c2(d),
        c2(a) ^ c3(b) ^ c0(c) ^ c1(d),
        c1(a) ^ c2(b) ^ c3(c) ^ c0(d),
    ]


def _normalize_path(path: str) -> str:
    return "/" + "/".join(seg for seg in path.split("/") if seg) + "/"


def _hkey(path: str, ts: int, nonce: str) -> str:
    mixed = _interleave(
        [
            _map_chars(str(ts), _CHARSET[: len(_CHARSET) - 2]),
            _map_chars(_normalize_path(path), _CHARSET),
            _map_chars(nonce, _CHARSET),
        ],
        20,
    )
    digest = hashlib.md5(mixed.encode()).hexdigest()
    tail = [ord(c) for c in digest[-6:]]
    checksum = (sum(_mix_column(tail[:4])) + tail[4] + tail[5]) % 100
    head = _map_chars(digest[:5], _CHARSET[: len(_CHARSET) - 4])
    return head + str(checksum).zfill(2)


def _signed_params(path: str) -> dict[str, str]:
    """公共参数 + 签名。签名用 ts+1 计算，_time 落原始 ts（平台约定）。"""
    ts = int(time.time())
    nonce = (
        hashlib.md5(f"{ts}{time.time()}{random.random()}".encode())
        .hexdigest()
        .upper()
    )
    params = dict(_ENV_PARAMS)
    params["heybox_id"] = ""
    params["_time"] = str(ts)
    params["nonce"] = nonce
    params["hkey"] = _hkey(path, ts + 1, nonce)
    return params


# ── 请求与解析 ────────────────────────────────────────────────────────


class CommunityHistoryError(Exception):
    """网络请求失败或响应不可解析；消息面向日志与同步状态。"""


async def fetch_history_points(
    appid: int, region_code: str, package_id: int | None = None
) -> list[dict]:
    """取该游戏该区的上游历史点（原始字段）。

    package_id 传入时按该 Steam 包取序列（请求上下文即归属证据）；
    返回 [{date(unix 秒), price(str), discount(int), currency(str)}]；
    上游无数据返回空列表（HTTP 200 + ok + 空 result 是「无数据」的表达，
    不是错误）。
    """
    params = {
        **_signed_params(HISTORY_PATH),
        "appid": str(int(appid)),
        "platf": "steam",
        "cc": region_cc(region_code),
    }
    if package_id:
        params["package_id"] = str(int(package_id))
    headers = {
        "User-Agent": _UA,
        "Origin": "https://www.xiaoheihe.cn",
        "Referer": f"https://www.xiaoheihe.cn/app/topic/game/pc/{int(appid)}",
    }
    try:
        async with httpx.AsyncClient(timeout=20.0, headers=headers, trust_env=False) as client:
            resp = await client.get(f"{_HOST}{HISTORY_PATH}", params=params)
            resp.raise_for_status()
            payload = resp.json()
    except httpx.HTTPError as exc:
        raise CommunityHistoryError(f"网络请求失败：{exc}") from exc
    except ValueError as exc:
        raise CommunityHistoryError("响应不是 JSON") from exc
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        msg = str(payload.get("msg") or "响应状态异常") if isinstance(payload, dict) else "未知响应"
        raise CommunityHistoryError(f"上游返回异常：{msg[:120]}")
    result = payload.get("result") or {}
    points = result.get("prices") or []
    if not isinstance(points, list):
        raise CommunityHistoryError("上游点集字段形态异常")
    return [p for p in points if isinstance(p, dict)]


def _price_to_minor(raw: Any) -> int | None:
    """主单位字符串 → 库内 ×100 口径的整数；不可解析返回 None（跳过该点）。"""
    try:
        return int((Decimal(str(raw)) * _PRICE_SCALE).to_integral_value(rounding="ROUND_HALF_UP"))
    except (InvalidOperation, ValueError):
        return None


def _point_snapshot_at(raw: Any) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(raw), _BJ).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def normalize_points(
    appid: int,
    region_code: str,
    points: list[dict],
    package_id: int | None = None,
    identity: dict | None = None,
) -> list[dict]:
    """上游点 → 历史事实行，坏点跳过。

    package_id 给定（包通道）：sub_id 取包号——归属来自请求上下文；
    identity 给定（本地同包观测的版本分类佐证）时套用其
    version_suffix/is_gold/is_bundle 并落已识别态，否则保持未知态
    （sub 已知但版本分类未验证，不进标准版谓词）。
    缺省（appid 兜底通道）：商品身份不可辨，全量未知态。
    """
    rows: list[dict] = []
    for p in points:
        price = _price_to_minor(p.get("price"))
        snapshot_at = _point_snapshot_at(p.get("date"))
        if price is None or price <= 0 or snapshot_at is None:
            continue
        fixed = dict(_COMMUNITY_ROW_FIXED)
        if package_id:
            fixed["sub_id"] = int(package_id)
            if identity:
                fixed.update(
                    version_suffix=identity.get("version_suffix"),
                    is_gold=bool(identity.get("is_gold")),
                    is_bundle=bool(identity.get("is_bundle")),
                )
                fixed["product_unknown"] = False
        rows.append(
            {
                "appid": int(appid),
                "region_code": region_code.upper(),
                "currency": str(p.get("currency") or "").upper() or None,
                "price": price,
                "discount_percent": int(p.get("discount") or 0),
                "snapshot_at": snapshot_at,
                **fixed,
            }
        )
    return rows


async def _sale_sub_for_region(appid: int, region_code: str) -> int | None:
    """该区当前在售 sub（与读侧 get_game_history 的并集右项同口径：
    ok 行、sub_id 非空非 0、updated_at 最新一条）。"""
    from sqlalchemy import select as _select

    from app.core.database import get_session_factory
    from app.domains.games.models import GameCurrentPrice

    async with get_session_factory()() as session:
        sub = (
            await session.execute(
                _select(GameCurrentPrice.sub_id)
                .where(
                    GameCurrentPrice.appid == int(appid),
                    GameCurrentPrice.region_code == region_code.upper(),
                    GameCurrentPrice.price_status == "ok",
                    GameCurrentPrice.sub_id.is_not(None),
                )
                .order_by(GameCurrentPrice.updated_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
    return int(sub) if sub else None


async def _local_sub_identity(
    appid: int, region_code: str, sub_id: int
) -> dict | None:
    """本地同包观测的版本分类（最近一条 steam 行）；无本地行返回 None。"""
    from sqlalchemy import select as _select

    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                _select(
                    GamePriceHistory.version_suffix,
                    GamePriceHistory.is_gold,
                    GamePriceHistory.is_bundle,
                )
                .where(
                    GamePriceHistory.appid == int(appid),
                    GamePriceHistory.region_code == region_code.upper(),
                    GamePriceHistory.sub_id == int(sub_id),
                    or_(
                        GamePriceHistory.product_unknown.is_(False),
                        GamePriceHistory.product_unknown.is_(None),
                    ),
                )
                .order_by(GamePriceHistory.snapshot_at.desc())
                .limit(1)
            )
        ).first()
    if row is None:
        return None
    return {
        "version_suffix": row.version_suffix,
        "is_gold": bool(row.is_gold),
        "is_bundle": bool(row.is_bundle),
    }


# ── 单飞任务与状态 ────────────────────────────────────────────────────

# 同一 (appid, region) 只允许一个在飞任务：键为小写 region
_INFLIGHT: dict[tuple[int, str], asyncio.Task] = {}


def inflight_regions(appid: int) -> list[str]:
    return sorted(r for (a, r) in _INFLIGHT if a == appid)


def is_inflight(appid: int, region_code: str) -> bool:
    return (int(appid), region_code.lower()) in _INFLIGHT


def _now() -> datetime:
    return datetime.now()


async def _write_rows(appid: int, region_code: str, rows: list[dict]) -> int:
    """历史事实行幂等落库（BACKGROUND 写闸）。INSERT OR IGNORE：六段
    逻辑键命中即跳过——绝不覆盖本地官方观测，重复回填零重复。与本地
    已识别行（steam 观测 / 包通道）同日同价的点视为同一观测的重复
    表达，直接跳过不落（佐证去重）。返回新增行数。
    """
    from app.domains.games.derived import update_price_derived

    async with write_session(WritePriority.BACKGROUND, label="history_backfill") as session:
        insertable = rows
        if rows:
            # 佐证去重：窗口内已识别行 (日期, 价格) 集合；命中即跳过该社区点
            lo = min(r["snapshot_at"] for r in rows).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            # 上界取最大日收盘：steam 观测行带时刻，同日必晚于社区零点行
            hi = max(r["snapshot_at"] for r in rows).replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
            identified = (
                await session.execute(
                    select(GamePriceHistory.snapshot_at, GamePriceHistory.price).where(
                        GamePriceHistory.appid == int(appid),
                        GamePriceHistory.region_code == region_code.upper(),
                        GamePriceHistory.price.is_not(None),
                        or_(
                            GamePriceHistory.product_unknown.is_(False),
                            GamePriceHistory.product_unknown.is_(None),
                        ),
                        GamePriceHistory.snapshot_at >= lo,
                        GamePriceHistory.snapshot_at <= hi,
                    )
                )
            ).all()
            seen = {(snap.date(), int(price)) for snap, price in identified}
            insertable = [
                r for r in rows if (r["snapshot_at"].date(), int(r["price"])) not in seen
            ]
        inserted = 0
        if insertable:
            stmt = sqlite_insert(GamePriceHistory).values(insertable)
            result = await session.execute(stmt.on_conflict_do_nothing())
            inserted = result.rowcount or 0
        if inserted:
            await update_price_derived([int(appid)], session=session)
        await session.commit()
    return inserted


async def _record_sync_state(
    appid: int, region_code: str, upstream_rows: int, inserted: int
) -> None:
    """整轮回填末尾写一次同步状态（包通道 + 兜底通道的合计口径）。"""
    now = _now()
    ok_at = now if upstream_rows else None
    empty_at = now if not upstream_rows else None
    async with write_session(WritePriority.BACKGROUND, label="history_backfill") as session:
        await session.execute(
            sqlite_insert(HistoryBackfillState)
            .values(
                appid=int(appid),
                region_code=region_code.upper(),
                last_ok_at=ok_at,
                last_empty_at=empty_at,
                last_error_at=None,
                last_error=None,
                points_stored=inserted,
                updated_at=now,
            )
            .on_conflict_do_update(
                index_elements=[
                    HistoryBackfillState.appid,
                    HistoryBackfillState.region_code,
                ],
                set_={
                    "last_ok_at": ok_at,
                    "last_empty_at": empty_at,
                    "last_error_at": None,
                    "last_error": None,
                    "points_stored": inserted,
                    "updated_at": now,
                },
            )
        )
        await session.commit()


async def backfill_region(appid: int, region_code: str) -> dict:
    """单飞回填一个 (appid, region)：抓取 → 幂等落库 → 状态更新 → 史低标记。

    返回 {"state": "ok"|"empty"|"error", "points": n, "error": str|None}。
    抓取/写入任一失败只记状态，不抛出（调用方是端点与前端轮询）。
    """
    region = region_code.lower()
    try:
        total_rows = 0
        total_inserted = 0
        # 阶段一（包通道）：现售 sub 按包取序列——归属=请求上下文；
        # 版本分类沿用本地同包观测佐证，无佐证则 sub 已知、分类未知
        sale_sub = await _sale_sub_for_region(appid, region)
        if sale_sub:
            identity = await _local_sub_identity(appid, region, sale_sub)
            pkg_rows = normalize_points(
                appid,
                region,
                await fetch_history_points(appid, region, package_id=sale_sub),
                package_id=sale_sub,
                identity=identity,
            )
            total_inserted += await _write_rows(appid, region, pkg_rows)
            total_rows += len(pkg_rows)
        # 阶段二（兜底通道）：appid 级序列——阶段一已写的同日同价点
        # 被佐证去重跳过，残余差异保持未知态
        rows = normalize_points(appid, region, await fetch_history_points(appid, region))
        total_inserted += await _write_rows(appid, region, rows)
        total_rows += len(rows)
        inserted = total_inserted
        await _record_sync_state(appid, region, total_rows, total_inserted)
    except CommunityHistoryError as exc:
        await _record_error(appid, region, str(exc))
        log_event(
            logger,
            "社区历史回填失败",
            tag="未完成",
            level=logging.WARNING,
            detail={"AppID": appid, "区域": region.upper(), "原因": str(exc)},
        )
        return {"state": "error", "points": 0, "error": str(exc)[:200]}
    except Exception as exc:  # noqa: BLE001 —— 状态化收口，不上抛
        await _record_error(appid, region, f"{type(exc).__name__}: {exc}")
        log_event(
            logger,
            "社区历史回填异常",
            tag="未完成",
            level=logging.ERROR,
            exc_info=True,
            detail={"AppID": appid, "区域": region.upper()},
        )
        return {"state": "error", "points": 0, "error": f"{type(exc).__name__}"}

    log_event(
        logger,
        "社区历史回填完成" if total_rows else "社区历史无数据",
        tag="成功" if total_rows else "空",
        detail={
            "AppID": appid,
            "区域": region.upper(),
            "现售包": sale_sub,
            "上游点数": total_rows,
            "新增": inserted,
        },
    )
    return {"state": "ok" if total_rows else "empty", "points": total_rows, "error": None}


async def _record_error(appid: int, region_code: str, message: str) -> None:
    now = _now()
    try:
        async with write_session(WritePriority.BACKGROUND, label="history_backfill") as session:
            await session.execute(
                sqlite_insert(HistoryBackfillState)
                .values(
                    appid=int(appid),
                    region_code=region_code.upper(),
                    last_error_at=now,
                    last_error=message[:200],
                    updated_at=now,
                )
                .on_conflict_do_update(
                    index_elements=[
                        HistoryBackfillState.appid,
                        HistoryBackfillState.region_code,
                    ],
                    set_={
                        "last_error_at": now,
                        "last_error": message[:200],
                        "updated_at": now,
                    },
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 —— 状态写失败不影响主流程
        log_event(logger, "回填失败状态落库失败", level=logging.WARNING, exc_info=True)


async def start_backfill(appid: int, region_code: str) -> dict:
    """派发一个回填任务（同键已在飞则直接返回运行态，不重复派发）。"""
    appid = int(appid)
    region = region_code.lower()
    key = (appid, region)
    task = _INFLIGHT.get(key)
    if task is not None and not task.done():
        return {"started": False, "state": "running"}

    async def _run() -> dict:
        try:
            return await backfill_region(appid, region)
        finally:
            _INFLIGHT.pop(key, None)

    _INFLIGHT[key] = asyncio.create_task(_run())
    return {"started": True, "state": "running"}


async def backfill_brief(appid: int, region_code: str) -> dict:
    """图表只读接口附带的最小同步状态（行为状态，非事实）。"""
    appid = int(appid)
    region = region_code.lower()
    if is_inflight(appid, region):
        return {"state": "running", "lastOkAt": None, "lastError": None}
    from sqlalchemy import select

    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(HistoryBackfillState).where(
                    HistoryBackfillState.appid == appid,
                    HistoryBackfillState.region_code == region.upper(),
                )
            )
        ).scalar_one_or_none()
    if row is None:
        return {"state": "idle", "lastOkAt": None, "lastError": None}
    if row.last_error_at is not None and (
        row.last_ok_at is None or row.last_error_at > row.last_ok_at
    ):
        state = "error"
    elif row.last_empty_at is not None and (
        row.last_ok_at is None or row.last_empty_at > row.last_ok_at
    ):
        state = "empty"
    else:
        state = "ok"
    return {
        "state": state,
        "lastOkAt": row.last_ok_at.isoformat() if row.last_ok_at else None,
        "lastError": row.last_error,
    }
