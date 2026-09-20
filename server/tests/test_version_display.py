"""版本显示链路验收。

四条链路（合成 appid 播种 game_price_history，夹具清理）：
1. extract_version_suffix 真实 option_text 形态（价格尾巴 / 打折双价 /
   HTML span / 中文 name_en / 标准版 / RU-VN-PL 货币尾巴）
2. get_game_versions：history 每 (region, sub) 最新行聚合；is_bundle 剔除；
   标准版排序优先
3. db_writer 标准版候选排除 is_bundle（current 不被 bundle 价污染）
4. refresh_hl_flags 排除 is_bundle（bundle 低价不产生假史低）
"""
import asyncio
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.database import get_session_factory, init_db
from app.crawler.db_writer import DbWriter
from app.crawler.utils import extract_version_suffix
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory
from app.domains.games import service as games_service

APPID = 995_001


# ── 1. 提取器：生产真实形态（全部带价格尾巴——修复前这些全提空）───


def test_suffix_real_option_text_forms():
    cases = [
        # (option_text, name_en, 期望 suffix)
        # 生产样本：Far Cry 5 gold（552520 sub 252910）——大小写保留 Steam 原文
        ("Far Cry 5 - Gold edition - ¥ 448.00", "Far Cry 5", "Gold edition"),
        # 生产样本：Gourmet Edition（728880 sub 1066582）
        ("Overcooked! 2 - Gourmet Edition - ¥ 189.99", "Overcooked! 2", "Gourmet Edition"),
        # 本体 sub：与 name_en 相同 → 标准版
        ("Overcooked! 2 - ¥ 98.00", "Overcooked! 2", ""),
        # 打折双价格尾巴（original + final span）
        (
            'Overcooked! 2 - Gourmet Edition - <span class="discount_original_price">¥ 209.00</span>'
            '<span class="discount_final_price">¥ 189.99</span>',
            "Overcooked! 2",
            "Gourmet Edition",
        ),
        # 美元 ISO 形态 + <br>
        (
            'Name - Deluxe Edition - <span class="discount_original_price">US$ 59.99</span>'
            '<br /><span class="discount_final_price">US$ 39.99</span>',
            "Name",
            "Deluxe Edition",
        ),
        # name_en 是 schinese 中文名（修复前 startswith 恒失败，全靠词表兜底）
        (
            "Assassin's Creed Black Flag - Gold Edition - ¥ 168.00",
            "刺客信条4：黑旗",
            "Gold Edition",
        ),
        # 数字开头但非价格的版本名不被误剥
        ("4 Deluxe - ¥ 98.00", "4 Deluxe", ""),
        # E2E 样本（隔离实例抓 Portal 2）：RU 区卢布缩写尾巴
        ("Portal 2 - 385 руб.", "Portal 2", ""),
        ("Portal 2 - 1 199 руб.", "Portal 2", ""),
        # 波兰 zł / 欧洲 € 尾符号形态
        ("Portal 2 - 199,99 zł", "Portal 2", ""),
        ("Portal 2 - 42,00 €", "Portal 2", ""),
        # VN 区 ₫ / 印尼 Rp 前缀缩写（E2E 形态）
        ("Portal 2 - 142.000₫", "Portal 2", ""),
        ("Portal 2 - Rp 90 999", "Portal 2", ""),
    ]
    for opt, name_en, expected in cases:
        got = extract_version_suffix(opt, name_en)
        assert got == expected, f"option_text={opt!r} → {got!r}（期望 {expected!r}）"


def test_price_segment_detector_all_regions():
    """价格段判定器：41 区真实尾巴全集（appid 620 逐区枚举）+
    反例（版本名不误判）。新货币形态先跑这个枚举脚本再补 token。"""
    from app.crawler.utils import _is_price_segment

    tails = [
        "¥ 37.00", "259 руб.", "1 850₸", "169₴", "$5.49 USD", "CLP$ 4.400",
        "P289.95", "1.95 KD", "21.95 SR", "R 79.00", "24.99 QR", "RM23.50",
        "฿189.00", "S/.22.00", "Mex$ 113.99", "S$10.00", "29.00 AED", "$U229",
        "COL$ 18.500", "₩ 10,500", "NZ$ 12.39", "35,99 zł", "₡4.600",
        "CDN$ 11.49", "8,19€", "72,00 kr", "Rp 69 999", "120.000₫", "₪36.95",
        "CHF 10.50", "CHF 25.--", "HK$ 52.00", "NT$ 186", "R$ 20,69", "A$ 14.50", "$9.99",
        "¥ 600", "1 199 руб.", "142.000₫", "90 999 Rp",
    ]
    non_price = [
        "4 Deluxe", "Gold Edition", "The Final Hours", "Overcooked! 2",
        "Gourmet Edition", "2", "Season Pass", "Volume 1",
    ]
    for t in tails:
        assert _is_price_segment(t), f"漏判价格段: {t!r}"
    for n in non_price:
        assert not _is_price_segment(n), f"版本名被误判为价格: {n!r}"


