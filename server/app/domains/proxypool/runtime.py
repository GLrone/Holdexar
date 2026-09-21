"""P1.3-C：池文件 → 内核运行时的**事实读取**与对账。

这一层只做三件事，且刻意只做三件：

1. **由池生成内核启动配置**（`prepare_runtime_config`）：`crawl-pool.yaml` 是只读
   产物，内核实际启动用的另存为 `crawl-runtime.yaml`。
2. 读内核运行时事实：`GET {controller}/proxies`，取出内核当前装载的代理名。
3. 与 Registry / 池文件对账：**按名字空间比集合**，把差额记出来。

它不启动内核、不做发布与回滚——内核由既有的 `ClashRuntime` 负责
（`clash_manager`），本模块只消费 `start()` 之后暴露的控制器地址与密钥。

**M 只能按 Registry 的名字空间取，不能数整个 JSON**：`/proxies` 里还混着内核内置
的逻辑节点（实测本版为 DIRECT / REJECT / REJECT-DROP / GLOBAL / COMPATIBLE /
PASS / PASS-RULE 共 7 个），数总数会把 M 虚高。

关于「内核少加载了几个节点」：实测本版内核对**重复名**与**不认的协议**都是
`level=fatal` 直接拒绝启动，而不是静默跳过。所以真实世界里 `M < N` 的常见真身是
「内核根本没起来」——`wait_proxy_names` 因此把「不可达」单独抛成
`RuntimeUnreachableError`，与「起来了但集合不同」严格区分。
"""
from __future__ import annotations

import asyncio
import socket
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx
import yaml
from sqlalchemy.ext.asyncio import AsyncSession

from app.domains.proxypool import events
from app.domains.proxypool.exits import MAX_CRAWL_WORKERS, ExitSlot, select_exit_slots
from app.domains.proxypool.pool import (
    PoolBuildError,
    build_pool,
    eligible_nodes,
    pool_path,
)

# 内核内置的逻辑节点（不是池里的节点）。仅用于把 /proxies 里的「多出来的键」
# 解释清楚；它们随内核版本可能变，所以**不**作为硬门禁参与 ok 判定。
BUILTIN_PROXY_NAMES = frozenset({
    "DIRECT", "REJECT", "REJECT-DROP", "GLOBAL", "COMPATIBLE", "PASS", "PASS-RULE",
})

DEFAULT_WAIT_TIMEOUT = 15.0


class RuntimeUnreachableError(RuntimeError):
    """控制器在超时内不可用——通常意味着内核**没有起来**（配置解析 fatal）。"""


class RuntimeUnavailableError(RuntimeError):
    """拿不到可用的池 Runtime 代理地址。

    **Fail closed**：受管爬取在这种情况下必须拒绝启动，绝不静默退回直连或旧订阅
    代理——否则"池坏了"会伪装成"爬取成功"，而且两套 Runtime 的隔离会被悄悄破坏。
    """


RUNTIME_CONFIG_FILENAME = "crawl-runtime.yaml"
# lane 计划：本次 run 的「lane → 出口 IP → 节点」对照表，作为归因与诊断的事实来源。
# 运行配置里只有组与 listener；出口 IP 不在内核配置里，必须自己留一份。
LANE_PLAN_FILENAME = "crawl-lanes.yaml"

# 运行期专属键（只进运行配置，绝不回流进池文件）
RUNTIME_MODE = "global"          # GLOBAL 是维护入口；生产流量走 lane，不经过它
RUNTIME_BIND_ADDRESS = "127.0.0.1"  # 默认是 '*'：测出口 IP 不该把本机入口开给局域网
RUNTIME_CONTROLLER_HOST = "127.0.0.1"
# 池 Runtime 自己的 controller 凭据（本机回环专用）。刻意**不随每次重建轮换**：
# 轮换只会制造"拿旧凭据访问新实例"的陷阱，而 controller 的隔离靠端口就够。
RUNTIME_CONTROLLER_SECRET = "holdexar-proxypool"

# ── lane：一个内核进程内互不干扰的固定出口工位 ──────────────────
# 结构 = 一个 `select` 组（成员是池内全部节点）+ 一个 mixed listener，listener 的
# `proxy` 指向该组。组的当前选择由控制器逐条设置，因此切一条 lane 不改变其它 lane。
#
# GLOBAL 与 lane 的分工：GLOBAL 是维护入口（健康检查、人工查看、旧消费者），
# lane 是生产入口。crawler 只拿 lane 地址，不碰任何选择器。
LANE_GROUP_PREFIX = "lane-"
# lane 数的上限 = 生产 worker 上限（`exits.MAX_CRAWL_WORKERS`）：工位数与出口槽数同源，
# 两处取不同数值会出现「选得出 60 个出口，却只开得出 32 个工位」这种半截容量。
MAX_LANES = MAX_CRAWL_WORKERS


