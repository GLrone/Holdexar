"""监控面通知落账测试：五类新事实进 FactNotice + 候选管线（幂等 + 冷却）。

邮件正文渲染与 SMTP 投递验收在 test_mail_rendering.py 与 test_notification_smtp.py。
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
from sqlalchemy import select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.alerts import service as alerts_service
from app.domains.games.models import Bundle, BundleRegionPrice, Game, GameCurrentPrice
from app.domains.notifications.models import FactNotice, NotificationCandidate
from app.domains.notifications import policy
from app.core.database import write_session
from app.domains.wishlist.models import WishlistItem

NOW = datetime.now()


def _price(appid: int, region: str, fen: int, *, discount: int = 0, currency: str = "CNY"):
    return GameCurrentPrice(
        appid=appid, region_code=region, currency=currency,
        price=fen, original_price=fen, discount_percent=discount,
        sub_id=0, price_status="ok", cny_fen=fen, updated_at=NOW,
    )


def _bundle_row(bid: int, region: str, fen: int, *, discount: int, kind: int = 0):
    return BundleRegionPrice(
        bundle_id=bid, region_code=region, currency="CNY", price=fen,
        original_price=fen, discount_percent=discount, bundle_base_discount=0,
        price_status="ok", cny_fen=fen, app_ids=[1, 2], crawled_at=NOW,
    )


async def _seed_bundle():
    async with write_session() as session:
        session.add_all([
            Bundle(bundle_id=1, name="包一", item_kind=0),
            Bundle(bundle_id=2, name="包二", item_kind=0),
        ])
        session.add_all([
            _bundle_row(1, "CN", 6000, discount=98),
            _bundle_row(1, "AR", 3000, discount=98),
            _bundle_row(2, "CN", 200, discount=97),   # ¥2 → 低于价格下限，排除
        ])
        await session.commit()


async def _facts():
    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        facts = (await session.execute(
            select(FactNotice).order_by(FactNotice.id))).scalars().all()
        cands = (await session.execute(
            select(NotificationCandidate).order_by(NotificationCandidate.id))).scalars().all()
    return facts, cands


async def _release_discovery_gate():
    await alerts_service.settings_service.set_value(alerts_service.KEY_BUNDLE_DEAL_AT, 0)


# ── 捆绑包精选：按「包 + 深度」事实落账 ─────────────────────────


@pytest.mark.asyncio
async def test_bundle_deals_fact_and_candidate():
    await policy.save_prefs({"enabled": True})
    await _seed_bundle()
    picks = await alerts_service.check_bundle_deals()
    assert [p["bundle_id"] for p in picks] == [1]
    # 无号主区时按「折扣最深 → 价最低」选区：AR 同折扣更便宜 → 链接带 cc=ar
    assert picks[0]["region_code"] == "AR"
    assert picks[0]["url"].endswith("/bundle/1/?cc=ar")

    facts, cands = await _facts()
    assert [f.fact_key for f in facts] == ["bundle:1:98"]
    assert len(cands) == 1 and cands[0].event_type == "BUNDLE_DEAL"
    assert cands[0].payload_json["title"] == "包一 -98%"
    assert cands[0].payload_json["priceText"].startswith("¥")

    # 20h 发现闸内直接返回；放行后同深度事实幂等，不重复落账
    assert await alerts_service.check_bundle_deals() == []
    await _release_discovery_gate()
    assert await alerts_service.check_bundle_deals() == []
    facts, _ = await _facts()
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_bundle_deals_only_reenlists_on_deeper_discount():
    await policy.save_prefs({"enabled": True})
    await _seed_bundle()
    await alerts_service.check_bundle_deals()
    await _release_discovery_gate()

    async with write_session() as session:
        row = (await session.execute(select(BundleRegionPrice).where(
            BundleRegionPrice.bundle_id == 1, BundleRegionPrice.region_code == "CN"))).scalar_one()
        row.discount_percent = 99
        await session.commit()

    picks = await alerts_service.check_bundle_deals()
    assert [p["max_discount"] for p in picks] == [99]
    facts, _ = await _facts()
    assert sorted(f.fact_key for f in facts) == ["bundle:1:98", "bundle:1:99"]


# ── Epic 喜加一：当期条目逐条落账，预告不记账 ───────────────────


@pytest.mark.asyncio
async def test_epic_free_tracks_current_only(monkeypatch):
    await policy.save_prefs({"enabled": True})
    from app.domains.metadata import service as metadata_service

    async def _offers():
        return {"ok": True, "offers": [
            {"title": "FreeA", "titleCn": "免费A", "appid": 100, "upcoming": False,
             "end": "2026-01-08", "url": "https://epic/a"},
            {"title": "SoonB", "appid": 200, "upcoming": True,
             "start": "2026-01-08", "url": "https://epic/b"},
        ]}

    monkeypatch.setattr(metadata_service, "epic_free_offers", _offers)
    picks = await alerts_service.check_epic_free()
    assert [p.get("titleCn") or p["title"] for p in picks] == ["免费A"]

    facts, cands = await _facts()
    assert [f.fact_key for f in facts] == ["epic:appid:100"]
    assert len(cands) == 1 and cands[0].event_type == "EPIC_FREE"
    assert cands[0].payload_json["url"] == "https://epic/a"

    assert await alerts_service.check_epic_free() == []  # 同条目幂等
    facts, _ = await _facts()
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_epic_free_no_offers_when_channel_down(monkeypatch):
    from app.domains.metadata import service as metadata_service

    async def _offers():
        return {"ok": False, "offers": []}

    monkeypatch.setattr(metadata_service, "epic_free_offers", _offers)
    assert await alerts_service.check_epic_free() == []
    facts, _ = await _facts()
    assert facts == []


# ── Humble Choice 当月包：同月幂等 ──────────────────────────────


@pytest.mark.asyncio
async def test_hb_choice_month_fact():
    await policy.save_prefs({"enabled": True})
    async with write_session() as session:
        session.add(Game(appid=100, name="G100"))
        await session.commit()

    result = {"recorded": True, "label": "HB慈善包26年1月包", "productName": "January 2026",
              "marked": [{"appid": 100, "title": "G100"}]}
    games = await alerts_service.check_hb_choice(result)
    assert games == ["G100"]

    facts, cands = await _facts()
    assert [f.fact_key for f in facts] == ["hb_choice:HB慈善包26年1月包"]
    assert len(cands) == 1 and cands[0].event_type == "HB_CHOICE"
    assert "G100" in cands[0].payload_json["games"]

    assert await alerts_service.check_hb_choice(result) == []  # 同月幂等
    # 未结案（有未解析条目）不落账
    assert await alerts_service.check_hb_choice({**result, "recorded": False}) == []
    facts, _ = await _facts()
    assert len(facts) == 1


# ── 代理通道 / 系统告警：冷却与「未配置不算异常」────────────────


@pytest.mark.asyncio
async def test_proxy_alert_skips_when_no_channel(monkeypatch):
    from app.domains.proxies import service as proxies_service

    async def _stats():
        return {"pool": {"total": 0, "ok": 0},
                "clash": {"running": False, "nodes": 0, "okNodes": 0,
                          "exitIps": 0, "okExitIps": 0},
                "available": 0, "total": 0}

    monkeypatch.setattr(proxies_service, "pool_stats", _stats)
    assert await alerts_service.check_proxy_health() is False
    facts, _ = await _facts()
    assert facts == []


@pytest.mark.asyncio
async def test_proxy_alert_records_then_cools_down(monkeypatch):
    await policy.save_prefs({"enabled": True})
    from app.domains.proxies import service as proxies_service

    async def _stats():
        return {"pool": {"total": 2, "ok": 0},
                "clash": {"running": True, "nodes": 10, "okNodes": 0,
                          "exitIps": 8, "okExitIps": 0},
                "available": 0, "total": 10}

    async def _subs():
        return [{"id": 1, "kind": "clash", "label": "机场A",
                 "url": "https://secret/sub", "lastStats": {"traffic": {"upload": 0, "download": 0}}}]

    monkeypatch.setattr(proxies_service, "pool_stats", _stats)
    monkeypatch.setattr(proxies_service, "list_subscriptions", _subs)

    assert await alerts_service.check_proxy_health() is True
    facts, cands = await _facts()
    assert len(facts) == 1 and facts[0].fact_key.startswith("proxy_alert:")
    assert len(cands) == 1 and cands[0].event_type == "PROXY_ALERT"
    assert "全部代理通道不可用" in cands[0].payload_json["title"]
    assert "https://secret/sub" not in str(cands[0].payload_json), "订阅 URL 是凭据，绝不进通知载荷"

    assert await alerts_service.check_proxy_health() is False  # 12h 冷却
    facts, _ = await _facts()
    assert len(facts) == 1


@pytest.mark.asyncio
async def test_system_alert_cooldown_per_kind():
    await policy.save_prefs({"enabled": True})
    assert await alerts_service.send_system_alert(
        kind="backup", title="自动备份失败", summary="未完成",
        rows=[("失败原因", "disk full")],
    ) is True
    facts, cands = await _facts()
    assert len(facts) == 1 and facts[0].fact_key.startswith("system_alert:backup:")
    assert len(cands) == 1 and cands[0].event_type == "SYSTEM_ALERT"

    # 同 kind 冷却内不落新事实；不同 kind 互不影响
    assert await alerts_service.send_system_alert(
        kind="backup", title="自动备份失败", summary="未完成",
    ) is False
    store = await alerts_service.settings_service.get_value(alerts_service.KEY_SYS_ALERT_AT)
    store["backup"] = time.time() - 25 * 3600  # 过冷却
    await alerts_service.settings_service.set_value(alerts_service.KEY_SYS_ALERT_AT, store)
    assert await alerts_service.send_system_alert(
        kind="backup", title="自动备份失败", summary="未完成",
    ) is True
    facts, _ = await _facts()
    assert len(facts) == 2


# ── 爬取任务失败告警：接线（任务状态仍要诚实收敛为 failed）────────


@pytest.mark.asyncio
async def test_crawl_job_failure_triggers_system_alert(monkeypatch):
    from app.domains.crawl import service as crawl_service

    finished: list[str] = []
    alerts: list[dict] = []

    async def _boom(*a, **kw):
        raise RuntimeError("network down")

    async def _finish(job_id, status, stats=None, error=None):
        finished.append(status)

    async def _alert(**kw):
        alerts.append(kw)
        return True

    monkeypatch.setattr(crawl_service, "run_crawl", _boom)
    monkeypatch.setattr(crawl_service, "_finish_job", _finish)
    monkeypatch.setattr(crawl_service.alerts_service, "send_system_alert", _alert)

    await crawl_service._execute(42, [(620, "CN")], None, asyncio.Event())

    assert finished == ["failed"], "任务状态必须先诚实落 failed"
    assert alerts and alerts[0]["kind"] == "crawl"
