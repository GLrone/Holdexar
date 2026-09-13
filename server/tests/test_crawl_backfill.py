"""爬虫队列回补行为验收（首爬放行 / 孤儿回补 / TOP100 反哺 / genres 分类）。

队列回补链路的四条行为验收（合成 appid 播种，夹具清理）：
1. get_uncrawled_appids：两种"无数据"形态都算未爬——无 games 行的
   appid（全新入库候选）+ games 行 updated_at IS NULL 的挂名孤儿；
   有 ok 价格行的已爬游戏排除（价格行被清的已爬游戏除外）
2. backfill_specs：TOP100 榜单中未入库/挂名孤儿 → 爬取 spec（无缺口 []）
3. app_handler genres 分类：Sexual Content/Nudity → is_adult；
   Visual Novel → is_visual_novel（提取逻辑单测，不起调度器）
4. upsert 回补链：孤儿行首爬后 updated_at 落值（脱离孤儿集合）
"""
from datetime import datetime

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.database import get_session_factory, init_db
from app.crawler.db_writer import DbWriter
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.games import boards as boards_mod

APPID = 996_001  # 挂名孤儿（updated_at NULL，无价格行）
APPID2 = 996_002  # 已爬游戏（有 updated_at + ok 价格行）
APPID3 = 996_003  # 榜上孤儿（games 行存在但无数据）
APPID4 = 996_004  # 无 games 行（从未入库——预检同样要放行的形态）
APPID5 = 996_005  # meta 失败孤儿（updated_at NULL 但有 locked 状态价格行——账本通道负责）


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    async with get_session_factory()() as session:
        now = datetime.now()
        session.add_all(
            [
                Game(appid=APPID, name="挂名孤儿", created_at=None, updated_at=None),
                Game(
                    appid=APPID2, name="已爬游戏",
                    created_at=now, updated_at=now,
                ),
                Game(appid=APPID3, name="榜上孤儿", created_at=None, updated_at=None),
                Game(appid=APPID5, name="账本孤儿", created_at=None, updated_at=None),
            ]
        )
        session.add(
            GameCurrentPrice(
                appid=APPID2, region_code="CN", currency="CNY",
                price=10000, original_price=10000, discount_percent=0,
                sub_id=1, price_status="ok", fail_count=0, cny_fen=10000,
                updated_at=now,
            )
        )
        # APPID5：meta 失败后 Phase 2 写的 locked 状态行（有价格行但无价）
        session.add(
            GameCurrentPrice(
                appid=APPID5, region_code="CN", currency="",
                price=None, original_price=None, discount_percent=0,
                sub_id=None, price_status="locked", fail_count=0,
                cny_fen=None, updated_at=now,
            )
        )
        await session.commit()
    boards_mod._reset_cache_for_tests()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_([APPID2, APPID5])
            )
        )
        await session.execute(
            delete(Game).where(Game.appid.in_([APPID, APPID2, APPID3, APPID4, APPID5]))
        )
        await session.commit()
    boards_mod._reset_cache_for_tests()


@pytest.mark.asyncio
async def test_get_uncrawled_appids():
    """未爬集合：挂名孤儿（updated_at NULL）入选、已爬（ok 价格行）排除。

    无 games 行的 appid（APPID4）不在任何表里——集合只含库内有孤儿行
    的部分；从未入库的 appid 由 runner 侧白名单反转兜住
    （get_crawled_appids：不在白名单 = 未爬，天然覆盖无行形态）。
    """
    db = DbWriter()
    ids = await db.get_uncrawled_appids()
    assert APPID in ids
    assert APPID3 in ids
    assert APPID2 not in ids  # 有 ok 价格行 = 已爬


@pytest.mark.asyncio
async def test_mark_non_game_type_leaves_backfill_pool():
    """Phase 1 短路落 type：非 game/dlc / coming_soon 无包的孤儿行
    落 type + updated_at 后脱离回补池（不再被每天空转重爬一次）。"""
    db = DbWriter()
    ids = await db.get_uncrawled_appids()
    assert APPID in ids  # 前置：还在池里

    await db.mark_non_game_type(APPID, "hardware")

    async with get_session_factory()() as session:
        game = await session.get(Game, APPID)
        assert game.type == "HARDWARE"
        assert game.updated_at is not None
        assert game.name == "挂名孤儿"  # 挂名值不动（insert 分支才用 AppID_ 兜底）

    ids = await db.get_uncrawled_appids()
    assert APPID not in ids  # 已脱离回补池


