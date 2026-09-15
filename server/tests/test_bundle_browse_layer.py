"""捆绑包新抓取层（IStoreBrowseService）解析与批行为验收。

纯函数 + 打桩 `_fetch_regions_batched`，不出网：
- 条目判定：未收录（name/slug 全空）不写行；真实但锁区写 locked 而不写价格；
- 价格单位：`*_in_cents` 一律「分」落库，cny_fen 按分口径折算（零小数货币不得 ×100）；
- 选项选择：优先 best_purchase_option，其次 default 组第一个；
- 位置对齐：条数一致按位置（未收录条目回 id=0），条数不符降级按 id；
- 单包身份兜底：按库内身份问不到真实条目时换另一身份再问，force_package 不兜底。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.bundles import refresh  # noqa: E402


def _item(**kw) -> dict:
    base = {
        "id": 13608,
        "item_type": 2,
        "name": "Overcooked! 2 - Complete the Set",
        "store_url_slug": "Overcooked_2_Complete_the_Set",
        "success": 1,
        "visible": True,
        "included_appids": [728880, 858240],
        "best_purchase_option": {
            "final_price_in_cents": 4856,
            "original_price_in_cents": 5396,
            "discount_pct": 10,
            "bundle_discount_pct": 10,
            "package_group": "default",
        },
        "assets": {
            "asset_url_format": "steam/bundles/13608/${FILENAME}",
            "header": "header.jpg",
        },
    }
    base.update(kw)
    return base


class TestItemJudgement:
    def test_not_listed_item_writes_nothing(self) -> None:
        """未收录（伪造 id）与真·锁区同形（success=15/visible=false），靠 name 区分。"""
        bogus = {
            "id": 999_999_999,
            "item_type": 2,
            "name": None,
            "store_url_slug": "",
            "success": 15,
            "visible": False,
        }
        assert refresh._is_real_item(bogus) is False
        assert refresh._browse_row(bogus, 999_999_999, "us", "bundleid") is None

    def test_restricted_real_item_is_locked(self) -> None:
        """真实存在但本区不售：写 locked 行（该区不售 ≠ 抓取欠账）。"""
        restricted = _item(
            id=510898,
            item_type=1,
            success=15,
            visible=False,
            unvailable_for_country_restriction=True,
            best_purchase_option=None,
        )
        row = refresh._browse_row(restricted, 510898, "cn", "packageid")
        assert row is not None
        assert row["price_status"] == "locked"
        assert row["price"] is None
        assert row["mps"] == 1  # item_type=1 → Sub

    def test_visible_without_option_is_locked(self) -> None:
        row = refresh._browse_row(_item(best_purchase_option=None, purchase_options=[]),
                                 13608, "cn", "bundleid")
        assert row["price_status"] == "locked"

    def test_missing_item_writes_nothing(self) -> None:
        assert refresh._browse_row(None, 13608, "us", "bundleid") is None


class TestPriceUnits:
    def test_zero_decimal_currency_price_stays_cents(self) -> None:
        """JPY final=499500 即「¥4,995」的分，落库不得再乘 100。"""
        item = _item(best_purchase_option={
            "final_price_in_cents": 499_500,
            "price_before_bundle_discount": 549_500,
            "discount_pct": 9,
            "bundle_discount_pct": 10,
            "package_group": "default",
        })
        row = refresh._browse_row(item, 13608, "jp", "bundleid")
        assert row["price"] == 499_500
        assert row["original_price"] == 549_500
        assert row["currency"] == "JPY"
        # cny_fen 按「分 → 分」折算：round(price × rate)，不得再乘 100
        assert refresh._row_cny_fen(row, {"JPY": 0.04353}) == round(499_500 * 0.04353)

    def test_original_price_prefers_before_bundle_discount(self) -> None:
        item = _item(best_purchase_option={
            "final_price_in_cents": 1000,
            "original_price_in_cents": 2000,
            "price_before_bundle_discount": 1800,
            "package_group": "default",
        })
        row = refresh._browse_row(item, 13608, "us", "bundleid")
        assert row["original_price"] == 1800

    def test_discount_falls_back_to_computed(self) -> None:
        """discount_pct 缺失时按原价/现价反算（Steam 先定价后折后取整）。"""
        item = _item(best_purchase_option={
            "final_price_in_cents": 7500,
            "price_before_bundle_discount": 10_000,
            "package_group": "default",
        })
        row = refresh._browse_row(item, 13608, "us", "bundleid")
        assert row["discount_percent"] == 25

    def test_cny_fen_none_without_rate(self) -> None:
        row = refresh._browse_row(_item(), 13608, "us", "bundleid")
        assert refresh._row_cny_fen(row, {}) is None

    def test_header_image_from_asset_format(self) -> None:
        row = refresh._browse_row(_item(), 13608, "us", "bundleid")
        assert row["header_image"].endswith("/steam/bundles/13608/header.jpg")


class TestOptionSelection:
    def test_prefers_best_purchase_option(self) -> None:
        item = _item(
            best_purchase_option={"final_price_in_cents": 4856, "package_group": "default"},
            purchase_options=[{"final_price_in_cents": 9999, "package_group": "default"}],
        )
        assert refresh._browse_option(item)[1]["final_price_in_cents"] == 4856

    def test_default_group_filtered_when_no_best(self) -> None:
        item = _item(
            best_purchase_option=None,
            purchase_options=[
                {"final_price_in_cents": 111, "package_group": "other"},
                {"final_price_in_cents": 222, "package_group": "default"},
            ],
        )
        assert refresh._browse_option(item)[1]["final_price_in_cents"] == 222

    def test_no_price_option_is_no_price(self) -> None:
        item = _item(best_purchase_option=None,
                     purchase_options=[{"package_group": "default"}])
        status, opt = refresh._browse_option(item)
        assert status == "no_price" and opt is not None


class TestIdentitySemanticsSplit:
    """形态（item_kind：链接/CDN 用）与购买语义（mps：是否必须整包）解耦。

    实证：bundle 63575 形态是 bundle（item_type=2）但 Steam 在选项级明示
    must_purchase_as_set=True——只看 item_type 会把「必须整包」错标成可补齐，
    反之只看选项 mps 又会把所有 sub 混淆（131 个 sub 选项级 mps 全 False）。
    """

    def test_bundle_shape_must_set_full(self) -> None:
        item = _item(item_type=2, best_purchase_option={
            "final_price_in_cents": 10_000,
            "must_purchase_as_set": True,
            "package_group": "default",
        })
        row = refresh._browse_row(item, 63575, "us", "bundleid")
        assert row["item_kind"] == 0  # 形态：bundle
        assert row["mps"] == 1  # 语义：必须整包

    def test_sub_shape_semantics_from_option(self) -> None:
        item = _item(item_type=1, best_purchase_option={
            "final_price_in_cents": 499_500,
            "must_purchase_as_set": False,
            "package_group": "default",
        })
        row = refresh._browse_row(item, 1066582, "jp", "packageid")
        assert row["item_kind"] == 1
        assert row["mps"] == 1  # Sub 形态恒不可拆，选项 mps 只锦上添花

    def test_plain_bundle_shape(self) -> None:
        row = refresh._browse_row(_item(), 13608, "us", "bundleid")
        assert row["item_kind"] == 0 and row["mps"] == 0

    def test_locked_region_casts_no_semantics_vote(self) -> None:
        """锁区行（best 无选项）不投语义票：63575 的 ru 反对票实证场景。"""
        item = _item(
            item_type=2,
            success=15,
            visible=False,
            unvailable_for_country_restriction=True,
            best_purchase_option=None,
            purchase_options=[],
        )
        row = refresh._browse_row(item, 63575, "ru", "bundleid")
        assert row["price_status"] == "locked"
        assert row["item_kind"] == 0  # 形态仍有（顶层 item_type 不受锁区影响）
        assert row["mps"] is None  # 语义不投票

    def test_majority_vote_decides_mps(self) -> None:
        """多数决：40 区 mps=1 + 1 区 mps=0（首行）→ 仍判 1。"""
        votes = [0] + [1] * 40
        assert refresh._majority(votes) == 1
        assert refresh._majority([1, 1, 0]) == 1
        assert refresh._majority([]) is None
        assert refresh._majority([0, 1]) is None  # 平票不改写

    def test_browse_kind_reads_shape_not_semantics(self) -> None:
        """选键判据是形态：mps=1 的 bundle 形态包必须仍按 bundleid 问。"""
        assert refresh._browse_kind(63575, 0) == "bundleid"
        assert refresh._browse_kind(1066582, 1) == "packageid"

    @pytest.mark.asyncio
    async def test_upsert_persists_item_kind(self) -> None:
        """库内形态/语义同值不改写；历史行（无 item_type）item_kind 兜底 mps。"""
        from datetime import datetime

        from sqlalchemy import delete, select

        from app.core.database import get_session_factory, init_db
        from app.domains.games.models import Bundle, BundleRegionPrice

        bid = 990_421
        await init_db()
        try:
            async with get_session_factory()() as db:
                db.add(Bundle(bundle_id=bid, name="桩_形态落库",
                              must_purchase_as_set=0, item_kind=-1))
                await db.commit()
            regions = [dict(
                bundle_id=bid, region_code="us", currency="USD", price=4856,
                original_price=None, discount_percent=10, bundle_base_discount=10,
                app_ids=[728880, 858240], price_status="ok", name="x", header_image="",
                mps=0, item_kind=0, kind="bundleid",
            )]
            assert await refresh._upsert_bundle_rows(
                bid, regions, {"USD": 7.0}, datetime.utcnow()
            ) is True
            async with get_session_factory()() as db:
                b = (await db.execute(
                    select(Bundle).where(Bundle.bundle_id == bid)
                )).scalar_one()
                mps, kind = b.must_purchase_as_set, b.item_kind
            assert (mps, kind) == (0, 0)
        finally:
            async with get_session_factory()() as db:
                await db.execute(delete(BundleRegionPrice).where(
                    BundleRegionPrice.bundle_id == bid))
                await db.execute(delete(Bundle).where(Bundle.bundle_id == bid))
                await db.commit()

    @pytest.mark.asyncio
    async def test_singleton_filtered_and_purged(self) -> None:
        """单包甄别：包内 appid<2 的发现桩在落库层整包删除（防单品灌表）。"""
        from datetime import datetime

        from sqlalchemy import delete, select

        from app.core.database import get_session_factory, init_db
        from app.domains.games.models import Bundle, BundleRegionPrice

        bid = 990_433  # 单 DLC sub 桩（垃圾实测来源：Civ5 巴比伦类）
        await init_db()
        try:
            async with get_session_factory()() as db:
                db.add(Bundle(bundle_id=bid, name="桩_单DLC sub",
                              must_purchase_as_set=1, item_kind=1))
                db.add(BundleRegionPrice(bundle_id=bid, region_code="us",
                                          price=499, price_status="ok"))
                await db.commit()
            regions = [dict(
                bundle_id=bid, region_code="us", currency="USD", price=499,
                original_price=None, discount_percent=0, bundle_base_discount=0,
                app_ids=[16801], price_status="ok", name="x", header_image="",
                mps=1, item_kind=1, kind="packageid",
            )]
            assert await refresh._upsert_bundle_rows(
                bid, regions, {"USD": 7.0}, datetime.utcnow()
            ) is False
            async with get_session_factory()() as db:
                b = (await db.execute(
                    select(Bundle).where(Bundle.bundle_id == bid)
                )).scalar_one_or_none()
                n_prices = len((await db.execute(
                    select(BundleRegionPrice).where(
                        BundleRegionPrice.bundle_id == bid)
                )).scalars().all())
            assert b is None and n_prices == 0  # 整包删净
        finally:
            async with get_session_factory()() as db:
                await db.execute(delete(BundleRegionPrice).where(
                    BundleRegionPrice.bundle_id == bid))
                await db.execute(delete(Bundle).where(Bundle.bundle_id == bid))
                await db.commit()

    @pytest.mark.asyncio
    async def test_manual_import_allows_singleton(self) -> None:
        """手动导入例外：allow_singleton=True 保留单 appid 的 sub（用户明确意图）。"""
        from datetime import datetime

        from sqlalchemy import delete, select

        from app.core.database import get_session_factory, init_db
        from app.domains.games.models import Bundle, BundleRegionPrice

        bid = 990_434
        await init_db()
        try:
            regions = [dict(
                bundle_id=bid, region_code="us", currency="USD", price=1499,
                original_price=None, discount_percent=0, bundle_base_discount=0,
                app_ids=[440], price_status="ok", name="Prime Status Upgrade",
                header_image="", mps=1, item_kind=1, kind="packageid",
            )]
            assert await refresh._upsert_bundle_rows(
                bid, regions, {"USD": 7.0}, datetime.utcnow(),
                allow_singleton=True,
            ) is True
            async with get_session_factory()() as db:
                b = (await db.execute(
                    select(Bundle).where(Bundle.bundle_id == bid)
                )).scalar_one()
            assert b.name == "Prime Status Upgrade"
        finally:
            async with get_session_factory()() as db:
                await db.execute(delete(BundleRegionPrice).where(
                    BundleRegionPrice.bundle_id == bid))
                await db.execute(delete(Bundle).where(Bundle.bundle_id == bid))
                await db.commit()


class TestBundleDiscovery:
    """游戏条目 purchase_options → 捆绑包发现桩（链尾全量刷新统一抓价）。"""

    def test_extracts_identity_and_semantics(self) -> None:
        from app.crawler import browse_store as bs

        item = {
            "appid": 728880, "name": "Overcooked! 2", "success": 1, "visible": True,
            "purchase_options": [
                {"bundleid": 8017, "must_purchase_as_set": True,
                 "purchase_option_name": "Overcooked! 1 & 2 Bundle"},
                {"packageid": 1066582, "must_purchase_as_set": False,
                 "purchase_option_name": "Overcooked! 2 - Gourmet Edition"},
                {"packageid": 279940, "must_purchase_as_set": False,
                 "purchase_option_name": "Overcooked! 2"},  # 本体 sub 同名 → 丢弃
                {"bundleid": 8017, "must_purchase_as_set": True,
                 "purchase_option_name": "重复项去重"},
                {"packageid": 123, "must_purchase_as_set": False,
                 "purchase_option_name": ""},  # 无名选项跳过
            ],
        }
        found = bs._discover_bundles_from_item(item)
        assert [(f["bundle_id"], f["mps"], f["item_kind"]) for f in found] == [
            (8017, 1, 0), (1066582, 1, 1),
        ]
        assert found[0]["app_ids"] == [728880]
        # sub 形态语义由形态蕴含（游戏页 sub 选项的 mps 恒 False，不可照抄）
        assert found[1]["mps"] == 1

    def test_dlc_page_single_dlc_sub_dropped(self) -> None:
        """DLC 页的单 DLC sub（选项名 == DLC 名）不是捆绑包，不收。"""
        from app.crawler import browse_store as bs

        item = {
            "appid": 16801, "name": "Overcooked! 2: Season Pass",
            "success": 1, "visible": True,
            "purchase_options": [
                {"packageid": 6477, "must_purchase_as_set": False,
                 "purchase_option_name": "Overcooked! 2: Season Pass"},
                {"bundleid": 575, "must_purchase_as_set": False,
                 "purchase_option_name": "Season Pass + Extras"},
            ],
        }
        found = bs._discover_bundles_from_item(item)
        assert [(f["bundle_id"], f["item_kind"]) for f in found] == [(575, 0)]

    def test_upgrade_sub_kept_when_name_differs(self) -> None:
        """异名 sub 升级包（Orange Box / Gourmet Edition）是真实整包单元，收。"""
        from app.crawler import browse_store as bs

        item = {
            "appid": 440, "name": "Team Fortress 2", "success": 1, "visible": True,
            "purchase_options": [
                {"packageid": 469, "must_purchase_as_set": False,
                 "purchase_option_name": "The Orange Box"},
            ],
        }
        found = bs._discover_bundles_from_item(item)
        assert [(f["bundle_id"], f["mps"], f["item_kind"]) for f in found] == [(469, 1, 1)]

    def test_invisible_item_yields_nothing(self) -> None:
        from app.crawler import browse_store as bs

        assert bs._discover_bundles_from_item({"success": 15, "visible": False}) == []
        assert bs._discover_bundles_from_item(None) == []

    @pytest.mark.asyncio
    async def test_discovery_stub_insert_only(self) -> None:
        """发现桩只 INSERT 库内没有的包，既有完整主档不被覆盖。"""
        from datetime import datetime

        from sqlalchemy import delete, select

        from app.core.database import get_session_factory, init_db
        from app.crawler.db_writer import DbWriter
        from app.domains.games.models import Bundle, BundleRegionPrice

        bid_new, bid_known = 990_431, 990_432
        await init_db()
        try:
            async with get_session_factory()() as db:
                db.add(Bundle(bundle_id=bid_known, name="完整主档", must_purchase_as_set=0,
                              item_kind=0, app_ids=[728880],
                              updated_at=datetime.utcnow()))
                await db.commit()
            writer = DbWriter()
            n = await writer.record_bundle_discoveries([
                {"bundle_id": bid_new, "name": "新发现的包", "mps": 1, "item_kind": 0,
                 "app_ids": [728880]},
                {"bundle_id": bid_known, "name": "覆盖尝试", "mps": 0, "item_kind": 1,
                 "app_ids": [728880]},
            ])
            assert n == 1  # 只插入新的
            async with get_session_factory()() as db:
                new = (await db.execute(
                    select(Bundle).where(Bundle.bundle_id == bid_new)
                )).scalar_one()
                known = (await db.execute(
                    select(Bundle).where(Bundle.bundle_id == bid_known)
                )).scalar_one()
            assert (new.must_purchase_as_set, new.item_kind) == (1, 0)
            assert new.updated_at is None  # 发现桩：播种查询按 NULL 捞
            assert (known.must_purchase_as_set, known.item_kind, known.name) == (0, 0, "完整主档")
        finally:
            async with get_session_factory()() as db:
                await db.execute(delete(BundleRegionPrice).where(
                    BundleRegionPrice.bundle_id.in_([bid_new, bid_known])))
                await db.execute(delete(Bundle).where(
                    Bundle.bundle_id.in_([bid_new, bid_known])))
                await db.commit()


class TestAlignment:
    def test_positional_when_counts_match(self) -> None:
        items = [{"id": 1}, {"id": 0}, {"id": 3}]  # 未收录条目回 id=0
        assert refresh._align_items(items, [1, 2, 3]) == [(1, {"id": 1}), (2, {"id": 0}), (3, {"id": 3})]

    def test_fallback_by_id_when_counts_differ(self) -> None:
        items = [{"id": 3}, {"id": 1}]
        assert refresh._align_items(items, [1, 2, 3]) == [
            (1, {"id": 1}), (2, None), (3, {"id": 3})
        ]


class TestBundleRegionsIdentity:
    """单包出口的身份兜底：库内身份问不到真实条目 → 换另一身份再问。"""

    def test_falls_back_to_packageid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[tuple[int, int | None]]] = []

        async def fake(session, want):
            calls.append(want)
            if want[0][1] == 1:  # 换到 packageid 才有真实条目
                return {13608: [{"price_status": "ok", "mps": 1, "kind": "packageid"}]}
            return {}

        monkeypatch.setattr(refresh, "_fetch_regions_batched", fake)
        rows = _run(refresh._fetch_bundle_regions(None, 13608))
        assert [w[0][1] for w in calls] == [0, 1]
        assert rows and rows[0]["mps"] == 1

    def test_force_package_skips_bundle_probe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[tuple[int, int | None]]] = []

        async def fake(session, want):
            calls.append(want)
            return {}

        monkeypatch.setattr(refresh, "_fetch_regions_batched", fake)
        assert _run(refresh._fetch_bundle_regions(None, 1066582, force_package=True)) == []
        assert [w[0][1] for w in calls] == [1]  # 只问一次，不兜底

    def test_locked_rows_count_as_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake(session, want):
            return {13608: [{"price_status": "locked", "mps": 0, "kind": "bundleid"}]}

        monkeypatch.setattr(refresh, "_fetch_regions_batched", fake)
        rows = _run(refresh._fetch_bundle_regions(None, 13608))
        assert len(rows) == 1 and rows[0]["price_status"] == "locked"


def _run(coro):
    """跑协程（不引入 pytest-asyncio 标志，纯函数测试保持同步风格）。"""
    import asyncio

    return asyncio.run(coro)


class TestFetchRegions:
    """抓取区集：只发监控启用区；南亚（pk 聚合位）拆 pk/bd 两发。"""

    def test_fetch_ccs_follows_enabled_and_expands_south_asia(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from app.domains.regions import service as regions_service

        async def _enabled(_explicit):
            return ["cn", "pk", "us"]

        monkeypatch.setattr(regions_service, "effective_regions", _enabled)
        assert _run(refresh._bundle_fetch_ccs()) == ["cn", "pk", "bd", "us"]

    def test_fetch_regions_batched_fans_out_monitored_ccs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """整表抓取只对监控区（含南亚拆区）各发一发，不再全量 41 区。"""
        from app.domains.regions import service as regions_service

        async def _enabled(_explicit):
            return ["cn", "pk"]

        seen: list[str] = []

        async def _fake_region(session, specs, cc):
            seen.append(cc)
            assert len(specs) == 1
            return [_item(id=13608)]

        monkeypatch.setattr(regions_service, "effective_regions", _enabled)
        monkeypatch.setattr(refresh, "_fetch_browse_region", _fake_region)

        out = _run(refresh._fetch_regions_batched(None, [(13608, 0)]))

        assert sorted(seen) == ["bd", "cn", "pk"]
        assert {r["region_code"] for r in out[13608]} == {"cn", "pk", "bd"}

    def test_bd_currency_follows_south_asia_slot(self) -> None:
        """BD 不在 CC_LIST，币种随南亚位（USD）——落行不出现 None 币种。"""
        assert refresh._CC_CURRENCY["bd"] == refresh._CC_CURRENCY["pk"] == "USD"
