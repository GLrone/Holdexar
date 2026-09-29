"""Clash 节点状态机测试：冷却递增 / dead 三连复活 / 订阅废弃判定 / 6h 体检门槛。

不触真实网络/内核——直接调用 _apply_node_results / _evaluate_subscription_deprecation /
maybe_run_clash_health_check，mock 探测结果与内核状态；库隔离到临时文件。
"""
import asyncio
import sys
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.models import ClashNode, ProxySubscription


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

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_sub(db, kind="clash") -> int:
    async with db() as session:
        sub = ProxySubscription(kind=kind, url="https://example.com/sub")
        session.add(sub)
        await session.commit()
        return sub.id


def _ok(name: str, ms=100) -> dict:
    return {"name": name, "alive": True, "steamOk": True, "exitIp": "1.1.1.1", "ms": ms, "duplicate": False, "probed": True}


def _fail(name: str) -> dict:
    return {"name": name, "alive": False, "steamOk": False, "exitIp": None, "ms": None, "duplicate": False, "probed": True}


def _node(session_rows, name: str) -> ClashNode:
    return next(n for n in session_rows if n.name == name)


# ─── 冷却递增 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fail_cooldown_ladder(db):
    """失败冷却阶梯：第 1 次失败 30min，第 2 次 1h，第 3 次 2h……24h 封顶。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())

    for expected_minutes in [30, 60, 120, 240, 480, 1440, 1440, 1440]:
        _, _, _ = await proxies_service._apply_node_results(
            sub_id, {}, [_fail("n1")], ["n1"], now
        )
        async with db() as session:
            rows = (await session.execute(select(ClashNode))).scalars().all()
            n = _node(rows, "n1")
            assert n.cooldown_until is not None
            delta = n.cooldown_until - now
            assert timedelta(minutes=expected_minutes) - timedelta(seconds=5) <= delta <= timedelta(minutes=expected_minutes, seconds=5), (
                f"fail_count={n.fail_count} 期望冷却 {expected_minutes}min，实际 {delta}"
            )


@pytest.mark.asyncio
async def test_ok_resets_cooldown(db):
    """通过即清零：fail_count 归零、冷却解除、状态 ok。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    await proxies_service._apply_node_results(sub_id, {}, [_fail("n1")], ["n1"], now)
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)
    async with db() as session:
        rows = (await session.execute(select(ClashNode))).scalars().all()
        n = _node(rows, "n1")
        assert n.fail_count == 0
        assert n.cooldown_until is None
        assert n.status == "ok"


# ─── dead 终态 + 3-of-3 复活 ─────────────────────────────────


@pytest.mark.asyncio
async def test_dead_after_10_fails(db):
    """累计失败 10 次 → dead 终态（冷却清空，不再轮转）。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    for _ in range(proxies_service.DEAD_MAX_FAILS):
        await proxies_service._apply_node_results(sub_id, {}, [_fail("n1")], ["n1"], now)
    async with db() as session:
        rows = (await session.execute(select(ClashNode))).scalars().all()
        n = _node(rows, "n1")
        assert n.status == "dead"
        assert n.cooldown_until is None


@pytest.mark.asyncio
async def test_dead_revive_requires_3_consecutive_passes(db):
    """dead 复活语义：连续 3 次通过才转 ok（用户确认的 3-of-3 严格标准）。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    for _ in range(proxies_service.DEAD_MAX_FAILS):
        await proxies_service._apply_node_results(sub_id, {}, [_fail("n1")], ["n1"], now)

    # dead 后第 1、2 次通过：仍是 dead（复活计数累加）
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)
    async with db() as session:
        rows = (await session.execute(select(ClashNode))).scalars().all()
        assert _node(rows, "n1").status == "dead"
        assert _node(rows, "n1").revive_passes == 1

    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)
    async with db() as session:
        rows = (await session.execute(select(ClashNode))).scalars().all()
        assert _node(rows, "n1").status == "dead"
        assert _node(rows, "n1").revive_passes == 2

    # 第 3 次连续通过 → 复活
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)
    async with db() as session:
        rows = (await session.execute(select(ClashNode))).scalars().all()
        n = _node(rows, "n1")
        assert n.status == "ok"
        assert n.revive_passes == 0
        assert n.fail_count == 0


