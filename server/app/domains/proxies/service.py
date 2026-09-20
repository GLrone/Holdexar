"""proxies 域服务：代理池 CRUD / 健康检查 / 策略引擎 / 走线日志。

策略（存 app_settings: proxy.strategy）：
- direct_only  默认直连
- direct_first 直连（预留 M4 后续：失败换代理重试）
- proxy_only   从启用代理轮询取一个用于整个任务
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import weakref
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
from sqlalchemy import delete, func, select

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj


# ─── Clash 检测串行锁（按事件循环缓存）──────────────────────
# test_clash_nodes 逐个切 selector 探测——手动检测/启动首检/定时体检三源
# 并发会互踩 selector 与延迟读数。锁按事件循环缓存：uvicorn 单 loop 常态
# 下等效模块级单例；测试里每用例新 loop 则各拿各的锁，互不卡死。
_clash_test_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _clash_test_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _clash_test_locks.get(loop)
    if lock is None:
        lock = asyncio.Lock()
        _clash_test_locks[loop] = lock
    return lock
from . import clash_manager
from .models import (
    ADMISSION_ACTIVE,
    ADMISSION_CANDIDATE,
    ClashNode,
    Proxy,
    ProxyEvent,
    ProxySubscription,
)

logger = logging.getLogger(__name__)

# 健康检查目标：真实 Steam 端点而非 ping
TEST_URL = "https://store.steampowered.com/api/appdetails"
TEST_PARAMS = {"appids": "220", "cc": "us", "l": "english", "filters": "price_overview"}
TEST_TIMEOUT = 12.0
MAX_CONSECUTIVE_FAILURES = 5  # 连续失败自动禁用

# ── 代理池加权选择（_weighted_pick）的参数 ──
SPEED_REF_MS = int(TEST_TIMEOUT * 1000)  # 速度参照 = 体检超时；比这更慢的一律按最慢算
SPEED_FLOOR = 0.5  # 最慢出口仍保留的速度分（不归零：否则慢节点永不复测、永远翻不了身）
UNTESTED_SPEED = 0.7  # 未测速节点的速度分：中游，够它分到流量被测出来
WEIGHT_SCALE = 100_000  # 权重分辨率，不影响各出口的相对份额

# ── Clash 节点状态机（2026-09 节点级状态存储）────────────────
HEALTH_INTERVAL_HOURS = 6  # 体检间隔（本地软件不常驻，跨重启靠库门槛）
SUBSCRIPTION_REFRESH_HOURS = 6  # 订阅重拉间隔（与节点体检同拍，跨重启靠 KV 门槛）
DEAD_MAX_FAILS = 10  # 累计失败 → dead 终态
REVIVE_PASSES_REQUIRED = 3  # dead 复活需连续 3-of-3 测通过
SUBSCRIPTION_DEPRECATE_RATIO = 0.95  # 不可用节点占比 >95% → 订阅废弃
# 失败冷却递增（分钟）：30min → 1h → 2h → 4h → 8h → 24h 封顶
_FAIL_COOLDOWN_MINUTES = [30, 60, 120, 240, 480, 1440]


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo else dt


def proxy_label(p: Proxy | None) -> str:
    return p.label or (f"{p.scheme}://{p.host}:{p.port}") if p else "direct"


# ─── 策略 ────────────────────────────────────────────────────

STRATEGIES = ("proxy_first", "direct_only", "direct_first", "proxy_only")


async def get_strategy() -> dict:
    from app.domains.settings.service import get_value, set_value

    strategy = await get_value("proxy.strategy", None)
    if strategy is None or strategy == "direct_only":
        # 默认策略升级：Steam 域直连基本不可用（成功属侥幸），
        # 代理优先成为默认；存量 direct_only 一次性迁移。
        strategy = "proxy_first"
        await set_value("proxy.strategy", strategy)
    if strategy in ("pinned", "clash"):
        # 策略下架：pinned/clash 移除（自启 Verge 由代理优先回落探测
        # 覆盖），存量值一次性迁移，避免静默退化为 proxy_only 语义（无代理即报错）。
        strategy = "proxy_first"
        await set_value("proxy.strategy", strategy)
        from app.domains.settings.service import delete_value

        await delete_value("proxy.pinned_id")

    return {
        "strategy": strategy,
        "clashPort": await get_value("proxy.clash_port", 7890),
    }


async def set_strategy(strategy: str | None = None, clash_port: int | None = None) -> None:
    from app.domains.settings.service import set_value

    if strategy is not None:
        if strategy not in STRATEGIES:
            raise ValueError(f"未知策略: {strategy}")
        await set_value("proxy.strategy", strategy)
    if clash_port is not None:
        await set_value("proxy.clash_port", clash_port)


async def _local_clash_alive() -> bool:
    """探测 proxy.clash_port 指向的本地混合端口是否活着（1s TCP 连接）。

    只验证端口在监听，不验证出口能通 Steam——后者由调用方超时/重试兜底。
    """
    port = await _clash_port_cached()
    try:
        _, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.close()
        await writer.wait_closed()
        return True
    except OSError:
        return False


_clash_port_cache: tuple[float, int] | None = None


async def _clash_port_cached() -> int:
    """clash_port 设置读取（30s 内存缓存，避免高频探活反复进库）。"""
    global _clash_port_cache
    from app.domains.settings.service import get_value

    now = time.monotonic()
    if _clash_port_cache and now - _clash_port_cache[0] < 30:
        return _clash_port_cache[1]
    port = await get_value("proxy.clash_port", 7890)
    _clash_port_cache = (now, int(port or 7890))
    return _clash_port_cache[1]


async def resolve_proxy_url() -> str | None:
    """策略引擎：为本次任务解析代理 URL。None = 直连。

    若策略要求代理但无可用代理，抛 RuntimeError（调用方转 400）。
    """
    from app.domains.settings.service import get_value

    strategy = await get_value("proxy.strategy", "proxy_first")

    if strategy in ("direct_only", "direct_first"):
        return None

    if strategy == "proxy_first":
        # 代理优先（2026-09 起默认）：Clash 内核在跑 → 内核端口；
        # 代理池有启用节点 → 轮询；都没有 → 探本地混合端口（用户自启的
        # Verge/Clash 不在本服务管辖内，但 steam 域直连基本不可用，
        # 活着的本地代理永远优于直连兜底）；仍无 → 直连。
        status = clash_manager.runtime.status()
        if status["running"] and status.get("port"):
            return f"http://127.0.0.1:{status['port']}"
        enabled = await list_proxies(enabled_only=True)
        if enabled:
            return await _weighted_pick(enabled)
        if await _local_clash_alive():
            port = await get_value("proxy.clash_port", 7890)
            return f"http://127.0.0.1:{port}"
        return None

    # proxy_only: 加权随机（延迟+健康反馈到流量分配；rr_index 顺序轮询兜底）
    enabled = await list_proxies(enabled_only=True)
    if not enabled:
        raise RuntimeError(f"策略 {strategy} 需要至少一条启用的代理")
    return await _weighted_pick(enabled)


async def resolve_failover_proxy_url() -> str | None:
    """失败换代理取值口（direct_first 的 failover 通道）：强制取一个代理出口。

    与 resolve_proxy_url 的差异：direct 类策略平时返回 None（直连），
    但失败换代理时需要「有什么代理用什么」——Clash 在跑走内核端口，
    池有节点走加权随机，再探本地混合端口，全无 → None（真没有代理）。
    """
    status = clash_manager.runtime.status()
    if status["running"] and status.get("port"):
        return f"http://127.0.0.1:{status['port']}"
    enabled = await list_proxies(enabled_only=True)
    if enabled:
        return await _weighted_pick(enabled)
    if await _local_clash_alive():
        from app.domains.settings.service import get_value

        port = await get_value("proxy.clash_port", 7890)
        return f"http://127.0.0.1:{port}"
    return None


async def _round_robin(enabled: list[Proxy]) -> str:
    """代理池轮询取一条（rr_index 存 app_settings）。"""
    from app.domains.settings.service import get_value, set_value

    idx = int(await get_value("proxy.rr_index", 0) or 0)
    chosen = enabled[idx % len(enabled)]
    await set_value("proxy.rr_index", idx + 1)
    return chosen.url()


async def _weighted_pick(enabled: list[Proxy]) -> str:
    """按健康度加权随机取一条出口（策略引擎每批任务调用一次）。

    为什么不用朴素轮询：轮询把已失败的出口和健康出口当等价，流量会继续平摊
    到死节点上，直到它撞满禁用线——每次都先白烧几个请求才发现。加权让健康度
    直接决定流量分配：快节点多干活，正在降级的节点被迅速挤出。

    权重 = **速度分 × 健康分**，两个分都是 [0,1]，相乘后放大成整数权重。
    拆成两个因子而不是拼成一个式子，因为两者性质不同：速度是「能多快」的连续
    量，健康是「还值不值得信」的判断——一个刚连败 3 次的节点再快也不该按快节点
    分流量。所以健康分是主控项（可归零、指数衰减），速度分只在健康分内部调节。

    - **速度分**：以体检超时 `TEST_TIMEOUT` 为参照，越慢越低，但保留 `SPEED_FLOOR`
      的底线。不归零是有意的：拿不到流量的节点永远不会被复测，也就永远翻不了身。
      未测速的新节点给 `UNTESTED_SPEED`（中游），同样是为了让它持续分到流量、
      尽快被测出来，而不是在取样之前就被判死。
    - **健康分**：连续失败 ≥ `MAX_CONSECUTIVE_FAILURES` 直接 0——禁用线与体检的
      自动禁用取同一个常量，一处判定两处一致，不会出现「权重说不能用、enable
      说能用」。未到线的按 2^-失败次数 衰减，留翻身机会。

    权重全为 0（整池皆死）时退回顺序轮询：策略引擎宁可拿一个可能失败的出口
    去试，也不要在这里抛异常——「没有可用出口」的判断留给上层。

    proxies 表的 latency/status/consecutive_failures 由体检（手动/定时）持续
    维护，本函数只消费、不修改。
    """
    import random

    def _speed(p: Proxy) -> float:
        """速度分 ∈ [SPEED_FLOOR, 1]：以体检超时为参照，越慢越低。"""
        if not p.latency_ms:
            return UNTESTED_SPEED
        slow = min(p.latency_ms, SPEED_REF_MS) / SPEED_REF_MS
        return SPEED_FLOOR + (1.0 - SPEED_FLOOR) * (1.0 - slow)

    def _health(p: Proxy) -> float:
        """健康分 ∈ [0, 1]：0 = 不可用，指数衰减 = 降级中，1 = 正常。"""
        fails = p.consecutive_failures or 0
        if fails >= MAX_CONSECUTIVE_FAILURES:
            return 0.0
        if fails == 0 and p.status != "failed":
            return 1.0
        # 有失败计数、或被人工标成 failed 的，按 2^-失败次数 降权（1/2, 1/4, 1/8…）。
        # 手动标 failed 但计数为 0 时按 1 次失败算，否则人工拉黑的出口权重照旧。
        return 2.0 ** -max(fails, 1)

    scores = [_speed(p) * _health(p) for p in enabled]
    if sum(scores) <= 0:
        return await _round_robin(enabled)
    # 健康分为 0 的出口权重精确为 0（不被下限抬起来），其余最低也有
    # SPEED_FLOOR × 2^-(MAX-1) × WEIGHT_SCALE，不会因取整掉到 0
    weights = [round(s * WEIGHT_SCALE) for s in scores]
    chosen = random.choices(enabled, weights=weights, k=1)[0]
    return chosen.url()


async def _saved_proxy_candidates(limit: int = 3) -> list[str]:
    """订阅拉取的借道出口：项目自己保存的可用代理（proxies 表启用项）。

    只在**直连失败后**才轮到它们（通道链里排在直连之后，见
    clash_manager._download_attempts）。取多条而不是一条：池里的单条代理
    随时可能自己失效，一次只试一条等于把「借道」变成掷骰子；加权随机
    天然倾向健康节点，重复抽到同一条不影响正确性（去重后可能少于 limit）。

    这里**只**返回代理池——内核端口 / 本地混合端口由通道链自行探测补位，
    不在本函数重复解析（也避免拉取路径多读一次设置库）。
    池空返回空表（调用方即「直连之外没有可借道的池代理」）。
    """
    out: list[str] = []
    seen: set[str] = set()
    try:
        enabled = await list_proxies(enabled_only=True)
    except Exception:  # noqa: BLE001 —— 池不可用按空池处理，不拖垮拉取
        enabled = []
    for _ in range(limit):
        if not enabled:
            break
        try:
            pick = await _weighted_pick(enabled)
        except Exception:  # noqa: BLE001
            break
        if pick not in seen:
            seen.add(pick)
            out.append(pick)
    return out


async def _apply_subscription_name(sub_id: int, name: str) -> str | None:
    """订阅名落库（本地长期保存）：仅在本行尚无名称时写入。

    拉到订阅名（profile-title / Content-Disposition / URL 末段）后调一次，
    让「只有链接、没有名字」的订阅自动获得可读名称并长期保存在本地库——
    名字一旦落地就是用户的资产，重拉二十次也不该把「我的机场」换回面板
    里的 token，故已有名称的行一律不覆写。
    返回写入后的名称（无名可写 / 订阅不存在时返回 None）。
    """
    name = (name or "").strip()[:100]
    if not name:
        return None
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return None
        if sub.label:
            return sub.label
        sub.label = name
        await session.commit()
        return name


async def update_subscription_label(sub_id: int, label: str) -> dict:
    """订阅手动改名（订阅名不再自动回填——面板 profile-title 覆盖面窄，
    且回填时机零散；名字由用户自己维护，空串可清名）。"""
    label = (label or "").strip()
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            raise ValueError("订阅不存在")
        sub.label = label or None
        await session.commit()
        return {"id": sub.id, "kind": sub.kind, "url": sub.url, "label": sub.label}


async def update_subscription(
    sub_id: int, label: str | None = None, url: str | None = None
) -> dict:
    """编辑订阅：改名 + 换链接；链接变更的 clash 订阅**保存即自动重拉**。

    链接是订阅的唯一身份——换了链接等于换了一个机场，磁盘上的 config.yaml
    与 clash_nodes 账本都还属于旧链接，不重拉就一直对不上。重拉通道同
    「重拉」按钮：直连优先，失败自动借道项目保存的可用代理（见
    _saved_proxy_candidates）。

    重拉失败**不回滚**改名换链：名字和链接是用户刚确认的输入，重拉失败
    是网络的一次抖动，把用户的输入撤掉只会让他再敲一遍。失败以 warning
    回传，由前端提示可稍后手动重拉。

    返回 {id, kind, url, label, synced, nodes?, traffic?, alive?, total?,
    restarted?, warning?}；synced=true 表示本次确实重拉过。
    """
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            raise ValueError("订阅不存在")
        if label is not None:
            sub.label = (label or "").strip() or None
        url_changed = False
        if url is not None:
            new_url = (url or "").strip()
            if not new_url.lower().startswith(("http://", "https://")):
                raise ValueError("订阅链接必须是 http(s) URL")
            if new_url != sub.url:
                sub.url = new_url
                url_changed = True
        await session.commit()
        result: dict = {
            "id": sub.id, "kind": sub.kind, "url": sub.url,
            "label": sub.label, "synced": False,
        }
    if url_changed and result["kind"] == "clash":
        try:
            sync = await refresh_clash_subscription(sub_id)
        except Exception as e:  # noqa: BLE001 —— 重拉失败不撤销改名换链
            result["warning"] = f"订阅链接已更新，但自动重拉失败：{e}"
            return result
        result.update({
            "synced": True,
            "label": sync.get("label", result["label"]),
            "nodes": sync.get("nodes"),
            "traffic": sync.get("traffic"),
            "alive": sync.get("alive", 0),
            "total": sync.get("total", 0),
            "restarted": sync.get("restarted", False),
        })
    return result


async def refresh_subscription_traffic(sub_id: int) -> dict:
    """流量统计实时回填：轻量拉订阅响应头 subscription-userinfo。

    面板流量随消耗实时增长，仅在内核启动时落库会滞后数十 GB（用户
    实证 315 GB 停滞 vs 实际 318）。只更新 lastStats.traffic，不动
    config.yaml、不重启内核；失败抛 ValueError 由路由转 400/502。
    """
    sub = await get_subscription(sub_id)
    if sub is None:
        raise ValueError("订阅不存在")
    if sub.kind != "clash":
        raise ValueError("该订阅不是 Clash 订阅（面板流量头仅机场订阅有）")
    try:
        proxy = None
        try:
            proxy = await resolve_proxy_url()
        except Exception:  # noqa: BLE001
            proxy = None
        headers = await clash_manager.runtime.fetch_subscription_headers(
            sub.url, proxy_url=proxy
        )
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"订阅头拉取失败: {e}") from e
    userinfo = (headers or {}).get("userinfo")
    if not userinfo:
        raise ValueError("面板未返回流量头（subscription-userinfo）")
    await _merge_last_stats(sub_id, traffic=userinfo)
    return {"id": sub_id, "traffic": userinfo}


async def _mark_refreshed(sub_id: int) -> None:
    """重拉成功留痕：订阅行 last_imported_at（前端「上次拉取」口径）+ KV 门槛。

    KV 门槛（proxy.sub_last_refresh_at）是定时重拉的间隔依据，手动重拉同样
    计数——刚手动拉过就不必再来一次自动拉。缓存回退（下载全败）不算成功，
    门槛不被消费，下一拍继续试。
    """
    from app.domains.settings.service import set_value

    now = _naive(get_beijing_time_obj())
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is not None:
            sub.last_imported_at = now
            await session.commit()
    await set_value("proxy.sub_last_refresh_at", now.isoformat())


async def refresh_clash_subscription(sub_id: int) -> dict:
    """独立刷新 Clash 订阅：重新下载配置 + 流量回填 + 账本收敛。

    不依赖「重启内核」——服务常驻期间订阅内容也能刷新。下载成功后：
    - 内核在跑且配置有变化 → 重启生效（节点增删立即反映）
    - 内核没跑 → 只下载不启动（下次启动自然用新配置）
    存活统计落库：lastStats.alive/total 用账本 + 当前配置节点名口径。
    成功后写「上次拉取」留痕（订阅行时间戳 + 定时重拉门槛 KV，见
    _mark_refreshed）；缓存回退（全部通道下载失败）不算成功。

    通道：**直连优先**，直连失败自动改用项目保存的可用代理（_saved_proxy_candidates）。
    本行还没有名称时，用本次拉到的订阅名（profile-title 等）回填并本地保存
    ——见 _apply_subscription_name；已有名称（用户手改或此前自动取过）不覆写。
    """
    sub = await get_subscription(sub_id)
    if sub is None:
        raise ValueError("订阅不存在")
    if sub.kind != "clash":
        raise ValueError("该订阅不是 Clash 订阅（kind=clash）")
    label = sub.label
    from app.core.config import get_settings

    settings = get_settings()
    try:
        meta = await clash_manager.runtime.download_subscription(
            sub.url, settings.data_dir, await _saved_proxy_candidates()
        )
    except ValueError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ValueError(f"订阅下载失败: {e}") from e

    # 流量/节点数一并回填（下载响应里就有；失败不阻塞刷新）
    fields: dict = {
        "nodes": meta.get("nodes"),
        "cached": meta.get("cached", False),
    }
    if meta.get("userinfo"):
        fields["traffic"] = meta["userinfo"]

    # 账本收敛：以新下载内容为准，已下线/改名节点的旧行删除
    current_names = set(clash_manager.parse_node_names(
        Path(meta["path"]).read_text(encoding="utf-8", errors="ignore")
    ))
    pruned = 0
    if current_names:
        pruned = await prune_clash_node_ledger(sub_id, current_names)

        # 存活统计（账本 × 当前内容）：新下载节点无账本 → 从 0 起算
        alive = total = 0
        async with get_session_factory()() as session:
            rows = (
                await session.execute(
                    select(ClashNode).where(ClashNode.subscription_id == sub_id)
                )
            ).scalars().all()
        for row in rows:
            if row.name in current_names:
                total += 1
                if row.status == "ok":
                    alive += 1
        fields["alive"] = alive
        fields["total"] = total
    await _merge_last_stats(sub_id, **fields)

    # 自动取名（本地保存）：本行还没有名字时才写，手改过的名字不被覆盖
    if not label and meta.get("title"):
        label = await _apply_subscription_name(sub_id, str(meta["title"])) or label

    # 内核在跑且跑的就是这条订阅：配置变化自动重启生效；跑的是别的订阅
    # 或没在跑 → 只下载不重启（避免共用缓存路径把内核悄悄切到别的订阅）
    restarted = False
    detect = clash_manager.detect_kernel(settings.data_dir)
    if clash_manager.runtime.running_subscription_is(sub.url) and detect["found"]:
        try:
            prev_pid = clash_manager.runtime.process and clash_manager.runtime.process.pid
            clash_manager.runtime.start(
                detect["path"], meta["path"], restart_if_changed=True
            )
            now_pid = clash_manager.runtime.process and clash_manager.runtime.process.pid
            restarted = bool(now_pid and now_pid != prev_pid)
        except Exception:  # noqa: BLE001 —— 重启失败保留旧内核运行
            logger.exception("[订阅刷新] 内核重启失败（沿用运行中的实例）")
    if not meta.get("cached"):
        await _mark_refreshed(sub_id)
    return {
        "id": sub_id,
        "label": label,
        "nodes": meta.get("nodes"),
        "traffic": fields.get("traffic"),
        "alive": fields.get("alive", 0),
        "total": fields.get("total", 0),
        "usedCache": meta.get("cached", False),
        "restarted": restarted,
        "prunedLedger": pruned,
    }


async def maybe_refresh_active_clash_subscription() -> dict:
    """定时重拉「内核正在跑的」Clash 订阅（间隔门槛 SUBSCRIPTION_REFRESH_HOURS）。

    为什么必须定时重拉：机场节点列表会增删/改名/换入口，本地 config.yaml 不
    重拉就一直是旧节点集——6h 体检也只是反复测这批旧节点，新节点永远进不来。

    为什么只重拉正在跑的那条：
    - 没在跑的订阅没有流量走它，重拉只是白耗一次外网请求（下次启动内核时
      本来就会重下）；
    - config.yaml 是各订阅共用的缓存文件，重拉非在跑订阅会把这个文件换成
      别家内容，与运行中的内核状态对不上。

    「正在跑哪条」的判据只有一条：内核启动文本里含该订阅 URL
    （running_subscription_is）——共用缓存文件本身会被后续任何一次下载覆盖，
    文件名/时间戳都不能作为依据。判定不出来就跳过，不猜、不动。

    门槛（proxy.sub_last_refresh_at）由重拉成功时写（见 _mark_refreshed），
    失败不消费——下一拍继续试。

    返回 {state, subscriptionId, nodes, restarted}，state 取值：
    clash_not_running / throttled / no_subscription / unknown_subscription /
    refreshed / failed（非 refreshed 时后三者无意义）。restarted=true 表示
    新配置已重启内核生效——新节点在账本里还是空行，调用方应接一次节点检测，
    否则「存活 x/y」与仪表盘可用数会停在账本口径等下个 6h 窗口。
    """
    from app.domains.settings.service import get_value

    skip = {"subscriptionId": None, "nodes": None, "restarted": False}
    status = clash_manager.runtime.status()
    if not status["running"]:
        return {"state": "clash_not_running", **skip}
    last = await get_value("proxy.sub_last_refresh_at")
    if last:
        try:
            elapsed = _naive(get_beijing_time_obj()) - datetime.fromisoformat(str(last))
        except ValueError:
            elapsed = None
        if elapsed is not None and elapsed < timedelta(hours=SUBSCRIPTION_REFRESH_HOURS):
            return {"state": "throttled", **skip}

    usable = [s for s in await list_subscriptions("clash") if not s["deprecated"]]
    if not usable:
        return {"state": "no_subscription", **skip}
    # 从新到旧找「URL 出现在启动文本里」的那条（与「最近一条」默认序一致）
    sub = next(
        (
            s
            for s in reversed(usable)
            if clash_manager.runtime.running_subscription_is(s["url"])
        ),
        None,
    )
    if sub is None:
        logger.info("[订阅重拉] 无法确认内核在跑哪条订阅，跳过（可手动重拉）")
        return {"state": "unknown_subscription", **skip}
    try:
        result = await refresh_clash_subscription(sub["id"])
    except Exception as e:  # noqa: BLE001 —— 失败不消费门槛，下一拍重试
        logger.warning("[订阅重拉] 订阅 %s 拉取失败：%s", sub["id"], e)
        return {"state": "failed", **skip}
    logger.info(
        "[订阅重拉] 订阅 %s：%s 节点%s",
        sub["id"],
        result.get("nodes"),
        "，内核已重启生效" if result.get("restarted") else "（配置无变化，内核沿用）",
    )
    return {
        "state": "refreshed",
        "subscriptionId": sub["id"],
        "nodes": result.get("nodes"),
        "restarted": bool(result.get("restarted")),
    }


# ─── CRUD ────────────────────────────────────────────────────

async def list_proxies(enabled_only: bool = False) -> list[Proxy]:
    query = select(Proxy).order_by(Proxy.id)
    if enabled_only:
        query = query.where(Proxy.enabled.is_(True))
    async with get_session_factory()() as session:
        return list((await session.execute(query)).scalars())


def _proxy_dict(p: Proxy, mask_auth: bool = True) -> dict:
    return {
        "id": p.id,
        "label": p.label,
        "scheme": p.scheme,
        "host": p.host,
        "port": p.port,
        "hasAuth": bool(p.username),
        "url": p.url().replace(
            f"{p.username}:{p.password}@", f"{p.username}:****@"
        )
        if (p.username and not mask_auth)
        else (f"{p.scheme}://{p.host}:{p.port}" if not p.username else f"{p.scheme}://***@{p.host}:{p.port}"),
        "enabled": p.enabled,
        "status": p.status,
        "latencyMs": p.latency_ms,
        "consecutiveFailures": p.consecutive_failures,
        "lastCheckedAt": p.last_checked_at.isoformat() if p.last_checked_at else None,
    }


async def add_proxy(
    url: str | None = None,
    *,
    scheme: str = "http",
    host: str = "",
    port: int = 0,
    username: str | None = None,
    password: str | None = None,
    label: str | None = None,
) -> dict:
    if url:
        parsed = _parse_proxy_url(url)
        scheme, host, port, username, password = parsed
    if not host or not port:
        raise ValueError("host/port 不能为空")
    if scheme not in ("http", "socks5"):
        raise ValueError(f"不支持的协议: {scheme}")
    async with get_session_factory()() as session:
        proxy = Proxy(
            label=label, scheme=scheme, host=host, port=int(port),
            username=username, password=password, enabled=True,
            created_at=_naive(get_beijing_time_obj()),
        )
        session.add(proxy)
        await session.commit()
        return _proxy_dict(proxy)


def _parse_proxy_url(url: str) -> tuple[str, str, int, str | None, str | None]:
    """解析 scheme://user:pass@host:port 或 host:port。"""
    from urllib.parse import urlparse

    raw = url.strip()
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"无法解析代理地址: {url}")
    scheme = parsed.scheme.lower()
    if scheme == "https":
        scheme = "http"
    return (
        scheme,
        parsed.hostname,
        parsed.port,
        parsed.username,
        parsed.password,
    )


