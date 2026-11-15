"""领航员用量台账与上限迁移写的闸位回归。"""
import asyncio

import pytest

from app.core.database import init_db
from app.domains.pilot import config
from app.domains.settings import service as settings_service


@pytest.mark.asyncio
async def test_concurrent_add_usage_accumulates_exactly():
    """并发轮同键累加：写闸串行化读改写，五轮并发后总数精确等于各轮之和
    （无闸读改写会互相覆盖少计——usage 少计导致 cap>0 时熔断漏触）。"""
    await init_db()
    await asyncio.gather(*(config.add_usage(100, 50) for _ in range(5)))
    usage = await config.usage_month()
    assert usage["inp"] == 500
    assert usage["out"] == 250
    assert usage["calls"] == 5


@pytest.mark.asyncio
async def test_legacy_cap_migration_writes_through_gate():
    """旧默认上限一次性迁为不限：迁移写与其他领航员配置写点同闸位，
    迁移后 KV 落 0、平面配置读到不限。"""
    await init_db()
    await settings_service.set_value(config._CAP_KEY, config._LEGACY_DEFAULT_CAP)
    cfg = await config.load_config()
    assert cfg["monthly_cap"] == 0
    assert await settings_service.get_value(config._CAP_KEY, None) == 0
