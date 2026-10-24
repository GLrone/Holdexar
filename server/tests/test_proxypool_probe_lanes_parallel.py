"""探测 lane 并行路径：L1/L2 经池运行时的 `HlProbeLane` 组并发探测，与 GLOBAL 解耦。

桩形态：控制器桩记录每个组的 PUT/GET 组选择；每条探测 lane 一个本地 HTTP 桩充当
listener（收到代理请求直接应答）。各 lane 返回**专属**出口 IP/价格——归因错了立刻
暴露；mixed-port 指向死端口——走了 GLOBAL 通道就会失败。无 lane 时串行兜底。
"""
from __future__ import annotations

import json
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import yaml
from sqlalchemy import func, select, update

from app.core.database import get_session_factory, init_db
from app.domains.proxypool import health as health_mod
from app.domains.proxypool.health import (
    business_check_pool,
    current_maintenance_run,
    exit_ip_check_pool,
    pool_probe_lanes,
)
from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription
from app.domains.proxypool.models import (
    HealthObservation,
    HealthRun,
    ProxyNode,
    node_fingerprint,
)
from app.domains.proxypool.pool import POOL_FILENAME
from app.domains.proxypool.runtime import runtime_config_path
from app.domains.proxypool.scheduling import MAINTENANCE_LAST_SIG_KEY, _eligible_signature, run_maintenance_cycle
from app.domains.settings.service import get_value

NOW = datetime(2026, 9, 21, 12, 0, 0)
NODES = ("1|nodeA", "1|nodeB", "1|nodeC")
L1_URL = "http://probe.invalid/ip"
L2_URL = "http://probe.invalid/browse"


