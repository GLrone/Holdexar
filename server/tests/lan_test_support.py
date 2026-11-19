from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core import access, lan_access
from app.domains.system.router import router as system_router
from app.domains.system.session_router import reset_bootstrap, router as session_router

LAN_IP = "192.168.1.5"
LAN_PEER_IP = "192.168.1.25"
LAN_HOST = f"{LAN_IP}:28765"
LOOPBACK_IP = "127.0.0.1"


@pytest.fixture(autouse=True)
def isolate_lan_state(monkeypatch):
    monkeypatch.delenv(lan_access.ENV_ACCESS, raising=False)
    monkeypatch.delenv(lan_access.ENV_ACCESS_PASSWORD, raising=False)
    access.reset_token()
    reset_bootstrap()
    lan_access.reset_unlock_state()
    lan_access.set_enabled(False)
    access.set_extra_hosts(())
    yield
    access.reset_token()
    reset_bootstrap()
    lan_access.reset_unlock_state()
    lan_access.set_enabled(False)
    access.set_extra_hosts(())


def access_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(access.LocalAccessMiddleware)
    app.include_router(system_router, prefix="/api/v1")
    app.include_router(session_router, prefix="/api/v1")
    return app


def peer_client(app: FastAPI, *, peer_ip: str, host: str) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=app, client=(peer_ip, 43210)),
        base_url=f"http://{host}",
    )


def enable_lan() -> None:
    lan_access.set_enabled(True)
    access.set_extra_hosts([*lan_access.local_ips(), LAN_IP])


def attack_cases() -> list[dict[str, object]]:
    contract = Path(__file__).resolve().parents[2] / "tests" / "contracts" / "lan_access.json"
    return json.loads(contract.read_text(encoding="utf-8"))["cases"]
