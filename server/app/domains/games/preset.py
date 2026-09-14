"""预设游戏池登记：导入文件（关注数据）与 TOP 榜两条来源的 appid 账本。

预设 = 本机导入的精选游戏（池页「导入文件」） + TOP 榜 5 页（初始游戏库）。
两条来源写同一张 preset_games 表：本机侧它们照常走既有通道（导入入池 +
首爬、榜单反哺入库），本表只做**登记**；导出资产种子（scripts/export_seed.py）
时整表随包下发，客户端并入（seed_assets.merge_preset_seed）把缺行的 appid
落成 games 行——新用户开箱即有初始游戏库，全池主轮随即接管价格刷新。

语义边界：
- 本模块不触发爬取、不参与调度（落库/爬取由调用方负责）；
- 导入来源只增不改：同一 appid 重复导入保留首次来源与落档时间；
- 榜单来源整批替换：预设的 TOP 部分 = 最近一次成功抓取的榜（清单大小
  稳定在榜单封顶内，不随历史累积膨胀）；
- 登记失败不应阻断主链路：调用方吞异常记日志。
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import get_session_factory
from app.domains.games.models import Game, PresetGame

logger = logging.getLogger(__name__)

# 榜单来源标记（与导出/并入侧约定同名；文件来源直接用文件名）
BOARD_SOURCE = "topsellers"
MAX_SOURCE_LEN = 80


def _clean(appids: list[int]) -> list[int]:
    """去重 + 过滤非法 appid（稳定升序，落库顺序可预期）。"""
    out: set[int] = set()
    for a in appids:
        try:
            appid = int(a)
        except (TypeError, ValueError):
            continue
        if appid > 0:
            out.add(appid)
    return sorted(out)


async def _names_of(session, appids: list[int]) -> dict[int, str | None]:
    """库内已有名字随登记带回（未爬过的新条目留空，导出侧再兜底一次）。"""
    if not appids:
        return {}
    rows = await session.execute(
        select(Game.appid, Game.name).where(Game.appid.in_(appids))
    )
    return {int(a): n for a, n in rows}


async def record_imported(appids: list[int], source: str) -> int:
    """登记「导入文件」来源的 appid（source = 文件名）。返回新登记行数。

    只增不改（INSERT OR IGNORE 语义）：同一 appid 重复导入（换文件、重导入）
    保留首次来源与落档时间——预设清单是首见即定档的出厂集。
    """
    clean = _clean(appids)
    src = (source or "").strip()[:MAX_SOURCE_LEN]
    if not clean or not src:
        return 0

    async with get_session_factory()() as session:
        existing = {
            int(a)
            for (a,) in await session.execute(
                select(PresetGame.appid).where(PresetGame.appid.in_(clean))
            )
        }
        fresh = [a for a in clean if a not in existing]
        if fresh:
            names = await _names_of(session, fresh)
            now = datetime.now()
            await session.execute(
                sqlite_insert(PresetGame).values(
                    [
                        {"appid": a, "name": names.get(a), "source": src, "added_at": now}
                        for a in fresh
                    ]
                )
            )
            await session.commit()
        return len(fresh)


async def record_board(appids: list[int], source: str = BOARD_SOURCE) -> int:
    """登记榜单来源（整批替换）：先清该来源旧行，再落本轮榜。

    与导入来源共表时 appid 冲突保留首次行（导入优先）——已登记过的游戏
    不需要榜单再补。榜单抓取失败（空集）由调用方拦住，不触发替换。
    """
    clean = _clean(appids)
    if not clean:
        return 0

    async with get_session_factory()() as session:
        await session.execute(delete(PresetGame).where(PresetGame.source == source))
        names = await _names_of(session, clean)
        now = datetime.now()
        await session.execute(
            sqlite_insert(PresetGame)
            .values(
                [
                    {"appid": a, "name": names.get(a), "source": source, "added_at": now}
                    for a in clean
                ]
            )
            .on_conflict_do_nothing(index_elements=["appid"])
        )
        await session.commit()
        return len(clean)
