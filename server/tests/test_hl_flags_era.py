"""refresh_hl_flags 折扣期语义（新史低只属首个到达该价的折扣期 + 兜底窗）。

- 新低期：现价比折扣期起点之前的最低价便宜 ≥1 角 → 本期首次到该价，
  整期持续记 1（持久性由「期起点」保证，不依赖单轮比较——现价行
  updated_at 每轮刷新、历史行只在变价时落库）；同一价位之后的折扣期
  期前最低已含上一期 → 平史低 2，只给第一个到达该价的期记新史低。
- 兜底窗：历史全部落在现价期内（首发即打折、历史短程看不到既往期）
  无法用既往期背书，期起点 ≤30 天记 1、超期回落 2（防短程历史反复
  判新史低）。
- 同价带：导入历史价只精确到角且进一（9207 分记成 9210），现价与期前
  最低差在带内视作同一价位 → flag 2；恰好便宜 1 角 → 新低期。
- 从未变价且无折扣 → 0；现价明显高于历史最低 → 打折 3 / 无折扣 0。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module
from app.core.database import get_session_factory, init_db
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

A_ERA = 996_101       # 新低期内多轮刷新
A_BAND = 996_102      # 角精度同价带（9207 vs 9210）
A_BAND_EDGE = 996_103  # 恰好便宜 1 角（9200 vs 9210）
A_OLD = 996_104       # 有既往期背书的新低期超 30 天（整期持续）
A_FIRST = 996_105     # 首发即打折
A_ABOVE_D = 996_106   # 高于历史最低且打折
A_ABOVE_N = 996_107   # 高于历史最低无折扣
A_CONST = 996_108     # 从未变价无折扣
A_REP = 996_109       # 同一价位第二个折扣期
A_CAP = 996_110       # 短程历史兜底窗超期


@pytest_asyncio.fixture(autouse=True)
async def _tmp_db(monkeypatch, tmp_path):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    database_module.get_settings.cache_clear()
    database_module.get_engine.cache_clear()
    database_module.get_session_factory.cache_clear()
    await init_db()
    yield
    database_module.get_settings.cache_clear()
    database_module.get_engine.cache_clear()
    database_module.get_session_factory.cache_clear()


def _hist(session, appid, cny_fen, days_ago, *, discount=0):
    session.add(
        GamePriceHistory(
            appid=appid, region_code="CN", currency="CNY",
            price=cny_fen, original_price=cny_fen, discount_percent=discount,
            sub_id=1, is_gold=False, version_suffix=None, is_bundle=False,
            price_status="ok", cny_fen=cny_fen,
            snapshot_at=datetime.now() - timedelta(days=days_ago),
        )
    )


def _game(session, appid, cn_fen, discount, updated_at):
    session.add(
        Game(appid=appid, name=f"史低期测试{appid}", created_at=updated_at, updated_at=updated_at)
    )
    session.add(
        GameCurrentPrice(
            appid=appid, region_code="CN", currency="CNY",
            price=cn_fen, original_price=cn_fen, discount_percent=discount,
            sub_id=1, price_status="ok", fail_count=0, cny_fen=cn_fen,
            updated_at=updated_at,
        )
    )


async def _flag(appid: int) -> int:
    async with get_session_factory()() as session:
        game = await session.get(Game, appid)
        return game.hl_flag or 0


@pytest.mark.asyncio
async def test_new_low_era_persists_across_refresh_cycles():
    """折扣期起点前最低 27600、现价 23115（6 天前进入）：每轮刷新都记
    新史低——现价行 updated_at 推进不改变期起点，flag 不随轮次消退。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_ERA, 23115, 33, now - timedelta(days=6))
        _hist(session, A_ERA, 27600, 40)
        _hist(session, A_ERA, 23115, 6, discount=33)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_ERA) == 1

    # 下一轮：现价行 updated_at 推进、价格不变（无新历史行），再刷新仍新史低
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (A_ERA, "CN"))
        row.updated_at = now
        await session.commit()
    await games_service.refresh_hl_flags([A_ERA])
    assert await _flag(A_ERA) == 1


