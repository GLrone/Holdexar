"""价格事实事件（Price Event）：本轮观察相对此前有效观察的变化，一行一事件。
检测在 Cycle finalizing 统一做（建立在最终有效结果上，不由各 job 自产——防
「暂时失败→补抓成功」误报成对）。判定前提：①未观察不产生事件（没抓到推不出
不可用）；②只认变化（hl_flag/pp_flag 等持续量按当前值机械生成会每轮重复）；
③上一有效观察取持久证据——价格用 INSERT-only 的 game_price_history，状态类
用本表最近一条状态事件+窗口前有效快照，首次观察不产生。价格比较用该区货币
price 不用 cny_fen（防汇率波动误判），0 价归 FREE_PROMO。region_code=NULL=
游戏级对象（free_kind/removed_at），历史低价/永降按 CN 标准版判定。结果窗口
与 Coverage 同口径 [started_at, finished_at]，窗口内并行的 repair/手动来源不分。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime

from sqlalchemy import (
    BigInteger, DateTime, Index, Integer, JSON, String,
    bindparam, column, func, select, text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import WritePriority, get_session_factory, write_session, Base
from app.core.database import write_gate
from app.domains.crawl.cycle import PriceCycle
from app.domains.games.models import Game, GameCurrentPrice

logger = logging.getLogger(__name__)

PRICE_DROP = "PRICE_DROP"
PRICE_INCREASE = "PRICE_INCREASE"
NEW_HISTORICAL_LOW = "NEW_HISTORICAL_LOW"
HISTORICAL_LOW_MATCH = "HISTORICAL_LOW_MATCH"
PERMANENT_PRICE_CHANGE = "PERMANENT_PRICE_CHANGE"
REGION_LOCKED = "REGION_LOCKED"
REGION_UNLOCKED = "REGION_UNLOCKED"
PRICE_UNAVAILABLE = "PRICE_UNAVAILABLE"
PRICE_RESTORED = "PRICE_RESTORED"
FREE_PROMO = "FREE_PROMO"
REMOVED = "REMOVED"

EVENT_TYPES = (
    PRICE_DROP, PRICE_INCREASE,
    NEW_HISTORICAL_LOW, HISTORICAL_LOW_MATCH, PERMANENT_PRICE_CHANGE,
    REGION_LOCKED, REGION_UNLOCKED, PRICE_UNAVAILABLE, PRICE_RESTORED,
    FREE_PROMO, REMOVED,
)

# 状态类事件：它们的 current_json.status 就是该单元此后的「上一状态」
STATUS_EVENT_TYPES = (REGION_LOCKED, REGION_UNLOCKED, PRICE_UNAVAILABLE, PRICE_RESTORED)

# 历史低价 / 永降的判定区域（与 hl_flag / pp_flag 的计算对象一致）
BENCHMARK_REGION = "CN"

# 失败态：明确表示「本轮没有拿到可购买价格」
_FAILED_STATUSES = ("missing", "blocked")

# 证据查询在 SQL 侧按观察单元下推（每单元一次索引探测取 argmax/聚合行），
# 结果集与全史解耦；region_code 库内单一来源大写，SQL 按大写对齐索引前缀。
_GPH_STANDARD = (
    "(is_gold IS NULL OR is_gold = 0) "
    "AND (version_suffix IS NULL OR version_suffix = '') "
    "AND (is_bundle IS NULL OR is_bundle = 0)"
)
_GPH_SNAP_SQL = text(
    "SELECT w.a, w.r, h2.price, h2.original_price, h2.cny_fen, h2.snapshot_at, h2.id "
    "FROM (SELECT value->>'a' AS a, value->>'r' AS r, CAST(value->>'s' AS INTEGER) AS s "
    "FROM json_each(:want)) w "
    "JOIN game_price_history h2 ON h2.id = ("
    "SELECT h3.id FROM game_price_history h3 "
    "WHERE h3.appid = w.a AND h3.region_code = w.r AND COALESCE(h3.sub_id, 0) = w.s "
    "AND h3.snapshot_at IS NOT NULL AND h3.snapshot_at >= :started AND h3.snapshot_at <= :ended "
    f"AND {_GPH_STANDARD} "
    "ORDER BY h3.snapshot_at DESC, h3.id DESC LIMIT 1)"
).columns(
    column("a", Integer), column("r", String), column("price", BigInteger),
    column("original_price", BigInteger), column("cny_fen", BigInteger),
    column("snapshot_at", DateTime), column("id", Integer),
).bindparams(bindparam("started", type_=DateTime), bindparam("ended", type_=DateTime))
_GPH_PREV_SQL = text(
    "SELECT w.a, w.r, "
    "(SELECT MIN(h1.price) FROM game_price_history h1 "
    " WHERE h1.appid = w.a AND h1.region_code = w.r AND COALESCE(h1.sub_id, 0) = w.s "
    " AND h1.snapshot_at IS NOT NULL AND h1.snapshot_at < :started "
    " AND h1.price IS NOT NULL AND h1.price != 0 "
    f"AND {_GPH_STANDARD}) AS low, "
    "h2.price, h2.original_price, h2.cny_fen, h2.snapshot_at, h2.id "
    "FROM (SELECT value->>'a' AS a, value->>'r' AS r, CAST(value->>'s' AS INTEGER) AS s "
    "FROM json_each(:want)) w "
    "JOIN game_price_history h2 ON h2.id = ("
    "SELECT h3.id FROM game_price_history h3 "
    "WHERE h3.appid = w.a AND h3.region_code = w.r AND COALESCE(h3.sub_id, 0) = w.s "
    "AND h3.snapshot_at IS NOT NULL AND h3.snapshot_at < :started "
    "AND h3.price IS NOT NULL AND h3.price != 0 "
    f"AND {_GPH_STANDARD} "
    "ORDER BY h3.snapshot_at DESC, h3.id DESC LIMIT 1)"
).columns(
    column("a", Integer), column("r", String), column("low", BigInteger),
    column("price", BigInteger), column("original_price", BigInteger),
    column("cny_fen", BigInteger), column("snapshot_at", DateTime),
    column("id", Integer),
).bindparams(bindparam("started", type_=DateTime))
_GPH_HAD_SQL = text(
    "SELECT w.value->>'a' AS a, w.value->>'r' AS r FROM json_each(:units) w WHERE EXISTS ("
    "SELECT 1 FROM game_price_history h "
    "WHERE h.appid = CAST(w.value->>'a' AS INTEGER) AND h.region_code = w.value->>'r' "
    "AND h.snapshot_at IS NOT NULL "
    "AND NOT (h.snapshot_at >= :started AND h.snapshot_at <= :ended) "
    "AND h.price IS NOT NULL AND h.price != 0 "
    f"AND {_GPH_STANDARD})"
).bindparams(bindparam("started", type_=DateTime), bindparam("ended", type_=DateTime))
_PREV_STATUS_SQL = text(
    "SELECT appid, region_code, MAX(id) AS last_id, current_json FROM price_events "
    "WHERE appid IN (SELECT CAST(value AS INTEGER) FROM json_each(:appids)) "
    "AND event_type IN :types "
    "AND COALESCE(json_extract(current_json, '$.status'), '') != '' "
    "GROUP BY appid, region_code"
).columns(
    column("appid", Integer), column("region_code", String),
    column("last_id", Integer), column("current_json", JSON),
).bindparams(bindparam("types", expanding=True))


class PriceEvent(Base):
    """一次价格事实变化。只增不改：读接口只读，没有状态机、没有确认与删除。"""

    __tablename__ = "price_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 事件归属：>0 挂轮执行（PriceCycle.id）；<0 非周期执行（-job_id）
    cycle_id: Mapped[int] = mapped_column(Integer)
    appid: Mapped[int] = mapped_column(Integer)
    # NULL = 该事件由游戏级对象表达，不属于单一区域
    region_code: Mapped[str | None] = mapped_column(String(8))
    event_type: Mapped[str] = mapped_column(String(32))
    # 变化前的有效值 / 变化后的值；没有前值时 previous_json 为 NULL
    previous_json: Mapped[dict | None] = mapped_column(JSON)
    current_json: Mapped[dict | None] = mapped_column(JSON)
    # 该事件依据的观察时刻（本轮快照时间 / 状态行更新时间 / removed_at）
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime | None] = mapped_column(DateTime)

    __table_args__ = (
        # 幂等依据：同一轮里同一单元的同一类事件只能有一条。
        # region_code 可空，唯一键按 COALESCE 归一（SQLite 视多个 NULL 互不相等）
        Index(
            "ux_price_event_identity",
            "cycle_id", "appid", text("COALESCE(region_code, '')"), "event_type",
            unique=True,
        ),
        Index("ix_price_events_cycle", "cycle_id"),
        Index("ix_price_events_appid", "appid", "occurred_at"),
    )


async def detect(cycle_id: int) -> list[dict]:
    """检测本轮事件并落库（幂等：重复调用不新增行）。返回本次新写入的事件。"""
    async with get_session_factory()() as session:
        cycle = await session.get(PriceCycle, cycle_id)
        if cycle is None:
            return []
        started_at = cycle.started_at
        window_end = cycle.finished_at or datetime.now()
        expected = dict(cycle.expected_json or {})
    if started_at is None:
        return []

    appids = [int(a) for a in (expected.get("appids") or [])]
    regions = {str(r).strip().lower() for r in (expected.get("regions") or [])}
    if not appids or not regions:
        return []
    return await detect_window(appids, regions, started_at, window_end, cycle_id)


async def detect_window(
    appids: list[int],
    regions,
    window_start: datetime,
    window_end: datetime,
    cycle_id: int,
) -> list[dict]:
    """把窗口内观察相对历史的变化写成事件（幂等：重复调用不新增行）。

    检测逻辑只有这一份，两种调用方共用：挂轮执行走 `detect`（窗口=Cycle
    冻结期望集与起止时刻）；非周期执行（手动单发 / 补抓拍，job 无 Cycle）
    直接给本 job 的对象集、区服与起止时刻。`cycle_id` 是事件归属键的组成
    部分：>0 挂轮；<0 = 非周期执行（-job_id）——不同执行各自成键，同一
    事实不会因跨执行重检而重复落行。
    """
    started_at = window_start
    appids = [int(a) for a in appids]
    regions = {str(r).strip().lower() for r in (regions or [])}
    if not appids or not regions:
        return []

    # ── 本轮观察：只认窗口内的行（未观察的单元不产生任何事件）；区服与
    # 窗口过滤在 SQL 完成，结果只含本轮观察到的单元 ──
    observed: dict[tuple[int, str], dict] = {}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    GameCurrentPrice.appid, GameCurrentPrice.region_code,
                    GameCurrentPrice.price, GameCurrentPrice.cny_fen,
                    GameCurrentPrice.price_status, GameCurrentPrice.sub_id,
                    GameCurrentPrice.updated_at,
                ).where(
                    GameCurrentPrice.appid.in_(appids),
                    func.lower(func.trim(GameCurrentPrice.region_code)).in_(regions),
                    GameCurrentPrice.updated_at.is_not(None),
                    GameCurrentPrice.updated_at >= started_at,
                    GameCurrentPrice.updated_at <= window_end,
                )
            )
        ).all()
    for appid, region_code, price, cny_fen, status, sub_id, updated_at in rows:
        region = str(region_code).strip().lower()
        observed[(int(appid), region)] = {
            "price": price, "cnyFen": cny_fen, "subId": sub_id,
            "status": str(status or "").strip().lower(), "at": updated_at,
        }

    snapshots, previous, had_valid = await _history_evidence(
        appids, started_at, window_end, observed
    )
    prev_status = await _previous_status(appids)
    games = await _game_states(appids)

    events: list[dict] = []
    for (appid, region), cur in observed.items():
        unit = f"{appid}:{region}"
        prev = previous.get(unit)
        snap = snapshots.get(unit)

        # 价格涨跌：两侧都要是真实正价（0 价是促销态，不参与涨跌）
        if prev is not None and prev["price"] and cur["price"]:
            if cur["price"] < prev["price"]:
                events.append(_event(
                    cycle_id, appid, region, PRICE_DROP,
                    previous={"price": prev["price"], "cnyFen": prev["cnyFen"]},
                    current={"price": cur["price"], "cnyFen": cur["cnyFen"]},
                    occurred_at=cur["at"],
                ))
            elif cur["price"] > prev["price"]:
                events.append(_event(
                    cycle_id, appid, region, PRICE_INCREASE,
                    previous={"price": prev["price"], "cnyFen": prev["cnyFen"]},
                    current={"price": cur["price"], "cnyFen": cur["cnyFen"]},
                    occurred_at=cur["at"],
                ))

        # 历史低价 / 永降：按 CN 标准版，且本轮必须真的写下了新快照
        if region == BENCHMARK_REGION.lower() and snap is not None:
            low = previous.get(f"{appid}:{BENCHMARK_REGION.lower()}", {}).get("low")
            if cur["price"] and low and cur["price"] < low:
                events.append(_event(
                    cycle_id, appid, BENCHMARK_REGION, NEW_HISTORICAL_LOW,
                    previous={"low": low}, current={"price": cur["price"]},
                    occurred_at=snap["at"],
                ))
            elif cur["price"] and low and cur["price"] == low and (
                prev is None or prev["price"] != low
            ):
                events.append(_event(
                    cycle_id, appid, BENCHMARK_REGION, HISTORICAL_LOW_MATCH,
                    previous={"low": low, "price": prev["price"] if prev else None},
                    current={"price": cur["price"]},
                    occurred_at=snap["at"],
                ))
            if (
                prev is not None
                and snap["original"] is not None
                and prev["original"] is not None
                and snap["original"] != prev["original"]
            ):
                events.append(_event(
                    cycle_id, appid, BENCHMARK_REGION, PERMANENT_PRICE_CHANGE,
                    previous={"originalPrice": prev["original"]},
                    current={"originalPrice": snap["original"]},
                    occurred_at=snap["at"],
                ))

        # 状态跃迁：没有上一状态（首次观察的新单元）不产生事件
        prev_state = prev_status.get(unit) or (
            "ok" if unit in had_valid else None
        )
        transition = (prev_state, cur["status"])
        if transition == ("ok", "locked"):
            events.append(_status_event(cycle_id, appid, region, REGION_LOCKED,
                                        prev_state, cur))
        elif transition == ("locked", "ok"):
            events.append(_status_event(cycle_id, appid, region, REGION_UNLOCKED,
                                        prev_state, cur))
        elif prev_state in ("ok", "locked") and cur["status"] in _FAILED_STATUSES:
            events.append(_status_event(cycle_id, appid, region, PRICE_UNAVAILABLE,
                                        prev_state, cur))
        elif prev_state in _FAILED_STATUSES and cur["status"] == "ok":
            events.append(_status_event(cycle_id, appid, region, PRICE_RESTORED,
                                        prev_state, cur))

    # 促销免费：本轮写下促销快照（0 价 + 有原价），且窗口前最近快照不是促销。
    # 探区优先 CN——与写库侧 _classify_free_kind 的锚区口径一致
    for appid in appids:
        unit, snap = _promo_unit(appid, snapshots, regions)
        if snap is None:
            continue
        prev = previous.get(unit)
        if prev is None:
            continue  # 首次观察：没有「之前」可比，不声称发生了进入促销
        if not prev.get("promo"):
            events.append(_event(
                cycle_id, appid, None, FREE_PROMO,
                previous={"freeKind": None},
                current={"freeKind": "promo", "region": unit.split(":", 1)[1].upper()},
                occurred_at=snap["at"],
            ))

    # 下架：removed_at 落在本轮窗口 = 本轮标记的那一刻（持续态不重复产生）
    for appid, removed_at in games.items():
        if removed_at is not None and started_at <= removed_at <= window_end:
            events.append(_event(
                cycle_id, appid, None, REMOVED,
                previous={"removedAt": None},
                current={"removedAt": removed_at.isoformat()},
                occurred_at=removed_at,
            ))

    return await _persist(events)


# ── 证据查询 ──


async def _history_evidence(
    appids: list[int], started_at: datetime, window_end: datetime,
    observed: dict[tuple[int, str], dict],
) -> tuple[dict[str, dict], dict[str, dict], set[str]]:
    """返回 (本轮新快照, 窗口前的有效基线, 窗口前有有效价的单元集)。

    只比较**与当前标准版同一个 sub_id** 的快照：`game_current_prices` 记的是写库侧
    选定的标准版（多个候选里 `min(sub_id)`），同一单元在库内可能同时存在 gold /
    版本后缀之外的其它候选（同区多个在售包）；跨 sub 比较会把「换了另一个包」
    误判成价格变化。

    本轮新快照：窗口内该 sub 最后写入的 ok 快照（同 `snapshot_at` 时按 id 取最后
    一条——与写库侧基线同序）。窗口前基线：该 sub 在窗口之前的最低有效价（`low`）
    与最近一条快照（`price` / `original`），只取正价；`unobserved` 与 0 价不参与
    比较。三段证据查询按观察单元在 SQL 侧下推（每单元一次索引探测取 argmax /
    聚合行，结果集与全史解耦）；「窗口前有有效价」只覆盖本轮观察单元
    （消费面即 detect 的 observed 循环）。
    """
    want: list[dict] = []
    units: list[dict] = []
    for (appid, region), item in observed.items():
        units.append({"a": appid, "r": region.upper()})
        if item.get("subId") is not None:
            want.append({"a": appid, "r": region.upper(), "s": int(item["subId"])})
    snaps: dict[str, dict] = {}
    prevs: dict[str, dict] = {}
    async with get_session_factory()() as session:
        if want:
            snap_rows = (
                await session.execute(
                    _GPH_SNAP_SQL,
                    {"want": json.dumps(want), "started": started_at, "ended": window_end},
                )
            ).all()
            prev_rows = (
                await session.execute(
                    _GPH_PREV_SQL, {"want": json.dumps(want), "started": started_at}
                )
            ).all()
        else:
            snap_rows, prev_rows = [], []
        had_rows = (
            await session.execute(
                _GPH_HAD_SQL,
                {"units": json.dumps(units), "started": started_at, "ended": window_end},
            )
        ).all()

    snaps: dict[str, dict] = {}
    for a, r, price, original, cny_fen, snap_at, row_id in snap_rows:
        snaps[f"{int(a)}:{str(r).strip().lower()}"] = {
            "at": snap_at, "rowId": row_id, "price": price, "original": original,
            "cnyFen": cny_fen, "promo": price == 0 and (original or 0) > 0,
        }
    prevs: dict[str, dict] = {}
    for a, r, low, price, original, cny_fen, snap_at, row_id in prev_rows:
        prevs[f"{int(a)}:{str(r).strip().lower()}"] = {
            "low": low, "price": price, "original": original, "cnyFen": cny_fen,
            "at": snap_at, "rowId": row_id, "promo": price == 0 and (original or 0) > 0,
        }
    # 「窗口前有过有效价」不清 sub：只要该单元当时有价，就说明它当时是 ok，
    # 这是状态类事件的基线（与价格比较用的同 sub 基线分开）
    had_valid = {f"{int(a)}:{str(r).strip().lower()}" for a, r in had_rows}
    return snaps, prevs, had_valid


async def _previous_status(appids: list[int]) -> dict[str, str]:
    """每个单元最近一条状态事件隐含的状态（每单元 SQL 侧取 MAX(id) 一行）。

    价格状态没有历史行（`mark_region_status` 只 UPSERT 当前表），本表自身就是
    状态跃迁的持久记忆；没有记录时不猜，由调用方回落到「窗口前有有效快照 ⇒ ok」。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                _PREV_STATUS_SQL, {"appids": json.dumps(appids), "types": list(STATUS_EVENT_TYPES)}
            )
        ).all()
    out: dict[str, str] = {}
    for appid, region_code, _last_id, current in rows:
        status = (current or {}).get("status")
        if status:
            out[f"{int(appid)}:{str(region_code or '').strip().lower()}"] = status
    return out