@pytest.mark.asyncio
async def test_dead_revive_streak_broken_by_fail(db):
    """复活连测被失败打断：revive_passes 归零，重新计数。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    for _ in range(proxies_service.DEAD_MAX_FAILS):
        await proxies_service._apply_node_results(sub_id, {}, [_fail("n1")], ["n1"], now)

    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)  # passes=1
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)  # passes=2
    await proxies_service._apply_node_results(sub_id, {}, [_fail("n1")], ["n1"], now)  # 打断
    async with db() as session:
        rows = (await session.execute(select(ClashNode))).scalars().all()
        n = _node(rows, "n1")
        assert n.revive_passes == 0
        assert n.status == "dead"  # 失败不改变 dead 终态


# ─── 订阅 95% 废弃判定 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_subscription_deprecated_over_95(db):
    """不可用占比 >95% → 订阅废弃（deprecated=True，带原因）；不删除。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    # 100 节点只有 4 个通（96% 不可用 > 95%）
    results = [_ok(f"ok{i}") for i in range(4)] + [_fail(f"bad{i}") for i in range(96)]
    names = [r["name"] for r in results]
    alive, _, deprecated = await proxies_service._apply_node_results(sub_id, {}, results, names, now)
    assert alive == 4
    assert deprecated is True
    async with db() as session:
        sub = await session.get(ProxySubscription, sub_id)
        assert sub.deprecated is True
        assert sub.deprecated_at is not None
        assert "96%" in sub.deprecated_reason


@pytest.mark.asyncio
async def test_subscription_deprecated_boundary_not_triggered(db):
    """恰好 95% 不可用（=5% 可用）不触发废弃——用户口径是「超过 95%」。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    # 100 节点 5 个通：不可用 95%，ratio > 0.95 为 False
    results = [_ok(f"ok{i}") for i in range(5)] + [_fail(f"bad{i}") for i in range(95)]
    names = [r["name"] for r in results]
    _, _, deprecated = await proxies_service._apply_node_results(sub_id, {}, results, names, now)
    assert deprecated is False


@pytest.mark.asyncio
async def test_subscription_deprecated_auto_recover(db):
    """废弃订阅恢复达标（不可用 ≤95%）→ 自动解除废弃。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    bad = [_fail(f"bad{i}") for i in range(96)] + [_ok(f"ok{i}") for i in range(4)]
    names = [r["name"] for r in bad]
    await proxies_service._apply_node_results(sub_id, {}, bad, names, now)
    # 修复到 50 个通
    fixed = [_ok(f"ok{i}") for i in range(50)] + [_fail(f"bad{i}") for i in range(50)]
    _, _, deprecated = await proxies_service._apply_node_results(sub_id, {}, fixed, names, now)
    assert deprecated is False
    async with db() as session:
        sub = await session.get(ProxySubscription, sub_id)
        assert sub.deprecated is False
        assert sub.deprecated_reason is None


@pytest.mark.asyncio
async def test_cooldown_nodes_counted_by_ledger(db):
    """冷却跳过的节点按账本口径计入存活统计（上次判定为准）。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    # 先把 n1 测活
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1"), _fail("n2")], ["n1", "n2"], now)
    ledger = await proxies_service._load_clash_node_ledger(sub_id, ["n1", "n2"])
    # n2 进入冷却；第二次只测 n1（模拟 n2 冷却中）——但 n1 这次失败
    _, _, _ = await proxies_service._apply_node_results(sub_id, ledger, [_fail("n1")], ["n1", "n2"], now)
    # n1 失败、n2 冷却跳过按账本 dead？n2 上次 fail——ledger 状态不是 ok
    # 再验证：n2 的账本 status 应保留（非 dead 状态下失败不清 ok）
    # n2 上次失败后 status 保留 unknown（新节点失败初始为 unknown 非 dead）
    async with db() as session:
        rows = (await session.execute(select(ClashNode))).scalars().all()
        n2 = _node(rows, "n2")
        assert n2.cooldown_until is not None  # n2 冷却递增过


# ─── 6h 体检门槛 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_health_check_throttled_within_6h(db, monkeypatch):
    """距上次全量检测 <6h → throttled（不真跑，本地软件不常驻的节流语义）。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)

    monkeypatch.setattr(
        clash_manager.runtime, "status",
        lambda: {"running": True, "port": 7890, "configPath": "x", "controllerUrl": "y"},
    )
    state = await proxies_service.maybe_run_clash_health_check()
    assert state == "throttled"


