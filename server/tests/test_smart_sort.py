"""smart 排序（sort=smart）的行为验收：打破「折扣硬墙」。

合成 99xxxx 探针行（真实库只读前提 + 幂等清理），只造两款的对照组：

  P1（原价高价值）：差价 ¥30、97% 好评 / 20 万评测、无折扣
  P2（打折低价值）：差价 ¥1、80% 好评 / 50 评测、新史低 70% off

legacy（默认 default 排序）下 P2 必在前——P1 是 hl_priority=0 的原价游戏，
被所有打折游戏压在后面；smart 下 P1 应反超。这正是本次改版的验证点。
"""
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, init_db
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.games.scoring import smart_score

P1, P2 = 99600301, 99600302
# 零评价成熟度对照组：P3 老游戏零评价 / P4 新发行零评价 / P5 有评测对照
P3, P4, P5 = 99600303, 99600304, 99600305


@pytest_asyncio.fixture(autouse=True)
async def _migrated():
    """存量库 ALTER 加列（smart_score）幂等执行，与 test_games_query 同法。"""
    await init_db()
    yield


async def _seed() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_([P1, P2])))
        await s.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_([P1, P2])))
        # P1：原价，差价 ¥30（CN ¥100 / US ¥70），高口碑高评测
        s.add(Game(
            appid=P1, name="smart-probe-flat", type="GAME",
            positive_rate=9700, review_count=200000, hl_flag=0, diff_fen=0,
        ))
        s.add(GameCurrentPrice(
            appid=P1, region_code="CN", price_status="ok", currency="CNY",
            price=10000, original_price=10000, discount_percent=0, cny_fen=10000,
        ))
        s.add(GameCurrentPrice(
            appid=P1, region_code="US", price_status="ok", currency="USD",
            price=1000, original_price=1000, discount_percent=0, cny_fen=7000,
        ))
        # P2：新史低 70% off，差价 ¥1，低评测
        s.add(Game(
            appid=P2, name="smart-probe-discount", type="GAME",
            positive_rate=8000, review_count=50, hl_flag=1, diff_fen=0,
        ))
        s.add(GameCurrentPrice(
            appid=P2, region_code="CN", price_status="ok", currency="CNY",
            price=10000, original_price=33333, discount_percent=70, cny_fen=10000,
        ))
        s.add(GameCurrentPrice(
            appid=P2, region_code="US", price_status="ok", currency="USD",
            price=1400, original_price=1400, discount_percent=0, cny_fen=9900,
        ))
        await s.commit()
    # 预计算列（diff_fen / smart_score）按增量刷新生效
    await games_service.refresh_sort_cache([P1, P2])


async def _cleanup() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_([P1, P2])))
        await s.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_([P1, P2])))
        await s.commit()


async def _probe_ids(sort: str) -> list[int]:
    res = await games_service.list_games(sort=sort, limit=100, q="smart-probe")
    return [it["appid"] for it in res["items"]]


@pytest.mark.asyncio
async def test_legacy_puts_discounted_first():
    """对照组：legacy 下打折的 P2 在前，原价 P1 沉底（折扣硬墙）。"""
    await _seed()
    try:
        ids = await _probe_ids("default")
        assert ids == [P2, P1]
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_smart_breaks_discount_wall():
    """smart 下 P1 反超：原价但差价/质量/熟悉度俱佳的款回到前排。"""
    await _seed()
    try:
        ids = await _probe_ids("smart")
        assert ids == [P1, P2]
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_smart_score_persisted_and_exposed():
    """落库值与响应拆解因子一致（实验池展示依赖同一口径）。"""
    await _seed()
    try:
        res = await games_service.list_games(sort="smart", limit=100, q="smart-probe")
        by_id = {it["appid"]: it for it in res["items"]}
        p1 = by_id[P1]
        expected = smart_score(3000, 9700, 200000, 0, 0)
        assert p1["smartScore"] == pytest.approx(expected, abs=1e-4)
        # 四因子拆解：save(¥30)>0 / quality 高 / timing=0（原价）/ familiarity=1
        f = p1["smartFactors"]
        assert f["save"] > 0.3 and f["quality"] > 0.9
        assert f["timing"] == 0.0 and f["familiarity"] == 1.0
        # 打折款的 timing 拿满、save 几乎为 0——两款的差异来路一目了然
        p2 = by_id[P2]
        assert p2["smartFactors"]["timing"] == 1.0
        assert p2["smartFactors"]["save"] < 0.05
    finally:
        await _cleanup()


