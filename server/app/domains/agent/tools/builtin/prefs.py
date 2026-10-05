"""偏好工具：用户长期偏好的查询与显式保存（agent_memory 账本）。"""
from __future__ import annotations

from app.domains.agent.tools.base import ToolSpec


async def _get_user_preferences(args: dict, sid: str | None = None) -> dict:
    from app.domains.agent import memory as agent_memory
    memories = await agent_memory.list_memories()
    return {"kind": "user_preferences", "items": memories, "count": len(memories)}


async def _set_user_preference(args: dict, sid: str | None = None) -> dict:
    from app.domains.agent import memory as agent_memory
    category = str(args.get("category") or "preference")
    key = str(args.get("key") or "")
    value = args.get("value")
    if not key:
        return {"kind": "empty", "note": "key_required"}
    saved = await agent_memory.upsert_memory(
        category=category,
        key=key,
        value=value,
        confidence=1.0,
        source="user_explicit",
    )
    return {"kind": "user_preference_saved", "item": saved}


SPECS = [
    ToolSpec(
        name="get_user_preferences", group="read", risk="low",
        description="查询用户已保存的长期偏好清单（如常用区服、类型偏好、预算习惯）。用户询问『我的偏好是什么/你记住了我什么』时调用",
        parameters={"type": "object", "properties": {}},
        handler=_get_user_preferences, step_label="getPreferences",
    ),
    ToolSpec(
        name="set_user_preference", group="read", risk="medium",
        description="保存或更新用户的长期偏好。用户明确要求『记住我的偏好/记住我喜欢XX/记住我只买XX区』时调用",
        parameters={"type": "object", "properties": {
            "category": {"type": "string", "enum": ["region_preference", "genre_preference", "budget_habit", "preference"], "description": "偏好类别"},
            "key": {"type": "string", "description": "偏好键名（如 preferred_regions, liked_genres, max_budget_cny）"},
            "value": {"description": "偏好内容（可以是列表、字典或字符串/数值）"},
        }, "required": ["key", "value"]},
        handler=_set_user_preference, step_label="setPreference",
    ),
]
