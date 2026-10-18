"""领航员内部快速决策代理（System 1 Decision Agent）。

负责意图评估、置信度门禁、实体解析、卡片准入与大模型双向状态对齐。
为外层大模型（System 2）提供快速决策底座与卡片控制能力，支持置信度弃权与显式抑制。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from app.domains.pilot import intent as pilot_intent
from app.domains.pilot import tools as pilot_tools

logger = logging.getLogger(__name__)

_STRUCTURED_KINDS = frozenset({
    "structured", "metric-grid", "data-table", "time-series", "coverage-matrix", "status-board",
})

_CORE_CARD_KINDS = {
    "proposal", "stepper", "price", "regions", "compare",
    "games", "family", "achievements", "epic",
    *_STRUCTURED_KINDS,
}
_MAX_CARDS_PER_TURN = 4

# 典型非价格咨询特征词（涉及硬件、性能、玩法、剧情等）
_NON_PRICE_KEYWORDS = (
    "配置", "显卡", "cpu", "内存", "优化", "帧率", "画质", "卡顿", "闪退",
    "打不开", "黑屏", "启动不了", "好玩吗", "好不好玩", "评价", "通关", "攻略",
    "流程", "剧情", "结局", "主角", "背景", "故事", "玩法", "联机", "mod",
    "汉化", "补丁", "按键", "怎么过", "怎么打",
)

# 单对象核验工具集
_SINGLE_ITEM_CHECK_TOOLS = frozenset({
    "list_follows", "list_alerts", "list_owned", "list_wishlist", "list_family_library",
})

# 技术状态与底层诊断工具集
_DIAGNOSTIC_TOOLS = frozenset({
    "proxy_pool_status", "proxy_exit_detail", "proxy_latency_test",
    "write_gate_status", "recent_job_failures", "diagnose_price", "list_tasks",
})

# 纯数据提供类工具集（供大模型推导与综合，默认不外泄为 UI 原始表格卡片）
_DATA_SYNTHESIS_TOOLS = frozenset({
    "steam_free", "hb_monthly", "calendar_events", "rates_overview",
})

# 系统技术排查特征词
_DIAGNOSTIC_KEYWORDS = (
    "代理", "节点", "网络", "延迟", "写闸", "报错", "为什么抓取",
    "排查", "体检", "诊断", "失败任务", "抓取任务", "抓取状态", "丢包", "超时",
)

# 家庭规则/机制咨询特征词
_FAMILY_RULES_KEYWORDS = (
    "怎么", "如何", "规则", "限制", "最多几人", "锁区吗", "教程",
    "开启", "邀请", "能不能共享", "退组", "cd", "冷却", "共享机制",
)

# 个人家庭成员查看特征词
_PERSONAL_FAMILY_KEYWORDS = (
    "我的家庭", "看家庭", "家庭成员", "谁在家庭", "家里有谁", "家庭组里有谁", "家庭成员列表",
)

# 游戏成就难度/通关特征词
_GAME_ACHIEVEMENT_KEYWORDS = (
    "难吗", "好拿吗", "难度", "几周目", "白金难不难", "怎么全成就", "攻略",
    "全成就难", "全成就容易", "难不难",
)

# 个人成就进度查看特征词
_PERSONAL_ACHIEVEMENT_KEYWORDS = (
    "我的成就", "成就进度", "解锁了几个", "看了看成就", "完成率",
    "白金奖杯数", "我的奖杯", "成就列表", "成就概况",
)


@dataclass(frozen=True)
class CardDecision:
    """内部决策代理对卡片发放的裁决。"""

    should_emit: bool
    card: dict | None
    kind: str | None
    status: str  # "rendered" | "suppressed"
    reason: str  # "ok" | "suppressed_by_model" | "low_confidence" | "no_price_intent" | "quota_exceeded" | "empty"
    summary: str | None = None


# 跨轮去重的卡种：静态清单类可抑制（同内容已呈现过）；价格/对比类不抑制
# ——价格随刷新轮变化，重复询问往往正是要看最新价
_REPEATABLE_SUPPRESS_KINDS = frozenset({"games", "family", "achievements", "rows", "epic"})

# 追问键白名单（前端按 pilot.followup.{key} 取双语词条，缺词即丢弃该条）
_FOLLOWUP_KEYS = frozenset({
    "saleNext", "saleLive", "saleWatch", "priceDropsRecent", "openCalendar",
    "compareRegions", "priceHistory", "similarGames", "addFollow", "setAlert",
    "myFollows", "ownedSkip", "freeNow", "auditFollows", "retryWeb",
    "hotDeals", "highRated", "budgetFriendly", "familyOverlap", "testLatency",
    "switchNode", "checkFailures", "listAlerts",
})

# 意图 → 候选追问（按序取前若干条；带 {name} 的需有本轮游戏对象才成立）
_FOLLOWUP_BY_INTENT: dict[str, tuple[str, ...]] = {
    "price_analysis": ("priceHistory", "compareRegions", "similarGames", "addFollow", "setAlert"),
    "find_games": ("highRated", "budgetFriendly", "ownedSkip", "priceDropsRecent"),
    "add_monitor": ("myFollows", "setAlert", "priceDropsRecent"),
    "create_alert": ("listAlerts", "myFollows"),
    "how_to": (),
    "navigate": (),
    "chat": (),
}

# 问句特征 → 追加候选（覆盖「意图路由没认出来」的开放问题）
_FOLLOWUP_BY_TOPIC: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("大促", "特卖", "促销", "打折活动", "夏促", "秋促", "冬促", "史低", "什么时候开始"),
     ("saleNext", "saleLive", "saleWatch", "openCalendar")),
    (("活动", "日历", "游戏节", "新品节", "fest"), ("openCalendar", "saleNext", "saleLive")),
    (("免费", "喜加一", "白嫖", "白送", "限免"), ("freeNow", "priceDropsRecent")),
    (("降价", "跌了", "新低", "便宜了"), ("priceDropsRecent", "myFollows", "hotDeals")),
    (("汇率", "折合", "换算"), ("compareRegions",)),
    (("家庭", "共享"), ("familyOverlap",)),
    # 全区比价口径：不一定命中价格意图词（「划算」「最便宜」不在价格词表里）
    (("哪个区", "哪个地区", "全区", "各区", "各区域", "比价", "划算", "最便宜", "哪里买"),
     ("compareRegions", "priceHistory", "setAlert", "priceDropsRecent")),
    (("代理", "节点", "延迟", "网络", "超时"), ("testLatency", "switchNode", "checkFailures")),
    # 排障口径：现象描述类问句往往不含任何业务关键词
    (("没更新", "不更新", "没刷新", "为什么", "报错", "异常", "失败", "卡住", "不动了"),
     ("checkFailures", "testLatency", "switchNode")),
)

# 意图与主题都没命中时按本轮卡片兜底（有卡就有得问，别退回与所问无关的通用推荐）
_FOLLOWUP_BY_CARD: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("price", "regions", "compare"), ("priceHistory", "compareRegions", "similarGames",
                                       "addFollow", "setAlert")),
    (("games",), ("highRated", "budgetFriendly", "ownedSkip", "priceDropsRecent")),
    (("family",), ("familyOverlap",)),
    (("rows", "epic"), ("priceDropsRecent", "myFollows")),
)


# 本产品能力域信号：问句沾不上任何一条时，本轮不产出产品追问
# ——鼠标行情、赛事比分、别家游戏机制这类问题硬塞「看降价/查促销」等于把回答拖成跑题。
# 只收「本产品确实有数据源」的窄词，不收「史低/价格」这类通用词：外设、饰品、
# 别家平台活动也问「多少钱/值不值」，把它们算进来等于没设这道闸。
_DOMAIN_HINT: tuple[str, ...] = (
    "steam", "愿望单", "喜加一", "夏促", "秋促", "冬促", "特卖", "区服",
    "俄区", "土区", "港区", "港服", "国区", "阿根廷", "土耳其", "激活码",
    "家庭共享", "成就", "奖杯", "关注列表", "降价提醒", "比价", "汇率折算",
    "节点", "延迟", "刷新", "入库", "促销日历",
)

# 意图已被路由到本产品能力：问句措辞未必带域内词，但确实答的是本产品的事
_DOMAIN_INTENTS = frozenset({"price_analysis", "find_games", "add_monitor", "create_alert"})

# 能力外对象词：命中即说明本产品根本没这类数据（手柄/平板是硬件、选手/直播是电竞、
# 联动/榜单是别家平台的运营）。意图误路由到「找游戏」时靠它把追问闸回去。
_OUT_OF_DOMAIN: tuple[str, ...] = (
    "手柄", "鼠标", "键盘", "耳机", "电源", "显卡", "主板", "内存", "硬盘",
    "平板", "手机", "笔记本", "主机", "外设", "显示器", "路由器",
    "选手", "战队", "比分", "夺冠", "冠军", "决赛", "半决赛", "小组赛",
    "解说", "直播", "主播", "签约", "转会", "禁赛",
    "联动", "周边", "手办", "快递", "门店", "客服申诉", "解封", "封号",
    "战绩查询", "段位", "排位", "匹配机制",
)


def suggest_followups(question: str, intent: str, cards: list[dict],
                      last_game: dict | None,
                      tools_ran: int = 0) -> list[dict]:
    """本轮追问：按意图与问句主题确定性产出结构化候选（key + args）。

    只产出白名单内的键——文案由前端 i18n 承载，这里不出用户可见文本，
    中英界面都不会被模型或后端的措辞污染。带 {name} 的候选需要本轮有明确
    游戏对象（价格卡 / 地区卡 / 对比卡里的名称，或本轮写动作的对象）才成立。
    无候选时返回空列表，前端回落它自己的固定开场推荐。"""
    q = (question or "").strip()
    ordered: list[str] = []

    def _add(keys: tuple[str, ...]) -> None:
        for k in keys:
            if k in _FOLLOWUP_KEYS and k not in ordered:
                ordered.append(k)

    _add(_FOLLOWUP_BY_INTENT.get(intent, ()))
    for words, keys in _FOLLOWUP_BY_TOPIC:
        if any(w in q for w in words):
            _add(keys)
    # 意图与主题都空手时按本轮卡片兜底：有卡说明答的是具体对象，追问就该围着它
    if not ordered:
        kinds = {str(c.get("kind") or "") for c in (cards or []) if isinstance(c, dict)}
        for want, keys in _FOLLOWUP_BY_CARD:
            if kinds & set(want):
                _add(keys)
                break

    name = ""
    if isinstance(last_game, dict):
        name = str(last_game.get("name") or "").strip()
    if not name:
        for card in cards or []:
            if not isinstance(card, dict):
                continue
            if card.get("kind") in ("price", "regions", "compare"):
                name = str(card.get("name") or "").strip()
                if name:
                    break
    # 本轮一次工具都没跑成：给一条「换个说法再查」的退路，别只给不相关推荐
    if not tools_ran and "retryWeb" not in ordered:
        ordered.append("retryWeb")
    if not ordered:
        return []
    # 问句与出卡都沾不上本产品能力域时，一条都不给：宁可没有追问，也不给跑题的。
    # 意图被路由到本产品能力（比价/找游戏/加关注/设提醒）也算域内——问句措辞未必带域内词；
    # 但问句里出现能力外对象（硬件/电竞/别家运营）时以对象为准，那才是用户真正问的东西。
    _in_domain = (intent in _DOMAIN_INTENTS or any(w in q for w in _DOMAIN_HINT)) \
        and not any(w in q for w in _OUT_OF_DOMAIN)
    if not (_in_domain or any(isinstance(c, dict) for c in cards or [])):
        return []

    named = {"compareRegions", "priceHistory", "similarGames", "addFollow", "setAlert"}
    out: list[dict] = []
    for k in ordered:
        if k in named:
            if not name:
                continue
            out.append({"key": k, "args": {"name": name}})
            continue
        out.append({"key": k, "args": {}})
    return out[:4]


def _card_identity(kind: str, card: dict) -> str:
    """卡片身份：同 kind 下区分内容的最小锚（名称/对象/首行）。"""
    if kind == "epic":
        offers = card.get("offers") or []
        first = offers[0].get("title") or offers[0].get("titleCn") if offers else ""
        return f"epic:{first}:{len(offers)}"
    for key in ("name", "appid", "title", "titleKey"):
        v = card.get(key)
        if v:
            return str(v)
    items = card.get("items") or card.get("rows") or []
    for it in items:
        if isinstance(it, dict) and it.get("name"):
            return str(it["name"])
        if isinstance(it, dict) and it.get("k"):
            return str(it["k"])
    return ""


class DecisionAgent:
    """内部快速决策引擎。"""

    def suggest_followups(self, question: str, intent: str, cards: list[dict],
                          last_game: dict | None, tools_ran: int = 0) -> list[dict]:
        """本轮追问候选（确定性产出，转发模块级实现）。"""
        return suggest_followups(question, intent, cards, last_game, tools_ran=tools_ran)

    def evaluate_intent(self, question: str) -> dict[str, Any]:
        """评估问句的意图与价格/导购倾向。"""
        q = (question or "").strip()
        intent = pilot_intent.route(q)
        has_price_words = any(w in q for w in pilot_intent._PRICE_WORDS)
        has_find_words = any(w in q for w in pilot_intent._FIND_WORDS)
        is_price = intent == pilot_intent.PRICE_ANALYSIS or has_price_words
        is_find = intent == pilot_intent.FIND_GAMES or has_find_words
        is_non_price = any(w in q for w in _NON_PRICE_KEYWORDS) and not has_price_words
        nav_target = pilot_intent.nav_target(q)
        confidence = "high" if (is_price or is_find or nav_target) else "low"
        return {
            "intent": intent,
            "confidence": confidence,
            "is_price": is_price,
            "is_find": is_find,
            "is_non_price": is_non_price,
            "nav_target": nav_target,
        }

    def _has_single_game_target(self, q: str) -> bool:
        """检查问句是否明确针对单款游戏。"""
        if pilot_tools.extract_title(q):
            return True
        if any(p in q for p in ("它", "这游戏", "该游戏", "这款")):
            return True
        check_patterns = (
            "在不在", "有没有", "买了吗", "买了没", "在关注里吗",
            "在库里吗", "在愿望单吗", "关注了吗", "关注了么", "设置了提醒吗",
        )
        return any(p in q for p in check_patterns)

    def _is_full_list_request(self, q: str) -> bool:
        """检查问句是否旨在查看完整清单，而非核查单个对象。"""
        single_item_indicators = (
            "在不在", "有没有", "买了吗", "买了没", "在吗",
            "关注了吗", "在关注里吗", "在库里吗", "在愿望单吗",
        )
        if any(ind in q for ind in single_item_indicators):
            return False
        return any(w in q for w in (
            "列表", "清单", "哪些", "所有", "全部", "一览", "都有啥",
            "都有什么", "有哪些", "全部关注", "所有游戏", "查看关注",
            "看看关注", "看下关注", "看关注", "看愿望单", "看游戏库",
            "展示关注", "显示关注", "列出",
        ))

    def _is_diagnostic_query(self, q: str) -> bool:
        return any(w in q for w in _DIAGNOSTIC_KEYWORDS)

    def _is_family_rules_query(self, q: str) -> bool:
        return any(w in q for w in _FAMILY_RULES_KEYWORDS)

    def _is_personal_family_query(self, q: str) -> bool:
        return any(w in q for w in _PERSONAL_FAMILY_KEYWORDS)

    def _is_game_achievement_query(self, q: str) -> bool:
        return any(w in q for w in _GAME_ACHIEVEMENT_KEYWORDS)

    def _is_personal_achievement_query(self, q: str) -> bool:
        return any(w in q for w in _PERSONAL_ACHIEVEMENT_KEYWORDS)

    def stream_card_hint(
        self,
        tool_name: str,
        arguments: dict | None,
        question: str,
        current_cards: list[dict],
        base_hint: str | None,
        recent_cards: list[tuple[str, str]] | None = None,
    ) -> str | None:
        """执行期骨架预告裁决：模型主动关闭或意图不符时不挂骨架占位。"""
        if not base_hint:
            return None
        args = arguments or {}
        if args.get("emit_card") is False:
            return None
        if len(current_cards) >= _MAX_CARDS_PER_TURN:
            return None
        intent_info = self.evaluate_intent(question)
        explicit_allow = args.get("emit_card") is True

        if base_hint in ("price", "regions", "compare") and intent_info["is_non_price"] and not explicit_allow:
            return None
        if base_hint == "games":
            if tool_name == "search_games" and not intent_info["is_find"] and not explicit_allow:
                return None
            if self._has_single_game_target(question) and not self._is_full_list_request(question) and not explicit_allow:
                return None
        if tool_name in _DATA_SYNTHESIS_TOOLS and not explicit_allow:
            return None
        if tool_name in _SINGLE_ITEM_CHECK_TOOLS and self._has_single_game_target(question) and not self._is_full_list_request(question) and not explicit_allow:
            return None
        if tool_name in _DIAGNOSTIC_TOOLS and not self._is_diagnostic_query(question) and not explicit_allow:
            return None
        if tool_name == "family_status" and self._is_family_rules_query(question) and not self._is_personal_family_query(question) and not explicit_allow:
            return None
        if tool_name == "achievements_summary" and self._is_game_achievement_query(question) and not self._is_personal_achievement_query(question) and not explicit_allow:
            return None

        has_core = any(c.get("kind") in _CORE_CARD_KINDS for c in current_cards)
        if has_core and base_hint not in _CORE_CARD_KINDS:
            return None
        if base_hint == "rows" and any(c.get("kind") == "rows" for c in current_cards):
            return None
        if recent_cards and base_hint in _REPEATABLE_SUPPRESS_KINDS and not explicit_allow:
            args_h = args or {}
            # 骨架期拿不到结果卡，仅按工具参数可得的身份粗探：无身份参数的
            # 清单/概况类工具，近轮同 kind 已呈现时不再挂骨架（结果期还有
            # 精确身份裁决）；带 query/appid 的检索不在此列
            if not any(args_h.get(a) for a in ("appid", "query", "name")) \
                    and any(k == base_hint for k, _ in recent_cards):
                return None
        return base_hint

    def decide_card(
        self,
        tool_name: str,
        result: dict,
        arguments: dict | None,
        question: str,
        current_cards: list[dict],
        base_card: dict | None,
        recent_cards: list[tuple[str, str]] | None = None,
    ) -> CardDecision:
        """裁决工具结果是否在前端界面生成可视化卡片。

        recent_cards 是跨轮卡片账本（kind, identity）清单——静态清单类卡
        近轮已呈现过即不再重复发卡，价格/对比类不受此限（价格会变）。"""
        args = arguments or {}

        # 1. 外层模型显式控制：允许模型主动关闭卡片渲染
        if args.get("emit_card") is False:
            return CardDecision(
                should_emit=False,
                card=None,
                kind=base_card.get("kind") if base_card else None,
                status="suppressed",
                reason="suppressed_by_model",
                summary="模型显式指定 emit_card=False，卡片未向用户展示",
            )

        # 2. 空结果、失败或拒绝
        if base_card is None or result.get("kind") in ("failed", "timeout", "denied", "not_found"):
            return CardDecision(
                should_emit=False,
                card=None,
                kind=None,
                status="suppressed",
                reason="empty_or_failed",
                summary="工具未返回有效可视化数据",
            )

        kind = base_card.get("kind")
        intent_info = self.evaluate_intent(question)
        explicit_allow = args.get("emit_card") is True

        # 3. 置信度与意图门禁（置信度不足时弃权发卡）
        if kind in ("price", "regions", "compare"):
            if intent_info["is_non_price"] and not explicit_allow:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="no_price_intent",
                    summary="问句非比价导向，内部决策代理已抑制价格卡片",
                )

        if kind == "games":
            items = base_card.get("items") or []
            if not items:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="empty_candidates",
                    summary="未检索到匹配游戏候选",
                )
            if tool_name == "search_games" and not intent_info["is_find"] and not explicit_allow:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="intermediate_retrieval",
                    summary="作为中间推导数据未向用户展示搜索候选卡片",
                )
            if self._has_single_game_target(question) and not self._is_full_list_request(question) and not explicit_allow:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="single_game_query",
                    summary="针对单款游戏核查，已抑制候选列表卡片",
                )

        if tool_name in _SINGLE_ITEM_CHECK_TOOLS:
            if self._has_single_game_target(question) and not self._is_full_list_request(question) and not explicit_allow:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="single_game_query",
                    summary="问句仅核验单款游戏，内部决策代理已抑制全量清单卡片",
                )

        if kind == "epic":
            offers = base_card.get("offers") or []
            if not offers and not base_card.get("mobile"):
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="empty_candidates",
                    summary="未获取到有效 Epic 免费游戏数据",
                )

        if tool_name in _DATA_SYNTHESIS_TOOLS and not explicit_allow:
            return CardDecision(
                should_emit=False,
                card=None,
                kind=kind,
                status="suppressed",
                reason="internal_data_for_synthesis",
                summary="原始数据表已作为内部推导数据供大模型总结，未在界面单独发卡",
            )

        if tool_name in _DIAGNOSTIC_TOOLS:
            if not self._is_diagnostic_query(question) and not explicit_allow:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="internal_diagnostic",
                    summary="技术诊断数据供内部推演，已抑制技术状态卡片展示",
                )

        if tool_name == "family_status":
            if self._is_family_rules_query(question) and not self._is_personal_family_query(question) and not explicit_allow:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="family_rules_query",
                    summary="问句为家庭共享规则咨询，已抑制个人家庭成员卡片",
                )

        if tool_name == "achievements_summary":
            if self._is_game_achievement_query(question) and not self._is_personal_achievement_query(question) and not explicit_allow:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="game_achievement_query",
                    summary="问句为游戏成就难度咨询，已抑制个人成就概况卡片",
                )

        # 4. 跨轮卡片账本：静态清单类同内容卡近轮已呈现过，不重复发卡
        if recent_cards and kind in _REPEATABLE_SUPPRESS_KINDS and not explicit_allow:
            identity = _card_identity(kind, base_card)
            if identity and (kind, identity) in recent_cards:
                return CardDecision(
                    should_emit=False,
                    card=None,
                    kind=kind,
                    status="suppressed",
                    reason="already_presented",
                    summary="近几轮已向用户呈现过同内容卡片，本轮不重复发卡",
                )

        # 5. 配额与去重门禁
        if base_card in current_cards:
            return CardDecision(
                should_emit=False,
                card=None,
                kind=kind,
                status="suppressed",
                reason="duplicate_card",
                summary="卡片已在当前会话中存在",
            )
        if len(current_cards) >= _MAX_CARDS_PER_TURN:
            return CardDecision(
                should_emit=False,
                card=None,
                kind=kind,
                status="suppressed",
                reason="quota_exceeded",
                summary="已达单轮卡片数量上限",
            )
        has_core = any(c.get("kind") in _CORE_CARD_KINDS for c in current_cards)
        if has_core and kind not in _CORE_CARD_KINDS:
            return CardDecision(
                should_emit=False,
                card=None,
                kind=kind,
                status="suppressed",
                reason="core_card_priority",
                summary="核心业务卡片优先，辅助卡片已抑制",
            )
        if kind == "rows" and any(c.get("kind") == "rows" for c in current_cards):
            return CardDecision(
                should_emit=False,
                card=None,
                kind=kind,
                status="suppressed",
                reason="duplicate_rows_card",
                summary="同类型列表卡片去重",
            )

        # 6. 准入通过，组装卡片摘要供大模型回灌感知
        summary = self._summarize_card(base_card)
        return CardDecision(
            should_emit=True,
            card=base_card,
            kind=kind,
            status="rendered",
            reason="ok",
            summary=summary,
        )

    def _summarize_card(self, card: dict) -> str:
        """生成供外层大模型感知的卡片摘要。"""
        kind = card.get("kind")
        if kind == "price":
            name = card.get("name") or "游戏"
            curr = card.get("currentPrice") or card.get("cnyFen")
            curr_str = f"¥{curr/100:.2f}" if isinstance(curr, (int, float)) else "未知价格"
            cut = card.get("cut") or card.get("discount")
            cut_str = f"-{cut}%" if cut else "原价"
            return f"价格卡片已在用户界面呈现：《{name}》现价 {curr_str} ({cut_str})"
        if kind == "games":
            items = card.get("items") or []
            names = [it.get("name") for it in items[:3] if it.get("name")]
            name_str = f" ({'、'.join(names)})" if names else ""
            total_str = f"，共 {card.get('total')} 款" if card.get("total") is not None else ""
            return f"游戏列表卡片已在用户界面呈现：展示 {len(items)} 款游戏{name_str}{total_str}"
        if kind == "regions":
            name = card.get("name") or "游戏"
            return f"全区比价卡片已在用户界面呈现：《{name}》各区域售价走势与最低区"
        if kind == "compare":
            items = card.get("items") or []
            names = [it.get("name") for it in items if it.get("name")]
            name_str = f" ({'、'.join(names)})" if names else ""
            return f"多游戏对比卡片已在用户界面呈现：并排对比 {len(items)} 款游戏参数{name_str}"
        if kind == "proposal":
            act = card.get("action") or "操作"
            items = card.get("items") or []
            return f"待确认动作卡片已在用户界面呈现：{act}，涉及 {len(items)} 项条目"
        if kind == "epic":
            offers = card.get("offers") or []
            titles = [o.get("titleCn") or o.get("title") for o in offers[:3] if (o.get("title") or o.get("titleCn"))]
            titles_str = f" ({'、'.join(titles)})" if titles else ""
            total_str = f"，共 {card.get('total')} 款" if card.get("total") is not None else ""
            return f"Epic 喜加一卡片已在用户界面呈现：展示 {len(offers)} 款免费游戏{titles_str}{total_str}"
        if kind == "rows":
            title = card.get("titleKey") or "数据清单"
            rows = card.get("rows") or []
            sample = "、".join(str(r.get("k", "")) for r in rows[:3] if r.get("k"))
            sample_str = f" ({sample})" if sample else ""
            return f"数据列表卡片已在用户界面呈现：【{title}】包含 {len(rows)} 项数据{sample_str}"
        if kind == "family":
            members = card.get("members") or []
            names = "、".join(m.get("personaName") or str(m.get("steamId")) for m in members[:3])
            name_str = f" ({names})" if names else ""
            return f"家庭共享卡片已在用户界面呈现：包含 {len(members)} 位成员{name_str}"
        if kind == "achievements":
            unlocked = card.get("unlocked", 0)
            total = card.get("total", 0)
            rate = card.get("completionRate")
            rate_str = f"，完成率 {rate}%" if rate is not None else ""
            return f"成就概况卡片已在用户界面呈现：已解锁 {unlocked}/{total} 项{rate_str}"
        if kind == "stepper":
            title = card.get("title") or "流程流水线"
            steps = card.get("steps") or []
            idx = card.get("currentStepIndex", 0)
            step_title = steps[idx].get("title") if idx < len(steps) else "就绪"
            return f"流水线进度卡片已在用户界面呈现：【{title}】当前进行至「{step_title}」"
        if kind == "action":
            act = card.get("action") or "操作执行"
            name = card.get("name")
            name_str = f"《{name}》" if name else ""
            return f"操作结果卡片已在用户界面呈现：已执行 {act} {name_str}"
        if kind == "navigate":
            target = card.get("target") or card.get("path") or ""
            return f"页面导航卡片已在用户界面呈现：目标「{target}」"
        if card.get("componentId") or kind in _STRUCTURED_KINDS:
            c_id = card.get("componentId") or kind
            title = card.get("title") or c_id
            return f"结构化 UI 组件已在用户界面呈现：【{title}】({c_id})"
        return f"{kind} 可视化卡片已在用户界面呈现"

    def enhance_model_view(self, result: dict, decision: CardDecision) -> dict:
        """在回灌大模型的视图中注入卡片呈现状态（打通双系统认知）。"""
        out = {k: v for k, v in result.items() if k != "trend"} if "trend" in result else dict(result)
        out["_ui_card"] = {
            "status": decision.status,
            "kind": decision.kind,
            "summary": decision.summary,
            "reason": decision.reason,
        }
        return out


DECISION_AGENT = DecisionAgent()
