"""P1.4-A：`/proxies/{name}/delay` 最小健康检测（4 条）。

第一版只回答：**这个节点经 Mihomo 发起一次真实 HTTP(S) 探测，能否在规定时间内
拿到预期状态。** 不回答出口 IP（L1），也不回答能不能扛爬虫业务（L2）。

探测目标默认固定成 Holdexar 真正依赖的 Steam 同域 URL（见 `PROBE_TARGET_URL`）；
测试里换成**本地**目标服务，才能既真实又不需要外网。

真内核、真控制器、真 `/delay`；池内「可用节点」是一个本地真实 CONNECT 隧道代理
（内核真的连它、真的经它发请求），不是 mock 掉 runtime。
本机没有内核资产时整组跳过，不伪装成通过。
"""
from __future__ import annotations

import json
import os
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from app.crawler.browse_store import StoreBrowseAPI  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxypool.health import (  # noqa: E402
    BUSINESS_OK,
    EXIT_IP_TARGET_URL,
    INVALID_BUSINESS_RESPONSE,
    PROBE_TARGET_URL,
    TARGET_SERVICE_FAILED,
    business_check_pool,
    business_probe_url,
    exit_ip_check_pool,
    health_check_pool,
    probe_node,
)
from app.domains.proxypool.models import (  # noqa: E402
    HealthObservation,
    ProxyNode,
    ProxyNodeSource,
    node_fingerprint,
)
from app.domains.proxypool.pool import build_pool  # noqa: E402
from app.domains.proxypool.runtime import (  # noqa: E402
    prepare_runtime_config,
    wait_proxy_names,
)
from app.domains.proxypool.state import (  # noqa: E402
    NODE_ACTIVE,
    NODE_DEAD,
    NODE_NEW,
    NODE_RETIRED,
    NODE_STALE,
)

NOW = datetime(2026, 9, 19, 12, 0, 0)


# ── 本地真实 CONNECT 隧道代理（当"可用节点"用）────────────────────
def _pipe(a: socket.socket, b: socket.socket) -> None:
    try:
        while True:
            chunk = a.recv(65536)
            if not chunk:
                break
            b.sendall(chunk)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def _tunnel(conn: socket.socket, redirect_to: int | None = None) -> None:
    try:
        head = conn.recv(8192)
        if not head:
            return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) >= 2 and parts[0].upper() == "CONNECT":
            if redirect_to is not None:
                # L1 用：忽略 CONNECT 目标，一律隧道到本节点专属回显，
                # 让"每个节点有自己的出口身份"可证
                upstream = socket.create_connection(("127.0.0.1", redirect_to), timeout=5)
            else:
                host, _, port = parts[1].partition(":")
                upstream = socket.create_connection((host, int(port)), timeout=5)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            threading.Thread(target=_pipe, args=(conn, upstream), daemon=True).start()
            _pipe(upstream, conn)
        else:
            conn.sendall(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n")
    except Exception:  # noqa: BLE001
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


class _LocalConnectProxy:
    def __init__(self, redirect_to: int | None = None) -> None:
        self._redirect_to = redirect_to
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(64)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=_tunnel, args=(conn, self._redirect_to),
                             daemon=True).start()

    def close(self) -> None:
        self._sock.close()


class _Target(BaseHTTPRequestHandler):
    """L0 的探测目标（只求 2xx）。"""

    def do_GET(self):  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


