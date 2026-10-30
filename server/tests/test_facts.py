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
from app.domains.notifications import service as notifications_service
from app.domains.notifications.models import FactNotice  # noqa: F401


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()


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
    assert rows[0].data["appid"] == 3
    assert "appid:3" in rows[0].fact_key

    # 同一批新增重复刷新（fact_key 相同）不落第二行
    await metadata_service._publish_epic_rotation(old, new)
    assert len(await _all_rows(db)) == 1


@pytest.mark.asyncio
async def test_publish_rotation_omits_cover_appid_for_multiple_games(db):
    old = _payload({"appid": 1, "title": "A"})
    new = _payload(
        {"appid": 1, "title": "A"},
        {"appid": 2, "title": "B"},
        {"appid": 3, "title": "C"},
    )

    await metadata_service._publish_epic_rotation(old, new)

    rows = await _all_rows(db)
    assert len(rows) == 1
    assert "appid" not in rows[0].data


@pytest.mark.asyncio
async def test_new_content_fact_enqueues_actual_identity_and_frozen_source_once(db, monkeypatch):
    calls = []
    async def capture(source_kind, facts, *, session):
        fact = facts[0]
        stored = await session.get(FactNotice, fact["id"])
        assert stored is not None
        assert stored.fact_key == "epic-free-captured"
        calls.append((source_kind, facts))
    monkeypatch.setattr(notifications_service, "enqueue_facts", capture, raising=False)
    data = {"appid": 7, "count": 1, "items": [{"name": "Example", "url": "https://store.epicgames.com/p/example",
            "end": "2026-11-01T15:00:00Z"}]}
    assert await facts_service.record_fact("epic_free", "free_rotation", "epic-free-captured", data)
    assert not await facts_service.record_fact("epic_free", "free_rotation", "epic-free-captured", data)
    assert len(calls) == 1
    kind, facts = calls[0]
    stored = (await _all_rows(db))[0]
    assert kind == "fact_notice"
    fact = facts[0]
    assert fact["id"] == stored.id
    assert fact["appid"] == 7 and fact["region"] is None
    assert fact["eventType"] == "EPIC_FREE" and fact["category"] == "content"
    assert fact["payload"]["items"] == data["items"]
    assert fact["payload"]["source"] == "epic_free"
    assert fact["payload"]["observedAt"] == stored.occurred_at.isoformat()
    assert "price" not in fact["payload"]["items"][0]


@pytest.mark.asyncio
async def test_fact_and_qualification_rollback_together(db, monkeypatch):
    async def fail(*args, **kwargs):
        raise RuntimeError("qualification unavailable")
    monkeypatch.setattr(notifications_service, "enqueue_facts", fail, raising=False)
    assert not await facts_service.record_fact("hb_choice", "bundle_changed", "atomic-content", {"count": 2})
    assert await _all_rows(db) == []


@pytest.mark.asyncio
async def test_unrelated_fact_source_is_not_reinterpreted_as_content_mail(db, monkeypatch):
    calls = []
    async def capture(*args, **kwargs):
        calls.append(args)
    monkeypatch.setattr(notifications_service, "enqueue_facts", capture, raising=False)
    assert await facts_service.record_fact("achievements", "level_up", "level:2", {"level": 2})
    assert calls == []


@pytest.mark.asyncio
async def test_epic_fact_preserves_original_names_official_link_and_period(db):
    offer = {"appid": 7, "title": "Example", "titleCn": "示例", "url": "https://store.epicgames.com/p/example",
             "image": "https://cdn.example.org/cover.jpg", "start": "2026-10-29", "end": "2026-11-05"}
    await metadata_service._publish_epic_rotation(_payload({"appid": 1, "title": "Previous"}), _payload(offer))
    fact = (await _all_rows(db))[0]
    item = fact.data["items"][0]
    assert item["name"] == "示例"
    assert item["url"] == offer["url"] and item["end"] == offer["end"]
    assert item["start"] == offer["start"]
    assert "price" not in item
