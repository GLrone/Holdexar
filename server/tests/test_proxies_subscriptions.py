"""proxies 订阅域测试：手动改名 / 流量实时回填 / 级联删除 / 账本收敛 / 存活落库。

不触真实网络/内核——替换规范下载与策略引擎；
库隔离到临时 sqlite（同 test_proxies_stats.py 范式）。
"""
import base64
import sys
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import secretbox
from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.subscription_secret import open_url
from app.domains.proxies.models import Proxy, ProxySubscription
from app.domains.proxypool import subscription as subscription_core
from app.domains.settings import service as settings_service

SUB_URL = "https://example.com/sub?token=abc"
USERINFO = "upload=100; download=300; total=500; expire=2524608000"


@pytest.mark.asyncio
async def test_single_usable_node_keeps_large_subscription_available(db):
    sid = await _add_sub(db, last_stats={"traffic": USERINFO})
    assert not await proxies_service._evaluate_subscription_deprecation(sid, 1, 232)


@pytest.mark.asyncio
async def test_expired_subscription_is_unavailable_despite_old_alive_count(db):
    sid = await _add_sub(db, last_stats={"traffic": "expire=1; total=1000; download=1"})
    assert await proxies_service._evaluate_subscription_deprecation(sid, 20, 20)


def test_subscription_resource_limits_use_saved_facts():
    expired = ProxySubscription(last_stats={"traffic": "expire=1"})
    exhausted = ProxySubscription(last_stats={"traffic": "upload=2; download=8; total=10"})
    unlimited = ProxySubscription(last_stats={"traffic": "upload=2; download=8; total=0; expire="})
    assert expired.resource_block_reason() == "订阅已到期"
    assert exhausted.resource_block_reason() == "订阅流量已用尽"
    assert unlimited.resource_block_reason() is None


@pytest.mark.asyncio
async def test_unprobed_results_preserve_previous_subscription_verdict(db, monkeypatch):
    sid = await _add_sub(db, last_stats={"alive": 1, "total": 1})
    result = await proxies_service._summarize_node_results(sid, [{'name': 'one', 'alive': False, 'probed': False}], ['one'], datetime.now(), {})
    assert result[0] == 1 and result[2] is False
    async with db() as session:
        sub = await session.get(ProxySubscription, sid)
        assert sub.last_stats["alive"] == 1 and not sub.deprecated


@pytest.mark.asyncio
async def test_subscription_list_marks_expired_resources_unavailable(db):
    sid = await _add_sub(db, last_stats={"traffic": "expire=1"})
    rows = await proxies_service.list_subscriptions("clash")
    row = next(r for r in rows if r["id"] == sid)
    assert row["deprecated"] and row["deprecatedReason"] == "订阅已到期"


@pytest.mark.asyncio
async def test_mixed_probe_failure_keeps_unprobed_healthy_node_available(db):
    from datetime import timedelta

    sid = await _add_sub(db, last_stats={"alive": 1, "total": 2})
    await _seed_registry_node(db, sid, "known", exit_ip="1.1.1.1")
    result = await proxies_service._summarize_node_results(sid, [{'name': 'known', 'alive': False, 'probed': False}, {'name': 'failed', 'alive': False, 'probed': True, 'ms': None}], ['known', 'failed'], datetime.now(), await _registry_map(db, sid, ['known', 'failed']))
    assert result == (1, 1, False)


async def _no_proxy() -> None:
    """策略引擎桩：直连（避免测试环境探本地 Clash 端口）。"""
    return None


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()
async def _add_sub(db, *, kind: str = "clash", last_stats: dict | None = None) -> int:
    async with db() as session:
        sub = ProxySubscription(kind=kind, url=SUB_URL, last_stats=last_stats)
        session.add(sub)
        await session.commit()
        return sub.id


async def _get_sub(db, sub_id: int) -> ProxySubscription:
    async with db() as session:
        return await session.get(ProxySubscription, sub_id)


async def _seed_registry_node(db, sub_id: int, name: str, *, exit_ip: str | None = None) -> None:
    """权威账本节点种子：冷却跳过节点的存活口径读它。"""
    from datetime import timedelta

    from app.domains.proxypool.models import ProxyNode
    from app.domains.proxypool.state import NODE_ACTIVE

    async with db() as session:
        session.add(ProxyNode(node_id=f'{sub_id}-{name}', runtime_name=f'{sub_id}|{name}', proxy_type='http', server='127.0.0.1', normalized_config={'name': name, 'type': 'http', 'server': '127.0.0.1', 'port': 80}, state=NODE_ACTIVE, subscription_id=sub_id, original_name=name, exit_ip=exit_ip, last_l2_at=datetime.now() - timedelta(seconds=1)))
        await session.commit()


async def _registry_map(db, sub_id: int, names: list[str]) -> dict:
    from app.domains.proxypool.models import ProxyNode

    async with db() as session:
        rows = (
            await session.execute(
                select(ProxyNode).where(
                    ProxyNode.original_name.in_(names),
                    (ProxyNode.subscription_id == sub_id)
                    | ProxyNode.runtime_name.like(f"{sub_id}|%"),
                )
            )
        ).scalars().all()
        return {n.original_name: n for n in rows if n.original_name}


# ─── 手动改名 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_set_and_clear(db):
    """改名正常落库；空串清名回 None（订阅名由用户手动维护）。"""
    sub_id = await _add_sub(db)

    renamed = await proxies_service.update_subscription(sub_id, label="  我的机场  ")
    assert renamed["label"] == "我的机场"

    cleared = await proxies_service.update_subscription(sub_id, label="   ")
    assert cleared["label"] is None
    assert (await _get_sub(db, sub_id)).label is None


