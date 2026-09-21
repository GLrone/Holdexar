"""P1.3-C：`crawl-pool.yaml` → Mihomo，第一次**真实**运行时对账。

本阶段验收语义只有一句，不要写成更宽的话：

    Mihomo 已加载 Registry 中的全部合格节点，且运行时名称与 Registry 一致。

**不**等于「Mihomo 已经能承载爬虫流量」——池文件只有 `proxies:`，内核默认 `rule`
模式且没有策略组，真正把流量导向某个节点是后面的事。

用真内核、真控制器、真 `/proxies`，不 mock runtime。本机没有内核资产时（仓库
不分发 `assets/clash/`）整组**跳过**并说明原因，不伪装成通过。
"""
from __future__ import annotations

import os
import socket
import time
from datetime import datetime
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from app.core.config import get_settings  # noqa: E402
from app.core.database import get_engine, get_session_factory, init_db  # noqa: E402
from app.domains.proxies import clash_manager  # noqa: E402
from app.domains.proxies.clash_manager import ClashRuntime  # noqa: E402
from app.domains.proxies.kernel_release import kernel_filename  # noqa: E402
from app.domains.proxypool.models import (  # noqa: E402
    ProxyNode, ProxyNodeSource, node_fingerprint,
)
from app.domains.proxypool.pool import build_pool, pool_path  # noqa: E402
from app.domains.proxypool.runtime import (  # noqa: E402
    RuntimeUnreachableError,
    align_lanes,
    lane_proxy_urls,
    mixed_port_of,
    prepare_runtime_config,
    reconcile,
    runtime_config_path,
    wait_lane_ports,
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


def _kernel_candidates(data_dir: Path):
    """内核可执行文件的候选位置（仓库不分发大二进制，只能就地找）。"""
    yield clash_manager.kernel_exe(data_dir)
    yield clash_manager.bundled_clash_dir() / kernel_filename()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        yield from sorted(Path(local).glob(f"holdexar*/clash/{kernel_filename()}"))


@pytest.fixture
def kernel_exe_path(tmp_data_dir) -> Path:
    for candidate in _kernel_candidates(tmp_data_dir):
        if candidate.is_file():
            return candidate
    pytest.skip("本机没有可用的 mihomo 内核资产（assets/clash/ 不随仓库分发）")


@pytest.fixture
def clash_runtime():
    """本测试自己的运行时实例——不碰应用那个模块级单例。"""
    runtime = ClashRuntime()
    yield runtime
    runtime.stop()


async def _add(state: str, runtime_name: str, *, server: str) -> None:
    config = {
        "name": runtime_name.split("|", 1)[-1],
        "type": "ss",
        "server": server,
        "port": 1,
        "cipher": "aes-128-gcm",
        "password": "x",
    }
    fp = node_fingerprint(config)
    async with get_session_factory()() as s:
        s.add(ProxyNode(
            node_id=fp, fingerprint=fp, runtime_name=runtime_name,
            proxy_type="ss", server=server, normalized_config=config, state=state,
            first_seen=NOW, last_seen=NOW, last_source_seen=NOW,
        ))
        # 合格集口径含「至少一个当前来源」：直接登记的行也要给一条来源
        s.add(ProxyNodeSource(
            node_id=fp, subscription_id=1, original_name=config["name"],
            first_seen=NOW, last_seen=NOW,
        ))
        await s.commit()


async def _registry_names() -> set[str]:
    """独立算一遍「Registry 里的合格节点名」——不复用 build_pool 的选择结果。"""
    async with get_session_factory()() as s:
        rows = await s.execute(
            select(ProxyNode).where(
                ProxyNode.state.in_([NODE_NEW, NODE_ACTIVE, NODE_STALE])
            )
        )
        return {row.runtime_name for row in rows.scalars()}


async def _build(data_dir: Path):
    async with get_session_factory()() as s:
        return await build_pool(s, data_dir=data_dir)


async def _start_and_read(runtime: ClashRuntime, exe: Path, config: Path,
                          *, timeout: float = 15.0) -> frozenset[str]:
    status = runtime.start(str(exe), str(config))
    assert status["controllerUrl"], "内核启动后必须注入/解析出控制器地址"
    return await wait_proxy_names(
        status["controllerUrl"], runtime.secret, timeout=timeout
    )


# ── 1. 真启动 + 真加载 + 三方集合一致 ────────────────────────────
@pytest.mark.asyncio
async def test_kernel_loads_exactly_the_pool_nodes(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    await init_db()
    for state, server in (
        (NODE_NEW, "n1.example.net"),
        (NODE_ACTIVE, "n2.example.net"),
        (NODE_STALE, "n3.example.net"),
    ):
        await _add(state, f"1|{server}", server=server)

    build = await _build(tmp_data_dir)
    registry_names = await _registry_names()
    assert registry_names == set(build.runtime_names), "池写入前：Registry 与池必须同集"

    observed = await _start_and_read(
        clash_runtime, kernel_exe_path, prepare_runtime_config(tmp_data_dir)
    )
    ledger = reconcile(
        registry_names=registry_names,
        pool_names=set(build.runtime_names),
        observed_names=observed,
    )

    assert ledger.ok, f"对账失败：缺 {sorted(ledger.missing)}，多 {sorted(ledger.unexpected)}"
    assert ledger.runtime_names == registry_names, "运行时名称集合必须与 Registry 完全一致"
    assert len(ledger.runtime_names) == build.written_count == build.expected_count
    assert ledger.observed_total > len(ledger.runtime_names), (
        "/proxies 里还有内核内置逻辑节点，M 只能按 Registry 的名字空间取，不能数整个 JSON"
    )


# ── 2. DEAD / RETIRED 不得凭空出现在内核里 ───────────────────────
@pytest.mark.asyncio
async def test_dead_and_retired_never_reach_the_kernel(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    await init_db()
    for state, server in (
        (NODE_NEW, "n1.example.net"),
        (NODE_ACTIVE, "n2.example.net"),
        (NODE_STALE, "n3.example.net"),
        (NODE_DEAD, "dead.example.net"),
        (NODE_RETIRED, "retired.example.net"),
    ):
        await _add(state, f"1|{server}", server=server)

    build = await _build(tmp_data_dir)
    assert build.written_count == 3, "DEAD / RETIRED 不该进池"

    observed = await _start_and_read(
        clash_runtime, kernel_exe_path, prepare_runtime_config(tmp_data_dir)
    )
    assert "1|dead.example.net" not in observed
    assert "1|retired.example.net" not in observed
    assert {n for n in observed if n.startswith("1|")} == {
        "1|n1.example.net", "1|n2.example.net", "1|n3.example.net",
    }


# ── 3. 对账函数：差额必须被记出来，而不是静默通过 ─────────────────
def test_reconcile_reports_missing_and_ignores_builtins() -> None:
    """内核少加载一个节点时必须记成差额。

    注意：这是对**比较逻辑**的单元测试，不是假 runtime——真 runtime 在前两条里。
    之所以要单独测，是因为真实内核在这种情况下不会"少加载"，而是直接 fatal
    （见第 4 条），所以差额只能在这一层构造出来。
    """
    ledger = reconcile(
        registry_names={"1|A", "1|B", "1|C"},
        pool_names={"1|A", "1|B", "1|C"},
        observed_names={"1|A", "1|B", "DIRECT", "GLOBAL", "REJECT"},
    )

    assert not ledger.ok
    assert ledger.missing == {"1|C"}
    assert ledger.runtime_names == {"1|A", "1|B"}
    assert ledger.unexpected == frozenset(), (
        "内置逻辑节点（DIRECT/GLOBAL/...）不是「多出来的节点」"
    )
    assert ledger.observed_total == 5


# ── 4. 真实差额成因：池里有一条内核不认的节点 ────────────────────
@pytest.mark.asyncio
async def test_unloadable_node_makes_the_kernel_die_not_shrink(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    """真实观测到的成因：内核**不会**静默少加载，而是直接拒绝启动。

    实测日志：`level=fatal msg="Parse config error: proxy 3: unsupport proxy type: ..."`
    所以「M < N」在本版内核里不可达，它的真身是「内核根本没起来」。这条断言把
    这个区别钉住：将来设计 reconcile 时必须先区分「没起来」与「起来了但不一样」。
    """
    await init_db()
    for state, server in ((NODE_ACTIVE, "n1.example.net"), (NODE_ACTIVE, "n2.example.net")):
        await _add(state, f"1|{server}", server=server)
    await _build(tmp_data_dir)

    config = pool_path(tmp_data_dir)
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))
    doc["proxies"].append({
        "name": "9|内核不认的协议", "type": "bogus-proto-not-real",
        "server": "127.0.0.1", "port": 1,
    })
    config.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False),
                      encoding="utf-8")

    with pytest.raises(RuntimeUnreachableError, match="不可用"):
        await _start_and_read(
            clash_runtime, kernel_exe_path, prepare_runtime_config(tmp_data_dir),
            timeout=8.0,
        )