class RuntimeConfigError(RuntimeError):
    """运行配置不可用（例如缺 mixed-port）。"""


def runtime_config_path(data_dir: Path) -> Path:
    """内核启动配置的路径（与只读的池文件同目录、不同文件）。"""
    return Path(data_dir) / "proxypool" / RUNTIME_CONFIG_FILENAME


def lane_plan_path(data_dir: Path) -> Path:
    """lane 计划的路径（运行配置的旁挂产物，内核不读它）。"""
    return Path(data_dir) / "proxypool" / LANE_PLAN_FILENAME


def write_lane_plan(data_dir: Path, plan: Sequence[Mapping]) -> Path:
    """原子写 lane 计划（先 `.tmp` 再 replace，避免半截文件）。"""
    out = lane_plan_path(data_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        {"lanes": [dict(entry) for entry in plan]}, allow_unicode=True, sort_keys=False
    )
    tmp = out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    return out


def read_lane_plan(data_dir: Path) -> list[dict]:
    """读 lane 计划；缺失或形态不对返回空列表（读不到就是读不到，不是异常）。"""
    try:
        doc = yaml.safe_load(lane_plan_path(data_dir).read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return []
    entries = doc.get("lanes") if isinstance(doc, Mapping) else None
    return [dict(e) for e in entries if isinstance(e, Mapping)] if isinstance(entries, list) else []


def exit_ip_for_lane(data_dir: Path, index: int) -> str | None:
    """第 `index` 条 lane 绑定的出口 IP（未记录 → None）。"""
    for entry in read_lane_plan(data_dir):
        try:
            if int(entry.get("lane", -1)) == int(index):
                value = entry.get("exitIp")
                return str(value) if value else None
        except (TypeError, ValueError):
            continue
    return None


def _free_local_port() -> int:
    """让系统分配一个空闲的本机 TCP 端口（L1 的代理入口）。

    首版刻意**不做端口租约**：真撞上时内核会暴露失败（日志里不会出现
    「Mixed proxy listening」），到真实使用中出现证据再决定要不要做端口管理。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def mixed_port_of(data_dir: Path) -> int:
    """读运行配置里的 mixed-port——GLOBAL 入口，维护路径经它发真实请求。"""
    path = runtime_config_path(data_dir)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    port = doc.get("mixed-port") if isinstance(doc, Mapping) else None
    if not isinstance(port, int) or port <= 0:
        raise RuntimeConfigError(f"运行配置里没有可用的 mixed-port：{path}")
    return port


def runtime_lane_ports(data_dir: Path) -> tuple[int, ...]:
    """读运行配置里各 lane 的 listener 端口（按 lane 序号）。

    端口读的是**配置**，不代表在监听——就绪判定见 `lane_proxy_urls`。
    """
    path = runtime_config_path(data_dir)
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return ()
    listeners = doc.get("listeners") if isinstance(doc, Mapping) else None
    if not isinstance(listeners, list):
        return ()
    by_index: dict[int, int] = {}
    for entry in listeners:
        if not isinstance(entry, Mapping):
            continue
        name = str(entry.get("name") or "")
        port = entry.get("port")
        if not name.startswith(LANE_GROUP_PREFIX) or not isinstance(port, int) or port <= 0:
            continue
        tail = name[len(LANE_GROUP_PREFIX):].split("-", 1)[0]
        if tail.isdigit():
            by_index[int(tail)] = port
    return tuple(by_index[i] for i in sorted(by_index))


def _port_listening(port: int, timeout: float = 0.3) -> bool:
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def lane_proxy_urls(data_dir: Path) -> list[str]:
    """生产入口：每条 lane 一个本机代理地址（按 lane 序号）。

    **要么全给，要么不给**：worker 与 lane 的绑定按序号，部分就绪时把地址交出去会让
    绑定整体错位（worker-3 落到另一条 lane 上），因此只要有任一 lane 入口没在听就返回
    空列表，由调用方按「Runtime 不可用」处理（fail closed）。
    """
    ports = runtime_lane_ports(data_dir)
    if not ports:
        return []
    for port in ports:
        if not _port_listening(port):
            return []
    return [f"http://127.0.0.1:{port}" for port in ports]


async def wait_lane_ports(
    data_dir: Path, *, timeout: float = DEFAULT_WAIT_TIMEOUT
) -> tuple[int, ...]:
    """等到**全部** lane 入口都在监听，返回端口元组。

    与 `wait_mixed_port` 同一理由（控制器就绪 ≠ 入口就绪），只是入口从一个变多个：
    入口没起就把 URL 交给 crawler，每个 worker 都会立刻 `ConnectError`。
    """
    ports = runtime_lane_ports(data_dir)
    if not ports:
        raise RuntimeConfigError(f"运行配置里没有任何 lane listener：{runtime_config_path(data_dir)}")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if all(_port_listening(port) for port in ports):
            return ports
        await asyncio.sleep(0.05)
    raise RuntimeUnreachableError(
        f"lane 入口 {ports} 在 {timeout}s 内没有全部开始监听（控制器起来了但入口没起）"
    )


def lane_group_name(index: int) -> str:
    return f"{LANE_GROUP_PREFIX}{index}"


def is_lane_group(name: str) -> bool:
    """名字是否为运行配置生成的 lane 组（`lane-<序号>`）。"""
    if not name.startswith(LANE_GROUP_PREFIX):
        return False
    tail = name[len(LANE_GROUP_PREFIX):]
    return tail.isdigit()


def lane_count_for(pool_size: int, requested: int | None = None) -> int:
    """由池规模决定开几条 lane：一个节点最多占一条工位，上限 `MAX_LANES`。

    池为空返回 0——此时连 GLOBAL 入口都不该被当作可用出口（无节点必然回落 DIRECT）。
    """
    if pool_size <= 0:
        return 0
    n = pool_size if requested is None else requested
    return max(0, min(MAX_LANES, pool_size, int(n)))


def prepare_runtime_config(data_dir: Path, *, lanes: int | None = None) -> Path:
    """由 `crawl-pool.yaml` 生成 `crawl-runtime.yaml`——内核实际启动用的文件。

    为什么必须分文件：`ClashRuntime.start()` 会把 `external-controller` / `secret`
    **写回它收到的那个文件**。若直接启动池文件，池就不再等于 `build_pool` 校验过的
    产物，而且下一次 `build_pool` 一覆盖就把控制器注入抹掉——而健康检测
    正依赖控制器，这个矛盾不能带进健康模块。

    职责：
    - `crawl-pool.yaml` = Registry 的纯运行集，**只读产物**，内核对它零写入；
    - `crawl-runtime.yaml` = 临时、可重建的内核启动配置（控制器等运行期键由
      `ClashRuntime.start()` 注入到这里）。

    池里的 `proxies` 之外，再写运行期专属键：
    - `proxy-groups` + `listeners`：每条 lane 一个 `select` 组（成员 = 池内全部节点，
      当前选择由控制器设置）+ 一个 mixed listener，listener 的 `proxy` 指向该组。
      入口流量因此固定走指定组，与 GLOBAL 无关；
    - `mode: global`：GLOBAL 仍是维护入口（健康检查按它逐节点探测）；
    - `allow-lan: false` + `bind-address: 127.0.0.1`（默认是 `*`，测出口 IP 不该把
      本机代理入口开给局域网）；
    - `mixed-port: <动态空闲端口>`：GLOBAL 入口。

    这些键**只**进运行配置，绝不回流进池文件。
    """
    pool = pool_path(data_dir)
    doc = yaml.safe_load(pool.read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping) or not isinstance(doc.get("proxies"), list):
        raise PoolBuildError(f"池文件形态不对，无法生成运行配置：{pool}")

    names = [str(entry["name"]) for entry in doc["proxies"]]
    n_lanes = lane_count_for(len(names), lanes)
    groups = [
        {"name": f"{LANE_GROUP_PREFIX}{i}", "type": "select", "proxies": list(names)}
        for i in range(n_lanes)
    ]
    listeners = [
        {
            "name": f"{LANE_GROUP_PREFIX}{i}-in",
            "type": "mixed",
            "port": _free_local_port(),
            "listen": RUNTIME_BIND_ADDRESS,
            "proxy": f"{LANE_GROUP_PREFIX}{i}",
        }
        for i in range(n_lanes)
    ]

    out = runtime_config_path(data_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump(
        {
            "mode": RUNTIME_MODE,
            "allow-lan": False,
            "bind-address": RUNTIME_BIND_ADDRESS,
            # 自带 controller，**不依赖注入的共享默认端口**：老订阅内核若没有
            # controller 行，`ensure_controller` 会给它注入 19090，恰好撞上本实例。
            # 与 mixed-port 同一思路：各用各的动态端口。
            "external-controller": f"{RUNTIME_CONTROLLER_HOST}:{_free_local_port()}",
            "secret": RUNTIME_CONTROLLER_SECRET,
            "mixed-port": _free_local_port(),
            "proxies": list(doc["proxies"]),
            "proxy-groups": groups,
            "listeners": listeners,
        },
        allow_unicode=True,
        sort_keys=False,
    )
    tmp = out.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    return out


@dataclass(frozen=True)
class RuntimeReconcile:
    """一次三方对账的账目。`ok` 才代表「内核已加载 Registry 的全部合格节点」。"""

    registry_names: frozenset[str]
    pool_names: frozenset[str]
    # 与池同域：池 ∩ 内核可见。内置逻辑节点天然进不来，M 因此不会被虚高。
    runtime_names: frozenset[str]
    # 池里有、内核没有——这是真正要盯的差额
    missing: frozenset[str]
    # 内核有、池里没有、也不是内置——正常情况下应为空
    unexpected: frozenset[str]
    # /proxies 返回的全部键数（含内置），仅作记录
    observed_total: int
    ok: bool


def _headers(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"} if secret else {}


async def wait_mixed_port(
    data_dir: Path, *, timeout: float = DEFAULT_WAIT_TIMEOUT
) -> int:
    """等到运行配置里的 `mixed-port` **真的在监听**，返回端口号。

    控制器就绪 ≠ 入口就绪：内核先把 RESTful API 起来，随后才开混合入口，两者之间
    有一个窗口。这个窗口里任何"经 mixed-port 发请求"都会以 `ConnectError` 失败——
    重建后紧接着就用新端口的调用方（crawler）正好落在窗口里。所以重建流程必须等到
    入口真的可用再把 URL 交出去。
    """
    port = mixed_port_of(data_dir)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.3)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return port
        await asyncio.sleep(0.05)
    raise RuntimeUnreachableError(
        f"mixed-port {port} 在 {timeout}s 内没有开始监听（控制器起来了但入口没起）"
    )


async def wait_proxy_names(
    base_url: str, secret: str, *,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
    interval: float = 0.25,
) -> frozenset[str]:
    """轮询控制器直到 `/proxies` 可用，返回其中全部代理名。

    内核是异步进程：`Popen` 返回不代表控制器已监听（实测约 20ms 起来，但不能假定）。
    超时抛 `RuntimeUnreachableError`，并且要说清这是「没起来」而不是「少节点」。
    """
    deadline = time.monotonic() + timeout
    last = "控制器始终没有响应"
    headers = _headers(secret)
    async with httpx.AsyncClient(timeout=3.0, headers=headers) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(f"{base_url}/proxies")
                if resp.status_code == 200:
                    return frozenset(resp.json().get("proxies", {}).keys())
                last = f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001 —— 连不上就是还没起来，继续等
                last = f"{type(exc).__name__}"
            await asyncio.sleep(interval)
    raise RuntimeUnreachableError(
        f"内核控制器 {base_url} 在 {timeout}s 内不可用（{last}）："
        "先看内核日志——这是「内核没起来」，不是「少加载了几个节点」"
    )


def reconcile(
    *,
    registry_names: set[str] | frozenset[str],
    pool_names: set[str] | frozenset[str],
    observed_names: set[str] | frozenset[str],
) -> RuntimeReconcile:
    """三方对账：Registry 合格集、池文件写入集、内核可见集必须**集合相等**。

    只比数量是不够的：改名、错位、去重都能让数量对上而集合不同，而名字是池与
    内核之间唯一的对账钥匙。

    内核可见集里除了内置逻辑节点，还有**运行配置自己生成的 lane 组**——它们是运行
    期条目而不是"池外多出来的节点"，因此同样不参与 `unexpected` 判定。
    """
    registry = frozenset(registry_names)
    pool = frozenset(pool_names)
    observed = frozenset(observed_names)

    runtime_names = observed & pool
    missing = pool - observed
    unexpected = observed - pool - BUILTIN_PROXY_NAMES - {n for n in observed if is_lane_group(n)}

    return RuntimeReconcile(
        registry_names=registry,
        pool_names=pool,
        runtime_names=runtime_names,
        missing=missing,
        unexpected=unexpected,
        observed_total=len(observed),
        ok=(registry == pool) and not missing and not unexpected,
    )


# ══ 池重建 + Runtime 选择恢复 ═════════════════════════════════════
# 选择器（`GLOBAL` 与各 lane 组）的选择权属于 Runtime，**不属于 crawler**：crawler 有
# worker 池，而选择器是共享状态——多个 worker 各自"请求前确保选择正确"会互相踩。
# 所以由这里在一次重建后把选择恢复回去；crawler 只拿"当前运行时代理地址"，
# 不理解选择、重启、重建与端口变化。
#
# GLOBAL 与 lane 的差别在于**共享范围**：GLOBAL 全局共享，lane 组各自独立。
# 因此生产流量走 lane（互不干扰），GLOBAL 留给维护路径。

DEFAULT_SELECT_TIMEOUT = 10.0


class RuntimeRebuildError(RuntimeError):
    """重建失败：对账不过，或选择恢复没能成立。"""


class _KernelRuntime(Protocol):
    """重建只需要这两件事——不把具体内核实现（ClashRuntime）耦合进本域。"""

    def start(self, exe_path: str, config_path: str) -> dict: ...
    def stop(self) -> object: ...


@dataclass(frozen=True)
class RebuildResult:
    proxy_url: str          # GLOBAL 入口（维护路径与单入口消费者用）
    mixed_port: int
    controller_url: str
    pool_names: tuple[str, ...]
    previous_selection: str | None
    selection: str | None    # 恢复后的 GLOBAL
    expected_count: int
    written_count: int
    lane_urls: tuple[str, ...] = ()       # 生产入口：每条 lane 一个本机地址
    lane_nodes: tuple[str, ...] = ()      # 各 lane 绑定到的节点（按 lane 序号）
    lane_exits: tuple[str, ...] = ()      # 各 lane 绑定的出口 IP（同序；容量单位）


def restore_selection(previous: str | None, available: Sequence[str]) -> str | None:
    """重建后恢复 `GLOBAL`：优先原节点，它不在新池则取新池第一项（确定性，不排序）。

    只依赖「上次选择 + 新池」两个输入。内核自己会恢复仍存在于配置中的选择、节点消失
    则回退 `DIRECT`，但那是**内核的内部行为**，不作为本层的契约——它变了我们也不改。
    """
    if previous and previous in available:
        return previous
    return available[0] if available else None


async def current_global_selection(
    controller_url: str, secret: str, *,
    timeout: float = DEFAULT_SELECT_TIMEOUT,
) -> str | None:
    """读当前 `GLOBAL`。控制器不可达 → `None`（当作"没有已知选择"）。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_headers(secret)) as client:
            resp = await client.get(f"{controller_url}/proxies/GLOBAL")
            return resp.json().get("now")
    except Exception:  # noqa: BLE001 —— 读不到就不恢复，由 fallback 接手
        return None


