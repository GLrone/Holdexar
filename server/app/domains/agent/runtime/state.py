"""Run 状态机：合法迁移表 + 唯一跃迁校验入口。

Run 状态只能经 `assert_transition` 校验后由 service 层落库，禁止对
`AgentRun.status` 散落赋值——状态行变更与对应事件在服务层同一写事务
内产生（账本连续性：状态投影与事实流水不可分叉）。

迁移表口径：
- queued：执行尚未开始，可启动、可取消；启动前发现不可运行的条件
  （配置缺失等）允许直接 failed；
- running：全部终态出口与审批挂起都在此；
- awaiting_approval：审批挂起可恢复运行、也可被取消/失败/预算终止
  （审批器后续阶段接入，迁移面先放开，避免接口卡死后续扩展）；
- 四个终态无出边：done / failed / cancelled / budget_exhausted，
  任何终态再跃迁都是非法迁移。
"""
from __future__ import annotations

RUN_QUEUED = "queued"
RUN_RUNNING = "running"
RUN_AWAITING_APPROVAL = "awaiting_approval"
RUN_DONE = "done"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_BUDGET_EXHAUSTED = "budget_exhausted"

RUN_STATES = (
    RUN_QUEUED,
    RUN_RUNNING,
    RUN_AWAITING_APPROVAL,
    RUN_DONE,
    RUN_FAILED,
    RUN_CANCELLED,
    RUN_BUDGET_EXHAUSTED,
)

TERMINAL_STATES = frozenset({RUN_DONE, RUN_FAILED, RUN_CANCELLED, RUN_BUDGET_EXHAUSTED})

TRANSITIONS: dict[str, frozenset[str]] = {
    RUN_QUEUED: frozenset({RUN_RUNNING, RUN_FAILED, RUN_CANCELLED}),
    RUN_RUNNING: frozenset({
        RUN_AWAITING_APPROVAL,
        RUN_DONE,
        RUN_FAILED,
        RUN_CANCELLED,
        RUN_BUDGET_EXHAUSTED,
    }),
    RUN_AWAITING_APPROVAL: frozenset({
        RUN_RUNNING,
        RUN_DONE,
        RUN_FAILED,
        RUN_CANCELLED,
        RUN_BUDGET_EXHAUSTED,
    }),
}


class IllegalRunTransition(ValueError):
    """非法状态迁移：携带当前态与目标态，调用方不得捕获后继续写库。"""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"illegal run transition: {current} -> {target}")


# ── 域终态 → run 终态映射（统一状态投影，唯一来源）──────────────────
#
# run 记调度事实；业务成败由域账本裁判（price_cycles / crawl_jobs）。
# 映射只回答「这次调度收在哪个 run 终态」：partial（部分成功）的业务
# 细节留在 price_cycles，run 不冒充业务结论，照样记 done。
DOMAIN_TERMINAL_TO_RUN: dict[str, str] = {
    # price_cycles 终态：completed / partial / failed / cancelled
    "completed": RUN_DONE,
    "partial": RUN_DONE,
    "failed": RUN_FAILED,
    "cancelled": RUN_CANCELLED,
    # crawl_jobs 终态：done / failed / stopped
    "done": RUN_DONE,
    "stopped": RUN_CANCELLED,
}


def map_domain_status(status: str | None) -> str | None:
    """域终态 → run 终态；未观测到业务结论（None/未知/非终态）返回 None。"""
    if status is None:
        return None
    return DOMAIN_TERMINAL_TO_RUN.get(str(status))


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATES


def assert_transition(current: str, target: str) -> None:
    """校验 current -> target 是否合法；非法抛 IllegalRunTransition。

    未知状态（不在 RUN_STATES）一律视为非法迁移目标/来源。"""
    if target not in RUN_STATES:
        raise IllegalRunTransition(current, target)
    if current not in RUN_STATES:
        raise IllegalRunTransition(current, target)
    if current == target or target not in TRANSITIONS.get(current, frozenset()):
        raise IllegalRunTransition(current, target)
