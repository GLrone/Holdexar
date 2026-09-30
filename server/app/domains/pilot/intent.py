"""pilot 意图路由：确定性关键词分派。

固定意图集合用词表直接命中，未命中交给 LLM 自由问答。意图是内部运行
信息（ask 返回体不携带），不进用户文案。
"""
from __future__ import annotations

# 怎么做类（关注/提醒/设置的指引）优先：这类问句常含价格词，先拦避免误分；
# 疑问词用复合词形，「今天天气怎么样」这类日常寒暄不会命中「怎么样」
_HOW_TO_WORDS = (
    "怎么设置", "怎么添加", "怎么加", "怎么弄", "如何设置", "如何添加", "在哪",
    "哪里设置", "提醒我", "告诉我一声", "加进关注", "加入关注", "关注一下",
    "帮我关注", "怎么用",
)
_PRICE_WORDS = (
    "价格", "史低", "最低价", "值不值", "值得买", "值得入手", "多少钱", "贵不",
    "几折", "折扣", "降价", "打折吗", "入手", "等等党", "还差多少",
)
_FIND_WORDS = (
    "推荐", "找一个", "找几", "找款", "类似", "有没有好玩", "特卖", "便宜的游戏",
    "好玩的游戏", "小众", "捡漏",
)

HOW_TO = "how_to"
PRICE_ANALYSIS = "price_analysis"
FIND_GAMES = "find_games"
CHAT = "chat"


def route(question: str) -> str:
    q = (question or "").strip()
    if not q:
        return CHAT
    if any(w in q for w in _HOW_TO_WORDS):
        return HOW_TO
    if any(w in q for w in _PRICE_WORDS):
        return PRICE_ANALYSIS
    if any(w in q for w in _FIND_WORDS):
        return FIND_GAMES
    return CHAT
