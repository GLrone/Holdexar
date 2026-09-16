"""smart 排序评分（V1 公式，纯函数）。

四个因子取代 legacy 的「史低优先度硬前缀」字典序——折扣从参赛资格
降级为加分项，原价但差价大的游戏凭省钱/质量/熟悉度回到前排：

  S_smart = 0.50*S_save + 0.28*S_quality + 0.14*S_timing + 0.08*S_familiarity

  S_save        = min(1, ln(1+D/20) / ln(11))            D = 差价（元），¥200 封顶
  S_quality     = p - (p-0.5)·2^(-log10(N+1))            SteamDB 置信度收缩；
                                                           N=0 时定义 0.5（无数据 = 中性，
                                                           不是差评）
  S_timing      = 原价 0 / 普通折扣 0.35 / 平史低 0.75 / 新史低 1.00
  S_familiarity = min(1, log10(N+1) / log10(100001))     10 万评测封顶，刻意轻权重
                                                           （N 已参与 quality，只做
                                                           「淡季不全是陌生游戏」的托底）

timing 只认 hl_flag 分档，不再叠加 discount_percent——史低+90%off 是同一
件事，不重复奖励（因子相关性）。pp_flag 是近期调价方向（新闻信号），
与「长期低价」语义错位，V1 不参与。

全部在 Python 侧计算（refresh_sort_cache 落库），不依赖 SQLite 数学函数
——打包产物捆绑的 sqlite 未验证 SQLITE_ENABLE_MATH_FUNCTIONS。
"""
from __future__ import annotations

import math

# ── 权重（V1 拍板值；调参改这里，不动公式）──
W_SAVE = 0.50
W_QUALITY = 0.28
W_TIMING = 0.14
W_FAMILIARITY = 0.08

# S_save：对数压缩。差价 ¥20 起价值感陡增，¥200 之后边际价值趋零
# 归一化分母 = ln(1 + 200/20) = ln(11)

# S_familiarity：10 万评测 = 熟悉度满分
FAMILIAR_CAP_REVIEWS = 100000

# S_timing 四档（键 = hl_flag）
TIMING_FLAT_LOW = 0.75  # 平史低
TIMING_NEW_LOW = 1.00  # 新史低
TIMING_DISCOUNT = 0.35  # 打折非史低（含有折扣但无 hl 标记）


def save_score(diff_fen: int | None) -> float:
    """省钱因子：对数压缩的绝对差价（分），¥200 封顶 1.0。"""
    d = max(0, diff_fen or 0) / 100.0
    return min(1.0, math.log1p(d / 20.0) / math.log(11))


def quality_score(positive_rate: int | None, review_count: int | None) -> float:
    """质量因子：SteamDB 置信度收缩。零评测中性 0.5，负反馈游戏 <0.5。"""
    n = review_count or 0
    if n <= 0 or positive_rate is None:
        return 0.5
    p = positive_rate / 10000.0
    return p - (p - 0.5) * (2.0 ** (-math.log10(n + 1)))


def timing_score(hl_flag: int | None, discount_percent: int | None) -> float:
    """购买时机因子：无折扣一律 0（与 legacy hl_priority 的硬前缀同口径），
    有折扣再按 hl_flag 分档。"""
    if not discount_percent or discount_percent <= 0:
        return 0.0
    flag = hl_flag or 0
    if flag == 1:
        return TIMING_NEW_LOW
    if flag == 2:
        return TIMING_FLAT_LOW
    return TIMING_DISCOUNT


def familiarity_score(review_count: int | None) -> float:
    """熟悉度因子：评测规模的对数，10 万封顶。刻意轻权重（quality 已用了 N）。"""
    n = review_count or 0
    return min(1.0, math.log10(n + 1) / math.log10(FAMILIAR_CAP_REVIEWS + 1))


def smart_score(
    diff_fen: int | None,
    positive_rate: int | None,
    review_count: int | None,
    hl_flag: int | None,
    discount_percent: int | None,
) -> float:
    """V1 综合分（0~1）。四因子加权，无隐藏项。"""
    return (
        W_SAVE * save_score(diff_fen)
        + W_QUALITY * quality_score(positive_rate, review_count)
        + W_TIMING * timing_score(hl_flag, discount_percent)
        + W_FAMILIARITY * familiarity_score(review_count)
    )
