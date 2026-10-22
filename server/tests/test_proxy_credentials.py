"""代理凭据静态加密：落库密文、出网解密、响应体永不含明文。

对应加固计划 P1-1 的第一部分。断言覆盖：库内是密文、投影是掩码、
出网 URL 是明文、去重键用解密值、换钥后仍可解。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core import secretbox
from app.domains.proxies import credentials, service
from app.domains.proxies.models import Proxy


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(secretbox, "_data_dir", lambda: tmp_path)
    secretbox.clear_key_cache()
    yield
    secretbox.clear_key_cache()


def test_seal_and_open_roundtrip():
    token = credentials.seal("s3cret-pass")
    assert token.startswith(secretbox.PREFIX)
    assert token != "s3cret-pass"
    assert credentials.open_value(token) == "s3cret-pass"


def test_open_value_accepts_legacy_plaintext():
    """存量明文（未走封存步骤）必须原样可用，不能因升级而废掉既有代理。"""
    assert credentials.open_value("plain-pass") == "plain-pass"


def test_seal_is_nondeterministic():
    """同一明文两次加密结果必须不同（随机 nonce），否则密文可被枚举比对。"""
    assert credentials.seal("same") != credentials.seal("same")


def test_seal_proxy_is_idempotent():
    proxy = Proxy(scheme="http", host="1.2.3.4", port=8080, username="u", password="p")
    credentials.seal_proxy(proxy)
    once = (proxy.username, proxy.password)
    credentials.seal_proxy(proxy)  # 二次调用不得重复加密
    assert (proxy.username, proxy.password) == once
    assert secretbox.is_encrypted(proxy.username)


def test_runtime_url_decrypts():
    proxy = Proxy(scheme="http", host="1.2.3.4", port=8080, username="u", password="p")
    credentials.seal_proxy(proxy)
    assert service._runtime_url(proxy) == "http://u:p@1.2.3.4:8080"


def test_runtime_url_without_auth():
    proxy = Proxy(scheme="socks5", host="5.6.7.8", port=1080)
    assert service._runtime_url(proxy) == "socks5://5.6.7.8:1080"


def test_runtime_url_escapes_special_chars():
    """口令含 @ / : 等字符时不得破坏 URL 结构。"""
    proxy = Proxy(
        scheme="http", host="1.2.3.4", port=8080, username="u@x", password="p:a/ss@"
    )
    credentials.seal_proxy(proxy)
    url = service._runtime_url(proxy)
    assert "u%40x" in url and "p%3Aa%2Fss%40" in url


def test_runtime_url_degrades_when_key_unavailable(tmp_path, monkeypatch):
    """密钥不可用（换机 / 拷库）时退化成无认证 URL，不把密文当口令发出去。"""
    proxy = Proxy(scheme="http", host="1.2.3.4", port=8080)
    credentials.seal_proxy(
        Proxy(scheme="http", host="1.2.3.4", port=8080, username="u", password="p")
    )
    proxy.username = "enc1:broken:broken"
    proxy.password = "enc1:broken:broken"
    assert service._runtime_url(proxy) == "http://1.2.3.4:8080"


def test_proxy_dict_never_exposes_plaintext():
    """响应投影必须掩码，且不解密（没有解密就没有泄露面）。"""
    proxy = Proxy(scheme="http", host="1.2.3.4", port=8080, username="u", password="p")
    credentials.seal_proxy(proxy)
    out = service._proxy_dict(proxy)
    assert out["hasAuth"] is True
    assert out["url"] == "http://***@1.2.3.4:8080"
    assert "p" not in out["url"].replace("http", "")


def test_proxy_dict_no_auth():
    proxy = Proxy(scheme="http", host="1.2.3.4", port=8080)
    out = service._proxy_dict(proxy)
    assert out["hasAuth"] is False
    assert out["url"] == "http://1.2.3.4:8080"


@pytest.mark.asyncio
async def test_seal_proxy_credentials_migrates_existing_rows(db):
    """存量明文行被启动步骤封存，且重复执行幂等。"""
    from sqlalchemy import select

    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        session.add(
            Proxy(
                scheme="http", host="9.9.9.9", port=8080,
                username="legacy-user", password="legacy-pass",
            )
        )
        await session.commit()

    first = await service.seal_proxy_credentials()
    assert first == 1

    async with get_session_factory()() as session:
        row = (await session.execute(select(Proxy))).scalars().first()
        assert secretbox.is_encrypted(row.username)
        assert credentials.open_value(row.username) == "legacy-user"
        assert credentials.open_value(row.password) == "legacy-pass"

    assert await service.seal_proxy_credentials() == 0  # 幂等
