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

from app.core import secretbox
from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.subscription_secret import open_url
from app.domains.proxies.models import ClashNode, Proxy, ProxySubscription
from app.domains.settings import service as settings_service

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
    # 启动候选读「最近显式选中」KV，settings 会话同样打桩到本库
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
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


# ─── 编辑订阅（改名 + 换链接自动重拉）─────────────────────────


@pytest.mark.asyncio
async def test_update_subscription_rename_only(db):
    """只改名（不传链接）：不触发重拉，落库即返；空串清名。"""
    sub_id = await _add_sub(db)

    res = await proxies_service.update_subscription(sub_id, label="  我的机场  ")
    assert res["synced"] is False
    assert res["label"] == "我的机场"
    assert res["url"] == SUB_URL
    assert (await _get_sub(db, sub_id)).label == "我的机场"

    res2 = await proxies_service.update_subscription(sub_id, label="   ")
    assert res2["label"] is None


@pytest.mark.asyncio
async def test_update_subscription_url_change_triggers_sync(db, monkeypatch):
    """换链接的 clash 订阅保存即自动重拉：synced=True，节点/流量并道回传。"""
    sub_id = await _add_sub(db)
    calls: list[int] = []

    async def fake_refresh(sid):
        calls.append(sid)
        return {
            "id": sid, "label": "新名字", "nodes": 7, "traffic": USERINFO,
            "alive": 3, "total": 7, "usedCache": False,
            "restarted": False, "prunedLedger": 0,
        }

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", fake_refresh)

    res = await proxies_service.update_subscription(
        sub_id, label="新名字", url="https://example.com/sub?token=xyz"
    )
    assert calls == [sub_id]
    assert res["synced"] is True
    assert res["nodes"] == 7
    assert res["traffic"] == USERINFO
    assert res["label"] == "新名字"
    assert secretbox.is_encrypted((await _get_sub(db, sub_id)).url)
    assert open_url((await _get_sub(db, sub_id)).url) == "https://example.com/sub?token=xyz"


@pytest.mark.asyncio
async def test_update_subscription_same_url_no_sync(db, monkeypatch):
    """链接没变（同值再存）：不触发重拉。"""
    sub_id = await _add_sub(db)

    async def boom(sid):
        raise AssertionError("链接未变化不应触发重拉")

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", boom)
    res = await proxies_service.update_subscription(sub_id, url=SUB_URL)
    assert res["synced"] is False


@pytest.mark.asyncio
async def test_update_subscription_plain_url_change_no_sync(db, monkeypatch):
    """明文订阅换链接不自动重拉（导入是显式动作）。"""
    sub_id = await _add_sub(db, kind="plain")

    async def boom(sid):
        raise AssertionError("明文订阅换链接不应触发重拉")

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", boom)
    res = await proxies_service.update_subscription(
        sub_id, url="https://example.com/plain2"
    )
    assert res["synced"] is False
    assert res["url"] == "https://example.com/plain2"


@pytest.mark.asyncio
async def test_update_subscription_sync_failure_keeps_changes(db, monkeypatch):
    """重拉失败不回滚改名换链：warning 回传，库里的新名新链保留。"""
    sub_id = await _add_sub(db)

    async def failing(sid):
        raise ValueError("订阅下载失败: 全部通道不可用")

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", failing)
    res = await proxies_service.update_subscription(
        sub_id, label="新名字", url="https://example.com/sub?token=xyz"
    )
    assert res["synced"] is False
    assert "自动重拉失败" in res["warning"]
    row = await _get_sub(db, sub_id)
    assert row.label == "新名字"
    assert secretbox.is_encrypted(row.url)
    assert open_url(row.url) == "https://example.com/sub?token=xyz"


@pytest.mark.asyncio
async def test_update_subscription_invalid_url(db):
    sub_id = await _add_sub(db)
    with pytest.raises(ValueError, match="http\\(s\\) URL"):
        await proxies_service.update_subscription(sub_id, url="ftp://bad")


