"""系统诊断与联网工具：任务失败 / 代理池 / 出口明细 / 延迟检测 / 写闸 / 联网搜索 / 价格补抓 / 测试邮件 / 日志读取 / 诊断证据面 / 订阅同步。"""
from __future__ import annotations

import asyncio
import re
import time

import aiohttp

from app.core.database import write_scheduler_diagnostics
from app.core.logging import TAG_SEVERITY, ring_log_handler
from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.alerts.notify import get_smtp_config, send_test_mail
from app.domains.crawl import service as crawl_service
from app.domains.games import service as games_service
from app.domains.proxies import service as proxies_service


async def recent_job_failures() -> dict:
    """最近失败的抓取任务与原因（crawl_jobs 账本只读投影）。"""
    jobs = await crawl_service.list_jobs(20)
    failed = [j for j in jobs if j.get("status") == "failed"]
    rows = [
        {
            "k": f"#{j.get('id')} {j.get('kind') or ''}".strip(),
            "v": str(j.get("error") or "")[:80],
            "at": j.get("startedAt"),
            "tone": "bad",
        }
        for j in failed
    ]
    return _shared.rows_result("jobs", rows, total=len(failed))


async def proxy_pool_status() -> dict:
    """代理通道可用状态（手动池 + Clash 出口，pool_stats 只读投影）。"""
    stats = await proxies_service.pool_stats()
    pool = stats.get("pool") or {}
    clash = stats.get("clash") or {}
    rows = [
        {"k": "", "vKey": "proxyPool",
         "data": {"ok": pool.get("ok", 0), "total": pool.get("total", 0)},
         "tone": "ok" if pool.get("ok") else "warn"},
        {"k": "", "vKey": "proxyExits",
         "data": {"ok": clash.get("okExitIps", 0), "total": clash.get("exitIps", 0)},
         "tone": "ok" if clash.get("okExitIps") else "warn"},
        {"k": "", "vKey": "proxyRunning" if clash.get("running") else "proxyStopped",
         "tone": "ok" if clash.get("running") else "warn"},
    ]
    return _shared.rows_result("proxy", rows)


async def proxy_exit_detail() -> dict:
    """出口节点明细（clash_nodes 账本只读投影）：逐节点状态/延迟/出口 IP。

    只读最近一次检测留下的账本事实，不发起探测；存活节点按延迟升序在前。"""
    overview = await proxies_service.clash_nodes_overview()
    if not overview.get("running"):
        return _shared.rows_result("proxyExits", [{"k": "", "vKey": "proxyStopped", "tone": "warn"}])
    nodes = overview.get("nodes") or []
    rows = [
        {
            "k": str(n.get("name") or ""),
            "v": " · ".join(bits) if (bits := [b for b in (
                str(n.get("exit_ip") or ""),
                f"{n['latency_ms']}ms" if n.get("latency_ms") is not None else "",
            ) if b]) else "",
            "tone": "ok" if n.get("status") == "ok" else "bad",
        }
        for n in nodes
    ]
    return _shared.rows_result("proxyExits", rows, total=len(rows))


# 出口延迟检测的轮询窗口（秒）：略低于 projection 里给该工具的执行预算，
# 超窗未收口按超时行返回——由模型决定是否稍后重试
_PROXY_TEST_WAIT_S = 170.0
_PROXY_TEST_POLL_S = 2.0


