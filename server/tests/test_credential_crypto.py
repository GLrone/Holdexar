"""凭据静态加密测试：AES-256-GCM 封装/解封、用途隔离与篡改拒解、机器指纹
变化后的不可解降级、密文落库（Cookie / API Key）、存量明文启动封存。
网络零涉及；库与密钥材料（secret.salt）均为独立临时目录。"""
import base64
import json
import sys
import time
import types
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import secretbox
from app.core.database import Base
from app.core.secretbox import SecretBoxError
from app.domains.account import service as account_service
from app.domains.account.models import SteamAccount
from app.domains.settings import service as settings_service

A = "76561198000000001"
_DAY = 86400.0


# ── Cookie / JWT 构造（同 test_account_session 口径）──────

def _jwt(claims: dict) -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'EdDSA', 'typ': 'JWT'})}.{seg(claims)}.sig"


def cookie_str(*, access_exp: float) -> str:
    return "; ".join(
        [
            "sessionid=abc123",
            "steamCountry=CN%7C",
            f"steamLoginSecure={A}%7C%7C{_jwt({'sub': A, 'exp': access_exp})}",
            f"steamRefresh_steam={A}%7C%7C{_jwt({'sub': A, 'aud': ['web', 'renew'], 'exp': time.time() + 200 * _DAY})}",
        ]
    )


# ── 夹具 ────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _key_material(tmp_path, monkeypatch):
    """密钥材料（盐 / 兜底密钥）隔离到临时目录；用途密钥缓存按用例清空。"""
    monkeypatch.setattr(secretbox, "_data_dir", lambda: tmp_path)
    secretbox.clear_key_cache()
    yield
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
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed(steam_id: str, cookies: str) -> None:
    async with account_service.get_session_factory()() as session:
        session.add(SteamAccount(steam_id=steam_id, cookies=cookies, is_active=True))
        await session.commit()


async def _row(steam_id: str) -> SteamAccount:
    async with account_service.get_session_factory()() as session:
        return await session.get(SteamAccount, steam_id)


# ── secretbox 原语 ──────────────────────────────────────

def test_encrypt_decrypt_roundtrip_and_random_nonce():
    ct1 = secretbox.encrypt_secret("hello-credential", "p1")
    ct2 = secretbox.encrypt_secret("hello-credential", "p1")
    assert ct1.startswith("enc1:") and ct1 != ct2  # 每次随机 nonce
    assert secretbox.is_encrypted(ct1) and not secretbox.is_encrypted("raw-plain")
    assert secretbox.decrypt_secret(ct1, "p1") == "hello-credential"
    assert secretbox.encrypt_secret("", "p1") == ""
    assert secretbox.decrypt_secret("", "p1") == ""


def test_purpose_isolation():
    ct = secretbox.encrypt_secret("secret", "p1")
    with pytest.raises(SecretBoxError):
        secretbox.decrypt_secret(ct, "p2")


def test_tampered_ciphertext_rejected():
    ct = secretbox.encrypt_secret("secret", "p1")
    nonce_b64, _, sealed_b64 = ct[len(secretbox.PREFIX):].partition(":")
    raw = bytearray(base64.urlsafe_b64decode(sealed_b64))
    raw[0] ^= 0x01
    tampered = (
        secretbox.PREFIX + nonce_b64 + ":"
        + base64.urlsafe_b64encode(bytes(raw)).decode("ascii")
    )
    with pytest.raises(SecretBoxError):
        secretbox.decrypt_secret(tampered, "p1")
    with pytest.raises(SecretBoxError):
        secretbox.decrypt_secret("not-a-sealed-value", "p1")


def test_key_stable_across_cache_clear():
    ct = secretbox.encrypt_secret("secret", "p1")
    secretbox.clear_key_cache()  # 等价进程重启：同一数据目录重新派生
    assert secretbox.decrypt_secret(ct, "p1") == "secret"


def test_machine_fingerprint_change_invalidates(monkeypatch):
    ct = secretbox.encrypt_secret("secret", "p1")
    monkeypatch.setattr(secretbox, "_machine_fingerprint", lambda: b"another-machine")
    secretbox.clear_key_cache()
    with pytest.raises(SecretBoxError):
        secretbox.decrypt_secret(ct, "p1")


# ── 账号 Cookie 密文落库 ────────────────────────────────

@pytest.mark.asyncio
async def test_save_cookies_stores_ciphertext(db):
    raw = cookie_str(access_exp=time.time() + _DAY)
    await account_service.save_cookies(raw)

    row = await _row(A)
    assert secretbox.is_encrypted(row.cookies)
    assert "steamLoginSecure" not in row.cookies  # 明文不上库
    assert await account_service.get_cookies() == raw  # 消费侧解密还原

    status = await account_service.get_status()
    assert status["has_cookie"] is True
    assert status["session_expired"] is False
    assert status["session_expires_at"] is not None


