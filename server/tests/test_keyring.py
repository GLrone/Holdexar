"""密钥托管（keyring）+ 换钥重加密（rekey）+ 敏感数据加密导出（data_export）测试。

覆盖：三模式切换后的读写闭环、口令模式的锁定/解锁、换钥后既有密文可解、导出
只含用户侧数据且口令加密可回读、锁定态导出被拒。网络零涉及；库与密钥材料
（secret.salt / secret.master）均为独立临时目录。
"""
import sys
import types
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import data_export, keyring, rekey, secretbox
from app.core.database import Base
from app.core.secretbox import SecretBoxError
from app.domains.account import service as account_service
from app.domains.account.models import SteamAccount
from app.domains.proxies.models import ProxySubscription
from app.domains.settings import service as settings_service
from app.domains.settings.models import AppSetting

A = "76561198000000001"


# ── 夹具 ────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """数据目录与密钥材料隔离到临时目录；进程态密钥缓存按用例清空。"""
    monkeypatch.setattr(secretbox, "_data_dir", lambda: tmp_path)
    monkeypatch.setattr(
        data_export,
        "get_settings",
        lambda: types.SimpleNamespace(
            data_dir=tmp_path, app_name="Holdexar", version="0.0.0-test"
        ),
    )
    keyring.reset_state()
    secretbox.clear_key_cache()
    yield
    keyring.reset_state()
    secretbox.clear_key_cache()


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(account_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(rekey, "get_session_factory", lambda: factory)
    monkeypatch.setattr(data_export, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.account.models  # noqa: F401
    import app.domains.proxies.models  # noqa: F401
    import app.domains.proxypool.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_user_rows() -> None:
    """一条解密后应可读的用户数据：账号 Cookie + 订阅链接 + API Key 设置。"""
    async with rekey.get_session_factory()() as session:
        session.add(
            SteamAccount(
                steam_id=A,
                cookies=secretbox.encrypt_secret("COOKIE-DATA", "steam-cookies"),
                is_active=True,
            )
        )
        session.add(
            ProxySubscription(
                kind="clash",
                url=secretbox.encrypt_secret(
                    "https://sub.example/url", "proxy-subscription"
                ),
            )
        )
        await session.commit()
    await settings_service.set_secret_value("account.steam_api_key", "APIKEY-123")


async def _read_cookie() -> str:
    async with rekey.get_session_factory()() as session:
        row = await session.get(SteamAccount, A)
        return row.cookies if row else ""


# ── 模式与锁定 ──────────────────────────────────────────


def test_default_mode_is_legacy(tmp_path):
    assert keyring.mode_of(tmp_path) == keyring.MODE_LEGACY
    assert keyring.is_locked(tmp_path) is False


@pytest.mark.asyncio
async def test_passphrase_enable_locks_then_unlocks(db, tmp_path):
    await _seed_user_rows()
    ct = await _read_cookie()

    await rekey.switch_mode("passphrase", passphrase="pw-123456")
    assert keyring.mode_of(tmp_path) == keyring.MODE_PASSPHRASE
    assert keyring.is_locked(tmp_path) is False  # 刚设置即当前会话口令

    # 模拟重启：清进程态 → 锁定
    keyring.reset_state()
    secretbox.clear_key_cache()
    assert keyring.is_locked(tmp_path) is True
    assert await settings_service.get_secret_value("account.steam_api_key", "") == ""
    with pytest.raises(SecretBoxError):
        secretbox.decrypt_secret(ct, "steam-cookies")

    # 错误口令不解锁
    assert keyring.unlock(tmp_path, "wrong") is False
    # 正确口令解锁 → 换钥后的密文可解
    assert keyring.unlock(tmp_path, "pw-123456") is True
    secretbox.clear_key_cache()
    assert await settings_service.get_secret_value("account.steam_api_key") == "APIKEY-123"
    assert secretbox.decrypt_secret(await _read_cookie(), "steam-cookies") == "COOKIE-DATA"


@pytest.mark.asyncio
async def test_switch_back_to_legacy_reencrypts(db, tmp_path):
    await _seed_user_rows()
    await rekey.switch_mode("passphrase", passphrase="pw-123456")
    await rekey.switch_mode("legacy", current_passphrase="pw-123456")

    keyring.reset_state()
    secretbox.clear_key_cache()
    assert keyring.mode_of(tmp_path) == keyring.MODE_LEGACY
    assert keyring.is_locked(tmp_path) is False
    assert secretbox.decrypt_secret(await _read_cookie(), "steam-cookies") == "COOKIE-DATA"
    assert await settings_service.get_secret_value("account.steam_api_key") == "APIKEY-123"


@pytest.mark.asyncio
async def test_switch_requires_current_passphrase_when_locked(db, tmp_path):
    await _seed_user_rows()
    await rekey.switch_mode("passphrase", passphrase="pw-123456")
    keyring.reset_state()
    secretbox.clear_key_cache()
    with pytest.raises(keyring.KeyringError):
        await rekey.switch_mode("legacy")  # 未提供当前口令
    with pytest.raises(keyring.KeyringError):
        await rekey.switch_mode("legacy", current_passphrase="nope")


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows 可用")
@pytest.mark.asyncio
async def test_dpapi_mode_roundtrip(db, tmp_path):
    await _seed_user_rows()
    ct = await _read_cookie()

    await rekey.switch_mode("dpapi")
    keyring.reset_state()
    secretbox.clear_key_cache()
    assert keyring.mode_of(tmp_path) == keyring.MODE_DPAPI
    assert keyring.is_locked(tmp_path) is False  # 系统凭据，无需口令
    assert secretbox.decrypt_secret(ct, "steam-cookies") == "COOKIE-DATA"
    assert await settings_service.get_secret_value("account.steam_api_key") == "APIKEY-123"


# ── 导出 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_export_excludes_seed_and_decrypts_secrets(db, tmp_path):
    await _seed_user_rows()
    await settings_service.set_value("seed.imported_version", "2026-01-01")
    await settings_service.set_value("ui.theme", "light")

    payload = await data_export.build_payload()

    # 种子 / 公共目录表不进导出
    for table in ("games", "game_current_prices", "game_price_history", "fx_rates",
                  "crawl_regions", "bundles"):
        assert table not in payload["tables"]
    # 种子 marker 键被剔除
    keys = {row["key"]: row["value"] for row in payload["tables"]["app_settings"]}
    assert "seed.imported_version" not in keys
    assert keys["ui.theme"] == "light"
    # 凭据解密后随正文导出
    assert keys["account.steam_api_key"] == "APIKEY-123"
    assert payload["tables"]["steam_accounts"][0]["cookies"] == "COOKIE-DATA"
    assert payload["tables"]["proxy_subscriptions"][0]["url"] == "https://sub.example/url"

    # 口令加密 → 回读
    envelope = data_export.encrypt_payload(payload, "exportpw")
    assert envelope["cipher"] == "AES-256-GCM"
    back = data_export.decrypt_payload(envelope, "exportpw")
    assert back["counts"] == payload["counts"]
    with pytest.raises(SecretBoxError):
        data_export.decrypt_payload(envelope, "wrong-pw")


@pytest.mark.asyncio
async def test_create_export_writes_file(db, tmp_path):
    await _seed_user_rows()
    info = await data_export.create_export("exportpw")
    out = Path(info["path"])
    assert out.is_file() and out.suffix == data_export.EXPORT_SUFFIX
    assert data_export.safe_export_path(out.name) == out
    assert [item["name"] for item in data_export.list_exports()] == [out.name]
    with pytest.raises(ValueError):
        await data_export.create_export("short")
    with pytest.raises(ValueError):
        data_export.safe_export_path("../escape.hxexport")


@pytest.mark.asyncio
async def test_export_blocked_when_locked(db, tmp_path):
    await _seed_user_rows()
    await rekey.switch_mode("passphrase", passphrase="pw-123456")
    keyring.reset_state()
    secretbox.clear_key_cache()
    with pytest.raises(SecretBoxError):
        await data_export.build_payload()