class _Echo(BaseHTTPRequestHandler):
    """模仿 ipify 的出口 IP 回显（每个节点一个，返回各自的合成 IP）。"""

    fake_ip = "0.0.0.0"
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        body = json.dumps({"ip": self.fake_ip}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class _BrokenTarget(BaseHTTPRequestHandler):
    """回显服务自身故障：用来证明 L1 失败不得归因给节点。"""

    def do_GET(self):  # noqa: N802
        self.send_response(503)
        self.end_headers()

    def log_message(self, *args):
        pass


class _FakeStoreBrowse(BaseHTTPRequestHandler):
    """假 StoreBrowse：返回**实测到的真实信封结构**（可换 payload / 状态码）。"""

    payload: dict = {}
    status = 200
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        type(self).hits.append(self.path)
        body = json.dumps(self.payload, ensure_ascii=False).encode()
        self.send_response(self.status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _browse_payload(appid: int = 220, final: str = "999",
                    success=1) -> dict:
    """生产 GetItems 的真实结构：`success` 是整数 1，价格是**字符串**。"""
    return {"response": {"store_items": [{
        "id": appid, "appid": appid, "success": success,
        "visible": True, "item_type": 0, "name": "Half-Life 2",
        "best_purchase_option": {
            "packageid": 36, "package_group": "default",
            "final_price_in_cents": final,
        },
    }]}}


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── fixtures ─────────────────────────────────────────────────────
@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture
def kernel_exe_path(tmp_data_dir) -> Path:
    for candidate in (
        clash_manager.kernel_exe(tmp_data_dir),
        clash_manager.bundled_clash_dir() / kernel_filename(),
        *(
            sorted(Path(os.environ["LOCALAPPDATA"]).glob(f"holdexar*/clash/{kernel_filename()}"))
            if os.environ.get("LOCALAPPDATA") else []
        ),
    ):
        if candidate.is_file():
            return candidate
    pytest.skip("本机没有可用的 mihomo 内核资产（assets/clash/ 不随仓库分发）")


@pytest.fixture
def clash_runtime():
    runtime = ClashRuntime()
    yield runtime
    runtime.stop()


@pytest.fixture
def local_proxies():
    """按需开本地 CONNECT 代理，每个返回一个独立端口。

    `redirect_to` 给 L1 用：把该节点的一切流量固定隧道到自己专属的回显服务，
    这样"选了 A 却拿到 B 的出口 IP"会立刻暴露。
    """
    made: list[_LocalConnectProxy] = []

    def new(redirect_to: int | None = None) -> int:
        proxy = _LocalConnectProxy(redirect_to)
        made.append(proxy)
        return proxy.port

    yield new
    for proxy in made:
        proxy.close()


@pytest.fixture
def echoes():
    """按需造出口 IP 回显服务，返回 (端口, 命中记录)。"""
    made: list[ThreadingHTTPServer] = []

    def new(fake_ip: str) -> tuple[int, list[str]]:
        hits: list[str] = []
        handler = type("_EchoX", (_Echo,), {"fake_ip": fake_ip, "hits": hits})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        made.append(server)
        return server.server_address[1], hits

    yield new
    for server in made:
        server.shutdown()


@pytest.fixture
def broken_target():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BrokenTarget)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


@pytest.fixture
def fake_browse():
    """按需造假 StoreBrowse，返回 (端口, 命中记录)。"""
    made: list[ThreadingHTTPServer] = []

    def new(payload: dict, status: int = 200) -> tuple[int, list[str]]:
        hits: list[str] = []
        handler = type("_BrowseX", (_FakeStoreBrowse,),
                       {"payload": payload, "status": status, "hits": hits})
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        made.append(server)
        return server.server_address[1], hits

    yield new
    for server in made:
        server.shutdown()


@pytest.fixture
def probe_target():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Target)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/"
    server.shutdown()


# ── 数据与流水线 ─────────────────────────────────────────────────
async def _add(state: str, runtime_name: str, *, port: int,
               with_source: bool = True) -> str:
    config = {"name": runtime_name.split("|", 1)[-1], "type": "http",
              "server": "127.0.0.1", "port": port}
    fp = node_fingerprint(config)
    async with get_session_factory()() as s:
        s.add(ProxyNode(
            node_id=fp, fingerprint=fp, runtime_name=runtime_name,
            proxy_type="http", server="127.0.0.1", normalized_config=config,
            state=state, first_seen=NOW, last_seen=NOW, last_source_seen=NOW,
        ))
        if with_source:
            s.add(ProxyNodeSource(
                node_id=fp, subscription_id=1, original_name="x",
                first_seen=NOW, last_seen=NOW,
            ))
        await s.commit()
    return fp


async def _start_kernel(clash_runtime: ClashRuntime, exe: Path, data_dir: Path):
    async with get_session_factory()() as s:
        await build_pool(s, data_dir=data_dir)
    status = clash_runtime.start(str(exe), str(prepare_runtime_config(data_dir)))
    base = status["controllerUrl"]
    await wait_proxy_names(base, clash_runtime.secret, timeout=15)
    return base, clash_runtime.secret