@pytest.mark.asyncio
async def test_rename_missing_subscription(db):
    with pytest.raises(ValueError, match="订阅不存在"):
        await proxies_service.update_subscription(999, label="x")


# ─── 编辑订阅（改名 + 换链接自动重拉）─────────────────────────


@pytest.mark.asyncio
async def test_update_subscription_rename_only(db):
    """只改名（不传链接）：不触发重拉，落库即返；空串清名。"""
    sub_id = await _add_sub(db)

    res = await proxies_service.update_subscription(sub_id, label="  我的机场  ")
    assert res["synced"] is False
    assert res["label"] == "我的机场"
    assert res["url"] == "https://example.com/***"
    assert (await _get_sub(db, sub_id)).label == "我的机场"

    res2 = await proxies_service.update_subscription(sub_id, label="   ")
    assert res2["label"] is None


@pytest.mark.asyncio
async def test_update_subscription_url_change_triggers_sync(db, monkeypatch, tmp_path):
    """换链接的 clash 订阅保存即自动重拉：synced=True，节点/流量并道回传。"""
    from types import SimpleNamespace
    from app.domains.proxypool.subscription import subscription_url_identity

    sub_id = await _add_sub(db)
    clash_manager.write_subscription_meta(tmp_path, sub_id, {
        "urlIdentity": subscription_url_identity(SUB_URL),
    })
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )
    calls: list[int] = []
    switches: list[bool] = []

    async def fake_refresh(sid, *, auto_switch=False):
        calls.append(sid)
        switches.append(auto_switch)
        return {
            "id": sid, "label": "新名字", "nodes": 7, "traffic": USERINFO,
            "alive": 3, "total": 7, "usedCache": False,
            "restarted": False,
        }

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", fake_refresh)

    res = await proxies_service.update_subscription(
        sub_id, label="新名字", url="https://example.com/sub?token=xyz"
    )
    assert calls == [sub_id]
    # 换链接是用户主动动作：带自动切换开关（定时周期重拉不带）
    assert switches == [True]
    assert res["synced"] is True
    assert res["nodes"] == 7
    assert res["traffic"] == USERINFO
    assert res["label"] == "新名字"
    assert secretbox.is_encrypted((await _get_sub(db, sub_id)).url)
    assert open_url((await _get_sub(db, sub_id)).url) == "https://example.com/sub?token=xyz"
    assert (await _get_sub(db, sub_id)).cache_url_identity == subscription_url_identity(SUB_URL)


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
    assert res["url"] == "https://example.com/***"


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
    from app.domains.proxypool.subscription import subscription_url_identity
    assert row.cache_url_identity == subscription_url_identity(SUB_URL)


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
    """流量刷新复用统一订阅刷新并要求在线流量头。"""
    sub_id = await _add_sub(db)
    calls = []

    async def _fake_refresh(requested_id, *, _require_fresh_traffic=False):
        calls.append((requested_id, _require_fresh_traffic))
        return {"id": requested_id, "traffic": USERINFO}

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", _fake_refresh)

    result = await proxies_service.refresh_subscription_traffic(sub_id)

    assert result == {"id": sub_id, "traffic": USERINFO}
    assert calls == [(sub_id, True)]


@pytest.mark.asyncio
async def test_refresh_traffic_plain_rejected(db):
    """明文订阅没有面板流量头概念，拒绝而非白跑一次外网请求。"""
    sub_id = await _add_sub(db, kind="plain")

    with pytest.raises(ValueError):
        await proxies_service.refresh_subscription_traffic(sub_id)


@pytest.mark.asyncio
async def test_refresh_traffic_no_header(db, monkeypatch):
    """统一刷新没有在线流量头时保留旧值并报错。"""

    async def _fake_refresh(requested_id, *, _require_fresh_traffic=False):
        assert _require_fresh_traffic is True
        raise ValueError("面板未返回在线流量头")

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", _fake_refresh)

    sub_id = await _add_sub(db, last_stats={"traffic": "upload=1; download=2; total=3"})
    with pytest.raises(ValueError, match="流量头"):
        await proxies_service.refresh_subscription_traffic(sub_id)
    stats = (await _get_sub(db, sub_id)).last_stats
    assert stats["traffic"] == "upload=1; download=2; total=3"


# ─── 删除订阅级联清理账本 ────────────────────────────────────