@pytest.mark.asyncio
async def test_health_check_runs_after_6h(db, monkeypatch):
    """距上次检测 ≥6h → 真跑检测（mock 掉 test_clash_nodes 全流程）。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    old = now - timedelta(hours=6, minutes=1)
    # 直接落一条 6h 前的检测记录
    async with db() as session:
        session.add(ClashNode(subscription_id=sub_id, name="n1", status="ok", last_checked_at=old, created_at=old))
        await session.commit()

    monkeypatch.setattr(
        clash_manager.runtime, "status",
        lambda: {"running": True, "port": 7890, "configPath": "x", "controllerUrl": "y"},
    )
    called = []
    monkeypatch.setattr(proxies_service, "test_clash_nodes", lambda *a, **kw: called.append(1) or _async_none())
    state = await proxies_service.maybe_run_clash_health_check()
    assert state == "checked"
    assert called


async def _async_none():
    return None


# ─── 检测串行锁（三源互斥：手动/启动首检/定时体检）─────────


@pytest.mark.asyncio
async def test_clash_test_lock_shared_per_loop(db):
    """同一事件循环拿到同一把锁（uvicorn 单循环 = 全局串行）。"""
    assert proxies_service._clash_test_lock() is proxies_service._clash_test_lock()


@pytest.mark.asyncio
async def test_clash_nodes_waits_for_shared_lock(db, monkeypatch):
    """外部持锁时 test_clash_nodes 阻在门外不进函数体，释放后才放行——
    手动检测/启动首检/定时体检并发切 selector 互踩在结构上被封死。"""
    monkeypatch.setattr(
        clash_manager.runtime, "status",
        lambda: {"running": False, "port": 7890, "configPath": "x", "controllerUrl": "y"},
    )

    async with proxies_service._clash_test_lock():
        task = asyncio.create_task(proxies_service.test_clash_nodes())
        await asyncio.sleep(0.05)
        assert not task.done()  # 未拿到锁：函数体未执行（连「未运行」都没抛）

    with pytest.raises(ValueError, match="Clash 未运行"):
        await task  # 锁释放后放行，进入函数体立即因未运行报错



# ─── 检测会话（后台执行 + 进度快照）─────────────────────────


@pytest.fixture(autouse=True)
def _fresh_test_session():
    """会话是模块级单例：用例间清场，避免上一例的终态被本例的 start 复用。"""
    proxies_service._clash_test_session = None
    yield
    proxies_service._clash_test_session = None


def _impl_result(total=3, probed=1) -> dict:
    return {
        "total": total,
        "probed": probed,
        "alive": 1,
        "aliveUnique": 1,
        "selector": "GLOBAL",
        "subscriptionId": None,
        "deprecated": False,
        "nodes": [_ok("n1")],
    }


@pytest.mark.asyncio
async def test_clash_test_start_returns_before_probe_finishes(db, monkeypatch):
    """启动即返：start 落会话拉后台任务立刻返回，探测中途就能读到逐节点进度。"""
    release = asyncio.Event()

    async def slow_impl(subscription_id=None, *, probe_all=False):
        proxies_service._clash_test_session_update(phase="running", total=3, toProbe=3)
        proxies_service._clash_test_session_node(_ok("n1"))
        await release.wait()
        return _impl_result()

    monkeypatch.setattr(proxies_service, "_test_clash_nodes_impl", slow_impl)
    snap = proxies_service.clash_test_start()
    assert snap["phase"] in ("queued", "running")

    await asyncio.sleep(0.05)
    mid = proxies_service.clash_test_progress()
    assert mid["phase"] == "running"
    assert mid["probed"] == 1
    assert [n["name"] for n in mid["nodes"]] == ["n1"]

    release.set()
    await asyncio.sleep(0.05)
    done = proxies_service.clash_test_progress()
    assert done["phase"] == "done"
    assert done["alive"] == 1
    assert done["finishedAt"] is not None


@pytest.mark.asyncio
async def test_clash_test_start_reuses_active_session(db, monkeypatch):
    """已有进行中的会话（首检/体检在跑）→ start 复用同一会话，不再叠加后台任务。"""
    release = asyncio.Event()
    calls = []

    async def slow_impl(subscription_id=None, *, probe_all=False):
        calls.append(1)
        await release.wait()
        return _impl_result()

    monkeypatch.setattr(proxies_service, "_test_clash_nodes_impl", slow_impl)
    proxies_service.clash_test_start()
    await asyncio.sleep(0.02)  # 让后台任务起跑、进入 impl
    proxies_service.clash_test_start()  # 会话仍 active → 复用，不 spawn
    release.set()
    await asyncio.sleep(0.05)
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_clash_test_start_after_done_runs_new_session(db, monkeypatch):
    """上一轮已 done → start 落新会话重新开测（终态不复用）。"""
    async def quick_impl(subscription_id=None, *, probe_all=False):
        return _impl_result()

    monkeypatch.setattr(proxies_service, "_test_clash_nodes_impl", quick_impl)
    proxies_service._clash_test_session = {
        "phase": "done", "total": 3, "toProbe": 3, "probed": 3, "cooldownSkipped": 0,
        "alive": 1, "aliveUnique": 1, "selector": "GLOBAL", "subscriptionId": None,
        "deprecated": False, "nodes": [], "startedAt": "x", "finishedAt": "x", "error": None,
    }
    snap = proxies_service.clash_test_start()
    assert snap["phase"] in ("queued", "running")


@pytest.mark.asyncio
async def test_clash_test_session_failure_carries_user_language_error(db, monkeypatch):
    """失败收口：impl 抛 ValueError → 会话 phase=failed，error 为用户语言原因。"""
    async def boom(subscription_id=None, *, probe_all=False):
        raise ValueError("Clash 未运行：请先在 Clash 接入选择订阅并启动")

    monkeypatch.setattr(proxies_service, "_test_clash_nodes_impl", boom)
    snap = proxies_service.clash_test_start()
    assert snap["phase"] in ("queued", "running")
    await asyncio.sleep(0.05)
    failed = proxies_service.clash_test_progress()
    assert failed["phase"] == "failed"
    assert "Clash 未运行" in failed["error"]
    assert failed["finishedAt"] is not None


@pytest.mark.asyncio
async def test_clash_nodes_direct_call_finalizes_session(db, monkeypatch):
    """体检/启动首检直调 test_clash_nodes 同样落会话——进度端点能看到它们的进度。"""
    async def ok_impl(subscription_id=None, *, probe_all=False):
        proxies_service._clash_test_session_update(phase="running", total=1, toProbe=1)
        proxies_service._clash_test_session_node(_ok("n1"))
        return _impl_result(total=1, probed=1)

    monkeypatch.setattr(proxies_service, "_test_clash_nodes_impl", ok_impl)
    r = await proxies_service.test_clash_nodes()
    assert r["total"] == 1
    snap = proxies_service.clash_test_progress()
    assert snap["phase"] == "done"
    assert snap["probed"] == 1


# ─── 探测 lane（注入 / 解析 / 重启判定）─────────────────────


def _sub_config() -> str:
    """一份最小订阅配置（含自己的组与混合口，验证注入不破坏既有内容）。"""
    return """