async def proxy_latency_test() -> dict:
    """发起一次全量出口延迟检测并等到收口（与手动检测/首检同一会话）。

    返回逐节点 存活/延迟/出口 IP；存活按延迟升序在前，方便模型直接引用
    「最快的前几个」。检测进行中重复调用会复用在飞会话。"""
    snap = proxies_service.clash_test_start()
    deadline = time.monotonic() + _PROXY_TEST_WAIT_S
    while snap and snap.get("phase") in ("queued", "running") and time.monotonic() < deadline:
        await asyncio.sleep(_PROXY_TEST_POLL_S)
        snap = proxies_service.clash_test_progress() or snap
    if not snap:
        return _shared.rows_result("proxyTest", [{"k": "", "vKey": "proxyStopped", "tone": "warn"}])
    if snap.get("phase") not in ("done", "failed"):
        return _shared.rows_result("proxyTest", [{"k": "", "vKey": "proxyTestPending", "tone": "warn"}])
    if snap.get("phase") == "failed":
        return _shared.rows_result("proxyTest", [{"k": "", "v": str(snap.get("error") or "")[:80], "tone": "bad"}])
    probed = [n for n in (snap.get("nodes") or []) if n.get("probed")]
    probed.sort(key=lambda n: (not n.get("alive"),
                               n.get("ms") if n.get("ms") is not None else 1 << 30))
    alive = sum(1 for n in probed if n.get("alive"))
    rows: list[dict] = [{"k": "", "v": f"{alive}/{len(probed)}", "tone": "ok" if alive else "bad"}]
    rows.extend({
        "k": str(n.get("name") or ""),
        "v": (f"{n.get('exitIp') or '-'} · {n.get('ms')}ms" if n.get("alive") else ""),
        "tone": "ok" if n.get("alive") else "bad",
    } for n in probed)
    return _shared.rows_result("proxyTest", rows, total=len(probed))


async def write_gate_status() -> dict:
    """写入调度状态：有没有写入在执行、排队多少（诊断第③层出口）。"""
    d = write_scheduler_diagnostics()
    waiting = int(d.get("waiting_interactive") or 0) + int(d.get("waiting_background") or 0)
    rows = [
        {"k": "", "vKey": "gateBusy" if d.get("busy") else "gateIdle",
         "v": str(d.get("owner_label") or ""),
         "tone": "warn" if d.get("busy") else "ok"},
        {"k": "", "vKey": "gateWaiting", "data": {"n": waiting},
         "tone": "warn" if waiting else "ok"},
    ]
    return _shared.rows_result("gate", rows)


_DDG_RESULT_RE = re.compile(
    r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?'
    r'class="result__snippet"[^>]*>(.*?)</a>',
    re.S,
)


def _parse_ddg(html: str, limit: int = 5) -> list[dict]:
    """DuckDuckGo HTML 结果解析（纯函数）：标题/链接/摘要，纯文本化。"""
    out: list[dict] = []
    for m in _DDG_RESULT_RE.finditer(html or ""):
        href = m.group(1)
        if "uddg=" in href:
            from urllib.parse import parse_qs, unquote, urlparse
            qs = parse_qs(urlparse(href.replace("&amp;", "&")).query)
            href = unquote(qs.get("uddg", [""])[0])
        text = re.sub(r"<[^>]+>", "", m.group(2))
        snippet = re.sub(r"<[^>]+>", "", m.group(3))
        if not href or not text.strip():
            continue
        out.append({"title": text.strip()[:120], "url": href[:300], "snippet": snippet.strip()[:160]})
        if len(out) >= limit:
            break
    return out


async def web_search(query: str) -> dict:
    """互联网搜索（DuckDuckGo HTML 端点，免密钥）。只外发搜索词本身，不带任何本地信息。"""
    term = " ".join((query or "").split())[:200]
    if not term:
        return {"kind": "empty", "note": "bad_query"}
    try:
        proxy = await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001 — 出口解析失败按无代理直试
        proxy = None
    html = ""
    try:
        async with aiohttp.ClientSession(
            connector=aiohttp.TCPConnector(limit=2, ttl_dns_cache=60),
            timeout=aiohttp.ClientTimeout(total=12),
        ) as session:
            async with session.get(
                "https://html.duckduckgo.com/html/",
                params={"q": term},
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"},
                proxy=proxy,
            ) as resp:
                if resp.status == 200:
                    html = await resp.text()
    except (aiohttp.ClientError, asyncio.TimeoutError):
        html = ""
    rows = [
        {"k": r["title"], "v": r["snippet"][:80]}
        for r in _parse_ddg(html)
    ]
    return _shared.rows_result("web", rows, total=len(rows))


