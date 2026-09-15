"""games 域路由层参数校验测试：锁国区 region=locked 的 HTTP 层回归。

背景：service 直调测试（test_games_query 的 region="LOCKED"）全绿，但 router
的 Query(max_length=5) 把 6 字符的 "locked" 拒成 422——直调绕过校验层，
这类回归只有真实 HTTP 调用能拦住。本文件只挂 games 路由的最小 FastAPI 应用
（不起完整 app.main 避免 lifespan 副作用），service 以桩替换，零 DB 依赖。
"""
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.games import service as games_service
from app.domains.games.router import router as games_router


@pytest.fixture
def client(monkeypatch):
    """最小应用：只挂 games 路由；service.list_games 桩返回空页。"""
    async def _stub(**kw):
        return {"items": [], "total": 0, "hasMore": False, "nextCursor": None}

    monkeypatch.setattr(games_service, "list_games", _stub)
    app = FastAPI()
    app.include_router(games_router, prefix="/api/v1")
    return TestClient(app)


def test_region_locked_passes_query_validation(client):
    """锁国区选项发送 region=locked（6 字符）必须通过 max_length 校验。

    回归防线：值曾为 5，前端锁国区按键因此恒 422、页面空报错。
    """
    r = client.get("/api/v1/games", params={"region": "locked"})
    assert r.status_code == 200, r.text


def test_normal_region_still_validated(client):
    """常规区码（2 字符）照常 200；越界长值（7 字符）仍被 422 拒。"""
    assert client.get("/api/v1/games", params={"region": "us"}).status_code == 200
    too_long = client.get("/api/v1/games", params={"region": "lockedx"})
    assert too_long.status_code == 422