@pytest.mark.asyncio
async def test_delete_subscription_cascades_pool_ledger(db):
    """clash 订阅删除必须级联生产池账本：来源行/快照行清、身份行保留并按状态机
    降级、仍被别的订阅提供的来源不受影响、重建被请求——否则已删订阅的健康节点
    会永远留在生产池里。"""
    from sqlalchemy import select

    from app.domains.proxypool import scheduling as pool_scheduling
    from app.domains.proxypool.models import (
        ProxyNode,
        SubscriptionSnapshot,
    )

    seen = datetime(2026, 10, 11, 12, 0, 0)
    async with db() as session:
        sub_a = ProxySubscription(kind="clash", url=SUB_URL, admission_status="ACTIVE")
        sub_b = ProxySubscription(kind="clash", url=SUB_URL, admission_status="ACTIVE")
        session.add_all([sub_a, sub_b])
        await session.flush()
        a, b = sub_a.id, sub_b.id

        def _node(node_id: str, runtime_name: str, sub_id: int) -> ProxyNode:
            return ProxyNode(node_id=node_id, runtime_name=runtime_name, subscription_id=sub_id, original_name=node_id, proxy_type='ss', server='10.0.0.1', normalized_config={'name': node_id, 'type': 'ss', 'server': '10.0.0.1', 'port': 1}, state='ACTIVE', first_seen=seen, last_seen=seen, last_source_seen=seen)

        session.add_all([
            _node("only-a", f"{a}|only-a", a),
            _node("shared-a", f"{a}|shared", a),
            _node("only-b", f"{b}|only-b", b),
            _node("shared-b", f"{b}|shared", b),
        ])
        session.add(SubscriptionSnapshot(
            subscription_id=a, sha256="sha-a", format="yaml", raw_path="a.bin",
            url="sealed-a", node_count=2, status="OK", fetched_at=seen,
        ))
        await session.commit()

    assert await proxies_service.delete_subscription(a) is True

    async with db() as session:
        sources_a = (await session.scalars(
            select(ProxyNode).where(ProxyNode.subscription_id == a))).all()
        sources_b = (await session.scalars(
            select(ProxyNode).where(ProxyNode.subscription_id == b))).all()
        snaps_a = (await session.scalars(
            select(SubscriptionSnapshot)
            .where(SubscriptionSnapshot.subscription_id == a))).all()
        nodes = {n.node_id: n for n in (await session.scalars(select(ProxyNode))).all()}
    assert sources_a == [], "已删订阅的节点必须随删清空"
    assert {s.original_name for s in sources_b} == {"shared-b", "only-b"}
    assert snaps_a == [], "已删订阅的快照行随订阅走"
    assert "only-a" not in nodes and "shared-a" not in nodes, "节点随订阅删除"
    assert nodes["only-b"].state == "ACTIVE", "别的订阅的节点不动"
    assert nodes["shared-b"].state == "ACTIVE"
    assert pool_scheduling.rebuild_pending() is True, "合格集变化必须请求重建"
    pool_scheduling.take_rebuild_pending()



# ─── 账本收敛 ────────────────────────────────────────────────






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
    monkeypatch.setattr(clash_manager.ClashRuntime, "_wait_mixed_port", lambda self: True)
    monkeypatch.setattr(clash_manager, "_port_owned_by_process", lambda *_a: True)

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
async def test_node_summary_records_alive(db):
    """节点检测后存活/总数落进订阅 last_stats（订阅行「存活 x/y」数据源），
    订阅统计合并保留流量及内容节点数。"""
    sub_id = await _add_sub(db, last_stats={"traffic": USERINFO, "nodes": 3})
    results = [
        {"name": "节点A", "alive": True, "steamOk": True, "exitIp": "1.1.1.1",
         "ms": 100, "duplicate": False, "probed": True},
        {"name": "节点B", "alive": False, "steamOk": False, "exitIp": None,
         "ms": 200, "duplicate": False, "probed": True},
    ]
    alive, unique_alive, deprecated = await proxies_service._summarize_node_results(sub_id, results, ['节点A', '节点B'], datetime(2026, 9, 5, 12, 0, 0), {})

    assert alive == 1
    assert unique_alive == 1
    assert deprecated is False
    stats = (await _get_sub(db, sub_id)).last_stats
    assert stats["alive"] == 1
    assert stats["total"] == 2
    assert stats["traffic"] == USERINFO  # 合并不覆盖既有键
    assert stats["nodes"] == 3

# ─── 明文订阅导入即体检（新节点入库自动校验标记）──


async def _stub_sub_fetch(monkeypatch, text: str) -> None:
    """明文订阅响应桩：通过共享下载器的 direct 通道返回固定行集。"""

    class _Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            return httpx.Response(
                200, content=text.encode("utf-8"),
                headers={"content-type": "text/plain"},
            )

    monkeypatch.setattr(
        subscription_core, "build_channels", lambda **_kwargs: [
            subscription_core.Channel("direct", None, "direct")
        ],
    )
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


@pytest.mark.asyncio
async def test_import_plain_uses_shared_subscription_fetcher(db, monkeypatch):
    sub_id = await _add_sub(db, kind="plain")
    await _stub_single_check(monkeypatch, {("8.8.8.8", 3128): ("ok", 120)})
    calls = []

    async def _fetch(url, channels, **options):
        calls.append((url, channels, options))
        return subscription_core.FetchResult(
            raw=b"8.8.8.8:3128\n", http_status=200,
            content_type="text/plain", channel="direct",
            fmt=subscription_core.FORMAT_UNKNOWN,
            resolved_nodes=[{"scheme": "http", "host": "8.8.8.8", "port": 3128}],
        )

    monkeypatch.setattr(subscription_core, "fetch_subscription", _fetch)
    monkeypatch.setattr(
        subscription_core, "build_channels", lambda **_kwargs: [
            subscription_core.Channel("direct", None, "direct")
        ],
    )

    stats = await proxies_service.import_plain_subscription(sub_id)

    assert stats["fetched"] == 1 and stats["alive"] == 1
    assert len(calls) == 1
    assert calls[0][2].get("response_parser") is not None


@pytest.mark.asyncio
async def test_plain_import_failure_keeps_last_success_snapshot(db, monkeypatch):
    from sqlalchemy import select
    from app.domains.proxypool.models import SubscriptionSnapshot

    sub_id = await _add_sub(db, kind="plain")
    async with db() as session:
        session.add(SubscriptionSnapshot(
            subscription_id=sub_id, sha256="a" * 64, format="unknown",
            raw_path="last-success.bin", url="sealed", node_count=1,
            status="OK", fetched_at=datetime(2026, 10, 1),
        ))
        await session.commit()

    async def _fail(*_args, **_kwargs):
        raise subscription_core.FetchFailedError(
            SUB_URL,
            [subscription_core.FetchAttempt(
                "direct", False, None, subscription_core.FORMAT_UNKNOWN,
                "request_timeout", "timeout", 12000, "direct",
            )],
            duration_ms=12000,
        )

    monkeypatch.setattr(subscription_core, "fetch_subscription", _fail)
    with pytest.raises(ValueError, match="订阅抓取失败"):
        await proxies_service.import_plain_subscription(sub_id)

    async with db() as session:
        snapshots = (await session.scalars(
            select(SubscriptionSnapshot)
            .where(SubscriptionSnapshot.subscription_id == sub_id)
            .order_by(SubscriptionSnapshot.id)
        )).all()
    assert [item.status for item in snapshots] == ["OK", "FAILED"]
    assert snapshots[0].sha256 == "a" * 64
    assert snapshots[0].raw_path == "last-success.bin"
    assert (await _get_sub(db, sub_id)).last_fetch_status == "FAILED"


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


