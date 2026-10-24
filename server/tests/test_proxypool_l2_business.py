"""L2 业务探针：`StoreBrowseAPI.probe_url` 契约 + 现有响应分类。

分类沿用 health 既有语义：`BUSINESS_OK` / `TARGET_SERVICE_FAILED` /
`INVALID_BUSINESS_RESPONSE`——正常业务响应进 BUSINESS_OK，异常响应进这两类失败分类，
不新增状态。
"""
from __future__ import annotations

import json
from urllib.parse import quote

import pytest

from app.domains.proxypool import health as H


@pytest.mark.asyncio
@pytest.mark.parametrize("probe_name", ["probe_exit_ip", "probe_business"])
@pytest.mark.parametrize("selector", [None, "probe lane/1"])
@pytest.mark.parametrize("selected", ["1|probe", "other-node"])
async def test_probe_verifies_its_selector_before_request(
    monkeypatch, probe_name, selector, selected,
):
    control_calls = []
    outbound = []
    client_type = H.httpx.AsyncClient

    def control(request):
        control_calls.append((request.method, request.url.raw_path.decode()))
        if request.method == "PUT":
            assert json.loads(request.content) == {"name": "1|probe"}
            return H.httpx.Response(204)
        return H.httpx.Response(200, json={"now": selected})

    async def request(port, target, timeout):
        outbound.append((port, target))
        body = {"ip": "203.0.113.1"} if probe_name == "probe_exit_ip" else _VALID_PAYLOAD
        return 200, json.dumps(body), 7, ""

    monkeypatch.setattr(
        H.httpx, "AsyncClient",
        lambda **kw: client_type(transport=H.httpx.MockTransport(control), **kw),
    )
    monkeypatch.setattr(H, "_request_via_port", request)
    kwargs = {} if selector is None else {"selector": selector}
    result = await getattr(H, probe_name)(
        "http://controller", "secret", 41001, "1|probe",
        url="https://probe.invalid/test", **kwargs,
    )

    path = f"/proxies/{quote(selector or 'GLOBAL', safe='')}"
    assert control_calls == [("PUT", path), ("GET", path)]
    assert result.ok is (selected == "1|probe")
    assert outbound == ([(41001, "https://probe.invalid/test")] if result.ok else [])
    if not result.ok:
        assert result.detail.startswith(H.NODE_PROBE_FAILED)

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
    def __init__(
        self, status_code: int = 200, payload=None, non_json: bool = False,
        raw_text: str | None = None,
    ):
        self.status_code = status_code
        self._payload = payload
        self._non_json = non_json
        if raw_text is not None:
            self.text = raw_text
        else:
            self.text = "not json at all" if non_json else json.dumps(payload or {})

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

    monkeypatch.setattr(H, "_select_selector", _select_ok)
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


@pytest.mark.asyncio
async def test_gateway_502_with_failed_control_probe_is_node_tunnel_failed(monkeypatch):
    _patch(monkeypatch, resp=_Resp(502, raw_text=""))

    async def _control_fail(*a, **kw):
        return H.ProbeResult("1|probe", False, None, "内核探测失败（HTTP 503）", valid=True)

    monkeypatch.setattr(H, "probe_node", _control_fail)
    result = await _probe()
    assert result.ok is False
    assert result.http_status == 502
    assert result.attribution == H.ATTR_NODE
    assert result.detail.startswith(H.NODE_TUNNEL_FAILED)


@pytest.mark.asyncio
async def test_gateway_502_with_passing_control_probe_is_target_service_failed(monkeypatch):
    _patch(monkeypatch, resp=_Resp(502, raw_text=""))

    async def _control_ok(*a, **kw):
        return H.ProbeResult("1|probe", True, 18, "", valid=True)

    monkeypatch.setattr(H, "probe_node", _control_ok)
    result = await _probe()
    assert result.ok is False
    assert result.http_status == 502
    assert result.attribution == H.ATTR_TARGET
    assert result.detail.startswith(H.TARGET_SERVICE_FAILED)


@pytest.mark.asyncio
async def test_gateway_502_with_invalid_control_probe_is_uncertain_failure(monkeypatch):
    _patch(monkeypatch, resp=_Resp(502, raw_text=""))

    async def _control_invalid(*a, **kw):
        return H.ProbeResult("1|probe", False, None, "控制器请求异常：ConnectError", valid=False)

    monkeypatch.setattr(H, "probe_node", _control_invalid)
    result = await _probe()
    assert result.ok is False
    assert result.http_status == 502
    assert result.attribution == H.ATTR_UNCERTAIN
    assert result.detail.startswith(H.UNCERTAIN_FAILURE)
