"""HB 当月包游戏侧标记链验收（refresh_hb_choice，无账本表）。

纯逻辑：月份标签换算 / 页面解析（RSC flight 流 + subscriptionProducts +
平台交付分类）/ storesearch 命中匹配。链路：合成 membership 页 + 合成解析
结果，走真实标记落库（合成 990xxx appid 播种 + 清理，不污染真实库），验证：
- steam 条目打标（is_hb + hb_data 月份标签），非 steam 条目跳过；
- 缺行占位 updated_at NULL（回补池判据），已有行保留旧标记并逗号续写；
- 解析失败条目进 unresolved 不阻塞其余条目；
- machine_name 未变时幂等跳过（app_settings 记账）；
- 补价通道：无任何价格行的标记行进首爬候选，已有价格行（missing 状态）
  的归补抓账本不进，空候选不发起爬取。
"""
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, init_db  # noqa: E402
from app.domains.games.models import Game, GameCurrentPrice  # noqa: E402
from app.domains.metadata import service as hb  # noqa: E402
from app.domains.settings.models import AppSetting  # noqa: E402

TEST_STATE_KEY = "hb_choice_last_month_test"
MACHINE = "september_2026_choice"
PRODUCT = "September 2026"
LABEL = "HB慈善包26年9月包"

APP_NEW = 990_401
APP_EXISTING = 990_402
APP_UNRESOLVED = 990_403
OLD_LABEL = "HB慈善包25年3月包"

_RESOLVE_MAP = {
    "Frosttest 2": APP_NEW,
    "Keytest": APP_EXISTING,
    "Missing Game": None,
}


def _product(title: str, delivery: list[str], cent_amount: float | None = 1999) -> dict:
    product = {
        "id": title.lower().replace(" ", "-"),
        "title": title,
        "deliveryMethods": delivery,
        "endDate": "2026-10-06T17:00:00",
        "thumbnail": f"https://hb.test/{title.lower().replace(' ', '-')}.jpg",
    }
    if cent_amount is not None:
        product["displayPrice"] = {"currentPrice": {"centAmount": cent_amount}}
    return product


def _membership_html() -> str:
    """合成 membership 页（RSC 形态）：flight 流分段内嵌
    subscriptionProducts 清单与期号描述，清单劈两段验证跨段拼接，重复 key
    首见生效；含字符串内嵌花括号与中括号的条目（验证括号配平扫描抗性）。"""
    products = [
        _product("Frosttest 2", ["steam"]),
        _product("Keytest", ["steam"]),
        _product("GOGtest Odyssey", ["gog"]),
        _product("Epictest Rider", ["epic", "external_key"]),
        _product("Plus } {Test [x]", ["external_key"], cent_amount=None),
        _product("Missing Game", ["steam"]),
    ]
    payload = (
        "Get September 2026 Humble Choice - monthly games. "
        '"subscriptionProducts": ' + json.dumps(products, ensure_ascii=False) + " "
        '"subscriptionProducts": ' + json.dumps(products[:1], ensure_ascii=False)
    )

    def _js_escape(text: str) -> str:
        return json.dumps(text)[1:-1]

    mid = len(payload) // 2
    chunk_a, chunk_b = payload[:mid], payload[mid:]
    return (
        "<html><head>"
        '<meta name="description" content="Get September 2026 Humble Choice">'
        "</head><body>"
        f"<script>self.__next_f.push([1,\"{_js_escape(chunk_a)}\"])</script>"
        f"<script>self.__next_f.push([1,\"{_js_escape(chunk_b)}\"])</script>"
        "</body></html>"
    )




def test_month_label_from_product_name():
    assert hb._choice_month_label(MACHINE, PRODUCT) == LABEL


def test_month_label_from_machine_name():
    assert hb._choice_month_label("july_2026_choice", None) == "HB慈善包26年7月包"


def test_month_label_fallback_generic():
    assert hb._choice_month_label("whatever", "Mystery Box") == "HB慈善包"