async def update_proxy(proxy_id: int, *, enabled: bool | None = None, label: str | None = None) -> dict:
    async with get_session_factory()() as session:
        proxy = await session.get(Proxy, proxy_id)
        if proxy is None:
            raise ValueError("代理不存在")
        if enabled is not None:
            proxy.enabled = enabled
            proxy.consecutive_failures = 0
        if label is not None:
            proxy.label = label
        await session.commit()
        return _proxy_dict(proxy)


async def delete_proxy(proxy_id: int) -> bool:
    async with get_session_factory()() as session:
        proxy = await session.get(Proxy, proxy_id)
        if proxy is None:
            return False
        await session.delete(proxy)
        await session.commit()
        return True


# ─── 健康检查 ────────────────────────────────────────────────

async def test_proxy(proxy_id: int) -> dict:
    async with get_session_factory()() as session:
        proxy = await session.get(Proxy, proxy_id)
        if proxy is None:
            raise ValueError("代理不存在")
        result = await _check(proxy)
        await session.commit()
        return {**_proxy_dict(proxy), "testError": result[2]}


async def test_all() -> list[dict]:
    proxies = await list_proxies()
    if not proxies:
        return []
    results = await asyncio.gather(
        *[_check_single(p) for p in proxies], return_exceptions=True
    )
    return [r for r in results if isinstance(r, dict)]


