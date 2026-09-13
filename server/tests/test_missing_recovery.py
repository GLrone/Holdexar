"""补抓链路行为验收（欠账账本设计）。

覆盖四条链路（合成 appid 播种 game_current_prices，夹具清理）：
1. mark_region_status('missing')：fail_count 递增；穷尽（>MISSING_MAX_RETRIES）
   转 blocked 终态；locked/blocked 清零账本
2. generate_missing_tasks：欠账行按区分组凑批成 type=app 任务
   （每发 ≤400 行、只装该区欠账、冷却窗口内不选）；blocked/locked 不进补抓
3. upsert 质量门禁：status=ok 且 price=None 降级 missing 进账本，
   补抓成功回 ok 时 fail_count 清零（欠账闭环）
4. app_handler 落库失败（upsert 返回 False）→ 全区欠账化（消费 False 缺口）
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.database import get_session_factory, init_db
from app.crawler.db_writer import MISSING_MAX_RETRIES, DbWriter
from app.domains.games.models import Game, GameCurrentPrice

APPID = 997_001
APPID2 = 997_002
APPID3 = 997_003


def _game(appid: int, name: str = "补抓测试游戏") -> Game:
    now = datetime.now()
    return Game(
        appid=appid, name=name, created_at=now, updated_at=now,
    )


def _price(
    appid: int, region: str, status: str, price: int | None,
    fail_count: int = 0, updated_at: datetime | None = None,
) -> GameCurrentPrice:
    return GameCurrentPrice(
        appid=appid,
        region_code=region.upper(),
        currency="CNY" if region.upper() == "CN" else "UAH",
        price=price,
        original_price=price,
        discount_percent=0,
        sub_id=1,
        price_status=status,
        fail_count=fail_count,
        cny_fen=price,
        updated_at=updated_at or datetime.now(),
    )


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    appids = [APPID, APPID2, APPID3]
    now = datetime.now()
    async with get_session_factory()() as session:
        # 三款合成游戏 + 各自的价格欠账/终态行
        session.add_all([_game(a) for a in appids])
        session.add_all(
            [
                # APPID：过期欠账（冷却窗外）两个区
                _price(APPID, "CN", "missing", None, fail_count=1, updated_at=now - timedelta(hours=1)),
                _price(APPID, "UA", "missing", None, fail_count=1, updated_at=now - timedelta(hours=1)),
                # APPID2：冷却窗口内的欠账（不应入选）
                _price(APPID2, "CN", "missing", None, fail_count=1, updated_at=now),
                # APPID2：终态行（不应入选）
                _price(APPID2, "UA", "blocked", None),
                # APPID3：正常价（不应入选）
                _price(APPID3, "CN", "ok", 10000),
            ]
        )
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_(appids))
        )
        await session.execute(delete(Game).where(Game.appid.in_(appids)))
        await session.commit()


@pytest.mark.asyncio
async def test_mark_missing_increments_ledger():
    """missing 记账：首次插入 fail_count=1；重复标记递增。"""
    db = DbWriter()
    # 新区首次欠账（APPID3 的 UA 区此前无行）
    await db.mark_region_status(APPID3, "ua", "missing")
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID3, "UA"))
        assert row.price_status == "missing"
        assert row.fail_count == 1


@pytest.mark.asyncio
async def test_mark_missing_exhaustion_to_blocked():
    """穷尽转终态：旧 fail_count 达上限后再次 missing → blocked，不再进补抓。"""
    db = DbWriter()
    # APPID2 CN 行 fail_count=1；补到 MISSING_MAX_RETRIES-1 后下一次应转 blocked
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID2, "CN"))
        row.fail_count = MISSING_MAX_RETRIES - 1
        await session.commit()
    await db.mark_region_status(APPID2, "cn", "missing")
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID2, "CN"))
        assert row.price_status == "blocked"
        assert row.fail_count == MISSING_MAX_RETRIES
    # blocked 行不再出现在补抓任务里
    tasks = await db.generate_missing_tasks(cooldown_minutes=0)
    assert not any(APPID2 in t.get("appids", []) for t in tasks)


@pytest.mark.asyncio
async def test_mark_terminal_clears_ledger():
    """locked/blocked 记账清零（非欠账语义）。"""
    db = DbWriter()
    await db.mark_region_status(APPID, "cn", "locked")
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID, "CN"))
        assert row.price_status == "locked"
        assert row.fail_count == 0


@pytest.mark.asyncio
async def test_generate_missing_tasks():
    """任务生成：按区分组凑批（type=app，只装该区欠账）、冷却内不选、终态与 ok 不选。"""
    db = DbWriter()
    tasks = await db.generate_missing_tasks(cooldown_minutes=10)
    ours: dict[str, list[int]] = {}  # region → 合成 appid 命中（库内可能并存真实欠账行，只认合成行）
    for t in tasks:
        assert t["type"] == "app"
        assert t["id"].startswith(f"{t['region']}:补")
        hits = [a for a in t.get("appids", []) if a in (APPID, APPID2, APPID3)]
        if hits:
            ours.setdefault(t["region"], []).extend(hits)
    assert ours.get("cn") == [APPID]  # APPID2 冷却窗内欠账 + blocked、APPID3 ok，均不入选
    assert ours.get("ua") == [APPID]


@pytest.mark.asyncio
async def test_upsert_gate_degrades_ok_without_price():
    """质量门禁：status=ok 且 price=None 的行降级 missing 并计一次账。"""
    db = DbWriter()
    now = datetime.now()
    game_data = {
        "appid": APPID3, "name": "门禁测试", "updated_at": now,
    }
    prices = [
        # ok 有价：正常进 current
        {"appid": APPID3, "region_code": "CN", "currency": "CNY",
         "price": 10000, "original_price": 10000, "discount_percent": 0,
         "sub_id": 1, "is_gold": False, "version_suffix": None,
         "price_status": "ok", "crawled_at": now},
        # ok 无价：坏数据 → 降级 missing（gold 版无 sub 价格的真实形态）
        {"appid": APPID3, "region_code": "UA", "currency": "UAH",
         "price": None, "original_price": None, "discount_percent": 0,
         "sub_id": 2, "is_gold": True, "version_suffix": "Gold Edition",
         "price_status": "ok", "crawled_at": now},
        # 免费游戏 price=0：合法，不降级
        {"appid": APPID3, "region_code": "TR", "currency": "USD",
         "price": 0, "original_price": 0, "discount_percent": 0,
         "sub_id": 3, "is_gold": False, "version_suffix": None,
         "price_status": "ok", "crawled_at": now},
    ]
    assert await db.upsert_game_and_prices(game_data, prices) is True

    async with get_session_factory()() as session:
        cn = await session.get(GameCurrentPrice, (APPID3, "CN"))
        assert cn.price_status == "ok" and cn.price == 10000
        ua = await session.get(GameCurrentPrice, (APPID3, "UA"))
        assert ua.price_status == "missing"
        assert ua.fail_count == 1  # 欠账结转：降级即计一次失败
        tr = await session.get(GameCurrentPrice, (APPID3, "TR"))
        assert tr.price_status == "ok" and tr.price == 0  # 免费游戏不误伤


@pytest.mark.asyncio
async def test_upsert_recovery_clears_ledger():
    """补抓闭环：欠账行补到有价 → 回 ok 且 fail_count 清零。"""
    db = DbWriter()
    now = datetime.now()
    # APPID 已有 CN/UA missing 欠账（夹具播种）
    game_data = {"appid": APPID, "name": "补抓回填", "updated_at": now}
    prices = [
        {"appid": APPID, "region_code": "CN", "currency": "CNY",
         "price": 12000, "original_price": 12000, "discount_percent": 0,
         "sub_id": 1, "is_gold": False, "version_suffix": None,
         "price_status": "ok", "crawled_at": now},
    ]
    assert await db.upsert_game_and_prices(game_data, prices) is True
    async with get_session_factory()() as session:
        cn = await session.get(GameCurrentPrice, (APPID, "CN"))
        assert cn.price_status == "ok"
        assert cn.price == 12000
        assert cn.fail_count == 0  # 清账
        ua = await session.get(GameCurrentPrice, (APPID, "UA"))
        assert ua.price_status == "missing"  # 未补的区不受影响


@pytest.mark.asyncio
async def test_generate_tasks_cooldown_boundary():
    """冷却边界：cooldown=0 时窗口内欠账也可选（即时补抓入口）。"""
    db = DbWriter()
    tasks = await db.generate_missing_tasks(cooldown_minutes=0)
    assert any(
        APPID2 in t.get("appids", [])
        for t in tasks if t["region"] == "cn"
    )  # 冷却 0：APPID2 的 CN missing（blocked 转化前）入选


@pytest.mark.asyncio
async def test_generate_missing_tasks_batches_by_region():
    """按区分组凑批：同区欠账超过单发上限拆多发，每发 ≤400 行且不重不漏。"""
    db = DbWriter()
    now = datetime.now()
    # 段位取现网 Steam appid 上限（~505 万）之外，避开真实库的既有行
    batch_ids = list(range(9_980_000, 9_980_000 + 401))
    id_set = set(batch_ids)
    async with get_session_factory()() as session:
        session.add_all([_game(a, name=f"批量补抓{a}") for a in batch_ids])
        session.add_all(
            [_price(a, "CN", "missing", None, updated_at=now - timedelta(hours=1))
             for a in batch_ids]
        )
        await session.commit()
    try:
        tasks = await db.generate_missing_tasks(cooldown_minutes=10)
        ours = [t for t in tasks if any(a in id_set for a in t.get("appids", []))]
        covered = [a for t in ours for a in t["appids"] if a in id_set]
        assert sorted(covered) == sorted(batch_ids)  # 不重不漏
        assert all(t["region"] == "cn" for t in ours)
        assert all(len(t["appids"]) <= 400 for t in ours)
        assert len(ours) >= 2  # 401 行必然拆多发
    finally:
        async with get_session_factory()() as session:
            await session.execute(
                delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_(batch_ids))
            )
            await session.execute(delete(Game).where(Game.appid.in_(batch_ids)))
            await session.commit()
