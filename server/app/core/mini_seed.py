"""小种子（holdexar_mini_seed.db）：目录身份 + 人工列 + 捆绑包主档的轻量豁免资产。

覆盖去种子化后三块难以恢复的数据（全量目录、人工策划列、捆绑包主档），
**不含任何价格数据**（现价 / 历史价 / 捆绑包区价一概不进种子，由客户端
采集逐步建立）。资产随安装包携带（离线可用、无下载链），严格文件名 +
结构校验（sqlite quick_check + seed_meta 语义 + 表集全等），不做 .db
通配。导入走 write_session/write_gate 单一写入口，启动链后台分批执行，
不阻塞开门；同版本幂等跳过，失败留日志可重试。

合并语义（union-only，绝不回写覆盖本地事实）：
- catalog：只补缺失的 games 行（身份 + 名称 + 类型），updated_at 记种子
  数据时刻（非「已成功抓取」——价格覆盖由现价表缺席如实表达「尚未抓价」；
  非空 updated_at 同时保证孤儿补抓层不会把目录全集捡进爬取队列）；
- curated：本地已有行走「只补空/正向并集」的 UPDATE（xgp_tier 无本地
  写者为权威覆写例外）；不凭快照回写活动事件，在线 Epic/HB 自动更新链
  照常工作且不被旧快照降级；
- bundle_index：只补缺失的捆绑包主档（ID/名称/链接形态/购买语义/成员），
  不写现价、排序快照；item_kind 保持原值（含 -1 未知），不凭数字猜
  sub/bundle。

种子语义边界（seed_meta 携带，导入时随日志留痕）：目录为快照不是全
Steam 清单；XGP 为快照非历史上下架流；Epic/HB 只含既有字段能表达的
历史。XGP 快照 as-of 由 marker 与 seed_meta 的 source_data_as_of 记录。
"""
from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import PROJECT_ROOT
from app.core.database import WritePriority, write_session
from app.core.logging import log_event
from app.domains.games.models import Bundle, Game

logger = logging.getLogger(__name__)

MINI_SEED_FILENAME = "holdexar_mini_seed.db"
MINI_SEED_SCHEMA = 1
# 幂等 marker：值 = 种子 exported_at_utc（版本变化才重并）
MARKER_KEY = "miniseed.imported_version"

# 允许出现在小种子里的表集（缺一或多一都按坏档拒绝）
_EXPECTED_TABLES = {"seed_meta", "catalog", "curated", "bundle_index"}

# 分块行数（SQLite 变量上限约束，与既有种子期通道同型）
_CHUNK = 500


def mini_seed_candidates() -> list[Path]:
    """小种子候选路径（按序探测）：开发态 assets/seed；打包态版本目录
    与 _MEIPASS 下的 seed/。数据目录不参与——本资产随包发行，无下载链。"""
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):  # pragma: no cover - 打包态
        meipass = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates += [
            Path(sys.executable).resolve().parent / "seed",
            meipass / "seed",
        ]
    else:
        candidates.append(PROJECT_ROOT / "assets" / "seed")
    return candidates


def mini_seed_path() -> Path | None:
    for candidate in mini_seed_candidates():
        path = candidate / MINI_SEED_FILENAME
        if path.is_file():
            return path
    return None


def read_mini_meta(path: Path) -> dict:
    """读 seed_meta KV + 结构校验；文件缺失/损坏/结构不符返回空 dict
    （调用方按无小种子处理，坏档只留日志不阻塞启动）。"""
    if not path.is_file():
        return {}
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            if tables != _EXPECTED_TABLES:
                raise ValueError(f"表集不符：{sorted(tables)}")
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("quick_check 未通过")
            meta = dict(con.execute("SELECT key, value FROM seed_meta").fetchall())
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001 —— 坏种子等同无种子
        log_event(
            logger,
            "小种子读取失败，按无小种子处理",
            tag="忽略",
            level=logging.WARNING,
            detail={"路径": str(path), "原因": str(e)},
        )
        return {}
    if str(meta.get("schema_version") or "") != str(MINI_SEED_SCHEMA):
        log_event(
            logger,
            "小种子 schema 版本不认识，跳过导入",
            tag="忽略",
            level=logging.WARNING,
            detail={"schema": meta.get("schema_version")},
        )
        return {}
    if (
        str(meta.get("price_history_included") or "") != "false"
        or str(meta.get("current_prices_included") or "") != "false"
    ):
        log_event(
            logger,
            "小种子声明含价格数据，拒绝导入",
            tag="拒绝",
            level=logging.WARNING,
        )
        return {}
    return meta


def _seed_rows(path: Path, table: str, columns: tuple[str, ...]) -> list[dict]:
    con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        col_sql = ", ".join(columns)
        rows = con.execute(f"SELECT {col_sql} FROM {table}").fetchall()
    finally:
        con.close()
    return [dict(zip(columns, row)) for row in rows]


def _parse_stamp(meta: dict) -> datetime:
    try:
        return datetime.fromisoformat(str(meta.get("exported_at_utc") or "").replace("+00:00", ""))
    except ValueError:
        return datetime.now()


# ── 三个合并段（每段独立小事务分块，段间交还写者位）──────────────────