async def _game_states(appids: list[int]) -> dict[int, datetime | None]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Game.appid, Game.removed_at).where(Game.appid.in_(appids))
            )
        ).all()
    return {int(a): r for a, r in rows}





def _promo_unit(
    appid: int, snapshots: dict[str, dict], regions: set[str]
) -> tuple[str, dict | None]:
    """该对象本轮的促销快照（CN 优先，其余区按名字序兜底）。"""
    cn_unit = f"{appid}:{BENCHMARK_REGION.lower()}"
    snap = snapshots.get(cn_unit)
    if snap is not None and snap.get("promo"):
        return cn_unit, snap
    for region in sorted(regions):
        unit = f"{appid}:{region}"
        candidate = snapshots.get(unit)
        if candidate is not None and candidate.get("promo"):
            return unit, candidate
    return cn_unit, None


# ── 组装与落库 ──


def _event(
    cycle_id: int, appid: int, region: str | None, event_type: str, *,
    previous: dict | None, current: dict, occurred_at: datetime,
) -> dict:
    return {
        "cycle_id": cycle_id,
        "appid": appid,
        "region_code": region.upper() if region else None,
        "event_type": event_type,
        "previous_json": previous,
        "current_json": current,
        "occurred_at": occurred_at,
    }


def _status_event(
    cycle_id: int, appid: int, region: str, event_type: str,
    prev_state: str | None, cur: dict,
) -> dict:
    return _event(
        cycle_id, appid, region, event_type,
        previous={"status": prev_state},
        current={"status": cur["status"], "cnyFen": cur["cnyFen"]},
        occurred_at=cur["at"],
    )


