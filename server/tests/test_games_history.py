"""games 历史价格查询（版本化切片）的行为验收。

用合成 appid 播种 game_price_history（标准版 / Deluxe / Gold / 他区 / 锁区行），
验证 sub_id 过滤、缺省标准版语义、days 窗口（0=全部）、versions 地区感知下拉源；
夹具负责播种与清理，不触碰真实库数据。
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.database import get_session_factory, init_db
from app.domains.games import service
from app.domains.games.models import GamePriceHistory

APPID = 990_001  # 合成 appid，避开真实库
STANDARD_SUB = 78771001
DELUXE_SUB = 78771002
GOLD_SUB = 78771003


def _h(
    appid: int,
    region: str,
    price: int | None,
    discount: int,
    sub_id: int,
    is_gold: bool,
    suffix: str | None,
    at: datetime,
    status: str = "ok",
) -> GamePriceHistory:
    return GamePriceHistory(
        appid=appid,
        region_code=region,
        currency="CNY" if region == "CN" else "UAH",
        price=price,
        original_price=price,
        discount_percent=discount,
        sub_id=sub_id,
        is_gold=is_gold,
        version_suffix=suffix,
        price_status=status,
        cny_fen=price,  # 直接给 CNY 分，规避汇率换算依赖
        snapshot_at=at,
    )


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    now = datetime.now()
    rows = [
        # 标准版三切片：¥100 → ¥80（折）→ ¥90
        _h(APPID, "CN", 10000, 0, STANDARD_SUB, False, None, now - timedelta(days=200)),
        _h(APPID, "CN", 8000, 20, STANDARD_SUB, False, None, now - timedelta(days=100)),
        _h(APPID, "CN", 9000, 10, STANDARD_SUB, False, None, now - timedelta(days=10)),
        # Deluxe 版两切片
        _h(APPID, "CN", 20000, 0, DELUXE_SUB, False, "Deluxe Edition", now - timedelta(days=150)),
        _h(APPID, "CN", 16000, 20, DELUXE_SUB, False, "Deluxe Edition", now - timedelta(days=50)),
        # Gold 版（is_gold 无后缀）
        _h(APPID, "CN", 30000, 0, GOLD_SUB, True, None, now - timedelta(days=80)),
        # UA 区仅标准版（versions 地区感知）
        _h(APPID, "UA", 5000, 0, STANDARD_SUB, False, None, now - timedelta(days=30)),
        # 锁区行：不应进 points（versions 查询也只取 ok）
        _h(APPID, "CN", None, 0, STANDARD_SUB, False, None, now - timedelta(days=5), status="locked"),
    ]
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(delete(GamePriceHistory).where(GamePriceHistory.appid == APPID))
        await session.commit()


@pytest.mark.asyncio
async def test_default_is_standard():
    """缺省 sub_id = 标准版切片（非 gold、无后缀），锁区行不进 points。"""
    r = await service.get_game_history(APPID, region="cn")
    assert [p["cnyFen"] for p in r["points"]] == [10000, 8000, 9000]
    assert r["lowest"]["cnyFen"] == 8000
    assert r["highest"]["cnyFen"] == 10000
    assert r["count"] == 3
    # 史低次数：¥8000 是唯一跌破前低的点（10000 → 8000 一次），回升后未再触及
    assert r["lowestHits"] == 1


@pytest.mark.asyncio
async def test_lowest_hits_counts_repeat_visits():
    """史低次数：反复回到同一史低价的每次独立事件都计数，同一促销期
    内的连续同价位快照只计一次。"""
    now = datetime.now()
    repeat_appid = 990_002
    rows = [
        # 原价 → 折后史低 → 回原价 → 再回史低（第二次）→ 回原价 → 史低期三连快照（第三次）
        _h(repeat_appid, "CN", 10000, 0, STANDARD_SUB, False, None, now - timedelta(days=300)),
        _h(repeat_appid, "CN", 5000, 50, STANDARD_SUB, False, None, now - timedelta(days=200)),
        _h(repeat_appid, "CN", 10000, 0, STANDARD_SUB, False, None, now - timedelta(days=150)),
        _h(repeat_appid, "CN", 5000, 50, STANDARD_SUB, False, None, now - timedelta(days=100)),
        _h(repeat_appid, "CN", 10000, 0, STANDARD_SUB, False, None, now - timedelta(days=60)),
        _h(repeat_appid, "CN", 5000, 50, STANDARD_SUB, False, None, now - timedelta(days=30)),
        _h(repeat_appid, "CN", 5000, 50, STANDARD_SUB, False, None, now - timedelta(days=29)),
        _h(repeat_appid, "CN", 5000, 50, STANDARD_SUB, False, None, now - timedelta(days=28)),
    ]
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()
    try:
        r = await service.get_game_history(repeat_appid, region="cn")
        assert [p["cnyFen"] for p in r["points"]] == [10000, 5000, 10000, 5000, 10000, 5000, 5000, 5000]
        assert r["lowest"]["cnyFen"] == 5000
        # 三段史低价：-200d 单点 / -100d 单点 / -30~-28d 连续三快照（一段）
        assert r["lowestHits"] == 3
    finally:
        async with get_session_factory()() as session:
            await session.execute(
                delete(GamePriceHistory).where(GamePriceHistory.appid == repeat_appid)
            )
            await session.commit()


@pytest.mark.asyncio
async def test_sub_id_filter():
    """显式 sub_id 按包号精确匹配（含 Gold / 带后缀版本）。"""
    r = await service.get_game_history(APPID, region="cn", sub_id=DELUXE_SUB)
    assert [p["cnyFen"] for p in r["points"]] == [20000, 16000]
    r = await service.get_game_history(APPID, region="cn", sub_id=GOLD_SUB)
    assert [p["cnyFen"] for p in r["points"]] == [30000]


@pytest.mark.asyncio
async def test_versions_region_scoped():
    """versions 只含该地区 ok 状态 distinct 版本，标准版排最前。"""
    r = await service.get_game_history(APPID, region="cn")
    versions = r["versions"]
    assert len(versions) == 3
    assert versions[0]["suffix"] is None and not versions[0]["isGold"]  # 标准版首位
    assert {v["subId"] for v in versions} == {STANDARD_SUB, DELUXE_SUB, GOLD_SUB}
    deluxe = next(v for v in versions if v["subId"] == DELUXE_SUB)
    assert deluxe["suffix"] == "Deluxe Edition"

    r_ua = await service.get_game_history(APPID, region="ua")
    assert [v["subId"] for v in r_ua["versions"]] == [STANDARD_SUB]


@pytest.mark.asyncio
async def test_days_window():
    """days 窗口裁剪旧切片；days=0 = 全部时间。"""
    r = await service.get_game_history(APPID, region="cn", days=60)
    assert [p["cnyFen"] for p in r["points"]] == [9000]
    r_all = await service.get_game_history(APPID, region="cn", days=0)
    assert [p["cnyFen"] for p in r_all["points"]] == [10000, 8000, 9000]


@pytest.mark.asyncio
async def test_price_context():
    """price-context：当时价 = 截止日前最后一个有效快照，史低 = 截止日前最小值；
    日后的切片不漏入；非标准版（Deluxe/Gold）不混入；空窗/非法日期返回 null 对。"""
    # -50d：窗口内只剩 -200d(10000) 与 -100d(8000) 两个标准版切片
    day = (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")
    r = await service.get_price_context(APPID, date=day)
    assert r["at"]["cnyFen"] == 8000
    assert r["at"]["discount"] == 20
    assert r["lowest"]["cnyFen"] == 8000
    assert r["lowest"]["snapshotAt"] == r["at"]["snapshotAt"]

    # -150d：只剩首个切片（当时价 = 史低 = 原价）
    early = (datetime.now() - timedelta(days=150)).strftime("%Y-%m-%d")
    r2 = await service.get_price_context(APPID, date=early)
    assert r2["at"]["cnyFen"] == 10000 and r2["lowest"]["cnyFen"] == 10000

    # 日后的切片（-10d 的 9000）与更晚的同 sub 高价行都不得漏入
    late_appid = 990_006
    rows = [
        _h(late_appid, "CN", 10000, 0, STANDARD_SUB, False, None, datetime.now() - timedelta(days=90)),
        _h(late_appid, "CN", 12000, 0, STANDARD_SUB, False, None, datetime.now() - timedelta(days=1)),
    ]
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()
    try:
        cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        r3 = await service.get_price_context(late_appid, date=cutoff)
        assert r3["at"]["cnyFen"] == 10000 and r3["lowest"]["cnyFen"] == 10000
    finally:
        async with get_session_factory()() as session:
            await session.execute(delete(GamePriceHistory).where(GamePriceHistory.appid == late_appid))
            await session.commit()

    # 无任何切片的日期 → null 对；非法日期不抛异常
    r4 = await service.get_price_context(APPID, date="2000-01-01")
    assert r4["at"] is None and r4["lowest"] is None
    r5 = await service.get_price_context(APPID, date="not-a-date")
    assert r5["at"] is None and r5["lowest"] is None


@pytest.mark.asyncio
async def test_price_context_batch():
    """批量上下文：有效对出值，无历史/非法对回 null 对且不抛错；顺序保持。"""
    day = (datetime.now() - timedelta(days=50)).strftime("%Y-%m-%d")
    r = await service.get_price_context_batch([(APPID, day), (999_999_999, day), (0, "")])
    assert r["results"][0]["at"]["cnyFen"] == 8000
    assert r["results"][1]["at"] is None and r["results"][1]["lowest"] is None
    assert r["results"][2]["at"] is None and r["results"][2]["lowest"] is None


@pytest.mark.asyncio
async def test_empty_region_still_lists_versions():
    """无切片地区返回空 points 但结构完整（versions 为空列表）。"""
    r = await service.get_game_history(APPID, region="us")
    assert r["points"] == []
    assert r["lowest"] is None and r["highest"] is None
    assert r["count"] == 0
    assert "lowestHits" not in r  # 空序列不返回史低次数
    assert r["versions"] == []


# ── 跨 sub 代际的标准版序列 ──────────────────────────────────────────
# Steam 改包内容就换 sub_id，若把默认序列钉在「当前在售」那一个 sub 上，
# 换代之前的历史会被整段滤掉（真实库：872410/CN 只出 2 个点而库里 130 行）。
# 下面三条锁定修复后的口径。

GEN_APPID = 990_003
OLD_GEN_SUB = 78772001
NEW_GEN_SUB = 78772002
BUNDLE_APPID = 990_004
BUNDLE_SUB = 78773001
ZERO_APPID = 990_005


async def _seed_current(appid: int, region: str, sub_id: int | None, price: int = 9000) -> None:
    """播一行 game_current_prices，让缺省标准版能解析出 sale_sub。"""
    from app.domains.games.models import GameCurrentPrice

    async with get_session_factory()() as session:
        session.add(
            GameCurrentPrice(
                appid=appid,
                region_code=region,
                currency="CNY",
                price=price,
                sub_id=sub_id,
                price_status="ok",
                cny_fen=price,
                updated_at=datetime.now(),
            )
        )
        await session.commit()


async def _cleanup(*appids: int) -> None:
    from app.domains.games.models import GameCurrentPrice

    async with get_session_factory()() as session:
        await session.execute(delete(GamePriceHistory).where(GamePriceHistory.appid.in_(appids)))
        await session.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_(appids)))
        await session.commit()


@pytest.mark.asyncio
async def test_standard_series_spans_sub_generations():
    """标准版序列跨 sub 代际取并集：旧代际的历史不被新代际的在售 sub 滤掉。"""
    now = datetime.now()
    rows = [
        # 旧代际：三切片（2021 起）
        _h(GEN_APPID, "CN", 10000, 0, OLD_GEN_SUB, False, None, now - timedelta(days=1200)),
        _h(GEN_APPID, "CN", 6000, 40, OLD_GEN_SUB, False, None, now - timedelta(days=900)),
        _h(GEN_APPID, "CN", 10000, 0, OLD_GEN_SUB, False, None, now - timedelta(days=600)),
        # 换代：新代际只有 2 个点
        _h(GEN_APPID, "CN", 12000, 0, NEW_GEN_SUB, False, None, now - timedelta(days=30)),
        _h(GEN_APPID, "CN", 8000, 33, NEW_GEN_SUB, False, None, now - timedelta(days=3)),
        # 干扰项：Deluxe（带后缀）与 Gold 不得混入标准版序列
        _h(GEN_APPID, "CN", 50000, 0, 78772009, False, "Deluxe Edition", now - timedelta(days=500)),
        _h(GEN_APPID, "CN", 99000, 0, 78772010, True, None, now - timedelta(days=400)),
    ]
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()
    await _seed_current(GEN_APPID, "CN", NEW_GEN_SUB)
    try:
        r = await service.get_game_history(GEN_APPID, region="cn")
        # 旧代际 3 点 + 新代际 2 点（修复前只有新代际的 2 点）
        assert [p["cnyFen"] for p in r["points"]] == [10000, 6000, 10000, 12000, 8000]
        assert r["count"] == 5
        # 史低 6000 落在旧代际——只取在售 sub 时这个史低会被隐藏
        assert r["lowest"]["cnyFen"] == 6000
    finally:
        await _cleanup(GEN_APPID)


@pytest.mark.asyncio
async def test_bundle_only_region_still_served_via_current_sub():
    """该区唯一在售 sub 被判为 bundle-as-sub 时仍要有序列（红警 3 这类游戏
    只在捆绑包 sub 下售卖）。标准版判据排除捆绑包，靠并集右项的当前在售 sub 兜底。"""
    now = datetime.now()
    rows = [
        _h(BUNDLE_APPID, "CN", 30000, 0, BUNDLE_SUB, False, None, now - timedelta(days=40)),
        _h(BUNDLE_APPID, "CN", 15000, 50, BUNDLE_SUB, False, None, now - timedelta(days=10)),
    ]
    for row in rows:
        row.is_bundle = True
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()
    await _seed_current(BUNDLE_APPID, "CN", BUNDLE_SUB)
    try:
        r = await service.get_game_history(BUNDLE_APPID, region="cn")
        assert [p["cnyFen"] for p in r["points"]] == [30000, 15000]
    finally:
        await _cleanup(BUNDLE_APPID)


@pytest.mark.asyncio
async def test_zero_sub_id_does_not_widen_series():
    """sale_sub=0（db_writer 用 `or 0` 把 None 归一成 0）不是有效 sub，不能进并集——
    否则 `sub_id == 0` 会把全库所有 sub_id=0 的杂行拉进这一部游戏的走势。"""
    now = datetime.now()
    rows = [
        _h(ZERO_APPID, "CN", 10000, 0, 78774001, False, None, now - timedelta(days=100)),
        # 杂行：sub_id=0 且带版本后缀。带后缀 → 不满足标准版判据，
        # 只能从「当前在售 sub」那一项漏进来，正是守卫要挡的路径。
        _h(ZERO_APPID, "CN", 7000, 30, 0, False, "Deluxe Edition", now - timedelta(days=50)),
    ]
    async with get_session_factory()() as session:
        session.add_all(rows)
        await session.commit()
    await _seed_current(ZERO_APPID, "CN", 0)
    try:
        r = await service.get_game_history(ZERO_APPID, region="cn")
        assert [p["cnyFen"] for p in r["points"]] == [10000]
    finally:
        await _cleanup(ZERO_APPID)