async def refresh_game_price(appid: int) -> dict:
    """单游戏价格补抓：手动 job 挂后台执行，完成自动入库。"""
    names = await games_service.names_for([appid])
    name = names.get(appid, f"AppID {appid}")
    try:
        await crawl_service.start_job(scope="appids", appids=[appid], kind="manual")
    except RuntimeError:
        return _shared.rows_result("refresh", [{"k": name, "vKey": "refreshBusy", "tone": "warn"}])
    except ValueError:
        return _shared.rows_result("refresh", [{"k": name, "vKey": "refreshRejected", "tone": "warn"}])
    return _shared.rows_result("refresh", [{"k": name, "vKey": "refreshStarted", "tone": "ok"}])


async def notify_test() -> dict:
    """发一封连通性测试邮件（只走一次 SMTP，不碰业务数据）。"""
    cfg = await get_smtp_config()
    if not all(cfg.get(k) for k in ("host", "user", "password", "to_addr")):
        return _shared.rows_result("notify", [{
            "k": "", "vKey": "notifyNoConfig", "tone": "warn",
        }])
    try:
        await send_test_mail(
            host=cfg["host"],
            port=int(cfg["port"]),
            user=cfg["user"],
            password=cfg["password"],
            to_addr=cfg["to_addr"],
            use_ssl=bool(cfg["use_ssl"]),
        )
    except ValueError as e:
        return _shared.rows_result("notify", [{"k": "", "vKey": "notifyFailed", "v": str(e)[:80], "tone": "bad"}])
    return _shared.rows_result("notify", [{"k": "", "vKey": "notifySent", "tone": "ok"}])


# ─────────────────────── 感知面：日志 / 诊断证据 / 订阅同步 ───────────────────────

# 日志行的结果词片段：时间 [结果词] 模块中文名 · 主句 │ 键=值
_LOG_LINE_TAG_RE = re.compile(r"\[([^\]]{1,6})\]")
_LOG_SEVERITY_FILTERS = {
    "info": frozenset({"info"}),
    "warning": frozenset({"warning"}),
    "error": frozenset({"error"}),
}
_LOG_TAIL_MAX = 400
_LOG_SNAPSHOT_LIMIT = 2000


def _line_severity(line: str) -> str | None:
    m = _LOG_LINE_TAG_RE.search(line)
    return TAG_SEVERITY.get(m.group(1)) if m else None


def _log_tone(sev: str | None) -> str:
    return {"warning": "warn", "error": "bad"}.get(sev or "", "")


async def read_logs(keyword: str = "", level: str = "", tail: int = 120) -> dict:
    """读运行日志的最近片段（内存环形缓冲，与日志页同源）。

    按关键词（不区分大小写子串）与级别过滤后取尾部 N 行；随结果附级别
    计数，供模型先看分布再读细节。行内只含已脱敏的运行日志，无凭据面。"""
    try:
        tail = max(10, min(int(tail or 120), _LOG_TAIL_MAX))
    except (TypeError, ValueError):
        tail = 120
    lines = ring_log_handler.snapshot(_LOG_SNAPSHOT_LIMIT)
    want = _LOG_SEVERITY_FILTERS.get(str(level or "").lower())
    if want is not None:
        lines = [ln for ln in lines if _line_severity(ln) in want]
    kw = str(keyword or "").strip().lower()
    if kw:
        lines = [ln for ln in lines if kw in ln.lower()]
    counts: dict[str, int] = {}
    for ln in lines:
        sev = _line_severity(ln) or "unknown"
        counts[sev] = counts.get(sev, 0) + 1
    rows = []
    for ln in lines[-tail:]:
        head, sep, body = ln.partition(" · ")
        sev = _line_severity(ln)
        rows.append({
            "k": (head if sep else ln)[:56],
            "v": (body if sep else "")[:200],
            "tone": _log_tone(sev),
        })
    out = {"kind": "rows", "titleKey": "logs", "rows": rows, "total": len(lines)}
    if counts:
        out["counts"] = counts
    return out