async def _persist(events: list[dict]) -> list[dict]:
    """写库：唯一键冲突即跳过（重复执行同一轮不新增行）。"""
    if not events:
        return []
    written: list[dict] = []
    now = datetime.now()
    async with write_session(WritePriority.BACKGROUND) as session:
        for item in events:
            stmt = (
                sqlite_insert(PriceEvent)
                .values(**item, created_at=now)
                .on_conflict_do_nothing()
            )
            result = await session.execute(stmt)
            if result.rowcount:
                written.append(item)
        await session.commit()
    return written


# ── 只读查询 ──


async def list_events(
    *, cycle_id: int | None = None, appid: int | None = None,
    event_type: str | None = None, limit: int = 50,
) -> list[dict]:
    """最近的价格事件（新→旧）。事实记录：只读，没有确认/删除/状态。

    排序按 `occurred_at`（事实发生时刻）而非 `id`：同一轮内事件按对象/地区顺序
    写入，`id` 序与发生时刻不同序；`id` 只作同时刻的次序兜底。
    """
    stmt = (
        select(PriceEvent)
        .order_by(PriceEvent.occurred_at.desc(), PriceEvent.id.desc())
        .limit(limit)
    )
    if cycle_id is not None:
        stmt = stmt.where(PriceEvent.cycle_id == cycle_id)
    if appid is not None:
        stmt = stmt.where(PriceEvent.appid == appid)
    if event_type is not None:
        stmt = stmt.where(PriceEvent.event_type == event_type)
    async with get_session_factory()() as session:
        rows = (await session.execute(stmt)).scalars().all()
    return [
        {
            "id": e.id,
            "cycleId": e.cycle_id,
            "appid": e.appid,
            "region": e.region_code,
            "eventType": e.event_type,
            "previous": e.previous_json,
            "current": e.current_json,
            "occurredAt": e.occurred_at.isoformat() if e.occurred_at else None,
        }
        for e in rows
    ]
