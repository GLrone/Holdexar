"""汇率刷新原子快照验收（rates → cny_fen → games/bundles 排序快照）。

架构约束（本用例验收对象）：
- refresh_rates 单事务完成「汇率 → 全部 cny_fen → games sort → bundles sort」，
  GET 只可能读到旧快照或新快照，不会出现 rates=新 / cny_fen=旧 / diff=旧；
- 重算数据源是**本事务内的 fx_rates 行**，不依赖进程内 get_rates() 缓存
  ——用例把 get_rates 打桩成抛异常，刷新仍必须成功；
- 无对应币种汇率 / 无单价的行保留原值（不回写 NULL、不猜）。

隔离：tmp 库 + get_session_factory 打桩（test_price_repair_schedule 模式）；
汇率源打桩（不出网）；追踪区打桩为 None（等价未启用任何区 = 全量不过滤）。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.core.database as database_module  # noqa: E402
from app.core.database import Base  # noqa: E402
from app.domains.bundles import service as bundles_service  # noqa: E402
from app.domains.games import service as games_service  # noqa: E402
from app.domains.games.models import (  # noqa: E402
    Bundle,
    BundleRegionPrice,
    Game,
    GameCurrentPrice,
)
from app.domains.rates import service as rates_service  # noqa: E402
from app.domains.rates.models import FxRate  # noqa: E402

APPID = 998_101  # 主验收：CN + US 两区
APPID_TRY = 998_102  # 无汇率币种：保留原值
BID = 998_301

OLD_USD_RATE = 8.0  # 种子汇率
NEW_USD_RATE = 7.0  # 本轮刷新汇率


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    for mod in (rates_service, games_service, bundles_service):
        monkeypatch.setattr(mod, "get_session_factory", lambda: factory)

    async def _all_regions():
        return None

    monkeypatch.setattr(bundles_service, "_tracked_region_codes", _all_regions)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def _seed(db):
    """旧汇率（USD=8）下的完整快照：cny_fen 与排序快照都是旧值。"""
    async with db() as session:
        session.add(FxRate(currency_code="CNY", rate_to_cny=1.0))
        session.add(FxRate(currency_code="USD", rate_to_cny=OLD_USD_RATE))
        # 旧快照：US 500 分 ×8 = 4000 → diff = 10000 − 4000 = 6000
        session.add(
            Game(
                appid=APPID, name="快照游戏",
                min_cny_fen=4000, diff_fen=6000,
            )
        )
        session.add(
            Game(appid=APPID_TRY, name="无汇率币种", min_cny_fen=1234, diff_fen=0)
        )
        session.add_all(
            [
                GameCurrentPrice(
                    appid=APPID, region_code="CN", currency="CNY",
                    price=10000, original_price=10000, discount_percent=0,
                    price_status="ok", cny_fen=10000,
                ),
                GameCurrentPrice(
                    appid=APPID, region_code="US", currency="USD",
                    price=500, original_price=500, discount_percent=0,
                    price_status="ok", cny_fen=4000,
                ),
                # 无对应汇率（TRY 不在本轮 fx_rates 里）：保留原值
                GameCurrentPrice(
                    appid=APPID_TRY, region_code="TR", currency="TRY",
                    price=5000, original_price=5000, discount_percent=0,
                    price_status="ok", cny_fen=1234,
                ),
            ]
        )
        session.add(Bundle(bundle_id=BID, name="快照包", must_purchase_as_set=0))
        session.add_all(
            [
                BundleRegionPrice(
                    bundle_id=BID, region_code="cn", currency="CNY",
                    price=20000, price_status="ok", cny_fen=20000,
                    app_ids=[APPID, 1],
                ),
                BundleRegionPrice(
                    bundle_id=BID, region_code="us", currency="USD",
                    price=500, price_status="ok", cny_fen=4000,
                    app_ids=[APPID, 1],
                ),
            ]
        )
        await session.commit()


def _stub_rate_source(monkeypatch):
    """汇率源打桩：augmentedsteam 返回 USD=7（不出网）。"""
    async def _fake() -> dict[str, float]:
        return {"USD": NEW_USD_RATE}

    monkeypatch.setattr(rates_service, "_fetch_augmentedsteam", _fake)


@pytest.mark.asyncio
async def test_refresh_rates_rebuilds_snapshot_in_one_transaction(db, monkeypatch):
    """一次刷新把汇率 + 两域 cny_fen + 两域排序快照全部推进到新口径。"""
    _stub_rate_source(monkeypatch)

    # 关键反证：重算不得依赖进程内汇率缓存（300s TTL 的「DB 新/cache 旧」）
    async def _boom():
        raise AssertionError("recompute 不应调用 get_rates()（进程内汇率缓存）")

    monkeypatch.setattr(games_service, "get_rates", _boom)

    result = await rates_service.refresh_rates()
    assert result["source"] == "augmentedsteam"
    assert result["count"] == 2  # USD + 强制 CNY
    # 两表都被重算（命中汇率的行；不依赖 driver 对「同值更新」是否计入 rowcount）
    assert result["recomputed"]["games"] >= 1
    assert result["recomputed"]["bundles"] >= 1

    async with db() as session:
        rates = {
            r.currency_code: r.rate_to_cny
            for r in (await session.execute(select(FxRate))).scalars()
        }
        game = (
            await session.execute(select(Game).where(Game.appid == APPID))
        ).scalar_one()
        game_try = (
            await session.execute(select(Game).where(Game.appid == APPID_TRY))
        ).scalar_one()
        prices = {
            (p.appid, p.region_code): p.cny_fen
            for p in (await session.execute(select(GameCurrentPrice))).scalars()
        }
        bundle = (
            await session.execute(select(Bundle).where(Bundle.bundle_id == BID))
        ).scalar_one()
        bundle_prices = {
            p.region_code: p.cny_fen
            for p in (
                await session.execute(
                    select(BundleRegionPrice).where(BundleRegionPrice.bundle_id == BID)
                )
            ).scalars()
        }

    # ① 汇率快照
    assert rates["USD"] == NEW_USD_RATE
    # ② cny_fen（500 × 7 = 3500）
    assert prices[(APPID, "US")] == 3500
    assert prices[(APPID, "CN")] == 10000
    # ③ games 排序快照：min 3500 / diff = 10000 − 3500
    assert game.min_cny_fen == 3500
    assert game.diff_fen == 6500
    # ④ bundles 排序快照：min 3500 / diff = 20000 − 3500
    assert bundle_prices["us"] == 3500
    assert bundle.min_cny_fen == 3500
    assert bundle.diff_fen == 16500
    assert bundle.is_lowest is False
    # ⑤ 无汇率币种：保留原值，不回写 NULL、不被清零
    assert prices[(APPID_TRY, "TR")] == 1234
    assert game_try.min_cny_fen == 1234


@pytest.mark.asyncio
async def test_refresh_rates_rolls_back_all_on_failure(db, monkeypatch):
    """排序快照重建失败 → 汇率也回滚：宁可整体不更新，也不留半刷新状态。"""
    _stub_rate_source(monkeypatch)

    async def _boom(*args, **kwargs):
        raise RuntimeError("排序快照重建失败")

    monkeypatch.setattr(games_service, "refresh_sort_cache", _boom)

    with pytest.raises(RuntimeError):
        await rates_service.refresh_rates()

    async with db() as session:
        usd = (
            await session.execute(
                select(FxRate).where(FxRate.currency_code == "USD")
            )
        ).scalar_one()
        price = (
            await session.execute(
                select(GameCurrentPrice).where(
                    GameCurrentPrice.appid == APPID,
                    GameCurrentPrice.region_code == "US",
                )
            )
        ).scalar_one()
    # 汇率与 cny_fen 都停在旧口径（同事务回滚），没有半态
    assert usd.rate_to_cny == OLD_USD_RATE
    assert price.cny_fen == 4000
