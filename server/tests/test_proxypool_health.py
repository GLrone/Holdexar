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

import os
import socket
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxypool.health import (  # noqa: E402
    PROBE_TARGET_URL,
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


def _tunnel(conn: socket.socket) -> None:
    try:
        head = conn.recv(8192)
        if not head:
            return
        parts = head.split(b"\r\n", 1)[0].decode("latin-1").split()
        if len(parts) >= 2 and parts[0].upper() == "CONNECT":
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
    def __init__(self) -> None:
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
            threading.Thread(target=_tunnel, args=(conn,), daemon=True).start()

    def close(self) -> None:
        self._sock.close()


class _Target(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


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
    """按需开本地 CONNECT 代理，每个返回一个独立端口。"""
    made: list[_LocalConnectProxy] = []

    def new() -> int:
        proxy = _LocalConnectProxy()
        made.append(proxy)
        return proxy.port

    yield new
    for proxy in made:
        proxy.close()


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

    assert PROBE_TARGET_URL.startswith("https://store.steampowered.com/"), (
        "默认探测目标必须固定在与业务同域的 Steam URL，而不是公共 generate_204"
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