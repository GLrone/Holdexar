"""pilot LLM 适配层单测：协议请求构造、鉴权头与探测失败分类（不触网）。"""
import httpx
import pytest

from app.domains.pilot import llm as pilot_llm
from app.domains.pilot import router as pilot_router
from app.domains.pilot.schemas import DetectRequest


def test_anonymous_endpoints_send_no_auth_header():
    """免费匿名端点 / 本地网关无密钥：空值鉴权头会被 httpx 拒绝。"""
    assert pilot_llm._auth_headers("openai", "") == {}
    assert pilot_llm._auth_headers("ollama", "") == {}
    assert pilot_llm._auth_headers("ollama", "unused") == {}
    assert pilot_llm._auth_headers("anthropic", "") == {"anthropic-version": "2023-06-01"}


def test_keyed_endpoints_send_auth_header():
    assert pilot_llm._auth_headers("openai", "sk-x") == {"Authorization": "Bearer sk-x"}
    assert pilot_llm._auth_headers("anthropic", "sk-x") == {
        "x-api-key": "sk-x",
        "anthropic-version": "2023-06-01",
    }


def test_request_builders_use_auth_headers():
    url, headers, _ = pilot_llm._openai_request("https://x/v1", "", "m", [], None)
    assert url == "https://x/v1/chat/completions"
    assert "Authorization" not in headers
    _, headers, _ = pilot_llm._openai_request("https://x/v1", "sk-x", "m", [], None)
    assert headers["Authorization"] == "Bearer sk-x"
    _, headers, _ = pilot_llm._anthropic_request("https://x", "sk-x", "m", [], None)
    assert headers["x-api-key"] == "sk-x"


def test_list_models_url_per_protocol():
    assert pilot_llm._list_models_url("openai", "https://x/v1") == "https://x/v1/models"
    assert pilot_llm._list_models_url("anthropic", "https://x") == "https://x/v1/models"
    assert pilot_llm._list_models_url("ollama", "http://127.0.0.1:11434") == "http://127.0.0.1:11434/api/tags"


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x/v1/models")
    resp = httpx.Response(code, request=req)
    return httpx.HTTPStatusError(f"HTTP {code}", request=req, response=resp)


async def _stub_list_models(outcome):
    async def _fake(protocol, base_url, api_key):
        if outcome == "ok":
            return ["m1", "m2"]
        if isinstance(outcome, int):
            raise _status_error(outcome)
        raise httpx.ConnectError("refused", request=httpx.Request("GET", "https://x/v1/models"))
    return _fake


@pytest.mark.asyncio
async def test_detect_reason_success(monkeypatch):
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models("ok"))
    r = await pilot_llm.detect_provider("https://x/v1", "sk-x")
    assert r["key_valid"] is True
    assert r["reason"] is None
    assert r["models"] == ["m1", "m2"]


@pytest.mark.asyncio
async def test_detect_reason_key_invalid(monkeypatch):
    for code in (401, 403):
        monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models(code))
        r = await pilot_llm.detect_provider("https://x/v1", "bad")
        assert r["key_valid"] is False
        assert r["reason"] == "key_invalid"


@pytest.mark.asyncio
async def test_detect_reason_not_found(monkeypatch):
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models(404))
    r = await pilot_llm.detect_provider("https://x", "sk-x")
    assert r["key_valid"] is None
    assert r["reason"] == "not_found"


@pytest.mark.asyncio
async def test_detect_reason_upstream_and_unreachable(monkeypatch):
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models(503))
    r = await pilot_llm.detect_provider("https://x/v1", "sk-x")
    assert r["reason"] == "upstream"
    monkeypatch.setattr(pilot_llm, "list_models", await _stub_list_models("net"))
    r = await pilot_llm.detect_provider("https://x/v1", "sk-x")
    assert r["reason"] == "unreachable"
    assert r["key_valid"] is None


@pytest.mark.asyncio
async def test_detect_uses_provider_key_not_active(monkeypatch):
    """编辑态识别：带 provider_id 用该供应商密钥；无 provider_id 才回落活跃配置。"""
    captured: dict = {}

    async def fake_provider_key(pid: str) -> str:
        return {"p-1": "sk-p1"}.get(pid, "")

    async def fake_load_config() -> dict:
        return {"api_key": "sk-active"}

    async def fake_detect(base_url: str, api_key: str, protocol: str | None) -> dict:
        captured["key"] = api_key
        return {"protocol": "openai", "vendor": "", "models": ["m"], "suggested": [],
                "key_valid": True, "reason": None}

    monkeypatch.setattr(pilot_router.pilot_config, "provider_key", fake_provider_key)
    monkeypatch.setattr(pilot_router.pilot_config, "load_config", fake_load_config)
    monkeypatch.setattr(pilot_router.pilot_llm, "detect_provider", fake_detect)

    r = await pilot_router.detect(DetectRequest(base_url="https://x/v1", provider_id="p-1"))
    assert captured["key"] == "sk-p1"
    await pilot_router.detect(DetectRequest(base_url="https://x/v1"))
    assert captured["key"] == "sk-active"
    # 供应商无密钥（匿名网关）或 id 不存在：保持匿名，不跨家回落活跃密钥
    await pilot_router.detect(DetectRequest(base_url="https://x/v1", provider_id="ghost"))
    assert captured["key"] == ""
    assert r.models == ["m"]
    assert r.reason is None