async def _check_single(proxy: Proxy) -> dict:
    async with get_session_factory()() as session:
        fresh = await session.get(Proxy, proxy.id)
        result = await _check(fresh)
        await session.commit()
        return {**_proxy_dict(fresh), "testError": result[2]}


async def _check(proxy: Proxy) -> tuple[str, int | None, str | None]:
    """对真实 Steam 端点测延迟。返回 (status, latency_ms, error)。"""
    started = time.monotonic()
    status, latency, error = "failed", None, None
    try:
        async with httpx.AsyncClient(timeout=TEST_TIMEOUT, proxy=proxy.url()) as client:
            resp = await client.get(TEST_URL, params=TEST_PARAMS)
            latency = int((time.monotonic() - started) * 1000)
            if resp.status_code == 200:
                status = "ok"
                proxy.consecutive_failures = 0
            else:
                error = f"HTTP {resp.status_code}"
                proxy.consecutive_failures += 1
    except Exception as e:  # noqa: BLE001
        latency = int((time.monotonic() - started) * 1000)
        error = f"{type(e).__name__}: {e}" if not str(e) else str(e)
        proxy.consecutive_failures += 1

    if proxy.consecutive_failures >= MAX_CONSECUTIVE_FAILURES and proxy.enabled:
        proxy.enabled = False
        logger.warning("代理 %s 连续失败 %d 次，已自动禁用", proxy_label(proxy), proxy.consecutive_failures)

    proxy.status = status
    proxy.latency_ms = latency
    proxy.last_checked_at = _naive(get_beijing_time_obj())

    await record_event(
        kind="test", target=TEST_URL, proxy_label=proxy_label(proxy),
        status_code=None, duration_ms=latency, error=error,
    )
    return status, latency, error