def test_parse_choice_page_products_and_dedup():
    parsed = hb._parse_choice_page(_membership_html())
    assert parsed["machineName"] == MACHINE
    assert parsed["productName"] == PRODUCT
    assert parsed["endDate"] == "2026-10-06T17:00:00"
    assert parsed["steamTitles"] == ["Frosttest 2", "Keytest", "Missing Game"]
    assert parsed["platformGames"] == [
        {"title": "GOGtest Odyssey", "platform": "gog",
         "thumbnail": "https://hb.test/gogtest-odyssey.jpg",
         "retailFen": 1999, "endDate": "2026-10-06T17:00:00"},
        {"title": "Epictest Rider", "platform": "epic",
         "thumbnail": "https://hb.test/epictest-rider.jpg",
         "retailFen": 1999, "endDate": "2026-10-06T17:00:00"},
    ]


def test_pick_appid_matching_tiers():
    hits = [
        {"name": "Something Else", "id": "111"},
        {"name": "Keytest: Deluxe Edition", "id": "222"},
        {"name": "KEYTEST", "id": "333"},
    ]
    assert hb._pick_appid("Keytest", hits) == 333
    assert hb._pick_appid("keytest !", hits) == 222
    assert hb._pick_appid("Nothing", hits) is None


def test_pick_appid_playtest_normalized():
    hits = [{"name": "Eldtest Escape", "id": "444"}, {"name": "Eldtest Escape Demo", "id": "445"}]
    # Humble 给测试键：页内标题带 Playtest，商店侧无该条目 → 剥词对准本体
    assert hb._pick_appid("Eldtest Escape Playtest", hits) == 444


def test_month_page_url_from_machine_name():
    assert hb._month_page_url("september_2026_choice") == \
        "https://www.humblebundle.com/membership/September-2026"
    assert hb._month_page_url("july_2026_choice") == \
        "https://www.humblebundle.com/membership/July-2026"
    assert hb._month_page_url("whatever") == hb._MEMBERSHIP_URL
    assert hb._month_page_url(None) == hb._MEMBERSHIP_URL


@pytest.mark.asyncio
async def test_offers_payload_and_empty_month(monkeypatch):
    """展示链：有游标 + 标记行 → ok 载荷（URL 推导/国区价/评价排序）；
    游标指向查无标记行的月份 → ok=False 空态（2019 年在 HB 档案范围外，
    真实库不会有该标签，断言与库内其他数据无关）。"""
    from app.domains.settings.service import set_value

    monkeypatch.setattr(hb, "_HB_STATE_KEY", TEST_STATE_KEY)
    await set_value(TEST_STATE_KEY, {"machineName": MACHINE, "productName": PRODUCT})

    async with get_session_factory()() as session:
        existing = await session.get(Game, APP_EXISTING)
        existing.hb_data = f"{OLD_LABEL}, {LABEL}"
        existing.review_count = 500
        # 先清价格行残留（前次运行中断可能已落库；真实库有同 appid 行同理）
        await session.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_([APP_NEW, APP_EXISTING])
            )
        )
        session.add(
            Game(appid=APP_NEW, name="Frosttest 2", is_hb=True,
                 hb_data=LABEL, created_at=datetime_now())
        )
        session.add(GameCurrentPrice(
            appid=APP_NEW, region_code="CN", currency="CNY",
            price=14900, original_price=22900, discount_percent=35,
            price_status="ok",
        ))
        session.add(GameCurrentPrice(
            appid=APP_EXISTING, region_code="CN", currency="CNY",
            price=9900, original_price=9900, discount_percent=0,
            price_status="ok",
        ))
        await session.commit()

    res = await hb.hb_choice_offers()
    assert res["ok"] is True
    assert res["label"] == LABEL
    assert res["monthUrl"] == "https://www.humblebundle.com/membership/September-2026"
    assert res["skipUrl"].endswith("/user/skip-month")
    by_id = {g["appid"]: g for g in res["games"]}
    assert by_id[APP_NEW]["priceFen"] == 14900
    assert by_id[APP_NEW]["discount"] == 35
    assert by_id[APP_EXISTING]["name"] == "Keytest"
    # 评价量多的在前（库内其余标记行可能混入，只断言合成行相对序）
    ids = [g["appid"] for g in res["games"]]
    assert ids.index(APP_EXISTING) < ids.index(APP_NEW)

    await set_value(TEST_STATE_KEY, {"machineName": "september_2019_choice"})
    empty = await hb.hb_choice_offers()
    assert empty["ok"] is False

    await set_value(TEST_STATE_KEY, {
        "machineName": MACHINE, "productName": PRODUCT,
        "platformGames": [{"title": "GOGtest Odyssey", "platform": "gog",
                           "thumbnail": "https://hb.test/g.jpg",
                           "retailFen": 1999, "endDate": ""}],
    })
    merged = await hb.hb_choice_offers()
    assert merged["ok"] is True
    platform_rows = [g for g in merged["games"] if g["appid"] == 0]
    assert len(platform_rows) == 1
    assert platform_rows[0]["name"] == "GOGtest Odyssey"
    assert platform_rows[0]["platform"] == "gog"
    assert platform_rows[0]["originalPriceFen"] == 1999
    assert platform_rows[0]["priceFen"] is None




