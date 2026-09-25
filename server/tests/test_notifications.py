"""通知基础层测试（P6-C）：策略 → 候选 → 去重 → 静默 → 聚合 → 投递。

测的是「一个事实是否值得告诉用户」的决策与投递账本，不是价格判断——
价格判断是 P5 的事，通知层只消费已落库的 price_events。

隔离：tmp 库 + get_session_factory 逐模块打桩；send_mail 用桩记录调用，
不出网、不发真邮件。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.crawl import cycle as cycle_mod
from app.domains.crawl import events as events_mod
from app.domains.crawl import service as crawl_service
from app.domains.crawl.cycle import PriceCycle
from app.domains.crawl.events import PriceEvent
from app.domains.games.models import Game
from app.domains.notifications import policy as policy_mod
from app.domains.notifications import service as notification_service
from app.domains.notifications.mail import describe_event
from app.domains.notifications.models import NotificationCandidate
from app.domains.notifications.policy import PREFS_KEY, load_prefs, save_prefs
from app.domains.regions import service as regions_service
from app.domains.settings import service as settings_service

APP = 880001
RECIPIENT = "owner@example.com"


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    for module in (
        cycle_mod, events_mod, notification_service, settings_service, regions_service
    ):
        monkeypatch.setattr(module, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


@pytest.fixture
def delivery(monkeypatch):
    """send_mail / SMTP 配置 / 监控池范围的桩：记录调用，不出网。"""
    calls: list[dict] = []

    async def _send_mail_ex(subject, html_body):
        calls.append({"subject": subject, "html": html_body})
        return True, None

    async def _smtp_config():
        return {"to_addr": RECIPIENT}

    async def _pool(scope, appids=None):
        return [APP]

    async def _regions():
        return [{"code": "ru", "name": "俄罗斯"}, {"code": "cn", "name": "中国"}]

    monkeypatch.setattr(notification_service, "send_mail_ex", _send_mail_ex)
    monkeypatch.setattr(notification_service, "smtp_config", _smtp_config)
    monkeypatch.setattr(crawl_service, "plan_scope_appids", _pool)
    monkeypatch.setattr(regions_service, "list_regions", _regions)
    return calls


async def _enable(db, **over):
    prefs = {"enabled": True, "categories": {"price": True, "low": True,
                                             "availability": True, "lifecycle": True}}
    prefs.update(over)
    await save_prefs(prefs)
    return await load_prefs()


async def _cycle(db) -> int:
    async with db() as session:
        cycle = PriceCycle(
            kind="scheduled", status="completed", scope="pool",
            expected_json={"appids": [APP], "regions": ["cn"]},
            started_at=datetime.now() - timedelta(hours=2),
            finished_at=datetime.now(),
        )
        session.add(cycle)
        await session.commit()
        return cycle.id


async def _seed_event(
    db, cycle_id, event_type, *, appid=APP, region="RU",
    previous=None, current=None,
) -> int:
    now = datetime.now()
    async with db() as session:
        event = PriceEvent(
            cycle_id=cycle_id, appid=appid,
            region_code=region.upper() if region else None,
            event_type=event_type, previous_json=previous, current_json=current,
            occurred_at=now, created_at=now,
        )
        session.add(event)
        await session.commit()
        return event.id


async def _candidates(db) -> list[NotificationCandidate]:
    async with db() as session:
        rows = (
            await session.execute(select_stmt())
        ).scalars().all()
    return sorted(rows, key=lambda c: c.id)


def select_stmt():
    from sqlalchemy import select

    return select(NotificationCandidate)


async def _seed_game(db, appid=APP):
    now = datetime.now()
    async with db() as session:
        session.add(Game(appid=appid, name="Stubbs", created_at=now, updated_at=now))
        await session.commit()


async def _cycle_status(db, cid) -> str:
    async with db() as session:
        cycle = await session.get(PriceCycle, cid)
        return cycle.status


# ── 策略 ──


@pytest.mark.asyncio
async def test_policy_disabled_produces_nothing(db):
    """总开关关闭：不产生候选，也不留积压。"""
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})
    result = await notification_service.dispatch(cid)

    assert result["created"] == 0
    assert await _candidates(db) == []
    assert (await load_prefs())["enabled"] is False


@pytest.mark.asyncio
async def test_policy_allowed_event_creates_delivered_candidate(db, delivery):
    """非静默期：候选建完即随本轮摘要投递（pending 是建到发之间的瞬态）。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900, "cnyFen": 1500},
                      current={"price": 9900, "cnyFen": 800})
    result = await notification_service.dispatch(cid)

    assert result["created"] == 1
    rows = await _candidates(db)
    assert len(rows) == 1
    assert rows[0].status == "delivered"
    assert rows[0].category == "price"
    assert rows[0].recipient == RECIPIENT
    assert rows[0].delivered_at is not None


