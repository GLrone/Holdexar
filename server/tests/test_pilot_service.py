"""pilot 意图路由与问答编排单测（LLM 以替身注入，不触网、不依赖真实库）。"""
import pytest

from app.domains.pilot import config as pilot_config
from app.domains.pilot import intent as pilot_intent
from app.domains.pilot import service
from app.domains.pilot.llm import PilotLlmError

_CFG = {
    "enabled": True,
    "base_url": "http://127.0.0.1:9/v1",
    "model": "test-model",
    "api_key": "sk-test",
    "monthly_cap": 1000,
}
_FACTS = {
    "kind": "price",
    "appid": 1,
    "name": "测试游戏",
    "positiveRate": 0.9,
    "reviewCount": 100,
    "cn": {"cnyFen": 5000, "discount": 50},
    "lowest": {"cnyFen": 4000, "snapshotAt": "2026-01-01T00:00:00"},
    "year": {"minFen": 4000, "maxFen": 12000, "medianFen": 8000, "count": 50},
}


@pytest.fixture(autouse=True)
def _clear_cache():
    service._cache.clear()
    yield
    service._cache.clear()


def _patch_ready(monkeypatch, *, cfg=None, usage=0, fail=False, calls=None):
    monkeypatch.setattr(pilot_config, "load_config", _async_return(cfg or _CFG))
    monkeypatch.setattr(pilot_config, "usage_month", _async_return(usage))
    monkeypatch.setattr(
        pilot_config, "add_usage", _record_usage(calls if calls is not None else {})
    )
    if fail:
        async def _boom(**kw):
            raise PilotLlmError("boom")
        monkeypatch.setattr(service.pilot_llm, "chat_complete", _boom)
    else:
        async def _ok(**kw):
            return "LLM 回答", 100, 20
        monkeypatch.setattr(service.pilot_llm, "chat_complete", _ok)


def _async_return(value):
    async def _fn(*a, **kw):
        return value
    return _fn


def _record_usage(store):
    async def _fn(tokens):
        store["tokens"] = store.get("tokens", 0) + tokens
    return _fn


class TestIntentRoute:
    def test_how_to(self):
        assert pilot_intent.route("把双人成行加进关注") == pilot_intent.HOW_TO
        assert pilot_intent.route("星露谷物语低于 48 块提醒我") == pilot_intent.HOW_TO

    def test_price(self):
        assert pilot_intent.route("赛博朋克2077 现在史低多少") == pilot_intent.PRICE_ANALYSIS
        assert pilot_intent.route("这游戏值不值得入手") == pilot_intent.PRICE_ANALYSIS

    def test_find(self):
        assert pilot_intent.route("推荐两个适合双人玩的游戏") == pilot_intent.FIND_GAMES

    def test_chat_fallback(self):
        assert pilot_intent.route("今天天气怎么样") == pilot_intent.CHAT