@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    async with get_session_factory()() as session:
        session.add(
            Game(
                appid=APP_EXISTING, name="Keytest",
                is_hb=True, hb_data=OLD_LABEL,
                created_at=datetime_now(),
            )
        )
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(Game).where(
                Game.appid.in_([APP_NEW, APP_EXISTING, APP_UNRESOLVED])
            )
        )
        await session.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_([APP_NEW, APP_EXISTING, APP_UNRESOLVED])
            )
        )
        await session.execute(
            delete(AppSetting).where(AppSetting.key == TEST_STATE_KEY)
        )
        await session.commit()

def datetime_now():
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().replace(tzinfo=None)


class _NoSleep:
    """asyncio.sleep 打桩（链路内 storesearch 礼貌间隔）。"""

    @staticmethod
    async def sleep(_: float) -> None:
        return None


@pytest.mark.asyncio
async def test_refresh_partial_unresolved_keeps_retrying(monkeypatch):
    """有未解析条目 → 打标生效但不记账（次日重试），重跑幂等不重复续写。"""
    monkeypatch.setattr(hb, "_HB_STATE_KEY", TEST_STATE_KEY)
    monkeypatch.setattr(hb, "asyncio", _NoSleep)

    async def fake_fetch(session, proxy):
        return _membership_html()

    async def fake_resolve(session, title, proxy):
        return _RESOLVE_MAP.get(title)

    monkeypatch.setattr(hb, "_fetch_membership_page", fake_fetch)
    monkeypatch.setattr(hb, "_resolve_appid", fake_resolve)

    first = await hb.refresh_hb_choice()
    assert first["ok"] is True
    assert [m["appid"] for m in first["marked"]] == [APP_NEW, APP_EXISTING]
    assert first["unresolved"] == ["Missing Game"]
    assert first["recorded"] is False

    async with get_session_factory()() as session:
        assert await session.get(AppSetting, TEST_STATE_KEY) is None

    async with get_session_factory()() as session:
        new_row = await session.get(Game, APP_NEW)
        assert new_row is not None and new_row.is_hb is True
        assert new_row.hb_data == LABEL
        assert new_row.updated_at is None
        assert new_row.name == "Frosttest 2"

        existing = await session.get(Game, APP_EXISTING)
        assert existing.hb_data == f"{OLD_LABEL}, {LABEL}"
        assert existing.is_hb is True

    second = await hb.refresh_hb_choice()
    assert second["skipped"] is False
    async with get_session_factory()() as session:
        existing = await session.get(Game, APP_EXISTING)
        assert existing.hb_data == f"{OLD_LABEL}, {LABEL}"


