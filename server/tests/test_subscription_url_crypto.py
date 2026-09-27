"""订阅链接静态加密测试：密封/开封往返、存量明文兼容、换链比较口径、
快照 provenance 密封与开封匹配、启动封存幂等、不可解时的用户指引。
网络层打桩，库为独立临时库。"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import yaml
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import secretbox
from app.core.database import Base
from app.domains.proxies import service as proxies_service
from app.domains.proxies.models import ProxySubscription
from app.domains.proxies.subscription_secret import (
    open_url,
    seal_url,
    url_matches,
)
from app.domains.proxypool import subscription as sub_module
from app.domains.proxypool.models import SubscriptionSnapshot
from app.domains.proxypool.subscription import (
    FetchAttempt,
    FetchResult,
    build_snapshot,
    detect_format,
    latest_snapshot,
    persist_snapshot,
)

URL_A = "https://provider.example/api/v1/client/subscribe?token=aaaa"
URL_B = "https://provider.example/api/v1/client/subscribe?token=bbbb"


@pytest.fixture(autouse=True)
def _key_material(tmp_path, monkeypatch):
    """密钥材料（盐）隔离到临时目录；用途密钥缓存按用例清空。"""
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
    monkeypatch.setattr(proxies_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.proxypool.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_sub(url: str, *, kind: str = "clash") -> int:
    async with proxies_service.get_session_factory()() as session:
        row = ProxySubscription(kind=kind, url=url)
        session.add(row)
        await session.commit()
        return row.id


async def _row_url(sub_id: int) -> str:
    async with proxies_service.get_session_factory()() as session:
        row = await session.get(ProxySubscription, sub_id)
        return row.url


# ── 原语 ────────────────────────────────────────────────

def test_seal_open_roundtrip_and_plaintext_compat():
    sealed = seal_url(URL_A)
    assert sealed.startswith("enc1:") and URL_A not in sealed
    assert open_url(sealed) == URL_A
    assert open_url(URL_A) == URL_A  # 存量明文原样可用
    assert seal_url("") == "" and open_url("") == ""


def test_url_matches_and_unreadable(monkeypatch):
    sealed = seal_url(URL_A)
    assert url_matches(sealed, URL_A) and not url_matches(sealed, URL_B)
    assert not url_matches("", URL_A) and not url_matches(None, URL_A)
    # 换机（指纹不同）后解不开 → 空串 + 不匹配
    original = secretbox._machine_fingerprint
    secretbox._machine_fingerprint = lambda: b"another-machine"
    secretbox.clear_key_cache()
    try:
        assert open_url(sealed) == ""
        assert not url_matches(sealed, URL_A)
    finally:
        secretbox._machine_fingerprint = original
        secretbox.clear_key_cache()


# ── 订阅行：落库密文、面板明文、换链比较 ─────────────────

@pytest.mark.asyncio
async def test_add_and_list_subscription_encrypted_at_rest(db, monkeypatch):
    monkeypatch.setattr(
        proxies_service.clash_manager,
        "detect_kernel",
        lambda _data_dir: {"found": True, "path": "kernel", "version": "v1"},
    )

    async def _fake_download(url, data_dir, candidates, *, target_path=None):
        return {"path": str(target_path), "nodes": 3, "cached": False, "title": "测试机场"}

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", _fake_download
    )

    result = await proxies_service.add_subscription("clash", URL_A, "测试机场")

    stored = await _row_url(result["id"])
    assert stored.startswith("enc1:") and URL_A not in stored  # 落库即密文
    listed = await proxies_service.list_subscriptions("clash")
    target = next(s for s in listed if s["id"] == result["id"])
    assert target["url"] == URL_A  # 面板取到的是开封后的明文


@pytest.mark.asyncio
async def test_update_subscription_compares_and_seals(db):
    sub_id = await _seed_sub(seal_url(URL_A))

    # 同链重存：开封比较相等，不触发换链重写（仍是密封的同一明文）
    await proxies_service.update_subscription(sub_id, url=URL_A)
    stored = await _row_url(sub_id)
    assert secretbox.is_encrypted(stored) and open_url(stored) == URL_A

    # 换链：开封比较不等 → 新链接密封落库，返回明文
    r2 = await proxies_service.update_subscription(sub_id, url=URL_B)
    assert r2["url"] == URL_B
    stored = await _row_url(sub_id)
    assert secretbox.is_encrypted(stored) and open_url(stored) == URL_B


@pytest.mark.asyncio
async def test_migrate_legacy_kv_dedupes_on_decrypted_url(db, monkeypatch):
    from app.domains.settings import service as settings_service

    # 预置一条同链密文行：迁移的去重比对要开封识别出「已存在」
    await _seed_sub(seal_url(URL_A))

    async def _get_value(key, default=None):
        return URL_A if key == "proxy.subscription_url" else default

    set_calls: list = []

    async def _set_value(key, value):
        set_calls.append((key, value))

    monkeypatch.setattr(settings_service, "get_value", _get_value)
    monkeypatch.setattr(settings_service, "set_value", _set_value)

    await proxies_service.migrate_legacy_subscription()

    rows = await proxies_service.list_subscriptions("clash")
    assert len(rows) == 1  # 同链不重复迁移
    assert set_calls  # 迁移后清 KV


# ── 快照 provenance：密封落库 + 开封匹配 ─────────────────

def _fetch_result() -> FetchResult:
    nodes = [{"name": "hk-1", "type": "ss", "server": "1.2.3.4", "port": 8388}]
    raw = yaml.safe_dump({"proxies": nodes}, allow_unicode=True).encode("utf-8")
    return FetchResult(
        raw=raw, http_status=200, content_type="text/yaml", channel="test",
        fmt=detect_format(raw), attempts=[FetchAttempt("test", True, 200, "yaml")],
    )


@pytest.mark.asyncio
async def test_snapshot_provenance_sealed_and_matched(db, tmp_path):
    sub_id = await _seed_sub(seal_url(URL_A))
    snap = build_snapshot(sub_id, URL_A, _fetch_result(), [{"name": "hk-1"}])
    async with proxies_service.get_session_factory()() as session:
        row = await persist_snapshot(session, snap, data_dir=tmp_path)
        stored = await session.get(SubscriptionSnapshot, row.id)
        assert secretbox.is_encrypted(stored.url) and URL_A not in stored.url  # 落库即密文

        got = await latest_snapshot(session, sub_id, url=URL_A)
        assert got is not None and got.url == URL_A
        # 换链后旧快照不得顶替（provenance 语义保持）
        assert await latest_snapshot(session, sub_id, url=URL_B) is None


@pytest.mark.asyncio
async def test_snapshot_unreadable_url_skipped_in_match(db, tmp_path, monkeypatch):
    """provenance 解不开的快照不参与匹配（换机场景），不误当当前 URL 的基线。"""
    sub_id = await _seed_sub(seal_url(URL_A))
    snap = build_snapshot(sub_id, URL_A, _fetch_result(), [{"name": "hk-1"}])
    async with proxies_service.get_session_factory()() as session:
        row = await persist_snapshot(session, snap, data_dir=tmp_path)
        # 把行内密文换成「另一台机器」封装的密文：开封必败
        original = secretbox._machine_fingerprint
        secretbox._machine_fingerprint = lambda: b"another-machine"
        secretbox.clear_key_cache()
        try:
            row.url = seal_url(URL_A)
        finally:
            secretbox._machine_fingerprint = original
            secretbox.clear_key_cache()
        await session.commit()

        assert await latest_snapshot(session, sub_id, url=URL_A) is None


# ── 启动封存步骤 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_seal_subscription_urls_idempotent(db):
    plain_id = await _seed_sub(URL_A)  # 存量明文行
    sealed_id = await _seed_sub(seal_url(URL_B))  # 已是密文
    await _seed_sub("", kind="plain")  # 空值跳过

    assert await proxies_service.seal_subscription_urls() == 1
    assert secretbox.is_encrypted(await _row_url(plain_id))
    assert open_url(await _row_url(plain_id)) == URL_A
    # 已是密文的行原样保留（开封后仍是同一明文）
    assert secretbox.is_encrypted(await _row_url(sealed_id))
    assert open_url(await _row_url(sealed_id)) == URL_B
    assert await proxies_service.seal_subscription_urls() == 0  # 幂等


@pytest.mark.asyncio
async def test_unreadable_row_reports_user_guidance(db, monkeypatch):
    """解不开的行：使用侧抛带用户指引的 ValueError，不外泄实现细节。"""
    sub_id = await _seed_sub(URL_A)
    await proxies_service.seal_subscription_urls()
    stored = await _row_url(sub_id)

    class _Row:
        id = sub_id
        kind = "clash"
        url = stored

    async def _fake_get(_sub_id):
        return _Row()

    monkeypatch.setattr(proxies_service, "get_subscription", _fake_get)

    # 「另一台机器」读这行密文 → 空串 → 用户语言报错（可重新添加订阅）
    original = secretbox._machine_fingerprint
    secretbox._machine_fingerprint = lambda: b"another-machine"
    secretbox.clear_key_cache()
    try:
        with pytest.raises(ValueError, match="重新添加"):
            await proxies_service.refresh_subscription_traffic(sub_id)
    finally:
        secretbox._machine_fingerprint = original
        secretbox.clear_key_cache()