@pytest.mark.asyncio
async def test_rounded_prior_within_band_is_flat():
    """既往最低 9210（角精度进一口径）、现价 9207：差 3 分在 0.1 元带内
    → 平史低（期起点回溯到同价位的 9210 快照）；带外恰便宜 1 角 → 新史低。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_BAND, 9207, 67, now - timedelta(days=1))
        _hist(session, A_BAND, 9210, 90)
        _hist(session, A_BAND, 9210, 20)
        _hist(session, A_BAND, 9207, 1, discount=67)

        _game(session, A_BAND_EDGE, 9200, 67, now - timedelta(days=1))
        _hist(session, A_BAND_EDGE, 9210, 90)
        _hist(session, A_BAND_EDGE, 9200, 1, discount=67)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_BAND) == 2
    assert await _flag(A_BAND_EDGE) == 1


@pytest.mark.asyncio
async def test_first_low_era_persists_past_30d():
    """有既往期背书的首次新低期整期持续：期起点已 40 天仍记新史低
    （30 天回落只是历史短程时的兜底，不作用于可背书的新低期）。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_OLD, 23115, 33, now)
        _hist(session, A_OLD, 27600, 100)
        _hist(session, A_OLD, 23115, 40, discount=33)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_OLD) == 1


@pytest.mark.asyncio
async def test_second_same_price_period_is_flat():
    """同一价位只给第一个折扣期记新史低：第一期（15 天前进新低）记 1；
    涨回原价后第二次同价打折（3 天前开始）→ 期前最低已含上一期 → 平史低。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_REP, 23115, 33, now - timedelta(days=15))
        _hist(session, A_REP, 34500, 30)
        _hist(session, A_REP, 23115, 15, discount=33)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_REP) == 1

    # 第二期：涨回原价（10 天前）后再打同价折（3 天前开始，现行价）
    async with get_session_factory()() as session:
        row = await session.get(GameCurrentPrice, (A_REP, "CN"))
        row.updated_at = now - timedelta(days=3)
        _hist(session, A_REP, 34500, 10)
        _hist(session, A_REP, 23115, 3, discount=33)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_REP) == 2


@pytest.mark.asyncio
async def test_short_history_fallback_caps_at_30d():
    """兜底窗：历史短程（只有现价期，看不到既往期）时无法背书首次新低，
    期起点超 30 天回落平史低，防止短程历史反复判新史低。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_CAP, 23115, 33, now - timedelta(days=40))
        _hist(session, A_CAP, 23115, 40, discount=33)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_CAP) == 2


@pytest.mark.asyncio
async def test_first_sale_counts_as_new_low():
    """历史全部落在现价期内（首发即打折，6 天前进入）：有史以来第一次
    到该价 → 新史低；期起点即首行快照。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_FIRST, 23115, 33, now - timedelta(days=6))
        _hist(session, A_FIRST, 23115, 6, discount=33)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_FIRST) == 1


@pytest.mark.asyncio
async def test_price_above_low_not_flagged():
    """现价明显高于历史最低：打折记 3，无折扣记 0。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_ABOVE_D, 15000, 20, now - timedelta(days=5))
        _hist(session, A_ABOVE_D, 10000, 10)
        _hist(session, A_ABOVE_D, 15000, 5, discount=20)

        _game(session, A_ABOVE_N, 15000, 0, now - timedelta(days=5))
        _hist(session, A_ABOVE_N, 10000, 10)
        _hist(session, A_ABOVE_N, 15000, 5)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_ABOVE_D) == 3
    assert await _flag(A_ABOVE_N) == 0


@pytest.mark.asyncio
async def test_constant_price_nondiscounted_is_zero():
    """从未变价且无折扣：无期前最低，不入平史低。"""
    now = datetime.now()
    async with get_session_factory()() as session:
        _game(session, A_CONST, 10000, 0, now)
        _hist(session, A_CONST, 10000, 40)
        await session.commit()

    await games_service.refresh_hl_flags(None)
    assert await _flag(A_CONST) == 0