@pytest.mark.asyncio
async def test_refresh_records_state_and_skips_when_resolved(monkeypatch):
    """全量解析结案 → 记账，machine_name 未变时幂等跳过。"""
    monkeypatch.setattr(hb, "_HB_STATE_KEY", TEST_STATE_KEY)
    monkeypatch.setattr(hb, "asyncio", _NoSleep)

    async def fake_fetch(session, proxy):
        return _membership_html()

    async def fake_resolve(session, title, proxy):
        if title == "Missing Game":
            return APP_UNRESOLVED
        return _RESOLVE_MAP[title]

    monkeypatch.setattr(hb, "_fetch_membership_page", fake_fetch)
    monkeypatch.setattr(hb, "_resolve_appid", fake_resolve)

    first = await hb.refresh_hb_choice()
    assert first["recorded"] is True
    assert first["unresolved"] == []

    async with get_session_factory()() as session:
        state = await session.get(AppSetting, TEST_STATE_KEY)
        assert state is not None
        assert state.value_json["machineName"] == MACHINE
        assert state.value_json["marked"] == 3
        assert [pg["platform"] for pg in state.value_json["platformGames"]] == ["gog", "epic"]

    second = await hb.refresh_hb_choice()
    assert second["skipped"] is True
    assert second["lastMarked"] == 3

    async with get_session_factory()() as session:
        row = await session.get(Game, APP_UNRESOLVED)
        assert row is not None and row.hb_data == LABEL

    third = await hb.refresh_hb_choice(force=True)
    assert third.get("skipped") is not True
    assert third["recorded"] is True
    async with get_session_factory()() as session:
        row = await session.get(Game, APP_UNRESOLVED)
        assert row is not None and row.hb_data == LABEL


@pytest.mark.asyncio
async def test_unpriced_hb_appids_filters_priced():
    """候选查询两通道判据：无任何价格行的标记行进候选；已有价格行
    （missing 状态）的归补抓账本不进（合成 appid 断言，容忍库内真实行）。"""
    async with get_session_factory()() as session:
        session.add(Game(appid=APP_NEW, name="Frosttest 2", is_hb=True,
                         hb_data=LABEL, created_at=datetime_now()))
        await session.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_([APP_NEW, APP_EXISTING])
            )
        )
        session.add(GameCurrentPrice(
            appid=APP_EXISTING, region_code="CN", currency="CNY",
            price=None, price_status="missing",
        ))
        await session.commit()

    appids = await hb.unpriced_hb_appids()
    assert APP_NEW in appids
    assert APP_EXISTING not in appids


@pytest.mark.asyncio
async def test_backfill_hb_prices_trigger(monkeypatch):
    """触发行为：候选为空不发起爬取；候选非空以 appids spec 进
    run_sequential（kind=hb_backfill），返回任务 id。"""
    from app.domains.crawl import service as crawl_service

    calls: list[list[dict]] = []

    async def fake_run_sequential(specs, **_):
        calls.append(specs)
        return [{"id": 7}]

    monkeypatch.setattr(crawl_service, "run_sequential", fake_run_sequential)

    async def fake_unpriced(limit=60):
        return [APP_NEW, APP_EXISTING]

    monkeypatch.setattr(hb, "unpriced_hb_appids", fake_unpriced)
    res = await hb.backfill_hb_prices()
    assert res["started"] is True and res["jobId"] == 7
    assert calls[0][0]["kind"] == "hb_backfill"
    assert calls[0][0]["appids"] == [APP_NEW, APP_EXISTING]

    async def fake_unpriced_empty(limit=60):
        return []

    monkeypatch.setattr(hb, "unpriced_hb_appids", fake_unpriced_empty)
    res2 = await hb.backfill_hb_prices()
    assert res2["pending"] == 0 and res2["started"] is False
    assert len(calls) == 1
