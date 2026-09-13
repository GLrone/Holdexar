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
                "productSlug": "live-game/home",
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
                "productSlug": None, "urlSlug": "upcoming-game",
                "price": {"totalPrice": {"fmtPrice": {"originalPrice": "¥68.00"}}},
                "promotions": {"promotionalOffers": [], "upcomingPromotionalOffers": [
                    {"promotionalOffers": [_offer_payload(2, 9, 0)]},
                ]},
            },
            # 打折 20% 不是白送，必须排除
            {
                "id": "elem-off", "title": "Discounted",
                "promotions": {"promotionalOffers": [
                    {"promotionalOffers": [_offer_payload(-3, 4, 20)]},
                ]},
            },
        ]}}}}
    games = parse_free_games(payload)
    assert [g.title for g in games] == ["Live Game", "Upcoming Game"]
    live, up = games
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


def test_parse_gamerpower_mobile():
    """GamerPower 判别式：App 表述 + Game + 有效截止日；PC Epic 限免不误收。"""
    from app.crawler.epic_free import parse_gamerpower_mobile

    payload = [
        {
            "title": "Alone With You (Mobile) Giveaway", "status": "Active", "type": "Game",
            "description": "Alone With You (Mobile version) is free until September 17 "
                           "on the Epic Games Store App for iPhone, iPad, and Android.",
            "image": "https://gp/img.jpg", "open_giveaway_url": "https://gp/open/x",
            "published_date": "2026-09-10 11:19:31", "end_date": "2026-09-17 23:59:00",
            "worth": "$4.99",
        },
        # PC 端 Epic 限免（描述无 App 字样）不误收
        {
            "title": "Some Game (Epic Games) Giveaway", "status": "Active", "type": "Game",
            "description": "Claim it via Epic Games Store!", "open_giveaway_url": "https://gp/open/y",
            "published_date": "2026-09-11 09:00:00", "end_date": "2026-09-18 23:59:00",
        },
        # key 赠品：无有效截止日不收
        {
            "title": "Keys Giveaway", "status": "Active", "type": "Game",
            "description": "Keys for the Epic Games Store App users.",
            "open_giveaway_url": "https://gp/open/z",
            "published_date": "2026-09-12 09:00:00", "end_date": "N/A",
        },
        # 非在送不收
        {
            "title": "Expired (Mobile) Giveaway", "status": "Expired", "type": "Game",
            "description": "Was free on the Epic Games Store App.",
            "open_giveaway_url": "https://gp/open/w", "end_date": "2026-09-01 00:00:00",
        },
    ]
    out = parse_gamerpower_mobile(payload)
    assert out == {
        "title": "Alone With You", "image": "https://gp/img.jpg",
        "url": "https://gp/open/x", "end": "2026-09-17", "worth": "$4.99",
    }
    assert parse_gamerpower_mobile([]) is None
    assert parse_gamerpower_mobile({"error": 1}) is None


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


@pytest.mark.asyncio
async def test_resolve_store_page_url(monkeypatch):
    """标题 → 商品页 URL：搜索首条 offerId/sandboxId → catalogNs.pageSlug。"""
    from app.crawler import epic_free as ef

    async def fake_persisted(op, variables, sha):
        if op == "primarySearchAutocomplete":
            assert variables["keywords"] == "Some Game"
            return {"data": {"Catalog": {"searchStore": {"elements": [
                {"offerId": "off" * 8, "sandboxId": "sb" * 16, "title": "Some Game"},
            ]}}}}
        assert op == "getCatalogOffer"
        assert variables["offerId"] == "off" * 8
        return {"data": {"Catalog": {"catalogOffer": {
            "catalogNs": {"mappings": [{"pageSlug": "some-game-android-52b29b"}]},
            "keyImages": [
                {"type": "Thumbnail", "url": "https://cdn/thumb.jpg"},
                {"type": "OfferImageWide", "url": "https://cdn/wide.jpg"},
            ],
        }}}}

    monkeypatch.setattr(ef, "_egs_persisted", fake_persisted)
    page = await ef.resolve_store_page_url("Some Game")
    assert page == {
        "url": "https://store.epicgames.com/p/some-game-android-52b29b",
        "image": "https://cdn/wide.jpg",
    }