async def _health(data_dir: Path, base: str, secret: str, target: str,
                  *, timeout_ms: int = 3000):
    async with get_session_factory()() as s:
        outcomes = await health_check_pool(
            s, data_dir=data_dir, controller_url=base, secret=secret,
            now=NOW, url=target, timeout_ms=timeout_ms,
        )
        await s.commit()
        return outcomes


async def _state_of(runtime_name: str) -> str:
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxyNode).where(ProxyNode.runtime_name == runtime_name)
        )).scalar_one()
        return row.state


# ── 1. 可用节点：真探测拿到 delay ────────────────────────────────
@pytest.mark.asyncio
async def test_working_node_gets_a_delay(tmp_data_dir, kernel_exe_path,
                                         clash_runtime, local_proxies,
                                         probe_target) -> None:
    await init_db()
    await _add(NODE_NEW, "1|香港01", port=local_proxies())
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _health(tmp_data_dir, base, secret, probe_target)

    assert outcome.ok, f"可用节点应当探测成功，实际 detail={outcome.detail}"
    assert isinstance(outcome.delay_ms, int) and outcome.delay_ms >= 0
    assert outcome.runtime_name == "1|香港01"

    async with get_session_factory()() as s:
        obs = (await s.execute(select(HealthObservation))).scalar_one()
    assert obs.level == "L0" and obs.ok is True
    assert obs.latency_ms == outcome.delay_ms and obs.observed_at == NOW

    assert PROBE_TARGET_URL == "http://www.gstatic.com/generate_204", (
        "L0 只证传输可用：目标必须是最轻的连通性探测，且不得是生产业务主机"
    )
    assert "steampowered" not in PROBE_TARGET_URL, (
        "生产主机的抖动会被 L0 失败→DEAD 放大成节点误杀；业务可用性归 L2"
    )


# ── 2. 不可用节点：明确的失败观测 ────────────────────────────────
@pytest.mark.asyncio
async def test_dead_node_yields_explicit_failure_observation(
    tmp_data_dir, kernel_exe_path, clash_runtime, probe_target
) -> None:
    await init_db()
    await _add(NODE_NEW, "1|香港01", port=_closed_port())
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _health(tmp_data_dir, base, secret, probe_target)

    assert not outcome.ok
    assert outcome.delay_ms is None
    assert "探测失败" in outcome.detail, (
        "内核已定位到节点但探测失败（503），与「名字没定位到」（404）必须分开"
    )

    async with get_session_factory()() as s:
        obs = (await s.execute(select(HealthObservation))).scalar_one()
    assert obs.ok is False and obs.latency_ms is None and obs.detail


# ── 3. 特殊 runtime_name 能正确定位节点 ──────────────────────────
@pytest.mark.asyncio
async def test_special_runtime_names_are_located(
    tmp_data_dir, kernel_exe_path, clash_runtime, probe_target
) -> None:
    """`|` `:` `#` 空格 Unicode `/` `?` `&` `=` 都必须能定位到节点。

    `#` 不编码会被当成 URL fragment 截断——实测那样请求会落到 404，而不是找到节点。
    """
    await init_db()
    names = ["1|香港01", "2|usa: west #1", "3|东京 ⚡", "4|a/b?c&d=e"]
    ports, seen = [], set()
    while len(ports) < len(names):
        port = _closed_port()
        if port not in seen:
            seen.add(port)
            ports.append(port)
    for name, port in zip(names, ports):
        await _add(NODE_NEW, name, port=port)
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    outcomes = await _health(tmp_data_dir, base, secret, probe_target)

    assert [o.runtime_name for o in outcomes] == names
    for outcome in outcomes:
        assert not outcome.ok, "这些节点都指向死端口，必然探测失败"
        assert "没有这个节点" not in outcome.detail, (
            f"{outcome.runtime_name!r} 没被定位到——路径编码有问题"
        )

    # 对照：内核里没有的名字必须走另一条分支，证明上面不是「一律失败」
    missing = await probe_node(base, secret, "根本不存在的节点",
                              url=probe_target, timeout_ms=1000)
    assert not missing.ok and "没有这个节点" in missing.detail


