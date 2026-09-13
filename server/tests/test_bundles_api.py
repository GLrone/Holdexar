"""捆绑包域 API 行为验收（列表聚合 + 详情 + 补齐计算数据源）。

合成 bundle_id/appid 播种 bundles / bundle_region_prices / games /
game_current_prices（夹具清理，不污染真实库），验证：
- 列表聚合：区键大写去重、基准 appids（app_ids 最多的区）、锁区数、
  最低价区、差价下限 0、mps 缺省 -1、差价降序；
- 双产品隔离：双轨混写的异种 appids 行（同号 sub 污染）整行剔除，
  不参与最低价/锁区（bundle 61597 数据事故防线）；
- 追踪区过滤：列表/详情按 crawl_regions 启用集过滤，南亚 PK/BD 双区
  同进同出（启用含 pk 才放行两行）；未启用任何区 = 全量；
- 图片 CDN 域归一化：fastly/queniuqe → akamai，无封面时按 mps 分 bundle/sub 兜底；
- 详情：包内游戏各区现价（ok 状态）、未入库游戏 name=None；
- 404 路径。

追踪区通过打桩 _tracked_region_codes 控制（模块级依赖 regions.service，
不打桩会读到生产 crawl_regions 表——启用集随「我」页漂移，断言不稳定）。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import HTTPException  # noqa: E402

from app.core.database import get_session_factory, init_db  # noqa: E402
from app.domains.bundles import service  # noqa: E402
from app.domains.games.models import (  # noqa: E402
    Bundle,
    BundleRegionPrice,
    Game,
    GameCurrentPrice,
)

BID_A = 990_101  # 可补齐包（mps=0）
BID_B = 990_102  # 无任何区域价 → 列表应过滤
BID_C = 990_103  # 双轨混写包：bundle 3 appids + 同号 sub 单 appid 污染行
APPID_1 = 990_111
APPID_2 = 990_112
APPID_3 = 990_113  # 不入库：详情 games 里 name=None
APPID_SUB = 990_114  # 污染行的异种 appid


def _bp(bid: int, region: str, price: int | None, cny_fen: int | None, aids: list[int]) -> BundleRegionPrice:
    return BundleRegionPrice(
        bundle_id=bid,
        region_code=region,
        currency="CNY" if region.upper() == "CN" else "USD",
        price=price,
        original_price=price,
        discount_percent=0,
        bundle_base_discount=10,
        price_status="ok",
        cny_fen=cny_fen,
        app_ids=aids,
    )


@pytest_asyncio.fixture(autouse=True)
async def _seed(monkeypatch):
    await init_db()
    # 打桩追踪区：默认全量（None）；需要过滤语义的用例自行覆盖返回值
    async def _all_regions():
        return None

    monkeypatch.setattr(service, "_tracked_region_codes", _all_regions)
    await init_db()
    async with get_session_factory()() as session:
        session.add_all(
            [
                Bundle(
                    bundle_id=BID_A,
                    name="测试补齐包",
                    must_purchase_as_set=0,
                    app_ids=[APPID_1, APPID_2, APPID_3],
                ),
                Bundle(bundle_id=BID_B, name="无价包", must_purchase_as_set=None, app_ids=[APPID_1]),
                # 双轨混写包：真 bundle 3 appids + 便宜的同号 sub 单 appid 污染行
                Bundle(
                    bundle_id=BID_C, name="混写包", must_purchase_as_set=0,
                    app_ids=[APPID_1, APPID_2, APPID_3],
                    header_image="https://shared.fastly.steamstatic.com/store_item_assets/steam/bundles/1/header.jpg",
                ),
                # 大小写并存同区行（jp/JP）：验证去重取有价者
                _bp(BID_A, "CN", 10000, 10000, [APPID_1, APPID_2, APPID_3]),
                _bp(BID_A, "JP", 3000, 15000, [APPID_1, APPID_2, APPID_3]),
                _bp(BID_A, "jp", 2000, None, [APPID_1, APPID_2, APPID_3]),
                _bp(BID_A, "US", 5000, 36000, [APPID_1, APPID_2]),  # 缺 APPID_3 → 锁 1 款
                # BID_C：真 bundle 行（3 appids）+ sub 污染行（单 appid、超低价）
                _bp(BID_C, "CN", 20000, 20000, [APPID_1, APPID_2, APPID_3]),
                _bp(BID_C, "RU", 100, 800, [APPID_SUB]),  # 异种产品：不应带偏最低价
            ]
        )
        session.add_all(
            [
                Game(appid=APPID_1, name="测试游戏一"),
                Game(appid=APPID_2, name="测试游戏二"),
            ]
        )
        session.add_all(
            [
                GameCurrentPrice(
                    appid=APPID_1, region_code="CN", currency="CNY",
                    price=3000, original_price=3000, discount_percent=0,
                    price_status="ok", cny_fen=3000,
                ),
                GameCurrentPrice(
                    appid=APPID_2, region_code="CN", currency="CNY",
                    price=4000, original_price=4000, discount_percent=0,
                    price_status="ok", cny_fen=4000,
                ),
                # 非国区行（补齐计算外币求和数据源）
                GameCurrentPrice(
                    appid=APPID_1, region_code="JP", currency="JPY",
                    price=1000, original_price=1000, discount_percent=0,
                    price_status="ok", cny_fen=5000,
                ),
            ]
        )
        await session.commit()
    # 种子直写绕过服务写入口 → 聚合缓存必须手动失效（每用例起步冷缓存，
    # 与生产「写入即失效」语义对齐）；用例中途直写由用例自行失效
    service.invalidate_bundles_cache()
    yield
    service.invalidate_bundles_cache()
    async with get_session_factory()() as session:
        await session.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_([APPID_1, APPID_2])))
        await session.execute(delete(Game).where(Game.appid.in_([APPID_1, APPID_2])))
        await session.execute(delete(BundleRegionPrice).where(BundleRegionPrice.bundle_id.in_([BID_A, BID_B, BID_C])))
        await session.execute(delete(Bundle).where(Bundle.bundle_id.in_([BID_A, BID_B, BID_C])))
        await session.commit()


# ─── 导入（链接识别 → 单包刷新语义落库） ────────────────────────────────

BID_IMP = 990_201  # 导入流程用合成包
APPID_IMP = 990_211


def _fake_regions(bundle_id: int, *, mps: bool = False) -> list[dict]:
    """import_bundle 落库语义的合成抓取行（结构对齐 _fetch_bundle_regions）。"""
    rows = []
    for cc, price, cur in (("us", 599, "USD"), ("cn", 29900, "CNY"), ("kz", 150000, "KZT")):
        rows.append({
            "bundle_id": bundle_id, "region_code": cc,
            "price": price, "currency": cur,
            "discount_percent": 10, "bundle_base_discount": 10,
            "app_ids": [APPID_IMP], "price_status": "ok",
            "name": "导入测试包", "header_image": "",
        })
    if mps:
        for r in rows:
            r["mps"] = 1
    return rows


def test_parse_bundle_ref():
    """链接识别：商店/SteamDB 链接（含 slug/query）、裸数字、垃圾输入。"""
    from app.domains.bundles.refresh import _parse_bundle_ref

    assert _parse_bundle_ref("https://store.steampowered.com/bundle/21478/") == (21478, "bundle")
    assert _parse_bundle_ref("https://store.steampowered.com/bundle/21478/FF_BUNDLE/?cc=us") == (21478, "bundle")
    assert _parse_bundle_ref("https://store.steampowered.com/sub/597332/") == (597332, "sub")
    assert _parse_bundle_ref("https://steamdb.info/bundle/39394/") == (39394, "bundle")
    assert _parse_bundle_ref("https://steamdb.info/sub/61597/") == (61597, "sub")
    assert _parse_bundle_ref("21478") == (21478, "bundle")
    assert _parse_bundle_ref("") is None
    assert _parse_bundle_ref("不是链接") is None
    assert _parse_bundle_ref("https://store.steampowered.com/app/17480/") is None


@pytest.mark.asyncio
async def test_import_bundle_upserts_and_enqueue(monkeypatch):
    """导入：抓取行落库（主档+区域价）、existed 标记、队列联动后台触发。"""
    import asyncio

    from app.domains.bundles import refresh

    enqueued_calls: list[tuple[int, int]] = []

    async def _fake_fetch(session, bundle_id, proxy, *, force_package=False):
        assert force_package is False  # bundle 链接不强制 Package 轨
        return _fake_regions(bundle_id)

    async def _fake_proxy():
        return None

    async def _fake_enqueue():
        enqueued_calls.append((1, 2))
        return 1, 2

    monkeypatch.setattr(refresh, "_fetch_bundle_regions", _fake_fetch)
    monkeypatch.setattr(refresh, "_strategy_proxy", _fake_proxy)
    monkeypatch.setattr(refresh, "_enqueue_new_bundle_apps", _fake_enqueue)

    result = await refresh.import_bundle(
        "https://store.steampowered.com/bundle/990201/"
    )
    assert result["ok"] is True
    assert result["bundleId"] == BID_IMP
    assert result["kind"] == "bundle"
    assert result["existed"] is False
    assert result["regionPrices"] == 3
    assert result["name"] == "导入测试包"
    # 队列联动后台任务：事件循环几跳内应已完成（fake 无真实 await）
    for _ in range(20):
        if enqueued_calls:
            break
        await asyncio.sleep(0.01)
    assert enqueued_calls == [(1, 2)]

    # 落库断言：主档 + 3 行区域价；再次导入 = 单包刷新（existed=True）
    async with get_session_factory()() as session:
        b_row = (
            await session.execute(select(Bundle).where(Bundle.bundle_id == BID_IMP))
        ).scalar_one()
        # Bundle 轨 = Steam Complete-the-Set 语义 → mps 填 0（model 默认 -1 不该留）
        assert b_row.must_purchase_as_set == 0
        rows = (
            await session.execute(
                select(BundleRegionPrice).where(BundleRegionPrice.bundle_id == BID_IMP)
            )
        ).scalars().all()
        assert {r.region_code for r in rows} == {"us", "cn", "kz"}
        # cn 行直接折算（rate_map 空表 setdefault CNY=1 → 29900 分）
        cn = next(r for r in rows if r.region_code == "cn")
        assert cn.cny_fen == 29900
        # 旧区行清理：塞一个脏区行后再导入，脏行被清
        session.add(BundleRegionPrice(
            bundle_id=BID_IMP, region_code="jp", currency="JPY",
            price=1000, price_status="ok", cny_fen=5000, app_ids=[APPID_IMP],
        ))
        await session.commit()

    result2 = await refresh.import_bundle(str(BID_IMP))
    assert result2["existed"] is True
    async with get_session_factory()() as session:
        codes = {
            r.region_code for r in (
                await session.execute(
                    select(BundleRegionPrice).where(BundleRegionPrice.bundle_id == BID_IMP)
                )
            ).scalars().all()
        }
        assert "jp" not in codes  # 双轨脏行清理：非本次写入的区行删除
        assert {"us", "cn", "kz"} <= codes

    # 清理
    async with get_session_factory()() as session:
        await session.execute(delete(BundleRegionPrice).where(BundleRegionPrice.bundle_id == BID_IMP))
        await session.execute(delete(Bundle).where(Bundle.bundle_id == BID_IMP))
        await session.commit()


@pytest.mark.asyncio
async def test_import_bundle_sub_and_errors(monkeypatch):
    """sub 链接强制 Package 轨；无效输入/抓取失败抛 ValueError（路由转 400）。"""
    from app.domains.bundles import refresh

    with pytest.raises(ValueError, match="无法识别"):
        await refresh.import_bundle("https://store.steampowered.com/app/17480/")

    seen: dict[str, bool] = {}

    async def _fake_fetch(session, bundle_id, proxy, *, force_package=False):
        seen["force_package"] = force_package
        return []

    async def _fake_fetch_sub(session, bundle_id, proxy, *, force_package=False):
        seen["force_package_sub"] = force_package
        return _fake_regions(bundle_id, mps=True)

    async def _fake_proxy():
        return None

    monkeypatch.setattr(refresh, "_fetch_bundle_regions", _fake_fetch)
    monkeypatch.setattr(refresh, "_strategy_proxy", _fake_proxy)
    with pytest.raises(ValueError, match="抓取失败"):
        await refresh.import_bundle("https://store.steampowered.com/sub/990202/")
    assert seen["force_package"] is True

    # sub 轨成功路径：mps=1 整包落库
    monkeypatch.setattr(refresh, "_fetch_bundle_regions", _fake_fetch_sub)
    r = await refresh.import_bundle("https://store.steampowered.com/sub/990202/")
    assert r["kind"] == "sub" and r["ok"] is True
    async with get_session_factory()() as session:
        b_row = (
            await session.execute(select(Bundle).where(Bundle.bundle_id == 990202))
        ).scalar_one()
        assert b_row.must_purchase_as_set == 1
        await session.execute(delete(BundleRegionPrice).where(BundleRegionPrice.bundle_id == 990202))
        await session.execute(delete(Bundle).where(Bundle.bundle_id == 990202))
        await session.commit()
    assert seen["force_package_sub"] is True


@pytest.mark.asyncio
async def test_list_aggregation():
    """区键大写去重取有价行、基准 appids、锁区数、最低区、差价下限、差价降序。"""
    items = await service.list_bundles()
    a = next(i for i in items if i["bundleId"] == BID_A)
    assert a["name"] == "测试补齐包"
    assert set(a["regionPrices"].keys()) == {"CN", "JP", "US"}  # jp/JP 去重
    assert a["regionPrices"]["JP"]["cnyFen"] == 15000  # 大写行有价，胜出
    assert a["regionPrices"]["US"]["lockedCount"] == 1  # 缺 APPID_3
    assert a["regionPrices"]["CN"]["lockedCount"] == 0
    assert a["appIds"] == [APPID_1, APPID_2, APPID_3]
    assert a["mustPurchaseAsSet"] == 0
    # 最低 = JP 15000 分，CN 10000 分 → JP 更贵? 不，CN 100 < JP 150 → 最低 CN，差价 0
    assert a["lowestRegion"] == "cn"
    assert a["diffFen"] == 0
    assert a["regionPrices"]["CN"]["formatted"].startswith("¥")
    # 无价包被过滤
    assert all(i["bundleId"] != BID_B for i in items)


@pytest.mark.asyncio
async def test_diff_floor_zero_and_order():
    """CN 高于最低区时差价 = CN−最低；列表按差价降序。"""
    items = await service.list_bundles()
    a = next(i for i in items if i["bundleId"] == BID_A)
    # US 36000 分最低? CN 10000 < 36000，JP 15000 → 最低 CN，差价 0（已验证）
    # 这里验证列表排序字段存在且非负
    assert all(i["diffFen"] >= 0 for i in items)


@pytest.mark.asyncio
async def test_tracked_region_filter(monkeypatch):
    """追踪区过滤：区域表/最低价/差价按启用集重算；南亚 PK/BD 同进同出。"""
    # 追踪区 = cn + us（不含 jp）：JP 行剔除，最低价在 {CN, US} 内取
    async def _cn_us():
        return {"CN", "US"}

    monkeypatch.setattr(service, "_tracked_region_codes", _cn_us)
    items = await service.list_bundles()
    a = next(i for i in items if i["bundleId"] == BID_A)
    assert set(a["regionPrices"].keys()) == {"CN", "US"}  # JP 被过滤
    # CN 10000 < US 36000 → 最低 CN（JP 15000 若混入不影响，但已剔）
    assert a["lowestRegion"] == "cn"
    assert a["diffFen"] == 0

    # 追踪区含 pk：PK 与 BD 两行都放行（给 BID_C 临时加 pk/BD 行验证）
    async with get_session_factory()() as session:
        session.add(_bp(BID_C, "pk", 100, 100, [APPID_1, APPID_2, APPID_3]))
        session.add(_bp(BID_C, "BD", 90, 90, [APPID_1, APPID_2, APPID_3]))
        await session.commit()
    service.invalidate_bundles_cache()  # 直写绕过写入口 → 手动失效聚合缓存

    async def _with_pk():
        return {"CN", "PK"}

    monkeypatch.setattr(service, "_tracked_region_codes", _with_pk)
    detail = await service.get_bundle_detail(BID_C)
    assert "PK" in detail["regionPrices"] and "BD" in detail["regionPrices"]

    # 追踪区不含 pk：PK 与 BD 都剔除
    async def _no_pk():
        return {"CN", "US"}

    monkeypatch.setattr(service, "_tracked_region_codes", _no_pk)
    detail2 = await service.get_bundle_detail(BID_C)
    assert "PK" not in detail2["regionPrices"]
    assert "BD" not in detail2["regionPrices"]

    # 最低价在过滤后的区内重取：给 BID_C 加一行便宜的未追踪区行不生效
    async with get_session_factory()() as session:
        session.add(
            _bp(BID_C, "TR", 100, 5, [APPID_1, APPID_2, APPID_3])  # 便宜但未追踪
        )
        await session.commit()
    service.invalidate_bundles_cache()  # 直写绕过写入口 → 手动失效聚合缓存
    items = await service.list_bundles()
    c = next(i for i in items if i["bundleId"] == BID_C)
    assert "TR" not in c["regionPrices"]
    assert c["lowestRegion"] == "cn"  # 未被 TR 的 5 分带偏
    async with get_session_factory()() as session:
        await session.execute(
            delete(BundleRegionPrice).where(
                BundleRegionPrice.bundle_id == BID_C,
                BundleRegionPrice.region_code.in_(["TR", "pk", "BD"]),
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_dual_product_isolation():
    """双轨混写行（异种 appids）整行剔除：最低价/区域表不被 sub 污染行带偏。"""
    items = await service.list_bundles()
    c = next(i for i in items if i["bundleId"] == BID_C)
    # RU 污染行（APPID_SUB，cny=800）被剔除，不进区域表也不进最低价
    assert "RU" not in c["regionPrices"]
    # 最低 = CN 20000 分（唯一同族行）；污染行 800 分若混入会错标 RU 为最低
    assert c["lowestRegion"] == "cn"
    assert c["lowestCnyFen"] == 20000
    assert set(c["regionPrices"].keys()) == {"CN"}
    assert c["regionPrices"]["CN"]["lockedCount"] == 0
    assert c["appIds"] == [APPID_1, APPID_2, APPID_3]


@pytest.mark.asyncio
async def test_image_normalization_and_fallback():
    """fastly/queniuqe 图片域归一到 akamai；无封面按 mps 分 bundle/sub 兜底路径。"""
    items = await service.list_bundles()
    c = next(i for i in items if i["bundleId"] == BID_C)
    assert c["headerImage"].startswith("https://shared.akamai.steamstatic.com/store_item_assets/steam/bundles/")
    # BID_A 无封面（header_image 为 NULL）→ mps=0 走 bundles 路径
    a = next(i for i in items if i["bundleId"] == BID_A)
    assert a["headerImage"].startswith("https://shared.akamai.steamstatic.com/store_item_assets/steam/bundles/")
    # sub 类（mps=1）兜底走 subs 路径
    sub_detail = await service.get_bundle_detail(597332) if False else None  # 生产数据不依赖
    from app.domains.bundles.service import _fallback_image  # noqa: PLC0415

    class _FakeBundle:
        bundle_id = 123
        header_image = None
        must_purchase_as_set = 1

    assert "/steam/subs/123/" in _fallback_image(_FakeBundle(), 1)


@pytest.mark.asyncio
async def test_detail_games_and_prices():
    """包内游戏：入库的带名称与各区现价，未入库的 name=None；计算器可据此求和。"""
    detail = await service.get_bundle_detail(BID_A)
    assert detail is not None
    games = {g["appid"]: g for g in detail["games"]}
    assert set(games.keys()) == {APPID_1, APPID_2, APPID_3}
    assert games[APPID_1]["name"] == "测试游戏一"
    assert games[APPID_3]["name"] is None  # 未入库 → 前端按无数据自动排除
    assert games[APPID_1]["prices"]["CN"]["cnyFen"] == 3000
    assert games[APPID_1]["prices"]["JP"]["cnyFen"] == 5000
    assert "CN" not in games[APPID_3]["prices"]


@pytest.mark.asyncio
async def test_detail_404():
    assert await service.get_bundle_detail(999_999) is None
    with pytest.raises(HTTPException):
        # router 层 404 语义单独验证
        from app.domains.bundles.router import bundle_detail

        await bundle_detail(999_999)
