from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import scheduler
from app.domains.settings.service import set_value
from app.domains.system.router import router


@pytest.mark.asyncio
async def test_http_does_not_claim_scheduler_or_collection_readiness(monkeypatch):
    monkeypatch.setattr(scheduler, "scheduler", SimpleNamespace(running=False))
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        health = (await client.get("/api/v1/health")).json()
        assert health["status"] == "ok" and health["schedulerReady"] is False
        ready = (await client.get("/api/v1/system/readiness")).json()
        assert ready["httpReady"] is True
        assert ready["schedulerReady"] is False and ready["collectionReady"] is False
        assert ready["transportReady"] is False and ready["lanes"] == 0


@pytest.mark.asyncio
async def test_collection_respects_upgrade_protection_and_explicit_transport(monkeypatch):
    monkeypatch.setattr(scheduler, "scheduler", SimpleNamespace(running=True))
    await set_value("proxy.strategy", "direct_only")
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.update_verification = {"protected": True}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as client:
        ready = (await client.get("/api/v1/system/readiness")).json()
        assert ready["schedulerReady"] is True and ready["transportReady"] is True
        assert ready["collectionReady"] is False
        app.state.update_verification["protected"] = False
        ready = (await client.get("/api/v1/system/readiness")).json()
        assert ready["collectionReady"] is True
