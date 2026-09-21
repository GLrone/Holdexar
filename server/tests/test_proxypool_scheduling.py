"""P1.6-B：调度边界（7 条）。

把 P1.6-A 摸到的现状固化成契约：

    Crawl 是 Runtime 的占用者；L1/L2 是维护者；rebuild 是破坏性操作。
    维护者只能在占用结束后做破坏性操作。

1. 两条 crawler 路径统一互斥（`start_job` 与 bundles 直调 `run_crawl` 共用同一占用语义）
2. crawl 占线时 L0 照跑、只改状态、置 `rebuild_pending`，**不重启内核**
3. crawl 结束后空闲时消费 pending → rebuild → 新端口 + GLOBAL 恢复
4. crawl 占线时 L1/L2 **直接跳过**（不排队、不动 GLOBAL）
5. L1/L2 是 capture → probe → restore 事务（恢复读**当前**池，不用开始时缓存的列表）
6. proxypool Runtime 与老订阅 Runtime 真正隔离（不碰对方进程/端口）
7. proxypool 的 controller 端口不依赖共享默认值 19090（不与其他 Runtime 撞）

不测（本批边界）：固定 mixed-port、失败 1/2/3 次阈值、运行中请求切换。
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.crawler.occupancy import CrawlerBusyError, begin_crawl, crawler_busy, end_crawl  # noqa: E402
from app.crawler.runner import CrawlRunConfig, run_crawl  # noqa: E402
from app.domains.crawl import service as crawl_service  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode, ProxyNodeSource, node_fingerprint,
)
from app.domains.proxypool.pool import build_pool, pool_path  # noqa: E402
from app.domains.proxypool.runtime import (  # noqa: E402
    prepare_runtime_config, rebuild_runtime, runtime_config_path, wait_mixed_port,
    wait_proxy_names,
)
from app.domains.proxypool.scheduling import (  # noqa: E402
    rebuild_pending, request_rebuild, run_l0_cycle, run_maintenance_cycle,
    run_pending_rebuild, take_rebuild_pending,
)
from app.domains.proxypool.state import NODE_ACTIVE, NODE_DEAD  # noqa: E402

NOW = datetime(2026, 9, 19, 12, 0, 0)
DEFAULT_CONTROLLER_PORT = 19090  # 项目注入的共享默认值——proxypool 不得依赖它


# ── 本地夹具 ─────────────────────────────────────────────────────
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


class _RedirectProxy:
    """本地 CONNECT 代理：忽略目标，一律隧道到同一回显。"""

    def __init__(self, redirect_to: int) -> None:
        self._redirect_to = redirect_to
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(32)
        self.port = self._sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        upstream = None
        try:
            head = conn.recv(8192)
            if not head:
                return
            upstream = socket.create_connection(("127.0.0.1", self._redirect_to),
                                                timeout=8)
            if head.split(b" ", 1)[0].upper() == b"CONNECT":
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                upstream.sendall(head)
            threading.Thread(target=_pipe, args=(conn, upstream), daemon=True).start()
            _pipe(upstream, conn)
        except Exception:  # noqa: BLE001
            pass
        finally:
            for s in (conn, upstream):
                if s is not None:
                    try:
                        s.close()
                    except OSError:
                        pass

    def close(self) -> None:
        self._sock.close()


class _ProbeEcho(BaseHTTPRequestHandler):
    """同时充当 L1（出口 IP）与 L2（StoreBrowse）目标，按路径分流。"""

    def do_GET(self):  # noqa: N802
        if "/browse" in self.path:
            payload = {"response": {"store_items": [{
                "id": 220, "appid": 220, "success": 1, "visible": True,
                "best_purchase_option": {"final_price_in_cents": "999"},
            }]}}
        else:  # /ip
            payload = {"ip": "203.0.113.10"}
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


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


@pytest.fixture(autouse=True)
def clean_scheduling_state():
    """占用与 pending 都是进程内状态，逐个测试清干净。"""
    take_rebuild_pending()
    if crawler_busy():
        end_crawl()
    yield
    take_rebuild_pending()
    if crawler_busy():
        end_crawl()


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
def proxy_runtime():
    """proxypool 自己的 Runtime 实例——绝不共用 clash_manager.runtime。"""
    runtime = ClashRuntime()
    yield runtime
    runtime.stop()


@pytest.fixture
def redirect_proxies():
    made: list[_RedirectProxy] = []

    def new(redirect_to: int) -> int:
        proxy = _RedirectProxy(redirect_to)
        made.append(proxy)
        return proxy.port

    yield new
    for proxy in made:
        proxy.close()


@pytest.fixture
def probe_echo():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _ProbeEcho)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_address[1]
    server.shutdown()


# ── 数据与启动辅助 ───────────────────────────────────────────────
async def _add(runtime_name: str, *, port: int, state: str = NODE_ACTIVE) -> None:
    config = {"name": runtime_name.split("|", 1)[-1], "type": "http",
              "server": "127.0.0.1", "port": port}
    fp = node_fingerprint(config)
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id=fp, fingerprint=fp, runtime_name=runtime_name,
                        proxy_type="http", server="127.0.0.1",
                        normalized_config=config, state=state,
                        first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        s.add(ProxyNodeSource(node_id=fp, subscription_id=1, original_name="x",
                              first_seen=NOW, last_seen=NOW))
        await s.commit()


async def _set_state(runtime_name: str, state: str) -> None:
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxyNode).where(ProxyNode.runtime_name == runtime_name)
        )).scalar_one()
        row.state = state
        await s.commit()


async def _state_of(runtime_name: str) -> str:
    async with get_session_factory()() as s:
        return (await s.execute(
            select(ProxyNode).where(ProxyNode.runtime_name == runtime_name)
        )).scalar_one().state


async def _boot(runtime: ClashRuntime, exe: Path, data_dir: Path):
    async with get_session_factory()() as s:
        await build_pool(s, data_dir=data_dir)
    status = runtime.start(str(exe), str(prepare_runtime_config(data_dir)))
    await wait_proxy_names(status["controllerUrl"], runtime.secret, timeout=20)
    await wait_mixed_port(data_dir, timeout=20)
    return status["controllerUrl"], runtime.secret


async def _global_now(base: str, secret: str) -> str | None:
    async with httpx.AsyncClient(
        timeout=10, headers={"Authorization": f"Bearer {secret}"}
    ) as c:
        return (await c.get(f"{base}/proxies/GLOBAL")).json().get("now")


async def _select(base: str, secret: str, name: str) -> str | None:
    async with httpx.AsyncClient(
        timeout=10, headers={"Authorization": f"Bearer {secret}"}
    ) as c:
        await c.put(f"{base}/proxies/GLOBAL", json={"name": name})
        return (await c.get(f"{base}/proxies/GLOBAL")).json().get("now")


# ══ 1. 两条 crawler 路径统一互斥 ═════════════════════════════════
@pytest.mark.asyncio
async def test_crawl_paths_share_one_occupancy(tmp_data_dir, monkeypatch) -> None:
    """`run_crawl` 是唯一的生产执行入口；占用语义必须统一，不能只看 `_active`。

    bundles 直调 `run_crawl` 不经 `start_job`，所以占用必须落在**执行入口**上。
    """
    await init_db()
    begin_crawl("test-bundles")          # 模拟 bundles 那条直调路径正在跑

    with pytest.raises(CrawlerBusyError):
        await run_crawl(None, config=CrawlRunConfig(regions=["us"]))

    # 手动路径同样必须被挡下（且在创建 job 之前就挡，不留下"失败的任务行"）
    with pytest.raises(RuntimeError, match="已有爬取任务"):
        await crawl_service.start_job(scope="appids", appids=[220], regions=["us"])

    end_crawl()
    assert not crawler_busy()


# ══ 2. 占线时 L0 只改状态、置 pending、不重启内核 ════════════════
@pytest.mark.asyncio
async def test_l0_while_busy_marks_pending_without_restart(
    tmp_data_dir, kernel_exe_path, proxy_runtime, redirect_proxies, probe_echo
) -> None:
    await init_db()
    await _add("1|A", port=redirect_proxies(probe_echo))
    await _add("1|B", port=redirect_proxies(probe_echo))
    await _add("1|C", port=_free_port())          # 死端口 → L0 必失败
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    pid_before = proxy_runtime.process.pid
    port_before = proxy_runtime.port

    begin_crawl("test-crawl")                      # crawl 占线
    async with get_session_factory()() as s:
        outcomes = await run_l0_cycle(
            s, data_dir=tmp_data_dir, controller_url=base, secret=secret,
            now=NOW, target_url=f"http://probe.invalid/ip",
        )
        await s.commit()

    assert await _state_of("1|C") == NODE_DEAD, "L0 该改状态（可与 crawl 并行）"
    assert rebuild_pending() is True, "池内容脏了 → 只置信号，不立即重建"
    assert proxy_runtime.process.pid == pid_before, "占线时绝不能重启内核"
    assert proxy_runtime.port == port_before
    assert len(outcomes) == 3
    end_crawl()


# ══ 3. 空闲时消费 pending → rebuild ══════════════════════════════
@pytest.mark.asyncio
async def test_pending_rebuild_consumed_when_idle(
    tmp_data_dir, kernel_exe_path, proxy_runtime
) -> None:
    await init_db()
    await _add("1|A", port=_free_port())
    await _add("1|B", port=_free_port())
    await _add("1|C", port=_free_port())
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    assert await _select(base, secret, "1|B") == "1|B"
    old_mixed = proxy_runtime.port
    old_controller = proxy_runtime.controller_url

    await _set_state("1|C", NODE_DEAD)
    request_rebuild()
    assert rebuild_pending() is True

    async with get_session_factory()() as s:
        result = await run_pending_rebuild(
            s, data_dir=tmp_data_dir, controller_url=base, secret=secret,
            runtime=proxy_runtime, exe_path=str(kernel_exe_path),
        )
        await s.commit()

    assert result is not None
    assert rebuild_pending() is False, "消费过就该清空"
    assert result.mixed_port != old_mixed, "重建后端口必然变化（P1.5 实测）"
    assert result.selection == "1|B", "GLOBAL 必须恢复到重建前的选择"
    assert await _global_now(result.controller_url, secret) == "1|B"
    assert proxy_runtime.controller_url != old_controller


# ══ 4. 占线时 L1/L2 直接跳过 ═════════════════════════════════════
@pytest.mark.asyncio
async def test_maintenance_skips_while_busy(
    tmp_data_dir, kernel_exe_path, proxy_runtime, redirect_proxies, probe_echo
) -> None:
    await init_db()
    await _add("1|A", port=redirect_proxies(probe_echo))
    await _add("1|B", port=redirect_proxies(probe_echo))
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    assert await _select(base, secret, "1|B") == "1|B"

    begin_crawl("test-crawl")
    async with get_session_factory()() as s:
        result = await run_maintenance_cycle(
            s, data_dir=tmp_data_dir, controller_url=base, secret=secret, now=NOW,
            l1_url="http://probe.invalid/ip", l2_url="http://probe.invalid/browse",
        )
        await s.commit()
    end_crawl()

    assert result is None, "占线时是跳过，不是排队"
    assert await _global_now(base, secret) == "1|B", "跳过的维护不得动 GLOBAL"


# ══ 5. L1/L2 是 capture → probe → restore 事务 ═══════════════════
@pytest.mark.asyncio
async def test_maintenance_restores_global_from_current_pool(
    tmp_data_dir, kernel_exe_path, proxy_runtime, redirect_proxies, probe_echo
) -> None:
    await init_db()
    await _add("1|A", port=redirect_proxies(probe_echo))
    await _add("1|B", port=redirect_proxies(probe_echo))
    await _add("1|C", port=redirect_proxies(probe_echo))
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    assert await _select(base, secret, "1|B") == "1|B"

    async with get_session_factory()() as s:
        result = await run_maintenance_cycle(
            s, data_dir=tmp_data_dir, controller_url=base, secret=secret, now=NOW,
            l1_url="http://probe.invalid/ip", l2_url="http://probe.invalid/browse",
        )
        await s.commit()

    assert result is not None
    assert result.previous_selection == "1|B"
    assert result.selection == "1|B", "维护只观察，不得把 crawler 的出口改掉"
    assert await _global_now(base, secret) == "1|B"
    assert result.l1 and result.l2, "L1/L2 确实跑过（它们才是切换 GLOBAL 的人）"

    # 原节点已出池 → 落到当前池第一项（确定性）
    await _set_state("1|B", NODE_DEAD)
    assert await _select(base, secret, "1|B") == "1|B"   # 内核里还在，先选上
    async with get_session_factory()() as s:
        second = await run_maintenance_cycle(
            s, data_dir=tmp_data_dir, controller_url=base, secret=secret, now=NOW,
            l1_url="http://probe.invalid/ip", l2_url="http://probe.invalid/browse",
        )
        await s.commit()
    assert second is not None
    assert second.previous_selection == "1|B"
    assert second.selection == "1|A", "previous 已不在池 → 当前池第一项"
    assert await _global_now(base, secret) == "1|A"


# ══ P1.6-C：接线（cycle / 两个注入点 / fail closed）══════════════
async def _cycle(session, data_dir, base, secret, runtime, exe, **kw):
    from app.domains.proxypool.scheduling import run_proxypool_cycle

    return await run_proxypool_cycle(
        session, data_dir=data_dir, controller_url=base, secret=secret,
        runtime=runtime, exe_path=str(exe), now=NOW,
        l0_target_url=kw.get("l0_target_url"),
        l1_url=kw.get("l1_url"), l2_url=kw.get("l2_url"),
    )


# ── 8. Scheduler 实际调用 proxypool cycle（单 job）────────────────
@pytest.mark.asyncio
async def test_scheduler_job_calls_proxypool_cycle(tmp_data_dir, monkeypatch) -> None:
    from app.core import scheduler as core_scheduler
    from app.domains.proxypool import scheduling as sched

    await init_db()
    await _add("1|A", port=_free_port())
    async with get_session_factory()() as s:
        await build_pool(s, data_dir=tmp_data_dir)
    prepare_runtime_config(tmp_data_dir)      # 前置条件：池 Runtime 配置存在

    calls: list[dict] = []

    async def _fake_cycle(session, **kwargs):
        calls.append(kwargs)
        return sched.ProxypoolCycleResult(
            l0=(), busy=False, rebuilt=None, maintenance=None
        )

    monkeypatch.setattr(sched, "run_proxypool_cycle", _fake_cycle)
    await core_scheduler._job_proxypool_cycle()

    assert calls and calls[0]["data_dir"] == tmp_data_dir


# ── 9/10. 占线：L0 照跑改状态 + pending，但不重建/不 L1/L2 ────────
@pytest.mark.asyncio
async def test_cycle_while_busy_only_runs_l0(
    tmp_data_dir, kernel_exe_path, proxy_runtime, redirect_proxies, probe_echo
) -> None:
    await init_db()
    await _add("1|A", port=redirect_proxies(probe_echo))
    await _add("1|B", port=redirect_proxies(probe_echo))
    await _add("1|C", port=_free_port())
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    pid_before, port_before = proxy_runtime.process.pid, proxy_runtime.port

    begin_crawl("test-crawl")
    async with get_session_factory()() as s:
        result = await _cycle(
            s, tmp_data_dir, base, secret, proxy_runtime, kernel_exe_path,
            l0_target_url="http://probe.invalid/ip",
            l1_url="http://probe.invalid/ip", l2_url="http://probe.invalid/browse",
        )
        await s.commit()
    end_crawl()

    assert await _state_of("1|C") == NODE_DEAD
    assert rebuild_pending() is True
    assert result.busy is True
    assert result.rebuilt is None and result.maintenance is None, "占线时不重建、不 L1/L2"
    assert proxy_runtime.process.pid == pid_before and proxy_runtime.port == port_before


# ── 11/12. 空闲：消费 pending 真重建，之后才 L1/L2 并恢复 GLOBAL ──
@pytest.mark.asyncio
async def test_cycle_when_idle_rebuilds_then_maintains(
    tmp_data_dir, kernel_exe_path, proxy_runtime, redirect_proxies, probe_echo
) -> None:
    await init_db()
    await _add("1|A", port=redirect_proxies(probe_echo))
    await _add("1|B", port=redirect_proxies(probe_echo))
    await _add("1|C", port=_free_port())
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    assert await _select(base, secret, "1|B") == "1|B"
    old_port = proxy_runtime.port

    async with get_session_factory()() as s:
        first = await _cycle(
            s, tmp_data_dir, base, secret, proxy_runtime, kernel_exe_path,
            l0_target_url="http://probe.invalid/ip",
            l1_url="http://probe.invalid/ip", l2_url="http://probe.invalid/browse",
        )
        await s.commit()

    assert first.busy is False
    assert first.rebuilt is not None, "空闲时该消费 pending 真重建"
    assert first.rebuilt.mixed_port != old_port
    assert first.rebuilt.selection == "1|B", "重建必须恢复 GLOBAL"
    assert first.maintenance is not None
    assert first.maintenance.selection == "1|B", "维护结束仍要是 B（只观察）"
    assert await _global_now(first.rebuilt.controller_url, secret) == "1|B"
    assert rebuild_pending() is False


# ── 13/14. 两个 production run 拿到当前 Runtime（每 run 只取一次）──
@pytest.mark.asyncio
async def test_crawl_service_injects_current_runtime_per_run(
    tmp_data_dir, kernel_exe_path, proxy_runtime, monkeypatch
) -> None:
    """run A 全程用端口 X；重建后 run B 用端口 Y——**不是** worker 各自取。"""
    from app.domains.crawl import service as cs

    await init_db()
    await _add("1|A", port=_free_port())
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    port_x = proxy_runtime.port

    captured: list[str | None] = []

    async def _stub_run_crawl(pairs, *, config, stop_event=None, pre_tasks=None):
        captured.append(config.proxy_url)
        return {"total": 1, "processed": 1, "success": 1, "failed": 0}

    async def _regions(regions=None):
        return regions or ["us"]

    monkeypatch.setattr(cs, "run_crawl", _stub_run_crawl)
    monkeypatch.setattr(cs, "effective_regions", _regions)

    await cs.start_job(scope="appids", appids=[220], regions=["us"], kind="manual")
    await cs._active.task
    assert captured[-1] == f"http://127.0.0.1:{port_x}"

    async with get_session_factory()() as s:
        await rebuild_runtime(
            s, data_dir=tmp_data_dir, controller_url=base, secret=secret,
            runtime=proxy_runtime, exe_path=str(kernel_exe_path),
        )
        await s.commit()
    port_y = proxy_runtime.port
    assert port_y != port_x

    await cs.start_job(scope="appids", appids=[220], regions=["us"], kind="manual")
    await cs._active.task
    assert captured[-1] == f"http://127.0.0.1:{port_y}", "新 run 必须用新端口"
    assert len(set(captured)) == 2


# ── 15/16. bundles 注入 + 两条路径 fail closed ───────────────────
@pytest.mark.asyncio
async def test_bundles_path_injects_runtime_and_fails_closed(
    tmp_data_dir, kernel_exe_path, proxy_runtime, monkeypatch
) -> None:
    from app.domains.bundles import refresh as rf
    from app.domains.games.models import Bundle
    from app.domains.proxypool.runtime import RuntimeUnavailableError
    from app.domains.crawl import service as cs

    await init_db()
    await _add("1|A", port=_free_port())
    async with get_session_factory()() as s:
        s.add(Bundle(bundle_id=900001, name="test-bundle", app_ids=[220]))
        await s.commit()

    captured: list[str | None] = []

    async def _stub_run_crawl(pairs, *, config, stop_event=None, pre_tasks=None):
        captured.append(config.proxy_url)
        return {"total": 1, "processed": 1, "success": 1, "failed": 0}

    async def _fake_type(*a, **k):
        return ("game", "Test Game")

    async def _regions(*a, **k):
        return ["us"]

    monkeypatch.setattr(rf, "_fetch_app_type", _fake_type)
    monkeypatch.setattr("app.crawler.runner.run_crawl", _stub_run_crawl)
    monkeypatch.setattr("app.domains.regions.service.enabled_regions", _regions)

    # ① 没有池 Runtime → 拒绝启动（不直连、不退旧订阅代理）
    await rf._enqueue_new_bundle_apps()
    assert captured == [], "拿不到 Runtime 时不得启动 run"

    # ② 有 Runtime → 注入当前地址
    base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
    await rf._enqueue_new_bundle_apps()
    assert captured == [f"http://127.0.0.1:{proxy_runtime.port}"]

    # ③ crawl 域同样 fail closed
    async def _regions2(regions=None):
        return regions or ["us"]

    monkeypatch.setattr(cs, "effective_regions", _regions2)
    monkeypatch.setattr(cs, "run_crawl", _stub_run_crawl)
    proxy_runtime.stop()                       # Runtime 不可用
    with pytest.raises(RuntimeUnavailableError):
        await cs.start_job(scope="appids", appids=[220], regions=["us"])


# ══ 6. 与老订阅 Runtime 真正隔离 ═════════════════════════════════
@pytest.mark.asyncio
async def test_proxypool_rebuild_leaves_subscription_runtime_alone(
    tmp_data_dir, kernel_exe_path, proxy_runtime
) -> None:
    """两个 Runtime 是两套东西：对象、配置、`-d`、controller、mixed-port、生命周期。

    模拟"老订阅内核"：独立实例 + `<data>/clash/config.yaml` + 自己的端口。
    """
    await init_db()
    await _add("1|A", port=_free_port())
    await _add("1|B", port=_free_port())

    # —— 老订阅 Runtime（独立实例，绝不用 clash_manager.runtime）——
    legacy_dir = tmp_data_dir / "clash"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_controller = _free_port()
    legacy_mixed = _free_port()
    (legacy_dir / "config.yaml").write_text(
        f"external-controller: 127.0.0.1:{legacy_controller}\n"
        f"secret: legacy-secret\nmixed-port: {legacy_mixed}\n"
        "mode: rule\n",
        encoding="utf-8",
    )
    legacy = ClashRuntime()
    legacy.start(str(kernel_exe_path), str(legacy_dir / "config.yaml"))
    assert await wait_proxy_names(f"http://127.0.0.1:{legacy_controller}",
                                  "legacy-secret", timeout=20) is not None

    try:
        legacy_pid = legacy.process.pid
        base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
        async with get_session_factory()() as s:
            await rebuild_runtime(
                s, data_dir=tmp_data_dir, controller_url=base, secret=secret,
                runtime=proxy_runtime, exe_path=str(kernel_exe_path),
            )
            await s.commit()

        assert legacy.process.pid == legacy_pid, "池重建不得动老订阅内核进程"
        assert _listening(legacy_controller), "老 controller 仍可访问"
        assert _listening(legacy_mixed), "老 mixed-port 仍可用"
        assert proxy_runtime.process.pid != legacy_pid
        assert proxy_runtime.controller_url != f"http://127.0.0.1:{legacy_controller}"
    finally:
        legacy.stop()


# ══ 7. controller 端口不依赖共享默认值 19090 ════════════════════
@pytest.mark.asyncio
async def test_proxypool_controller_port_is_dedicated(
    tmp_data_dir, kernel_exe_path, proxy_runtime
) -> None:
    await init_db()
    await _add("1|A", port=_free_port())
    async with get_session_factory()() as s:
        await build_pool(s, data_dir=tmp_data_dir)
    config = prepare_runtime_config(tmp_data_dir)
    text = runtime_config_path(tmp_data_dir).read_text(encoding="utf-8")
    doc = __import__("yaml").safe_load(text)

    assert "external-controller" in doc, "运行配置必须自带 controller：不依赖注入的默认值"
    assert doc["external-controller"] != f"127.0.0.1:{DEFAULT_CONTROLLER_PORT}", (
        "19090 是共享默认值——老订阅没有 controller 行时也会拿到它"
    )
    pool_port = int(str(doc["external-controller"]).rsplit(":", 1)[1])
    assert pool_port > 0 and not _listening(pool_port)
    assert doc.get("secret"), "自带 secret，避免与别的实例共用凭据"
    assert config == runtime_config_path(tmp_data_dir)

    # 现实场景：老 Runtime 的配置没有 external-controller → ensure_controller 注入 19090
    legacy_dir = tmp_data_dir / "clash"
    legacy_dir.mkdir(parents=True, exist_ok=True)
    legacy_mixed = _free_port()
    (legacy_dir / "config.yaml").write_text(
        f"mixed-port: {legacy_mixed}\nmode: rule\n", encoding="utf-8"
    )
    legacy = ClashRuntime()
    legacy.start(str(kernel_exe_path), str(legacy_dir / "config.yaml"))
    try:
        assert legacy.controller_url == f"http://127.0.0.1:{DEFAULT_CONTROLLER_PORT}", (
            "前提：没有 controller 行的老配置会拿到 19090"
        )
        base, secret = await _boot(proxy_runtime, kernel_exe_path, tmp_data_dir)
        assert proxy_runtime.controller_url != legacy.controller_url, (
            "两者不能抢同一个 controller 端口（后启动者会绑定失败）"
        )
        assert await _global_now(base, secret) is not None
        assert await _global_now(legacy.controller_url, legacy.secret) is not None
    finally:
        legacy.stop()
