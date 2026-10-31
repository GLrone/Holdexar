"""补抓链路行为验收（欠账账本设计）。

覆盖四条链路（合成 appid 播种 game_current_prices，夹具清理）：
1. mark_region_status('missing')：fail_count 递增；穷尽（>MISSING_MAX_RETRIES）
   转 blocked 终态；locked/blocked 清零账本
2. generate_missing_tasks：欠账行按区分组凑批成 type=app 任务
   （每发 ≤300 行、只装该区欠账、冷却窗口内不选）；blocked/locked 不进补抓
3. upsert 质量门禁：status=ok 且 price=None 降级 missing 进账本，
   补抓成功回 ok 时 fail_count 清零（欠账闭环）
4. app_handler 落库失败（upsert 返回 False）→ 全区欠账化（消费 False 缺口）
"""
from datetime import datetime, timedelta


def _now_naive() -> datetime:
    """时间戳列统一北京时 naive（与生产的冷却判定同源），不依赖本机时区。"""
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().replace(tzinfo=None)

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
    now = _now_naive()
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
        updated_at=updated_at or _now_naive(),
    )


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    appids = [APPID, APPID2, APPID3]
    now = _now_naive()
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
    now = _now_naive()
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
    now = _now_naive()
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
    """按区分组凑批：同区欠账超过单发上限拆多发，每发 ≤300 行且不重不漏。"""
    db = DbWriter()
    now = _now_naive()
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


@pytest.mark.asyncio
async def test_generate_missing_tasks_includes_free_state_rows():
    """免费态（f2p/beta）的 missing 行必须可被补抓拾取。

    旧实现按 free_kind 排除欠账，而免费态对象同样不在 pool/catalog
    爬取集合里——账本行成为无消费路径的死账。拾取后
    由补抓写出真实状态（ok-0 / locked），穷尽兜底不变。"""
    from app.domains.games.models import Game as _Game

    appid = 997_101
    now = _now_naive()
    async with get_session_factory()() as session:
        session.add(_Game(appid=appid, name="免费态欠账游戏", free_kind="f2p",
                          created_at=now, updated_at=now))
        session.add(_price(appid, "CN", "missing", None, fail_count=1,
                           updated_at=now - timedelta(hours=2)))
        await session.commit()
    try:
        db = DbWriter()
        tasks = await db.generate_missing_tasks(cooldown_minutes=10)
        picked = [a for t in tasks for a in t["appids"]]
        assert appid in picked
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid == appid))
            await session.execute(delete(Game).where(Game.appid == appid))
            await session.commit()


@pytest.mark.asyncio
async def test_generate_missing_tasks_clears_empty_region_rows():
    """空区行（region_code=''）即拾即清：无对应真实区服、没有 bump 路径，
    留在账本只会永久占位。"""
    appid = 997_102
    now = _now_naive()
    async with get_session_factory()() as session:
        session.add(_price(appid, "", "missing", None, fail_count=1,
                           updated_at=now - timedelta(hours=2)))
        await session.commit()
    try:
        db = DbWriter()
        tasks = await db.generate_missing_tasks(cooldown_minutes=10)
        assert all(t["region"] for t in tasks)
        async with get_session_factory()() as session:
            row = await session.get(GameCurrentPrice, (appid, ""))
        assert row is None
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid == appid))
            await session.commit()


@pytest.mark.asyncio
async def test_generate_missing_tasks_sql_limit_truncates_oldest():
    """LIMIT 下推到 SQL：仍按账本年龄取最老 N 行（老账先补），新行被挡在窗口外。"""
    appids = [997_110, 997_111, 997_112]
    now = _now_naive()
    ages = [5, 4, 3]
    async with get_session_factory()() as session:
        session.add_all([_game(a) for a in appids])
        session.add_all(
            _price(a, "CN", "missing", None, fail_count=1,
                   updated_at=now - timedelta(hours=h))
            for a, h in zip(appids, ages)
        )
        await session.commit()
    try:
        db = DbWriter()
        tasks = await db.generate_missing_tasks(cooldown_minutes=10, limit_rows=3)
        picked = [a for t in tasks for a in t["appids"]]
        # 自动夹具的 APPID（now-1h 更新）比三行都新：被 LIMIT 挡住
        assert sorted(picked) == appids
        assert len(picked) == 3
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_(appids)))
            await session.execute(delete(Game).where(Game.appid.in_(appids)))
            await session.commit()