def _seed_sub_cache(target_path) -> None:
    """顶替 download_subscription 时把缓存文件写出来。

    真实下载函数必然落盘（返回值里的 path 就是刚写的文件）；顶替它却不产文件，
    下游按约定读该路径就会拿到「文件不存在」，属替身失真而非被测逻辑的问题。
    """
    path = Path(target_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_MINI_CONFIG, encoding="utf-8")


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
    monkeypatch.setattr(clash_manager.ClashRuntime, "_wait_mixed_port", lambda self: True)
    monkeypatch.setattr(clash_manager, "_port_owned_by_process", lambda *_a: True)


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
async def test_hot_reload_rejects_mixed_port_owned_by_another_process(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MINI_CONFIG, encoding="utf-8")
    _patch_kernel_launch(monkeypatch)

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    cfg.write_text(_MINI_CONFIG_B, encoding="utf-8")
    _FakeControllerClient.known = {"B节点", "GLOBAL", "DIRECT", "REJECT", "COMPATIBLE", "Pass"}
    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _FakeControllerClient)
    monkeypatch.setattr(clash_manager, "_port_listening", lambda _port: True)
    owners = iter([False, True, True, True])
    monkeypatch.setattr(
        clash_manager, "_port_owned_by_process", lambda _port, _pid: next(owners)
    )

    outcome = await r.ensure_running("exe", str(cfg), subscription_url="https://b.example/sub")

    assert outcome["started"] is True and outcome["reloaded"] is False
    assert r.entry_listening() is True
    r.stop()


def test_entry_listener_must_belong_to_the_live_runtime(monkeypatch):
    runtime = clash_manager.ClashRuntime()

    class Process:
        pid = 6789

        def poll(self):
            return None

    runtime.process = Process()
    runtime.port = 41234
    monkeypatch.setattr(clash_manager, "_port_listening", lambda _port: True)
    monkeypatch.setattr(clash_manager, "_port_owned_by_process", lambda _port, _pid: False)

    assert runtime.entry_listening() is False


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
    monkeypatch.setattr(clash_manager, "_port_listening", lambda _port: True)

    outcome = await r.ensure_running("exe", str(cfg), subscription_url="https://a.example/sub")
    assert outcome["reloaded"] is False and outcome["started"] is False
    assert _FakeControllerClient.puts == [], "内容没变不打控制器"
    r.stop()


@pytest.mark.asyncio
async def test_ensure_running_reallocates_auto_port_when_listener_is_lost(monkeypatch, tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_MINI_CONFIG, encoding="utf-8")
    _patch_kernel_launch(monkeypatch)

    r = clash_manager.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    first_proc = r.process
    old_port = r.port
    new_port = old_port + 1
    monkeypatch.setattr(clash_manager, "_free_local_port", lambda: new_port)
    monkeypatch.setattr(clash_manager, "_port_listening", lambda _port: True)
    monkeypatch.setattr(
        clash_manager, "_port_owned_by_process", lambda port, _pid: port == new_port,
    )

    assert r.status()["entryReady"] is False
    result = await r.ensure_running(
        "exe", str(cfg), subscription_url="https://a.example/sub",
        allocate_mixed_port=True,
    )

    assert result["started"] is True and result["reloaded"] is False
    assert r.process is not first_proc
    assert r.port == new_port
    assert r.entry_listening() is True
    r.stop()


# ─── 下载通道顺序（代理优先，直连垫底）──────────────────────