async def _put_global(
    controller_url: str, secret: str, name: str, *,
    timeout: float = DEFAULT_SELECT_TIMEOUT,
) -> str | None:
    async with httpx.AsyncClient(timeout=timeout, headers=_headers(secret)) as client:
        await client.put(f"{controller_url}/proxies/GLOBAL", json={"name": name})
        return (await client.get(f"{controller_url}/proxies/GLOBAL")).json().get("now")


async def apply_global_selection(
    controller_url: str, secret: str, name: str, *,
    timeout: float = DEFAULT_SELECT_TIMEOUT,
) -> str | None:
    """把 `GLOBAL` 设为指定节点并回读确认（维护事务与重建共用同一动作）。"""
    return await _put_global(controller_url, secret, name, timeout=timeout)


def plan_lane_assignment(
    previous: Sequence[str | None], available: Sequence[str], *, lanes: int
) -> list[str | None]:
    """分配每条 lane 绑定的节点：**同一节点不占两条 lane**，输出长度恒为 `lanes`。

    `lanes` 必须显式给：它的事实源是运行配置里的 listener 条数，不是上一次的绑定
    列表——首次启动时上次绑定为空，若按它定长度就会一条都不分配，各 lane 全留在
    组内默认项上（所有 lane 走同一个节点，"多入口"静默退化成单出口）。

    规则：
    1. 上次绑定的节点仍在可用集里 → 原样保留（重建不该无故改动生产出口）；
    2. 其余 lane 按可用集顺序补位，跳过已被占用的节点；
    3. 可用节点不够 → 尾部 lane 记 `None`（**不重复绑同一节点**：两条 lane 绑同一
       出口等于把并发重新压回一个 IP，正是 lane 要解决的问题）；
    4. 可用节点多于 lane → 多余节点不占工位。

    纯函数，不碰网络与文件，便于单独验证。
    """
    count = max(0, int(lanes))
    used: set[str] = set()
    plan: list[str | None] = []
    for i in range(count):
        node = previous[i] if i < len(previous) else None
        if node and node in available and node not in used:
            used.add(node)
            plan.append(node)
        else:
            plan.append(None)
    spare = [name for name in available if name not in used]
    cursor = 0
    for i, node in enumerate(plan):
        if node is None and cursor < len(spare):
            plan[i] = spare[cursor]
            cursor += 1
    return plan