async def system_diagnostics() -> dict:
    """机器排障证据面快照（/system/diagnostics 同源）：身份 + 最近价格轮 + 异常信号。"""
    from app.domains.system.router import diagnostics as _diagnostics

    snap = await _diagnostics()
    rows: list[dict] = []
    ident = snap.get("identity")
    if isinstance(ident, dict) and "error" not in ident:
        rows.append({"k": f"v{ident.get('app_version', '?')}",
                     "v": f"策略 {ident.get('strategy', '?')} · 出口 {ident.get('pool_available_exits', '?')}",
                     "tone": "ok"})
    cycles = snap.get("cycles") or []
    if isinstance(cycles, list) and cycles:
        c0 = cycles[0] if isinstance(cycles[0], dict) else {}
        status = str(c0.get("status") or "")
        rows.append({"k": "价格轮", "v": f"最近一轮 {status or '未知'}",
                     "tone": "ok" if status in ("completed", "partial") else "bad"})
    for s in (snap.get("signals") or [])[:10]:
        if not isinstance(s, dict):
            continue
        rows.append({"k": str(s.get("code") or "")[:32], "v": str(s.get("text") or "")[:140],
                     "tone": {"error": "bad", "warning": "warn"}.get(str(s.get("severity")), "")})
    if not (snap.get("signals") or []):
        rows.append({"k": "", "v": "诊断面未检出异常信号", "tone": "ok"})
    return _shared.rows_result("diag", rows, total=len(rows))


async def sync_subscriptions() -> dict:
    """重新拉取全部启用的代理订阅（与订阅页同步按钮同源：下载新配置 + 内核按需重启）。

    回执只含订阅标签与结果，绝不含订阅链接本身。"""
    subs = await proxies_service.list_subscriptions("clash")
    active = [s for s in subs if not s.get("deprecated")]
    if not active:
        return _shared.rows_result("subs", [{"k": "", "vKey": "subsNone", "tone": "warn"}])
    rows: list[dict] = []
    for s in active:
        label = str(s.get("label") or f"#{s.get('id')}")
        try:
            await proxies_service.refresh_clash_subscription(int(s.get("id") or 0))
            rows.append({"k": label, "vKey": "subsSynced", "tone": "ok"})
        except Exception as e:  # noqa: BLE001 —— 单订阅失败不影响其余
            rows.append({"k": label, "vKey": "subsFailed", "v": str(e)[:60], "tone": "bad"})
    return _shared.rows_result("subs", rows, total=len(rows))


async def _read_logs(args: dict, sid: str | None = None) -> dict:
    return await read_logs(
        keyword=str(args.get("keyword") or ""),
        level=str(args.get("level") or ""),
        tail=args.get("tail") if args.get("tail") is not None else 120,
    )


async def _system_diagnostics(args: dict, sid: str | None = None) -> dict:
    return await system_diagnostics()


async def _sync_subscriptions(args: dict, sid: str | None = None) -> dict:
    return await sync_subscriptions()


async def _recent_job_failures(args: dict, sid: str | None = None) -> dict:
    return await recent_job_failures()


async def _proxy_pool_status(args: dict, sid: str | None = None) -> dict:
    return await proxy_pool_status()


async def _proxy_exit_detail(args: dict, sid: str | None = None) -> dict:
    return await proxy_exit_detail()


async def _proxy_latency_test(args: dict, sid: str | None = None) -> dict:
    return await proxy_latency_test()


async def _write_gate_status(args: dict, sid: str | None = None) -> dict:
    return await write_gate_status()


async def _web_search(args: dict, sid: str | None = None) -> dict:
    return await web_search(str(args.get("query") or ""))


async def _refresh_game_price(args: dict, sid: str | None = None) -> dict:
    return await refresh_game_price(int(args.get("appid") or 0))


async def _notify_test(args: dict, sid: str | None = None) -> dict:
    return await notify_test()


