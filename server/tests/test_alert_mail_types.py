"""监控范围扩展邮件测试：模板渲染 + 取数/游标/冷却行为验收。

库隔离到临时 sqlite，SMTP 出口全程打桩（send_mail 记录器），不触真实网络
与生产库；KV 走内存字典（不碰 app_settings 真表）。
"""
from __future__ import annotations

import sys
import time
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.alerts import notify
from app.domains.alerts import service as alerts_service
from app.domains.games.models import Bundle, BundleRegionPrice, Game, GameCurrentPrice
from app.domains.wishlist.models import WishlistItem

NOW = datetime.now()

# ── SMTP 出口记录器（不触网、不读真库里的 SMTP 配置）──────────────

_SENT: list[tuple[str, str]] = []


async def _record_send(subject: str, html_body: str) -> bool:
    _SENT.append((subject, html_body))
    return True


_SENT_STORE: dict = {}


async def _fake_get_value(key, default=None):
    return _SENT_STORE.get(key, default)


async def _fake_set_value(key, value):
    _SENT_STORE[key] = value


_OWNER = {"region": None}


async def _fake_primary():
    from app.domains.account.models import SteamAccount

    if _OWNER["region"] is None:
        return None
    return SteamAccount(
        steam_id="76561190000000001", cookies="x", is_active=True,
        wallet_json={"region_code": _OWNER["region"]},
    )