async def current_lane_selections(
    controller_url: str, secret: str, count: int, *,
    timeout: float = DEFAULT_SELECT_TIMEOUT,
) -> list[str | None]:
    """读各 lane 组当前的选择（按 lane 序号）。读不到记 `None`。"""
    out: list[str | None] = []
    try:
        async with httpx.AsyncClient(timeout=timeout, headers=_headers(secret)) as client:
            for i in range(count):
                try:
                    resp = await client.get(f"{controller_url}/proxies/{LANE_GROUP_PREFIX}{i}")
                    out.append(resp.json().get("now"))
                except Exception:  # noqa: BLE001 —— 单条读不到不影响其它 lane
                    out.append(None)
    except Exception:  # noqa: BLE001 —— 控制器不可达：当作"没有已知绑定"
        return [None] * count
    return out


async def assign_lanes(
    controller_url: str, secret: str, selections: Sequence[str | None], *,
    timeout: float = DEFAULT_SELECT_TIMEOUT,
) -> list[str | None]:
    """逐条设置 lane 组的当前节点并回读确认，返回实际生效值。

    回读不一致即抛 `RuntimeRebuildError`：lane 的选择没建立起来，该条 lane 的流量会
    落到组内默认项（而不是被分配的节点），出口归因随之失真。
    """
    applied: list[str | None] = []
    async with httpx.AsyncClient(timeout=timeout, headers=_headers(secret)) as client:
        for i, node in enumerate(selections):
            group = f"{LANE_GROUP_PREFIX}{i}"
            if node is None:
                applied.append(None)
                continue
            await client.put(f"{controller_url}/proxies/{group}", json={"name": node})
            now = (await client.get(f"{controller_url}/proxies/{group}")).json().get("now")
            if now != node:
                raise RuntimeRebuildError(
                    f"lane {i} 选择未成立：now={now!r}，期望 {node!r}"
                )
            applied.append(now)
    return applied


