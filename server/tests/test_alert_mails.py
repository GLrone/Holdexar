"""降价提醒/新史低邮件测试：主题骨架统一 + check_new_lows 行为验收。

库隔离到临时 sqlite，SMTP 全程打桩，不触真实网络与生产库。
"""
from __future__ import annotations

import email
import email.policy
import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.alerts import notify
from app.domains.alerts import service as alerts_service
from app.domains.games.models import Game, GameCurrentPrice

NOW = datetime.now()


# ── 主题模板纯函数验收（同款深色主题）──────────────────────────


@pytest.mark.parametrize(
    "builder",
    [
        lambda: notify.alert_mail_html([
            {"name": "Portal 2", "region": "CN", "price": 4201, "discount": 90},
        ]),
        lambda: notify.new_low_mail_html([
            {"name": "Portal 2", "region_name": "国区", "price_text": "¥42.01",
             "cn_price_text": "¥42.01", "discount": 90},
        ]),
        lambda: notify._test_mail_html(host="smtp.qq.com", port=465, user="me@qq.com",
                                       to_addr="to@qq.com", use_ssl=True),
    ],
    ids=["alert", "new_low", "test"],
)
def test_all_mails_share_theme(builder):
    """三封邮件同一深色主题骨架：品牌头 + 主卡 + 页脚 + 关键色。"""
    html = builder()
    for needle in ("Holdexar", "STEAM 多区价格监控终端",
                   "#66c0f4", "#1b2838", "#0e1a27", "border-radius:14px"):
        assert needle in html, f"{needle} 缺失"


def test_alert_mail_renders_rows():
    html = notify.alert_mail_html([
        {"name": "A<B>", "region": "CN", "price": 4201, "discount": 90},
        {"name": "B", "region": "AR", "price": 500, "discount": 80},
    ])
    assert "价格提醒已触发" in html
    assert "A&lt;B&gt;" in html  # 游戏名过 HTML 转义
    assert "¥42.01" in html and "¥5.00" in html  # 价格分 → 元
    assert "-90%" in html and "-80%" in html


def test_new_low_mail_renders_rows():
    html = notify.new_low_mail_html([
        {"name": "Portal 2", "region_name": "国区", "price_text": "¥42.01",
         "cn_price_text": "¥42.01", "discount": 90},
    ])
    assert "新史低速报" in html
    assert "Portal 2" in html and "国区" in html
    assert "¥42.01" in html and "-90%" in html


# ── check_new_lows 行为验收 ───────────────────────────────────


class _FakeSMTP:
    last_subject: str | None = None
    last_body: str | None = None

    def __init__(self, host, port, timeout=0):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, from_addr, to_addrs, msg_string):
        parsed = email.message_from_string(msg_string, policy=email.policy.default)
        type(self).last_subject = parsed["Subject"]
        type(self).last_body = parsed.get_content()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _price(appid: int, region: str, fen: int, *, discount: int = 0, currency: str = "CNY"):
    return GameCurrentPrice(
        appid=appid, region_code=region, currency=currency,
        price=fen, original_price=fen, discount_percent=discount,
        sub_id=0, price_status="ok", cny_fen=fen, updated_at=NOW,
    )


_owner_region_holder: dict = {"region": None}


async def _fake_primary():
    """主账号桩：region=None 时无绑定（号主区未知）。"""
    from app.domains.account.models import SteamAccount

    if _owner_region_holder["region"] is None:
        return None
    return SteamAccount(
        steam_id="76561190000000001", cookies="x", is_active=True,
        wallet_json={"region_code": _owner_region_holder["region"]},
    )


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)

    from app.domains.alerts import service as alerts

    monkeypatch.setattr(alerts, "get_session_factory", lambda: factory)
    # 号主区直接对 alerts 引用的账号域函数打桩（账号域内部的 session
    # factory 绑定不可替换，没必要为取一个 region_code 走全链路）
    monkeypatch.setattr(alerts.account_service, "get_primary_account", _fake_primary)
    monkeypatch.setattr(alerts.regions_service, "list_regions", _fake_regions)
    monkeypatch.setattr(alerts.settings_service, "set_value", _fake_set_value)
    monkeypatch.setattr(alerts.settings_service, "get_value", _fake_get_value)
    return factory


async def _fake_regions():
    return [{"code": "cn", "name": "国区"}, {"code": "ar", "name": "阿根廷"}]


async def _fake_get_value(key, default=None):
    return _sent_store.get(key, default)


async def _fake_set_value(key, value):
    _sent_store[key] = value


