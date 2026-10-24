"""proxypool 体检台账读口（`GET /proxypool/health-runs`）：行为测试。

只验读口：camelCase 投影、`NULL` 保持 null、started_at 降序（同刻按 id
降序）、limit 边界。台账行的写入语义归 test_proxypool_health*.py。
隔离：tmp_data_dir 模式（同域其它测试）——临时数据目录 + 建表 + 缓存收尾。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.core.database import get_session_factory, init_db
from app.domains.proxypool.models import HealthRun

NOW = datetime(2026, 10, 12, 12, 0, 0)

_RUN_KEYS = {
    "id", "channel", "subscriptionId", "startedAt", "finishedAt", "durationMs",
    "total", "steamOk", "ipKnown", "failed", "endReason",
}


async def _seed_rows(session):
    # id 1..4 顺序落库；C/D 同刻启动，验证同刻按 id 降序的次序稳定
    session.add_all([
        HealthRun(  # 最旧：正常收尾行
            channel="scheduled", started_at=NOW - timedelta(hours=3),
            finished_at=NOW - timedelta(hours=3) + timedelta(minutes=50),
            duration_ms=3_000_000, total=12, steam_ok=9, ip_known=11, failed=1,
        ),
        HealthRun(  # 启动链收尸行
            channel="scheduled", started_at=NOW - timedelta(hours=2),
            finished_at=NOW - timedelta(hours=2) + timedelta(minutes=10),
            duration_ms=600_000, total=12, steam_ok=0, ip_known=2, failed=10,
            end_reason="interrupted",
        ),
        HealthRun(  # manual 行 + 在跑行（无收尾事实）
            channel="manual", subscription_id=7, started_at=NOW,
            total=12,
        ),
        HealthRun(  # 最新一行（与 manual 同刻）
            channel="scheduled", started_at=NOW,
            finished_at=NOW + timedelta(minutes=30), duration_ms=1_800_000,
            total=12, steam_ok=12, ip_known=12, failed=0,
        ),
    ])
    await session.commit()


@pytest.mark.asyncio
async def test_health_runs_shape_order_and_limit(tmp_data_dir):
    """HTTP 层：形状（camelCase / ISO / null 保持）/ 排序 / limit。"""
    import httpx
    from fastapi import FastAPI

    from app.domains.proxypool.router import router as proxypool_router

    await init_db()
    async with get_session_factory()() as session:
        await _seed_rows(session)

    app = FastAPI()
    app.include_router(proxypool_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/proxypool/health-runs")
        assert r.status_code == 200, r.text
        runs = r.json()["runs"]
        # started_at 降序，同刻按 id 降序
        assert [row["id"] for row in runs] == [4, 3, 2, 1]

        newest, manual, interrupted, oldest = runs
        assert set(newest) == _RUN_KEYS
        assert newest["channel"] == "scheduled"
        assert newest["startedAt"] == NOW.isoformat()
        assert newest["finishedAt"] == (NOW + timedelta(minutes=30)).isoformat()
        assert newest["steamOk"] == 12 and newest["endReason"] is None

        # manual 行：订阅身份保留；在跑行无收尾事实 → null 保持 null
        assert manual["channel"] == "manual"
        assert manual["subscriptionId"] == 7
        assert manual["finishedAt"] is None
        assert manual["durationMs"] is None
        assert manual["endReason"] is None

        # 启动链收尸行的收尾原因原样透出
        assert interrupted["endReason"] == "interrupted"
        assert interrupted["failed"] == 10
        # scheduled 行无订阅身份
        assert oldest["subscriptionId"] is None
        assert oldest["durationMs"] == 3_000_000

        # limit：只取最新 N 行
        r2 = await client.get("/api/v1/proxypool/health-runs", params={"limit": 2})
        assert [row["id"] for row in r2.json()["runs"]] == [4, 3]

        # 边界拒绝
        assert (
            await client.get("/api/v1/proxypool/health-runs", params={"limit": 0})
        ).status_code == 422
        assert (
            await client.get("/api/v1/proxypool/health-runs", params={"limit": 51})
        ).status_code == 422


@pytest.mark.asyncio
async def test_health_runs_empty_ledger(tmp_data_dir):
    await init_db()
    import httpx
    from fastapi import FastAPI

    from app.domains.proxypool.router import router as proxypool_router

    app = FastAPI()
    app.include_router(proxypool_router, prefix="/api/v1")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        r = await client.get("/api/v1/proxypool/health-runs")
        assert r.status_code == 200
        assert r.json() == {"runs": []}