@pytest.mark.asyncio
async def test_policy_category_off_blocks_that_category_only(db, delivery):
    """类别开关是用户面开关：关掉「历史低价」不影响「价格变化」。"""
    await _enable(db, categories={"price": True, "low": False, "availability": True,
                                  "lifecycle": True})
    cid = await _cycle(db)
    await _seed_event(db, cid, "NEW_HISTORICAL_LOW", region="CN",
                      previous={"low": 7600}, current={"price": 7600})
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    result = await notification_service.dispatch(cid)
    assert result["created"] == 1
    rows = await _candidates(db)
    assert [r.event_type for r in rows] == ["PRICE_DROP"]


@pytest.mark.asyncio
async def test_policy_ignores_outside_watch_scope(db, monkeypatch):
    """目录层（非监控池）只是价格库维护：不进通知。"""
    async def _empty_pool(scope, appids=None):
        return []

    monkeypatch.setattr(crawl_service, "plan_scope_appids", _empty_pool)
    await _enable(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    assert (await notification_service.dispatch(cid))["created"] == 0
    assert await _candidates(db) == []


@pytest.mark.asyncio
async def test_policy_unknown_event_type_is_not_notified(db):
    """后端先于前端上了新类型：不属任何用户类别 → 不产生候选。"""
    await _enable(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "BRAND_NEW_TYPE",
                      previous={"price": 19900}, current={"price": 9900})

    assert (await notification_service.dispatch(cid))["created"] == 0


# ── 去重 ──


@pytest.mark.asyncio
async def test_duplicate_processing_creates_one_candidate_only(db, delivery):
    """重复处理同一事件（重启 / 重复 finalizing）：候选只有一条。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    event_id = await _seed_event(db, cid, "PRICE_DROP",
                                 previous={"price": 19900}, current={"price": 9900})

    first = await notification_service.dispatch(cid)
    second = await notification_service.dispatch(cid)

    assert first["created"] == 1
    assert second["created"] == 0
    rows = await _candidates(db)
    assert [r.event_id for r in rows] == [event_id]
    # 第二次投递没有可发的未投递候选：不再发第二封
    assert second["sent"] == 0
    assert len(delivery) == 1


# ── 静默期 ──


@pytest.mark.asyncio
async def test_quiet_period_defers_instead_of_dropping(db, delivery):
    """静默期内：候选存在、状态 suppressed、本轮不投递；窗口过后随下一轮投递。"""
    now = datetime.now()
    prefs = await _enable(db)
    prefs["quietEnabled"] = True
    prefs["quietStart"] = (now - timedelta(minutes=30)).strftime("%H:%M")
    prefs["quietEnd"] = (now + timedelta(minutes=30)).strftime("%H:%M")
    await save_prefs(prefs)

    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})
    first = await notification_service.dispatch(cid)

    rows = await _candidates(db)
    assert first["sent"] == 0
    assert [r.status for r in rows] == ["suppressed"]
    assert len(delivery) == 0, "静默期不发送"

    # 窗口移开（下一轮非静默）：顺延的候选随新批次一并投递，而不是被丢掉
    prefs["quietEnabled"] = False
    await save_prefs(prefs)
    second = await notification_service.dispatch(cid)

    rows = await _candidates(db)
    assert second["sent"] == 1
    assert [r.status for r in rows] == ["delivered"]
    assert rows[0].delivered_at is not None


# ── 聚合 ──


@pytest.mark.asyncio
async def test_one_cycle_produces_one_digest_mail(db, delivery):
    """一轮多事件 → 一封邮件：按类别聚合计数，事件数与游戏数都在正文里。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    for region in ("cn", "ru", "kz"):
        await _seed_event(db, cid, "PRICE_DROP", region=region,
                          previous={"price": 19900}, current={"price": 9900})
    await _seed_event(db, cid, "NEW_HISTORICAL_LOW", region="CN",
                      previous={"low": 7600}, current={"price": 7600})

    result = await notification_service.dispatch(cid)

    assert result["created"] == 4
    assert result["sent"] == 4
    assert len(delivery) == 1, "一轮只发一封"
    mail = delivery[0]
    assert "4 条变化" in mail["subject"]
    assert "价格变化" in mail["html"] and "历史低价" in mail["html"]
    assert "俄罗斯" in mail["html"] and "₽" in mail["html"]
    assert "199" in mail["html"] and "99" in mail["html"]


@pytest.mark.asyncio
async def test_digest_without_details_still_sends_summary(db, delivery):
    """includeDetails=False：只发聚合摘要，不逐条列。"""
    await _enable(db, includeDetails=False)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})
    await notification_service.dispatch(cid)

    assert len(delivery) == 1
    assert "价格变化" in delivery[0]["html"]
    assert "俄罗斯" not in delivery[0]["html"], "关掉明细就不该逐条列地区"


