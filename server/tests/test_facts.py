"""notifications 域事实通知：幂等落行、增量读取、Epic 当期集合比对。

offers fixture 为手工构造的最小 payload（appid/标题/upcoming 三形态），
不粘真实响应；HB 侧挂钩在 refresh_hb_choice 的结案分支内（网络链），
落行语义与 Epic 同走 record_fact，由本文件的幂等/增量用例覆盖。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module
from app.domains.metadata import service as metadata_service
from app.domains.notifications import facts as facts_service
from app.domains.notifications.models import FactNotice  # noqa: F401


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(facts_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(metadata_service, "get_session_factory", lambda: factory)
    async with engine.begin() as conn:
        await conn.run_sync(database_module.Base.metadata.create_all)
    yield factory
    await engine.dispose()


async def _all_rows(factory) -> list[FactNotice]:
    async with factory() as session:
        return list(
            (await session.execute(select(FactNotice).order_by(FactNotice.id))).scalars().all()
        )


# ── record_fact：幂等与字段 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_record_fact_idempotent_by_key(db):
    assert await facts_service.record_fact(
        "hb_choice", "bundle_changed", "hb_choice:september_2026_choice",
        {"label": "HB慈善包26年9月包", "count": 8},
    )
    # 同一事实重复进入（调度重跑 / 手动触发）不落第二行
    assert not await facts_service.record_fact(
        "hb_choice", "bundle_changed", "hb_choice:september_2026_choice",
        {"label": "HB慈善包26年9月包", "count": 8},
    )
    rows = await _all_rows(db)
    assert len(rows) == 1
    assert rows[0].source == "hb_choice"
    assert rows[0].data["count"] == 8


@pytest.mark.asyncio
async def test_record_fact_distinct_keys(db):
    await facts_service.record_fact("epic_free", "free_rotation", "epic_free:appid:1")
    await facts_service.record_fact("epic_free", "free_rotation", "epic_free:appid:2")
    rows = await _all_rows(db)
    assert [r.fact_key for r in rows] == ["epic_free:appid:1", "epic_free:appid:2"]


# ── list_facts：对齐与增量 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_facts_align_then_increment(db):
    aligned = await facts_service.list_facts()
    assert aligned == {"latestId": 0, "items": []}

    for i in (1, 2):
        await facts_service.record_fact("epic_free", "free_rotation", f"epic_free:appid:{i}")

    # 缺省 afterId = 只对齐：不回放历史
    aligned = await facts_service.list_facts()
    assert aligned["latestId"] == 2 and aligned["items"] == []

    after_first = await facts_service.list_facts(after_id=1)
    assert [i["id"] for i in after_first["items"]] == [2]

    await facts_service.record_fact("epic_free", "free_rotation", "epic_free:appid:3")
    fresh = await facts_service.list_facts(after_id=2)
    assert [i["id"] for i in fresh["items"]] == [3]
    assert fresh["items"][0]["source"] == "epic_free"


# ── Epic 当期集合比对 ────────────────────────────────────────────────────


def _payload(*offers: dict) -> dict:
    return {"source": "epic-offers", "offers": list(offers)}


def test_current_offer_identities_filters_upcoming_and_blank():
    offers = metadata_service._current_offer_identities(
        _payload(
            {"appid": 1, "title": "A"},
            {"appid": 2, "title": "Upcoming", "upcoming": True},
            {"title": "  "},
            {"appid": "3", "title": "C"},
        )
    )
    assert offers == {"appid:1", "appid:3"}


def test_offer_identity_appid_beats_title():
    assert metadata_service._offer_identity({"appid": 7, "title": "X"}) == "appid:7"
    assert metadata_service._offer_identity({"title": "Only Name"}) == "title:only name"
    assert metadata_service._offer_identity({"title": ""}) is None


@pytest.mark.asyncio
async def test_publish_rotation_new_items_only(db):
    old = _payload({"appid": 1, "title": "A"}, {"appid": 2, "title": "B"})
    # 首份快照（无「之前」）不落行
    await metadata_service._publish_epic_rotation(None, _payload({"appid": 1, "title": "A"}))
    assert await _all_rows(db) == []

    # 集合未变不落行
    await metadata_service._publish_epic_rotation(old, _payload({"appid": 1, "title": "A"}, {"appid": 2, "title": "B"}))
    assert await _all_rows(db) == []

    # 新增条目落行；预告段不算新增
    new = _payload(
        {"appid": 1, "title": "A"},
        {"appid": 2, "title": "B"},
        {"appid": 3, "title": "C", "titleCn": "三号游戏"},
        {"appid": 9, "title": "Soon", "upcoming": True},
    )
    await metadata_service._publish_epic_rotation(old, new)
    rows = await _all_rows(db)
    assert len(rows) == 1
    assert rows[0].source == "epic_free" and rows[0].kind == "free_rotation"
    assert rows[0].data["count"] == 1
    assert rows[0].data["titles"] == ["三号游戏"]
    assert "appid:3" in rows[0].fact_key

    # 同一批新增重复刷新（fact_key 相同）不落第二行
    await metadata_service._publish_epic_rotation(old, new)
    assert len(await _all_rows(db)) == 1
