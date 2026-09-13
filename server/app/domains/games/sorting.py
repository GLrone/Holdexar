"""排序规则统一管理（SQLAlchemy 表达式版）。

两层查询（预计算列层 / 地区 CTE 层）使用完全一致的排序规则，
五级排序：

  1. Is Unlocked:  国区价格存在且 > 0
  2. HL Priority:  史低优先度 (0-4)
  3. Diff Tier:    差价梯队 (每10元=1000分一档)
  4. Review Tier:  评测梯队 (每1000条一档)
  5. Positive Rate: 好评率

HL Priority:
  0 = 无折扣
  4 = 新史低(hl=1) 且 评测数 > 1000
  3 = 新史低(hl=1) 且 评测数 <= 1000, 或 平史低(hl=2)
  2 = 非史低但打折(hl=3)
  1 = 有折扣但无 hl 标记
"""
from __future__ import annotations

from sqlalchemy import ColumnElement, case, desc

from app.domains.games.models import Game, GameCurrentPrice


def hl_priority(g: type[Game], cn: type[GameCurrentPrice]) -> ColumnElement[int]:
    """史低优先度（对齐 HL_PRIORITY_SQL）。"""
    return case(
        (cn.discount_percent == 0, 0),
        (cn.discount_percent.is_(None), 0),
        (g.hl_flag == 1, case((g.review_count > 1000, 4), else_=3)),
        (g.hl_flag == 2, 3),
        (g.hl_flag == 3, 2),
        else_=1,
    )


def complex_sort(g: type[Game], cn: type[GameCurrentPrice]) -> list[ColumnElement]:
    """5 级复杂排序（对齐 COMPLEX_SORT_MV/CTE；整数除法即梯队取整）。"""
    return [
        desc(cn.price.is_not(None) & (cn.price > 0)),
        desc(hl_priority(g, cn)),
        desc(g.diff_fen / 1000),
        desc(g.review_count / 1000),
        desc(g.positive_rate),
    ]


def region_group_prefix(g: type[Game], cn: type[GameCurrentPrice]) -> list[ColumnElement[int]]:
    """地区筛选 3-group 前缀（对齐 buildCteOrderBy）：史低组(0) → 折扣组(1) → 无折扣组(2)。"""
    return [
        case(
            (g.hl_flag.in_((1, 2)), 0),
            (cn.discount_percent > 0, 1),
            else_=2,
        )
    ]


def build_order_by(
    g: type[Game],
    cn: type[GameCurrentPrice],
    *,
    sort: str,
    region_mode: bool = False,
    sr: type[GameCurrentPrice] | None = None,
) -> list[ColumnElement]:
    """构建 ORDER BY（对齐 buildMvOrderBy / buildCteOrderBy 的组合规则）。

    region_mode=True（选择了具体地区）：组内默认按该区差价 (cn.price - sr.cny_fen) 降序；
    其余排序按对应分支，前缀带 3-group。
    """
    prefix = region_group_prefix(g, cn) if region_mode else []

    if region_mode and sr is not None:
        # 地区模式组内默认：差价降序 → 好评率（global/cheaper/highdiff 共用）
        if sort == "rate":
            return prefix + [desc(g.review_count / 1000), desc(g.positive_rate), desc(g.appid)]
        if sort == "new2026":
            return prefix + [desc(g.release_date), desc(g.appid)]
        return prefix + [desc(cn.price - sr.cny_fen), desc(g.positive_rate), desc(g.appid)]

    if sort == "rate":
        return prefix + [desc(g.review_count / 1000), desc(g.positive_rate), desc(g.appid)]

    if sort == "discount":
        return prefix + [desc(cn.discount_percent)] + complex_sort(g, cn) + [desc(g.appid)]

    if sort == "new2026":
        return prefix + [desc(g.release_date), desc(g.appid)]

    if sort == "updated":
        # 最近变动优先（降价动态 feed 用；ix_games_updated 索引覆盖）
        return prefix + [desc(g.updated_at), desc(g.appid)]

    if sort == "diff":
        return prefix + [desc(g.diff_fen / 500)] + complex_sort(g, cn) + [desc(g.appid)]

    # default：5 级复杂排序
    return prefix + complex_sort(g, cn) + [desc(g.appid)]
