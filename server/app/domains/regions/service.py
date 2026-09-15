"""regions 域服务：区服配置的种子 / 查询 / 启用集 / 爬取生效区解析。

设计：
- CC_LIST（crawler/config.py）是唯一"全部可用区"来源，启动时种子进 crawl_regions 表；
- 启用状态只存 DB（旧的 app_settings.crawl.enabled_regions 首启迁移后删除）；
- 爬取生效区 = 显式参数（校验非法即报错，绝不静默扩大范围）> 表中 enabled 区；
- 语义上不再存在"None = 爬全区"：全禁用时任务拒绝启动。
- 展示名不带货币代号（"印度（INR）"→"印度"），代号由 currency 字段单独承载。
"""
from __future__ import annotations

import logging
import re

from sqlalchemy import select

from app.crawler.config import CC_LIST
from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from app.domains.regions.models import CrawlRegion
from app.domains.settings.models import AppSetting

logger = logging.getLogger(__name__)

# 已购游戏专用抓取区（app_settings KV）：
# - None/缺省 = 跟随全局启用集（默认，行为与历史上完全一致）
# - list     = 自定义子集（已购游戏与愿望单分道抓取）
OWNED_REGIONS_KEY = "crawl.owned_regions"

_LEGACY_KEY = "crawl.enabled_regions"

# CC_LIST 展示名里的货币后缀（"印度（INR）" → "印度"）
_CURRENCY_SUFFIX_RE = re.compile(r"（[A-Z]{3}）$")

# 首启默认勾选区数：只取 CC_LIST 前 N 区（cn/ru/kz/ua），新手默认最小集省配额
_DEFAULT_ENABLED_COUNT = 4


def display_name(raw: str) -> str:
    return _CURRENCY_SUFFIX_RE.sub("", raw).strip() or raw


async def ensure_seeded() -> None:
    """启动种子：CC_LIST → crawl_regions（新 code 补行、已有行刷新展示名/币种）；
    顺带迁移旧 app_settings 键。"""
    now = get_beijing_time_obj().replace(tzinfo=None)
    async with get_session_factory()() as session:
        rows = (await session.execute(select(CrawlRegion))).scalars().all()
        existing = {r.code: r for r in rows}

        legacy_value = None
        legacy = await session.get(AppSetting, _LEGACY_KEY)
        if legacy is not None:
            legacy_value = legacy.value_json
            await session.delete(legacy)

        changed = False
        for idx, (code, name, currency) in enumerate(CC_LIST):
            row = existing.get(code)
            if row is None:
                session.add(
                    CrawlRegion(
                        code=code, name=display_name(name), currency=currency,
                        # 首启默认只勾选前四区（新手默认最小
                        # 勾选集，避免 41 区全开耗配额）；存量行不碰，用户可自行全选
                        enabled=idx < _DEFAULT_ENABLED_COUNT,
                        sort=idx, updated_at=now,
                    )
                )
                changed = True
            elif row.name != display_name(name) or row.currency != currency:
                row.name = display_name(name)
                row.currency = currency
                row.updated_at = now
                changed = True

        # 旧键迁移：按旧启用集重设 enabled（只在首次建表时生效一次）
        if isinstance(legacy_value, list) and legacy_value:
            wanted = {str(c).lower() for c in legacy_value}
            for row in (await session.execute(select(CrawlRegion))).scalars():
                new_enabled = row.code in wanted
                if new_enabled != row.enabled:
                    row.enabled = new_enabled
                    changed = True

        if changed:
            await session.commit()


async def list_regions() -> list[dict]:
    """全量区服（含禁用标记），按 sort 排。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(CrawlRegion).order_by(CrawlRegion.sort, CrawlRegion.code)
            )
        ).scalars().all()
    return [
        {
            "code": r.code,
            "name": r.name,
            "currency": r.currency,
            "enabled": bool(r.enabled),
            "sort": r.sort,
        }
        for r in rows
    ]


async def enabled_regions() -> list[str]:
    """启用区（小写 code，按 sort 排）。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(CrawlRegion.code)
                .where(CrawlRegion.enabled.is_(True))
                .order_by(CrawlRegion.sort, CrawlRegion.code)
            )
        ).all()
    return [r[0] for r in rows]