# ── 4. 状态机：OK / FAIL 落到正确状态 ────────────────────────────
@pytest.mark.asyncio
async def test_probe_result_drives_state(tmp_data_dir, kernel_exe_path,
                                         clash_runtime, local_proxies,
                                         probe_target) -> None:
    await init_db()
    await _add(NODE_NEW, "1|新可用", port=local_proxies())
    await _add(NODE_NEW, "1|新不可用", port=_closed_port())
    await _add(NODE_ACTIVE, "1|活不可用", port=_closed_port())
    await _add(NODE_STALE, "1|旧可用", port=local_proxies())
    await _add(NODE_STALE, "1|旧可用但无来源", port=local_proxies(), with_source=False)
    await _add(NODE_RETIRED, "1|已退休", port=local_proxies())

    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)
    await _health(tmp_data_dir, base, secret, probe_target)

    assert await _state_of("1|新可用") == NODE_ACTIVE
    assert await _state_of("1|新不可用") == NODE_DEAD
    assert await _state_of("1|活不可用") == NODE_DEAD
    assert await _state_of("1|旧可用") == NODE_ACTIVE
    assert await _state_of("1|旧可用但无来源") == NODE_STALE, (
        "无来源的 STALE 即使探测成功也不该被提升为 ACTIVE——它已经没有任何订阅在提供"
    )
    assert await _state_of("1|已退休") == NODE_RETIRED, (
        "RETIRED 不参与本阶段：它不在池里、不会被探测，也就不会被一次成功自动复活"
    )


# ══ P1.4-B：L1 出口 IP ═══════════════════════════════════════════
# 本地回显是明文 HTTP，所以目标 URL 用 http://（https 会先握手 TLS，回显谈不了）。
LOCAL_EXIT_TARGET = "http://exit-echo.invalid/"


async def _l1(data_dir: Path, base: str, secret: str, target: str):
    async with get_session_factory()() as s:
        outcomes = await exit_ip_check_pool(
            s, data_dir=data_dir, controller_url=base, secret=secret,
            now=NOW, url=target,
        )
        await s.commit()
        return outcomes


async def _set_exit_ip(runtime_name: str, ip: str) -> None:
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxyNode).where(ProxyNode.runtime_name == runtime_name)
        )).scalar_one()
        row.exit_ip = ip
        await s.commit()


async def _node(runtime_name: str) -> ProxyNode:
    async with get_session_factory()() as s:
        return (await s.execute(
            select(ProxyNode).where(ProxyNode.runtime_name == runtime_name)
        )).scalar_one()


# ── L1-1. 单节点：选中它并拿到它专属的出口 IP ────────────────────
@pytest.mark.asyncio
async def test_l1_single_node_yields_its_own_exit_ip(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, echoes
) -> None:
    await init_db()
    echo_port, hits = echoes("203.0.113.10")
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(echo_port))
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _l1(tmp_data_dir, base, secret, LOCAL_EXIT_TARGET)

    assert outcome.ok, f"L1 应当成功，实际 detail={outcome.detail}"
    assert outcome.exit_ip == "203.0.113.10"
    assert hits, "请求没有经过该节点专属回显——说明没真的走出这个节点"
    assert EXIT_IP_TARGET_URL.startswith("https://api.ipify.org"), (
        "生产默认目标固定成公网 IP 回显服务；测试才换成本地目标"
    )


# ── L1-2. 串行切换：各节点的出口 IP 归属必须正确 ─────────────────
@pytest.mark.asyncio
async def test_l1_serial_switch_attributes_each_exit_ip(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, echoes
) -> None:
    """只有一个 GLOBAL 选择器，切换是全局状态——归因正确性是测量的前提。

    两个节点各自隧道到**自己的**回显（不同合成出口 IP），所以"选了 A 却拿到 B 的
    出口"会立刻暴露；若两个节点返回同一个出口，这条根本证不了。
    """
    await init_db()
    echo_a, hits_a = echoes("203.0.113.10")
    echo_b, hits_b = echoes("203.0.113.20")
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(echo_a))
    await _add(NODE_ACTIVE, "2|usa: west #1", port=local_proxies(echo_b))
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    outcomes = await _l1(tmp_data_dir, base, secret, LOCAL_EXIT_TARGET)

    assert [(o.runtime_name, o.exit_ip) for o in outcomes] == [
        ("1|香港01", "203.0.113.10"),
        ("2|usa: west #1", "203.0.113.20"),
    ]
    assert len(hits_a) == 1 and len(hits_b) == 1, "各节点只该命中自己的回显一次"
    assert await _node("1|香港01") is not None


