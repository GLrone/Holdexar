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
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import httpx
import yaml

from app.domains.proxypool.pool import PoolBuildError, pool_path

# 内核内置的逻辑节点（不是池里的节点）。仅用于把 /proxies 里的「多出来的键」
# 解释清楚；它们随内核版本可能变，所以**不**作为硬门禁参与 ok 判定。
BUILTIN_PROXY_NAMES = frozenset({
    "DIRECT", "REJECT", "REJECT-DROP", "GLOBAL", "COMPATIBLE", "PASS", "PASS-RULE",
})

DEFAULT_WAIT_TIMEOUT = 15.0


class RuntimeUnreachableError(RuntimeError):
    """控制器在超时内不可用——通常意味着内核**没有起来**（配置解析 fatal）。"""


RUNTIME_CONFIG_FILENAME = "crawl-runtime.yaml"

# 运行期专属键（只进运行配置，绝不回流进池文件）
RUNTIME_MODE = "global"          # L1 靠 GLOBAL 选择器切节点，不需要 proxy-groups
RUNTIME_BIND_ADDRESS = "127.0.0.1"  # 默认是 '*'：测出口 IP 不该把本机入口开给局域网


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