"""Agent 长期记忆：用户画像、偏好账本与提示装配。"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import delete, select

from app.core.database import WritePriority, get_session_factory, write_gate
from app.crawler.utils import get_beijing_time_obj
from app.domains.agent.models import AgentMemory

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return get_beijing_time_obj().replace(tzinfo=None)


def _new_id() -> str:
    return uuid.uuid4().hex


async def upsert_memory(
    category: str,
    key: str,
    value: Any,
    *,
    confidence: float = 1.0,
    source: str | None = None,
) -> dict:
    """写入或更新记忆项（category+key 唯一确定一行）。"""
    ts = _now()
    val_dict = value if isinstance(value, dict) else {"val": value}
    async with write_gate(WritePriority.INTERACTIVE, "agent_memory"), get_session_factory()() as session:
        stmt = select(AgentMemory).where(
            AgentMemory.category == category,
            AgentMemory.key == key,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            mid = _new_id()
            row = AgentMemory(
                id=mid,
                category=category,
                key=key,
                value=val_dict,
                confidence=confidence,
                source=source,
                created_at=ts,
                updated_at=ts,
            )
            session.add(row)
        else:
            row.value = val_dict
            row.confidence = confidence
            if source:
                row.source = source
            row.updated_at = ts
        await session.commit()
        return {
            "id": row.id,
            "category": row.category,
            "key": row.key,
            "value": row.value,
            "confidence": row.confidence,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


async def get_memory(category: str, key: str) -> dict | None:
    """按分类和键查询单条记忆。"""
    async with get_session_factory()() as session:
        stmt = select(AgentMemory).where(
            AgentMemory.category == category,
            AgentMemory.key == key,
        )
        row = (await session.execute(stmt)).scalar_one_or_none()
        if row is None:
            return None
        return {
            "id": row.id,
            "category": row.category,
            "key": row.key,
            "value": row.value,
            "confidence": row.confidence,
            "source": row.source,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }


async def list_memories(category: str | None = None) -> list[dict]:
    """查询记忆列表（按 category 与 key 排序）。"""
    async with get_session_factory()() as session:
        stmt = select(AgentMemory)
        if category:
            stmt = stmt.where(AgentMemory.category == category)
        stmt = stmt.order_by(AgentMemory.category, AgentMemory.key)
        rows = (await session.execute(stmt)).scalars().all()
        return [
            {
                "id": r.id,
                "category": r.category,
                "key": r.key,
                "value": r.value,
                "confidence": r.confidence,
                "source": r.source,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]


async def delete_memory(category: str, key: str) -> bool:
    """按分类和键删除单条记忆。"""
    async with write_gate(WritePriority.INTERACTIVE, "agent_memory"), get_session_factory()() as session:
        stmt = delete(AgentMemory).where(
            AgentMemory.category == category,
            AgentMemory.key == key,
        )
        result = await session.execute(stmt)
        await session.commit()
        return bool(result.rowcount > 0)


async def clear_memories(category: str | None = None) -> int:
    """清空记忆项。"""
    async with write_gate(WritePriority.INTERACTIVE, "agent_memory"), get_session_factory()() as session:
        stmt = delete(AgentMemory)
        if category:
            stmt = stmt.where(AgentMemory.category == category)
        result = await session.execute(stmt)
        await session.commit()
        return int(result.rowcount or 0)


async def build_profile_context(min_confidence: float = 0.5) -> str | None:
    """查询高置信度偏好，装配为模型系统提示的背景文本。"""
    memories = await list_memories()
    valid = [m for m in memories if (m.get("confidence") or 0.0) >= min_confidence]
    if not valid:
        return None

    labels = {
        "region_preference": "常用关注区服",
        "genre_preference": "游戏类型与标签偏好",
        "budget_habit": "价格与预算习惯",
        "user_profile": "用户习惯",
    }
    grouped: dict[str, list[str]] = {}
    for m in valid:
        cat = m["category"]
        val = m["value"]
        text_val = ""
        if isinstance(val, dict):
            if "val" in val:
                v = val["val"]
                text_val = ", ".join(v) if isinstance(v, list) else str(v)
            elif "items" in val:
                text_val = ", ".join(val["items"])
            else:
                text_val = ", ".join(f"{k}: {v}" for k, v in val.items())
        elif isinstance(val, list):
            text_val = ", ".join(str(x) for x in val)
        else:
            text_val = str(val)

        label = labels.get(cat, cat)
        key_name = m["key"]
        grouped.setdefault(label, []).append(f"{key_name}: {text_val}")

    lines = ["【用户已知长期偏好】（仅供决策与回答参考，不作为硬性限制）："]
    for cat_label, items in grouped.items():
        lines.append(f"- {cat_label}：{'; '.join(items)}")
    return "\n".join(lines)


_REGION_NAME_MAP = {
    "阿根廷": "AR",
    "土耳其": "TR",
    "国区": "CN",
    "日区": "JP",
    "美区": "US",
    "港区": "HK",
    "俄区": "RU",
    "巴西": "BR",
}

_GENRE_KEYWORDS = (
    "肉鸽", "Roguelike", "魂类", "魂系", "二次元", "开放世界",
    "动作", "模拟经营", "策略", "解谜", "恐怖", "FPS", "独立游戏",
)


async def extract_preferences_from_text(text: str, source: str = "auto_extract") -> list[dict]:
    """从文本中启发式提取显式偏好并写入记忆账本。"""
    if not text:
        return []
    extracted: list[dict] = []

    # 1. 区服偏好
    found_regions: list[str] = []
    for zh, code in _REGION_NAME_MAP.items():
        if zh in text or re.search(rf"\b{code}\b", text, re.IGNORECASE):
            if code not in found_regions:
                found_regions.append(code)
    if found_regions:
        rec = await upsert_memory(
            category="region_preference",
            key="preferred_regions",
            value=found_regions,
            confidence=0.85,
            source=source,
        )
        extracted.append(rec)

    # 2. 类型偏好
    found_genres: list[str] = []
    for g in _GENRE_KEYWORDS:
        if g in text and g not in found_genres:
            found_genres.append(g)
    if found_genres:
        rec = await upsert_memory(
            category="genre_preference",
            key="preferred_genres",
            value=found_genres,
            confidence=0.8,
            source=source,
        )
        extracted.append(rec)

    # 3. 史低 / 预算偏好
    if "史低" in text:
        rec = await upsert_memory(
            category="budget_habit",
            key="deal_timing",
            value="史低优先",
            confidence=0.8,
            source=source,
        )
        extracted.append(rec)

    budget_match = re.search(r"(?:预算|价格)(?:不超过|低于|在)\s*(\d+)\s*(?:块|元)?", text)
    if budget_match:
        limit = int(budget_match.group(1))
        rec = await upsert_memory(
            category="budget_habit",
            key="max_budget_cny",
            value=limit,
            confidence=0.9,
            source=source,
        )
        extracted.append(rec)

    return extracted
