"""系统诊断与联网工具：任务失败 / 代理池 / 写闸 / 联网搜索 / 价格补抓 / 测试邮件。"""
from __future__ import annotations

import asyncio
import re

import aiohttp

from app.core.database import write_scheduler_diagnostics
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


async def _recent_job_failures(args: dict, sid: str | None = None) -> dict:
    return await recent_job_failures()


async def _proxy_pool_status(args: dict, sid: str | None = None) -> dict:
    return await proxy_pool_status()


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
        name="write_gate_status", group="read", risk="low",
        description="查看写入调度状态。用户说『操作一直不生效/卡住了』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_write_gate_status, step_label="gateStatus",
    ),
]