# ── L1-3. 回显服务自身故障：只记观测，绝不判节点死 ───────────────
@pytest.mark.asyncio
async def test_l1_target_service_failure_never_kills_the_node(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, broken_target
) -> None:
    await init_db()
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(broken_target))
    await _set_exit_ip("1|香港01", "198.51.100.7")
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _l1(tmp_data_dir, base, secret, LOCAL_EXIT_TARGET)

    assert not outcome.ok
    assert "TARGET_SERVICE_FAILED" in outcome.detail, (
        "回显服务 503 必须记成目标服务故障，不能记成节点故障"
    )
    assert outcome.exit_ip is None

    node = await _node("1|香港01")
    assert node.state == NODE_ACTIVE, "L1 失败首版不改 state——否则 ipify 503 会判节点 DEAD"
    assert node.exit_ip == "198.51.100.7", "一次目标服务故障不得擦掉已观测到的出口事实"

    async with get_session_factory()() as s:
        obs = (await s.execute(select(HealthObservation))).scalar_one()
    assert obs.level == "L1" and obs.ok is False and obs.detail


# ── L1-4. 成功：落 L1 观测 + 出口 IP，不碰 state 与 L0 字段 ───────
@pytest.mark.asyncio
async def test_l1_success_records_observation_and_exit_ip_only(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, echoes
) -> None:
    await init_db()
    echo_port, _ = echoes("203.0.113.10")
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(echo_port))
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _l1(tmp_data_dir, base, secret, LOCAL_EXIT_TARGET)

    assert outcome.ok and outcome.exit_ip == "203.0.113.10"
    node = await _node("1|香港01")
    assert node.exit_ip == "203.0.113.10"
    assert node.state == NODE_ACTIVE, "L1 只记录观测，状态归规则层"
    assert node.last_l0_at is None, "L1 不得把耗时写进 L0 字段"
    assert node.capacity_score is None and node.consecutive_failures == 0

    async with get_session_factory()() as s:
        rows = list((await s.execute(select(HealthObservation))).scalars())
    assert len(rows) == 1, "L1 不该顺带跑一次 L0（那会污染 L0 的语义）"
    assert rows[0].level == "L1" and rows[0].ok is True and rows[0].observed_at == NOW
    assert rows[0].latency_ms is not None, "耗时记录下来，但首版不参与评分"


# ══ P1.4-C：L2 业务可用性（生产 StoreBrowse 契约）═══════════════
# 本地假服务是明文 HTTP，所以目标 URL 用 http://（https 会先握手 TLS）。
LOCAL_BUSINESS_TARGET = "http://fake-browse.invalid/IStoreBrowseService/GetItems/v1/"


async def _l2(data_dir: Path, base: str, secret: str, target: str):
    async with get_session_factory()() as s:
        outcomes = await business_check_pool(
            s, data_dir=data_dir, controller_url=base, secret=secret,
            now=NOW, url=target,
        )
        await s.commit()
        return outcomes


async def _l2_observations() -> list[HealthObservation]:
    async with get_session_factory()() as s:
        return list((await s.execute(select(HealthObservation))).scalars())


# ── L2-1. 业务成功：只落观测，别的一律不碰 ──────────────────────
@pytest.mark.asyncio
async def test_l2_business_ok_records_observation_only(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, fake_browse
) -> None:
    await init_db()
    browse_port, hits = fake_browse(_browse_payload(final="999"))
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(browse_port))
    await _set_exit_ip("1|香港01", "198.51.100.7")
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _l2(tmp_data_dir, base, secret, LOCAL_BUSINESS_TARGET)

    assert outcome.ok, f"业务应当成功，实际 detail={outcome.detail}"
    assert outcome.http_status == 200
    assert outcome.final_price_in_cents == 999, (
        'final_price_in_cents 生产返回字符串 "999"，必须按 crawler _to_int 语义解析'
    )
    assert BUSINESS_OK in outcome.detail
    assert hits, "业务请求没有经过该节点专属的假 StoreBrowse"

    node = await _node("1|香港01")
    assert node.state == NODE_ACTIVE, "L2 只记录观测，状态归规则层"
    assert node.exit_ip == "198.51.100.7", "L2 不得擦掉 L1 观测到的出口事实"
    assert node.last_l0_at is None and node.last_l1_at is None

    (obs,) = await _l2_observations()
    assert obs.level == "L2" and obs.ok is True and obs.observed_at == NOW
    assert obs.latency_ms is not None

    # 契约防回归：L2 目标必须就是生产 StoreBrowse 契约，不能在 health 里另建一套
    assert business_probe_url(220) == StoreBrowseAPI.probe_url("us", 220)
    assert "appdetails" not in business_probe_url(220)
    assert "IStoreBrowseService" in business_probe_url(220)