@pytest.mark.asyncio
async def test_smart_keeps_discount_hard_gate():
    """硬规则保留：国区无价的游戏不进 smart 列表（有资格门槛没被打破）。"""
    await _seed()
    try:
        async with get_session_factory()() as s:
            await s.execute(
                delete(GameCurrentPrice).where(
                    GameCurrentPrice.appid == P1, GameCurrentPrice.region_code == "CN"
                )
            )
            await s.commit()
        ids = await _probe_ids("smart")
        assert P1 not in ids and P2 in ids
    finally:
        await _cleanup()


async def _seed_zero_review() -> None:
    """零评价对照组：P3 发行 3 年仍 0 评价 / P4 刚发行 0 评价 / P5 有评测对照。"""
    fresh_day = (date.today() - timedelta(days=7)).isoformat()
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_([P3, P4, P5])))
        await s.execute(
            delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_([P3, P4, P5]))
        )
        # P3：¥30 差价 + 新史低 + 0 评价 + 2022 年发行（问题样本同型）
        s.add(Game(
            appid=P3, name="smart-probe-oldzero", type="GAME",
            review_count=0, hl_flag=1, diff_fen=0, release_date="2022-11-15",
        ))
        # P4：与 P3 同价差同时机，刚发行一周（评价尚未形成的合法状态）
        s.add(Game(
            appid=P4, name="smart-probe-freshzero", type="GAME",
            review_count=0, hl_flag=1, diff_fen=0, release_date=fresh_day,
        ))
        # P5：¥20 差价 + 新史低 + 60% 好评 / 15 条评测（真实质量信号）
        s.add(Game(
            appid=P5, name="smart-probe-modest", type="GAME",
            positive_rate=6000, review_count=15, hl_flag=1, diff_fen=0,
        ))
        for appid, cn_price, low_fen in ((P3, 10000, 7000), (P4, 10000, 7000), (P5, 10000, 8000)):
            s.add(GameCurrentPrice(
                appid=appid, region_code="CN", price_status="ok", currency="CNY",
                price=cn_price, original_price=int(cn_price / 0.7), discount_percent=30,
                cny_fen=cn_price,
            ))
            s.add(GameCurrentPrice(
                appid=appid, region_code="US", price_status="ok", currency="USD",
                price=low_fen // 10, original_price=low_fen // 10, discount_percent=0,
                cny_fen=low_fen,
            ))
        await s.commit()
    await games_service.refresh_sort_cache([P3, P4, P5])


async def _cleanup_zero_review() -> None:
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_([P3, P4, P5])))
        await s.execute(
            delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_([P3, P4, P5]))
        )
        await s.commit()


@pytest.mark.asyncio
async def test_old_zero_review_demoted_below_reviewed_rival():
    """零评价成熟度：老游戏 0 评价不能仅凭 save+timing 压过同档真实评测。

    P3（3 年 0 评价 + ¥30 差 + 新史低）在零评价=中性口径下总分 0.471，
    反超 P5（¥20 差 + 新史低 + 60%/15 条评测）的 0.460；质量跌破中性后
    P3 落到 P5 之下。刚发行的 P4（同 P3 条件）质量保持近中性，不被同等压制。
    """
    await _seed_zero_review()
    try:
        res = await games_service.list_games(sort="smart", limit=100, q="smart-probe")
        by_id = {it["appid"]: it for it in res["items"]}
        ids = [it["appid"] for it in res["items"]]
        assert ids.index(P5) < ids.index(P3)
        assert ids.index(P4) < ids.index(P3)
        # 质量因子语义：老零评价跌破中性；新零评价保持近中性
        assert by_id[P3]["smartFactors"]["quality"] < 0.32
        assert by_id[P4]["smartFactors"]["quality"] > 0.45
        # 落库列与接口拆解同口径（零评价新路径同样核一遍）
        async with get_session_factory()() as s:
            row = (await s.execute(select(Game).where(Game.appid == P3))).scalar_one()
        assert row.smart_score == pytest.approx(by_id[P3]["smartScore"], abs=1e-4)
        assert row.smart_score == pytest.approx(
            smart_score(3000, None, 0, 1, 30, "2022-11-15"), abs=1e-4
        )
    finally:
        await _cleanup_zero_review()