# ── 桩 ───────────────────────────────────────────────────────────
class _LaneEcho(BaseHTTPRequestHandler):
    """lane listener 桩：按本 lane 的专属值应答 IP 与业务探测（不转发）。"""

    ip = "0.0.0.0"
    browse_final = "999"

    def do_GET(self):  # noqa: N802
        if "/browse" in self.path:
            payload = {"response": {"store_items": [{
                "id": 220, "appid": 220, "success": 1, "visible": True,
                "best_purchase_option": {"final_price_in_cents": type(self).browse_final},
            }]}}
        else:
            payload = {"ip": type(self).ip}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _ControllerStub(BaseHTTPRequestHandler):
    """内核控制器桩：记录每个 selector 的切换与当前值。"""

    selections: dict[str, str] = {}
    put_counts: dict[str, int] = {}

    def do_PUT(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        group = self.path.split("/proxies/", 1)[-1]
        type(self).put_counts[group] = type(self).put_counts.get(group, 0) + 1
        type(self).selections[group] = str(body.get("name") or "")
        self.send_response(200)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/version"):
            payload = {"version": "stub"}
        else:
            group = self.path.split("/proxies/", 1)[-1]
            payload = {"now": type(self).selections.get(group, "")}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def lane_stubs():
    made: list[ThreadingHTTPServer] = []

    def new(ip: str, final: str = "999") -> int:
        handler = type("_LaneEchoX", (_LaneEcho,), {"ip": ip, "browse_final": final})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        made.append(server)
        return server.server_address[1]

    yield new
    for server in made:
        server.shutdown()


@pytest.fixture
def controller_stub():
    handler = type("_CtrlX", (_ControllerStub,), {"selections": {}, "put_counts": {}})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield handler, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def _seed_pool(tmp_data_dir, *, lane_ports: list[int], ctrl_port: int,
                     mixed_port: int) -> None:
    """池文件 + 带（或不带）探测 lane 的运行配置 + Registry 行。"""
    proxypool = tmp_data_dir / "proxypool"
    proxypool.mkdir(parents=True, exist_ok=True)
    (proxypool / POOL_FILENAME).write_text(yaml.safe_dump(
        {"proxies": [{"name": name, "type": "http", "server": "127.0.0.1",
                      "port": 9000 + i} for i, name in enumerate(NODES)]},
        allow_unicode=True, sort_keys=False,
    ), encoding="utf-8")
    doc: dict = {
        "mode": "global",
        "external-controller": f"127.0.0.1:{ctrl_port}",
        "secret": "",
        "mixed-port": mixed_port,
    }
    if lane_ports:
        doc["listeners"] = [
            {"name": f"HlProbeLane-{i}-in", "type": "mixed", "port": port,
             "listen": "127.0.0.1", "proxy": f"HlProbeLane-{i}"}
            for i, port in enumerate(lane_ports, start=1)
        ]
    runtime_config_path(tmp_data_dir).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    for i, name in enumerate(NODES):
        config = {"name": name.split("|", 1)[-1], "type": "http",
                  "server": "127.0.0.1", "port": 9000 + i}
        fp = node_fingerprint(config)
        async with get_session_factory()() as s:
            if await s.get(ProxySubscription, 1) is None:
                s.add(ProxySubscription(id=1, kind="clash", url="https://source.invalid/1",
                                        admission_status=ADMISSION_ACTIVE))
            s.add(ProxyNode(node_id=fp, fingerprint=fp, runtime_name=name,
                            proxy_type="http", server="127.0.0.1",
                            normalized_config=config, state="ACTIVE",
                            first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
            await s.execute(update(ProxyNode)
                .where(ProxyNode.node_id == fp)
                .values(subscription_id=1, original_name="x"))
            await s.commit()


# ── pool_probe_lanes 读取与端口预检 ──────────────────────────────
@pytest.mark.asyncio
async def test_pool_probe_lanes_reads_live_listeners(
    tmp_data_dir, lane_stubs, controller_stub
) -> None:
    await init_db()
    p1, p2 = lane_stubs("203.0.113.10"), lane_stubs("203.0.113.20")
    await _seed_pool(tmp_data_dir, lane_ports=[p1, p2],
                     ctrl_port=1, mixed_port=_closed_port())
    lanes = pool_probe_lanes(tmp_data_dir)
    assert ("HlProbeLane-1", p1) in lanes and ("HlProbeLane-2", p2) in lanes

    # 死端口的 lane 被剔除：走它会把「通道不通」误判成「节点不通」
    doc = yaml.safe_load(runtime_config_path(tmp_data_dir).read_text(encoding="utf-8"))
    doc["listeners"].append({"name": "HlProbeLane-3-in", "type": "mixed",
                             "port": _closed_port(), "listen": "127.0.0.1",
                             "proxy": "HlProbeLane-3"})
    runtime_config_path(tmp_data_dir).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    assert all(group != "HlProbeLane-3" for group, _ in pool_probe_lanes(tmp_data_dir))


@pytest.mark.asyncio
async def test_pool_probe_lanes_empty_without_config(tmp_data_dir) -> None:
    assert pool_probe_lanes(tmp_data_dir) == []
    (tmp_data_dir / "proxypool").mkdir(parents=True, exist_ok=True)
    runtime_config_path(tmp_data_dir).write_text("not: [valid", encoding="utf-8")
    assert pool_probe_lanes(tmp_data_dir) == []


# ── L2：lane 并行，GLOBAL 不被触碰 ───────────────────────────────
@pytest.mark.asyncio
async def test_l2_business_pool_runs_parallel_via_lanes(
    tmp_data_dir, lane_stubs, controller_stub
) -> None:
    controller_stub, base = controller_stub
    await init_db()
    p1 = lane_stubs("203.0.113.10", final="999")
    p2 = lane_stubs("203.0.113.20", final="888")
    await _seed_pool(tmp_data_dir, lane_ports=[p1, p2],
                     ctrl_port=1, mixed_port=_closed_port())
    controller_stub.selections["GLOBAL"] = "1|nodeA"

    async with get_session_factory()() as s:
        outcomes = await business_check_pool(
            s, data_dir=tmp_data_dir, controller_url=base,
            secret="", now=NOW, url=L2_URL,
        )
        await s.commit()

    assert [(o.runtime_name, o.ok, o.final_price_in_cents) for o in outcomes] == [
        ("1|nodeA", True, 999),   # wave1 → lane-1
        ("1|nodeB", True, 888),   # wave1 → lane-2（lane 归因错了价格立刻暴露）
        ("1|nodeC", True, 999),   # wave2 → lane-1
    ], "每个节点拿到的是它所在 lane 的专属价格——经 lane 直达所选节点"
    # 组选择记录：lane1 = A→C，lane2 = B；全程零 GLOBAL 切换
    assert controller_stub.put_counts == {"HlProbeLane-1": 2, "HlProbeLane-2": 1}
    assert controller_stub.selections["GLOBAL"] == "1|nodeA", "GLOBAL 不得被 lane 通道触碰"
    async with get_session_factory()() as s:
        rows = (await s.execute(
            select(HealthObservation).where(HealthObservation.level == "L2")
        )).scalars().all()
    assert len(rows) == 3 and all(r.ok for r in rows)


# ── L1：lane 并行，出口 IP 按 lane 归因 ──────────────────────────
@pytest.mark.asyncio
async def test_l1_exit_ip_pool_runs_parallel_via_lanes(
    tmp_data_dir, lane_stubs, controller_stub
) -> None:
    controller_stub, base = controller_stub
    await init_db()
    p1 = lane_stubs("203.0.113.10")
    p2 = lane_stubs("203.0.113.20")
    await _seed_pool(tmp_data_dir, lane_ports=[p1, p2],
                     ctrl_port=1, mixed_port=_closed_port())
    controller_stub.selections["GLOBAL"] = "1|nodeA"

    async with get_session_factory()() as s:
        outcomes = await exit_ip_check_pool(
            s, data_dir=tmp_data_dir, controller_url=base,
            secret="", now=NOW, url=L1_URL,
        )
        await s.commit()

    assert [(o.runtime_name, o.ok, o.exit_ip) for o in outcomes] == [
        ("1|nodeA", True, "203.0.113.10"),
        ("1|nodeB", True, "203.0.113.20"),
        ("1|nodeC", True, "203.0.113.10"),  # wave2 复用 lane-1 → lane-1 的出口
    ]
    assert controller_stub.put_counts == {"HlProbeLane-1": 2, "HlProbeLane-2": 1}
    assert controller_stub.selections["GLOBAL"] == "1|nodeA"
    async with get_session_factory()() as s:
        nodes = (await s.execute(select(ProxyNode))).scalars().all()
    by_name = {n.runtime_name: n for n in nodes}
    assert by_name["1|nodeA"].exit_ip == "203.0.113.10"
    assert by_name["1|nodeB"].exit_ip == "203.0.113.20"
    assert by_name["1|nodeC"].exit_ip == "203.0.113.10"


# ── 无 lane：GLOBAL 串行兜底 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_pools_fall_back_to_serial_global_without_lanes(
    tmp_data_dir, lane_stubs, controller_stub
) -> None:
    controller_stub, base = controller_stub
    await init_db()
    shared = lane_stubs("203.0.113.30", final="777")
    await _seed_pool(tmp_data_dir, lane_ports=[],
                     ctrl_port=1, mixed_port=shared)
    controller_stub.selections["GLOBAL"] = "1|nodeA"

    async with get_session_factory()() as s:
        l1 = await exit_ip_check_pool(
            s, data_dir=tmp_data_dir, controller_url=base,
            secret="", now=NOW, url=L1_URL,
        )
        l2 = await business_check_pool(
            s, data_dir=tmp_data_dir, controller_url=base,
            secret="", now=NOW, url=L2_URL,
        )
        await s.commit()

    assert all(o.ok and o.exit_ip == "203.0.113.30" for o in l1)
    assert all(o.ok and o.final_price_in_cents == 777 for o in l2)
    # 串行路径：每个节点各切一次 GLOBAL（L1 三次 + L2 三次）
    assert controller_stub.put_counts.get("GLOBAL", 0) == 6
    assert controller_stub.selections["GLOBAL"] == "1|nodeC"


# ── 维护事务整体：totals 播报 + progress 带 total + 收尾签名 ─────
@pytest.mark.asyncio
async def test_maintenance_cycle_parallel_with_totals(
    tmp_data_dir, lane_stubs, controller_stub, monkeypatch
) -> None:
    controller_stub, base = controller_stub
    from app.core.events import bus

    await init_db()
    p1 = lane_stubs("203.0.113.10")
    p2 = lane_stubs("203.0.113.20")
    await _seed_pool(tmp_data_dir, lane_ports=[p1, p2],
                     ctrl_port=1, mixed_port=_closed_port())
    controller_stub.selections["GLOBAL"] = "1|nodeA"

    events: list[tuple[str, dict]] = []

    def _capture(event, **payload):
        events.append((event, payload))

    monkeypatch.setattr(bus, "publish", _capture)

    async with get_session_factory()() as s:
        result = await run_maintenance_cycle(
            s, data_dir=tmp_data_dir, controller_url=base,
            secret="", now=NOW, l1_url=L1_URL, l2_url=L2_URL,
        )
        await s.commit()

    assert result is not None
    assert len(result.l1) == 3 and len(result.l2) == 3, "L1/L2 经 lane 并行探完整池"
    started = [payload for event, payload in events if event == "maintenance.started"]
    assert started and started[0]["totals"] == {"l1": 3, "l2": 3}, (
        "started 事件必须带计划总量（两层各 = 池节点数）"
    )
    progress = [payload for event, payload in events if event == "maintenance.progress"]
    assert progress and all(p.get("total") == 3 for p in progress), (
        "progress 事件必须带计划总量"
    )
    async with get_session_factory()() as s:
        run = (await s.execute(
            select(HealthRun).order_by(HealthRun.id.desc()).limit(1)
        )).scalar_one()
        assert run.finished_at is not None and run.end_reason is None
        assert run.total == 3 and run.steam_ok == 3
        assert await get_value(MAINTENANCE_LAST_SIG_KEY) == _eligible_signature(NODES), (
            "成功体检收尾必须记录合格集签名"
        )


@pytest.mark.asyncio
async def test_current_maintenance_run_reports_pool_totals(
    tmp_data_dir, lane_stubs, controller_stub
) -> None:
    await init_db()
    p1 = lane_stubs("203.0.113.10")
    await _seed_pool(tmp_data_dir, lane_ports=[p1], ctrl_port=1,
                     mixed_port=_closed_port())
    async with get_session_factory()() as s:
        s.add(HealthRun(channel="scheduled", started_at=NOW))
        await s.commit()

    live = await current_maintenance_run()
    assert live is not None
    assert live["probed"] == {"l1": 0, "l2": 0}
    assert live["totals"] == {"l1": 3, "l2": 3}, "计划总量 = 池文件节点数"