# ── L2-2. 串行切换：业务归因必须正确 ─────────────────────────────
@pytest.mark.asyncio
async def test_l2_serial_switch_attributes_business_response(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, fake_browse
) -> None:
    """两个节点各自隧道到自己的假 StoreBrowse，返回不同价格。

    若两个节点最终都走了同一个出口，「A→999、B→1234」这条会立刻失败。
    """
    await init_db()
    port_a, hits_a = fake_browse(_browse_payload(final="999"))
    port_b, hits_b = fake_browse(_browse_payload(final="1234"))
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(port_a))
    await _add(NODE_ACTIVE, "2|usa: west #1", port=local_proxies(port_b))
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    outcomes = await _l2(tmp_data_dir, base, secret, LOCAL_BUSINESS_TARGET)

    assert [(o.runtime_name, o.final_price_in_cents) for o in outcomes] == [
        ("1|香港01", 999),
        ("2|usa: west #1", 1234),
    ]
    assert len(hits_a) == 1 and len(hits_b) == 1, "各节点只该命中自己的假服务一次"


# ── L2-3. 业务目标故障：不改变节点状态 ───────────────────────────
@pytest.mark.asyncio
async def test_l2_target_service_failure_never_changes_state(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, fake_browse
) -> None:
    await init_db()
    browse_port, _ = fake_browse({"message": "boom"}, status=503)
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(browse_port))
    await _set_exit_ip("1|香港01", "198.51.100.7")
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _l2(tmp_data_dir, base, secret, LOCAL_BUSINESS_TARGET)

    assert not outcome.ok
    assert TARGET_SERVICE_FAILED in outcome.detail
    assert outcome.http_status == 503

    node = await _node("1|香港01")
    assert node.state == NODE_ACTIVE, (
        "业务目标 503 不得判节点死——否则就是让外部服务状态决定代理池健康"
    )
    assert node.exit_ip == "198.51.100.7"

    (obs,) = await _l2_observations()
    assert obs.level == "L2" and obs.ok is False and obs.detail


# ── L2-4. HTTP 200 但业务响应非法 ────────────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,marker",
    [
        # 生产实测 success 是整数 1；写成布尔 true 就是契约不符
        (_browse_payload(success=True), "success"),
        # 价格字符串不可解析 → 必须走 _to_int 语义而不是 isinstance(int)
        (_browse_payload(final="not-a-number"), "final_price_in_cents"),
    ],
)
async def test_l2_invalid_business_response(
    tmp_data_dir, kernel_exe_path, clash_runtime, local_proxies, fake_browse,
    payload: dict, marker: str
) -> None:
    await init_db()
    browse_port, _ = fake_browse(payload)
    await _add(NODE_ACTIVE, "1|香港01", port=local_proxies(browse_port))
    base, secret = await _start_kernel(clash_runtime, kernel_exe_path, tmp_data_dir)

    (outcome,) = await _l2(tmp_data_dir, base, secret, LOCAL_BUSINESS_TARGET)

    assert not outcome.ok, "HTTP 200 但业务契约不符，不能算成功"
    assert outcome.http_status == 200
    assert INVALID_BUSINESS_RESPONSE in outcome.detail
    assert marker in outcome.detail, "detail 要指出是哪一条业务契约不满足"
    assert outcome.final_price_in_cents is None

    node = await _node("1|香港01")
    assert node.state == NODE_ACTIVE
    (obs,) = await _l2_observations()
    assert obs.level == "L2" and obs.ok is False