async def set_enabled(codes: list[str] | None) -> None:
    """设置启用集：None = 全部启用；否则精确勾选（非法 code 报错）。"""
    valid = {code for code, _, _ in CC_LIST}
    if codes is None:
        wanted = valid
    else:
        wanted = {str(c).strip().lower() for c in codes}
        bad = wanted - valid
        if bad:
            raise ValueError(f"未知区服代码: {', '.join(sorted(bad))}")
    now = get_beijing_time_obj().replace(tzinfo=None)
    async with get_session_factory()() as session:
        rows = (await session.execute(select(CrawlRegion))).scalars().all()
        for row in rows:
            new_enabled = row.code in wanted
            if new_enabled != row.enabled:
                row.enabled = new_enabled
                row.updated_at = now
        await session.commit()
    # 追踪区集合变化 → 捆绑包排序快照（按追踪区过滤的派生列）整库重建：
    # 快照只能在写侧算（GET 不得现算最低价/差价），列表缓存同步失效。
    # 失败只记日志：快照沿用旧集合，等下次改动/下一轮刷新自愈。
    try:
        from app.domains.bundles import service as bundles_service

        rebuilt = await bundles_service.refresh_bundle_sort_cache()
        bundles_service.invalidate_bundles_cache()
        logger.info("追踪区变更：捆绑包排序快照重建 %d 个", rebuilt)
    except Exception:  # noqa: BLE001
        logger.exception("追踪区变更后捆绑包排序快照重建失败（快照沿用旧集合）")


async def effective_regions(explicit: list[str] | None) -> list[str]:
    """爬取生效区。显式参数校验后原样生效（任务级覆盖）；否则读启用集。

    任何分支都不再回退"全区"：配置非法抛 ValueError，全禁用抛 ValueError。
    """
    valid = {code for code, _, _ in CC_LIST}
    if explicit:
        wanted = [str(r).strip().lower() for r in explicit if str(r).strip()]
        bad = sorted({r for r in wanted if r not in valid})
        if bad:
            raise ValueError(f"未知区服代码: {', '.join(bad)}")
        deduped = list(dict.fromkeys(wanted))
        if not deduped:
            raise ValueError("区服参数为空")
        return deduped

    enabled = await enabled_regions()
    if not enabled:
        raise ValueError("未启用任何区服：请先在「我」页勾选要爬取的地区")
    return enabled


async def owned_regions() -> list[str] | None:
    """已购游戏抓取区。None = 跟随全局启用集；list = 自定义子集。"""
    from app.domains.settings.service import get_value

    value = await get_value(OWNED_REGIONS_KEY, None)
    if not isinstance(value, list) or not value:
        return None
    return [str(c).strip().lower() for c in value]


async def set_owned_regions(codes: list[str] | None) -> list[str] | None:
    """设置已购抓取区：None = 跟随启用集；列表 = 精确子集（空列表/非法 code 报错）。"""
    from app.domains.settings.service import set_value

    if codes is None:
        await set_value(OWNED_REGIONS_KEY, None)
        return None
    valid = {code for code, _, _ in CC_LIST}
    wanted = [str(c).strip().lower() for c in codes if str(c).strip()]
    bad = sorted({c for c in wanted if c not in valid})
    if bad:
        raise ValueError(f"未知区服代码: {', '.join(bad)}")
    deduped = list(dict.fromkeys(wanted))
    if not deduped:
        raise ValueError("已购抓取地区不能为空：请至少勾选一个区服，或改回「跟随监控地区」")
    await set_value(OWNED_REGIONS_KEY, deduped)
    return deduped
