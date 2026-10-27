"""社区历史源采集适配器验收：签名确定性、点解析口径、幂等落库与同步状态。

隔离：conftest 临时数据目录 + 全 monkeypatch 网络，不发真请求。
签名黄金值锚定上游真实口径（带该签名的历史请求返回全量数据）。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import func, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler import community_history as ch
from app.domains.games.models import GamePriceHistory

APPID = 997_001


@pytest.fixture
def db(monkeypatch):
    from app.domains.steam_events import service as steam_events_service

    async def _no_event(_ts):
        return None

    monkeypatch.setattr(steam_events_service, "active_event_key_at", _no_event)
    return None


async def _history_count(db):
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        return (
            await session.execute(
                select(func.count()).select_from(GamePriceHistory)
            )
        ).scalar_one()


def test_hkey_is_deterministic_and_matches_live_probe():
    """签名确定性：同 (路径, 时间戳, nonce) 恒等同值；黄金值=真实接口探针值。"""
    assert (
        ch._hkey(
            "/game/get_game_prices/history/v2", 1760000000,
            "AABBCCDDEEFF00112233445566778899",
        )
        == "X122045"
    )
    assert len(ch._hkey("/x/y/", 1, "00")) == 7


def test_region_cc_aliases():
    """区码映射：欧元区与英镑区走上游别名，其余恒等且小写化。"""
    assert ch.region_cc("pt") == "eu"
    assert ch.region_cc("gb") == "uk"
    assert ch.region_cc("CN") == "cn"
    assert ch.region_cc("ru") == "ru"


def test_normalize_points_scale_precision_and_identity():
    """点解析：主单位 ×100 入库；混合 str/int 日期；标准版身份与来源标记。"""
    rows = ch.normalize_points(
        APPID, "cn",
        [
            {"date": "1614355200", "discount": 0, "price": "298",
             "currency": "CNY", "rmb_price": "298"},
            {"date": 1792857600, "price": "89.4", "discount": 70, "currency": "CNY"},
            {"date": "bad", "price": "10", "currency": "CNY"},  # 坏日期跳过
            {"date": 1792857600, "price": "free", "currency": "CNY"},  # 坏价格跳过
            {"date": 1792857600, "price": "0", "currency": "CNY"},  # 非正价跳过
        ],
    )
    assert len(rows) == 2
    first, last = rows
    assert first["price"] == 29800 and last["price"] == 8940
    assert first["discount_percent"] == 0 and last["discount_percent"] == 70
    assert first["region_code"] == "CN"
    assert first["currency"] == "CNY"
    for row in (first, last):
        assert row["appid"] == APPID
        assert row["source"] == "community"
        assert row["product_unknown"] is True  # 商品身份未知：不冒充标准版
        assert row["sub_id"] is None and row["version_suffix"] is None
        assert row["is_gold"] is False and row["is_bundle"] is False
        assert row["cny_fen"] is None  # 折算交给读侧按历史日期汇率
        assert row["snapshot_at"] is not None
        assert row["snapshot_at"].hour == 0  # 上游点为北京时间零点


def _fake_points(points, pkg_points=None, calls=None):
    """fetch 桩：appid 级返回 points；package_id 调用返回 pkg_points
    （缺省复用 points）。calls 列表收集每次调用的 package_id 供断言。"""

    async def _fetch(appid, region, package_id=None):
        if calls is not None:
            calls.append(package_id)
        if package_id is not None:
            return pkg_points if pkg_points is not None else points
        return points

    return _fetch


def _fake_error(exc):
    async def _fetch(appid, region):
        raise exc
    return _fetch


@pytest.mark.asyncio
async def test_backfill_inserts_dedups_and_never_overwrites_steam(db, monkeypatch):
    """回填幂等：重复回填零重复；同键官方观测行不被覆盖（来源保持 steam）。"""
    from app.core.database import get_session_factory
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    points = [
        {"date": "1614355200", "discount": 0, "price": "298", "currency": "CNY"},
        {"date": 1792857600, "price": "89.4", "discount": 70, "currency": "CNY"},
    ]
    # 同日同价的官方观测先落库：该社区点被佐证去重跳过，steam 行不被覆盖
    steam_row = ch.normalize_points(
        APPID, "cn", [{"date": "1614355200", "discount": 0, "price": "298", "currency": "CNY"}]
    )[0]
    steam_row["source"] = "steam"
    steam_row["product_unknown"] = False
    async with get_session_factory()() as session:
        await session.execute(sqlite_insert(GamePriceHistory).values(steam_row))
        await session.commit()

    monkeypatch.setattr(ch, "fetch_history_points", _fake_points(points))
    result = await ch.backfill_region(APPID, "cn")
    assert result == {"state": "ok", "points": 2, "error": None}  # 上游点数
    # steam 行占位（同日同价社区点被佐证去重）+ 未被佐证的另一点
    assert await _history_count(db) == 2

    # 重复回填：唯一键命中全部跳过，零重复
    monkeypatch.setattr(ch, "fetch_history_points", _fake_points(points))
    assert (await ch.backfill_region(APPID, "cn"))["state"] == "ok"
    assert await _history_count(db) == 2

    async with get_session_factory()() as session:
        row = (
            await session.execute(
                select(GamePriceHistory).where(
                    GamePriceHistory.appid == APPID,
                    GamePriceHistory.snapshot_at == steam_row["snapshot_at"],
                )
            )
        ).scalar_one()
    assert row.source == "steam"


@pytest.mark.asyncio
async def test_backfill_state_transitions(db, monkeypatch):
    """同步状态机：ok / 上游空 / 失败三态落库，brief 按最近行为给态。"""
    monkeypatch.setattr(ch, "fetch_history_points", _fake_points([]))
    result = await ch.backfill_region(APPID, "cn")
    assert result["state"] == "empty"
    brief = await ch.backfill_brief(APPID, "cn")
    assert brief["state"] == "empty"

    monkeypatch.setattr(
        ch, "fetch_history_points",
        _fake_error(ch.CommunityHistoryError("网络请求失败：超时")),
    )
    result = await ch.backfill_region(APPID, "cn")
    assert result["state"] == "error" and result["error"]
    brief = await ch.backfill_brief(APPID, "cn")
    assert brief["state"] == "error" and "超时" in (brief["lastError"] or "")

    monkeypatch.setattr(ch, "fetch_history_points", _fake_points(
        [{"date": "1614355200", "discount": 0, "price": "298", "currency": "CNY"}]
    ))
    assert (await ch.backfill_region(APPID, "cn"))["state"] == "ok"
    brief = await ch.backfill_brief(APPID, "cn")
    assert brief["state"] == "ok" and brief["lastOkAt"]


@pytest.mark.asyncio
async def test_start_backfill_single_flight(db, monkeypatch):
    """同键在飞：第二次派发直接返回运行态，不重复起任务。"""
    import asyncio

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_fetch(appid, region):
        started.set()
        await release.wait()
        return []

    monkeypatch.setattr(ch, "fetch_history_points", slow_fetch)
    first = await ch.start_backfill(APPID, "cn")
    assert first["started"] is True
    await started.wait()
    second = await ch.start_backfill(APPID, "cn")
    assert second == {"started": False, "state": "running"}
    release.set()
    for _ in range(100):
        if not ch.is_inflight(APPID, "cn"):
            break
        await asyncio.sleep(0.01)
    assert not ch.is_inflight(APPID, "cn")


@pytest.mark.asyncio
async def test_backfill_refreshes_hl_flag_but_not_alerts(db, monkeypatch):
    """回填新事实后重算史低标记；不进价格事件/告警管线（不触发降价提醒）。"""
    from datetime import datetime
    from app.core.database import get_session_factory
    from app.domains.alerts import service as alerts_service
    from app.domains.games.models import Game, GameCurrentPrice

    def no_alerts(*a, **k):
        raise AssertionError("回填不得触发告警管线")

    async with get_session_factory()() as session:
        session.add(Game(appid=APPID, name="Backfill", hl_flag=3))
        session.add(GameCurrentPrice(appid=APPID, region_code="CN", currency="CNY",
            price=29800, original_price=39900, cny_fen=29800, discount_percent=25,
            price_status="ok", attempt_outcome="success", sub_id=7, updated_at=datetime.now()))
        session.add(GamePriceHistory(appid=APPID, region_code="CN", currency="CNY",
            price=39900, original_price=39900, cny_fen=39900, sub_id=7,
            price_status="ok", product_unknown=False, snapshot_at=datetime(2020, 1, 1)))
        await session.commit()
    monkeypatch.setattr(alerts_service, "check_new_lows", no_alerts)
    monkeypatch.setattr(ch, "fetch_history_points", _fake_points(
        [{"date": "1614355200", "discount": 0, "price": "298", "currency": "CNY"}]
    ))
    assert (await ch.backfill_region(APPID, "cn"))["state"] == "ok"
    async with get_session_factory()() as session:
        game = await session.get(Game, APPID)
        assert game.hl_flag == 1
        assert game.smart_score is not None


@pytest.mark.asyncio
async def test_history_and_derived_roll_back_together_on_cancel(db, monkeypatch):
    import asyncio
    from app.domains.games import derived

    async def interrupted(*_args, **_kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(derived, "update_price_derived", interrupted)
    rows = ch.normalize_points(APPID, "cn", [
        {"date": "1614355200", "discount": 0, "price": "298", "currency": "CNY"},
    ])
    with pytest.raises(asyncio.CancelledError):
        await ch._write_rows(APPID, "cn", rows)
    assert await _history_count(db) == 0


@pytest.mark.asyncio
async def test_backfill_dedup_only_for_corroborated_same_price(db, monkeypatch):
    """佐证去重口径：同日同价的已识别行命中才跳过；同日不同价保留；
    商品身份未知的既有行不构成佐证。"""
    from app.core.database import get_session_factory
    from sqlalchemy import select
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    day1, day2 = "1614355200", 1792857600
    from datetime import timedelta as _td
    steam_rows = ch.normalize_points(APPID, "cn", [
        {"date": day1, "discount": 0, "price": "298", "currency": "CNY"},
        {"date": day2, "discount": 0, "price": "999", "currency": "CNY"},
    ])
    for r in steam_rows:
        r["source"] = "steam"
        r["product_unknown"] = False
        # 观测行带真实抓取时刻（同日晚于社区零点行）——佐证窗口按日对齐
        r["snapshot_at"] = r["snapshot_at"] + _td(hours=6, minutes=10)
    unknown_existing = ch.normalize_points(APPID, "cn", [
        {"date": day1, "discount": 0, "price": "150", "currency": "CNY"},
    ])[0]
    async with get_session_factory()() as session:
        await session.execute(sqlite_insert(GamePriceHistory).values(steam_rows))
        await session.execute(
            sqlite_insert(GamePriceHistory)
            .values(unknown_existing)
            .on_conflict_do_nothing()
        )
        await session.commit()

    points = [
        {"date": day1, "discount": 0, "price": "298", "currency": "CNY"},
        {"date": day1, "discount": 0, "price": "150", "currency": "CNY"},
        {"date": day2, "discount": 70, "price": "89.4", "currency": "CNY"},
    ]
    monkeypatch.setattr(ch, "fetch_history_points", _fake_points(points))
    result = await ch.backfill_region(APPID, "cn")
    assert result["state"] == "ok"
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(GamePriceHistory.source, GamePriceHistory.price, GamePriceHistory.product_unknown)
            .where(GamePriceHistory.appid == APPID)
            .order_by(GamePriceHistory.price)
        )).all()
    assert (rows[0][0], rows[0][1]) == ("community", 8940)
    assert (rows[1][0], rows[1][1]) == ("community", 15000)
    assert bool(rows[1][2]) is True
    assert (rows[2][0], rows[2][1]) == ("steam", 29800)
    assert (rows[3][0], rows[3][1]) == ("steam", 99900)


async def _seed_current_price(appid: int, region: str, sub_id: int) -> None:
    from datetime import datetime as _dt

    from app.core.database import get_session_factory
    from app.domains.games.models import GameCurrentPrice

    async with get_session_factory()() as session:
        session.add(
            GameCurrentPrice(
                appid=appid,
                region_code=region.upper(),
                currency="CNY",
                price=29800,
                original_price=29800,
                discount_percent=0,
                sub_id=sub_id,
                price_status="ok",
                updated_at=_dt.now(),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_package_channel_writes_identified_rows_with_local_identity(db, monkeypatch):
    """包通道：现售 sub 请求上下文归属 + 本地同包观测分类佐证 → 已识别行
    进标准版序列；appid 兜底通道同日同价点被佐证去重。"""
    from datetime import datetime as _dt
    from datetime import timedelta as _td

    from app.core.database import get_session_factory
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    pkg_appid = 997_101
    sale_sub = 555_001
    # 本地同包 steam 观测：标准版分类（无后缀）
    seed_row = ch.normalize_points(
        pkg_appid, "cn",
        [{"date": "1614355200", "discount": 0, "price": "298", "currency": "CNY"}],
    )[0]
    seed_row.update(source="steam", product_unknown=False, sub_id=sale_sub,
                    snapshot_at=seed_row["snapshot_at"] + _td(hours=6))
    async with get_session_factory()() as session:
        await session.execute(sqlite_insert(GamePriceHistory).values(seed_row))
        await session.commit()
    await _seed_current_price(pkg_appid, "cn", sale_sub)

    calls: list = []
    pkg_pts = [
        {"date": "1614355200", "discount": 0, "price": "298", "currency": "CNY"},
        {"date": 1792857600, "price": "89.4", "discount": 70, "currency": "CNY"},
    ]
    # 两条通道返回同一份点集（上游两通道在标准包上重合的常态）
    monkeypatch.setattr(
        ch, "fetch_history_points", _fake_points(pkg_pts, pkg_points=pkg_pts, calls=calls)
    )
    result = await ch.backfill_region(pkg_appid, "cn")
    assert result["state"] == "ok"
    # 调用形态：先包通道（带 package_id），后 appid 通道（None）
    assert calls[0] == sale_sub and calls[1] is None

    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(GamePriceHistory.source, GamePriceHistory.sub_id,
                   GamePriceHistory.product_unknown, GamePriceHistory.price)
            .where(GamePriceHistory.appid == pkg_appid)
            .order_by(GamePriceHistory.price)
        )).all()
    # 8940 已识别社区行 + 29800 本地 steam 行（appid 通道同日同价被去重）
    assert [(r[0], r[1], bool(r[2]), r[3]) for r in rows] == [
        ("community", sale_sub, False, 8940),
        ("steam", sale_sub, False, 29800),
    ]

    from app.domains.games import service as games_service
    default = await games_service.get_game_history(pkg_appid, region="cn")
    assert [p["cnyFen"] for p in default["points"]] == [29800, 8940]
    unknown = await games_service.get_game_history(pkg_appid, region="cn", product="unknown")
    assert unknown["points"] == []


@pytest.mark.asyncio
async def test_package_channel_without_local_identity_stays_unknown(db, monkeypatch):
    """包通道无本地分类佐证：sub 已知（行带 sub_id）但分类未验证——
    product_unknown=1，不进标准版谓词，走未知序列呈现。"""
    from app.core.database import get_session_factory

    pkg_appid = 997_102
    sale_sub = 556_001
    await _seed_current_price(pkg_appid, "cn", sale_sub)

    calls: list = []
    pts = [{"date": 1792857600, "price": "89.4", "discount": 70, "currency": "CNY"}]
    monkeypatch.setattr(
        ch, "fetch_history_points",
        _fake_points([], pkg_points=pts, calls=calls),  # appid 通道空，包通道有点
    )
    result = await ch.backfill_region(pkg_appid, "cn")
    assert result["state"] == "ok" and result["points"] == 1
    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(GamePriceHistory.sub_id, GamePriceHistory.product_unknown)
            .where(GamePriceHistory.appid == pkg_appid)
        )).all()
    assert rows == [(sale_sub, 1)]
    from app.domains.games import service as games_service
    assert (await games_service.get_game_history(pkg_appid, region="cn"))["points"] == []
    unk = await games_service.get_game_history(pkg_appid, region="cn", product="unknown")
    assert [p["cnyFen"] for p in unk["points"]] == [8940]