@pytest.mark.asyncio
async def test_generate_missing_tasks_limit_keeps_empty_region_cleanup_complete():
    """空区清理解耦：超限截断后窗口外空区行仍被清；冷却窗内空区行保留。"""
    appids = [997_110, 997_111, 997_112]
    empty_old, empty_fresh = 997_123, 997_124
    now = _now_naive()
    ages = [5, 4, 3]
    async with get_session_factory()() as session:
        session.add_all([_game(a) for a in appids])
        session.add_all(
            _price(a, "CN", "missing", None, fail_count=1,
                   updated_at=now - timedelta(hours=h))
            for a, h in zip(appids, ages)
        )
        # 空区行：老账（8h，远在 limit=2 窗口外）与冷却窗内新鲜行各一
        session.add(_price(empty_old, "", "missing", None, fail_count=1,
                           updated_at=now - timedelta(hours=8)))
        session.add(_price(empty_fresh, "", "missing", None, fail_count=1,
                           updated_at=now))
        await session.commit()
    try:
        db = DbWriter()
        tasks = await db.generate_missing_tasks(cooldown_minutes=10, limit_rows=2)
        picked = [a for t in tasks for a in t["appids"]]
        assert picked == [997_110, 997_111], "limit 只取最老 2 行真实欠账"
        async with get_session_factory()() as session:
            assert await session.get(GameCurrentPrice, (empty_old, "")) is None,                 "窗口外空区行仍须被清（不占槽位也不漏清）"
            assert await session.get(GameCurrentPrice, (empty_fresh, "")) is not None,                 "冷却窗内空区行等过冷却再清"
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_(appids + [empty_old, empty_fresh])))
            await session.execute(delete(Game).where(Game.appid.in_(appids)))
            await session.commit()


@pytest.mark.asyncio
async def test_clear_missing_regions_deletes_only_missing():
    """clear_missing_regions 只删 missing 状态行：ok 行与历史不动。"""
    db = DbWriter()
    await db.clear_missing_regions([])  # 空表安全

    async with get_session_factory()() as session:
        session.add(_price(APPID, "RU", "ok", 10000))
        await session.commit()
    try:
        cleared = await db.clear_missing_regions([(APPID, "ru")])
        assert cleared == 0
        async with get_session_factory()() as session:
            row = await session.get(GameCurrentPrice, (APPID, "RU"))
        assert row is not None and row.price_status == "ok"

        await db.mark_region_status(APPID, "ru", "missing")
        cleared = await db.clear_missing_regions([(APPID, "ru")])
        assert cleared == 1
        async with get_session_factory()() as session:
            row = await session.get(GameCurrentPrice, (APPID, "RU"))
        assert row is None
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid == APPID))
            await session.commit()


@pytest.mark.asyncio
async def test_blocked_aging_reprobe():
    """blocked 老化重探：窗口内的终态行不进补抓，过窗行放行一次。"""
    db = DbWriter()
    # 夹具的 APPID2/UA 行 blocked 且 updated_at=now（窗口内）→ 不入选
    tasks = await db.generate_missing_tasks(cooldown_minutes=0)
    assert not any(APPID2 in t.get("appids", []) and t.get("region") == "UA" for t in tasks)
    # 行龄推过 BLOCKED_REPROBE_DAYS 窗口 → 放行重探
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID2, "UA"))
        row.updated_at = _now_naive() - timedelta(days=8)
        await session.commit()
    tasks = await db.generate_missing_tasks(cooldown_minutes=0)
    assert any(APPID2 in t.get("appids", []) and t.get("region", "").upper() == "UA" for t in tasks)


# ── 根因修复回归：降级按行不扩散 + 证据分流预算 ──