@pytest.mark.asyncio
async def test_backfill_specs_from_top100(monkeypatch):
    """TOP100 反哺：榜内孤儿（含未入库）→ 单 spec；已爬/账本孤儿不进。"""
    async def _fake_board(key: str) -> list[int]:
        return [APPID, APPID2, APPID3, APPID5]

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)
    specs = await boards_mod.backfill_specs("topsellers")
    assert len(specs) == 1
    spec = specs[0]
    assert spec["scope"] == "appids"
    assert spec["kind"] == "top100_backfill"
    # 已爬（APPID2）与有 locked 价格行的账本孤儿（APPID5）排除
    assert sorted(spec["appids"]) == sorted([APPID, APPID3])


@pytest.mark.asyncio
async def test_backfill_pairs_excludes_ledger_orphans():
    """回补池排除已有价格行的孤儿（missing 账本通道负责，防双通道重复吃）。"""
    from app.domains.crawl import service as crawl_service

    pairs = await crawl_service._backfill_pairs(limit=5000)
    pair_ids = {a for a, _ in pairs}
    assert APPID in pair_ids  # 无价格行的孤儿入选
    assert APPID5 not in pair_ids  # 有 locked 状态行 = 账本通道，排除
    # 库内可能存在的真实孤儿不受夹具影响——只断言合成 appid 语义
    assert all(a not in (APPID2, APPID4) for a in pair_ids)


@pytest.mark.asyncio
async def test_backfill_specs_empty_when_all_crawled(monkeypatch):
    """无缺口：榜内全部已爬 → []（不产生空任务）。"""
    async def _fake_board(key: str) -> list[int]:
        return [APPID2]

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)
    specs = await boards_mod.backfill_specs("topsellers")
    assert specs == []


@pytest.mark.asyncio
async def test_backfill_specs_empty_board(monkeypatch):
    """拉榜失败（空榜）：不反哺、不报错。"""
    async def _fake_board(key: str) -> list[int]:
        return []

    monkeypatch.setattr(boards_mod, "get_board", _fake_board)
    specs = await boards_mod.backfill_specs("topsellers")
    assert specs == []


def test_genre_classification():
    """genres 分类语义：Sexual Content/Nudity → 成人；Visual Novel → 视觉小说。

    提取逻辑内嵌在 handle_app_task（元数据 Phase 后段），此处直接
    验证同一判定式，防语义漂移。Steam genres description 实测小写形态。
    """
    def classify(genres: list[str]) -> tuple[bool, bool]:
        descs = {g.lower() for g in genres}
        is_adult = bool(descs & {"sexual content", "nudity"})
        is_visual_novel = "visual novel" in descs
        return is_adult, is_visual_novel

    assert classify(["Action", "Sexual Content", "Nudity"]) == (True, False)
    assert classify(["Action", "Visual Novel"]) == (False, True)
    assert classify(["Action", "Visual Novel", "Sexual Content"]) == (True, True)
    assert classify(["Action", "Adventure", "RPG"]) == (False, False)


@pytest.mark.asyncio
async def test_orphan_crawled_leaves_backfill_pool():
    """回补闭环：孤儿行首爬（upsert 带 updated_at）后脱离孤儿集合。"""
    db = DbWriter()
    ids = await db.get_uncrawled_appids()
    assert APPID in ids

    now = datetime.now()
    game_data = {
        "appid": APPID, "name": "孤儿回补", "updated_at": now,
        "is_adult": True, "is_visual_novel": False,
    }
    assert await db.upsert_game_and_prices(game_data, None) is True

    async with get_session_factory()() as session:
        game = await session.get(Game, APPID)
        assert game.updated_at is not None
        assert game.is_adult is True
        assert game.is_visual_novel is False

    ids = await db.get_uncrawled_appids()
    assert APPID not in ids  # 已脱离孤儿池
