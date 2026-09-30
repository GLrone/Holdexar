"""smart 排序评分（纯函数）。

五个因子取代「史低优先度硬前缀」字典序——折扣从参赛资格降级为加分项，
原价但差价大的游戏凭省钱/质量回到前排；两个认知度信号（Steam 官方榜单 /
系列 IP）把「玩家认识的名字」往前拉：

  S_smart = 0.50*S_save + 0.25*S_quality + 0.10*S_timing
          + 0.11*S_steam_board + 0.04*S_series

  S_save        = min(1, ln(1+D/20) / ln(11))            D = 差价（元），¥200 封顶
  S_quality     = p - (p-0.5)·2^(-log10(N+1))            SteamDB 置信度收缩；
                                                           N=0 时定义 0.5（无数据 = 中性，
                                                           不是差评）
  S_timing      = 原价 0 / 普通折扣 0.35 / 平史低 0.75 / 新史低 1.00
  S_steam_board = 上过 Steam 官方榜单 1.0 / 否则 0.0      账本 = monitor_sources
                                                           source='board'（热销 / 热门
                                                           新品 / 即将推出轮询落池）
  S_series      = 属于某个游戏系列 1.0 / 否则 0.0          账本 = games.series_id

两个认知度信号都**不用本作的 review_count**：N 已进 quality（置信度收缩），
认知度再用一次 N，就是同一信号被二次加权，识别不出「玩家一看名字就认识的
经典 IP」——知名系列新作评测可能只有几千却认知极高，冷门独立游戏评测三千
五百却无人识。「上没上过榜 / 是不是某个 IP 的一员」才是认知度的口径。

timing 只认 hl_flag 分档，不叠加 discount_percent——史低+90%off 是同一件事，
不重复奖励（因子相关性）。pp_flag 是近期调价方向（新闻信号），与「长期低价」
语义错位，不参与评分。

全部在 Python 侧计算（refresh_sort_cache 落库），不依赖 SQLite 数学函数
——打包产物捆绑的 sqlite 未验证 SQLITE_ENABLE_MATH_FUNCTIONS。
"""
from __future__ import annotations

import math

# ── 权重（调参改这里，不动公式）──
W_SAVE = 0.50
W_QUALITY = 0.25
W_TIMING = 0.10
W_STEAM_BOARD = 0.11
W_SERIES = 0.04

# S_save：对数压缩。差价 ¥20 起价值感陡增，¥200 之后边际价值趋零
# 归一化分母 = ln(1 + 200/20) = ln(11)

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


def steam_board_score(on_board: bool | None) -> float:
    """Steam 官方榜单因子：上过榜单（monitor_sources source='board'）= 1.0。"""
    return 1.0 if on_board else 0.0


def series_score(in_series: bool | None) -> float:
    """系列 / IP 因子：属于某个游戏系列（games.series_id 非空）= 1.0。"""
    return 1.0 if in_series else 0.0


def smart_score(
    diff_fen: int | None,
    positive_rate: int | None,
    review_count: int | None,
    hl_flag: int | None,
    discount_percent: int | None,
    *,
    steam_board: bool | None = False,
    series: bool | None = False,
) -> float:
    """V1 综合分（0~1）。五因子加权，无隐藏项。

    steam_board / series = 两个认知度开关（各自快照列，见 refresh_sort_cache）；
    缺省 False = 未认知，不影响前三个因子的公式与权重。
    """
    return (
        W_SAVE * save_score(diff_fen)
        + W_QUALITY * quality_score(positive_rate, review_count)
        + W_TIMING * timing_score(hl_flag, discount_percent)
        + W_STEAM_BOARD * steam_board_score(steam_board)
        + W_SERIES * series_score(series)
    )
