"""长期记忆单元测试：CRUD、偏好提取、系统提示装配与 API 端点。"""
from __future__ import annotations

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.database import Base
from app.domains.agent import memory as agent_memory
from app.domains.agent.models import AgentMemory
from app.domains.agent.router import router as agent_router
from app.domains.pilot import service as pilot_service
from app.domains.pilot import tools as pilot_tools


@pytest.fixture
def db(tmp_path, monkeypatch):
    """独立临时库，替换 session 工厂。"""
    import app.core.database as database_module

    db_file = tmp_path / "memory_test.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file.as_posix()}", echo=False)

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(agent_memory, "get_session_factory", lambda: factory)
    yield {"factory": factory, "url": f"sqlite+aiosqlite:///{db_file.as_posix()}"}


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    """建 agent_memories 表。"""
    engine = create_async_engine(db["url"])
    try:
        async with engine.begin() as conn:
            await conn.run_sync(
                Base.metadata.create_all,
                [AgentMemory.__table__],
            )
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_upsert_and_get_memory(db):
    item = await agent_memory.upsert_memory(
        category="region_preference",
        key="preferred_regions",
        value=["AR", "TR"],
        confidence=0.9,
        source="test",
    )
    assert item["key"] == "preferred_regions"
    assert item["category"] == "region_preference"
    assert item["value"] == {"val": ["AR", "TR"]}
    assert item["confidence"] == 0.9

    got = await agent_memory.get_memory("region_preference", "preferred_regions")
    assert got is not None
    assert got["value"] == {"val": ["AR", "TR"]}


@pytest.mark.asyncio
async def test_upsert_overwrite(db):
    await agent_memory.upsert_memory("budget_habit", "max_cny", 50, confidence=0.7)
    updated = await agent_memory.upsert_memory("budget_habit", "max_cny", 80, confidence=1.0)
    assert updated["value"] == {"val": 80}
    assert updated["confidence"] == 1.0

    items = await agent_memory.list_memories("budget_habit")
    assert len(items) == 1
    assert items[0]["value"] == {"val": 80}


@pytest.mark.asyncio
async def test_list_and_delete_memories(db):
    await agent_memory.upsert_memory("genre_preference", "liked", ["肉鸽", "魂类"])
    await agent_memory.upsert_memory("region_preference", "preferred_regions", ["CN"])

    all_items = await agent_memory.list_memories()
    assert len(all_items) == 2

    genre_items = await agent_memory.list_memories(category="genre_preference")
    assert len(genre_items) == 1
    assert genre_items[0]["key"] == "liked"

    ok = await agent_memory.delete_memory("genre_preference", "liked")
    assert ok is True
    assert await agent_memory.get_memory("genre_preference", "liked") is None

    cleared = await agent_memory.clear_memories()
    assert cleared == 1
    assert len(await agent_memory.list_memories()) == 0


@pytest.mark.asyncio
async def test_build_profile_context(db):
    assert await agent_memory.build_profile_context() is None

    await agent_memory.upsert_memory("region_preference", "preferred_regions", ["AR", "TR"], confidence=0.9)
    await agent_memory.upsert_memory("budget_habit", "deal_timing", "史低才买", confidence=0.8)
    await agent_memory.upsert_memory("low_conf", "test", "ignore", confidence=0.2)

    prompt = await agent_memory.build_profile_context(min_confidence=0.5)
    assert prompt is not None
    assert "常用关注区服" in prompt
    assert "AR, TR" in prompt
    assert "价格与预算习惯" in prompt
    assert "史低才买" in prompt
    assert "ignore" not in prompt


@pytest.mark.asyncio
async def test_extract_preferences_from_text(db):
    text = "我平时主要在阿根廷区和土耳其区买游戏，很喜欢肉鸽和魂类，只买史低，预算不超过100块。"
    extracted = await agent_memory.extract_preferences_from_text(text)
    assert len(extracted) >= 3

    regions = await agent_memory.get_memory("region_preference", "preferred_regions")
    assert regions is not None
    assert "AR" in regions["value"]["val"]
    assert "TR" in regions["value"]["val"]

    genres = await agent_memory.get_memory("genre_preference", "preferred_genres")
    assert genres is not None
    assert "肉鸽" in genres["value"]["val"]
    assert "魂类" in genres["value"]["val"]

    deal = await agent_memory.get_memory("budget_habit", "deal_timing")
    assert deal is not None

    budget = await agent_memory.get_memory("budget_habit", "max_budget_cny")
    assert budget is not None
    assert budget["value"] == {"val": 100}


@pytest.mark.asyncio
async def test_pilot_tools_preference_integration(db):
    set_res = await pilot_tools.execute_tool(
        "set_user_preference",
        {"category": "genre_preference", "key": "favorite", "value": "Roguelike"},
    )
    assert set_res["kind"] == "user_preference_saved"
    assert set_res["item"]["key"] == "favorite"

    get_res = await pilot_tools.execute_tool("get_user_preferences", {})
    assert get_res["kind"] == "user_preferences"
    assert get_res["count"] == 1
    assert get_res["items"][0]["key"] == "favorite"


@pytest.mark.asyncio
async def test_service_system_prompt_with_profile(db):
    await agent_memory.upsert_memory("region_preference", "preferred_regions", ["CN", "JP"])
    sys_prompt = await pilot_service._build_system_prompt()
    assert "【用户已知长期偏好】" in sys_prompt
    assert "CN, JP" in sys_prompt


@pytest.mark.asyncio
async def test_memory_router_endpoints(db):
    app = FastAPI()
    app.include_router(agent_router, prefix="/api/v1")

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 1. 初始列表为空
        r = await client.get("/api/v1/agent/memories")
        assert r.status_code == 200
        assert r.json()["items"] == []

        # 2. 创建记忆
        payload = {
            "category": "budget_habit",
            "key": "max_cny",
            "value": 150,
            "confidence": 0.95,
        }
        r = await client.post("/api/v1/agent/memories", json=payload)
        assert r.status_code == 200
        assert r.json()["key"] == "max_cny"

        # 3. 查列表
        r = await client.get("/api/v1/agent/memories?category=budget_habit")
        assert r.status_code == 200
        assert len(r.json()["items"]) == 1

        # 4. 删除单条
        r = await client.delete("/api/v1/agent/memories/budget_habit/max_cny")
        assert r.status_code == 200
        assert r.json()["ok"] is True

        # 5. 再查为空
        r = await client.get("/api/v1/agent/memories")
        assert len(r.json()["items"]) == 0
