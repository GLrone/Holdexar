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

from app.domains.proxypool.pool import PoolBuildError, build_pool, pool_path

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

# 运行期专属键（只进运行配置，绝不回流进池文件）
RUNTIME_MODE = "global"          # L1 靠 GLOBAL 选择器切节点，不需要 proxy-groups
RUNTIME_BIND_ADDRESS = "127.0.0.1"  # 默认是 '*'：测出口 IP 不该把本机入口开给局域网
RUNTIME_CONTROLLER_HOST = "127.0.0.1"
# 池 Runtime 自己的 controller 凭据（本机回环专用）。刻意**不随每次重建轮换**：
# 轮换只会制造"拿旧凭据访问新实例"的陷阱，而 controller 的隔离靠端口就够。
RUNTIME_CONTROLLER_SECRET = "holdexar-proxypool"


class RuntimeConfigError(RuntimeError):
    """运行配置不可用（例如缺 mixed-port）。"""


def runtime_config_path(data_dir: Path) -> Path:
    """内核启动配置的路径（与只读的池文件同目录、不同文件）。"""
    return Path(data_dir) / "proxypool" / RUNTIME_CONFIG_FILENAME


def _free_local_port() -> int:
    """让系统分配一个空闲的本机 TCP 端口（L1 的代理入口）。

    首版刻意**不做端口租约**：真撞上时内核会暴露失败（日志里不会出现
    「Mixed proxy listening」），到真实使用中出现证据再决定要不要做端口管理。
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def mixed_port_of(data_dir: Path) -> int:
    """读运行配置里的 mixed-port——L1 要经它发真实请求。"""
    path = runtime_config_path(data_dir)
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    port = doc.get("mixed-port") if isinstance(doc, Mapping) else None
    if not isinstance(port, int) or port <= 0:
        raise RuntimeConfigError(f"运行配置里没有可用的 mixed-port：{path}")
    return port


def prepare_runtime_config(data_dir: Path) -> Path:
    """由 `crawl-pool.yaml` 生成 `crawl-runtime.yaml`——内核实际启动用的文件。

    为什么必须分文件：`ClashRuntime.start()` 会把 `external-controller` / `secret`
    **写回它收到的那个文件**。若直接启动池文件，池就不再等于 `build_pool` 校验过的
    产物，而且下一次 `build_pool` 一覆盖就把控制器注入抹掉——而 P1.4 的健康检测
    正依赖控制器，这个矛盾不能带进健康模块。

    职责：
    - `crawl-pool.yaml` = Registry 的纯运行集，**只读产物**，内核对它零写入；
    - `crawl-runtime.yaml` = 临时、可重建的内核启动配置（控制器等运行期键由
      `ClashRuntime.start()` 注入到这里）。

    池里的 `proxies` 之外，再写运行期专属键：`mode: global`（L1 靠 GLOBAL 切节点，
    不需要 proxy-groups）、`allow-lan: false` + `bind-address: 127.0.0.1`（默认是 `*`，
    测出口 IP 不该把本机代理入口开给局域网）、`mixed-port: <动态空闲端口>`。
    这些键**只**进运行配置，绝不回流进池文件。
    """
    pool = pool_path(data_dir)
    doc = yaml.safe_load(pool.read_text(encoding="utf-8"))
    if not isinstance(doc, Mapping) or not isinstance(doc.get("proxies"), list):
        raise PoolBuildError(f"池文件形态不对，无法生成运行配置：{pool}")

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
    """
    registry = frozenset(registry_names)
    pool = frozenset(pool_names)
    observed = frozenset(observed_names)

    runtime_names = observed & pool
    missing = pool - observed
    unexpected = observed - pool - BUILTIN_PROXY_NAMES

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
# `GLOBAL` 的选择权属于 Runtime，**不属于 crawler**：crawler 有 worker 池，而 `GLOBAL`
# 是一个全局共享选择器——多个 worker 各自"请求前确保 GLOBAL 正确"会互相踩，直接破坏
# L1/L2 已经建立的节点归因模型。所以由这里在一次重建后把选择恢复回去；crawler 只拿
# "当前运行时代理 URL"，不理解选择、重启、重建与端口变化。

DEFAULT_SELECT_TIMEOUT = 10.0


class RuntimeRebuildError(RuntimeError):
    """重建失败：对账不过，或选择恢复没能成立。"""


class _KernelRuntime(Protocol):
    """重建只需要这两件事——不把具体内核实现（ClashRuntime）耦合进本域。"""

    def start(self, exe_path: str, config_path: str) -> dict: ...
    def stop(self) -> object: ...


@dataclass(frozen=True)
class RebuildResult:
    proxy_url: str          # 给 crawler 用的"当前运行时代理 URL"
    mixed_port: int
    controller_url: str
    pool_names: tuple[str, ...]
    previous_selection: str | None
    selection: str | None    # 恢复后的 GLOBAL
    expected_count: int
    written_count: int


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

        capture previous GLOBAL → build_pool → prepare_runtime_config
        → **显式 stop** → start → wait + reconcile → restore GLOBAL

    `start()` 对已在跑的内核**不会重启**（除非传 `restart_if_changed`），所以 `stop()`
    不能省——否则会出现"配置文件里是新端口、实际跑的还是旧内核"。
    """
    previous = (
        await current_global_selection(controller_url, secret)
        if controller_url else None
    )

    build = await build_pool(session, data_dir=data_dir)
    config = prepare_runtime_config(data_dir)

    runtime.stop()
    status = runtime.start(exe_path, str(config))
    base = status["controllerUrl"]
    new_secret = getattr(runtime, "secret", secret)
    observed = await wait_proxy_names(base, new_secret, timeout=wait_timeout)
    # 入口就绪晚于控制器就绪：不等它，交出去的 proxy_url 会立刻 ConnectError
    port = await wait_mixed_port(data_dir, timeout=wait_timeout)

    ledger = reconcile(
        registry_names=set(build.runtime_names),
        pool_names=set(build.runtime_names),
        observed_names=observed,
    )
    if not ledger.ok:
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

    return RebuildResult(
        proxy_url=f"http://127.0.0.1:{port}",
        mixed_port=port,
        controller_url=base,
        pool_names=tuple(build.runtime_names),
        previous_selection=previous,
        selection=chosen,
        expected_count=build.expected_count,
        written_count=build.written_count,
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