_sent_store: dict = {}


@pytest_asyncio.fixture(autouse=True)
async def _schema(db, monkeypatch):
    import app.domains.games.models  # noqa: F401
    import app.domains.alerts.models  # noqa: F401
    import app.domains.account.models  # noqa: F401
    import app.domains.regions.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    _sent_store.clear()
    _FakeSMTP.last_subject = None
    _FakeSMTP.last_body = None
    _owner_region_holder["region"] = None
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP", _FakeSMTP)
    yield


async def _call_check_new_lows(appids, owner_region=None):
    _owner_region_holder["region"] = owner_region
    return await alerts_service.check_new_lows(appids)


@pytest.mark.asyncio
async def test_new_low_picks_owner_region_and_caps_at_2(db):
    """3 款新史低：号主区(ar)命中的 2 款优先入选，第 3 款不展示（只挑 2）。"""
    async with db() as session:
        session.add_all([
            Game(appid=100, name="G100", hl_flag=1),
            Game(appid=200, name="G200", hl_flag=1),
            Game(appid=300, name="G300", hl_flag=1),
        ])
        session.add_all([
            # G100：ar 是全区最低 → 号主区命中
            _price(100, "CN", 10000, discount=50),
            _price(100, "AR", 2000, discount=50),
            # G200：tr 更低但 ar 可买 → 号主区命中（选 ar 价）
            _price(200, "CN", 10000, discount=50),
            _price(200, "TR", 1500),
            _price(200, "AR", 3000),
            # G300：仅 tr（号主区无关）→ 兜底候选
            _price(300, "CN", 10000),
            _price(300, "TR", 1000),
        ])
        await session.commit()

    picks = await _call_check_new_lows([100, 200, 300], owner_region="ar")

    assert {p["appid"] for p in picks} == {100, 200}  # 号主区两款优先，仅 2 款
    by_appid = {p["appid"]: p for p in picks}
    assert by_appid[100]["region_code"] == "AR" and by_appid[100]["region_name"] == "阿根廷"
    assert by_appid[200]["region_code"] == "AR"  # 号主区价优先于全局最低
    assert by_appid[200]["price_text"] == "¥30.00"
    # 邮件已发：主题 + 主题化正文
    assert _FakeSMTP.last_subject and "新史低" in _FakeSMTP.last_subject
    assert "新史低速报" in _FakeSMTP.last_body and "G100" in _FakeSMTP.last_body
    # 游标已写入全部 3 款（含未展示的 G300）
    assert set(_sent_store[alerts_service.KEY_NEW_LOW_SENT]) == {100, 200, 300}


@pytest.mark.asyncio
async def test_new_low_dedupes_via_cursor(db):
    """同批第二次检查：游标差集为空 → 不再发信。"""
    async with db() as session:
        session.add(Game(appid=100, name="G100", hl_flag=1))
        session.add_all([_price(100, "CN", 4200, discount=90)])
        await session.commit()

    first = await _call_check_new_lows([100], owner_region="cn")
    assert len(first) == 1 and _FakeSMTP.last_subject is not None

    _FakeSMTP.last_subject = None
    second = await _call_check_new_lows([100])
    assert second == [] and _FakeSMTP.last_subject is None  # 已通报过


@pytest.mark.asyncio
async def test_new_low_falls_back_when_owner_region_misses(db):
    """号主区(tr)无新史低时：按价差降序兜底，仍发信挑 2 款。"""
    async with db() as session:
        session.add_all([
            Game(appid=100, name="G100", hl_flag=1),
            Game(appid=200, name="G200", hl_flag=1),
        ])
        session.add_all([
            _price(100, "CN", 10000),
            _price(100, "AR", 9000),
            _price(200, "CN", 10000),
            _price(200, "AR", 1000),
        ])
        await session.commit()

    picks = await _call_check_new_lows([100, 200], owner_region="tr")
    assert len(picks) == 2
    # 价差大的 G200（省 90 元）排前
    assert picks[0]["appid"] == 200


@pytest.mark.asyncio
async def test_new_low_no_hl1_no_mail(db):
    """无新史低（hl_flag!=1）→ 不发信、游标不写。"""
    async with db() as session:
        session.add(Game(appid=100, name="G100", hl_flag=0))
        session.add_all([_price(100, "CN", 1000)])
        await session.commit()

    picks = await _call_check_new_lows([100], owner_region="cn")
    assert picks == []
    assert _FakeSMTP.last_subject is None
    assert alerts_service.KEY_NEW_LOW_SENT not in _sent_store