# ─── 订阅链接（clash / plain 双方式，长期保存在本地库）─────────

SUB_KINDS = ("clash", "plain")


async def migrate_legacy_subscription() -> None:
    """旧设置 proxy.subscription_url → proxy_subscriptions(kind=clash)，一次性。"""
    from app.domains.settings.service import get_value, set_value

    legacy = await get_value("proxy.subscription_url")
    if not legacy:
        return
    async with get_session_factory()() as session:
        exists = (
            await session.execute(
                select(ProxySubscription).where(
                    ProxySubscription.kind == "clash",
                    ProxySubscription.url == legacy.strip(),
                )
            )
        ).scalar()
        if not exists:
            session.add(
                ProxySubscription(
                    kind="clash",
                    url=legacy.strip(),
                    created_at=_naive(get_beijing_time_obj()),
                    # 旧 KV 迁移的是用户**原本就在用**的那条订阅：按 ACTIVE 兼容，
                    # 不能因为本切片把历史订阅降级成候选（那会让生产池突然空掉）。
                    admission_status=ADMISSION_ACTIVE,
                )
            )
            await session.commit()
            logger.info("已迁移旧 Clash 订阅链接到订阅表")
    await set_value("proxy.subscription_url", None)


async def list_subscriptions(kind: str | None = None) -> list[dict]:
    query = select(ProxySubscription).order_by(ProxySubscription.id)
    if kind:
        query = query.where(ProxySubscription.kind == kind)
    async with get_session_factory()() as session:
        rows = (await session.execute(query)).scalars().all()
    return [
        {
            "id": s.id,
            "kind": s.kind,
            "url": s.url,
            "label": s.label,
            "createdAt": s.created_at.isoformat() if s.created_at else None,
            "lastImportedAt": s.last_imported_at.isoformat() if s.last_imported_at else None,
            "lastStats": s.last_stats,
            "deprecated": bool(s.deprecated),
            "deprecatedAt": s.deprecated_at.isoformat() if s.deprecated_at else None,
            "deprecatedReason": s.deprecated_reason,
            # 生产准入（**仅 proxypool 的 Clash 链有语义**）：
            # - clash：NULL（ALTER 之前的历史行）与新行缺省都按候选之外的 ACTIVE 呈现，
            #   新订阅由 add_subscription 显式写成 CANDIDATE；
            # - 非 clash（plain）：没有 Candidate admission 生命周期，一律呈现 ACTIVE
            #   ——历史行哪怕曾被写成 CANDIDATE 也在这里归正，不需要数据迁移。
            "admissionStatus": (
                (s.admission_status or ADMISSION_ACTIVE)
                if s.kind == "clash"
                else ADMISSION_ACTIVE
            ),
        }
        for s in rows
    ]