async def _merge_catalog(path: Path, stamp: datetime) -> int:
    from app.domains.games.derived import update_price_derived

    rows = _seed_rows(path, "catalog", ("appid", "name", "name_en", "type"))
    async with write_session(WritePriority.BACKGROUND, label="mini_seed_catalog") as session:
        # 整列读取 + 集合差：全量 in_ 会撞 SQLite 绑定变量上限（捆绑包段 3.5 万）
        present = {int(a) for (a,) in await session.execute(select(Game.appid))}
        missing = [
            {
                "appid": int(r["appid"]),
                "name": r["name"] or str(r["appid"]),
                "name_en": r["name_en"],
                "type": r["type"],
                # 种子数据时刻而非当下：目录行「尚未抓价」由现价表缺席表达，
                # 非空 updated_at 保证孤儿补抓不把目录全集捡进爬取队列
                "updated_at": stamp,
            }
            for r in rows
            if int(r["appid"]) not in present
        ]
        for i in range(0, len(missing), _CHUNK):
            await session.execute(
                sqlite_insert(Game).values(missing[i : i + _CHUNK])
            )
        await update_price_derived([r["appid"] for r in missing], session=session, catalog_before={})
        await session.commit()
        return len(missing)


# 人工列合并口径：种子是冷启动引导而非权威——本地有在役编辑器/在线更新
# 链的列只补空/正向并集、不降级；xgp_tier 无本地写者，种子保持权威覆写
_CURATED_SETS = {
    "xgp_tier": ":xgp_tier",
    "is_epic": "CASE WHEN :is_epic = 1 THEN 1 ELSE games.is_epic END",
    "epic_date": (
        "CASE WHEN (games.epic_date IS NULL OR games.epic_date = '') "
        "THEN :epic_date ELSE games.epic_date END"
    ),
    "is_hb": "CASE WHEN :is_hb = 1 THEN 1 ELSE games.is_hb END",
    "hb_data": (
        "CASE WHEN (games.hb_data IS NULL OR games.hb_data = '') "
        "THEN :hb_data ELSE games.hb_data END"
    ),
    "series_id": (
        "CASE WHEN games.series_id IS NULL OR games.series_id = '' "
        "THEN :series_id ELSE games.series_id END"
    ),
}


async def _merge_curated(path: Path) -> int:
    rows = _seed_rows(
        path,
        "curated",
        ("appid", "xgp_tier", "epic_date", "is_epic", "is_hb", "hb_data", "series_id"),
    )
    sets_sql = ", ".join(f"{c} = {expr}" for c, expr in _CURATED_SETS.items())
    async with write_session(WritePriority.BACKGROUND, label="mini_seed_curated") as session:
        present = {int(a) for (a,) in await session.execute(select(Game.appid))}
        params = [
            {**{k: r[k] for k in r if k != "appid"}, "appid": int(r["appid"])}
            for r in rows
            if int(r["appid"]) in present
        ]
        for i in range(0, len(params), _CHUNK):
            await session.execute(
                text(f"UPDATE games SET {sets_sql} WHERE appid = :appid"),
                params[i : i + _CHUNK],
            )
        await session.commit()
        return len(params)


async def _merge_bundle_index(path: Path) -> int:
    rows = _seed_rows(
        path,
        "bundle_index",
        ("bundle_id", "item_kind", "name", "app_ids", "must_purchase_as_set"),
    )
    async with write_session(WritePriority.BACKGROUND, label="mini_seed_bundles") as session:
        present = {
            int(b) for (b,) in await session.execute(select(Bundle.bundle_id))
        }
        missing = []
        for r in rows:
            if int(r["bundle_id"]) in present:
                continue
            # app_ids 在种子侧是 JSON 文本，主库列为 JSON 类型——必须解析，
            # 直接透传字符串会被再编码成 JSON 字符串
            try:
                members = json.loads(r["app_ids"]) if r["app_ids"] else None
            except (TypeError, ValueError):
                members = None
            missing.append(
                {
                    "bundle_id": int(r["bundle_id"]),
                    "name": r["name"] or f"bundle-{r['bundle_id']}",
                    "item_kind": r["item_kind"] if r["item_kind"] is not None else -1,
                    "must_purchase_as_set": (
                        r["must_purchase_as_set"]
                        if r["must_purchase_as_set"] is not None
                        else -1
                    ),
                    "app_ids": members,
                }
            )
        for i in range(0, len(missing), _CHUNK):
            await session.execute(
                sqlite_insert(Bundle).values(missing[i : i + _CHUNK])
            )
        await session.commit()
        return len(missing)


async def import_mini_seed(seed_path: Path | None = None) -> dict | None:
    """启动链入口：小种子在位且版本新才合并；全程不抛（失败留日志可重试）。"""
    path = seed_path or mini_seed_path()
    meta = read_mini_meta(path) if path else {}
    if not meta:
        return None
    version = str(meta.get("exported_at_utc") or "")

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(MARKER_KEY)
    if marker == version:
        return None

    stamp = _parse_stamp(meta)
    stats: dict = {}
    try:
        stats["games"] = await _merge_catalog(path, stamp)
        stats["curated"] = await _merge_curated(path)
        stats["bundles"] = await _merge_bundle_index(path)
    except Exception as e:  # noqa: BLE001 —— 不阻塞启动；下次启动重试
        log_event(
            logger,
            "小种子导入失败，下次启动重试",
            tag="未完成",
            level=logging.ERROR,
            exc_info=True,
            detail={"原因": str(e), "已完成段": sorted(stats)},
        )
        return None

    await settings_service.set_value(MARKER_KEY, version)
    log_event(
        logger,
        "小种子已导入",
        tag="成功",
        detail={
            "版本": version,
            "数据截至": meta.get("source_data_as_of"),
            "目录新增": stats.get("games", 0),
            "人工列并集": stats.get("curated", 0),
            "捆绑包主档新增": stats.get("bundles", 0),
        },
    )
    return stats