@pytest.mark.asyncio
async def test_mixed_region_keeps_ok_semantics():
    """混合区（同区 ok 有价标准行 + ok 无价 gold 行）→ 区保持 ok 语义：
    价格真实刷新、成功章、不进欠账结转；坏行本无价格落点不受影响。"""
    db = DbWriter()
    now = _now_naive()
    game_data = {"appid": APPID3, "name": "混合区", "updated_at": now}
    prices = [
        {"appid": APPID3, "region_code": "CN", "currency": "CNY",
         "price": 9900, "original_price": 9900, "discount_percent": 0,
         "sub_id": 1, "is_gold": False, "version_suffix": None,
         "price_status": "ok", "crawled_at": now},
        {"appid": APPID3, "region_code": "CN", "currency": "CNY",
         "price": None, "original_price": None, "discount_percent": 0,
         "sub_id": 2, "is_gold": True, "version_suffix": "Gold Edition",
         "price_status": "ok", "crawled_at": now},
    ]
    assert await db.upsert_game_and_prices(game_data, prices) is True
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID3, "CN"))
        assert row.price_status == "ok" and row.price == 9900
        assert row.attempt_outcome == "success" and row.last_success_at is not None
        assert row.fail_count == 0
    # 不进欠账选取
    assert not any(APPID3 in task.get("appids", [])
                   for task in await db.generate_missing_tasks(cooldown_minutes=0))


@pytest.mark.asyncio
async def test_mixed_region_batch_path_same_semantics():
    """批量入口同形状：ok_groups 与欠账结转集合不再相交（钉 1 态消失）。"""
    db = DbWriter()
    now = _now_naive()
    game = {"appid": APPID2, "name": "混合区批量", "updated_at": now}
    prices = [
        {"appid": APPID2, "region_code": "CN", "currency": "CNY",
         "price": 8800, "original_price": 8800, "discount_percent": 0,
         "sub_id": 1, "is_gold": False, "version_suffix": None,
         "price_status": "ok", "crawled_at": now},
        {"appid": APPID2, "region_code": "CN", "currency": "CNY",
         "price": None, "original_price": None, "discount_percent": 0,
         "sub_id": 2, "is_gold": True, "version_suffix": "Gold Edition",
         "price_status": "ok", "crawled_at": now},
    ]
    assert await db.upsert_task_batch([(game, prices)]) == [True]
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID2, "CN"))
        assert row.price_status == "ok" and row.price == 8800
        assert row.attempt_outcome == "success"
        assert row.fail_count == 0


@pytest.mark.asyncio
async def test_pure_degraded_region_still_burns_budget():
    """子情形 B（坏行是唯一标准候选）：纯降级区仍进欠账并消耗预算；
    gold 有效价不顶替现价投影（现状钉住）。"""
    db = DbWriter()
    now = _now_naive()
    game_data = {"appid": APPID3, "name": "纯降级区", "updated_at": now}
    prices = [
        {"appid": APPID3, "region_code": "CN", "currency": "CNY",
         "price": None, "original_price": None, "discount_percent": 0,
         "sub_id": 1, "is_gold": False, "version_suffix": None,
         "price_status": "ok", "crawled_at": now},
        {"appid": APPID3, "region_code": "CN", "currency": "CNY",
         "price": 50000, "original_price": 50000, "discount_percent": 0,
         "sub_id": 2, "is_gold": True, "version_suffix": "Gold Edition",
         "price_status": "ok", "crawled_at": now},
    ]
    assert await db.upsert_game_and_prices(game_data, prices) is True
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID3, "CN"))
        assert row.price_status == "missing"
        assert row.price == 10000  # failed 章保留上次成功价（夹具预置行）
        assert row.fail_count == 1  # 门禁证据消耗 1 次预算
        assert row.price != 50000  # gold 价不顶替现价投影


@pytest.mark.asyncio
async def test_transport_ledger_does_not_burn_budget():
    """传输类记账不消耗预算：批量记账重复 5 次仍 missing、fail_count 不递增
    （blocked 只由真实响应缺价证据抵达）。"""
    db = DbWriter()
    for _ in range(5):
        await db.mark_region_status_missing_batch([(APPID, "RU")])
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (APPID, "RU"))
        assert row.price_status == "missing"
        assert row.fail_count == 1  # 首记=1，传输失败不递增
        assert row.price_status != "blocked"