def test_download_attempts_proxy_channels_before_direct(monkeypatch):
    """内核在跑、有已存代理、本地口可达 → 直连排最后（订阅域名直连多数
    被墙，直连打头会让每次刷新都先白等一次超时）。"""
    monkeypatch.setattr(clash_manager, "LOCAL_MIXED_PORTS", (7897,))
    monkeypatch.setattr(clash_manager, "_port_listening", lambda port: port == 7897)
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
        _seed_sub_cache(target_path)
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
    assert picked["configPath"].endswith(f"clash-sub-{live}.runtime.yaml")
    assert picked["rawPath"].endswith(f"clash-sub-{live}.yaml")
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
        _seed_sub_cache(target_path)
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
        _seed_sub_cache(target_path)
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
        _seed_sub_cache(target_path)
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
        _seed_sub_cache(target_path)
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
async def test_snapshot_only_uses_valid_subscription_cache_when_snapshot_is_absent(
    db, monkeypatch, tmp_path,
):
    sub_id = await _add_sub(db)
    async with db() as session:
        subscription = await session.get(ProxySubscription, sub_id)
        subscription.auto_refresh = False
        await session.commit()

    target = clash_manager.subscription_config_path(tmp_path, sub_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MINI_CONFIG, encoding="utf-8")

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("snapshot-only startup must not access the network")

    from app.domains.proxypool import subscription as subscription_fetch
    monkeypatch.setattr(subscription_fetch, "fetch_subscription", unexpected_fetch)

    result = await clash_manager.ClashRuntime().download_subscription(
        SUB_URL, tmp_path, [], target_path=target, snapshot_only=True,
    )

    assert result["cached"] is True
    assert result["nodes"] == 1
    assert Path(result["path"]).read_text(encoding="utf-8") == _MINI_CONFIG
    from datetime import timedelta
    from app.domains.proxypool.models import ProxyNode, SubscriptionSnapshot, node_fingerprint, subscription_tag
    identity = node_fingerprint(subscription_core.parse_nodes(_MINI_CONFIG.encode(), "yaml")[0], subscription_tag(SUB_URL))
    async with db() as session:
        assert (await session.execute(select(SubscriptionSnapshot))).scalars().all() == []
        node = await session.scalar(select(ProxyNode).where(ProxyNode.node_id == identity))
        assert node is not None and node.subscription_id == sub_id
        assert node.last_source_seen is None
        node.state = "DEAD"
        node.consecutive_failures = 3
        node.cooldown_until = datetime.now() + timedelta(hours=4)
        node.subscription_id = None
        deadline = node.cooldown_until
        before = await session.get(ProxySubscription, sub_id)
        admission = (before.admission_status, before.last_fetch_at, before.last_success_at)
        await session.commit()
    from sqlalchemy import event
    from app.core.database import get_engine
    commits = []
    def record_commit(_connection):
        commits.append(True)
    event.listen(get_engine().sync_engine, "commit", record_commit)
    try:
        await clash_manager.ClashRuntime().download_subscription(
            SUB_URL, tmp_path, [], target_path=target, snapshot_only=True,
        )
    finally:
        event.remove(get_engine().sync_engine, "commit", record_commit)
    assert commits == []
    async with db() as session:
        node = await session.scalar(select(ProxyNode).where(ProxyNode.node_id == identity))
        assert (node.state, node.consecutive_failures, node.cooldown_until) == ("DEAD", 3, deadline)
        assert node.subscription_id is None
        after = await session.get(ProxySubscription, sub_id)
        assert (after.admission_status, after.last_fetch_at, after.last_success_at) == admission
    import yaml
    from app.domains.proxypool.subscription import parse_nodes
    original_node = parse_nodes(_MINI_CONFIG.encode(), "yaml")[0]
    added = {**original_node, "name": "added", "server": "192.0.2.44"}
    target.write_text(yaml.safe_dump({"proxies": [original_node, added]}), encoding="utf-8")
    await clash_manager.ClashRuntime().download_subscription(
        SUB_URL, tmp_path, [], target_path=target, snapshot_only=True,
    )
    async with db() as session:
        existing = await session.scalar(select(ProxyNode).where(ProxyNode.node_id == identity))
        assert (existing.subscription_id, existing.state, existing.consecutive_failures, existing.cooldown_until) == (None, "DEAD", 3, deadline)
        new_identity = node_fingerprint(added, subscription_tag(SUB_URL))
        new_node = await session.scalar(select(ProxyNode).where(ProxyNode.node_id == new_identity))
        assert (new_node.subscription_id, new_node.state, new_node.last_source_seen) == (sub_id, "NEW", None)
        assert (await session.execute(select(SubscriptionSnapshot))).scalars().all() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["url", "snapshot"])
async def test_cache_registration_rechecks_source_inside_write_transaction(db, monkeypatch, tmp_path, change):
    from contextlib import asynccontextmanager
    from app.core import database
    from app.domains.proxypool.models import ProxyNode, SubscriptionSnapshot

    sub_id = await _add_sub(db)
    target = clash_manager.subscription_config_path(tmp_path, sub_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MINI_CONFIG, encoding="utf-8")
    original = database.write_session

    @asynccontextmanager
    async def changed_source(*args, **kwargs):
        async with original() as session:
            if change == "url":
                sub = await session.get(ProxySubscription, sub_id)
                sub.url = "https://example.test/changed"
            else:
                session.add(SubscriptionSnapshot(subscription_id=sub_id, url=SUB_URL, status="OK",
                                                 sha256="newer", format="yaml", node_count=1,
                                                 raw_path="not-read", fetched_at=datetime.now()))
            await session.commit()
        async with original(*args, **kwargs) as session:
            yield session

    monkeypatch.setattr(database, "write_session", changed_source)
    with pytest.raises(RuntimeError, match="来源已变化"):
        await clash_manager.ClashRuntime().download_subscription(
            SUB_URL, tmp_path, [], target_path=target, snapshot_only=True)
    async with db() as session:
        assert (await session.execute(select(ProxyNode))).scalars().all() == []


@pytest.mark.asyncio
async def test_snapshot_only_rejects_cache_tagged_for_previous_subscription_url(
    db, monkeypatch, tmp_path,
):
    from app.domains.proxypool.subscription import subscription_url_identity

    sub_id = await _add_sub(db)
    new_url = "https://example.com/sub?token=new"
    async with db() as session:
        subscription = await session.get(ProxySubscription, sub_id)
        subscription.url = new_url
        subscription.cache_url_identity = subscription_url_identity(SUB_URL)
        await session.commit()

    target = clash_manager.subscription_config_path(tmp_path, sub_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MINI_CONFIG, encoding="utf-8")

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("snapshot-only startup must not access the network")

    from app.domains.proxypool import subscription as subscription_fetch
    monkeypatch.setattr(subscription_fetch, "fetch_subscription", unexpected_fetch)

    with pytest.raises(RuntimeError, match="没有可恢复的成功快照或有效订阅缓存"):
        await clash_manager.ClashRuntime().download_subscription(
            new_url, tmp_path, [], target_path=target, snapshot_only=True,
        )


