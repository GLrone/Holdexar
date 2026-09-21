"""价格事实事件（Price Event）：本轮观察相对此前有效观察发生了什么变化。

Observation = 本轮实际观察到了什么（`game_price_history` / `game_current_prices`）。
Event = 这次观察相对此前有效状态发生的变化（`price_events` 一行）。两者不混用：
「RU = 99 RUB」是观察；「上一有效价 199 → 本次 99」才是事件。

事件在 Cycle finalizing 阶段统一检测，建立在本轮**最终有效结果**之上，不由各个
CrawlJob 自己产生——否则「本轮暂时失败、随后补抓成功」的单元会先报
PRICE_UNAVAILABLE 再报 PRICE_RESTORED，产生无意义噪音。

三条判定前提：

1. **未观察不产生事件**：该单元在本轮窗口内没有任何结果（`unobserved`）时一律
   跳过——「没抓到」推不出「价格不可用」。
2. **只认变化**：当前值与上一有效观察相同时不产生事件。`hl_flag` / `pp_flag` /
   `free_kind` / `removed_at` / `price_status` 都是持续量，按当前值机械生成会
   每轮重复。
3. **上一有效观察来自持久证据**：价格类用 `game_price_history`（INSERT-only）。
   状态类没有历史行（`mark_region_status` 只 UPSERT 当前表），故用「本表最近一条
   状态事件」+「窗口前存在有效快照 ⇒ 当时为 ok」作为上一状态；两者都拿不到
   （首次观察的新单元）时不产生状态事件。

本轮结果窗口与 Coverage 同口径：`[cycle.started_at, cycle.finished_at]`
（未收敛取当前时刻）。窗口内并行的 5min repair / 手动抓取无法与本轮区分，
这是 P3/P4 已记录的口径限制；来源不明的结果不额外制造事件。

事件类型与判定（`→` 左为上一有效状态，右为本轮观察）：

| 事件 | 粒度 | 判定 |
|---|---|---|
| `PRICE_DROP` | appid × region | 本轮有观察，窗口前最近有效快照价 > 本轮价（两者都 > 0） |
| `PRICE_INCREASE` | appid × region | 同上，方向相反 |
| `NEW_HISTORICAL_LOW` | appid × CN | 本轮写下新快照，价 < 窗口前所有有效快照的最低值 |
| `HISTORICAL_LOW_MATCH` | appid × CN | 本轮写下新快照，价 == 窗口前最低值（从更高价回落） |
| `PERMANENT_PRICE_CHANGE` | appid × CN | 本轮新快照的原价 ≠ 窗口前最近快照的原价 |
| `REGION_LOCKED` | appid × region | `ok → locked` |
| `REGION_UNLOCKED` | appid × region | `locked → ok` |
| `PRICE_UNAVAILABLE` | appid × region | `ok / locked → missing / blocked` |
| `PRICE_RESTORED` | appid × region | `missing / blocked → ok` |
| `FREE_PROMO` | appid | 本轮写下促销快照（0 价 + 有原价），且窗口前最近快照不是促销 |
| `REMOVED` | appid | `games.removed_at` 落在本轮窗口内（`NULL → 非空` 的那一刻） |

价格比较用**该区货币的 `price`**，不用 `cny_fen`——后者会把汇率波动误判成价格
变化；0 价（促销/永久免费）不参与涨跌判定，它属于 FREE_PROMO 的语义。

`region_code` 为 NULL 表示该事件由游戏级对象表达（`free_kind` / `removed_at`
存在 `games` 上），不属于单一区域；不拿 NULL 表示「没判断」。

历史低价与永降按 CN 标准版判定（与 `hl_flag` / `pp_flag` 的计算对象一致），
故其 `region_code` 为 `CN`——证据发生在 CN。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import (
    DateTime, Index, Integer, String, JSON, select, text,
)
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base, get_session_factory
from app.domains.crawl.cycle import PriceCycle
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

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


class PriceEvent(Base):
    """一次价格事实变化。只增不改：读接口只读，没有状态机、没有确认与删除。"""

    __tablename__ = "price_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # 事件发生在哪一轮（Cycle 是一级归属）
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


def _is_standard(
    is_gold: bool | None, version_suffix: str | None, is_bundle: bool | None
) -> bool:
    """标准版快照：与写库侧一致（非 gold、无版本后缀、非 bundle-as-sub）。"""
    return not is_gold and not (version_suffix or "") and not is_bundle


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

    # ── 本轮观察：只认窗口内的行（未观察的单元不产生任何事件）──
    observed: dict[tuple[int, str], dict] = {}
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    GameCurrentPrice.appid, GameCurrentPrice.region_code,
                    GameCurrentPrice.price, GameCurrentPrice.cny_fen,
                    GameCurrentPrice.price_status, GameCurrentPrice.sub_id,
                    GameCurrentPrice.updated_at,
                ).where(GameCurrentPrice.appid.in_(appids))
            )
        ).all()
    for appid, region_code, price, cny_fen, status, sub_id, updated_at in rows:
        region = str(region_code).strip().lower()
        if region not in regions or updated_at is None:
            continue
        if updated_at < started_at or updated_at > window_end:
            continue
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
    比较。
    """
    want: dict[str, int] = {}
    for (appid, region), item in observed.items():
        if item.get("subId") is not None:
            want[f"{appid}:{region}"] = int(item["subId"])
    snaps: dict[str, dict] = {}
    prevs: dict[str, dict] = {}
    had_valid: set[str] = set()
    if not want:
        want = {}

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    GamePriceHistory.appid, GamePriceHistory.region_code,
                    GamePriceHistory.sub_id, GamePriceHistory.price,
                    GamePriceHistory.original_price, GamePriceHistory.cny_fen,
                    GamePriceHistory.snapshot_at, GamePriceHistory.id,
                    GamePriceHistory.is_gold, GamePriceHistory.version_suffix,
                    GamePriceHistory.is_bundle,
                ).where(GamePriceHistory.appid.in_(appids))
            )
        ).all()
    for (appid, region_code, sub_id, price, original, cny_fen, snap_at, row_id,
         is_gold, suffix, is_bundle) in rows:
        region = str(region_code).strip().lower()
        if not _is_standard(is_gold, suffix, is_bundle) or snap_at is None:
            continue
        key = f"{int(appid)}:{region}"
        promo = price == 0 and (original or 0) > 0
        in_window = started_at <= snap_at <= window_end
        if not in_window and price:
            # 「窗口前有过有效价」不清 sub：只要该单元当时有价，就说明它当时是 ok，
            # 这是状态类事件的基线（与价格比较用的同 sub 基线分开）
            had_valid.add(key)
        if want.get(key) != int(sub_id or 0):
            continue
        if in_window:
            seen = snaps.get(key)
            if seen is None or (snap_at, row_id) >= (seen["at"], seen["rowId"]):
                snaps[key] = {"at": snap_at, "rowId": row_id, "price": price,
                              "original": original, "cnyFen": cny_fen,
                              "promo": promo}
            continue
        if snap_at >= started_at or not price:
            continue
        bucket = prevs.get(key)
        if bucket is None:
            prevs[key] = {"low": price, "price": price, "original": original,
                          "cnyFen": cny_fen, "at": snap_at, "rowId": row_id,
                          "promo": promo}
            continue
        if price < bucket["low"]:
            bucket["low"] = price
        if (snap_at, row_id) >= (bucket["at"], bucket["rowId"]):
            bucket["price"], bucket["original"] = price, original
            bucket["cnyFen"] = cny_fen
            bucket["at"], bucket["rowId"] = snap_at, row_id
            bucket["promo"] = promo
    return snaps, prevs, had_valid


async def _previous_status(appids: list[int]) -> dict[str, str]:
    """每个单元最近一条状态事件隐含的状态（按 id 升序遍历，后者覆盖前者）。

    价格状态没有历史行（`mark_region_status` 只 UPSERT 当前表），本表自身就是
    状态跃迁的持久记忆；没有记录时不猜，由调用方回落到「窗口前有有效快照 ⇒ ok」。
    """
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(PriceEvent.appid, PriceEvent.region_code, PriceEvent.current_json)
                .where(
                    PriceEvent.appid.in_(appids),
                    PriceEvent.event_type.in_(STATUS_EVENT_TYPES),
                )
                .order_by(PriceEvent.id)
            )
        ).all()
    out: dict[str, str] = {}
    for appid, region_code, current in rows:
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
    async with get_session_factory()() as session:
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
    """最近的价格事件（新→旧）。事实记录：只读，没有确认/删除/状态。"""
    stmt = select(PriceEvent).order_by(PriceEvent.id.desc()).limit(limit)
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