@pytest.mark.asyncio
async def test_update_subscription_missing(db):
    with pytest.raises(ValueError, match="订阅不存在"):
        await proxies_service.update_subscription(999, label="x")


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
    """重启开关只认「启动时登记的订阅 URL」——config.yaml 共用且会被任何一次
    下载覆写，文件本身不是依据；重拉非当前订阅不能把内核悄悄切过去。"""
    runtime = clash_manager.runtime
    monkeypatch.setattr(
        runtime, "status",
        lambda: {"running": True, "port": 7890, "configPath": "x"},
    )

    runtime.subscription_url = SUB_URL
    assert runtime.running_subscription_is(SUB_URL) is True
    assert runtime.running_subscription_is("https://other.com/sub") is False

    runtime.subscription_url = None  # 旧内核没登记归属
    assert runtime.running_subscription_is(SUB_URL) is False

    monkeypatch.setattr(
        runtime, "status",
        lambda: {"running": False, "port": None, "configPath": None},
    )
    runtime.subscription_url = SUB_URL
    assert runtime.running_subscription_is(SUB_URL) is False  # 没跑不放行

    runtime.subscription_url = None  # 恢复，不污染进程内单例


@pytest.mark.asyncio
async def test_start_switches_subscription_when_running(monkeypatch, tmp_path):
    """内核已在跑时点「启动」换了订阅 → 停旧实例按新订阅重启，不是幂等早退
    （否则界面怎么换选订阅，检测永远测的是自启拉起的那条）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "mixed-port: 17890\nproxies:\n  - name: A\n    type: ss\n"
        "    server: s.example.net\n    port: 8388\n    cipher: aes-128-gcm\n    password: p\n",
        encoding="utf-8",
    )

    class _FakeProc:
        pid = 111
        returncode = 0
        args: list = []
        _finished = False

        def poll(self):
            # 双语义：直用 Popen（内核进程句柄）时 None=在跑；
            # communicate 之后（subprocess.run 校验路径）返回退出码 0
            return 0 if self._finished else None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        # 启动链上的 subprocess.run（内核版本探测等）把 Popen 当上下文管理器用
        def communicate(self, input=None, timeout=None):
            self._finished = True
            return ("", "")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(clash_manager.subprocess, "Popen", lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(clash_manager.ClashRuntime, "_kill_orphans", lambda self, exe, cfgp: 0)
    monkeypatch.setattr(clash_manager, "ensure_kernel_files", lambda d: None)

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    assert r.subscription_url == "https://a.example/sub"
    first = r.process

    r.start("exe", str(cfg), subscription_url="https://b.example/sub")
    assert r.subscription_url == "https://b.example/sub", "不同订阅必须切换"
    assert r.process is not first, "切换 = 停旧起新"

    same = r.process
    r.start("exe", str(cfg), subscription_url="https://b.example/sub")
    assert r.process is same, "同订阅幂等：沿用运行中的实例"
    r.stop()
    assert r.subscription_url is None



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
    assert stats["checked"] == 2  # 只检测新增行
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
    """全部重复（added=0）：不触发任何检测，stats 不带 checked/alive 键。"""
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

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
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

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
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


# ─── 热重载切换（内核进程不动）──────────────────────────────

_MINI_CONFIG = (
    "mixed-port: 17890\nproxies:\n  - name: A节点\n    type: ss\n"
    "    server: s.example.net\n    port: 8388\n    cipher: aes-128-gcm\n    password: p\n"
)
_MINI_CONFIG_B = _MINI_CONFIG.replace("A节点", "B节点").replace("17890", "17891")


class _FakeResp:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeControllerClient:
    """假控制器：PUT /configs 收款、/version 200、/proxies 只认给定节点集。"""

    known: set[str] = set()
    puts: list[str] = []

    def __init__(self, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def put(self, url, **kw):
        type(self).puts.append(url)
        return _FakeResp(204)

    async def get(self, url, **kw):
        if url.endswith("/version"):
            return _FakeResp(200, {"version": "fake"})
        return _FakeResp(200, {"proxies": {name: {} for name in type(self).known}})


def _patch_kernel_launch(monkeypatch):
    class _FakeProc:
        pid = 111
        returncode = 0
        args: list = []
        _finished = False

        def poll(self):
            # 双语义：直用 Popen（内核进程句柄）时 None=在跑；
            # communicate 之后（subprocess.run 校验路径）返回退出码 0
            return 0 if self._finished else None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

        # 启动链上的 subprocess.run（内核版本探测等）把 Popen 当上下文管理器用
        def communicate(self, input=None, timeout=None):
            self._finished = True
            return ("", "")

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(clash_manager.subprocess, "Popen", lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(clash_manager.ClashRuntime, "_kill_orphans", lambda self, exe, cfgp: 0)
    monkeypatch.setattr(clash_manager, "ensure_kernel_files", lambda d: None)


@pytest.mark.asyncio
async def test_ensure_running_switches_via_hot_reload(monkeypatch, tmp_path):
    """在跑时换订阅：控制器 PUT /configs 热重载生效——进程对象不变、归属翻转、
    配置文件带注入段。与 Clash Verge Rev 的换配置路径同形态。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MINI_CONFIG, encoding="utf-8")
    _patch_kernel_launch(monkeypatch)

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    first_proc = r.process

    cfg.write_text(_MINI_CONFIG_B, encoding="utf-8")
    _FakeControllerClient.known = {"B节点", "GLOBAL", "DIRECT", "REJECT", "COMPATIBLE", "Pass"}
    _FakeControllerClient.puts = []
    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _FakeControllerClient)
    # lane 端口存活是真 socket 探测，单测无真内核——桩掉，真内核场景由隔离实例验证覆盖
    monkeypatch.setattr(clash_manager, "_port_listening", lambda port: True)

    outcome = await r.ensure_running("exe", str(cfg), subscription_url="https://b.example/sub")
    assert outcome["reloaded"] is True and outcome["started"] is False
    assert r.process is first_proc, "热重载不换进程"
    assert r.subscription_url == "https://b.example/sub"
    assert r.port == 17891, "混合端口跟随新配置"
    assert any("/configs" in u for u in _FakeControllerClient.puts)
    text = Path(cfg).read_text(encoding="utf-8")
    # external-controller 只经命令行下发（resolve_controller 契约），文件里不该有
    assert "HlProbeLane" in text and "external-controller" not in text
    r.stop()