async def add_subscription(kind: str, url: str, label: str | None = None) -> dict:
    """保存订阅。kind=clash 时一步到位：
    ① 内核缺失 → 自动安装（随包资产复制，缺失时才网络下载，进度可轮询）；
    ② 下载订阅验证内容可用（节点数/流量头回填，失败不删行——返回 warning）。
    """
    url = url.strip()
    if kind not in SUB_KINDS:
        raise ValueError(f"未知订阅方式: {kind}")
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("订阅链接必须是 http(s) URL")
    from app.core.config import get_settings

    settings = get_settings()
    kernel_installed = None  # None=原本就有 / dict=本次自动安装结果
    if kind == "clash" and not clash_manager.detect_kernel(settings.data_dir)["found"]:
        import asyncio

        kernel_installed = await asyncio.to_thread(
            clash_manager.install_kernel, settings.data_dir
        )
        if not kernel_installed.get("ok"):
            raise ValueError(
                f"内核安装失败：{kernel_installed.get('error')}；"
                "可稍后重试，或手动放置 mihomo 内核到 data/clash/"
            )

    async with get_session_factory()() as session:
        sub = ProxySubscription(
            kind=kind,
            url=url,
            label=label,
            created_at=_naive(get_beijing_time_obj()),
            # 生产准入（**仅 proxypool 的 Clash 链消费**）：
            # - clash 新订阅 → CANDIDATE：只抓取 + 落快照，不进 Registry/生产池；
            #   获准进生产走显式晋升（`POST /proxypool/subscriptions/{id}/promote`）；
            # - plain 新订阅 → ACTIVE：它不存在 Candidate admission 生命周期，
            #   节点直接进旧手工代理池（`import_plain_subscription`），不参与 proxypool。
            admission_status=(
                ADMISSION_CANDIDATE if kind == "clash" else ADMISSION_ACTIVE
            ),
        )
        session.add(sub)
        await session.commit()
        sub_id = sub.id
    result = {"id": sub_id, "kind": kind, "url": url, "label": label}

    # 订阅验证下载（clash：拉配置拿节点数/流量头；失败保留行，warning 带回）
    if kind == "clash":
        try:
            meta = await clash_manager.runtime.download_subscription(
                url, settings.data_dir, await _saved_proxy_candidates()
            )
            result["nodes"] = meta.get("nodes")
            result["traffic"] = meta.get("userinfo")
            result["cached"] = meta.get("cached", False)
            await _merge_last_stats(
                sub_id, nodes=meta.get("nodes"), traffic=meta.get("userinfo")
            )
            # 首次自动取名（Verge 链：profile-title → Content-Disposition →
            # URL 末段）：仅在用户没填 label 时落库，机场名可见但不抢
            # 手动命名权（_apply_subscription_name 保证已有名称不覆写）。
            if not label and meta.get("title"):
                named = await _apply_subscription_name(sub_id, str(meta["title"]))
                if named:
                    result["label"] = named
        except Exception as e:  # noqa: BLE001 —— 验证失败不撤销保存
            result["warning"] = f"订阅已保存，但下载验证失败：{e}（可稍后重拉）"
    if kernel_installed is not None:
        result["kernelInstalled"] = True
        result["kernelVersion"] = clash_manager.kernel_version(kernel_installed["path"])
    return result


async def delete_subscription(sub_id: int) -> bool:
    """删除订阅 + 级联清理账本（clash_nodes 按 subscription_id 挂靠，
    订阅没了节点行就是纯孤儿数据，一并删除防止永久累积）。"""
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return False
        await session.execute(
            delete(ClashNode).where(ClashNode.subscription_id == sub_id)
        )
        await session.delete(sub)
        await session.commit()
        return True


async def get_subscription(sub_id: int) -> ProxySubscription | None:
    async with get_session_factory()() as session:
        return await session.get(ProxySubscription, sub_id)


async def mark_imported(sub_id: int, stats: dict) -> None:
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return
        sub.last_imported_at = _naive(get_beijing_time_obj())
        sub.last_stats = stats
        await session.commit()


def parse_plain_proxy_line(line: str) -> dict | None:
    """解析明文代理行，支持常见商业代理池格式：
    scheme://user:pass@host:port | host:port | host:port:user:pass | user:pass@host:port"""
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" in raw:
        scheme, host, port, username, password = _parse_proxy_url(raw)
        return {"scheme": scheme, "host": host, "port": port, "username": username, "password": password}
    # user:pass@host:port
    if "@" in raw:
        auth, _, addr = raw.rpartition("@")
        user, _, pwd = auth.partition(":")
        host, _, port = addr.partition(":")
        if host and port.isdigit():
            return {"scheme": "http", "host": host, "port": int(port), "username": user or None, "password": pwd or None}
        return None
    parts = raw.split(":")
    if len(parts) == 2 and parts[1].isdigit():
        return {"scheme": "http", "host": parts[0], "port": int(parts[1]), "username": None, "password": None}
    if len(parts) == 4 and parts[1].isdigit():
        return {"scheme": "http", "host": parts[0], "port": int(parts[1]), "username": parts[2], "password": parts[3]}
    return None


async def import_plain_subscription(sub_id: int, timeout: float = 30.0) -> dict:
    """拉取明文代理订阅并导入节点池（按 host:port:user 去重）。"""
    sub = await get_subscription(sub_id)
    if sub is None:
        raise ValueError("订阅不存在")
    if sub.kind != "plain":
        raise ValueError("该订阅不是明文代理订阅（kind=plain）")
    # 订阅源直连大多被墙：代理优先（内核在跑走内核，其次池，最后直连）
    proxy = None
    try:
        proxy = await resolve_proxy_url()
    except Exception:  # noqa: BLE001
        proxy = None
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, proxy=proxy) as client:
        resp = await client.get(sub.url)
        resp.raise_for_status()
        text = resp.text

    parsed = []
    for line in text.splitlines():
        item = parse_plain_proxy_line(line)
        if item:
            parsed.append(item)
    if not parsed:
        raise ValueError("订阅内容没有可解析的代理行")

    existing = {(p.host, p.port, p.username or "") for p in await list_proxies()}
    existing_before_add = set(existing)
    added = skipped = 0
    async with get_session_factory()() as session:
        for item in parsed:
            key = (item["host"], item["port"], item["username"] or "")
            if key in existing:
                skipped += 1
                continue
            existing.add(key)
            session.add(
                Proxy(
                    label=item["username"] and f"{item['host']}:{item['port']}({item['username']})" or None,
                    scheme=item["scheme"], host=item["host"], port=item["port"],
                    username=item["username"], password=item["password"],
                    enabled=True, created_at=_naive(get_beijing_time_obj()),
                )
            )
            added += 1
        await session.commit()

    # ── 入库即体检（导入后自动校验标记可用节点）──
    # 只测本次新增行（存量行沿用既有 status，重测留给 6h 体检）；全失败也
    # 保留数据——enabled 由 _check 连败计数器自然处置，不在导入里删行。
    stats: dict = {"fetched": len(parsed), "added": added, "skipped": skipped}
    if added:
        new_keys = existing - existing_before_add
        fresh = [
            p for p in await list_proxies()
            if (p.host, p.port, p.username or "") in new_keys
        ]
        results = await asyncio.gather(
            *[_check_single(p) for p in fresh], return_exceptions=True
        )
        checked = [r for r in results if isinstance(r, dict)]
        stats["checked"] = len(checked)
        stats["alive"] = sum(1 for r in checked if r.get("status") == "ok")
        logger.info(
            "[导入体检] plain:%s 新增 %s 条，实测通过 %s/%s",
            sub_id, added, stats["alive"], stats["checked"],
        )
    await mark_imported(sub_id, stats)
    await record_event(
        kind="import", target=sub.url, proxy_label=f"plain:{sub_id}", error=None
    )
    return stats


