"""proxies 订阅域测试：手动改名 / 流量实时回填 / 级联删除 / 账本收敛 / 存活落库。

不触真实网络/内核——monkeypatch fetch_subscription_headers 与策略引擎；
库隔离到临时 sqlite（同 test_proxies_stats.py 范式）。
"""
import base64
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.models import ClashNode, Proxy, ProxySubscription

SUB_URL = "https://example.com/sub?token=abc"
USERINFO = "upload=100; download=300; total=500; expire=2524608000"


async def _no_proxy() -> None:
    """策略引擎桩：直连（避免测试环境探本地 Clash 端口）。"""
    return None


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
    import app.domains.proxies.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _add_sub(db, *, kind: str = "clash", last_stats: dict | None = None) -> int:
    async with db() as session:
        sub = ProxySubscription(kind=kind, url=SUB_URL, last_stats=last_stats)
        session.add(sub)
        await session.commit()
        return sub.id


async def _get_sub(db, sub_id: int) -> ProxySubscription:
    async with db() as session:
        return await session.get(ProxySubscription, sub_id)


# ─── 手动改名 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_set_and_clear(db):
    """改名正常落库；空串清名回 None（订阅名由用户手动维护）。"""
    sub_id = await _add_sub(db)

    renamed = await proxies_service.update_subscription_label(sub_id, "  我的机场  ")
    assert renamed["label"] == "我的机场"

    cleared = await proxies_service.update_subscription_label(sub_id, "   ")
    assert cleared["label"] is None
    assert (await _get_sub(db, sub_id)).label is None


@pytest.mark.asyncio
async def test_rename_missing_subscription(db):
    with pytest.raises(ValueError, match="订阅不存在"):
        await proxies_service.update_subscription_label(999, "x")


# ─── 流量实时回填 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_traffic_merges_last_stats(db, monkeypatch):
    """流量头局部合并进 last_stats——既有键（nodes/cached）不丢。"""

    async def _fake_headers(url, proxy_url=None):
        return {"userinfo": USERINFO}

    monkeypatch.setattr(
        clash_manager.runtime, "fetch_subscription_headers", _fake_headers
    )
    monkeypatch.setattr(proxies_service, "resolve_proxy_url", _no_proxy)

    sub_id = await _add_sub(db, last_stats={"nodes": 5, "cached": False})

    result = await proxies_service.refresh_subscription_traffic(sub_id)

    assert result["traffic"] == USERINFO
    stats = (await _get_sub(db, sub_id)).last_stats
    assert stats["traffic"] == USERINFO
    assert stats["nodes"] == 5
    assert stats["cached"] is False  # 局部合并不清空其他键


@pytest.mark.asyncio
async def test_refresh_traffic_plain_rejected(db):
    """明文订阅没有面板流量头概念，拒绝而非白跑一次外网请求。"""
    sub_id = await _add_sub(db, kind="plain")

    with pytest.raises(ValueError):
        await proxies_service.refresh_subscription_traffic(sub_id)


@pytest.mark.asyncio
async def test_refresh_traffic_no_header(db, monkeypatch):
    """面板没回流量头：报错（保留旧值，不写空统计）。"""

    async def _fake_headers(url, proxy_url=None):
        return {"userinfo": None}

    monkeypatch.setattr(
        clash_manager.runtime, "fetch_subscription_headers", _fake_headers
    )
    monkeypatch.setattr(proxies_service, "resolve_proxy_url", _no_proxy)

    sub_id = await _add_sub(db, last_stats={"traffic": "upload=1; download=2; total=3"})
    with pytest.raises(ValueError, match="流量头"):
        await proxies_service.refresh_subscription_traffic(sub_id)
    stats = (await _get_sub(db, sub_id)).last_stats
    assert stats["traffic"] == "upload=1; download=2; total=3"


# ─── 删除订阅级联清理账本 ────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_subscription_cascades_ledger(db):
    """删订阅连带删其账本行；别的订阅的行不受影响。"""
    sub_a = await _add_sub(db)
    sub_b = await _add_sub(db)
    async with db() as session:
        session.add_all(
            [
                ClashNode(subscription_id=sub_a, name="A1"),
                ClashNode(subscription_id=sub_a, name="A2"),
                ClashNode(subscription_id=sub_b, name="B1"),
            ]
        )
        await session.commit()

    assert await proxies_service.delete_subscription(sub_a) is True

    async with db() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(ClashNode))).scalars().all()
    assert sorted(r.name for r in rows) == ["B1"]