# ── 5. 池文件是只读产物，内核只吃由它生成的运行配置 ───────────────
@pytest.mark.asyncio
async def test_pool_file_is_never_touched_by_the_kernel(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    """`ClashRuntime.start()` 会把 `external-controller` / `secret` **写回它收到
    的文件**。把池文件直接交给它，池就不再等于 `build_pool` 校验过的产物，而且
    下一次 `build_pool` 会把注入抹掉——P1.4 的健康检测要靠控制器，这个矛盾必须
    在这里断开：池 = 纯运行集（只读），运行配置 = 临时可重建的内核启动文件。
    """
    await init_db()
    await _add(NODE_ACTIVE, "1|香港01", server="hk1.example.net")
    await _build(tmp_data_dir)

    pool = pool_path(tmp_data_dir)
    pool_bytes = pool.read_bytes()

    config = prepare_runtime_config(tmp_data_dir)
    assert config == runtime_config_path(tmp_data_dir)
    assert config != pool
    assert [p["name"] for p in
            yaml.safe_load(config.read_text(encoding="utf-8"))["proxies"]] == ["1|香港01"]

    await _start_and_read(clash_runtime, kernel_exe_path, config)

    assert pool.read_bytes() == pool_bytes, "内核启动不得改动池文件一个字节"
    text = config.read_text(encoding="utf-8")
    assert "external-controller" in text, "控制器该由运行配置承载"
    assert "secret" in text


# ── 6. 运行期专属键只进运行配置，且 mixed-port 真被监听 ──────────
@pytest.mark.asyncio
async def test_runtime_config_carries_runtime_only_keys(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    """`mode` / `mixed-port` / `bind-address` / `allow-lan` 属于运行配置这一层。

    这条顺带拦一个结构性倒退：以后有人为了 L1 把运行参数直接塞回池文件。
    """
    await init_db()
    await _add(NODE_ACTIVE, "1|香港01", server="hk1.example.net")
    await _build(tmp_data_dir)
    pool_bytes = pool_path(tmp_data_dir).read_bytes()

    config = prepare_runtime_config(tmp_data_dir)
    doc = yaml.safe_load(config.read_text(encoding="utf-8"))

    assert doc["mode"] == "global", "L1 靠 GLOBAL 选择器切节点"
    assert doc["allow-lan"] is False, "测出口 IP 不该把本机代理入口开给局域网"
    assert doc["bind-address"] == "127.0.0.1"
    port = doc["mixed-port"]
    assert isinstance(port, int) and port > 0, "必须是动态分配的真实端口，不是 0"
    bind_probe = socket.socket()
    bind_probe.bind(("127.0.0.1", port))
    bind_probe.close()
    assert pool_path(tmp_data_dir).read_bytes() == pool_bytes, (
        "运行期键不得回流进池文件——池只放 Registry 的运行集"
    )

    status = clash_runtime.start(str(kernel_exe_path), str(config))
    await wait_proxy_names(status["controllerUrl"], clash_runtime.secret, timeout=15)

    listening = False
    for _ in range(40):
        with socket.socket() as probe_sock:
            probe_sock.settimeout(0.3)
            if probe_sock.connect_ex(("127.0.0.1", port)) == 0:
                listening = True
                break
        time.sleep(0.25)
    assert listening, f"内核没有监听运行配置里的 mixed-port {port}"


# ── 多入口（lane）：一个内核进程内开 N 个可独立选路的 listener ──────
@pytest.mark.asyncio
async def test_kernel_opens_one_listener_per_lane_and_selects_independently(
    tmp_data_dir, kernel_exe_path, clash_runtime
) -> None:
    """真内核验收：每条 lane 一个入口，逐条设置选择互不影响，GLOBAL 入口并存。

    这条覆盖的是「一个内核 = 多个受控出口工位」的结构本身：入口是否真的开出来
    （`wait_lane_ports`）、lane 组是否可被控制器逐条选路并回读（`assign_lanes`）、
    lane 组是否被对账正确识别为运行期条目（不是池外多余节点）。
    """
    await init_db()
    names = []
    for i in range(3):
        runtime_name = f"1|n{i}.example.net"
        names.append(runtime_name)
        await _add(NODE_ACTIVE, runtime_name, server=f"n{i}.example.net")

    build = await _build(tmp_data_dir)
    assert list(build.runtime_names) == names

    status = clash_runtime.start(str(kernel_exe_path), str(prepare_runtime_config(tmp_data_dir)))
    base = status["controllerUrl"]
    await wait_proxy_names(base, clash_runtime.secret, timeout=15)

    ports = await wait_lane_ports(tmp_data_dir, timeout=15)
    assert len(ports) == 3, "一个节点一条 lane"
    assert lane_proxy_urls(tmp_data_dir) == [f"http://127.0.0.1:{p}" for p in ports]

    # 走生产入口（align_lanes）：首启没有上次绑定，也必须把每条 lane 都绑上不同节点
    aligned_ports, applied = await align_lanes(
        data_dir=tmp_data_dir, controller_url=base, secret=clash_runtime.secret,
        pool_names=build.runtime_names, previous=(),
    )
    assert tuple(aligned_ports) == ports
    assert applied == names, "首启即须逐条绑定；留空会让所有 lane 走组内默认项"
    assert len(set(applied)) == 3, "同一节点不得占两条 lane"

    observed = await wait_proxy_names(base, clash_runtime.secret, timeout=15)
    ledger = reconcile(
        registry_names=set(build.runtime_names),
        pool_names=set(build.runtime_names),
        observed_names=observed,
    )
    assert ledger.ok, f"lane 组不该被当成池外节点：多 {sorted(ledger.unexpected)}"
    assert "lane-0" in observed and "lane-2" in observed

    assert mixed_port_of(tmp_data_dir) > 0, "GLOBAL 入口仍并存，维护路径不受影响"