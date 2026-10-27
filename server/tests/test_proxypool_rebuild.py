"""P1.5：池重建 + Runtime 恢复的编排契约（4 条）。

契约（每一步都必须真实发生）。生效通道两段式，**先热后冷**：

    capture previous GLOBAL
      → build_pool() → 选出口槽
      → 热通道（控制器可达）：钉定端点生成新配置 → PUT /configs → wait
        + reconcile → restore GLOBAL → 重绑 lane —— 进程不动、端口不变
      → 热通道未通过 → 冷通道：prepare（全新端口）→ 显式 stop() → 显式 start()
        → wait controller + reconcile → restore GLOBAL → 重绑 lane

四条约束：
1. 原节点仍在池中 → 恢复它，不做无意义切换；
2. 原节点已出池 → 内核自己会落到 `DIRECT`，编排层必须 PUT 新池第一项；
3. 冷通道（进程重启）：旧入口端口随进程消亡（新连接被拒），新端口立即可用，
   GLOBAL 恢复——用**不可达的控制器**强制走冷通道来钉住这条路径；
4. 热通道：内核进程不变、mixed-port 沿用、新池节点真的进核（`hot_applied=True`）。

不测（本批边界）：失败 1/2/3 次阈值、运行中请求迁移、crawler 自动重选 GLOBAL、
Generation/rollback、worker lease、健康排序、空转闸（归调度层测试）。
"""
from __future__ import annotations

import threading
import socket
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from proxy_helpers import RedirectProxy as _RedirectProxy
from sqlalchemy import select, update

from app.core.database import get_session_factory, init_db  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode, node_fingerprint,
)
from app.domains.proxypool.pool import build_pool  # noqa: E402
from app.domains.proxypool.runtime import (  # noqa: E402
    prepare_runtime_config, rebuild_runtime, wait_mixed_port, wait_proxy_names,
)
from app.domains.proxypool.state import NODE_ACTIVE, NODE_DEAD  # noqa: E402

NOW = datetime(2026, 9, 19, 12, 0, 0)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ── 本地夹具：CONNECT 跳板（mihomo 的 http 节点出站走 CONNECT 隧道）──


