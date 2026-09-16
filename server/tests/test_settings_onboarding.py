"""settings 域 KV 标志测试：onboarding / 自动价格链 / 更新提示锚点。
直调 router 函数（项目测试风格，不起 TestClient 避免 lifespan 副作用）。

注意：stub 的 P 类必须列全 SettingsUpdate 的所有字段——router 直接读属性，
少一个就 AttributeError。新增 KV 时记得回来补。"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.settings import router as settings_router
from app.domains.settings import service as settings_service


@pytest.fixture
def db(tmp_path, monkeypatch):
    import asyncio

    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)

    async def init_db():
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    asyncio.run(init_db())
    yield factory
    asyncio.run(engine.dispose())


def payload_stub(**overrides):
    """SettingsUpdate 桩：默认全 None（不改任何键），按需覆盖。"""
    values = {
        "account": None,
        "onboarding_done": None,
        "auto_price": None,
        "update_notified": None,
        "update_notify": None,
        "update_auto": None,
        "theme": None,
    }
    values.update(overrides)
    return type("P", (), values)()


@pytest.mark.asyncio
async def test_first_launch_flag_default_false(db):
    payload = await settings_router.get_settings()
    assert payload.onboarding_done is False


@pytest.mark.asyncio
async def test_auto_price_flag_default_true_and_roundtrip(db):
    """自动价格链开关：默认 True（自动开）；PUT False 后 GET 持久 False；
    与 onboarding 正交（KV 各键独立）。"""
    assert (await settings_router.get_settings()).auto_price is True

    payload = await settings_router.update_settings(payload_stub(auto_price=False))
    assert payload.auto_price is False
    # 新请求（等价重启）仍为 False；onboarding 不受牵连
    fresh = await settings_router.get_settings()
    assert fresh.auto_price is False
    assert fresh.onboarding_done is False


@pytest.mark.asyncio
async def test_done_flag_roundtrip(db):
    payload = await settings_router.update_settings(payload_stub(onboarding_done=True))
    assert payload.onboarding_done is True
    # 新请求（等价重启）仍为 True
    assert (await settings_router.get_settings()).onboarding_done is True


@pytest.mark.asyncio
async def test_update_notified_default_empty_and_roundtrip(db):
    """更新提示锚点：默认空串（= 从未提示）；写入版本号后持久，
    且与 onboarding / auto_price 正交。"""
    assert (await settings_router.get_settings()).update_notified == ""

    payload = await settings_router.update_settings(payload_stub(update_notified="0.2.0"))
    assert payload.update_notified == "0.2.0"
    fresh = await settings_router.get_settings()
    assert fresh.update_notified == "0.2.0"
    assert fresh.onboarding_done is False
    assert fresh.auto_price is True

    # 只改 onboarding 不动提示锚点
    await settings_router.update_settings(payload_stub(onboarding_done=True))
    assert (await settings_router.get_settings()).update_notified == "0.2.0"


@pytest.mark.asyncio
async def test_update_notify_and_auto_defaults_and_roundtrip(db):
    """更新提示默认「开」、静默自动更新默认「关」，且两者与提示锚点正交。

    默认值即产品立场：提示默认开（有新版该告诉用户）；静默自动更新默认关
    ——后台悄悄下 100MB+ 是「用户没同意就不该做」的事，必须显式开启。
    """
    fresh = await settings_router.get_settings()
    assert fresh.update_notify is True
    assert fresh.update_auto is False

    payload = await settings_router.update_settings(
        payload_stub(update_notify=False, update_auto=True)
    )
    assert payload.update_notify is False
    assert payload.update_auto is True

    # 等价重启：新请求仍保持；两个开关互不牵连，也不动提示锚点
    again = await settings_router.get_settings()
    assert again.update_notify is False
    assert again.update_auto is True
    assert again.update_notified == ""

    await settings_router.update_settings(payload_stub(update_notify=True))
    third = await settings_router.get_settings()
    assert third.update_notify is True
    assert third.update_auto is True, "改提示开关不得顺手把静默更新关掉"


@pytest.mark.asyncio
async def test_account_update_orthogonal(db):
    class Account:
        steam_id = "76561198000000001"
        steam_api_key = None

    payload = await settings_router.update_settings(payload_stub(account=Account()))
    assert payload.account["steam_id"] == "76561198000000001"
    assert payload.onboarding_done is False