SPECS = [
    ToolSpec(
        name="web_search", group="read", risk="medium",
        description="联网搜索（全互联网，不只 Steam）。用户问『最新的 XX 消息/网上怎么说/帮我查一下 XX』"
                    "且本地工具答不了时调用；只把问题关键词作为搜索词",
        parameters={"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
        }, "required": ["query"]},
        handler=_web_search, step_label="webSearch",
    ),
    ToolSpec(
        name="refresh_game_price", group="read", risk="medium",
        description="补抓某款已入库游戏的当前价格（后台任务，完成后自动入库）。"
                    "用户说『刷新 XX 的价格』『XX 价格怎么还是旧的』时调用；appid 用检索工具确认",
        parameters={"type": "object", "properties": {
            "appid": {"type": "integer", "description": "游戏 AppID"},
        }, "required": ["appid"]},
        handler=_refresh_game_price, step_label="refreshPrice",
    ),
    ToolSpec(
        name="notify_test", group="read", risk="medium",
        description="发一封连通性测试邮件（不碰业务数据）。用户说『发个测试通知/试试邮件』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_notify_test, step_label="notifyTest",
    ),
    ToolSpec(
        name="recent_job_failures", group="read", risk="low",
        description="列出最近失败的抓取任务与原因。用户问『抓取为什么失败』『最近有什么报错』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_recent_job_failures, step_label="jobFailures",
    ),
    ToolSpec(
        name="proxy_pool_status", group="read", risk="low",
        description="查看代理通道可用状态（价格抓取依赖它）。诊断『价格不更新/抓取不动』类网络问题时与任务失败清单配合调用",
        parameters={"type": "object", "properties": {}},
        handler=_proxy_pool_status, step_label="proxyStatus",
    ),
    ToolSpec(
        name="proxy_exit_detail", group="read", risk="low",
        description="查看代理出口节点明细：逐节点的状态、延迟与出口 IP（最近一次检测的账本事实，不发起新探测）。"
                    "用户问『出口 IP 是什么/有哪些节点/哪个节点延迟最低』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_proxy_exit_detail, step_label="proxyExits",
    ),
    ToolSpec(
        name="proxy_latency_test", group="read", risk="medium",
        description="对全部代理出口节点发起一次真实延迟与连通性检测（耗时约 1~3 分钟，等它跑完再回答）。"
                    "用户说『测一下节点延迟/测试连通性/体检代理』时调用；只想看最近一次结果用 proxy_exit_detail",
        parameters={"type": "object", "properties": {}},
        handler=_proxy_latency_test, step_label="proxyLatencyTest",
    ),
    ToolSpec(
        name="write_gate_status", group="read", risk="low",
        description="查看写入调度状态。用户说『操作一直不生效/卡住了』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_write_gate_status, step_label="gateStatus",
    ),
    ToolSpec(
        name="read_logs", group="read", risk="low",
        description="读取应用运行日志的最近片段（与日志页同源，已脱敏）。"
                    "排查任何异常（抓取失败/同步出错/代理异常/界面报错）时先读这里："
                    "可按关键词过滤（如游戏名、mihomo、wallet），或按级别只看 warning/error，"
                    "并结合级别计数判断问题规模。返回行数有限时可用更大的 tail",
        parameters={"type": "object", "properties": {
            "keyword": {"type": "string", "description": "可选：只保留包含该关键词的行"},
            "level": {"type": "string", "enum": ["info", "warning", "error"],
                      "description": "可选：按级别过滤"},
            "tail": {"type": "integer", "description": "取尾部多少行，默认 120，最大 400"},
        }},
        handler=_read_logs, step_label="readLogs",
    ),
    ToolSpec(
        name="system_diagnostics", group="read", risk="low",
        description="读取实例诊断证据面快照：版本/策略/出口概况、最近价格轮状态，"
                    "以及机器检出的异常信号清单（如价格轮失败、写库竞争、代理池不可用）。"
                    "用户问『现在系统什么状况/有没有什么问题/为什么跑不动』时优先调用，"
                    "再按信号用 read_logs 深挖细节",
        parameters={"type": "object", "properties": {}},
        handler=_system_diagnostics, step_label="diagnostics",
    ),
    ToolSpec(
        name="sync_subscriptions", group="read", risk="medium",
        description="重新拉取全部启用的代理订阅（下载新配置，内核按需自动重启；约需十几秒）。"
                    "用户说『同步订阅/更新订阅/重新拉一下订阅』时调用。"
                    "只想看订阅状态用 proxy_pool_status；回执不含订阅链接",
        parameters={"type": "object", "properties": {}},
        handler=_sync_subscriptions, step_label="syncSubs",
    ),
]