@pytest.mark.asyncio
async def test_snapshot_only_rejects_legacy_cache_when_database_identity_is_stale(
    db, monkeypatch, tmp_path,
):
    from app.domains.proxypool.subscription import subscription_url_identity

    sub_id = await _add_sub(db)
    new_url = "https://example.com/sub?token=new"
    async with db() as session:
        subscription = await session.get(ProxySubscription, sub_id)
        subscription.url = new_url
        subscription.cache_url_identity = subscription_url_identity(SUB_URL)
        await session.commit()

    target = clash_manager.subscription_config_path(tmp_path, sub_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MINI_CONFIG, encoding="utf-8")

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("snapshot-only startup must not access the network")

    from app.domains.proxypool import subscription as subscription_fetch
    monkeypatch.setattr(subscription_fetch, "fetch_subscription", unexpected_fetch)

    with pytest.raises(RuntimeError, match="没有可恢复的成功快照或有效订阅缓存"):
        await clash_manager.ClashRuntime().download_subscription(
            new_url, tmp_path, [], target_path=target, snapshot_only=True,
        )


@pytest.mark.asyncio
async def test_url_change_sidecar_write_failure_cannot_reuse_previous_cache(
    db, monkeypatch, tmp_path,
):
    from types import SimpleNamespace

    sub_id = await _add_sub(db)
    new_url = "https://example.com/sub?token=new"
    target = clash_manager.subscription_config_path(tmp_path, sub_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MINI_CONFIG, encoding="utf-8")
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: SimpleNamespace(data_dir=tmp_path)
    )
    monkeypatch.setattr(clash_manager, "write_subscription_meta", lambda *_a, **_kw: None)

    async def failing_refresh(*_args, **_kwargs):
        raise ValueError("temporary fetch failure")

    monkeypatch.setattr(proxies_service, "refresh_clash_subscription", failing_refresh)
    result = await proxies_service.update_subscription(sub_id, url=new_url)
    assert "自动重拉失败" in result["warning"]

    with pytest.raises(RuntimeError, match="没有可恢复的成功快照或有效订阅缓存"):
        await clash_manager.ClashRuntime().download_subscription(
            new_url, tmp_path, [], target_path=target, snapshot_only=True,
        )


# ─── 缓存元数据随正文落盘 / 降级不丢名称流量 ─────────────────────


def test_subscription_id_of_reads_per_subscription_path(tmp_path):
    """按订阅分文件的路径能反解 id；共享单文件路径与非法名返回 None。"""
    assert clash_manager._subscription_id_of(
        clash_manager.subscription_config_path(tmp_path, 7)
    ) == 7
    assert clash_manager._subscription_id_of(clash_manager.kernel_dir(tmp_path) / "config.yaml") is None
    assert clash_manager._subscription_id_of(None) is None
    assert clash_manager._subscription_id_of(
        clash_manager.kernel_dir(tmp_path) / "clash-sub-abc.yaml"
    ) is None


def test_subscription_meta_roundtrip_and_corruption(tmp_path):
    """sidecar 写入后原样读回；缺失与损坏一律给空 dict（不抛）。"""
    assert clash_manager.read_subscription_meta(tmp_path, 3) == {}

    clash_manager.write_subscription_meta(
        tmp_path, 3, {"title": "赔钱机场", "userinfo": USERINFO}
    )
    got = clash_manager.read_subscription_meta(tmp_path, 3)
    assert got["title"] == "赔钱机场"
    assert got["userinfo"] == USERINFO

    clash_manager.subscription_meta_path(tmp_path, 3).write_text("{坏 json", encoding="utf-8")
    assert clash_manager.read_subscription_meta(tmp_path, 3) == {}


@pytest.mark.asyncio
async def test_snapshot_only_cache_fallback_keeps_last_known_title_and_traffic(
    db, monkeypatch, tmp_path,
):
    """快照缺失降级回读缓存时沿用上次成功拉到的标题与流量。

    回归：此前降级分支写死 title/userinfo 为 None，一次网络抖动就让订阅名与
    流量在界面上消失（真实案例：订阅 3 三通道全 ConnectError → label 为空、
    traffic 为 null）。
    """
    sub_id = await _add_sub(db)
    target = clash_manager.subscription_config_path(tmp_path, sub_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_MINI_CONFIG, encoding="utf-8")
    clash_manager.write_subscription_meta(
        tmp_path, sub_id, {"title": "赔钱机场", "userinfo": USERINFO}
    )

    async def unexpected_fetch(*_args, **_kwargs):
        raise AssertionError("snapshot-only startup must not access the network")

    from app.domains.proxypool import subscription as subscription_fetch
    monkeypatch.setattr(subscription_fetch, "fetch_subscription", unexpected_fetch)

    meta = await clash_manager.ClashRuntime().download_subscription(
        SUB_URL, tmp_path, [], target_path=target, snapshot_only=True,
    )
    assert meta["cached"] is True
    assert meta["title"] == "赔钱机场"
    assert meta["userinfo"] == USERINFO


@pytest.mark.asyncio
async def test_successful_download_persists_meta_sidecar(db, monkeypatch, tmp_path):
    """成功同步后响应头（标题/流量）随快照落 sidecar，供离线降级取用。"""
    sub_id = await _add_sub(db)
    target = clash_manager.subscription_config_path(tmp_path, sub_id)

    from app.domains.proxypool import bootstrap as bs
    from app.domains.proxypool.subscription import FORMAT_YAML, FetchResult

    async def ok_fetch(_url, _channels, **_kw):
        return FetchResult(
            raw=_MINI_CONFIG.encode("utf-8"), http_status=200,
            content_type="text/yaml", channel="direct", fmt=FORMAT_YAML,
            profile_title="base64:" + base64.b64encode("我的机场".encode()).decode(),
            userinfo=USERINFO,
        )

    monkeypatch.setattr(bs, "fetch_subscription", ok_fetch)

    meta = await clash_manager.ClashRuntime().download_subscription(
        SUB_URL, tmp_path, [], target_path=target,
    )
    assert meta["cached"] is False
    assert meta["title"] == "我的机场"

    saved = clash_manager.read_subscription_meta(tmp_path, sub_id)
    assert saved["title"] == "我的机场"
    assert saved["userinfo"] == USERINFO