@pytest.mark.asyncio
async def test_renewal_result_sealed(db, monkeypatch):
    """续期换发的新 Cookie 落库仍走密封；行内不出现明文。"""
    from app.domains.account import session as session_module

    expired = cookie_str(access_exp=time.time() - 10)
    await _seed(A, account_service._seal_credential(expired))
    new_access = f"{A}%7C%7C{_jwt({'sub': A, 'exp': time.time() + _DAY})}"
    transfer_url = "https://store.steampowered.com/login/settoken"

    class _FakeHeaders:
        def __init__(self, set_cookie):
            self._set_cookie = set_cookie

        def get_list(self, _name):
            return list(self._set_cookie)

    class _FakeResp:
        def __init__(self, json_body=None, set_cookie=None):
            self.status_code = 200
            self._json = json_body
            self.headers = _FakeHeaders(set_cookie or [])

        def json(self):
            return self._json

    class _FakeClient:
        def __init__(self, responses):
            self._responses = responses

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, **_kw):
            return self._responses[url]

    responses = {
        session_module.FINALIZE_URL: _FakeResp(
            json_body={"transfer_info": [{"url": transfer_url, "params": {"auth": "1"}}]}
        ),
        transfer_url: _FakeResp(set_cookie=[f"steamLoginSecure={new_access}; Path=/"]),
    }
    monkeypatch.setattr(
        session_module,
        "httpx",
        types.SimpleNamespace(AsyncClient=lambda **_kw: _FakeClient(responses)),
    )

    out = await account_service.ensure_live_session(A, expired)
    assert new_access in out
    row = await _row(A)
    assert secretbox.is_encrypted(row.cookies)
    assert new_access not in row.cookies


@pytest.mark.asyncio
async def test_plaintext_row_sealed_at_startup(db):
    """存量明文行由启动封存步骤加密；封存后消费侧照常解密，步骤幂等。"""
    raw = cookie_str(access_exp=time.time() + _DAY)
    await _seed(A, raw)

    assert await account_service.seal_credentials_at_rest() >= 1
    row = await _row(A)
    assert secretbox.is_encrypted(row.cookies)
    assert await account_service.get_cookies() == raw
    assert await account_service.seal_credentials_at_rest() == 0  # 幂等


@pytest.mark.asyncio
async def test_unreadable_ciphertext_degrades_to_expired(db, monkeypatch):
    """库文件被拷到新机（指纹不同）：解密失败按「已过期且无续期凭据」呈现，
    消费侧拿空串，行内密文保留。"""
    raw = cookie_str(access_exp=time.time() + _DAY)
    # 仅封装阶段用「另一台机器」的指纹，随后立即还原——新机上的读取侧
    # 用本机指纹派生密钥，解不开这枚密文
    original_fingerprint = secretbox._machine_fingerprint
    secretbox._machine_fingerprint = lambda: b"another-machine"
    try:
        secretbox.clear_key_cache()
        sealed = secretbox.encrypt_secret(raw, account_service._CREDENTIAL_PURPOSE)
    finally:
        secretbox._machine_fingerprint = original_fingerprint
        secretbox.clear_key_cache()
    await _seed(A, sealed)

    status = await account_service.get_status()
    assert status["has_cookie"] is True  # 绑过仍为真
    assert status["session_expired"] is True
    assert status["session_has_refresh"] is False
    assert await account_service.get_cookies() == ""

    result = await account_service._sync_wallet_of(A)
    assert result["status"] == "no_cookie"


# ── 凭据类设置键（API Key）───────────────────────────────

@pytest.mark.asyncio
async def test_api_key_secret_roundtrip(db):
    await settings_service.set_secret_value("account.steam_api_key", "fake-key-abcdef")

    stored = await settings_service.get_value("account.steam_api_key", "")
    assert secretbox.is_encrypted(str(stored))  # 落库即密文
    assert await settings_service.get_secret_value("account.steam_api_key") == "fake-key-abcdef"

    await settings_service.set_secret_value("account.steam_api_key", "")
    assert await settings_service.get_secret_value("account.steam_api_key", "") == ""


@pytest.mark.asyncio
async def test_api_key_plaintext_sealed_at_startup(db):
    await settings_service.set_value("account.steam_api_key", "plain-legacy-key")

    assert await settings_service.get_secret_value("account.steam_api_key") == "plain-legacy-key"
    assert await settings_service.seal_secret_values() == 1
    stored = await settings_service.get_value("account.steam_api_key", "")
    assert secretbox.is_encrypted(str(stored))
    assert await settings_service.get_secret_value("account.steam_api_key") == "plain-legacy-key"
    assert await settings_service.seal_secret_values() == 0
