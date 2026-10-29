"""pilot 工具层门面：实现已收编进 agent.tools（Agent V2 P3），本模块
保留旧公开符号与问句解析 helper，service / intent / 既有测试零改动。

一处声明（agent/tools/builtin/*/SPECS）+ 一个词条键 = 加一个新工具；
写动作守卫、逐工具预算与时间线映射由 agent/tools 的 policy / projection
统一承载。"""
from __future__ import annotations

from app.domains.account import service as account_service
from app.domains.achievements import service as achievements_service
from app.domains.alerts import service as alerts_service
from app.domains.bills import service as bills_service
from app.domains.bundles import service as bundles_service
from app.domains.crawl import events as crawl_events_service
from app.domains.crawl import service as crawl_service
from app.domains.family import service as family_service
from app.domains.games import service as games_service
from app.domains.metadata import service as metadata_service
from app.domains.monitoring import service as monitoring_service
from app.domains.proxies import service as proxies_service
from app.domains.rates import service as rates_service
from app.domains.regions import service as regions_service
from app.domains.redeem import service as redeem_service
from app.domains.steam_events import service as steam_events_service
from app.domains.wishlist import follows as wishlist_follows
from app.domains.wishlist import service as wishlist_service
from app.domains.agent.runtime import executor as _executor
from app.domains.agent.tools.builtin import _shared
from app.domains.agent.tools.builtin.catalog import recommend_games, search_games
from app.domains.agent.tools.builtin.prices import (
    _trend_series,
    compare_games,
    price_facts,
)
from app.domains.agent.tools.builtin.system import _parse_ddg
from app.domains.agent.tools.builtin.tracking import alert_add, monitor_add
from app.domains.agent.tools.projection import (
    BUDGET_DEFAULT_S as _BUDGET_DEFAULT_S,
    BUDGET_SLOW_S as _BUDGET_SLOW_S,
    BUDGET_TASK_S as _BUDGET_TASK_S,
    CARD_HINT_ROWS_TOOLS as _CARD_HINT_ROWS_TOOLS,
    CARD_KIND_HINTS as _CARD_KIND_HINTS,
    CARD_TOOLS as _CARD_TOOLS,
    CORE_TOOLS as _CORE_TOOLS,
    LIST_TOOLS as _LIST_TOOLS,
    QUICK_TOOLS,
    _READ_TOOLS,
    _SYNC_TOOLS,
    _TASK_TOOLS,
    _WRITE_TOOLS,
    tool_step,
)
from app.domains.agent.tools.registry import REGISTRY

# 触发 builtin 登记（工具全集由注册表自持，此处不保存数量）
from app.domains.agent.tools.builtin import catalog as _catalog  # noqa: F401 — 触发内置工具注册

_BULK_MAX_ITEMS = _shared._BULK_MAX_ITEMS
_INGEST_MAX = 20
_TREND_MAX_POINTS = 72

_PRICE_TAG_RE = _shared._PRICE_TAG_RE
_THRESH_RE = _shared._THRESH_RE

TOOL_META: dict[str, dict] = REGISTRY.meta()

# 工具表切面（唯一事实源在 agent/tools/projection.py）
CARD_TOOLS = _CARD_TOOLS
"""产卡工具：只有它们需要 emit_card 开关。"""

CORE_TOOLS = _CORE_TOOLS
"""默认可见的核心切面（四条主流程 + 会话/偏好/附件/导航 + list_tools）。"""

LIST_TOOLS = _LIST_TOOLS
"""按需解锁工具（builtin/info.py）。"""


def extract_title(question: str) -> str | None:
    """问句里《书名号》中的游戏名（写意图解析对象的优先词）。"""
    m = _PRICE_TAG_RE.search(question or "")
    return m.group(1) if m else None


def extract_alert(question: str) -> tuple[str, float | None]:
    """解析提醒条件。返回 (target_type, target_value_fen)：史低类无值；
    价格类按「低于 N 块/元」取 N×100 分；解析不出数值返回 price + None
    （调用方转指引，不臆造阈值）。"""
    q = question or ""
    if "史低" in q:
        return "historic_low", None
    m = _THRESH_RE.search(q)
    if m:
        return "price", int(float(m.group(1)) * 100)
    return "price", None


def tool_budget_s(name: str) -> float | None:
    """工具执行预算（秒）。"""
    spec = REGISTRY.get(name)
    return spec.timeout_s if spec else _BUDGET_DEFAULT_S


def expandable_tools() -> frozenset[str]:
    """核心切面之外的其余工具名——list_tools 解锁的对象（builtin/info.py）。

    注册表已由 builtin/__init__ 在导入期填满，这里现算：加一个新工具只需在
    它的 SPECS 里声明，核心与否由 projection.CORE_TOOLS 一处决定。"""
    return frozenset(REGISTRY.names()) - CORE_TOOLS - {LIST_TOOLS}


def tool_specs(*, expanded: frozenset[str] | None = None,
               full: bool = False) -> list[dict]:
    """agent 循环的工具表（OpenAI function 格式），来自注册表视图投影。

    默认只发**核心切面**：四条用户主流程（找游戏 / 看价格 / 关注提醒 / 免费与
    活动）加上会话、偏好、附件读取与导航，以及 list_tools 本身。工具表是每个
    请求都要重发一遍的固定开销，而核心之外的大半是长会话里很少用到的诊断与
    同步类——需要时由 list_tools 一次解锁（见 builtin/info.py），解锁后本会话
    不再回退（回退会让整段历史的前缀缓存作废）。

    expanded = 本会话已解锁的额外工具名（service.session_expanded_tools 从账本
    重建）；full=True 给全量表（诊断 / 测试用，运行时不用）。

    emit_card 只注入产卡工具（CARD_TOOLS）——不产卡的工具带上它只是白付开销。"""
    views = REGISTRY.views(card_tools=CARD_TOOLS)
    if full:
        return views
    names = CORE_TOOLS | (expanded or frozenset())
    return [v for v in views if v["function"]["name"] in names]


async def exact_catalog_appid(name: str) -> int | None:
    """书名号整名精确直取：目录 FTS 分词对带标点整名会错位（近似名挤占
    候选位），名称等值唯一命中才返回 appid，其余形态（零命中 / 多义）弃权。"""
    q = (name or "").strip()
    if not q:
        return None
    from sqlalchemy import func, or_, select

    from app.core.database import get_session_factory
    from app.domains.games.models import CatalogRemoval, Game

    async with get_session_factory()() as session:
        rows = (await session.execute(
            select(Game.appid)
            .where(or_(Game.name == q, func.lower(Game.name_en) == q.lower()))
            .where(Game.appid.not_in(select(CatalogRemoval.appid)))
        )).scalars().all()
    return int(rows[0]) if len(rows) == 1 else None


async def execute_tool(name: str, arguments: dict, *, guarded: bool = False,
                       sid: str | None = None) -> dict:
    """工具执行分发：守卫 / 校验 / 超时 / 失败归一由 agent.tools.executor 承载。

    写工具受守卫复核：问句含批量 / 删除 / 停用语义时拒绝执行（返回
    kind=denied，模型改走 propose_bulk 交用户确认）。"""
    spec = REGISTRY.get(name)
    if spec is None:
        return {"kind": "unknown_tool"}
    result = await _executor.execute(spec, arguments, guarded=guarded, sid=sid)
    return result.data