async def align_lanes(
    *, data_dir: Path, controller_url: str, secret: str,
    pool_names: Sequence[str], previous: Sequence[str | None] = (),
    slots: Sequence["ExitSlot"] | None = None,
    timeout: float = DEFAULT_WAIT_TIMEOUT,
) -> tuple[tuple[int, ...], list[str | None]]:
    """启动内核之后必须做的一件事：**等 lane 入口就绪并把绑定建立起来**。

    内核起来时各 lane 组的默认选择是组内第一项——不建立绑定的后果是所有 lane 走
    同一个节点，"多入口"退化成单出口而系统照样自称 ready。因此这一步与
    `wait_mixed_port` 同级：不完成就不算 Runtime 可用。

    `slots` 给的是**出口槽表**（`exits.select_exit_slots` 的产物）：lane i 绑槽 i 的
    代表节点。不给则退回「按池内顺序且互不重复」的分配（旧行为）。绑定成功后把
    「lane → 出口 IP → 节点」写成 lane 计划——运行配置里没有出口 IP，归因只能靠它。
    """
    lane_ports = runtime_lane_ports(data_dir)
    if not lane_ports:
        return (), []
    ports = await wait_lane_ports(data_dir, timeout=timeout)
    if slots is not None:
        plan: list[str | None] = [
            (slots[i].runtime_name if i < len(slots) else None) for i in range(len(ports))
        ]
    else:
        plan = plan_lane_assignment(previous, pool_names, lanes=len(ports))
    applied = await assign_lanes(controller_url, secret, plan)
    plan_path = lane_plan_path(data_dir)
    if slots is not None:
        write_lane_plan(
            data_dir,
            [
                {
                    "lane": i,
                    "port": ports[i],
                    "exitIp": slots[i].exit_ip if i < len(slots) else None,
                    "node": applied[i],
                    "alternatives": list(slots[i].alternatives) if i < len(slots) else [],
                }
                for i in range(len(ports))
            ],
        )
    elif plan_path.is_file():
        # 退回分配时旧的出口对照已失效：删掉它，宁可没有也不要错的归因
        plan_path.unlink(missing_ok=True)
    return ports, applied