# ── 失败隔离 ──


@pytest.mark.asyncio
async def test_permanent_smtp_failure_marks_candidates_failed_only(db, delivery, monkeypatch):
    """凭据/配置类失败：候选 failed 且**不再重试**，事件与 Cycle 原样。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    async def _auth_fail(subject, html):
        return False, "535 Authentication failed"

    monkeypatch.setattr(notification_service, "send_mail_ex", _auth_fail)
    result = await notification_service.dispatch(cid)

    assert result["failed"] == 1 and result["sent"] == 0
    rows = await _candidates(db)
    assert [r.status for r in rows] == ["failed"]
    assert "smtp" in rows[0].reason
    assert rows[0].attempts == 1
    # 事件事实与 Cycle 终态不被通知失败污染
    assert await _cycle_status(db, cid) == "completed"
    # 授权码错不能循环重发：下一次摘要不再带上它
    again = await notification_service.dispatch(cid)
    assert again["sent"] == 0 and again["retried"] == 0


@pytest.mark.asyncio
async def test_temporary_smtp_failure_is_retried_next_cycle(db, delivery, monkeypatch):
    """临时网络故障：候选失败后回到队列，下一次投递恢复 → delivered（§五 恢复路径）。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    async def _net_fail(subject, html):
        return False, "Connection unexpectedly closed"

    monkeypatch.setattr(notification_service, "send_mail_ex", _net_fail)
    first = await notification_service.dispatch(cid)
    assert first["failed"] == 1 and first["retryable"] is True

    async def _recovered(subject, html):
        delivery.append({"subject": subject, "html": html})
        return True, None

    monkeypatch.setattr(notification_service, "send_mail_ex", _recovered)
    second = await notification_service.dispatch(cid)

    assert second["retried"] == 1
    assert second["sent"] == 1
    rows = await _candidates(db)
    assert [r.status for r in rows] == ["delivered"]
    assert rows[0].attempts == 2
    assert rows[0].last_error is None
    assert len(delivery) == 1, "恢复后只补发一次，不是每轮都发"


@pytest.mark.asyncio
async def test_retry_is_bounded_by_max_attempts(db, delivery, monkeypatch):
    """重试有上限：临时故障反复失败，到上限后永久放弃，不再进队列。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    async def _net_fail(subject, html):
        return False, "Connection unexpectedly closed"

    monkeypatch.setattr(notification_service, "send_mail_ex", _net_fail)
    for _ in range(policy_mod.MAX_ATTEMPTS):
        await notification_service.dispatch(cid)
    rows = await _candidates(db)
    assert rows[0].attempts == policy_mod.MAX_ATTEMPTS

    # 到上限后不再重试
    again = await notification_service.dispatch(cid)
    assert again["sent"] == 0 and again["retried"] == 0
    rows = await _candidates(db)
    assert rows[0].attempts == policy_mod.MAX_ATTEMPTS


@pytest.mark.asyncio
async def test_retryable_classification(db):
    """只认明确的临时故障；未知错误按不可重试处理（不循环发送）。"""
    assert notification_service.retryable("SMTPServerDisconnected: Connection unexpectedly closed") is True
    assert notification_service.retryable("TimeoutError: timed out") is True
    assert notification_service.retryable("OSError: Network is unreachable") is True
    # 中文 Windows 会把 socket 错误本地化，必须也能认出来
    assert notification_service.retryable("ConnectionRefusedError: [WinError 10061] 由于目标计算机积极拒绝，无法连接。") is True
    assert notification_service.retryable("SMTPAuthenticationError: 535 Authentication failed") is False
    assert notification_service.retryable("SMTPSenderRefused: 553 mailbox unavailable") is False
    assert notification_service.retryable("SMTP 未配置") is False
    assert notification_service.retryable(None) is False
    assert notification_service.retryable("某个没见过的错") is False


@pytest.mark.asyncio
async def test_sending_candidates_are_not_requeued(db, delivery):
    """进程在投递途中被杀：候选停在 sending，不再重发（宁丢一封不重发）。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})
    await notification_service.dispatch(cid)
    rows = await _candidates(db)

    async with db() as session:
        row = await session.get(NotificationCandidate, rows[0].id)
        row.status = "sending"
        row.delivered_at = None
        await session.commit()

    again = await notification_service.dispatch(cid)
    assert again["sent"] == 0 and again["retried"] == 0
    assert [r.status for r in await _candidates(db)] == ["sending"]