# ─── 账本收敛 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_prune_ledger_removes_stale_nodes(db):
    """已不在订阅内容里的节点行删除，当前节点保留（状态不动）。"""
    sub_id = await _add_sub(db)
    async with db() as session:
        session.add_all(
            [
                ClashNode(subscription_id=sub_id, name="节点A", status="ok", fail_count=0),
                ClashNode(subscription_id=sub_id, name="节点B", status="dead", fail_count=10),
                ClashNode(subscription_id=sub_id, name="旧节点", status="ok"),
            ]
        )
        await session.commit()

    removed = await proxies_service.prune_clash_node_ledger(sub_id, {"节点A", "节点B"})

    assert removed == 1
    async with db() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(ClashNode))).scalars().all()
    assert sorted(r.name for r in rows) == ["节点A", "节点B"]
    by_name = {r.name: r for r in rows}
    assert by_name["节点B"].status == "dead"  # 保留行的状态机原样


@pytest.mark.asyncio
async def test_prune_ledger_empty_names_is_noop(db):
    """空节点集防呆：不删（全删等于销账，宁可保留）。"""
    sub_id = await _add_sub(db)
    async with db() as session:
        session.add(ClashNode(subscription_id=sub_id, name="节点A"))
        await session.commit()

    assert await proxies_service.prune_clash_node_ledger(sub_id, set()) == 0

    async with db() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(ClashNode))).scalars().all()
    assert len(rows) == 1


# ─── 内核重启归属判断（共用缓存不串台）──────────────────────


@pytest.mark.asyncio
async def test_running_subscription_is(db, monkeypatch):
    """重启开关只在「内核启动文本含该订阅 URL」时放行——config.yaml 共用，
    重拉非当前订阅不能把内核悄悄切过去。"""
    runtime = clash_manager.runtime
    monkeypatch.setattr(
        runtime, "status",
        lambda: {"running": True, "port": 7890, "configPath": "x"},
    )

    runtime._startup_text = f"mixed-port: 7890\nproxies:\n  - name: A\n{SUB_URL}\n"
    assert runtime.running_subscription_is(SUB_URL) is True
    assert runtime.running_subscription_is("https://other.com/sub") is False

    runtime._startup_text = "proxies: []"  # URL 不在配置文本里
    assert runtime.running_subscription_is(SUB_URL) is False

    monkeypatch.setattr(
        runtime, "status",
        lambda: {"running": False, "port": None, "configPath": None},
    )
    runtime._startup_text = f"proxies:\n{SUB_URL}"
    assert runtime.running_subscription_is(SUB_URL) is False  # 没跑不放行

    runtime._startup_text = None  # 恢复，不污染进程内单例



# ─── 检测存活统计落库 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_node_results_records_alive(db):
    """节点检测后存活/总数落进订阅 last_stats（订阅行「存活 x/y」数据源），
    且账本收敛删除已下线节点的旧行。"""
    sub_id = await _add_sub(db, last_stats={"traffic": USERINFO, "nodes": 3})
    async with db() as session:
        session.add_all(
            [
                ClashNode(subscription_id=sub_id, name="节点A", status="ok"),
                ClashNode(subscription_id=sub_id, name="节点B", status="unknown"),
                ClashNode(subscription_id=sub_id, name="旧节点", status="ok"),
            ]
        )
        await session.commit()

    results = [
        {"name": "节点A", "alive": True, "steamOk": True, "exitIp": "1.1.1.1",
         "ms": 100, "duplicate": False, "probed": True},
        {"name": "节点B", "alive": False, "steamOk": False, "exitIp": None,
         "ms": 200, "duplicate": False, "probed": True},
    ]
    alive, unique_alive, deprecated = await proxies_service._apply_node_results(
        sub_id, {}, results, ["节点A", "节点B"], datetime(2026, 9, 5, 12, 0, 0)
    )

    assert alive == 1
    assert unique_alive == 1
    assert deprecated is False
    stats = (await _get_sub(db, sub_id)).last_stats
    assert stats["alive"] == 1
    assert stats["total"] == 2
    assert stats["traffic"] == USERINFO  # 合并不覆盖既有键
    assert stats["nodes"] == 3

    async with db() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(ClashNode))).scalars().all()
    assert sorted(r.name for r in rows) == ["节点A", "节点B"]  # 旧节点行已收敛


# ─── 明文订阅导入即体检（新节点入库自动校验标记）──