# ─── Clash 节点检测（出口 IP 去重 + 存活）─────────────────────

_EXIT_IP_URLS = [
    "http://ip-api.com/json/?fields=query",
    "https://api.ip.sb/ip",
]


async def _fetch_exit_ip(client: httpx.AsyncClient) -> str | None:
    for url in _EXIT_IP_URLS:
        try:
            resp = await client.get(url, timeout=10)
            if resp.status_code == 200:
                text = resp.text.strip()
                # ip-api 返回 JSON；api.ip.sb 返回纯文本
                if text.startswith("{"):
                    match = re.search(r'"query"\s*:\s*"([\d.a-fA-F:]+)"', text)
                    if match:
                        return match.group(1)
                return text.strip('" \n')
        except Exception:  # noqa: BLE001
            continue
    return None


async def _active_clash_subscription_id(status: dict) -> int | None:
    """内核正在跑的订阅 id：配置文件里的订阅 URL 匹配 > 最近一条 clash 订阅。

    节点检测与统计（pool_stats）共用——「当前订阅」只有这一个定义。
    """
    async with get_session_factory()() as session:
        subs = (
            await session.execute(
                select(ProxySubscription)
                .where(ProxySubscription.kind == "clash")
                .order_by(ProxySubscription.id)
            )
        ).scalars().all()
    # 内核正在跑的配置文件即上次启动的订阅，按 URL 匹配
    sub_url_hint = None
    if status.get("configPath"):
        try:
            from pathlib import Path

            text = Path(status["configPath"]).read_text(encoding="utf-8", errors="ignore")
            for line in text.splitlines():
                s = line.strip()
                if s.startswith(("http://", "https://")) and "://" in s and not s.startswith(("http://127", "https://127")):
                    sub_url_hint = s.rstrip("\"'")
                    break
        except Exception:  # noqa: BLE001
            sub_url_hint = None
    if sub_url_hint:
        for s in subs:
            if s.url.strip() == sub_url_hint:
                return s.id
    return subs[-1].id if subs else None


async def test_clash_nodes(subscription_id: int | None = None) -> dict:
    """检测 Clash 订阅节点：逐个切换 selector，经混合端口探测。

    - 存活判定 = Steam 端点 HTTP 200（与手动代理池同款探测目标；
      ip-api 通 ≠ Steam 通，Steam 侧风控/路由差异只有真实端点能暴露）
    - 出口 IP 仅用于去重：Steam API 不回显出口 IP（无 Set-Cookie/IP 头），
      与存活探测并发执行不增耗时；出口 IP 相同 = 同落地（机场多入口
      同落地很常见），duplicate 标记
    - 节点状态机（clash_nodes 表）：ok → 失败冷却递增（30min→24h 封顶）、
      累计 10 次 → dead 终态；dead 复活需本次连续 3-of-3 全过；
      冷却期内跳过检测（不算死也不耗探测时间）
    - 订阅废弃判定：本次实检节点中不可用占比 >95% → 订阅 deprecated
      （后端不再选用；不删除，用户手动删）；恢复达标自动解除
    - selector 自愈：检测后若当前选中节点已死 → 自动切最快健康节点
    - 串行锁 _clash_test_lock：手动检测/启动首检/定时体检三源互斥——
      并发切 selector 会互踩（节点测出假延迟/假死）
    - 返回 {total, alive, aliveUnique, nodes, subscriptionId, deprecated}
      （ms 为 Steam 请求延迟，不再混入 IP 探测耗时）
    """
    async with _clash_test_lock():
        status = clash_manager.runtime.status()
        if not status["running"]:
            raise ValueError("Clash 未运行：请先在 Clash 接入选择订阅并启动")
        base = status["controllerUrl"]
        secret = clash_manager.runtime.secret
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        mixed_port = status["port"]

        # 本次检测归属的订阅：显式指定 > 内核配置文件对应 > 最近一条 clash 订阅
        sub_id = subscription_id
        if sub_id is None:
            sub_id = await _active_clash_subscription_id(status)
            if sub_id is None:
                raise ValueError("无 Clash 订阅记录：请先在订阅区保存订阅")

        async with httpx.AsyncClient(timeout=10, headers=headers) as ctl:
            resp = await ctl.get(f"{base}/proxies")
            resp.raise_for_status()
            all_proxies = resp.json().get("proxies", {})

        # 真实节点：来自配置文件的 proxies 段（排除内置与策略组）
        config_text = ""
        if status.get("configPath"):
            from pathlib import Path

            config_text = Path(status["configPath"]).read_text(encoding="utf-8", errors="ignore")
        node_names = clash_manager.parse_node_names(config_text)
        if not node_names:
            # 订阅用 proxy-providers 时退回控制器类型过滤
            node_names = [
                name for name, info in all_proxies.items()
                if str(info.get("type", "")).lower() not in clash_manager._NON_NODE_TYPES
                and name not in ("DIRECT", "REJECT", "GLOBAL", "COMPATIBLE", "Pass")
            ]

        # 节点状态账本：冷却期内跳过检测（结果沿用账本 status）
        ledger = await _load_clash_node_ledger(sub_id, node_names)
        to_probe = [n for n in node_names if not _in_cooldown(ledger.get(n))]
        now = _naive(get_beijing_time_obj())

        # 找一个可控 selector（优先名字含 GLOBAL/PROXY/选择 的组）
        groups = [
            info for info in all_proxies.values()
            if str(info.get("type", "")).lower() in ("selector", "fallback", "urltest")
        ]

        def _group_score(info: dict) -> int:
            name = str(info.get("name", ""))
            for i, kw in enumerate(("GLOBAL", "PROXY", "节点", "选择", "手动")):
                if kw in name:
                    return i
            return 99

        groups.sort(key=_group_score)
        selector = groups[0]["name"] if groups else "GLOBAL"

        async def _probe_steam(client: httpx.AsyncClient) -> bool:
            """Steam 端点存活探测（与手动代理池 _check 同一端点参数）。"""
            try:
                resp = await client.get(TEST_URL, params=TEST_PARAMS, timeout=TEST_TIMEOUT)
                return resp.status_code == 200
            except Exception:  # noqa: BLE001
                return False

        # rule 模式下流量按规则组走（不经过 GLOBAL），切 GLOBAL 无效——
        # 检测期间临时切 global 模式让 GLOBAL 成为总闸，测完恢复原模式。
        async with httpx.AsyncClient(timeout=8, headers=headers) as ctl:
            prev_mode = None
            try:
                mode_resp = await ctl.get(f"{base}/configs")
                if mode_resp.status_code == 200:
                    prev_mode = (mode_resp.json() or {}).get("mode")
                    if prev_mode != "global":
                        await ctl.patch(f"{base}/configs", json={"mode": "global"})
            except Exception:  # noqa: BLE001 —— 模式切换失败则按原模式尽力检测
                prev_mode = None

            results: list[dict] = []
            seen_exit_ips: dict[str, str] = {}
            try:
                for name in to_probe:
                    # 切换 selector 到该节点
                    try:
                        put = await ctl.put(
                            f"{base}/proxies/{quote(selector, safe='')}",
                            json={"name": name},
                        )
                        if put.status_code >= 400:
                            results.append({"name": name, "alive": False, "steamOk": False, "exitIp": None, "ms": None, "duplicate": False, "probed": False})
                            continue
                    except Exception:  # noqa: BLE001
                        results.append({"name": name, "alive": False, "steamOk": False, "exitIp": None, "ms": None, "duplicate": False, "probed": False})
                        continue

                    started = time.monotonic()
                    async with httpx.AsyncClient(
                        timeout=TEST_TIMEOUT, proxy=f"http://127.0.0.1:{mixed_port}"
                    ) as via:
                        # 存活判定（Steam）与出口 IP 探测并发：IP 仅用于同落地
                        # 去重，不阻塞也不参与 alive 判定
                        steam_ok, exit_ip = await asyncio.gather(
                            _probe_steam(via), _fetch_exit_ip(via)
                        )
                    ms = int((time.monotonic() - started) * 1000)
                    alive = steam_ok
                    duplicate = bool(exit_ip and exit_ip in seen_exit_ips)
                    if exit_ip and not duplicate:
                        seen_exit_ips[exit_ip] = name
                    results.append(
                        {"name": name, "alive": alive, "steamOk": steam_ok, "exitIp": exit_ip, "ms": ms, "duplicate": duplicate, "probed": True}
                    )
            finally:
                # 切回健康节点（selector 自愈见 _apply_node_results）+ 恢复原模式
                if prev_mode is not None and prev_mode != "global":
                    try:
                        await ctl.patch(f"{base}/configs", json={"mode": prev_mode})
                    except Exception:  # noqa: BLE001
                        pass
                self_heal_target = None  # 占位：健康节点选择在落库后进行
                _selector_ctl = ctl  # 供落库后自愈复用连接

        # ── 状态机落库 + dead 三连复活 + 冷却递增 ──
        alive_count, unique_alive, deprecated = await _apply_node_results(
            sub_id, ledger, results, node_names, now
        )

        # ── selector 自愈：当前选中节点已死 → 切最快健康节点 ──
        try:
            healthy = sorted(
                (r for r in results if r["alive"] and r["ms"] is not None),
                key=lambda r: r["ms"],
            )
            current = (all_proxies.get(selector) or {}).get("now")
            current_alive = any(r["name"] == current and r["alive"] for r in results) or (
                ledger.get(current) and ledger[current].get("status") == "ok" and not _in_cooldown(ledger.get(current))
            )
            if healthy and not current_alive:
                self_heal_target = healthy[0]["name"]
                async with httpx.AsyncClient(timeout=8, headers=headers) as ctl:
                    await ctl.put(
                        f"{base}/proxies/{quote(selector, safe='')}",
                        json={"name": self_heal_target},
                    )
                logger.info("[Clash自愈] 选中节点 %s 已死，切换到最快健康节点 %s", current, self_heal_target)
        except Exception:  # noqa: BLE001 —— 自愈失败不影响检测结果
            logger.warning("[Clash自愈] 切换健康节点失败（不阻塞检测）")

        return {
            "total": len(node_names),
            "probed": len(results),
            "alive": alive_count,
            "aliveUnique": unique_alive,
            "selector": selector,
            "subscriptionId": sub_id,
            "deprecated": deprecated,
            "nodes": [r for r in results] + _cooldown_nodes_payload(ledger, node_names, {r["name"] for r in results}),
        }


