"""L2 业务探针：`StoreBrowseAPI.probe_url` 契约 + 现有响应分类。

分类沿用 health 既有语义：`BUSINESS_OK` / `TARGET_SERVICE_FAILED` /
`INVALID_BUSINESS_RESPONSE`——正常业务响应进 BUSINESS_OK，异常响应进这两类失败分类，
不新增状态。
"""
from __future__ import annotations

import pytest

from app.domains.proxypool import health as H

_VALID_PAYLOAD = {
    "response": {
        "store_items": [
            {"appid": 220, "success": 1,
             "best_purchase_option": {"final_price_in_cents": 1234}},
        ]
    }
}
_SERVICE_FAILED_PAYLOAD = {
    "response": {"store_items": [{"appid": 220, "success": 15}]}
}
_BAD_ENVELOPE_PAYLOAD = {"response": {}}


def test_probe_url_same_host_and_path_as_production():
    """探针与生产主链路同主机、同路径、同编码；单 appid、不带 extras。"""
    url = H.business_probe_url(220)
    assert url.startswith("https://api.steampowered.com/IStoreBrowseService/GetItems/v1/")
    assert "input_json" in url
    assert "220" in url
    assert "include_assets" not in url and "include_reviews" not in url


class _Resp:
    def __init__(self, status_code: int = 200, payload=None, non_json: bool = False):
        self.status_code = status_code
        self._payload = payload
        self._non_json = non_json

    def json(self):
        if self._non_json:
            raise ValueError("not json")
        return self._payload


class _Client:
    def __init__(self, resp: _Resp | None = None, exc: Exception | None = None, **kw):
        self._resp = resp
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def get(self, url: str):
        if self._exc is not None:
            raise self._exc
        return self._resp


def _patch(monkeypatch, *, resp=None, exc=None) -> None:
    async def _select_ok(*a, **kw):
        return None  # 选中成功

    monkeypatch.setattr(H, "_select_global", _select_ok)
    monkeypatch.setattr(H.httpx, "AsyncClient", lambda **kw: _Client(resp, exc))


async def _probe() -> H.BusinessResult:
    return await H.probe_business(
        "http://127.0.0.1:9", "secret", 7890, "1|probe", appid=220, timeout=1.0
    )


@pytest.mark.asyncio
async def test_valid_business_response_is_ok(monkeypatch):
    _patch(monkeypatch, resp=_Resp(200, _VALID_PAYLOAD))
    result = await _probe()
    assert result.ok is True
    assert result.detail.startswith(H.BUSINESS_OK)


@pytest.mark.asyncio
async def test_service_failure_payload_is_invalid_business_response(monkeypatch):
    _patch(monkeypatch, resp=_Resp(200, _SERVICE_FAILED_PAYLOAD))
    result = await _probe()
    assert result.ok is False
    assert result.detail.startswith(H.INVALID_BUSINESS_RESPONSE)


@pytest.mark.asyncio
async def test_bad_envelope_is_invalid_business_response(monkeypatch):
    _patch(monkeypatch, resp=_Resp(200, _BAD_ENVELOPE_PAYLOAD))
    result = await _probe()
    assert result.ok is False
    assert result.detail.startswith(H.INVALID_BUSINESS_RESPONSE)


@pytest.mark.asyncio
async def test_non_json_body_is_invalid_business_response(monkeypatch):
    _patch(monkeypatch, resp=_Resp(200, non_json=True))
    result = await _probe()
    assert result.ok is False
    assert result.detail.startswith(H.INVALID_BUSINESS_RESPONSE)


@pytest.mark.asyncio
async def test_http_error_is_target_service_failed(monkeypatch):
    _patch(monkeypatch, resp=_Resp(500))
    result = await _probe()
    assert result.ok is False
    assert result.detail.startswith(H.TARGET_SERVICE_FAILED)


@pytest.mark.asyncio
async def test_transport_error_is_target_service_failed(monkeypatch):
    _patch(monkeypatch, exc=ConnectionError("boom"))
    result = await _probe()
    assert result.ok is False
    assert result.detail.startswith(H.TARGET_SERVICE_FAILED)