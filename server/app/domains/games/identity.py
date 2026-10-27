"""价格历史行的商品身份：统一谓词与显式未知态（唯一事实源）。

归属三要素：购买商品（sub_id / version_suffix / is_gold / is_bundle，
或显式 product_unknown=1）、地区（region_code）、来源（source）。
「标准版」与「商品身份未知」的判定只在此定义——写入侧（db_writer /
community_history）与全部读取侧（games/service 五处、crawl/events 证据
查询）一律引用本模块，禁止再手抄谓词（历史上六处手抄多次漂移）。

商品身份未知的行（社区历史源的 appid 级序列，上游不区分版本/金版/捆绑）
永远不并入标准版或任何具体版本——未知保持未知，展示层按独立序列呈现。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import and_, or_


def standard_product_where(cols: Any):
    """标准版谓词（SQLAlchemy 表达式）。

    cols = 模型类或别名，需带 is_gold / version_suffix / is_bundle /
    product_unknown 四列属性。空值兼容旧库行（迁移补列前写入的 NULL）。
    """
    return and_(
        or_(cols.is_gold.is_(False), cols.is_gold.is_(None)),
        or_(cols.version_suffix.is_(None), cols.version_suffix == ""),
        or_(cols.is_bundle.is_(False), cols.is_bundle.is_(None)),
        or_(cols.product_unknown.is_(False), cols.product_unknown.is_(None)),
    )


# 原生 SQL 侧同一谓词（crawl/events 的证据查询用）
STANDARD_PRODUCT_SQL = (
    "(is_gold IS NULL OR is_gold = 0) "
    "AND (version_suffix IS NULL OR version_suffix = '') "
    "AND (is_bundle IS NULL OR is_bundle = 0) "
    "AND (product_unknown IS NULL OR product_unknown = 0)"
)


def is_standard_row(row: dict) -> bool:
    """写入侧行字典的标准版判定（与 SQL 谓词同口径）。"""
    return (
        not row.get("is_gold")
        and not (row.get("version_suffix") or "")
        and not row.get("is_bundle")
        and not row.get("product_unknown")
    )