@pytest.mark.asyncio
async def test_resolve_store_page_url_none_paths(monkeypatch):
    """搜索无命中/缺 slug 返回 None（调用方回落 open 链）。"""
    from app.crawler import epic_free as ef

    async def no_hit(op, variables, sha):
        return {"data": {"Catalog": {"searchStore": {"elements": []}}}}

    monkeypatch.setattr(ef, "_egs_persisted", no_hit)
    assert await ef.resolve_store_page_url("Unknown") is None

    async def no_slug(op, variables, sha):
        if op == "primarySearchAutocomplete":
            return {"data": {"Catalog": {"searchStore": {"elements": [
                {"offerId": "x", "sandboxId": "y"},
            ]}}}}
        return {"data": {"Catalog": {"catalogOffer": {
            "keyImages": [{"type": "OfferImageWide", "url": "https://cdn/w2.jpg"}],
        }}}}

    monkeypatch.setattr(ef, "_egs_persisted", no_slug)
    # 无 slug 但有官方封面：仍返回（url 缺由调用方回落 open 链）
    assert await ef.resolve_store_page_url("Whatever") == {
        "url": None, "image": "https://cdn/w2.jpg",
    }


@pytest.mark.asyncio
async def test_resolve_mobile_checkout(monkeypatch):
    """标题 → 结账直链：android/ios 两端 Claim 合并成单条 offers 查询串。"""
    from app.crawler import epic_free as ef

    async def fake_persisted(op, variables, sha):
        return {"data": {"Catalog": {"searchStore": {"elements": [
            {"offerId": "pc-offer", "sandboxId": "ns123", "title": "X"},
        ]}}}}

    def fake_offers(namespace, platform):
        assert namespace == "ns123"
        if platform == "android":
            return [{"content": {"title": "X", "purchase": [{
                "purchaseType": "Claim",
                "price": {"decimalPrice": 0},
                "purchasePayload": {"offerId": "and-offer", "sandboxId": "ns123"},
                "discount": {"discountEndDate": "2026-09-17T15:00:00.000Z"},
            }]}}]
        return [{"content": {"purchase": [
            {"purchaseType": "Purchase", "price": {"decimalPrice": 499},
             "purchasePayload": {"offerId": "ios-paid", "sandboxId": "ns123"}},
            {"purchaseType": "Claim", "price": {"decimalPrice": "0"},
             "purchasePayload": {"offerId": "ios-free", "sandboxId": "ns123"}},
        ]}}]

    monkeypatch.setattr(ef, "_egs_persisted", fake_persisted)
    monkeypatch.setattr(ef, "_sandbox_offers_sync", fake_offers)
    out = await ef.resolve_mobile_checkout("X")
    assert out["url"] == ("https://store.epicgames.com/purchase"
                          "?offers=1-ns123-and-offer&offers=1-ns123-ios-free")
    assert out["end"] == "2026-09-17"


@pytest.mark.asyncio
async def test_resolve_mobile_checkout_none(monkeypatch):
    """无 Claim 条目 / 搜索无命中 → None（调用方回落商品页）。"""
    from app.crawler import epic_free as ef

    async def fake_persisted(op, variables, sha):
        return {"data": {"Catalog": {"searchStore": {"elements": [
            {"offerId": "pc", "sandboxId": "ns", "title": "X"},
        ]}}}}

    monkeypatch.setattr(ef, "_egs_persisted", fake_persisted)
    monkeypatch.setattr(ef, "_sandbox_offers_sync", lambda ns, p: [])
    assert await ef.resolve_mobile_checkout("X") is None

    async def no_hit(op, variables, sha):
        return {"data": {"Catalog": {"searchStore": {"elements": []}}}}

    monkeypatch.setattr(ef, "_egs_persisted", no_hit)
    assert await ef.resolve_mobile_checkout("Y") is None
