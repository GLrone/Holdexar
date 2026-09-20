"""Epic 喜加一自动链与外部名单导入验收（refresh_epic_free / import_epic_list）。

纯逻辑：日期归一变体。链路：合成促销端点结果（fetch_free_games 打桩），
走真实标记落库（合成 990xxx appid 播种 + 清理，不污染真实库），验证：
- 缺行占位 updated_at NULL（回补池判据），is_epic/epic_date 直接落位；
- 已有行补空白日期；已标记行既有 epic_date 不覆写（首见日期优先）；
- 未解析条目（storesearch 未命中）进 unresolved 不阻塞其余；
- 幂等：二次运行零新标；外部名单日期变体归一 / 非法条目跳过。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, init_db  # noqa: E402
from app.crawler.epic_free import EpicFreeGame  # noqa: E402
from app.domains.games.models import Game  # noqa: E402
from app.domains.metadata import service as epic  # noqa: E402

APP_NEW = 990_501      # 缺行占位
APP_PLAIN = 990_502    # 已有行，未标记
APP_DATED = 990_503    # 已标记且带历史日期（不覆写判据）
APP_LIST_NEW = 990_504  # 外部名单导入的占位行
OLD_DATE = "2020-1-1"


def _game(title: str, appid: int | None, free_start: str = "2026-9-10") -> EpicFreeGame:
    return EpicFreeGame(
        title=title, title_cn="", appid=appid,
        free_start=free_start, offer_type="FREE_PRODUCT", upcoming=False,
    )


def _now():
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().replace(tzinfo=None)


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    async with get_session_factory()() as session:
        session.add(Game(appid=APP_PLAIN, name="Plaintest", created_at=_now()))
        session.add(
            Game(
                appid=APP_DATED, name="Datedtest",
                is_epic=True, epic_date=OLD_DATE, created_at=_now(),
            )
        )
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(Game).where(
                Game.appid.in_([APP_NEW, APP_PLAIN, APP_DATED, APP_LIST_NEW])
            )
        )
        await session.commit()


# ─── 纯逻辑 ────────────────────────────────────────────────────────


def test_norm_epic_date_variants():
    assert epic._norm_epic_date("2026-5-28") == "2026-5-28"
    assert epic._norm_epic_date("2026-05-08") == "2026-5-8"
    assert epic._norm_epic_date("2026.5.28") == "2026-5-28"
    assert epic._norm_epic_date("2026/5/28") == "2026-5-28"
    assert epic._norm_epic_date("2026年5月28日") == "2026-5-28"
    # 区间串取首日期（起始日语义）
    assert epic._norm_epic_date("2026-5-28 ~ 2026-6-4") == "2026-5-28"
    assert epic._norm_epic_date("2026-13-01") is None  # 月越界
    assert epic._norm_epic_date("2026-5-32") is None  # 日越界
    assert epic._norm_epic_date("") is None
    assert epic._norm_epic_date("免费送过") is None


# ─── 展示链（仪表盘卡片）────────────────────────────────────────────


def _iso_offset(days: float) -> str:
    """距今 days 天的 ISO 时间戳：测试不锚死日期，跨期不腐。"""
    from datetime import datetime, timedelta, timezone

    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _day(days: float) -> str:
    """库内日期格式（YYYY-M-D 无前导零）。"""
    from datetime import datetime, timedelta, timezone

    dt = datetime.now(timezone.utc) + timedelta(days=days)
    return f"{dt.year}-{dt.month}-{dt.day}"


def _offer_payload(start_days: float, end_days: float, pct: int) -> dict:
    return {
        "startDate": _iso_offset(start_days),
        "endDate": _iso_offset(end_days),
        "discountSetting": {"discountPercentage": pct},
    }


def test_parse_free_games_rich_fields():
    """解析出展示字段：封面按类型优先、商店页 slug 剥 /home、原价、起止日。"""
    from app.crawler.epic_free import parse_free_games

    payload = {
        "data": {"Catalog": {"searchStore": {"elements": [
            {
                "id": "elem-live", "title": "Live Game", "offerType": "BASE_GAME",
                "namespace": "ns-live", "productSlug": "live-game/home",
                "keyImages": [
                    {"type": "Thumbnail", "url": "https://cdn/thumb.jpg"},
                    {"type": "OfferImageWide", "url": "https://cdn/wide.jpg"},
                ],
                "price": {"totalPrice": {"fmtPrice": {"originalPrice": "$19.99"}}},
                "promotions": {"promotionalOffers": [
                    {"promotionalOffers": [_offer_payload(-3, 4, 0)]},
                ], "upcomingPromotionalOffers": []},
            },
            {
                "id": "elem-up", "title": "Upcoming Game", "offerType": "BASE_GAME",
                "namespace": "ns-up", "productSlug": None, "urlSlug": "upcoming-game",
                "price": {"totalPrice": {"fmtPrice": {"originalPrice": "¥68.00"}}},
                "promotions": {"promotionalOffers": [], "upcomingPromotionalOffers": [
                    {"promotionalOffers": [_offer_payload(2, 9, 0)]},
                ]},
            },
            # 打折 20% 不是白送，必须排除
            {
                "id": "elem-off", "title": "Discounted", "namespace": "ns-off",
                "promotions": {"promotionalOffers": [
                    {"promotionalOffers": [_offer_payload(-3, 4, 20)]},
                ]},
            },
        ]}}}}
    games = parse_free_games(payload)
    assert [g.title for g in games] == ["Live Game", "Upcoming Game"]
    live, up = games
    assert live.namespace == "ns-live" and up.namespace == "ns-up"
    assert live.image == "https://cdn/wide.jpg"  # OfferImageWide 优先于 Thumbnail
    assert live.url == "https://store.epicgames.com/en-US/p/live-game"
    assert live.price_original == "$19.99"
    assert live.free_start == _day(-3)
    assert live.free_end == _day(4)
    assert live.upcoming is False and up.upcoming is True


def test_store_url_fallbacks():
    """商店页三级回退：productSlug → 目录页 slug → urlSlug → 总览页兜底。"""
    from app.crawler.epic_free import _store_url

    assert _store_url({"catalogNs": {"mappings": [{"pageSlug": "page-slug"}]}}) \
        == "https://store.epicgames.com/en-US/p/page-slug"
    assert _store_url({"urlSlug": "abc123"}) \
        == "https://store.epicgames.com/en-US/p/abc123"
    assert _store_url({}) == "https://store.epicgames.com/en-US/free-games"


def test_parse_mobile_breaker():
    """CMS 移动页只认 Free Giveaway breaker；缺图/结构变更返回 None。"""
    from app.crawler.epic_free import parse_mobile_breaker

    payload = {"layout": {"section": [
        {"breakers": {"breakerList": [
            {"title": "Earn up to 20%", "backgroundImage": "https://cdn/rewards.jpg"},
            {"title": "Free Giveaway", "backgroundImage": "https://cdn/breaker.jpg"},
        ]}},
    ]}}
    assert parse_mobile_breaker(payload) == {
        "image": "https://cdn/breaker.jpg",
        "url": "https://store.epicgames.com/en-US/mobile",
    }
    assert parse_mobile_breaker({"layout": {}}) is None  # 无 breaker
    assert parse_mobile_breaker({}) is None  # 空结构


def _mobile_game(title: str, namespace: str, *, upcoming: bool = False,
                 title_cn: str = "", image: str = "", price: str = "") -> EpicFreeGame:
    return EpicFreeGame(
        title=title, title_cn=title_cn, appid=None,
        free_start="2026-9-17", offer_type="BASE_GAME", upcoming=upcoming,
        namespace=namespace, image=image, price_original=price,
    )


def _claim_row(namespace: str, offer: str, end: str) -> dict:
    return {"content": {"purchase": [{
        "purchaseType": "Claim", "price": {"decimalPrice": 0},
        "purchasePayload": {"offerId": offer, "sandboxId": namespace},
        "discount": {"discountEndDate": f"{end}T15:00:00.000Z"},
    }]}}


@pytest.mark.asyncio
async def test_resolve_mobile_freebie(monkeypatch):
    """促销元素 → 移动白送：Claim 判别 + 双端合并直链 + 截止日/原价/中文名。"""
    from app.crawler import epic_free as ef

    probed: list[tuple[str, str]] = []

    def fake_offers(namespace, platform):
        probed.append((namespace, platform))
        if namespace != "ns-live":
            return []
        purchase: list[dict] = [
            # 付费条目混在前面也不影响 Claim 判别
            {"purchaseType": "Purchase", "price": {"decimalPrice": 1999},
             "purchasePayload": {"offerId": "paid", "sandboxId": "ns-live"}},
        ]
        offer = "and-offer" if platform == "android" else "ios-offer"
        purchase.append(_claim_row(namespace, offer, "2026-09-24")
                        ["content"]["purchase"][0])
        return [{"content": {"purchase": purchase}}]

    monkeypatch.setattr(ef, "_sandbox_offers_sync", fake_offers)
    games = [
        _game("No Namespace", None),  # 无 namespace 不探测
        _mobile_game("Upcoming", "ns-up", upcoming=True),  # 预告不探测
        _mobile_game("Other", "ns-other"),  # 无 Claim 跳过
        _mobile_game("Live", "ns-live", title_cn="真在送",
                     image="https://cdn/wide.jpg", price="$19.99"),
    ]
    out = await ef.resolve_mobile_freebie(games)
    assert out == {
        "title": "真在送",  # 中文名优先（无则回落英文）
        "image": "https://cdn/wide.jpg",
        "url": ("https://store.epicgames.com/purchase"
                "?offers=1-ns-live-and-offer&offers=1-ns-live-ios-offer"),
        "end": "2026-09-24",
        "worth": "$19.99",
    }
    # 预告与无 namespace 元素不发起探测
    assert ("ns-up", "android") not in probed
    assert all(ns for ns, _ in probed)


@pytest.mark.asyncio
async def test_resolve_mobile_freebie_latest_end(monkeypatch):
    """多元素同时带 Claim：取截止日最晚者（排除上一期的尾巴）。"""
    from app.crawler import epic_free as ef

    def fake_offers(namespace, platform):
        ends = {"ns-old": "2026-09-17", "ns-new": "2026-09-24"}
        end = ends.get(namespace)
        return [_claim_row(namespace, f"{namespace}-offer", end)] if end else []

    monkeypatch.setattr(ef, "_sandbox_offers_sync", fake_offers)
    games = [
        _mobile_game("Old", "ns-old"),
        _mobile_game("New", "ns-new"),
    ]
    out = await ef.resolve_mobile_freebie(games)
    assert out["title"] == "New"
    assert out["end"] == "2026-09-24"


@pytest.mark.asyncio
async def test_resolve_mobile_freebie_none(monkeypatch):
    """全无 Claim / 空元素列表 → None（调用方降级 breaker）。"""
    from app.crawler import epic_free as ef

    monkeypatch.setattr(ef, "_sandbox_offers_sync", lambda ns, p: [])
    games = [_mobile_game("X", "ns-x")]
    assert await ef.resolve_mobile_freebie(games) is None
    assert await ef.resolve_mobile_freebie([]) is None


# ─── 自动链 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_marks_and_preserves(monkeypatch):
    """窗口标记：占位/补标/日期保护三形态 + 未解析不阻塞；重跑幂等。"""

    async def fake_fetch(proxy=None, session=None):
        return [
            _game("Newtest", APP_NEW),
            _game("Plaintest", APP_PLAIN),
            _game("Datedtest", APP_DATED),
            _game("Missingtest", None),
        ]

    monkeypatch.setattr(epic, "fetch_free_games", fake_fetch)

    first = await epic.refresh_epic_free()
    assert first["ok"] is True
    assert first["window"] == 4
    assert {m["appid"] for m in first["marked"]} == {APP_NEW, APP_PLAIN}
    assert [u["title"] for u in first["unresolved"]] == ["Missingtest"]

    async with get_session_factory()() as session:
        new_row = await session.get(Game, APP_NEW)
        assert new_row is not None and new_row.is_epic is True
        assert new_row.epic_date == "2026-9-10"
        assert new_row.updated_at is None  # 占位行进回补池

        plain = await session.get(Game, APP_PLAIN)
        assert plain.is_epic is True
        assert plain.epic_date == "2026-9-10"

        dated = await session.get(Game, APP_DATED)
        assert dated.epic_date == OLD_DATE  # 首见日期不覆写

    # 幂等：二次运行零新标
    second = await epic.refresh_epic_free()
    assert second["marked"] == []


@pytest.mark.asyncio
async def test_refresh_empty_window_not_ok(monkeypatch):
    """促销端点空窗口 → ok=False（调用方按失败处理，不误报成功）。"""

    async def fake_fetch(proxy=None, session=None):
        return []

    monkeypatch.setattr(epic, "fetch_free_games", fake_fetch)
    result = await epic.refresh_epic_free()
    assert result["ok"] is False
    assert "error" in result


# ─── 外部名单 ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_epic_list_normalizes_and_marks():
    result = await epic.import_epic_list([
        {"appid": APP_LIST_NEW, "free_start": "2026年5月28日", "title": "Listtest"},
        {"appid": APP_PLAIN, "free_start": "2026-05-08"},  # 补标 + 归一
        {"appid": APP_DATED, "free_start": "2026-9-10"},    # 日期已存不覆写
        {"appid": "not-a-number"},                          # 非法跳过
        {"appid": APP_LIST_NEW},                            # 重复条目幂等
    ])
    assert result["ok"] is True
    assert result["received"] == 5
    assert result["skipped"] == 1
    # 重复条目第二次不再写；日期已存的不进 marked
    assert {m["appid"] for m in result["marked"]} == {APP_LIST_NEW, APP_PLAIN}

    async with get_session_factory()() as session:
        listed = await session.get(Game, APP_LIST_NEW)
        assert listed is not None and listed.is_epic is True
        assert listed.epic_date == "2026-5-28"  # 年月日形态归一
        assert listed.updated_at is None

        plain = await session.get(Game, APP_PLAIN)
        assert plain.is_epic is True
        assert plain.epic_date == "2026-5-8"  # 前导零剥除

        dated = await session.get(Game, APP_DATED)
        assert dated.epic_date == OLD_DATE


# ─── 展示链缓存：落库快照 + 后台刷新（stale-while-revalidate）─────────
# 仪表盘卡片语义：冷启动先回落库快照（stale）+ 后台刷新覆盖，避免每次
# 启动首开都等一轮完整抓取。落库键走 app_settings KV，夹具清账防串场。


def _offers_payload(title: str = "Cached Game") -> dict:
    return {
        "source": "epic-offers", "ok": True,
        "offers": [{
            "title": title, "titleCn": "", "appid": None,
            "start": "2026-9-10", "end": "2026-9-17", "upcoming": False,
            "image": "https://cdn/x.jpg",
            "url": "https://store.epicgames.com/en-US/p/x",
            "priceOriginal": "$9.99",
        }],
        "mobile": None,
        "fetchedAt": "2026-09-15T10:00:00",
    }


@pytest_asyncio.fixture(autouse=True)
async def _reset_offers_cache():
    """展示链缓存跨用例隔离：内存复位 + 落库键清账 + 后台任务收尾。"""
    from app.domains.settings import service as settings_service

    await init_db()
    epic._epic_offers_cache.update({"at": 0.0, "payload": None})
    epic._epic_offers_refreshing = False
    await settings_service.delete_value(epic._EPIC_OFFERS_CACHE_KEY)
    yield
    task = epic._epic_offers_refresh_task
    if task is not None and not task.done():
        await task
    epic._epic_offers_cache.update({"at": 0.0, "payload": None})
    epic._epic_offers_refreshing = False
    await settings_service.delete_value(epic._EPIC_OFFERS_CACHE_KEY)


@pytest.mark.asyncio
async def test_offers_cold_start_serves_snapshot_then_refreshes(monkeypatch):
    """冷启动（内存空）立即回落库快照（stale=True）+ 后台刷新；刷新完成覆盖。"""
    import time as _time

    from app.domains.settings import service as settings_service

    await settings_service.set_value(
        epic._EPIC_OFFERS_CACHE_KEY,
        {"fetched_at": _time.time() - epic._EPIC_OFFERS_TTL_SECONDS * 2,
         "payload": _offers_payload("Old Cached")},
    )

    async def fake_fetch_payload():
        return _offers_payload("Fresh From Net")

    monkeypatch.setattr(epic, "_fetch_offers_payload", fake_fetch_payload)

    first = await epic.epic_free_offers()
    assert first["ok"] is True
    assert first["cached"] is True and first["stale"] is True
    assert first["offers"][0]["title"] == "Old Cached"  # 先显旧快照，不等网络

    await epic._epic_offers_refresh_task  # 后台刷新收尾

    second = await epic.epic_free_offers()
    assert second["stale"] is False
    assert second["offers"][0]["title"] == "Fresh From Net"
    # 落库同步更新：下次冷启动读到的就是新数据
    snap = await settings_service.get_value(epic._EPIC_OFFERS_CACHE_KEY)
    assert snap["payload"]["offers"][0]["title"] == "Fresh From Net"


@pytest.mark.asyncio
async def test_offers_fresh_memory_cache_skips_network(monkeypatch):
    """内存缓存新鲜（< TTL）：直接返回、零抓取。"""
    import time as _time

    calls = {"n": 0}

    async def fake_fetch_payload():
        calls["n"] += 1
        return _offers_payload()

    monkeypatch.setattr(epic, "_fetch_offers_payload", fake_fetch_payload)
    epic._epic_offers_cache.update(
        {"at": _time.time(), "payload": _offers_payload("Hot")}
    )

    res = await epic.epic_free_offers()
    assert res["cached"] is True and res["stale"] is False
    assert res["offers"][0]["title"] == "Hot"
    assert calls["n"] == 0


@pytest.mark.asyncio
async def test_offers_first_fetch_writes_snapshot(monkeypatch):
    """全新安装首开（两层缓存全空）：现拉并落库快照，供下次冷启动直出。"""
    from app.domains.settings import service as settings_service

    async def fake_fetch_payload():
        return _offers_payload("First")

    monkeypatch.setattr(epic, "_fetch_offers_payload", fake_fetch_payload)

    res = await epic.epic_free_offers()
    assert res["ok"] is True and res["offers"][0]["title"] == "First"
    assert res.get("cached") is not True

    snap = await settings_service.get_value(epic._EPIC_OFFERS_CACHE_KEY)
    assert snap is not None
    assert snap["payload"]["offers"][0]["title"] == "First"
    assert isinstance(snap["fetched_at"], float)


@pytest.mark.asyncio
async def test_offers_pc_failure_not_snapshotted(monkeypatch):
    """PC 列表失败（只出移动卡）不落快照——下次启动重试 Epic 主数据。"""
    from app.domains.settings import service as settings_service

    async def fake_fetch_payload():
        return {
            "source": "epic-offers", "ok": True, "offers": [],
            "mobile": {
                "title": "M", "image": "https://cdn/m.jpg", "url": "https://x",
                "end": None, "worth": None, "source": "breaker",
            },
            "fetchedAt": "2026-09-15T10:00:00",
        }

    monkeypatch.setattr(epic, "_fetch_offers_payload", fake_fetch_payload)

    res = await epic.epic_free_offers()
    assert res["ok"] is True and res["mobile"] is not None
    assert await settings_service.get_value(epic._EPIC_OFFERS_CACHE_KEY) is None
