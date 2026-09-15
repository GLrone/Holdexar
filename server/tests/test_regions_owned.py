"""regions 域单元测试：owned_regions KV 读写与校验（tmp sqlite，不触网络）。"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.regions import service as regions_service
from app.domains.settings import service as settings_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module
    from app.domains.bundles import service as bundles_service

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(regions_service, "get_session_factory", lambda: factory)

    # set_enabled 会联动捆绑包排序快照整库重建（追踪区集合变化的写侧事件）：
    # 本文件只测 regions 域语义，打桩隔离，避免触碰真实库
    async def _noop_rebuild(*args, **kwargs):
        return 0

    monkeypatch.setattr(bundles_service, "refresh_bundle_sort_cache", _noop_rebuild)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.regions.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest.mark.asyncio
async def test_owned_regions_default_follow(db):
    """缺省 = None（跟随启用集），读写往返一致。"""
    assert await regions_service.owned_regions() is None

    got = await regions_service.set_owned_regions(["cn", "us", "ar"])
    assert got == ["cn", "us", "ar"]
    assert await regions_service.owned_regions() == ["cn", "us", "ar"]

    # 回跟随
    got = await regions_service.set_owned_regions(None)
    assert got is None
    assert await regions_service.owned_regions() is None


@pytest.mark.asyncio
async def test_owned_regions_validation(db):
    """非法 code / 空列表 / 归一化（大写转小写、去重去空格）。"""
    with pytest.raises(ValueError, match="未知区服代码"):
        await regions_service.set_owned_regions(["cn", "xx"])

    with pytest.raises(ValueError, match="不能为空"):
        await regions_service.set_owned_regions([])

    with pytest.raises(ValueError, match="不能为空"):
        await regions_service.set_owned_regions(["", "  "])

    got = await regions_service.set_owned_regions(["US", "cn", " US "])
    assert got == ["us", "cn"]


@pytest.mark.asyncio
async def test_owned_regions_empty_list_rejected_not_stored(db):
    """空列表被拒后不落库——不会把『跟随』意外写成空集。"""
    await regions_service.set_owned_regions(["cn"])
    with pytest.raises(ValueError):
        await regions_service.set_owned_regions([])
    assert await regions_service.owned_regions() == ["cn"]


@pytest.mark.asyncio
async def test_seed_default_first_four_enabled(db):
    """首启种子：只默认勾选 CC_LIST 前四区（cn/ru/kz/ua），其余不勾。

    存量用户配置不回改：set_enabled 后再跑种子（含 CC_LIST 换名/新区），
    已有行 enabled 保持用户设定。
    """
    from app.crawler.config import CC_LIST

    await regions_service.ensure_seeded()
    regions = await regions_service.list_regions()
    enabled = [r["code"] for r in regions if r["enabled"]]
    assert enabled == [c for c, _, _ in CC_LIST[:4]]

    # 用户改配置后，种子再跑（新 code 补行等路径）不得动已有行的 enabled
    await regions_service.set_enabled(["tr", "ar"])
    await regions_service.ensure_seeded()
    regions = await regions_service.list_regions()
    enabled = [r["code"] for r in regions if r["enabled"]]
    assert enabled == ["tr", "ar"]
