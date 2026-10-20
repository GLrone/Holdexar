"""预设登记将本机导入来源与可分发的公开榜单来源分开标记。"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import WritePriority, write_session
from app.domains.games.models import Game, PresetGame

logger = logging.getLogger(__name__)

# 榜单来源标记（与导出/并入侧约定同名）
BOARD_SOURCE = "topsellers"
MAX_SOURCE_LEN = 80
IMPORT_SOURCE_PREFIX = "file:"


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
    """登记本机导入来源的 appid，返回新登记行数。"""
    clean = _clean(appids)
    raw_source = (source or "").strip()
    if not clean or not raw_source:
        return 0
    src = f"{IMPORT_SOURCE_PREFIX}{raw_source[:MAX_SOURCE_LEN - len(IMPORT_SOURCE_PREFIX)]}"

    async with write_session(WritePriority.BACKGROUND) as session:
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
    """登记本轮公开榜单来源；公开来源覆盖同一游戏的本机导入标记。"""
    clean = _clean(appids)
    if not clean:
        return 0

    async with write_session(WritePriority.BACKGROUND) as session:
        await session.execute(delete(PresetGame).where(PresetGame.source == source))
        names = await _names_of(session, clean)
        now = datetime.now()
        statement = sqlite_insert(PresetGame).values(
            [
                {"appid": a, "name": names.get(a), "source": source, "added_at": now}
                for a in clean
            ]
        )
        await session.execute(
            statement.on_conflict_do_update(
                index_elements=["appid"],
                set_={
                    "name": statement.excluded.name,
                    "source": statement.excluded.source,
                    "added_at": statement.excluded.added_at,
                },
            )
        )
        await session.commit()
        return len(clean)