# ─── 删除订阅回收缓存产物（孤儿文件回归） ─────────────────────


@pytest.mark.asyncio
async def test_delete_subscription_removes_cache_files(db):
    """删订阅必须连缓存正文与 sidecar 一起回收，磁盘不留孤儿。

    只删库行会在 clash 目录留下无人引用的孤儿文件，「已删除」在文件系统层面
    不成立。
    """
    from app.core.config import get_settings

    data_dir = get_settings().data_dir
    sub_id = await _add_sub(db)
    target = clash_manager.subscription_config_path(data_dir, sub_id)
    target.write_text(_MINI_CONFIG, encoding="utf-8")
    clash_manager.write_subscription_meta(data_dir, sub_id, {"title": "X"})
    assert target.is_file()
    assert clash_manager.subscription_meta_path(data_dir, sub_id).is_file()

    assert await proxies_service.delete_subscription(sub_id) is True

    assert not target.exists()
    assert not clash_manager.subscription_meta_path(data_dir, sub_id).exists()


def test_remove_subscription_artifacts_is_idempotent(tmp_path):
    """回收是幂等的：目标不存在时不抛，返回空清单。"""
    assert clash_manager.remove_subscription_artifacts(tmp_path, 404) == []


# ─── 流量单调性：失败拉取不得清空已记录值 ─────────────────────


@pytest.mark.asyncio
async def test_merge_last_stats_never_clears_traffic(db):
    """traffic 为空视为「本次没取到」，保留既有值——防止界面流量倒退。"""
    sid = await _add_sub(db, last_stats={"traffic": USERINFO, "nodes": 5})

    await proxies_service._merge_last_stats(sid, traffic=None, nodes=6)

    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.last_stats["traffic"] == USERINFO
    assert row.last_stats["nodes"] == 6


@pytest.mark.asyncio
async def test_merge_last_stats_overwrites_traffic_with_fresh_value(db):
    """拿到新流量时按最新值覆盖（实时量，单调增长）。"""
    sid = await _add_sub(db, last_stats={"traffic": "download=1"})

    await proxies_service._merge_last_stats(sid, traffic="download=999")

    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.last_stats["traffic"] == "download=999"


# ─── 借道口可用性判定 ──────────────────────────────────────────


def test_local_mixed_ports_do_not_scan_holdexar_runtime_port():
    assert clash_manager.LOCAL_MIXED_PORTS == (7897,)


def test_local_channel_egress_ok_direct_always_true():
    """直连（proxy_url=None）不经过借道口，无需探活。"""
    assert clash_manager.local_channel_egress_ok(None) is True


def test_local_channel_egress_ok_false_on_failure(monkeypatch):
    """借道口探活失败即判定不可用。"""

    def _boom(*a, **kw):
        raise httpx.ConnectError("no egress")

    monkeypatch.setattr(clash_manager.httpx, "get", _boom)
    assert clash_manager.local_channel_egress_ok("http://127.0.0.1:7890") is False


# ─── 信息占位节点兜底取用量 ─────────────────────────────────────


_INFO_NODE_CONFIG = """\
mixed-port: 7890
proxies:
- name: 🇭🇰|香港-中转 01
  type: ss
  server: 10.0.0.1
  port: 1
proxy-groups:
- name: 手动选择
  type: select
  proxies:
  - 剩余流量：19.48 GB
  - 套餐到期：长期有效
  - 🇭🇰|香港-中转 01
"""




def test_auto_name_skips_opaque_url_token():
    """URL 末段是长 token 时不给名字——对用户没有辨识度，不如回落显示 URL。

    回归：订阅 3 的链接失效后，自动取名把 32 位 token 写成了「机场名」。
    """
    headers = httpx.Headers({})
    assert clash_manager.subscription_auto_name(
        headers, "https://cdn.example.com/kcyun/8879fd2fc650be890954f0de66b77b6f?nodeType=1"
    ) is None


def test_auto_name_keeps_readable_url_tail():
    """可读末段（含中文/短名）照常当名字用。"""
    headers = httpx.Headers({})
    assert clash_manager.subscription_auto_name(
        headers, "https://example.com/sub/我的机场"
    ) == "我的机场"
    assert clash_manager.subscription_auto_name(
        headers, "https://example.com/sub/airport.yaml"
    ) == "airport.yaml"


def test_auto_name_prefers_content_disposition_over_url():
    """有 Content-Disposition 时优先用它，不走 URL 末段。"""
    headers = httpx.Headers(
        {"content-disposition": "attachment; filename*=UTF-8''%E8%B5%94%E9%92%B1%E6%9C%BA%E5%9C%BA"}
    )
    assert clash_manager.subscription_auto_name(
        headers, "https://cdn.example.com/kcyun/8879fd2fc650be890954f0de66b77b6f"
    ) == "赔钱机场"


# ─── 名称三档链（用户改名 > 机场品牌名 > 缓存上次值）─────────────