async def prune_clash_node_ledger(sub_id: int, current_names: set[str] | None = None) -> int:
    """账本收敛：删除已不在订阅当前内容里的节点行（改名/下架的历史痕迹）。

    之前「保留不删」的设计在改名即新行的现实下会永久累积；改为每次
    订阅刷新/检测时按当前内容收敛——改名节点 = 旧行删除 + 新行从零
    开始（诚实反映「这是新节点」，绕过状态机的收益只剩一次性未知态）。
    current_names 缺省时从内核当前配置文件解析；空集防呆不删
    （解析不到节点宁可不裁，全删等于销账）。
    """
    if current_names is None:
        status = clash_manager.runtime.status()
        if not status.get("configPath") or not Path(status["configPath"]).is_file():
            return 0
        text = Path(status["configPath"]).read_text(encoding="utf-8", errors="ignore")
        current_names = set(clash_manager.parse_node_names(text))
    if not current_names:
        return 0
    async with get_session_factory()() as session:
        result = await session.execute(
            delete(ClashNode).where(
                ClashNode.subscription_id == sub_id,
                ClashNode.name.notin_(current_names),
            )
        )
        await session.commit()
        return result.rowcount or 0


async def _load_clash_node_ledger(sub_id: int, node_names: list[str]) -> dict[str, dict]:
    """读取节点状态账本（clash_nodes 表），返回 {节点名: 状态dict}。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(ClashNode).where(
                    ClashNode.subscription_id == sub_id,
                    ClashNode.name.in_(node_names),
                )
            )
        ).scalars().all()
        return {
            n.name: {
                "status": n.status,
                "fail_count": n.fail_count,
                "revive_passes": n.revive_passes,
                "latency_ms": n.latency_ms,
                "exit_ip": n.exit_ip,
                "last_checked_at": n.last_checked_at,
                "cooldown_until": n.cooldown_until,
            }
            for n in rows
        }


def _in_cooldown(ledger_entry: dict | None) -> bool:
    """节点是否处于冷却期（冷却到点 = 该测了，返回 False）。"""
    if not ledger_entry or not ledger_entry.get("cooldown_until"):
        return False
    until = ledger_entry["cooldown_until"]
    return until > _naive(get_beijing_time_obj())


def _cooldown_nodes_payload(
    ledger: dict[str, dict], node_names: list[str], probed: set[str]
) -> list[dict]:
    """冷却跳过节点的结果行（沿用账本状态，标注 cooling）。"""
    payload = []
    for name in node_names:
        if name in probed:
            continue
        entry = ledger.get(name)
        if entry is None:
            continue
        payload.append(
            {
                "name": name,
                "alive": entry.get("status") == "ok",
                "steamOk": entry.get("status") == "ok",
                "exitIp": entry.get("exit_ip"),
                "ms": entry.get("latency_ms"),
                "duplicate": False,
                "probed": False,
                "cooling": True,
            }
        )
    return payload


async def _apply_node_results(
    sub_id: int,
    ledger: dict[str, dict],
    results: list[dict],
    node_names: list[str],
    now: datetime,
) -> tuple[int, int, bool]:
    """探测结果写入 clash_nodes 账本 + 订阅废弃判定。

    状态机：
    - 通过：fail_count 清零、revive_passes 累计（dead 复活计数），
      3-of-3 连过 → status=ok 复活（revive_passes 归零）；非 dead 直接 ok
    - 失败：fail_count+1；累计 < DEAD_MAX_FAILS → 冷却递增；
      累计 ≥ DEAD_MAX_FAILS → status=dead（终态，仅 3-of-3 复活）
    - 账本里有但订阅已无此节点（换订阅内容）→ 保留不删（历史痕迹）
    返回 (alive_count, unique_alive, deprecated)。
    """
    alive_count = 0
    unique_ips: set[str] = set()

    async with get_session_factory()() as session:
        # 节点存在性以订阅当前内容为准：新节点 INSERT，已有节点 UPDATE
        existing = {
            n.name: n
            for n in (
                await session.execute(
                    select(ClashNode).where(ClashNode.subscription_id == sub_id)
                )
            ).scalars()
        }

        for r in results:
            if not r.get("probed"):
                continue  # selector 切换失败的节点不入账本
            name = r["name"]
            row = existing.get(name)
            if row is None:
                row = ClashNode(
                    subscription_id=sub_id, name=name, created_at=now,
                )
                session.add(row)

            row.exit_ip = r.get("exitIp")
            row.last_checked_at = now
            row.latency_ms = r.get("ms")

            if r["alive"]:
                alive_count += 1
                if r.get("exitIp"):
                    unique_ips.add(r["exitIp"])
                row.fail_count = 0
                row.cooldown_until = None
                if row.status == "dead":
                    row.revive_passes = (row.revive_passes or 0) + 1
                    if row.revive_passes >= REVIVE_PASSES_REQUIRED:
                        row.status = "ok"
                        row.revive_passes = 0
                        logger.info("[Clash节点] %s 连续 %d 次通过，复活为 ok", name, REVIVE_PASSES_REQUIRED)
                else:
                    row.status = "ok"
            else:
                row.fail_count = (row.fail_count or 0) + 1
                row.revive_passes = 0  # 复活连测被失败打断，重新计数
                if row.fail_count >= DEAD_MAX_FAILS:
                    row.status = "dead"
                    row.cooldown_until = None  # 终态不再冷却轮转（3-of-3 复活走连续体检）
                else:
                    # 冷却递增：第 n 次失败 → 阶梯第 min(n, len) 档
                    tier = min(row.fail_count, len(_FAIL_COOLDOWN_MINUTES)) - 1
                    cooldown_min = _FAIL_COOLDOWN_MINUTES[tier]
                    row.cooldown_until = now + timedelta(minutes=cooldown_min)
                    row.status = "unknown" if row.status == "unknown" else row.status  # 保留 ok（冷却后复检）

        await session.commit()

    # 冷却跳过的节点按账本口径计入存活统计（上次判定为准）
    probed_names = {r["name"] for r in results}
    for name in node_names:
        if name in probed_names:
            continue
        entry = ledger.get(name)
        if entry and entry.get("status") == "ok":
            alive_count += 1
            if entry.get("exit_ip"):
                unique_ips.add(entry["exit_ip"])

    unique_alive = len(unique_ips)
    deprecated = await _evaluate_subscription_deprecation(sub_id, alive_count, len(node_names))
    # 账本收敛：已不在订阅内容里的节点行随检测清理（检测读的就是当前配置）
    pruned = await prune_clash_node_ledger(sub_id, set(node_names))
    # 存活统计落库——订阅行「存活 x/y」标签的数据源（此前前端等这个字段）
    await _merge_last_stats(sub_id, alive=alive_count, total=len(node_names))
    if pruned:
        logger.info("[Clash节点] 账本收敛：订阅 %d 删除 %d 个已下线节点行", sub_id, pruned)

    await record_event(
        kind="clash", target="node-test",
        proxy_label=f"clash:{sub_id}",
        error=None if alive_count else "全部节点不可用",
    )
    return alive_count, unique_alive, deprecated


async def _merge_last_stats(sub_id: int, **fields) -> None:
    """订阅 last_stats 局部合并（不动其他键）——流量/存活回填共用出口。"""
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return
        stats = dict(sub.last_stats or {})
        stats.update(fields)
        sub.last_stats = stats
        await session.commit()


async def _evaluate_subscription_deprecation(sub_id: int, alive_count: int, total: int) -> bool:
    """订阅废弃判定：不可用占比 >95% → deprecated（后端不再选用，不删除）。

    恢复达标（不可用 ≤95%）自动解除——废弃是"当前不值得用"的标记，
    不是死亡判决。total 为 0（空订阅）不判定。
    """
    if total <= 0:
        return False
    unusable_ratio = 1 - (alive_count / total)
    async with get_session_factory()() as session:
        sub = await session.get(ProxySubscription, sub_id)
        if sub is None:
            return False
        if unusable_ratio > SUBSCRIPTION_DEPRECATE_RATIO:
            if not sub.deprecated:
                sub.deprecated = True
                sub.deprecated_at = _naive(get_beijing_time_obj())
                sub.deprecated_reason = f"不可用节点占比 {unusable_ratio:.0%}（可用 {alive_count}/{total}）"
                await session.commit()
                logger.warning(
                    "[Clash订阅] 订阅 %d 废弃：可用 %d/%d（%.0f%%），后端不再选用",
                    sub_id, alive_count, total, unusable_ratio * 100,
                )
            return True
        if sub.deprecated:
            sub.deprecated = False
            sub.deprecated_at = None
            sub.deprecated_reason = None
            await session.commit()
            logger.info(
                "[Clash订阅] 订阅 %d 恢复可用（%d/%d），解除废弃标记", sub_id, alive_count, total
            )
        return False


async def maybe_run_clash_health_check(force: bool = False) -> str:
    """体检门槛：距上次全量节点检测 ≥6h 才真跑（force 跳过门槛）。

    本地软件不常驻运行——APScheduler 间隔只是兜底，真正的节流靠
    clash_nodes 账本的 last_checked_at（跨重启有效）。返回执行状态。
    """
    if not clash_manager.runtime.status()["running"]:
        return "clash_not_running"
    async with get_session_factory()() as session:
        row = await session.execute(select(func.max(ClashNode.last_checked_at)))
        latest = row.scalar()
    if not force and latest is not None:
        elapsed = _naive(get_beijing_time_obj()) - latest
        if elapsed < timedelta(hours=HEALTH_INTERVAL_HOURS):
            return "throttled"
    try:
        await test_clash_nodes()
        return "checked"
    except ValueError:
        return "skipped"
    except Exception:  # noqa: BLE001
        logger.exception("[体检] Clash 节点检测失败")
        return "failed"


# ─── 统计（仪表盘口径：一个出口 IP 算一个代理）────────────────

async def pool_stats() -> dict:
    """代理可用性统计——按「一个出口 IP 算一个代理」口径。

    - 手动池：每条 host:port 就是一个代理；可用 = 启用且最近检测 ok
    - Clash：机场多入口常同落地，按出口 IP 去重（与节点检测的
      aliveUnique 同口径）；且只统计内核正在跑的当前订阅——内核没跑，
      账本再健康也没有流量走得到
    """
    async with get_session_factory()() as session:
        pool_rows = list((await session.execute(select(Proxy))).scalars())
    pool_total = len(pool_rows)
    pool_ok = sum(1 for p in pool_rows if p.enabled and p.status == "ok")

    status = clash_manager.runtime.status()
    running = bool(status["running"])
    clash: dict = {
        "running": running,
        "subscriptionId": None,
        "nodes": 0,
        "okNodes": 0,
        "exitIps": 0,
        "okExitIps": 0,
    }
    if running:
        sub_id = await _active_clash_subscription_id(status)
        clash["subscriptionId"] = sub_id
        if sub_id is not None:
            async with get_session_factory()() as session:
                rows = list(
                    (
                        await session.execute(
                            select(ClashNode).where(ClashNode.subscription_id == sub_id)
                        )
                    ).scalars()
                )
            # 只统计当前订阅内容里仍在的节点（换订阅后账本保留历史行不删）
            current_names: set[str] | None = None
            if status.get("configPath"):
                try:
                    from pathlib import Path

                    text = Path(status["configPath"]).read_text(
                        encoding="utf-8", errors="ignore"
                    )
                    names = clash_manager.parse_node_names(text)
                    if names:
                        current_names = set(names)
                except Exception:  # noqa: BLE001
                    current_names = None
            if current_names is not None:
                rows = [r for r in rows if r.name in current_names]

            all_idents: set[str] = set()
            ok_idents: set[str] = set()
            for r in rows:
                # 出口 IP 未知的节点（IP 探测失败）各自算一个
                ident = r.exit_ip or f"node:{r.id}"
                all_idents.add(ident)
                if r.status == "ok":
                    clash["okNodes"] += 1
                    ok_idents.add(ident)
            clash["nodes"] = len(rows)
            clash["exitIps"] = len(all_idents)
            clash["okExitIps"] = len(ok_idents)

    return {
        "pool": {"total": pool_total, "ok": pool_ok},
        "clash": clash,
        "available": pool_ok + clash["okExitIps"],
        "total": pool_total + clash["exitIps"],
    }


# ─── 走线日志 ────────────────────────────────────────────────

async def record_event(
    *, kind: str, target: str | None, proxy_label: str | None,
    status_code: int | None = None, duration_ms: int | None = None, error: str | None = None,
) -> None:
    try:
        async with get_session_factory()() as session:
            session.add(
                ProxyEvent(
                    ts=_naive(get_beijing_time_obj()), kind=kind, target=target,
                    proxy_label=proxy_label, status_code=status_code,
                    duration_ms=duration_ms, error=error,
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001
        logger.exception("记录走线日志失败")


async def recent_events(limit: int = 200) -> list[dict]:
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(ProxyEvent).order_by(ProxyEvent.id.desc()).limit(limit)
            )
        ).scalars().all()
    return [
        {
            "id": e.id,
            "ts": e.ts.isoformat() if e.ts else None,
            "kind": e.kind,
            "target": e.target,
            "proxyLabel": e.proxy_label,
            "statusCode": e.status_code,
            "durationMs": e.duration_ms,
            "error": e.error,
        }
        for e in rows
    ]