class _Echo(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(204)
        self.end_headers()

    def log_message(self, *args):
        pass


# ── fixtures ─────────────────────────────────────────────────────


@pytest.fixture
def clash_runtime():
    runtime = ClashRuntime()
    yield runtime
    runtime.stop()


# ── 数据与编排辅助 ───────────────────────────────────────────────
async def _set_exit_ip(runtime_name: str, exit_ip: str) -> None:
    """模拟机场轮换出口：同一节点的出口 IP 换成新值。"""
    from sqlalchemy import select as _select

    async with get_session_factory()() as s:
        node = (await s.execute(
            _select(ProxyNode).where(ProxyNode.runtime_name == runtime_name)
        )).scalar_one()
        node.exit_ip = exit_ip
        await s.commit()


async def _ensure_subscription() -> None:
    """合格集口径要求节点归属一条在册订阅：先保证 id=1 的订阅行存在。"""
    from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription

    async with get_session_factory()() as s:
        if await s.get(ProxySubscription, 1) is None:
            s.add(ProxySubscription(id=1, kind="plain", url="http://sub.example/list",
                                    admission_status=ADMISSION_ACTIVE))
            await s.commit()


async def _add(runtime_name: str, *, port: int, state: str = NODE_ACTIVE) -> None:
    await _ensure_subscription()
    config = {"name": runtime_name.split("|", 1)[-1], "type": "http",
              "server": "127.0.0.1", "port": port}
    fp = node_fingerprint(config)
    async with get_session_factory()() as s:
        s.add(ProxyNode(node_id=fp, runtime_name=runtime_name, proxy_type='http', server='127.0.0.1', normalized_config=config, state=state, first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
        await s.execute(update(ProxyNode)
            .where(ProxyNode.node_id == fp)
            .values(subscription_id=1, original_name="x"))
        await s.commit()


async def _set_state(runtime_name: str, state: str) -> None:
    async with get_session_factory()() as s:
        row = (await s.execute(
            select(ProxyNode).where(ProxyNode.runtime_name == runtime_name)
        )).scalar_one()
        row.state = state
        await s.commit()


async def _build(data_dir: Path):
    async with get_session_factory()() as s:
        return await build_pool(s, data_dir=data_dir)


async def _boot(runtime: ClashRuntime, exe: Path, data_dir: Path):
    """首次启动（等价于探针里的初始态）。"""
    await _build(data_dir)
    status = runtime.start(str(exe), str(prepare_runtime_config(data_dir)), inject_lanes=False)
    await wait_proxy_names(status["controllerUrl"], runtime.secret, timeout=20)
    await wait_mixed_port(data_dir, timeout=20)
    return status["controllerUrl"], runtime.secret


async def _rebuild(runtime: ClashRuntime, exe: Path, data_dir: Path,
                   base: str, secret: str):
    async with get_session_factory()() as s:
        result = await rebuild_runtime(
            s, data_dir=data_dir, controller_url=base, secret=secret,
            runtime=runtime, exe_path=str(exe),
        )
        await s.commit()
        return result


async def _select(base: str, secret: str, name: str) -> str | None:
    async with httpx.AsyncClient(
        timeout=10, headers={"Authorization": f"Bearer {secret}"}
    ) as c:
        await c.put(f"{base}/proxies/GLOBAL", json={"name": name})
        return (await c.get(f"{base}/proxies/GLOBAL")).json().get("now")


async def _global_now(base: str, secret: str) -> str | None:
    async with httpx.AsyncClient(
        timeout=10, headers={"Authorization": f"Bearer {secret}"}
    ) as c:
        return (await c.get(f"{base}/proxies/GLOBAL")).json().get("now")


async def _get_through(port: int, url: str, *, timeout: float) -> httpx.Response:
    async with httpx.AsyncClient(proxy=f"http://127.0.0.1:{port}",
                                 timeout=timeout) as client:
        return await client.get(url)


# ── 1. 原节点仍在池中 → 恢复它，不无意义切换 ─────────────────────
@pytest.mark.asyncio
async def test_rebuild_keeps_selection_when_node_survives(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    await init_db()
    await _add("1|A", port=_closed_port())
    await _add("1|B", port=_closed_port())
    base, secret = await _boot(clash_runtime, kernel_exe_path, tmp_data_dir)
    assert await _select(base, secret, "1|B") == "1|B"

    result = await _rebuild(clash_runtime, kernel_exe_path, tmp_data_dir, base, secret)

    assert result.previous_selection == "1|B"
    assert result.pool_names == ("1|A", "1|B")
    assert result.selection == "1|B", "原节点仍在池里，重建不该把它换掉"
    assert await _global_now(result.controller_url, secret) == "1|B"
    assert result.mixed_port > 0
    assert result.proxy_url == f"http://127.0.0.1:{result.mixed_port}"


# ── 2. 原节点已出池 → 编排层必须 PUT 新池第一项 ──────────────────
@pytest.mark.asyncio
async def test_rebuild_falls_back_when_selected_node_left_pool(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    await init_db()
    await _add("1|A", port=_closed_port())
    await _add("1|B", port=_closed_port())
    await _add("1|C", port=_closed_port())
    base, secret = await _boot(clash_runtime, kernel_exe_path, tmp_data_dir)
    assert await _select(base, secret, "1|C") == "1|C"

    # C 的死因归 L0（已在 P1.4-A 覆盖）；这里只关心"它出池之后"的恢复行为
    await _set_state("1|C", NODE_DEAD)

    result = await _rebuild(clash_runtime, kernel_exe_path, tmp_data_dir, base, secret)

    assert result.previous_selection == "1|C"
    assert result.pool_names == ("1|A", "1|B")
    assert result.selection == "1|A", "原节点失效时必须落到新池第一项（确定性，不排序）"
    assert await _global_now(result.controller_url, secret) == "1|A", (
        "不恢复的话内核会停在 DIRECT —— 业务会绕过代理池"
    )


# ── 3. 冷通道：旧入口随进程消亡，新入口立即可用 ──────────────────
@pytest.mark.asyncio
async def test_cold_rebuild_replaces_endpoints(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Echo)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    redirect = _RedirectProxy(server.server_address[1])
    try:
        await init_db()
        await _add("1|A", port=redirect.port)
        base, secret = await _boot(clash_runtime, kernel_exe_path, tmp_data_dir)
        assert await _select(base, secret, "1|A") == "1|A"
        old_port = clash_runtime.port

        # 控制器指向一个必然不可达的端口 → 热通道前置探测失败 → 强制走冷通道
        result = await _rebuild(clash_runtime, kernel_exe_path, tmp_data_dir,
                                "http://127.0.0.1:1", secret)
        assert result.hot_applied is False, "控制器不可达必须走冷通道（进程重启）"

        assert result.mixed_port != old_port, "冷通道以全新端口重启内核"
        with pytest.raises(httpx.HTTPError):
            await _get_through(old_port, "http://probe.invalid/x", timeout=5)
        fresh = await _get_through(result.mixed_port, "http://probe.invalid/x",
                                   timeout=20)
        assert fresh.status_code == 204, "重建后新端口必须立即可用"
        assert clash_runtime.secret != secret
        assert await _global_now(result.controller_url, clash_runtime.secret) == "1|A"
        async with httpx.AsyncClient(
            trust_env=False, headers={"Authorization": f"Bearer {secret}"},
        ) as client:
            assert (await client.get(f"{result.controller_url}/version")).status_code == 401
    finally:
        server.shutdown()


# ── 4. 热通道：进程不动、端口沿用、新池真的进核 ──────────────────
@pytest.mark.asyncio
async def test_hot_rebuild_keeps_process_and_port(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    await init_db()
    await _add("1|A", port=_closed_port())
    await _add("1|B", port=_closed_port())
    base, secret = await _boot(clash_runtime, kernel_exe_path, tmp_data_dir)
    assert await _select(base, secret, "1|B") == "1|B"
    pid_before = clash_runtime.process.pid
    port_before = clash_runtime.port

    # 池扩容一个节点：重建的唯一差异是池内容，热通道应把它加载进现役进程
    await _add("1|C", port=_closed_port())
    result = await _rebuild(clash_runtime, kernel_exe_path, tmp_data_dir, base, secret)

    assert result.hot_applied is True, "控制器可达时必须走热通道"
    assert clash_runtime.process.pid == pid_before, "热通道不得重启内核进程"
    assert result.mixed_port == port_before, "热通道沿用现役 mixed-port，入口 URL 不变"
    assert result.controller_url == base, "热通道沿用现役 controller"
    assert result.pool_names == ("1|A", "1|B", "1|C"), "新池节点必须真的进核"
    assert result.selection == "1|B", "GLOBAL 恢复契约与冷通道一致"
    assert await _global_now(result.controller_url, secret) == "1|B"

# ── 5. 出口轮换漂移：crawl_lane_plan 启动时就地收敛，进程不动 ────
@pytest.mark.asyncio
async def test_lane_plan_drift_recovers_inline(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    """机场轮换出口 → lane 计划与出口快照失配 → 取值口就地热收敛
    （重写计划、对齐 lane），本次调用直接拿到一致的入口，进程不动。"""
    from app.domains.proxypool import runtime as rt

    await init_db()
    await _add("1|A", port=_closed_port())
    base, secret = await _boot(clash_runtime, kernel_exe_path, tmp_data_dir)
    await _set_exit_ip("1|A", "1.1.1.1")
    await _build(tmp_data_dir)
    async with get_session_factory()() as s:
        slots = await rt.exit_slot_snapshot(s)
        names = tuple(n.runtime_name for n in await rt.eligible_nodes(s))
        await rt.align_lanes(
            data_dir=tmp_data_dir, controller_url=base, secret=secret,
            pool_names=list(names), slots=slots, timeout=20,
        )
    pid_before = clash_runtime.process.pid

    # 机场轮换：同一节点出口 IP 换新 → lane 计划立刻失配
    await _set_exit_ip("1|A", "2.2.2.2")
    async with get_session_factory()() as s:
        plan = await rt.crawl_lane_plan(
            s, data_dir=tmp_data_dir, runtime=clash_runtime,
        )
        await s.commit()

    assert list(plan["exit_keys"]) == ["2.2.2.2"], "收敛后必须拿到轮换后的新出口"
    assert clash_runtime.process.pid == pid_before, "收敛走热通道，进程不动"