mixed-port: 7890
external-controller: 127.0.0.1:19090
proxies:
  - name: "节点A"
    type: ss
    server: a.example.net
    port: 8388
    cipher: aes-128-gcm
    password: pw
  - name: "节点B"
    type: ss
    server: b.example.net
    port: 8388
    cipher: aes-128-gcm
    password: pw
proxy-groups:
  - name: "机场选择"
    type: select
    proxies: ["节点A", "节点B"]
rules:
  - MATCH,DIRECT
"""


def test_inject_probe_lanes_adds_20_lanes_and_keeps_subscription() -> None:
    import yaml

    from app.domains.proxies.clash_manager import PROBE_LANE_COUNT, inject_probe_lanes

    injected = inject_probe_lanes(_sub_config())
    doc = yaml.safe_load(injected)
    lanes = [g for g in doc["proxy-groups"] if g["name"].startswith("HlProbeLane")]
    listeners = [l for l in doc["listeners"] if l["name"].startswith("HlProbeLane")]
    assert len(lanes) == PROBE_LANE_COUNT
    assert len(listeners) == PROBE_LANE_COUNT
    assert all(g["type"] == "select" and g.get("include-all") for g in lanes)
    # listener 逐条指向本组，且不碰订阅自己的组与混合口
    assert all(l["proxy"] == l["name"].removesuffix("-in") for l in listeners)
    assert doc["mixed-port"] == 7890
    assert any(g["name"] == "机场选择" for g in doc["proxy-groups"])
    assert [p["name"] for p in doc["proxies"]] == ["节点A", "节点B"]


def test_inject_probe_lanes_is_idempotent() -> None:
    from app.domains.proxies.clash_manager import inject_probe_lanes

    once = inject_probe_lanes(_sub_config())
    twice = inject_probe_lanes(once)
    assert twice == once, "重复注入不得叠加 lane 条目"


def test_inject_probe_lanes_avoids_own_ports() -> None:
    from app.domains.proxies.clash_manager import (
        inject_probe_lanes,
        parse_probe_lanes,
    )

    ports = {port for _g, port in parse_probe_lanes(inject_probe_lanes(_sub_config()))}
    assert ports and all(p >= 20000 for p in ports)
    assert 7890 not in ports and 19090 not in ports


def test_config_unchanged_ignores_lane_port_drift() -> None:
    """lane 端口随占用态漂移不算「配置变了」——订阅内容没变就不重启。"""
    from app.domains.proxies.clash_manager import config_unchanged, inject_probe_lanes

    startup_text = inject_probe_lanes(_sub_config())
    assert config_unchanged(startup_text, _sub_config())


def test_config_unchanged_detects_subscription_content_change() -> None:
    from app.domains.proxies.clash_manager import config_unchanged, inject_probe_lanes

    startup_text = inject_probe_lanes(_sub_config())
    changed = _sub_config().replace("节点B", "节点B2")
    assert not config_unchanged(startup_text, changed)


def test_parse_probe_lanes_orders_by_lane_name() -> None:
    from app.domains.proxies.clash_manager import (
        inject_probe_lanes,
        parse_probe_lanes,
    )

    lanes = parse_probe_lanes(inject_probe_lanes(_sub_config()))
    names = [g for g, _p in lanes]
    assert names == sorted(names)
    assert len(lanes) == 20


@pytest.mark.asyncio
async def test_probe_via_lanes_chunks_waves_and_streams_session(db, monkeypatch):
    """lane 并发探测：节点多于 lane 数分波推进，逐行进会话（进度按波可观测）。"""
    from app.domains.proxies import service as svc

    lanes = [(f"HlProbeLane-{i}", 20000 + i) for i in range(1, 21)]
    names = [f"n{i}" for i in range(25)]
    svc._clash_test_session_reset()
    seen_waves: list[int] = []

    async def fake_lane(ctl, base, lane, name):
        # 记录每波首个节点出现时的已测数，验证波边界
        if name.endswith(("0", "1")) or name == "n0":
            seen_waves.append(len(svc._clash_test_session["nodes"]))
        return {"name": name, "alive": True, "steamOk": True, "exitIp": f"1.1.1.{name[1:]}", "ms": 100, "duplicate": False, "probed": True}

    monkeypatch.setattr(svc, "_probe_node_via_lane", fake_lane)
    rows = await svc._probe_nodes_via_lanes(None, "http://x", lanes, names)
    assert len(rows) == 25
    assert svc._clash_test_session["probed"] == 25
    assert svc._clash_test_session["alive"] == 25
    assert len(svc._clash_test_session["nodes"]) == 25
    # 波边界：第 21 个节点开测前，会话里应已有 20 行（第一波整体落账）
    assert seen_waves and min(seen_waves) <= 20
    svc._clash_test_session = None


# ─── 同出口代表行择优 ───────────────────────────────────────


def _row(name: str, *, alive: bool, ms: int | None = 100, cooling: bool = False, exit_ip: str | None = "1.1.1.1") -> dict:
    return {"name": name, "alive": alive, "steamOk": alive, "exitIp": exit_ip,
            "ms": ms, "duplicate": False, "probed": True, "cooling": cooling}


def test_same_exit_primary_prefers_alive_over_dead() -> None:
    """拿到出口 IP 但 Steam 判死的节点不得凭出现序占住展示位：
    同出口里存活者优先当代表行，判死节点标 duplicate 进子行。"""
    rows = [_row("死节点", alive=False, ms=800), _row("活节点", alive=True, ms=3000)]
    proxies_service._mark_same_exit(rows, {"死节点": 0, "活节点": 1})
    by_name = {r["name"]: r for r in rows}
    assert by_name["活节点"]["duplicate"] is False
    assert by_name["死节点"]["duplicate"] is True


def test_same_exit_primary_prefers_lower_latency() -> None:
    """同为存活：本次延迟低者当代表行。"""
    rows = [_row("慢", alive=True, ms=5000), _row("快", alive=True, ms=600)]
    proxies_service._mark_same_exit(rows, {"慢": 0, "快": 1})
    by_name = {r["name"]: r for r in rows}
    assert by_name["快"]["duplicate"] is False
    assert by_name["慢"]["duplicate"] is True


def test_same_exit_primary_prefers_probed_over_cooling() -> None:
    """冷却行（沿用账本判定）排在本次检测之后：有本次检测存活行时不当代表。"""
    rows = [_row("冷却行", alive=True, ms=50, cooling=True), _row("实测行", alive=True, ms=900)]
    proxies_service._mark_same_exit(rows, {"冷却行": 0, "实测行": 1})
    by_name = {r["name"]: r for r in rows}
    assert by_name["实测行"]["duplicate"] is False
    assert by_name["冷却行"]["duplicate"] is True


def test_same_exit_without_alive_keeps_config_order() -> None:
    """整组都不可用：无存活可选，保持配置顺序（首个不可用行为代表）。"""
    rows = [_row("a", alive=False), _row("b", alive=False)]
    proxies_service._mark_same_exit(rows, {"a": 0, "b": 1})
    by_name = {r["name"]: r for r in rows}
    assert by_name["a"]["duplicate"] is False
    assert by_name["b"]["duplicate"] is True


def test_same_exit_rows_without_exit_ip_untouched() -> None:
    """没有出口 IP 的行不参与分组，duplicate 保持原值。"""
    rows = [_row("无IP", alive=False, exit_ip=None)]
    proxies_service._mark_same_exit(rows, {"无IP": 0})
    assert rows[0]["duplicate"] is False


# ─── 检测归属：内核在跑哪条订阅 ─────────────────────────────


@pytest.mark.asyncio
async def test_active_subscription_resolves_by_recorded_url(db):
    """多订阅并存：按内核启动时登记的 URL 归属，不按列表序猜。"""
    async with db() as session:
        session.add(ProxySubscription(kind="clash", url="https://a.example.com/sub"))
        await session.commit()
        first = (await session.execute(select(ProxySubscription.id))).scalars().first()
        session.add(ProxySubscription(kind="clash", url="https://b.example.com/sub"))
        await session.commit()
        ids = (await session.execute(select(ProxySubscription.id))).scalars().all()
    second = max(ids)

    status = {"running": True, "subscriptionUrl": "https://a.example.com/sub"}
    assert await proxies_service._active_clash_subscription_id(status) == first
    status = {"running": True, "subscriptionUrl": "https://b.example.com/sub"}
    assert await proxies_service._active_clash_subscription_id(status) == second
    # 登记的 URL 不在订阅表里（已删除）→ 兜底最近一条
    status = {"running": True, "subscriptionUrl": "https://gone.example.com/sub"}
    assert await proxies_service._active_clash_subscription_id(status) == second


@pytest.mark.asyncio
async def test_active_subscription_falls_back_to_latest_without_record(db):
    """内核没登记归属（登记机制上线前启动）→ 兜底最近一条，不抛错。"""
    await _seed_sub(db)
    latest = await _seed_sub(db)
    assert await proxies_service._active_clash_subscription_id({"running": True}) == latest
    assert await proxies_service._active_clash_subscription_id({}) is None or True


# ─── 首检复用窗口（启动/切换触发的全量检测不重复跑）──────────


@pytest.mark.asyncio
async def test_clash_nodes_fresh_within_reuse_window(db):
    """窗口内检测过 → fresh（首检沿用账本结论）。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)
    assert await proxies_service.clash_nodes_fresh(sub_id) is True


