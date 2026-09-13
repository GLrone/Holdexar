"""捆绑包价格归一验收：存量单位迁移（schema v2）+ 落库时的大小写重复行清理。

迁移部分用 tmp 独立库（对齐 test_schema_migrations 的夹具模式），直接调用迁移体并
重放一次，验证三种历史写法都归位且**幂等**（重放不写库）：
- 「元价 + fen=price×rate×100」：Bundle 轨产物 → 只需 price ×100；
- 「分价 + fen=price×rate×100」：Sub 轨产物 → 只需纠正虚高 100 倍的 cny_fen；
- 「元价 + fen=price×rate」：更早版本产物 → price 与 fen 同时 ×100。
另验证：小数货币行、无汇率行、无 cny_fen 行都不动（不猜）。
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import database as database_module  # noqa: E402

JPY = 0.05  # 合成汇率：便于口算
VND = 0.0002
CPC = 0.001


@pytest_asyncio.fixture
async def isolated_db(tmp_path: Path, monkeypatch):
    """tmp 独立 SQLite 库：schema 就绪（user_version=1），迁移链保持真实登记。"""
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    monkeypatch.setattr(database_module, "get_engine", lambda: engine)
    monkeypatch.setattr(
        database_module,
        "get_session_factory",
        lambda: async_sessionmaker(engine, expire_on_commit=False),
    )
    await database_module.init_db()
    yield engine
    await engine.dispose()


async def _seed(engine, rows: list[tuple]) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("INSERT OR REPLACE INTO fx_rates (currency_code, rate_to_cny) VALUES (:c, :r)"),
            [{"c": c, "r": r} for c, r in (("JPY", JPY), ("VND", VND), ("CRC", CPC), ("USD", 7.0))],
        )
        await conn.execute(text("DELETE FROM bundle_region_prices"))
        await conn.execute(text("DELETE FROM bundles"))
        for bid, mps, rc, cur, price, fen in rows:
            await conn.execute(
                text("INSERT OR REPLACE INTO bundles"
                     " (bundle_id, name, must_purchase_as_set, item_kind, is_lowest, view_count)"
                     " VALUES (:b, :n, :m, :m, 0, 0)"),
                {"b": bid, "n": f"桩{bid}", "m": mps},
            )
            await conn.execute(
                text("INSERT INTO bundle_region_prices "
                     "(bundle_id, region_code, currency, price, cny_fen, price_status,"
                     " discount_percent, bundle_base_discount)"
                     " VALUES (:b, :r, :c, :p, :f, 'ok', 0, 0)"),
                {"b": bid, "r": rc, "c": cur, "p": price, "f": fen},
            )


async def _prices(engine) -> dict[tuple[int, str], tuple[int | None, int | None]]:
    async with engine.begin() as conn:
        rows = (
            await conn.execute(
                text("SELECT bundle_id, region_code, price, cny_fen FROM bundle_region_prices")
            )
        ).all()
    return {(int(b), str(r)): (p, f) for b, r, p, f in rows}


async def _apply(engine) -> None:
    """让 v2 在已播种数据上跑一次，再直接重放迁移体一次以验幂等。

    夹具里的 init_db 已在空表上跑过 v2（user_version=2），故先把账本退回 1，
    否则差额迁移会跳过——这正是中断续跑/重放场景。
    """
    async with engine.begin() as conn:
        await conn.execute(text("PRAGMA user_version = 1"))
    await database_module._run_schema_migrations()
    async with engine.begin() as conn:
        await database_module._migrate_bundle_price_units(conn)


@pytest.mark.asyncio
async def test_three_historical_shapes_normalized_and_idempotent(isolated_db) -> None:
    engine = isolated_db
    await _seed(engine, [
        # 参考行（小数货币，约定无歧义）：每包一条，量级即该包真实 CNY 分
        (900_001, 0, "us", "USD", 1000, 7000),
        (900_002, 1, "us", "USD", 900, 6000),
        (900_003, 1, "us", "USD", 1000, 7000),
        # ① 元价 + fen=price×rate×100（Bundle 轨）
        (900_001, 0, "jp", "JPY", 6237, round(6237 * JPY * 100)),
        # ② 分价 + fen=price×rate×100（Sub 轨，cny_fen 虚高 100 倍）
        (900_002, 1, "id", "JPY", 100_000, round(100_000 * JPY * 100)),
        # ③ 元价 + fen=price×rate（更早版本：两者同时小 100 倍）
        (900_003, 1, "vn", "VND", 291_500, round(291_500 * VND)),
        # ④ 小数货币 legacy 比例缺陷行（不属本次归一范围）
        (900_004, 0, "cr", "CRC", 5_436, 80),
        # ⑤ 无汇率行：PGY 汇率在下面删掉
        (900_005, 0, "py", "PYG", 1_000, 999),
        # ⑥ 无 cny_fen 行
        (900_006, 0, "jp", "JPY", 1_000, None),
    ])
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM fx_rates WHERE currency_code = 'PYG'"))

    await _apply(engine)
    after = await _prices(engine)

    assert after[(900_001, "jp")] == (623_700, round(6237 * JPY * 100))  # ① 价格 ×100，fen 保留
    assert after[(900_002, "id")] == (100_000, round(100_000 * JPY))  # ② 价格不动，fen 纠正
    assert after[(900_003, "vn")] == (29_150_000, round(291_500 * VND * 100))  # ③ 两者都 ×100
    assert after[(900_004, "cr")] == (5_436, 80)  # ④ 小数货币不动
    assert after[(900_005, "py")] == (1_000, 999)  # ⑤ 无汇率不动
    assert after[(900_006, "jp")] == (1_000, None)  # ⑥ 无 cny_fen 不动

    async with engine.begin() as conn:
        version = (await conn.execute(text("PRAGMA user_version"))).scalar_one()
        kinds = dict(
            (await conn.execute(
                text("SELECT bundle_id, item_kind FROM bundles")
            )).all()
        )
    assert version == database_module.SCHEMA_VERSION >= 2
    # v3 形态回填：item_kind 沿用 mps 旧值（形状当时即 mps 的旧语义之一）
    assert kinds == {900_001: 0, 900_002: 1, 900_003: 1, 900_004: 0, 900_005: 0, 900_006: 0}

    # 幂等：再重放迁移体，逐行与首次结果一致
    await _apply(engine)
    assert await _prices(engine) == after


@pytest.mark.asyncio
async def test_upsert_drops_uppercase_duplicate_rows() -> None:
    """落库清理：大小写重复的历史区行以本次小写行为权威（否则展示层会挑中未归一旧值）。"""
    from app.core.database import get_session_factory, init_db
    from app.domains.bundles import refresh
    from app.domains.games.models import Bundle, BundleRegionPrice

    bid = 990_401
    await init_db()
    try:
        async with get_session_factory()() as db:
            db.add(Bundle(bundle_id=bid, name="桩_大小写重复", must_purchase_as_set=None))
            db.add_all([
                BundleRegionPrice(bundle_id=bid, region_code="JP", currency="JPY",
                                  price=623700, cny_fen=26_126, price_status="ok"),
                BundleRegionPrice(bundle_id=bid, region_code="us", currency="USD",
                                  price=999, cny_fen=999, price_status="ok"),
            ])
            await db.commit()

        regions = [{
            "bundle_id": bid, "region_code": "jp", "currency": "JPY", "price": 623_700,
            "original_price": None, "discount_percent": 0, "bundle_base_discount": 0,
            "app_ids": [728880, 858240], "price_status": "ok", "name": "新名",
            "header_image": "", "mps": 0, "item_kind": 0, "kind": "bundleid",
        }]
        assert await refresh._upsert_bundle_rows(
            bid, regions, {"JPY": JPY}, datetime.utcnow()
        ) is True

        async with get_session_factory()() as db:
            codes = sorted(
                r.region_code
                for r in (
                    await db.execute(
                        select(BundleRegionPrice).where(BundleRegionPrice.bundle_id == bid)
                    )
                ).scalars().all()
            )
            row = (
                await db.execute(select(Bundle).where(Bundle.bundle_id == bid))
            ).scalar_one()
        assert codes == ["jp"]  # 大写的 'JP' 旧行被清掉
        assert row.must_purchase_as_set == 0  # mps 由 item_type 填 0
    finally:
        from sqlalchemy import delete

        async with get_session_factory()() as db:
            await db.execute(delete(BundleRegionPrice).where(BundleRegionPrice.bundle_id == bid))
            await db.execute(delete(Bundle).where(Bundle.bundle_id == bid))
            await db.commit()
