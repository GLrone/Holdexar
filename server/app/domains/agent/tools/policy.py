"""工具风险策略：守卫词表（自 intent.py 收编）、判定与审批骨架。

P3 语义：写工具 + 守卫问句 → deny（模型改走 propose_bulk 交用户确认）；
审批挂起/恢复分支 P5 激活，deny 之外的判定序届时扩展，fail-closed。"""
from __future__ import annotations

from app.domains.agent.tools.base import Group, Risk, ToolSpec

GUARD_WORDS = ("清空", "删除", "移除", "移出", "停用", "停止", "批量", "全部", "所有")


def is_guarded(text: str) -> bool:
    return any(w in (text or "") for w in GUARD_WORDS)


def risk_of(spec: ToolSpec) -> Risk:
    return spec.risk


def decide(spec: ToolSpec, *, guarded: bool) -> str:
    """allow / deny（P5 扩展 ask 分支）。"""
    if spec.group == "write" and guarded:
        return "deny"
    return "allow"


DENY_RESULT = {"kind": "denied", "note": "bulk_guard", "via": "propose_bulk"}