async def _fake_regions():
    return [
        {"code": "cn", "name": "国区"},
        {"code": "ar", "name": "阿根廷"},
        {"code": "tr", "name": "土耳其"},
    ]


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(alerts_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(alerts_service.account_service, "get_primary_account", _fake_primary)
    monkeypatch.setattr(alerts_service.regions_service, "list_regions", _fake_regions)
    monkeypatch.setattr(alerts_service.settings_service, "get_value", _fake_get_value)
    monkeypatch.setattr(alerts_service.settings_service, "set_value", _fake_set_value)
    monkeypatch.setattr(notify, "send_mail", _record_send)
    import app.core.database as database_module

    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _clean(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _SENT.clear()
    _SENT_STORE.clear()
    _OWNER["region"] = None
    yield


# ── 模板：新增六类邮件与主题骨架同款 ─────────────────────────────


@pytest.mark.parametrize(
    "builder",
    [
        pytest.param(
            lambda: notify.wishlist_deal_mail_html([
                {"appid": 620, "name": "Portal 2", "region_code": "AR",
                 "region_name": "阿根廷", "price_text": "¥20.00",
                 "cn_price_text": "¥42.00", "discount": 70},
            ]),
            id="wishlist",
        ),
        pytest.param(
            lambda: notify.bundle_deal_mail_html([
                {"bundle_id": 1, "name": "包", "url": "https://store.steampowered.com/bundle/1/",
                 "region_code": "CN", "region_name": "国区", "price_text": "¥10.00",
                 "cn_price_text": "¥10.00", "discount": 80, "condition": "包内 3 项"},
            ]),
            id="bundle",
        ),
        pytest.param(
            lambda: notify.epic_free_mail_html(
                [{"title": "X", "appid": 620, "cover": "https://img/x.jpg",
                  "url": "https://store.epicgames.com/x", "price_text": "免费",
                  "condition": "免费领取至 2026-01-01"}],
                [{"title": "Y", "appid": 570, "cover": "https://img/y.jpg",
                  "price_text": "即将免费"}],
            ),
            id="epic",
        ),
        pytest.param(
            lambda: notify.hb_choice_mail_html(
                month_label="HB慈善包26年1月包", product_name="January 2026",
                games=[{"appid": 620, "name": "Portal 2", "condition": "Steam 可兑换"}],
            ),
            id="hb_choice",
        ),
        pytest.param(
            lambda: notify.proxy_health_mail_html({
                "reason": "全部代理通道不可用", "available": 0, "total": 3,
                "pool_total": 2, "pool_ok": 0, "clash_running": True,
                "clash_nodes": 10, "clash_ok_nodes": 0,
                "clash_exit_ips": 8, "clash_ok_exit_ips": 0,
                "subscriptions": [{"label": "机场A", "traffic": {
                    "upload": 1, "download": 2, "total": 100, "expire": 1893456000}}],
            }),
            id="proxy_health",
        ),
        pytest.param(
            lambda: notify.system_alert_mail_html(
                title="自动备份失败", summary="本轮备份未完成",
                rows=[("失败原因", "disk full")], level="danger",
                hint="检查磁盘空间",
            ),
            id="system_alert",
        ),
    ],
)
def test_new_mails_share_theme(builder):
    """六类新邮件与存量三封同一深色骨架：品牌头 + 主卡 + 页脚 + 关键色。"""
    html = builder()
    for needle in ("Holdexar", "STEAM 多区价格监控终端",
                   "#66c0f4", "#1b2838", "#0e1a27", "border-radius:14px"):
        assert needle in html, f"{needle} 缺失"


def test_game_card_carries_cover_flag_and_region_link():
    """辨识度三件套：封面 URL、区旗 URL、该区商店直链（?cc=）都进正文。"""
    html = notify.wishlist_deal_mail_html([
        {"appid": 620, "name": "Portal 2", "region_code": "AR",
         "region_name": "阿根廷", "price_text": "¥20.00",
         "cn_price_text": "¥42.00", "discount": 70},
    ])
    # 封面：无 header_image 时按 appid 拼 Steam header
    assert "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/620/header.jpg" in html
    # 区旗 + 该区商店直链
    assert "https://flagcdn.com/w40/ar.png" in html
    assert "https://store.steampowered.com/app/620/?cc=ar" in html


def test_cover_host_is_normalized_to_akamai():
    """历史 CDN 域名的 header_image 在邮件里归一为现行 akamai（与域服务同口径）。"""
    html = notify.bundle_deal_mail_html([
        {"bundle_id": 9, "name": "包 B",
         "header_image": "https://shared.fastly.steamstatic.com/store_item_assets/steam/bundles/9/header.jpg",
         "region_code": "CN", "region_name": "国区", "price_text": "¥9.00", "discount": 60},
    ])
    assert "shared.akamai.steamstatic.com/store_item_assets/steam/bundles/9/header.jpg" in html
    assert "shared.fastly.steamstatic.com" not in html


# ── 监控池折扣速报：范围过滤 + 双重闸 ───────────────────────────


def _price(appid: int, region: str, fen: int, *, discount: int = 0, currency: str = "CNY"):
    return GameCurrentPrice(
        appid=appid, region_code=region, currency=currency,
        price=fen, original_price=fen, discount_percent=discount,
        sub_id=0, price_status="ok", cny_fen=fen, updated_at=NOW,
    )


def _item(appid: int, **kw) -> WishlistItem:
    base = dict(steamid="76561190000000001", appid=appid, active=True, owned=False)
    base.update(kw)
    return WishlistItem(**base)


async def _seed_pool(db):
    """池内四态：愿望单命中 / 榜单发现（应排除）/ 已拥有（应排除）/ 手动入池命中。"""
    async with db() as session:
        session.add_all([
            Game(appid=100, name="G100", header_image="https://img/100.jpg"),
            Game(appid=200, name="G200"),
            Game(appid=300, name="G300"),
            Game(appid=400, name="G400"),
        ])
        session.add_all([
            _item(100, wishlisted=True),
            _item(200, board_pool=True),
            _item(300, wishlisted=True, owned=True),
            _item(400, manual=True),
            _item(500, wishlisted=True, active=False),  # 停用条目：不参与
        ])
        session.add_all([
            _price(100, "CN", 10000, discount=90),
            _price(100, "AR", 2000, discount=90),
            _price(200, "CN", 10000, discount=95),
            _price(300, "CN", 10000, discount=95),
            _price(400, "CN", 5000, discount=50),
            _price(500, "CN", 1000, discount=99),
        ])
        await session.commit()


@pytest.mark.asyncio
async def test_wishlist_deals_scope_and_dedupe(db):
    """只有主动关注条目上榜；榜单/已购/停用被排除；游标按最深折扣记账。"""
    await _seed_pool(db)
    _OWNER["region"] = "ar"

    picks = await alerts_service.check_wishlist_deals()
    assert {p["appid"] for p in picks} == {100, 400}          # 200/300/500 被排除
    by_appid = {p["appid"]: p for p in picks}
    assert by_appid[100]["region_code"] == "AR"               # 号主区优先
    assert by_appid[100]["price_text"] == "¥20.00"
    assert by_appid[100]["cn_price_text"] == "¥100.00"
    assert len(_SENT) == 1 and "监控池折扣速报" in _SENT[0][0]
    cursor = _SENT_STORE[alerts_service.KEY_WISHLIST_DEAL_SENT]
    assert cursor == {"100": 90, "400": 50}
    assert "G100" in _SENT[0][1] and "flagcdn.com/w40/ar.png" in _SENT[0][1]

    # 频率闸：20h 内不再发（即使折扣没变）
    assert await alerts_service.check_wishlist_deals() == []
    assert len(_SENT) == 1


@pytest.mark.asyncio
async def test_wishlist_deals_only_reenlists_on_deeper_discount(db):
    """折扣未加深不再上榜；进一步加深才再发一封。"""
    await _seed_pool(db)
    await alerts_service.check_wishlist_deals()
    _SENT_STORE[alerts_service.KEY_WISHLIST_DEAL_AT] = 0  # 放行频率闸

    assert await alerts_service.check_wishlist_deals() == []  # 折扣未变
    assert len(_SENT) == 1

    async with db() as session:
        row = (
            await session.execute(
                __import__("sqlalchemy").select(GameCurrentPrice).where(
                    GameCurrentPrice.appid == 400,
                    GameCurrentPrice.region_code == "CN",
                )
            )
        ).scalars().first()
        row.discount_percent = 60
        await session.commit()
    _SENT_STORE[alerts_service.KEY_WISHLIST_DEAL_AT] = 0

    picks = await alerts_service.check_wishlist_deals()
    assert [p["appid"] for p in picks] == [400]
    assert len(_SENT) == 2


@pytest.mark.asyncio
async def test_wishlist_deals_skips_when_gate_active(db):
    """20h 频率闸内直接返回，连库都不查（空池也不写游标）。"""
    _SENT_STORE[alerts_service.KEY_WISHLIST_DEAL_AT] = time.time() - 60
    assert await alerts_service.check_wishlist_deals() == []
    assert alerts_service.KEY_WISHLIST_DEAL_SENT not in _SENT_STORE
    assert _SENT == []


# ── 捆绑包精选 ─────────────────────────────────────────────────


def _bundle_row(bid: int, region: str, fen: int, *, discount: int, kind: int = 0):
    return BundleRegionPrice(
        bundle_id=bid, region_code=region, currency="CNY", price=fen,
        original_price=fen, discount_percent=discount, bundle_base_discount=0,
        price_status="ok", cny_fen=fen, app_ids=[1, 2], crawled_at=NOW,
    )


@pytest.mark.asyncio
async def test_bundle_deals_pick_and_cursor(db):
    async with db() as session:
        session.add_all([
            Bundle(bundle_id=1, name="包一", item_kind=0),
            Bundle(bundle_id=2, name="包二", item_kind=0),
            # 包一没有 header_image → 封面回落到包内首款游戏的封面
            Game(appid=1, name="G1", header_image="https://img/1.jpg"),
        ])
        session.add_all([
            _bundle_row(1, "CN", 6000, discount=98),
            _bundle_row(1, "AR", 3000, discount=98),
            _bundle_row(2, "CN", 200, discount=97),   # ¥2 → 低于价格下限，排除
        ])
        await session.commit()

    picks = await alerts_service.check_bundle_deals()
    assert [p["bundle_id"] for p in picks] == [1]
    # 无号主区时按「折扣最深 → 价最低」选区：AR 同折扣更便宜 → 链接带 cc=ar
    assert picks[0]["region_code"] == "AR"
    assert picks[0]["url"].endswith("/bundle/1/?cc=ar")
    # bundle 资产路径拼不出可用封面（该 CDN 路径恒 404）→ 回落包内首款游戏封面
    assert picks[0]["header_image"] == "https://img/1.jpg"
    assert "first_appid" not in picks[0]
    assert len(_SENT) == 1 and "捆绑包精选" in _SENT[0][0]
    assert _SENT_STORE[alerts_service.KEY_BUNDLE_DEAL_SENT] == {"1": 98}

    assert await alerts_service.check_bundle_deals() == []  # 频率闸


@pytest.mark.asyncio
async def test_bundle_cover_falls_back_to_steam_header(db):
    """包内游戏不在库内时，封面仍要有图：拼 Steam header（不猜 bundle CDN）。"""
    async with db() as session:
        session.add(Bundle(bundle_id=7, name="包七", item_kind=0))
        session.add(_bundle_row(7, "CN", 5000, discount=90))
        await session.commit()

    picks = await alerts_service.check_bundle_deals()
    assert picks[0]["header_image"] == (
        "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/1/header.jpg"
    )


# ── Epic 喜加一 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_epic_free_tracks_current_only(db, monkeypatch):
    from app.domains.metadata import service as metadata_service

    async def _offers():
        return {"ok": True, "offers": [
            {"title": "FreeA", "titleCn": "免费A", "appid": 100, "upcoming": False,
             "end": "2026-01-08", "image": "https://img/a.jpg", "url": "https://epic/a"},
            {"title": "SoonB", "appid": 200, "upcoming": True,
             "start": "2026-01-08", "image": "https://img/b.jpg", "url": "https://epic/b"},
        ]}

    monkeypatch.setattr(metadata_service, "epic_free_offers", _offers)
    picks = await alerts_service.check_epic_free()
    assert [p["appid"] for p in picks] == [100]
    assert "Epic 喜加一" in _SENT[0][0]
    assert "免费领取至 2026-01-08" in _SENT[0][1]
    # 记账只含当期条目：预告转正后才会自己发一封
    assert _SENT_STORE[alerts_service.KEY_EPIC_FREE_SENT] == ["appid:100"]

    assert await alerts_service.check_epic_free() == []  # 已通报过
    assert len(_SENT) == 1


@pytest.mark.asyncio
async def test_epic_free_no_offers_when_channel_down(db, monkeypatch):
    from app.domains.metadata import service as metadata_service

    async def _offers():
        return {"ok": False, "offers": []}

    monkeypatch.setattr(metadata_service, "epic_free_offers", _offers)
    assert await alerts_service.check_epic_free() == []
    assert _SENT == []


# ── Humble Choice 当月包 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_hb_choice_month_cursor(db):
    async with db() as session:
        session.add(Game(appid=100, name="G100", header_image="https://img/100.jpg"))
        await session.commit()

    result = {
        "recorded": True, "label": "HB慈善包26年1月包", "productName": "January 2026",
        "marked": [{"appid": 100, "title": "G100"}],
    }
    games = await alerts_service.check_hb_choice(result)
    assert [g["name"] for g in games] == ["G100"]
    assert "Humble Choice" in _SENT[0][0]
    assert _SENT_STORE[alerts_service.KEY_HB_CHOICE_SENT] == result["label"]

    assert await alerts_service.check_hb_choice(result) == []  # 同月不重发
    # 未结案（有未解析条目）不发信
    assert await alerts_service.check_hb_choice({**result, "recorded": False}) == []
    assert len(_SENT) == 1


# ── 代理通道 / 系统告警：冷却与「未配置不算异常」────────────────


@pytest.mark.asyncio
async def test_proxy_alert_skips_when_no_channel(db, monkeypatch):
    from app.domains.proxies import service as proxies_service

    async def _stats():
        return {"pool": {"total": 0, "ok": 0},
                "clash": {"running": False, "nodes": 0, "okNodes": 0,
                          "exitIps": 0, "okExitIps": 0},
                "available": 0, "total": 0}

    monkeypatch.setattr(proxies_service, "pool_stats", _stats)
    assert await alerts_service.check_proxy_health() is False
    assert _SENT == []


@pytest.mark.asyncio
async def test_proxy_alert_sends_then_cools_down(db, monkeypatch):
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
    assert "代理通道告警" in _SENT[0][0]
    body = _SENT[0][1]
    assert "全部代理通道不可用" in body and "机场A" in body
    assert "https://secret/sub" not in body, "订阅 URL 是凭据，绝不能进邮件正文"

    assert await alerts_service.check_proxy_health() is False  # 12h 冷却
    assert len(_SENT) == 1


@pytest.mark.asyncio
async def test_system_alert_cooldown_per_kind(db, monkeypatch):
    assert await alerts_service.send_system_alert(
        kind="backup", title="自动备份失败", summary="未完成",
        rows=[("失败原因", "disk full")],
    ) is True
    assert "系统告警：自动备份失败" in _SENT[0][0]
    # 同 kind 冷却内不再发；不同 kind 互不影响
    assert await alerts_service.send_system_alert(
        kind="backup", title="自动备份失败", summary="未完成",
    ) is False
    assert len(_SENT) == 1
    store = _SENT_STORE[alerts_service.KEY_SYS_ALERT_AT]
    store["backup"] = time.time() - 25 * 3600  # 过冷却
    _SENT_STORE[alerts_service.KEY_SYS_ALERT_AT] = store
    assert await alerts_service.send_system_alert(
        kind="backup", title="自动备份失败", summary="未完成",
    ) is True
    assert len(_SENT) == 2


# ── 爬取任务失败告警：接线（任务状态仍要诚实收敛为 failed）────────


@pytest.mark.asyncio
async def test_crawl_job_failure_triggers_system_alert(monkeypatch):
    import asyncio

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
    assert "42" in dict(alerts[0]["rows"])["任务 ID"]