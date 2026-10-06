"""games_name_fts 名称影子检索：2 字中文 / 英文词 / 整句 / LIKE 兜底 / 触发器同步。

影子列 = CJK bigram + 英文原词（games/searchtext.name_bigram），v13 迁移建表
+ 触发器同步 + 全量回填；search_catalog 走 FTS 优先、LIKE 兜底。隔离临时库，
不经真实库。
"""
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.database import (
    _migrate_games_name_fts,
    get_engine,
    get_session_factory,
    init_db,
)
from app.domains.games import service

_GAMES = [
    dict(appid=900001, name="只狼：影逝二度", name_en="Sekiro: Shadows Die Twice", review_count=100),
    dict(appid=900002, name="艾尔登法环", name_en="ELDEN RING", review_count=300),
    dict(appid=900003, name="星露谷物语", name_en="Stardew Valley", review_count=200),
    dict(appid=900004, name="黑魂：重制版", name_en="DARK SOULS: REMASTERED", review_count=50),
    dict(appid=900005, name="群星", name_en="Stellaris", review_count=80),
    dict(appid=900006, name="", name_en="Hollow Knight", review_count=10),
]


@pytest_asyncio.fixture
async def seeded_db():
    await init_db()
    from app.domains.games.models import Game

    async with get_session_factory()() as session:
        for g in _GAMES:
            session.add(Game(**g))
        await session.commit()
    yield


def _appids(items: list[dict]) -> set[int]:
    return {it["appid"] for it in items}


@pytest.mark.asyncio
async def test_two_char_chinese_name_hits(seeded_db):
    """2 字中文游戏名经 bigram 影子列命中。"""
    out = await service.search_catalog("只狼")
    assert 900001 in _appids(out)


@pytest.mark.asyncio
async def test_english_word_hits_case_insensitive(seeded_db):
    out = await service.search_catalog("sekiro")
    assert 900001 in _appids(out)


@pytest.mark.asyncio
async def test_contiguous_multi_char_name_hits(seeded_db):
    out = await service.search_catalog("影逝二度")
    assert 900001 in _appids(out)


@pytest.mark.asyncio
async def test_full_sentence_query_hits_via_any_token(seeded_db):
    """整句自然语言：OR 语义 + 评测数排序——句中任一名称片段可命中。"""
    out = await service.search_catalog("有没有类似只狼的动作游戏")
    assert 900001 in _appids(out)


@pytest.mark.asyncio
async def test_two_char_prefix_hits(seeded_db):
    out = await service.search_catalog("星露")
    assert 900003 in _appids(out)


@pytest.mark.asyncio
async def test_name_en_only_game_hits(seeded_db):
    out = await service.search_catalog("Hollow")
    assert 900006 in _appids(out)


@pytest.mark.asyncio
async def test_single_char_falls_back_to_like(seeded_db):
    """单字 CJK 无 bigram 可用 → LIKE 通道兜底（行为与旧版一致）。"""
    out = await service.search_catalog("狼")
    assert 900001 in _appids(out)


@pytest.mark.asyncio
async def test_result_shape_unchanged(seeded_db):
    """返回契约零变化：找游戏页 / 领航员消费的键集合不变。"""
    (item,) = [it for it in await service.search_catalog("只狼") if it["appid"] == 900001]
    assert set(item.keys()) == {
        "appid", "name", "nameEn", "basePriceFen", "discount", "positiveRate", "reviewCount",
    }


@pytest.mark.asyncio
async def test_triggers_keep_shadow_in_sync(seeded_db):
    """触发器同步：UPDATE 换名旧词失效新词生效；DELETE 行 FTS 同步消失。"""
    async with get_session_factory()() as session:
        await session.execute(
            text("UPDATE games SET name = '丛雨的另外一个名字' WHERE appid = 900001")
        )
        await session.commit()
    assert _appids(await service.search_catalog("只狼")) == set()
    assert 900001 in _appids(await service.search_catalog("丛雨"))

    async with get_session_factory()() as session:
        await session.execute(text("DELETE FROM games WHERE appid = 900001"))
        await session.commit()
    assert _appids(await service.search_catalog("丛雨")) == set()


@pytest.mark.asyncio
async def test_migration_backfill_rebuilds_from_games_table(seeded_db):
    """回填幂等：清空影子表后重放 v13 迁移体，全量从 games 表重建。"""
    async with get_session_factory()() as session:
        await session.execute(text("DELETE FROM games_name_fts"))
        await session.commit()

    async with get_engine().begin() as conn:
        await _migrate_games_name_fts(conn)

    assert 900001 in _appids(await service.search_catalog("只狼"))
    assert 900005 in _appids(await service.search_catalog("群星"))