@pytest.mark.asyncio
async def test_name_chain_uses_panel_name_when_user_never_renamed(db):
    """① 用户没改过：机场品牌名写进来，并标记来源为 panel。"""
    sid = await _add_sub(db)

    got = await proxies_service.apply_subscription_name_chain(sid, "赔钱机场")

    assert got == "赔钱机场"
    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.label == "赔钱机场"
    assert row.last_stats["labelSource"] == proxies_service.LABEL_SOURCE_PANEL


@pytest.mark.asyncio
async def test_name_chain_follows_panel_rename_when_user_never_renamed(db):
    """② 用户没改过 → 机场自己改名，界面要跟着更新（不是冻死在旧名上）。"""
    sid = await _add_sub(db)
    await proxies_service.apply_subscription_name_chain(sid, "赔钱机场")

    got = await proxies_service.apply_subscription_name_chain(sid, "跑路机场")

    assert got == "跑路机场"


@pytest.mark.asyncio
async def test_name_chain_user_rename_wins_over_panel(db):
    """③ 用户真改过 → 机场改名不许覆盖（第一顺位锁死）。"""
    sid = await _add_sub(db)
    await proxies_service.apply_subscription_name_chain(sid, "赔钱机场")
    await proxies_service.update_subscription(sid, label="我的主力机场")

    got = await proxies_service.apply_subscription_name_chain(sid, "跑路机场")

    assert got == "我的主力机场"
    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.label == "我的主力机场"
    assert row.last_stats["labelSource"] == proxies_service.LABEL_SOURCE_USER


@pytest.mark.asyncio
async def test_saving_unchanged_name_is_not_a_user_rename(db):
    """④ 「打开窗口没碰名字就保存」不算改——不能锁死机场名。

    回归关键：判据是「值变了没有」，不是「保存按了没有」。此前保存会把
    输入框原值重写一遍，等于任何一次路过保存都把机场名永久冻死。
    """
    sid = await _add_sub(db)
    await proxies_service.apply_subscription_name_chain(sid, "赔钱机场")

    # 原样保存（值未变）
    await proxies_service.update_subscription(sid, label="赔钱机场")

    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.last_stats.get("labelSource") == proxies_service.LABEL_SOURCE_PANEL

    # 因此机场改名仍能更新过来
    assert await proxies_service.apply_subscription_name_chain(sid, "跑路机场") == "跑路机场"


@pytest.mark.asyncio
async def test_name_chain_falls_back_to_cached_value(db):
    """⑤ 本次没有机场名（降级且 sidecar 为空）→ 维持库中已有值，不清空。"""
    sid = await _add_sub(db)
    await proxies_service.apply_subscription_name_chain(sid, "赔钱机场")

    got = await proxies_service.apply_subscription_name_chain(sid, None)

    assert got == "赔钱机场"


@pytest.mark.asyncio
async def test_name_chain_returns_none_when_no_source(db):
    """⑥ 三档全无（新订阅、无名字、本次也没拉到）→ None，由前端回落显示 URL。"""
    sid = await _add_sub(db)

    assert await proxies_service.apply_subscription_name_chain(sid, None) is None


@pytest.mark.asyncio
async def test_legacy_row_without_source_follows_panel_rename(db):
    """⑦ 历史行（老版本自动取的名、无来源标记）名等于机场名 → 视为机场名可更新。

    老版本自动取的名没有来源标记；一律当用户命名锁死的话，机场改名
    永远跟不过来——名字等于机场名的历史行按机场名可更新处理。
    """
    sid = await _add_sub(db)
    async with db() as session:
        row = await session.get(ProxySubscription, sid)
        row.label = "赔钱机场"  # 老代码自动取的，没留 labelSource
        await session.commit()

    got = await proxies_service.apply_subscription_name_chain(sid, "赔钱机场")

    # 名字与机场一致 → 补上 panel 标记，此后可随机场改名更新
    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.last_stats["labelSource"] == proxies_service.LABEL_SOURCE_PANEL
    assert got == "赔钱机场"


@pytest.mark.asyncio
async def test_legacy_row_with_divergent_name_is_treated_as_user(db):
    """⑧ 历史行名字与机场给的不一致 → 视为用户动过，锁死不被覆盖。"""
    sid = await _add_sub(db)
    async with db() as session:
        row = await session.get(ProxySubscription, sid)
        row.label = "我自己起的名字"
        await session.commit()

    got = await proxies_service.apply_subscription_name_chain(sid, "机场给的名字")

    assert got == "我自己起的名字"
    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.label == "我自己起的名字"


# ─── 流量链独立于名称链 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_traffic_takes_fresh_value_over_ledger(db):
    """本次拿到新流量 → 用新的（实时量，always 最新）。"""
    sid = await _add_sub(db, last_stats={"traffic": "download=1"})

    assert await proxies_service._resolve_traffic(sid, "download=999") == "download=999"


@pytest.mark.asyncio
async def test_traffic_falls_back_to_ledger_when_fetch_fails(db):
    """本次没拉到 → 沿用账本里已有的流量，绝不清空。"""
    sid = await _add_sub(db, last_stats={"traffic": "download=888"})

    assert await proxies_service._resolve_traffic(sid, None) == "download=888"
    assert await proxies_service._resolve_traffic(sid, "") == "download=888"


@pytest.mark.asyncio
async def test_user_rename_does_not_affect_traffic(db):
    """名称与流量两条链互不干涉：用户改了名字，流量照样认机场给的最新值。"""
    sid = await _add_sub(db, last_stats={"traffic": "download=1"})
    await proxies_service.update_subscription(sid, label="我的主力机场")

    assert await proxies_service._resolve_traffic(sid, "download=555") == "download=555"

    async with db() as session:
        row = await session.get(ProxySubscription, sid)
    assert row.label == "我的主力机场"
    assert row.last_stats["labelSource"] == proxies_service.LABEL_SOURCE_USER