@pytest.mark.asyncio
async def test_ensure_running_falls_back_to_restart_when_reload_fails(monkeypatch, tmp_path):
    """控制器拒绝热重载 → 回退停旧起新（进程换新、归属正确），不把用户留在旧配置。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MINI_CONFIG, encoding="utf-8")
    _patch_kernel_launch(monkeypatch)

    class _RefusingClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, **kw):
            raise httpx.ConnectError("controller down")

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    first_proc = r.process
    cfg.write_text(_MINI_CONFIG_B, encoding="utf-8")
    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _RefusingClient)

    outcome = await r.ensure_running("exe", str(cfg), subscription_url="https://b.example/sub")
    assert outcome["started"] is True and outcome["reloaded"] is False
    assert r.process is not first_proc, "回退路径 = 停旧起新"
    assert r.subscription_url == "https://b.example/sub"
    r.stop()


@pytest.mark.asyncio
async def test_ensure_running_same_subscription_unchanged_is_idle(monkeypatch, tmp_path):
    """同订阅且内容没变：不发起热重载（零控制器调用），幂等返回。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MINI_CONFIG, encoding="utf-8")
    _patch_kernel_launch(monkeypatch)

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    _FakeControllerClient.puts = []
    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _FakeControllerClient)

    outcome = await r.ensure_running("exe", str(cfg), subscription_url="https://a.example/sub")
    assert outcome["reloaded"] is False and outcome["started"] is False
    assert _FakeControllerClient.puts == [], "内容没变不打控制器"
    r.stop()