class TestAskFlow:
    @pytest.mark.asyncio
    async def test_llm_answer_and_cache(self, monkeypatch):
        calls = {}
        _patch_ready(monkeypatch, calls=calls)
        monkeypatch.setattr(
            service.pilot_tools, "price_facts", _async_return(dict(_FACTS))
        )
        resp = await service.ask("这游戏值不值得入手", appid=1)
        assert resp["source"] == "llm"
        assert resp["answer"] == "LLM 回答"
        assert resp["cached"] is False
        assert calls["tokens"] == 120
        again = await service.ask("这游戏值不值得入手", appid=1)
        assert again["cached"] is True

    @pytest.mark.asyncio
    async def test_llm_off_price_falls_back_to_facts(self, monkeypatch):
        cfg = {**_CFG, "enabled": False}
        _patch_ready(monkeypatch, cfg=cfg)
        monkeypatch.setattr(
            service.pilot_tools, "price_facts", _async_return(dict(_FACTS))
        )
        resp = await service.ask("这游戏值不值得入手", appid=1)
        assert resp["source"] == "facts"
        assert resp["reason"] is None
        assert resp["facts"]["kind"] == "price"

    @pytest.mark.asyncio
    async def test_llm_off_chat_reports_reason(self, monkeypatch):
        cfg = {**_CFG, "enabled": False}
        _patch_ready(monkeypatch, cfg=cfg)
        resp = await service.ask("今天天气怎么样")
        assert resp["source"] == "none"
        assert resp["reason"] == "llm_off"

    @pytest.mark.asyncio
    async def test_cap_reached_degrades_to_facts(self, monkeypatch):
        _patch_ready(monkeypatch, usage=1000)
        monkeypatch.setattr(
            service.pilot_tools, "price_facts", _async_return(dict(_FACTS))
        )
        resp = await service.ask("这游戏值不值得入手", appid=1)
        assert resp["source"] == "facts"
        assert resp["reason"] == "cap_reached"

    @pytest.mark.asyncio
    async def test_llm_failure_degrades_to_facts(self, monkeypatch):
        _patch_ready(monkeypatch, fail=True)
        monkeypatch.setattr(
            service.pilot_tools, "price_facts", _async_return(dict(_FACTS))
        )
        resp = await service.ask("这游戏值不值得入手", appid=1)
        assert resp["source"] == "facts"
        assert resp["reason"] == "llm_failed"

    @pytest.mark.asyncio
    async def test_guide_intent_skips_llm(self, monkeypatch):
        _patch_ready(monkeypatch)
        resp = await service.ask("怎么设置价格提醒")
        assert resp["source"] == "guide"

    @pytest.mark.asyncio
    async def test_recommend_intent_uses_recommend_tool(self, monkeypatch):
        _patch_ready(monkeypatch)
        monkeypatch.setattr(
            service.pilot_tools,
            "recommend_games",
            _async_return([{"appid": 2, "name": "候选", "cnyFen": 2000, "discount": 50,
                            "positiveRate": 0.95, "reviewCount": 99}]),
        )
        resp = await service.ask("推荐几个打折的好评游戏")
        assert resp["source"] == "llm"
        assert resp["facts"]["kind"] == "games"

    @pytest.mark.asyncio
    async def test_no_data(self, monkeypatch):
        _patch_ready(monkeypatch)
        monkeypatch.setattr(service.pilot_tools, "price_facts", _async_return(None))
        monkeypatch.setattr(service.pilot_tools, "search_games", _async_return([]))
        monkeypatch.setattr(service.pilot_tools, "recommend_games", _async_return([]))
        resp = await service.ask("史低多少", appid=999)
        assert resp["source"] == "none"
        assert resp["reason"] == "no_data"

    @pytest.mark.asyncio
    async def test_price_without_target_falls_back_to_candidates(self, monkeypatch):
        _patch_ready(monkeypatch)
        monkeypatch.setattr(service.pilot_tools, "search_games", _async_return([]))
        monkeypatch.setattr(
            service.pilot_tools,
            "recommend_games",
            _async_return([{"appid": 2, "name": "候选", "cnyFen": 2000, "discount": 50,
                            "positiveRate": 0.95, "reviewCount": 99}]),
        )
        resp = await service.ask("史低多少")
        assert resp["source"] == "llm"
        assert resp["facts"]["kind"] == "games"

    @pytest.mark.asyncio
    async def test_ask_stream_events(self, monkeypatch):
        calls = {}
        _patch_ready(monkeypatch, calls=calls)
        monkeypatch.setattr(
            service.pilot_tools, "price_facts", _async_return(dict(_FACTS))
        )

        async def fake_stream(**kw):
            yield ("thinking", "思")
            yield ("thinking", "考")
            yield ("answer", "回")
            yield ("usage", (10, 5))

        monkeypatch.setattr(service.pilot_llm, "chat_complete_stream", fake_stream)
        events = [e async for e in service.ask_stream("这游戏值不值得入手", appid=1)]
        types = [e["type"] for e in events]
        assert types[0] == "facts"
        assert types.count("thinking") == 2 and types.count("answer") == 1
        done = events[-1]
        assert done["type"] == "done"
        assert done["source"] == "llm"
        assert done["answer"] == "回" and done["thinking"] == "思考"
        assert calls["tokens"] == 15

    @pytest.mark.asyncio
    async def test_stream_llm_failure_degrades(self, monkeypatch):
        _patch_ready(monkeypatch, fail=True)
        monkeypatch.setattr(
            service.pilot_tools, "price_facts", _async_return(dict(_FACTS))
        )
        events = [e async for e in service.ask_stream("这游戏值不值得入手", appid=1)]
        done = events[-1]
        assert done["type"] == "done"
        assert done["source"] == "facts"
        assert done["reason"] == "llm_failed"