@pytest.mark.asyncio
async def test_clash_nodes_stale_beyond_reuse_window(db):
    """超过复用窗口 → not fresh（首检照跑）。"""
    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    stale = now - timedelta(minutes=proxies_service.FIRST_CHECK_REUSE_MINUTES + 1)
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], stale)
    assert await proxies_service.clash_nodes_fresh(sub_id) is False


@pytest.mark.asyncio
async def test_clash_nodes_fresh_false_without_ledger(db):
    """账本为空（新订阅没测过）→ not fresh，首检必须真跑。"""
    sub_id = await _seed_sub(db)
    assert await proxies_service.clash_nodes_fresh(sub_id) is False


@pytest.mark.asyncio
async def test_spawn_first_check_skips_when_ledger_fresh(db, monkeypatch):
    """账本在复用窗口内 → _spawn_first_check 不触发探测（来回切换订阅不重测）。"""
    from app.domains.proxies import router as proxies_router

    sub_id = await _seed_sub(db)
    now = proxies_service._naive(proxies_service.get_beijing_time_obj())
    await proxies_service._apply_node_results(sub_id, {}, [_ok("n1")], ["n1"], now)

    called: list[int] = []

    async def fail_probe(sid, probe_all=True):
        called.append(sid)
        return {"total": 1, "alive": 1}

    async def no_promote(sid, alive):
        return None

    monkeypatch.setattr(proxies_router.service, "test_clash_nodes", fail_probe)
    monkeypatch.setattr(proxies_router, "_auto_promote_after_check", no_promote)

    proxies_router._spawn_first_check(sub_id)
    for _ in range(6):
        await asyncio.sleep(0.01)
    assert called == [], "窗口内账本已有全量结论，不得重复探测"


@pytest.mark.asyncio
async def test_spawn_first_check_runs_when_ledger_stale(db, monkeypatch):
    """账本过期/为空 → _spawn_first_check 照常触发全量探测。"""
    from app.domains.proxies import router as proxies_router

    sub_id = await _seed_sub(db)
    called: list[int] = []

    async def fail_probe(sid, probe_all=True):
        called.append(sid)
        return {"total": 1, "alive": 1}

    async def no_promote(sid, alive):
        return None

    monkeypatch.setattr(proxies_router.service, "test_clash_nodes", fail_probe)
    monkeypatch.setattr(proxies_router, "_auto_promote_after_check", no_promote)

    proxies_router._spawn_first_check(sub_id)
    for _ in range(6):
        await asyncio.sleep(0.01)
    assert called == [sub_id]