def controller_endpoint_of(data_dir: Path) -> tuple[str, str]:
    """读运行配置里**自带的** controller：返回 `(base_url, secret)`。

    自带 controller 是本实例隔离的一部分——不读它就只能去猜注入的默认端口，
    那个默认值是多个 Runtime 共用的。
    """
    path = runtime_config_path(data_dir)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    addr = doc.get("external-controller") if isinstance(doc, Mapping) else None
    if not isinstance(addr, str) or not addr.strip():
        raise RuntimeConfigError(f"运行配置里没有 external-controller：{path}")
    return f"http://{addr.strip()}", str(doc.get("secret") or "")


async def rebuild_runtime(
    session: AsyncSession, *,
    data_dir: Path,
    controller_url: str | None,
    secret: str,
    runtime: _KernelRuntime,
    exe_path: str,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
) -> RebuildResult:
    """一次完整的池重建 + Runtime 恢复。

    顺序本身是契约的一部分：

        capture previous GLOBAL / lane 绑定 → build_pool → prepare_runtime_config
        → **显式 stop** → start → wait（控制器 / 入口 / lane 入口）+ reconcile
        → restore GLOBAL → 重绑 lane

    `start()` 对已在跑的内核**不会重启**（除非传 `restart_if_changed`），所以 `stop()`
    不能省——否则会出现"配置文件里是新端口、实际跑的还是旧内核"。
    """
    previous = (
        await current_global_selection(controller_url, secret)
        if controller_url else None
    )
    # 上次各 lane 绑定的节点：重建时优先原样恢复，避免每次重建都换一遍生产出口
    previous_lanes: list[str | None] = []
    if controller_url:
        previous_lanes = await current_lane_selections(
            controller_url, secret, len(runtime_lane_ports(data_dir))
        )

    build = await build_pool(session, data_dir=data_dir)
    # 容量单位是**独立出口 IP**，不是节点数：槽数即本次 run 的 lane 数。
    # 一个出口 IP 都没探到时退回按节点数开工位（池刚建好、尚未跑 L1 的形态）。
    slots = select_exit_slots(await eligible_nodes(session))
    config = prepare_runtime_config(data_dir, lanes=len(slots) or None)

    runtime.stop()
    status = runtime.start(exe_path, str(config))
    base = status["controllerUrl"]
    new_secret = getattr(runtime, "secret", secret)
    observed = await wait_proxy_names(base, new_secret, timeout=wait_timeout)
    # 入口就绪晚于控制器就绪：不等它，交出去的代理地址会立刻 ConnectError
    port = await wait_mixed_port(data_dir, timeout=wait_timeout)

    ledger = reconcile(
        registry_names=set(build.runtime_names),
        pool_names=set(build.runtime_names),
        observed_names=observed,
    )
    if not ledger.ok:
        # 独立会话：本函数紧接着抛异常，调用方多半整事务回滚——事件必须自己落库，
        # 否则「重建被对账拒绝」这个事实最需要留痕的一条，恰恰会随回滚一起消失
        await events.record(
            events.KIND_RECONCILE_REJECTED,
            f"重建后对账失败：缺 {sorted(ledger.missing)}，多 {sorted(ledger.unexpected)}",
            level=events.LEVEL_ERROR,
            payload={
                "phase": "rebuild",
                "registryCount": len(ledger.registry_names),
                "poolCount": len(ledger.pool_names),
                "runtimeCount": len(ledger.runtime_names),
                "missingCount": len(ledger.missing),
                "unexpectedCount": len(ledger.unexpected),
                "observedTotal": ledger.observed_total,
            },
        )
        raise RuntimeRebuildError(
            f"重建后对账失败：缺 {sorted(ledger.missing)}，多 {sorted(ledger.unexpected)}"
        )

    chosen = restore_selection(previous, build.runtime_names)
    if chosen is not None:
        applied = await _put_global(base, new_secret, chosen)
        if applied != chosen:
            # 选择没建立起来 = 流量可能落到 DIRECT（绕过代理池），必须大声失败
            raise RuntimeRebuildError(
                f"恢复 GLOBAL 失败：now={applied!r}，期望 {chosen!r}"
            )

    lane_ports, lane_nodes = await align_lanes(
        data_dir=data_dir, controller_url=base, secret=new_secret,
        pool_names=build.runtime_names, previous=previous_lanes,
        slots=slots or None, timeout=wait_timeout,
    )

    return RebuildResult(
        proxy_url=f"http://127.0.0.1:{port}",
        mixed_port=port,
        controller_url=base,
        pool_names=tuple(build.runtime_names),
        previous_selection=previous,
        selection=chosen,
        expected_count=build.expected_count,
        written_count=build.written_count,
        lane_urls=tuple(f"http://127.0.0.1:{p}" for p in lane_ports),
        lane_nodes=tuple(node or "" for node in lane_nodes),
        lane_exits=tuple(slot.exit_ip for slot in slots[:len(lane_ports)]),
    )


