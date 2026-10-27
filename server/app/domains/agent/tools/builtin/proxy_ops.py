"""代理域台账工具：订阅清单（脱敏）/ 定向订阅重拉 / 代理池体检台账 / 生产作业台账。"""
from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.agent.tools.base import ToolSpec
from app.domains.agent.tools.builtin import _shared
from app.domains.proxies import service as proxies_service
from app.domains.proxypool import jobruns
from app.domains.proxypool.models import HealthRun


def _host_of(url: str) -> str:
    """订阅 URL → 域名级摘要（scheme + host）；路径/查询里的 token 一概不出工具面。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        return ""
    host = parts.hostname or ""
    return f"{parts.scheme}://{host}" if host and parts.scheme else host


def _sub_row(s: dict) -> dict:
    stats = s.get("lastStats") if isinstance(s.get("lastStats"), dict) else {}
    bits = [b for b in (
        _host_of(str(s.get("url") or "")),
        f"{stats.get('alive') or 0}/{stats.get('total') or 0}" if stats.get("total") else "",
        str(s.get("lastImportedAt") or "")[:16].replace("T", " "),
    ) if b]
    return {
        "k": str(s.get("label") or f"#{s.get('id')}"),
        "v": " · ".join(bits),
        "tone": "bad" if s.get("deprecated") else "ok",
    }


async def _subscriptions_list(args: dict, sid: str | None = None) -> dict:
    subs = await proxies_service.list_subscriptions()
    if not subs:
        return {"kind": "empty", "note": "subs_none"}
    rows = [_sub_row(s) for s in subs]
    return _shared.rows_result("subs", rows, total=len(rows))


async def _subscription_sync(args: dict, sid: str | None = None) -> dict:
    try:
        sub_id = int(args.get("sub_id") or 0)
    except (TypeError, ValueError):
        sub_id = 0
    subs = await proxies_service.list_subscriptions()
    target = next((s for s in subs if s.get("id") == sub_id), None)
    if target is None:
        return {"kind": "empty", "note": "sub_missing"}
    label = str(target.get("label") or f"#{sub_id}")
    try:
        result = await proxies_service.refresh_clash_subscription(sub_id)
    except ValueError as e:
        return {"kind": "failed", "error": str(e)[:120]}
    row = {"k": label, "vKey": "subsSynced", "tone": "ok"}
    nodes = result.get("nodes")
    if isinstance(nodes, int):
        row["data"] = {"n": nodes}
    return _shared.rows_result("subs", [row])


def _health_row(r: HealthRun) -> dict:
    bits = [b for b in (
        f"覆盖 {r.total or 0} · 可用 {r.steam_ok or 0}",
        f"失败 {r.failed}" if r.failed else "",
        str(r.channel or ""),
        str(r.end_reason or ""),
    ) if b]
    return {
        "k": r.started_at.strftime("%m-%d %H:%M") if r.started_at else f"#{r.id}",
        "v": " · ".join(bits),
        "tone": "bad" if r.failed or r.end_reason else "ok",
    }


async def _pool_health_history(args: dict, sid: str | None = None) -> dict:
    async with get_session_factory()() as session:
        runs = (
            await session.execute(
                select(HealthRun)
                .order_by(HealthRun.started_at.desc(), HealthRun.id.desc())
                .limit(20)
            )
        ).scalars().all()
    if not runs:
        return {"kind": "empty", "note": "no_health_runs"}
    rows = [_health_row(r) for r in runs]
    return _shared.rows_result("healthRuns", rows, total=len(rows))


def _job_row(it: dict) -> dict:
    bits = [b for b in (
        f"{it.get('successCount') or 0}/{it.get('taskCount') or 0}",
        str(it.get("kind") or ""),
        str(it.get("node") or ""),
    ) if b]
    status = str(it.get("status") or "")
    when = str(it.get("startedAt") or "").replace("T", " ")[:16]
    return {
        "k": f"#{it.get('id')} {when}".strip(),
        "v": " · ".join(bits),
        "tone": {"success": "ok", "partial": "warn", "running": "warn",
                 "failed": "bad", "interrupted": "bad"}.get(status, ""),
    }


async def _pool_job_runs(args: dict, sid: str | None = None) -> dict:
    try:
        limit = max(1, min(int(args.get("limit") if args.get("limit") is not None else 10), 100))
    except (TypeError, ValueError):
        limit = 10
    async with get_session_factory()() as session:
        payload = await jobruns.list_runs(session, limit=limit, offset=0, now=datetime.now())
    items = payload.get("items") or []
    if not items:
        return {"kind": "empty", "note": "no_job_runs"}
    rows = [_job_row(it) for it in items]
    s = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if s.get("runs"):
        rows.insert(0, {
            "k": "今日",
            "v": f"{s.get('runs') or 0} 轮 · 成功 {s.get('success') or 0} · 失败 {s.get('failed') or 0}",
            "tone": "bad" if s.get("failed") else ("warn" if s.get("partial") else "ok"),
        })
    return _shared.rows_result("jobRuns", rows, total=len(items))


SPECS = [
    ToolSpec(
        name="subscriptions_list", group="read", risk="low",
        description="查看代理订阅清单：名称、来源域名、节点存活与上次拉取时间（不含订阅链接）。"
                    "用户问『我有哪些订阅/订阅还剩多少节点』，或重拉单条订阅前确认 sub_id 时调用",
        parameters={"type": "object", "properties": {}},
        handler=_subscriptions_list, step_label="subsList",
    ),
    ToolSpec(
        name="subscription_sync", group="read", risk="high",
        description="重拉指定的一条代理订阅（下载新配置，内核按需自动重启，约需十几秒）。"
                    "用户说『重拉/更新这一个订阅』时调用；sub_id 先用订阅清单确认，全部重拉用 sync_subscriptions",
        parameters={"type": "object", "properties": {
            "sub_id": {"type": "integer", "description": "订阅 ID（subscriptions_list 返回的 id）"},
        }, "required": ["sub_id"]},
        handler=_subscription_sync, step_label="subSync",
    ),
    ToolSpec(
        name="pool_health_history", group="read", risk="low",
        description="查看代理池最近的体检台账：每轮时间、覆盖节点数、可用/失败数与收尾原因。"
                    "用户问『最近体检结果怎么样/节点为什么被换掉』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_pool_health_history, step_label="healthHistory",
    ),
    ToolSpec(
        name="pool_job_runs", group="read", risk="low",
        description="查看代理生产作业台账：最近每轮抓取作业的时间、成功/计划数与状态，附今日概览。"
                    "用户问『代理作业跑得怎么样/今天抓了几轮』时调用",
        parameters={"type": "object", "properties": {
            "limit": {"type": "integer", "description": "返回最近多少条，默认 10"},
        }},
        handler=_pool_job_runs, step_label="jobRuns",
    ),
]
