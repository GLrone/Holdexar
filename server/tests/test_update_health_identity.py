import os

import httpx
import pytest
from fastapi import FastAPI

from app.domains.system.router import router


@pytest.mark.asyncio
@pytest.mark.parametrize("attempt_id", [None, "test-update-attempt"])
async def test_health_exposes_current_process_update_identity(monkeypatch, attempt_id):
    if attempt_id is None:
        monkeypatch.delenv("HOLDEXAR_UPDATE_ATTEMPT_ID", raising=False)
    else:
        monkeypatch.setenv("HOLDEXAR_UPDATE_ATTEMPT_ID", attempt_id)
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.get("/api/v1/health")

    assert response.status_code == 200
    data = response.json()
    assert data["pid"] == os.getpid()
    if attempt_id is None:
        assert "updateAttemptId" not in data
    else:
        assert data["updateAttemptId"] == attempt_id