# ══ 受管爬取的代理地址：拿不到就拒绝启动 ═════════════════════════
# crawler 只该拿"当前运行时代理 URL"，不理解 GLOBAL / 重建 / 端口变化。而它**必须**
# 拿到——拿不到就拒绝启动：静默退回直连（或旧订阅代理）会把"池坏了"伪装成"爬取成功"，
# 并悄悄破坏两套 Runtime 的隔离。


def current_runtime_proxy_url(data_dir: Path) -> str | None:
    """当前 Runtime 的代理地址；不可用返回 `None`。

    可用 = 运行配置里有 `mixed-port` **且该端口真的在监听**。只看配置不够：内核没起来
    或入口还没就绪时，配置照样存在，而请求会直接 `ConnectError`。
    """
    try:
        port = mixed_port_of(data_dir)
    except (RuntimeConfigError, OSError):
        # 连运行配置都没有（首次启动）也是"不可用"，不是异常
        return None
    with socket.socket() as sock:
        sock.settimeout(0.3)
        if sock.connect_ex(("127.0.0.1", port)) != 0:
            return None
    return f"http://127.0.0.1:{port}"


def require_runtime_proxy_url(data_dir: Path) -> str:
    """同 `current_runtime_proxy_url`，但拿不到就抛 `RuntimeUnavailableError`。"""
    proxy_url = current_runtime_proxy_url(data_dir)
    if proxy_url is None:
        raise RuntimeUnavailableError(
            "代理运行时不可用（没有可用的池 Runtime），本次爬取未启动"
        )
    return proxy_url