# ─── 下载通道顺序（代理优先，直连垫底）──────────────────────


def test_download_attempts_proxy_channels_before_direct(monkeypatch):
    """内核在跑、有已存代理、本地口可达 → 直连排最后（订阅域名直连多数
    被墙，直连打头会让每次刷新都先白等一次超时）。"""
    monkeypatch.setattr(clash_manager, "LOCAL_MIXED_PORTS", (7897,))
    monkeypatch.setattr(clash_manager, "_port_reachable", lambda port: port == 7897)
    attempts = clash_manager._download_attempts(
        runtime_port=17890, proxy_url=["http://1.2.3.4:8080"]
    )
    labels = [label for _proxy, label in attempts]
    assert labels == ["经内核代理", "经已保存代理", "本地混合端口 7897", "直连"]


def test_download_attempts_direct_only_without_kernel(monkeypatch):
    """首次添加订阅（内核没跑、无已存代理）→ 直连是唯一通道。"""
    monkeypatch.setattr(clash_manager, "LOCAL_MIXED_PORTS", ())
    attempts = clash_manager._download_attempts(runtime_port=None, proxy_url=None)
    assert attempts == [(None, "直连")]


# ─── 热重载被拒：保留运行内核，不重启陪葬 ───────────────────


@pytest.mark.asyncio
async def test_ensure_running_reload_rejected_keeps_running_kernel(monkeypatch, tmp_path):
    """内核明确拒载（HTTP 4xx）：磁盘回滚到运行配置、内核进程不动、
    报用户语言错误——回退重启只会载着同一份坏配置起不来。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MINI_CONFIG, encoding="utf-8")
    _patch_kernel_launch(monkeypatch)

    class _RefusingKernel:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, **kw):
            return _FakeResp(400)

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    first_proc = r.process

    # 重拉已把新下载写入缓存文件（真实流程），内核随后拒载：
    cfg.write_text(_MINI_CONFIG_B, encoding="utf-8")
    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _RefusingKernel)

    with pytest.raises(ValueError, match="内核拒绝新配置"):
        await r.ensure_running("exe", str(cfg), subscription_url="https://b.example/sub")

    assert r.process is first_proc, "拒载不重启：内核继续跑旧配置"
    assert r.subscription_url == "https://a.example/sub", "归属不翻转"
    # 回滚 = 文件恢复到本进程写入前的内容（B 的原始下载，无注入段）
    assert cfg.read_text(encoding="utf-8") == _MINI_CONFIG_B


@pytest.mark.asyncio
async def test_ensure_running_rejects_malformed_config_before_kernel(monkeypatch, tmp_path):
    """新配置连 YAML 都不是：预检直接拒绝，不写盘、不碰内核、不重启。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MINI_CONFIG, encoding="utf-8")
    _patch_kernel_launch(monkeypatch)

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    first_proc = r.process

    broken = "proxies:\n  - {name:Broken, type: ss\n"
    cfg.write_text(broken, encoding="utf-8")

    class _NeverClient:
        def __init__(self, **kw):
            raise AssertionError("坏配置不该走到控制器")

    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _NeverClient)

    with pytest.raises(ValueError, match="合法 YAML"):
        await r.ensure_running("exe", str(cfg), subscription_url="https://b.example/sub")

    assert r.process is first_proc
    assert r.subscription_url == "https://a.example/sub"
    assert cfg.read_text(encoding="utf-8") == broken, "预检在写盘前拒绝：文件保持原样"


# ─── 启动候选遍历（订阅失效不连累内核启动）──────────────────


async def _add_labeled_sub(db, *, label: str, url: str = SUB_URL,
                           deprecated: bool = False) -> int:
    async with db() as session:
        sub = ProxySubscription(
            kind="clash", url=url, label=label, deprecated=deprecated,
        )
        session.add(sub)
        await session.commit()
        return sub.id


