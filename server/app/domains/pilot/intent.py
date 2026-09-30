"""pilot 意图路由：确定性关键词分派。

固定意图集合用词表直接命中，未命中交给 LLM 自由问答。意图是内部运行
信息（ask 返回体不携带），不进用户文案。

写意图风险门（确定性，白名单制）：只暴露加关注 / 设提醒两个单对象、
可逆、用户当句显式请求的动作；问句含批量 / 删除 / 停用类守卫词时一律
不给写工具，落回只读或指引。"""
from __future__ import annotations

# 守卫词：命中即禁用写意图（风险门的确定性部分）
_GUARD_WORDS = ("清空", "删除", "移除", "移出", "停用", "停止", "批量", "全部", "所有")

# 怎么做类（指引）：疑问词用复合词形，「今天天气怎么样」这类日常寒暄不会命中
_HOW_TO_WORDS = (
    "怎么设置", "怎么添加", "怎么加", "怎么弄", "如何设置", "如何添加", "在哪",
    "哪里设置", "怎么用", "教程",
)
# 设提醒类：渠道词（提醒 / 告诉我）+ 条件词（低于 / 史低 等）同时出现
_ALERT_CHANNEL_WORDS = ("提醒", "告诉我")
_ALERT_COND_WORDS = ("低于", "以下", "史低", "跌到", "跌至", "降至", "打")
# 关注类
_MONITOR_WORDS = ("加进关注", "加入关注", "关注一下", "帮我关注", "关注")
# 价格 / 找游戏类
_PRICE_WORDS = (
    "价格", "史低", "最低价", "值不值", "值得买", "值得入手", "多少钱", "贵不",
    "几折", "折扣", "降价", "打折吗", "入手", "等等党", "还差多少",
)
_FIND_WORDS = (
    "推荐", "找一个", "找几", "找款", "类似", "有没有好玩", "特卖", "便宜的游戏",
    "好玩的游戏", "小众", "捡漏",
)

HOW_TO = "how_to"
CREATE_ALERT = "create_alert"
ADD_MONITOR = "add_monitor"
PRICE_ANALYSIS = "price_analysis"
FIND_GAMES = "find_games"
CHAT = "chat"


def _looks_like_alert(q: str) -> bool:
    has_channel = any(w in q for w in _ALERT_CHANNEL_WORDS)
    has_condition = any(w in q for w in _ALERT_COND_WORDS)
    return has_channel and has_condition


def route(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return CHAT
    guarded = any(w in q for w in _GUARD_WORDS)
    if not guarded and any(w in q for w in _HOW_TO_WORDS):
        return HOW_TO
    if not guarded and _looks_like_alert(q):
        return CREATE_ALERT
    if not guarded and any(w in q for w in _MONITOR_WORDS):
        return ADD_MONITOR
    if any(w in q for w in _PRICE_WORDS):
        return PRICE_ANALYSIS
    if any(w in q for w in _FIND_WORDS):
        return FIND_GAMES
    if not guarded and any(w in q for w in _HOW_TO_WORDS):
        # 守卫命中时疑问句落指引（教用户自己去操作），不放行写工具
        return HOW_TO
    return CHAT
