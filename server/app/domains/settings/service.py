"""settings 域服务：键值读写。

已知键：
- account.steam_id          SteamID64
- account.steam_api_key     Steam Web API Key（用户自己的，明文只存本地库）

区服配置已迁至 domains/regions（crawl_regions 表 + GET /api/v1/regions）。
"""
from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import get_session_factory
from app.domains.settings.models import AppSetting
from app.crawler.utils import get_beijing_time_obj


async def get_value(key: str, default=None):
    async with get_session_factory()() as session:
        row = await session.get(AppSetting, key)
        return row.value_json if row else default


async def delete_value(key: str) -> None:
    """删除键（下架功能的设置项清账用，不存在时静默）。"""
    from sqlalchemy import delete

    async with get_session_factory()() as session:
        await session.execute(delete(AppSetting).where(AppSetting.key == key))
        await session.commit()


async def set_value(key: str, value) -> None:
    now = get_beijing_time_obj().replace(tzinfo=None)
    async with get_session_factory()() as session:
        stmt = sqlite_insert(AppSetting).values(
            key=key, value_json=value, updated_at=now
        )
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value_json": stmt.excluded.value_json, "updated_at": stmt.excluded.updated_at},
            )
        )
        await session.commit()
