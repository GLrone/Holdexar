"""玩家标签链路：browse 解析、整批替换、中英文名底座与懒请求。"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.crawler import tag_names
from app.domains.games import tags as tags_service
from app.domains.games.models import Tag
from app.domains.games.tag_names_en import TAG_NAMES_EN
from app.domains.games.tag_names_zh import TAG_NAMES_ZH


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    import app.domains.games.models  # noqa: F401 —— 建表需要模型注册

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


class _FakeClient:
    """假 SteamHttpClient：只记录被请求的 URL，返回给定载荷或抛错。"""

    def __init__(self, payload=None, error=None):
        self.payload = payload
        self.error = error
        self.calls: list[str] = []

    async def get_json(self, session, url):
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        return self.payload


class _FakeCtx:
    def __init__(self, client):
        self.http_client = client
        self.session = object()


def test_item_tags_prefers_weighted_tags():
    """tags 优先（带票重），缺席时回落裸 tagids（票重记 0）。"""
    from app.crawler.browse_store import _item_tags

    assert _item_tags(
        {"tags": [{"tagid": 19, "weight": 1187}, {"tagid": 21, "weight": 791}]}
    ) == [(19, 1187), (21, 791)]
    assert _item_tags({"tagids": [19, 21]}) == [(19, 0), (21, 0)]
    assert _item_tags({"tags": [], "tagids": [122]}) == [(122, 0)]
    assert _item_tags(None) == []


@pytest.mark.asyncio
async def test_replace_game_tags_replaces_whole_set(db):
    """整批替换语义：Steam 摘掉的标签不能在库里留成幽灵行。"""
    await tags_service.replace_game_tags_batch(
        [(620, [(19, 100), (21, 50)]), (440, [(19, 300)])]
    )
    got = await tags_service.tags_by_appid([620, 440])
    assert got[620] == [
        {"tagid": 19, "name": None, "nameEn": None, "weight": 100},
        {"tagid": 21, "name": None, "nameEn": None, "weight": 50},
    ]
    assert got[440] == [{"tagid": 19, "name": None, "nameEn": None, "weight": 300}]

    await tags_service.replace_game_tags_batch([(620, [(19, 120)])])
    assert (await tags_service.tags_by_appid([620]))[620] == [
        {"tagid": 19, "name": None, "nameEn": None, "weight": 120}
    ]


@pytest.mark.asyncio
async def test_seed_names_idempotent(db):
    """内置底座幂等：首跑按中英并集补全表，二跑零写入。"""
    expected = len(TAG_NAMES_ZH.keys() | TAG_NAMES_EN.keys())
    assert await tags_service.seed_names() == expected
    assert await tags_service.seed_names() == 0


@pytest.mark.asyncio
async def test_en_backfill_and_coalesce(db):
    """存量行补英文名；冲突更新按列 COALESCE，两种语言独立补齐互不洗掉。"""
    # 模拟旧底座：只有中文名的存量行
    await tags_service.upsert_names({19: "动作"})
    await tags_service.seed_names()
    async with db() as session:
        row = (
            (await session.execute(select(Tag).where(Tag.tagid == 19))).scalars().one()
        )
    assert row.name_zh == "动作"
    assert row.name_en == TAG_NAMES_EN[19]

    # 两语言都未收录 → NULL 占位；后续单语言补名互不覆盖
    await tags_service.upsert_names({424242: None}, {424242: None})
    await tags_service.upsert_names({}, {424242: "Foo Tag"})
    async with db() as session:
        row = (
            (await session.execute(select(Tag).where(Tag.tagid == 424242)))
            .scalars()
            .one()
        )
    assert row.name_zh is None
    assert row.name_en == "Foo Tag"
    await tags_service.upsert_names({424242: "中文名"})
    async with db() as session:
        row = (
            (await session.execute(select(Tag).where(Tag.tagid == 424242)))
            .scalars()
            .one()
        )
    assert row.name_zh == "中文名"
    assert row.name_en == "Foo Tag"


@pytest.mark.asyncio
async def test_named_tags_skips_unmapped(db):
    """展示面只出至少一个语言对照到名字的标签；冷门标签留档但不进展示。"""
    await tags_service.seed_names()
    await tags_service.replace_game_tags_batch([(620, [(19, 100), (999999, 50)])])
    rows = (await tags_service.tags_by_appid([620]))[620]
    assert [r["tagid"] for r in rows] == [19, 999999]
    assert tags_service.named_tags(rows) == [
        {"tagid": 19, "name": "动作", "nameEn": "Action"}
    ]


@pytest.mark.asyncio
async def test_resolve_tag_names_fetch_placeholder_and_cooldown(db):
    """懒请求三段语义：命中写名 / 未收录写 NULL 占位 / 拉取失败不写占位且冷却。"""
    tag_names._next_attempt_at = 0.0
    await tags_service.seed_names()

    async def name_of(tagid: int):
        async with db() as session:
            return (
                await session.execute(select(Tag.name_zh).where(Tag.tagid == tagid))
            ).first()

    # ① 只缺底座之外的 tagid → 中英两表各拉一次，命中写名
    client = _FakeClient([{"tagid": 424242, "name": "新晋标签"}])
    ctx = _FakeCtx(client)
    assert await tag_names.resolve_tag_names(ctx, {19, 424242}) == 1
    assert client.calls == [tag_names.POPULAR_TAGS_URLS["name_zh"], tag_names.POPULAR_TAGS_URLS["name_en"]]
    assert (await name_of(424242))[0] == "新晋标签"

    # ② 拉取成功但表里没有 → NULL 占位；同一 id 再遇不再请求
    tag_names._next_attempt_at = 0.0
    client.calls.clear()
    assert await tag_names.resolve_tag_names(ctx, {555555}) == 1
    assert len(client.calls) == 2
    assert (await name_of(555555))[0] is None
    tag_names._next_attempt_at = 0.0
    client.calls.clear()
    assert await tag_names.resolve_tag_names(ctx, {555555}) == 0
    assert client.calls == []

    # ③ 拉取失败不写占位（通路问题 ≠ 未收录），且冷却期内不重复打
    bad = _FakeClient(error=RuntimeError("boom"))
    ctx_bad = _FakeCtx(bad)
    tag_names._next_attempt_at = 0.0
    assert await tag_names.resolve_tag_names(ctx_bad, {777777}) == 0
    assert len(bad.calls) == 1
    assert await tags_service.missing_tag_ids({777777}) == {777777}
    assert await tag_names.resolve_tag_names(ctx_bad, {777777}) == 0
    assert len(bad.calls) == 1