def require_lane_proxy_urls(data_dir: Path) -> list[str]:
    """受管爬取的生产入口：每条 lane 一个地址，worker 按序号绑定。

    降级边界只有一条：运行配置里**没有 lane**（旧产物）时退回同一个 Runtime 的
    GLOBAL 入口——仍是池内出口，不构成"退回直连或旧订阅代理"。有 lane 但入口没全
    就绪属于 Runtime 不可用，直接抛错（fail closed），不把半就绪的入口交出去。
    """
    if runtime_lane_ports(data_dir):
        urls = lane_proxy_urls(data_dir)
        if not urls:
            raise RuntimeUnavailableError(
                "代理运行时的 lane 入口没有全部就绪，本次爬取未启动"
            )
        return urls
    return [require_runtime_proxy_url(data_dir)]


def lane_bindings(data_dir: Path) -> list[dict]:
    """本次 run 的 lane 绑定表：每条 lane 的地址、出口 IP、执行节点。

    出口 IP 与节点取自 lane 计划（`crawl-lanes.yaml`）；没有计划时出口记 `None`——
    宁可缺字段，也不要拿 lane 序号冒充出口身份。
    """
    urls = lane_proxy_urls(data_dir)
    if not urls:
        return []
    plan = {int(e.get("lane", -1)): e for e in read_lane_plan(data_dir)}
    bindings: list[dict] = []
    for i, url in enumerate(urls):
        entry = plan.get(i) or {}
        exit_ip = entry.get("exitIp")
        bindings.append({
            "lane": i,
            "url": url,
            "exitIp": str(exit_ip) if exit_ip else None,
            "node": str(entry["node"]) if entry.get("node") else None,
        })
    return bindings


def lane_run_plan(data_dir: Path) -> dict:
    """受管爬取一次 run 需要的入口三元组：地址 / 出口键 / 执行节点（三者同序）。

    **唯一取值口**：生产路径与验证脚本都从这里取，避免"两处各自拼一遍"再次出现
    「worker 拿到地址但没拿到出口身份」这类只在一侧发生的缺口。出口身份缺失时键退回
    `lane:<序号>`——它仍能保证同出口共享一份预算（同一 lane 上的 worker 同键），
    只是不跨 lane 合并同出口。
    """
    bindings = lane_bindings(data_dir)
    if not bindings:
        urls = require_lane_proxy_urls(data_dir)
        bindings = [
            {"lane": i, "url": url, "exitIp": None, "node": None}
            for i, url in enumerate(urls)
        ]
    return {
        "urls": [b["url"] for b in bindings],
        "exit_keys": [b["exitIp"] or f"lane:{b['lane']}" for b in bindings],
        "nodes": [b["node"] or "" for b in bindings],
        "bindings": bindings,
    }
    if runtime_lane_ports(data_dir):
        urls = lane_proxy_urls(data_dir)
        if not urls:
            raise RuntimeUnavailableError(
                "代理运行时的 lane 入口没有全部就绪，本次爬取未启动"
            )
        return urls
    return [require_runtime_proxy_url(data_dir)]