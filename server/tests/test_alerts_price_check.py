"""alerts 价格触发口径验收：price 类阈值 = 人民币分，与 cny_fen 同口径比较。

v7 迁移把阈值口径从 GameCurrentPrice.price（该区货币分）直比归一到 cny_fen
——US 规则填 1000 意为 $10，前端按 ¥ 展示。归一后触发检查只认 cny_fen，
本币分数值不再误触发；事件行落 price_cny 快照，邮件带本币价文本。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base  # noqa: E402
from app.domains.alerts import service as alerts_service  # noqa: E402
from app.domains.alerts.models import AlertEvent, PriceAlert  # noqa: E402
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory  # noqa: E402


@pytest_asyncio.fixture
async def alerts_db(tmp_path: Path, monkeypatch):
    """tmp 独立库 + alerts 服务依赖隔离（区服名走 code 兜底、发信记调用）。"""
    db = tmp_path / "alerts.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{db.as_posix()}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    # 写路径经 write_session 走数据库模块工厂：两处都要指向同一个独立库
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(alerts_service, "get_session_factory", lambda: factory)

    # 规则求值与候选资格都读库内通知偏好（默认关）：本文件只测规则口径，固定开启
    from app.domains.notifications.policy import save_prefs

    await save_prefs({"enabled": True})

    async def _region_name(code: str) -> str:
        return code

    monkeypatch.setattr(alerts_service, "region_display_name", _region_name)

    sent: dict[str, object] = {}

    async def _fake_send_mail(subject: str, html_body: str, *, recipient=None) -> bool:
        sent["subject"] = subject
        sent["html"] = html_body
        return True, None

    # 触发投递走统一候选管线，发信缝隙在通知服务
    from app.domains.notifications import service as notification_service
    from app.domains.settings.service import set_value

    await set_value("smtp.to_addr", "owner@example.test")
    monkeypatch.setattr(notification_service, "send_mail_ex", _fake_send_mail)
    yield factory, sent
    await engine.dispose()


async def _seed_game(factory, *, price: int | None, cny_fen: int | None, discount: int = 0) -> None:
    """Steam 现价行：US 区 $10（本币 1000 分 / cny_fen 7250）为基准场景。"""
    from datetime import datetime

    now = datetime.now()
    async with factory() as session:
        session.add(Game(appid=100, name="测试游戏", header_image="https://example/header.jpg"))
        session.add(
            GameCurrentPrice(
                appid=100,
                region_code="US",
                currency="USD",
                price=price,
                cny_fen=cny_fen,
                discount_percent=discount,
                sub_id=7,
                price_status="ok",
                attempt_outcome="success",
                steam_answer="ok",
                last_success_at=now,
                updated_at=now,
            )
        )
        # 当前观测需在历史链中确立产品身份（known_product 闸）
        if price is not None:
            session.add(GamePriceHistory(
                appid=100, region_code="US", currency="USD", sub_id=7, price=price,
                source="steam", price_status="ok", snapshot_at=now))
        await session.commit()


async def _add_alert(factory, *, target_type: str = "price", target_value: float | None = None) -> int:
    async with factory() as session:
        alert = PriceAlert(
            appid=100, region="US", target_type=target_type, target_value=target_value, active=True
        )
        session.add(alert)
        await session.commit()
        return int(alert.id)


@pytest.mark.asyncio
async def test_price_hit_compares_cny_fen_and_snapshots(alerts_db) -> None:
    """¥73 目标（7300 分）vs 现价 $10（cny_fen 7250）→ 触发；事件落人民币分
    快照，邮件字段带本币价文本与人民币参照。"""
    factory, sent = alerts_db
    await _seed_game(factory, price=1000, cny_fen=7250)
    await _add_alert(factory, target_value=7300)

    triggered = await alerts_service.check_appids([100])
    assert len(triggered) == 1
    row = triggered[0]
    assert row["price"] == 1000
    assert row["currency"] == "USD"
    assert sent["subject"]  # 触发即合并发信，邮件带本币价文本
    assert "$10.00" in sent["html"]

    async with factory() as session:
        event = (await session.execute(select(AlertEvent))).scalars().one()
        assert event.price == 1000
        assert event.price_cny == 7250


@pytest.mark.asyncio
async def test_price_miss_when_only_local_minor_below(alerts_db) -> None:
    """旧口径残留值 1000（=$10 的本币分）在 ¥10（1000 分）阈值语义下：
    cny_fen 7250 > 1000 → 不触发。这是 v7 口径修正的核心断言。"""
    factory, sent = alerts_db
    await _seed_game(factory, price=1000, cny_fen=7250)
    await _add_alert(factory, target_value=1000)

    triggered = await alerts_service.check_appids([100])
    assert triggered == []
    assert "subject" not in sent


@pytest.mark.asyncio
async def test_price_skips_when_cny_fen_missing(alerts_db) -> None:
    """cny_fen 缺档（汇率缺失轮）时 price 类不判：宁漏不误。"""
    factory, _ = alerts_db
    await _seed_game(factory, price=1000, cny_fen=None)
    await _add_alert(factory, target_value=99999)

    triggered = await alerts_service.check_appids([100])
    assert triggered == []


@pytest.mark.asyncio
async def test_events_listing_carries_cover_and_local_price(alerts_db) -> None:
    """触发历史出口：封面（缺档回退 Steam CDN 拼图）、本币价文本、人民币快照。"""
    factory, _ = alerts_db
    await _seed_game(factory, price=1000, cny_fen=7250)
    await _add_alert(factory, target_value=7300)
    await alerts_service.check_appids([100])

    events = await alerts_service.list_events()
    assert len(events) == 1
    ev = events[0]
    assert ev["gameName"] == "测试游戏"
    assert ev["gameHeader"] == "https://example/header.jpg"
    assert ev["priceText"] == "$10.00"
    assert ev["priceCny"] == 7250

    # 候选账本引用保护：被通知结果引用的触发记录不可删
    with pytest.raises(ValueError):
        await alerts_service.delete_event(ev["id"])
    assert len(await alerts_service.list_events()) == 1


@pytest.mark.asyncio
async def test_clear_events_removes_all(alerts_db) -> None:
    """清空只移除无引用记录：被候选账本引用的触发记录保留。"""
    factory, _ = alerts_db
    await _seed_game(factory, price=1000, cny_fen=7250)
    await _add_alert(factory, target_value=7300)
    await alerts_service.check_appids([100])

    removed = await alerts_service.clear_events()
    assert removed == {"removed": 0, "protected": 1}
    assert len(await alerts_service.list_events()) == 1
    assert (await alerts_service.clear_events())["removed"] == 0
