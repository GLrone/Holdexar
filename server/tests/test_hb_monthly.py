"""HB 当月包游戏侧标记链验收（refresh_hb_choice，无账本表）。

纯逻辑：月份标签换算 / 页面解析（转义 JSON + 区块重复去重）/ storesearch
命中匹配。链路：合成 membership 页 + 合成解析结果，走真实标记落库
（合成 990xxx appid 播种 + 清理，不污染真实库），验证：
- steam 条目打标（is_hb + hb_data 月份标签），非 steam 条目跳过；
- 缺行占位 updated_at NULL（回补池判据），已有行保留旧标记并逗号续写；
- 解析失败条目进 unresolved 不阻塞其余条目；
- machine_name 未变时幂等跳过（app_settings 记账）。
"""
import html
import json
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, init_db  # noqa: E402
from app.domains.games.models import Game  # noqa: E402
from app.domains.metadata import service as hb  # noqa: E402
from app.domains.settings.models import AppSetting  # noqa: E402

TEST_STATE_KEY = "hb_choice_last_month_test"
MACHINE = "septtest_2026_choice"
PRODUCT = "September 2026 Humble Choice"
LABEL = "HB慈善包26年9月包"

APP_NEW = 990_401  # 缺行占位
APP_EXISTING = 990_402  # 已有行（带旧月份标记，验证续写）
APP_UNRESOLVED = 990_403  # storesearch 未命中（只进 unresolved）
OLD_LABEL = "HB慈善包25年3月包"

_RESOLVE_MAP = {
    "Frosttest 2": APP_NEW,
    "Keytest": APP_EXISTING,
    "Missing Game": None,
}


def _item(title: str, delivery: list[str], steam_count: int | None = 100) -> dict:
    rating = {} if steam_count is None else {
        "steam_count": steam_count, "steam_percent": 0.9,
    }
    return {
        "recommendation_copy_dict": {"copy": " synth "},
        "delivery_methods": delivery,
        "title": title,
        "msrp": {"currency": "USD", "amount": 9.99},
        "user_rating": rating,
    }


def _membership_html() -> str:
    """合成 membership 页：条目为转义 JSON（引号 &#34;），清单重复两轮，
    另含一个 recommendation 文案内嵌花括号的条目（验证 raw_decode 抗性）。"""
    items = {
        "frosttest2": _item("Frosttest 2", ["steam"]),
        "keytest": _item("Keytest", ["steam"]),
        "plustest": _item("Plus Test", ["other-key"]),
        "missinggame": _item("Missing Game", ["steam"]),
    }
    # 花括号进字符串：模拟真实页 recommendation 文案含 {} 的场景
    items["keytest"]["recommendation_copy_dict"]["copy"] = "build {oil} coal"
    blocks = []
    for name, obj in items.items():
        blocks.append(f'"{name}": {json.dumps(obj, ensure_ascii=False)}')
    # quote=True：引号转义成 &#34;，与真实页转义形态一致
    blob = html.escape(", ".join(blocks))
    header = (
        f'{{"activeContentMachineName": "{MACHINE}", '
        f'"navbarOptions": {{"productHumanName": "{PRODUCT}", '
        f'"activeContentEndDate|datetime": "2026-10-06T17:00:00"}}}}'
    )
    return f"<html><script>var X = {header};</script><div data-json=\"{blob}\"></div>" \
           f"<div data-json=\"{blob}\"></div></html>"


# ─── 纯逻辑 ────────────────────────────────────────────────────────


def test_month_label_from_product_name():
    assert hb._choice_month_label(MACHINE, PRODUCT) == LABEL


def test_month_label_from_machine_name():
    assert hb._choice_month_label("july_2026_choice", None) == "HB慈善包26年7月包"


def test_month_label_fallback_generic():
    assert hb._choice_month_label("whatever", "Mystery Box") == "HB慈善包"


def test_parse_choice_page_dedup_and_fields():
    parsed = hb._parse_choice_page(_membership_html())
    assert parsed["machineName"] == MACHINE
    assert parsed["productName"] == PRODUCT
    assert parsed["endDate"] == "2026-10-06T17:00:00"
    assert set(parsed["items"]) == {
        "frosttest2", "keytest", "plustest", "missinggame",
    }
    assert parsed["items"]["keytest"]["delivery_methods"] == ["steam"]
    # 区块重复去重：条目对象完整解析（花括号文案不截断）
    assert parsed["items"]["keytest"]["recommendation_copy_dict"]["copy"] == "build {oil} coal"


def test_pick_appid_matching_tiers():
    hits = [
        {"name": "Something Else", "id": "111"},
        {"name": "Keytest: Deluxe Edition", "id": "222"},
        {"name": "KEYTEST", "id": "333"},  # 大小写归一命中
    ]
    # exact 优先于清洗命中（Deluxe 副标题剥掉后同形，但不能抢先）
    assert hb._pick_appid("Keytest", hits) == 333
    assert hb._pick_appid("keytest !", hits) == 222  # 无 exact，清洗层兜底
    assert hb._pick_appid("Nothing", hits) is None


def test_pick_appid_playtest_normalized():
    hits = [{"name": "Eldtest Escape", "id": "444"}, {"name": "Eldtest Escape Demo", "id": "445"}]
    # Humble 给测试键：页内标题带 Playtest，商店侧无该条目 → 剥词对准本体
    assert hb._pick_appid("Eldtest Escape Playtest", hits) == 444


# ─── 链路（真实落库 + 合成数据）──────────────────────────────────


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
        return _RESOLVE_MAP.get(title)  # Missing Game → None

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
        assert new_row.updated_at is None  # 占位行进回补池
        assert new_row.name == "Frosttest 2"

        existing = await session.get(Game, APP_EXISTING)
        assert existing.hb_data == f"{OLD_LABEL}, {LABEL}"
        assert existing.is_hb is True

    # 未记账 → 再次运行不跳过；打标幂等（hb_data 不重复续写）
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

    second = await hb.refresh_hb_choice()
    assert second["skipped"] is True
    assert second["lastMarked"] == 3

    # 幂等跳过：二次运行不再触达条目，hb_data 保持单月标记不重复续写
    async with get_session_factory()() as session:
        row = await session.get(Game, APP_UNRESOLVED)
        assert row is not None and row.hb_data == LABEL