@pytest.mark.asyncio
async def test_resolve_startable_falls_back_to_other_subscription(db, monkeypatch, tmp_path):
    """请求的订阅取不到配置（下载失败且无缓存）→ 降级到其余可用订阅拉起；
    被淘汰候选记入 attempts 供前端提示。"""
    dead = await _add_labeled_sub(db, label="快冲云", url="https://example.com/dead")
    live = await _add_labeled_sub(db, label="SakuraCat")

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
        if "dead" in sub_url:
            raise RuntimeError("Client error '404 Not Found'")
        return {"path": str(target_path), "title": None, "userinfo": None,
                "nodes": 3, "cached": False}

    async def no_saved_proxies():
        return []

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "_saved_proxy_candidates", no_saved_proxies)

    picked = await proxies_service.resolve_startable_clash(tmp_path, requested_id=dead)
    assert picked["subscription"]["id"] == live
    assert picked["configPath"].endswith(f"clash-sub-{live}.yaml")
    assert [a["id"] for a in picked["attempts"]] == [dead]
    assert "404" in picked["attempts"][0]["error"]


@pytest.mark.asyncio
async def test_resolve_startable_all_fail_raises_aggregated(db, monkeypatch, tmp_path):
    """所有候选都取不到配置 → ValueError 聚合各候选失败原因（面向用户）。"""
    id_a = await _add_labeled_sub(db, label="甲机场")
    id_b = await _add_labeled_sub(db, label="乙机场")

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
        raise RuntimeError("Client error '404 Not Found'")

    async def no_saved_proxies():
        return []

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "_saved_proxy_candidates", no_saved_proxies)

    with pytest.raises(ValueError) as ei:
        await proxies_service.resolve_startable_clash(tmp_path)
    msg = str(ei.value)
    assert "所有可用订阅都取不到配置" in msg
    assert "甲机场" in msg and "乙机场" in msg
    assert {id_a, id_b}  # 两条都进了候选（顺序断言在下方用例）


@pytest.mark.asyncio
async def test_resolve_startable_skips_deprecated_and_orders_newest_first(
    db, monkeypatch, tmp_path,
):
    """无指定订阅：候选按新→旧排序、废弃行剔除；只有排头可成功时选中排头。"""
    await _add_labeled_sub(db, label="老订阅")
    await _add_labeled_sub(db, label="新死的", deprecated=True)
    newest = await _add_labeled_sub(db, label="新可用")
    called: list[int] = []

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
        # 记录调用顺序：无法从 url 反推 id，借 target_path 文件名取候选 id
        sub_id = int(Path(target_path).stem.split("-")[-1])
        called.append(sub_id)
        if called[-1] != newest:
            raise RuntimeError("Client error '404 Not Found'")
        return {"path": str(target_path), "title": None, "userinfo": None,
                "nodes": 3, "cached": False}

    async def no_saved_proxies():
        return []

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "_saved_proxy_candidates", no_saved_proxies)

    picked = await proxies_service.resolve_startable_clash(tmp_path)
    assert picked["subscription"]["id"] == newest
    assert called[0] == newest, "无指定时先试最新的可用订阅"


@pytest.mark.asyncio
async def test_resolve_startable_prefers_fresh_over_stale_cache(db, monkeypatch, tmp_path):
    """最新那条已失效、只能读到本地缓存时不得粘住启动：继续找能在线取到的新鲜配置。

    缓存命中只说明曾经下载成功过；若直接返回，内核会被旧缓存钉死在一条死订阅上，
    在线可用的订阅永远轮不到，定时重拉也只会反复读这份旧缓存。
    """
    fresh = await _add_labeled_sub(db, label="在线可用", url="https://example.com/fresh")
    stale = await _add_labeled_sub(db, label="失效但有缓存", url="https://example.com/stale")
    called: list[int] = []

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
        sub_id = int(Path(target_path).stem.split("-")[-1])
        called.append(sub_id)
        return {
            "path": str(target_path), "title": None, "userinfo": None, "nodes": 3,
            "cached": "stale" in sub_url,
        }

    async def no_saved_proxies():
        return []

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "_saved_proxy_candidates", no_saved_proxies)

    picked = await proxies_service.resolve_startable_clash(tmp_path)
    assert picked["subscription"]["id"] == fresh, "必须选能在线取到的，而不是更新的缓存"
    assert picked["cached"] is False
    assert called[0] == stale, "先试最新那条（只能读缓存）"


