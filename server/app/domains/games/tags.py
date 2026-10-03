"""游戏标签：中英文名对照表的唯一维护者 + 标签读写。

- 标签本体 `game_tags`（一行一个 tagid + 票重）由爬取链路整批替换写入；
- 标签名只存在 `tags` 一处（`name_zh` / `name_en`），消费侧拿到的一律是
  这里的名字，别处不再维护第二份名字来源；
- `name_zh` / `name_en` IS NULL 是「已请求过对应语言的热门标签表、其中
  没有这个 tagid」的占位行，靠它把「遇到无法对照的 ID 才请求一次」落到实处
  （否则每轮爬取都重发）。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core import database as database_module
from app.core.database import WritePriority, write_gate
from app.crawler.utils import get_beijing_time_obj
from app.domains.games.models import GameTag, Tag
from app.domains.games.tag_names_en import TAG_NAMES_EN
from app.domains.games.tag_names_zh import TAG_NAMES_ZH


def _now():
    return get_beijing_time_obj().replace(tzinfo=None)


def _session_factory():
    """会话工厂走模块属性查找：测试夹具统一替换 app.core.database 上的它，
    直接 `from ... import` 会让本模块绕过夹具去读写真实库。"""
    return database_module.get_session_factory()


async def seed_names() -> int:
    """内置热门标签中英文名底座落库（幂等）。返回写入行数。

    缺行补整行；存量行只补缺失的 name_en（旧底座只有中文名），已存的
    名字一律不动。
    """
    tagids = TAG_NAMES_ZH.keys() | TAG_NAMES_EN.keys()
    rows = [
        {
            "tagid": int(tagid),
            "name_zh": TAG_NAMES_ZH.get(int(tagid)),
            "name_en": TAG_NAMES_EN.get(int(tagid)),
            "updated_at": _now(),
        }
        for tagid in tagids
    ]
    async with write_gate(WritePriority.BACKGROUND), _session_factory()() as session:
        existing = {
            int(tagid): name_en
            for tagid, name_en in (
                await session.execute(select(Tag.tagid, Tag.name_en))
            ).all()
        }
        missing = [r for r in rows if r["tagid"] not in existing]
        if missing:
            await session.execute(
                sqlite_insert(Tag)
                .values(missing)
                .on_conflict_do_nothing(index_elements=[Tag.tagid])
            )
        en_backfill = [
            {"tagid": r["tagid"], "name_en": r["name_en"], "updated_at": _now()}
            for r in rows
            if r["tagid"] in existing and existing[r["tagid"]] is None and r["name_en"]
        ]
        if en_backfill:
            insert_en = sqlite_insert(Tag)
            await session.execute(
                insert_en.values(en_backfill).on_conflict_do_update(
                    index_elements=[Tag.tagid],
                    set_={
                        "name_en": insert_en.excluded.name_en,
                        "updated_at": insert_en.excluded.updated_at,
                    },
                )
            )
        await session.commit()
    return len(missing) + len(en_backfill)


async def missing_tag_ids(tagids: Iterable[int]) -> set[int]:
    """库里连行都没有的 tagid = 触发一次懒请求的候选。"""
    ids = {int(t) for t in tagids}
    if not ids:
        return set()
    async with _session_factory()() as session:
        found = set(
            (await session.scalars(select(Tag.tagid).where(Tag.tagid.in_(ids)))).all()
        )
    return ids - found


async def upsert_names(
    names_zh: Mapping[int, str | None],
    names_en: Mapping[int, str | None] | None = None,
) -> int:
    """批量写名字；值为 None 即该语言未收录（占位）。

    冲突更新按列 COALESCE：任一语言的 None 不覆盖已存的非空值，两种语言
    独立补齐互不洗掉。
    """
    names_en = names_en or {}
    tagids = names_zh.keys() | names_en.keys()
    if not tagids:
        return 0
    now = _now()
    rows = [
        {
            "tagid": int(tagid),
            "name_zh": names_zh.get(int(tagid)),
            "name_en": names_en.get(int(tagid)),
            "updated_at": now,
        }
        for tagid in tagids
    ]
    stmt = sqlite_insert(Tag).values(rows)
    async with write_gate(WritePriority.BACKGROUND), _session_factory()() as session:
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[Tag.tagid],
                set_={
                    "name_zh": func.coalesce(stmt.excluded.name_zh, Tag.name_zh),
                    "name_en": func.coalesce(stmt.excluded.name_en, Tag.name_en),
                    "updated_at": stmt.excluded.updated_at,
                },
            )
        )
        await session.commit()
    return len(rows)


async def replace_game_tags_batch(
    entries: Iterable[tuple[int, Sequence[tuple[int, int]]]],
) -> int:
    """整批替换若干游戏的标签集合（先删后插，保证与最新响应一致）。返回写入行数。

    删除按主键前缀（appid）走，插入一发多行；票重随行保存，标签顺序即票重降序。
    """
    appids: list[int] = []
    rows: list[dict] = []
    for appid, tags in entries:
        appid = int(appid)
        appids.append(appid)
        for tagid, weight in tags:
            rows.append({"appid": appid, "tagid": int(tagid), "weight": int(weight)})
    if not appids:
        return 0
    async with write_gate(WritePriority.BACKGROUND), _session_factory()() as session:
        await session.execute(delete(GameTag).where(GameTag.appid.in_(appids)))
        if rows:
            await session.execute(
                sqlite_insert(GameTag)
                .values(rows)
                .on_conflict_do_nothing(index_elements=[GameTag.appid, GameTag.tagid])
            )
        await session.commit()
    return len(rows)


async def tags_by_appid(appids: Sequence[int]) -> dict[int, list[dict]]:
    """{appid: [{"tagid", "name", "nameEn", "weight"}, ...]}，按票重降序；名字可能为 None。"""
    ids = [int(a) for a in appids]
    if not ids:
        return {}
    async with _session_factory()() as session:
        rows = (
            await session.execute(
                select(
                    GameTag.appid, GameTag.tagid, GameTag.weight, Tag.name_zh, Tag.name_en
                )
                .join(Tag, Tag.tagid == GameTag.tagid, isouter=True)
                .where(GameTag.appid.in_(ids))
                .order_by(GameTag.appid, GameTag.weight.desc(), GameTag.tagid)
            )
        ).all()
    out: dict[int, list[dict]] = {}
    for appid, tagid, weight, name_zh, name_en in rows:
        out.setdefault(int(appid), []).append(
            {
                "tagid": int(tagid),
                "name": name_zh,
                "nameEn": name_en,
                "weight": int(weight),
            }
        )
    return out


def named_tags(rows: list[dict] | None) -> list[dict]:
    """展示面投影：只留至少一个语言对照到名字的标签（冷门标签留档但不进
    展示），数组顺序即票重降序 = 热门程度；前端按界面语言挑名。"""
    return [
        {"tagid": r["tagid"], "name": r["name"], "nameEn": r.get("nameEn")}
        for r in (rows or [])
        if r.get("name") or r.get("nameEn")
    ]