# ── 2-4. 库链路（播种 → 断言 → 清理）───


def _seed_history(session, sub_id, region, price, *, suffix=None, gold=False, bundle=False,
                  days_ago=1, cny_fen=None):
    session.add(
        GamePriceHistory(
            appid=APPID, region_code=region, currency="CNY" if region == "CN" else "USD",
            price=price, original_price=price, discount_percent=0, sub_id=sub_id,
            is_gold=gold, version_suffix=suffix, is_bundle=bundle, price_status="ok",
            cny_fen=cny_fen if cny_fen is not None else price,
            snapshot_at=datetime.now() - timedelta(days=days_ago),
        )
    )


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    now = datetime.now()
    async with get_session_factory()() as session:
        session.add(Game(appid=APPID, name="版本测试游戏", created_at=now, updated_at=now))
        # current：CN 标准版价（refresh_hl_flags 的基准）
        session.add(
            GameCurrentPrice(
                appid=APPID, region_code="CN", currency="CNY", price=10000,
                original_price=10000, discount_percent=0, sub_id=1,
                price_status="ok", fail_count=0, cny_fen=10000, updated_at=now,
            )
        )
        # history（prior_low 链路用）：标准版旧价必须高于 current（10000），
        # bundle 低价（9000）被排除后 prior=11000 → flag=新史低
        _seed_history(session, 1, "CN", 11000, days_ago=2)                    # 标准版（旧）
        _seed_history(session, 1, "CN", 12000, days_ago=3)                    # 更旧
        _seed_history(session, 1, "US", 1499, days_ago=2, cny_fen=10800)      # 标准版美区
        _seed_history(session, 2, "CN", 11500, days_ago=2, suffix="Gold Edition", gold=True)
        _seed_history(session, 3, "CN", 9000, days_ago=2, suffix="Gourmet Edition", bundle=True)
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(delete(GamePriceHistory).where(GamePriceHistory.appid == APPID))
        await session.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid == APPID))
        await session.execute(delete(Game).where(Game.appid == APPID))
        await session.commit()


@pytest.mark.asyncio
async def test_get_game_versions_aggregation():
    """versions 聚合：latest-wins、bundle 剔除、标准版排序优先。"""
    res = await games_service.get_game_versions(APPID)
    vs = res["versions"]
    by_sub = {v["subId"]: v for v in vs}
    # bundle（sub 3）被剔除
    assert 3 not in by_sub
    # latest-wins：CN 标准版取最新（11000，2 天前），非更旧的 12000
    assert by_sub[1]["regions"]["CN"]["cents"] == 11000
    assert by_sub[1]["regions"]["US"]["cents"] == 1499
    # gold 版在列
    assert by_sub[2]["isGold"] is True
    assert by_sub[2]["regions"]["CN"]["cents"] == 11500
    # 排序：标准版（sub 1）在 gold 之前
    assert vs[0]["subId"] == 1


@pytest.mark.asyncio
async def test_current_excludes_bundle_from_standard_candidates():
    """标准版候选排除 is_bundle：current 取标准 sub，不被 bundle 低价污染。"""
    db = DbWriter()
    now = datetime.now()
    prices = [
        {"appid": APPID, "region_code": "CN", "currency": "CNY", "price": 10000,
         "original_price": 10000, "discount_percent": 0, "sub_id": 1, "is_gold": False,
         "version_suffix": None, "is_bundle": False, "price_status": "ok", "crawled_at": now},
        {"appid": APPID, "region_code": "CN", "currency": "CNY", "price": 5000,
         "original_price": 5000, "discount_percent": 0, "sub_id": 3, "is_gold": False,
         "version_suffix": "Gourmet Edition", "is_bundle": True, "price_status": "ok",
         "crawled_at": now},
    ]
    assert await db.upsert_game_and_prices(
        {"appid": APPID, "name": "版本测试游戏", "updated_at": now}, prices
    ) is True
    async with get_session_factory()() as session:
        cn = await session.get(GameCurrentPrice, (APPID, "CN"))
        assert cn.sub_id == 1  # 标准版胜出（bundle 价更低也不参与候选）
        assert cn.price == 10000
        # history 落库且 bundle 行带标记（fixture + upsert 各一条，INSERT-only 不去重）
        rows = (await session.execute(
            select(GamePriceHistory).where(
                GamePriceHistory.appid == APPID, GamePriceHistory.sub_id == 3,
            )
        )).scalars().all()
        assert rows and all(r.is_bundle is True for r in rows)


@pytest.mark.asyncio
async def test_hl_flags_excludes_bundle_rows():
    """史低计算排除 bundle：bundle 低价（9000）不压 prior_low，edition 旧价
    （11000）成为基准 → 当前 10000 < 11000 = 新史低（flag 1）。
    若不排除 bundle，prior=9000 → flag 0。"""
    await games_service.refresh_hl_flags([APPID])
    async with get_session_factory()() as session:
        game = await session.get(Game, APPID)
        assert game.hl_flag == 1