@pytest.mark.asyncio
async def test_resolve_startable_uses_cache_only_when_nothing_fresh(
    db, monkeypatch, tmp_path,
):
    """全部候选都只能读缓存时仍要能拉起内核：返回第一条缓存回退候选。"""
    first = await _add_labeled_sub(db, label="甲", url="https://example.com/a")
    newest = await _add_labeled_sub(db, label="乙", url="https://example.com/b")

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
        return {"path": str(target_path), "title": None, "userinfo": None,
                "nodes": 3, "cached": True}

    async def no_saved_proxies():
        return []

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "_saved_proxy_candidates", no_saved_proxies)

    picked = await proxies_service.resolve_startable_clash(tmp_path)
    assert picked["cached"] is True
    assert picked["subscription"]["id"] == newest, "无新鲜候选时按新→旧取第一条缓存"
    assert first != newest


@pytest.mark.asyncio
async def test_resolve_startable_honors_remembered_selection(db, monkeypatch, tmp_path):
    """无显式请求时吃落库的「最近显式选中」：用户点选过的订阅先试，不再默认最新；
    指向已删除订阅时自然落空，按新→旧兜底。"""
    older = await _add_labeled_sub(db, label="甲机场", url="https://example.com/a")
    newest = await _add_labeled_sub(db, label="乙机场", url="https://example.com/b")
    called: list[int] = []

    async def fake_download(sub_url, data_dir, proxy_url=None, *, target_path=None):
        sub_id = int(Path(target_path).stem.split("-")[-1])
        called.append(sub_id)
        return {"path": str(target_path), "title": None, "userinfo": None,
                "nodes": 3, "cached": False}

    async def no_saved_proxies():
        return []

    monkeypatch.setattr(
        proxies_service.clash_manager.runtime, "download_subscription", fake_download
    )
    monkeypatch.setattr(proxies_service, "_saved_proxy_candidates", no_saved_proxies)

    await proxies_service.remember_selected_clash_sub(older)
    picked = await proxies_service.resolve_startable_clash(tmp_path)
    assert picked["subscription"]["id"] == older, "用户点选过的订阅先试"
    assert called[0] == older

    await proxies_service.remember_selected_clash_sub(9999)
    picked = await proxies_service.resolve_startable_clash(tmp_path)
    assert picked["subscription"]["id"] == newest, "落库选择失效时按新→旧兜底"


@pytest.mark.asyncio
async def test_remembered_selection_roundtrip(db):
    """点选 KV 读写回路：缺省 None，写入后原样读回。"""
    assert await proxies_service.remembered_clash_sub_id() is None
    await proxies_service.remember_selected_clash_sub(7)
    assert await proxies_service.remembered_clash_sub_id() == 7


@pytest.mark.asyncio
async def test_download_subscription_cache_fallback_when_all_channels_fail(
    monkeypatch, tmp_path,
):
    """全部下载通道失败且该订阅有本地缓存 → 用缓存启动语义成立（cached=True）；
    无缓存才抛最后一错。这是「订阅失效不让内核起不来」的兜底一环。"""
    r = clash_manager.ClashRuntime()

    class _DownClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, **kw):
            raise httpx.ConnectError("all channels down")

    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _DownClient)

    cache = clash_manager.kernel_dir(tmp_path) / "config.yaml"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(_MINI_CONFIG, encoding="utf-8")

    meta = await r.download_subscription("https://dead.example/sub", tmp_path)
    assert meta["cached"] is True
    assert meta["nodes"] == 1

    empty_dir = tmp_path / "fresh"
    with pytest.raises(httpx.ConnectError):
        await r.download_subscription("https://dead.example/sub", empty_dir)