async def _stub_sub_fetch(monkeypatch, text: str) -> None:
    """明文订阅拉取桩：resolve 直连（同域内既有桩法），GET 返回固定行集。"""

    class _Resp:
        status_code = 200
        raise_for_status = staticmethod(lambda: None)

        @property
        def text(self):
            return text

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            return _Resp()

    monkeypatch.setattr(proxies_service, "resolve_proxy_url", _no_proxy)
    monkeypatch.setattr(proxies_service.httpx, "AsyncClient", _Client)


async def _stub_single_check(monkeypatch, results: dict[str, tuple[str, int]]) -> list[dict]:
    """_check_single 桩：按 (host, port) 回放预置结果并落 status/latency，
    记录每次调用供断言只测了新增行。返回调用清单。"""
    calls: list[tuple[str, int]] = []

    async def _fake(proxy):
        calls.append((proxy.host, proxy.port))
        status, latency = results.get((proxy.host, proxy.port), ("failed", None))
        async with proxies_service.get_session_factory()() as session:
            row = await session.get(Proxy, proxy.id)
            row.status = status
            row.latency_ms = latency
            await session.commit()
        return {**proxies_service._proxy_dict(row), "testError": None}

    monkeypatch.setattr(proxies_service, "_check_single", _fake)
    return calls


@pytest.mark.asyncio
async def test_import_plain_checks_only_new_nodes(db, monkeypatch):
    """导入新增行即时体检：stats 带 checked/alive，DB 落 ok/failed 标记；
    跳过的存量行不重测（沿用既有 status）。"""
    sub_id = await _add_sub(db, kind="plain")
    # 预置一条存量代理：1.1.1.1 已存在（将命中 skipped），9.9.9.9 为新增
    async with db() as session:
        session.add(Proxy(scheme="http", host="1.1.1.1", port=8080,
                          status="ok", enabled=True))
        await session.commit()

    await _stub_sub_fetch(
        monkeypatch, "1.1.1.1:8080\nhttp://9.9.9.9:1080\n8.8.8.8:3128\n"
    )
    calls = await _stub_single_check(
        monkeypatch,
        {
            ("9.9.9.9", 1080): ("ok", 220),
            ("8.8.8.8", 3128): ("failed", None),
        },
    )

    stats = await proxies_service.import_plain_subscription(sub_id)

    assert stats["fetched"] == 3
    assert stats["added"] == 2
    assert stats["skipped"] == 1
    assert stats["checked"] == 2  # 只实测新增行
    assert stats["alive"] == 1
    assert sorted(calls) == [("8.8.8.8", 3128), ("9.9.9.9", 1080)]

    statuses = {
        (p.host, p.port): p.status
        for p in (await proxies_service.list_proxies())
    }
    assert statuses[("1.1.1.1", 8080)] == "ok"  # 存量行沿用，未重测
    assert statuses[("9.9.9.9", 1080)] == "ok"
    assert statuses[("8.8.8.8", 3128)] == "failed"
    # last_stats 落进订阅行（前端导入消息数据源）
    assert (await _get_sub(db, sub_id)).last_stats["alive"] == 1


@pytest.mark.asyncio
async def test_import_plain_all_duplicates_no_check(db, monkeypatch):
    """全部重复（added=0）：不触发任何实测，stats 不带 checked/alive 键。"""
    sub_id = await _add_sub(db, kind="plain")
    async with db() as session:
        session.add(Proxy(scheme="http", host="1.1.1.1", port=8080, enabled=True))
        await session.commit()

    await _stub_sub_fetch(monkeypatch, "1.1.1.1:8080\n")
    calls = await _stub_single_check(monkeypatch, {})

    stats = await proxies_service.import_plain_subscription(sub_id)

    assert stats == {"fetched": 1, "added": 0, "skipped": 1}
    assert calls == []  # added=0 短路，零网络副作用


@pytest.mark.asyncio
async def test_import_plain_check_errors_do_not_break_stats(db, monkeypatch):
    """体检并发抛错（return_exceptions）：按未通过计，导入 stats 仍完整返回。"""
    sub_id = await _add_sub(db, kind="plain")
    await _stub_sub_fetch(monkeypatch, "7.7.7.7:1080\n")

    async def _boom(proxy):
        raise RuntimeError("probe timeout")

    monkeypatch.setattr(proxies_service, "_check_single", _boom)

    stats = await proxies_service.import_plain_subscription(sub_id)

    assert stats["added"] == 1
    assert stats["checked"] == 0  # 异常结果不计入 checked
    assert stats["alive"] == 0
    async with db() as session:
        from sqlalchemy import select

        rows = (await session.execute(select(Proxy))).scalars().all()
    assert len(rows) == 1 and rows[0].status == "unknown"  # 行保留，未被删除