@pytest.mark.asyncio
async def test_notification_failure_never_breaks_cycle_finalizing(db, monkeypatch):
    """通知层抛异常也只记日志：调度器侧包住后 Cycle 照常收敛 completed。"""
    from app.core import scheduler as sched_mod

    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    async def _explode(cycle_id):
        raise RuntimeError("通知层炸了")

    monkeypatch.setattr(notification_service, "dispatch", _explode)
    # 调度器侧由 try/except 包住：通知挂了不能让 Cycle 变 failed
    await sched_mod._notify_cycle_events(cid)

    assert await _cycle_status(db, cid) == "completed"


# ── 重启 ──


@pytest.mark.asyncio
async def test_restart_does_not_resend_delivered(db, delivery):
    """投递成功后进程重启再次处理同一轮：不重复发送。"""
    await _enable(db)
    await _seed_game(db)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    await notification_service.dispatch(cid)
    await notification_service.dispatch(cid)

    assert len(delivery) == 1
    rows = await _candidates(db)
    assert [r.status for r in rows] == ["delivered"]


# ── 事实拼装（邮件正文的人话）──


@pytest.mark.asyncio
async def test_describe_event_keeps_fact_granularity():
    names = {"RU": "俄罗斯", "CN": "中国"}
    assert describe_event(
        event_type="PRICE_DROP", region_code="RU",
        previous={"price": 19900, "cnyFen": 1500},
        current={"price": 9900, "cnyFen": 800},
        region_names=names,
    ) == "价格下降 · 俄罗斯 · 199.00 ₽ → 99.00 ₽"

    assert describe_event(
        event_type="NEW_HISTORICAL_LOW", region_code="CN",
        previous={"low": 7600}, current={"price": 7600}, region_names=names,
    ) == "历史新低 · 中国 · ¥76.00 → ¥76.00"

    assert describe_event(
        event_type="REGION_LOCKED", region_code="UA",
        previous={"status": "ok"}, current={"status": "locked"},
        region_names={},
    ) == "进入锁区 · UA · 可购买 → 锁区"

    # 游戏级事件（促销免费）没有区码也没有前后值：不编造
    assert describe_event(
        event_type="FREE_PROMO", region_code=None,
        previous={"freeKind": None},
        current={"freeKind": "promo", "region": "cn"},
        region_names=names,
    ) == "限时免费 · 中国"


@pytest.mark.asyncio
async def test_test_mail_does_not_touch_business_data(db, monkeypatch):
    """测试邮件与 Price Event / Candidate / Cycle 完全分开：不写任何业务数据。"""
    from fastapi import HTTPException

    from app.domains.notifications import router as notifications_router

    async def _ok(**kwargs):
        return {"ok": True}

    monkeypatch.setattr(notifications_router, "send_test_mail", _ok)
    assert await notifications_router.send_test() == {"ok": True}
    assert await _candidates(db) == []

    async def _bad(**kwargs):
        raise ValueError("连接失败：535 Authentication failed")

    monkeypatch.setattr(notifications_router, "send_test_mail", _bad)
    with pytest.raises(HTTPException) as excinfo:
        await notifications_router.send_test()
    # 凭据错误必须是 HTTP 错误，不能显示成功
    assert excinfo.value.status_code == 400
    assert "535" in excinfo.value.detail


@pytest.mark.asyncio
async def test_smtp_unconfigured_creates_no_candidates(db, monkeypatch):
    """出口未配置：不产生候选也不投递（开启通知但没配邮箱是配置问题，不是积压）。"""
    await _enable(db)

    async def _no_smtp():
        return {"to_addr": ""}

    monkeypatch.setattr(notification_service, "smtp_config", _no_smtp)
    cid = await _cycle(db)
    await _seed_event(db, cid, "PRICE_DROP",
                      previous={"price": 19900}, current={"price": 9900})

    result = await notification_service.dispatch(cid)
    assert result["created"] == 0
    assert await _candidates(db) == []


@pytest.mark.asyncio
async def test_prefs_endpoint_smtp_view(db, monkeypatch):
    """路由层 get_prefs 端到端：_smtp_view 的取键与 get_smtp_config 的
    camelCase 返回（toAddr / useSsl）对齐，键形漂移即整页 500。"""
    from app.domains.notifications import router as notif_router

    async def _front_smtp():
        return {
            "host": "smtp.example.com",
            "port": 465,
            "user": "sender@example.com",
            "password": "s***r",
            "hasPassword": True,
            "toAddr": "owner@example.com",
            "useSsl": True,
        }

    monkeypatch.setattr(notif_router, "get_session_factory", lambda: db)
    monkeypatch.setattr(notif_router, "get_smtp_config", _front_smtp)

    payload = await notif_router.get_prefs()
    assert payload["smtp"]["configured"] is True
    assert payload["smtp"]["host"] == "smtp.example.com"
    assert payload["smtp"]["userMasked"].endswith("@example.com")
    assert payload["smtp"]["useSsl"] is True
    assert payload["smtp"]["hasPassword"] is True
    assert "password" not in payload["smtp"]