# ── 订阅自动取名链（行为对齐：profile-title → Content-Disposition → URL 末段）──


def _headers(**kw) -> httpx.Headers:
    """httpx.Headers 构造对非 ASCII 值默认按 ascii 编码炸——真实响应里
    中文头值就是 UTF-8 字节，用 encoding='utf-8' 喂入即还原 wire 形态。"""
    return httpx.Headers(kw, encoding="utf-8")


@pytest.mark.parametrize(
    "headers,url,expected",
    [
        # 1 级：profile-title 纯文本
        (
            _headers(**{"profile-title": "某机场"}),
            "https://sub.example.com/link/ABC123",
            "某机场",
        ),
        # 1 级：profile-title base64（面板专用通道既有语义）
        (
            _headers(**{"profile-title": "base64:" + base64.b64encode("5Lid6YWJ5Y2V".encode()).decode()}),
            "https://sub.example.com/link/ABC123",
            "5Lid6YWJ5Y2V",
        ),
        # 2 级：Content-Disposition filename* RFC 5987（机场名 UTF-8 percent 编码）
        (
            _headers(**{"content-disposition": "attachment; filename*=UTF-8''%E6%9C%BA%E5%9C%BA%E5%90%8D.yaml"}),
            "https://sub.example.com/link/ABC123",
            "机场名.yaml",
        ),
        # 2 级：Content-Disposition 普通 filename=
        (
            _headers(**{"content-disposition": 'attachment; filename="airport.yaml"'}),
            "https://sub.example.com/link/ABC123",
            "airport.yaml",
        ),
        # 3 级：URL 末段（去 query + percent 解码）
        (
            _headers(),
            "https://sub.example.com/%E6%9C%BA%E5%9C%BA?clash=1&token=x",
            "机场",
        ),
        # 3 级回落：末段是 token 码（多数机场现状——名字即 token，诚实展示）
        (
            _headers(),
            "https://sub.example.com/link/ABC123?clash=1",
            "ABC123",
        ),
        # 全空：URL 末段取不到（根路径）→ None（不造假名）
        (_headers(), "https://sub.example.com/", None),
    ],
)
def test_subscription_auto_name_chain(headers, url, expected):
    from app.domains.proxies.clash_manager import subscription_auto_name

    assert subscription_auto_name(headers, url) == expected


@pytest.mark.asyncio
async def test_add_subscription_auto_names_when_label_empty(db, monkeypatch):
    """保存订阅（clash，未填名）：验证下载成功 → 自动名落库；
    手动填名时机场名不抢命名权。下载桩带 Content-Disposition。"""

    async def fake_download(sub_url, data_dir, proxy_url=None):
        return {
            "path": "/tmp/x.yaml", "title": "机场名.yaml",
            "userinfo": USERINFO, "nodes": 3, "cached": False,
        }

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "resolve_proxy_url", _no_proxy)
    # 内核已装（避免探测下载）
    monkeypatch.setattr(
        proxies_service.clash_manager, "detect_kernel", lambda d: {"found": True}
    )

    out = await proxies_service.add_subscription("clash", SUB_URL, None)
    assert out["label"] == "机场名.yaml"

    async with db() as session:
        sub = await session.get(ProxySubscription, out["id"])
        assert sub.label == "机场名.yaml"

    # 手动填名优先：自动名不覆写用户输入
    out2 = await proxies_service.add_subscription(
        "clash", "https://example.com/other", "我的机场"
    )
    assert out2["label"] == "我的机场"


@pytest.mark.asyncio
async def test_add_subscription_no_title_keeps_null(db, monkeypatch):
    """三级取名全空（如 token 末段也取不到）：label 保持 null——
    不落假名，前端回落显示 URL。"""

    async def fake_download(sub_url, data_dir, proxy_url=None):
        return {"path": "/tmp/x.yaml", "title": None, "userinfo": None,
                "nodes": 3, "cached": False}

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "resolve_proxy_url", _no_proxy)
    monkeypatch.setattr(
        proxies_service.clash_manager, "detect_kernel", lambda d: {"found": True}
    )

    out = await proxies_service.add_subscription("clash", SUB_URL, None)
    assert out["label"] is None
