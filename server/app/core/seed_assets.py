"""资产种子包（assets/seed/holdexar_seed.db）：随包分发的公共数据切片。

种子 = 发布者集中维护的公共数据快照（汇率档案 + games 人工策划列 +
game_price_history 价格历史切片），结构上不含任何凭据/用户数据。
链路：生产库 → scripts/export_seed.py → 种子文件随包 → 启动时按数据
性质走两条并入通道；爬虫入库新游戏时 apply_curated() 补挂人工列
（否则"先爬到游戏、后装种子"标记永远贴不上）。

三条并入通道（marker 互相独立，各自按数据性质选择语义）：
- **一次性导入**（import_seed，汇率档案）：只在「全新安装」（games 表为空）
  时导入一次。老用户升级 = 换程序目录、data/ 原地保留——库里有数据
  （games>0）则写入跳过 marker 后永久不再导入。marker 形态：首次成功导入
  存 seed 本身 version；跳过存 "skipped:<version>"。
- **人工列名单合并**（merge_curated_seed，独立 marker）：XGP / Epic / HB /
  系列名单是种子唯一权威（爬虫 upsert 白名单不含，本地无编辑 UI），按种子
  版本对**所有用户**生效：本地已有行覆写人工列；缺行直接落完整 games 行
  （appid + name + 人工列，updated_at 记种子版本）——名单开箱即查，且行带
  非空 updated_at 永远不会被孤儿补抓层捡去爬（监控范围只由愿望单驱动，
  名单 ≠ 监控）。
- **价格历史合并**（merge_history_seed，独立 marker）：纯追加型快照数据，
  老用户库也并入——本地写入（用户自己的爬虫时间线）与种子时间线的键空间
  天然错开，合并 = 补充发布者时间线 + 同键行以种子覆盖价格列。

种子内四段数据语义：
- fx_rate_history 按 (currency_code, fetched_at原文) 去重追加
- fx_rates 快照 INSERT OR REPLACE（种子带最新快照）
- games_curated 人工列覆写 + 缺行落行（详见 merge_curated_seed）
- game_price_history 按逻辑键 (appid, region_code, sub_id, is_gold,
  snapshot_at原文) 同键覆盖价格列、缺键插入（详见 merge_history_seed）

merge_seed_incremental 是后两条通道的统一入口（lifespan 调用）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.config import get_settings
from app.core.database import get_session_factory
from app.domains.games.models import Game
from app.domains.settings.models import AppSetting

logger = logging.getLogger(__name__)

SEED_FILENAME = "holdexar_seed.db"
MARKER_KEY = "seed.imported_version"
# 一次性导入语义的 marker 语义值：库已有数据（games>0）时记录跳过的种子版本
SKIP_PREFIX = "skipped:"

# games 人工策划列（与 export_seed.py 白名单一致；爬虫 upsert 不含这些列）
CURATED_COLS = ("xgp_tier", "epic_date", "is_epic", "is_hb", "hb_data", "series_id")
# 名单行身份列（与 export_seed.py 的 CURATED_IDENTITY_COL 同名同义）：
# 覆写人工列时不碰 name，INSERT 缺行时才用它
CURATED_IDENTITY_COL = "name"

# 价格历史合并 marker：与一次性导入的 MARKER_KEY 互相独立——老用户库即使
# 被一次性导入永久跳过，价格历史也按种子版本照常并入
HISTORY_MARKER_KEY = "seed.history_imported_version"

# 人工列名单合并 marker：名单按种子版本对全体用户生效（详见模块 docstring）
CURATED_MARKER_KEY = "seed.curated_imported_version"

# game_price_history 种子列（与 export_seed.py 的 GPH_COLS 同序同集，不含自增 id）
GPH_COLS = (
    "appid", "region_code", "currency", "price", "original_price",
    "discount_percent", "sub_id", "is_gold", "version_suffix", "is_bundle",
    "price_status", "cny_fen", "snapshot_at",
)
# 合并时「覆盖对应列」的覆盖范围：价格取值列。键列（appid/region_code/
# snapshot_at/sub_id/is_gold）定义行的身份，形态列（version_suffix/is_bundle）
# 是 sub 的描述属性——都不在覆盖之列
GPH_OVERWRITE_COLS = (
    "currency", "price", "original_price", "discount_percent", "cny_fen",
    "price_status",
)


def seed_db_path() -> Path:
    return get_settings().seed_dir / SEED_FILENAME


def read_seed_meta(path: Path) -> dict:
    """读 seed_meta KV（文件缺失/损坏返回空 dict，调用方按无种子处理）。"""
    if not path.is_file():
        return {}
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute("SELECT key, value FROM seed_meta").fetchall()
        finally:
            con.close()
        return {k: v for k, v in rows}
    except Exception as e:  # noqa: BLE001 —— 坏种子等同无种子
        logger.warning("种子文件读取失败（忽略）：%s —— %s", path, e)
        return {}


# ── games 人工策划列索引（进程内缓存，爬虫热路径只做 dict 查找）──────────

_CURATED_CACHE: dict[str, tuple[float, dict[int, dict]]] = {}


def reset_cache() -> None:
    """清空人工列索引缓存（测试 / 种子重导后强制刷新用）。"""
    _CURATED_CACHE.clear()


def _load_curated_index(path: Path) -> dict[int, dict]:
    """种子 games_curated → {appid: {col: value}}；按 mtime 缓存。"""
    if not path.is_file():
        return {}
    mtime = path.stat().st_mtime
    cached = _CURATED_CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    index: dict[int, dict] = {}
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            cols = ", ".join(CURATED_COLS)
            for row in con.execute(f"SELECT appid, {cols} FROM games_curated"):
                index[int(row[0])] = dict(zip(CURATED_COLS, row[1:]))
        finally:
            con.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("种子人工列索引加载失败（忽略）：%s", e)
        return {}
    _CURATED_CACHE[str(path)] = (mtime, index)
    return index


async def apply_curated(appid: int, seed_path: Path | None = None) -> None:
    """爬虫入库联动：appid 在种子人工列索引内则整行覆写（本地无此数据编辑通道）。

    索引为空（未随种子/发布者未回填）时零开销直接返回；任何异常吞掉，
    绝不影响爬虫写库主链路。
    """
    try:
        index = _load_curated_index(seed_path or seed_db_path())
        row = index.get(int(appid))
        if not row:
            return
        sets = ", ".join(f"{c} = :{c}" for c in CURATED_COLS)
        async with get_session_factory()() as session:
            await session.execute(
                text(f"UPDATE games SET {sets} WHERE appid = :appid"),
                {**row, "appid": int(appid)},
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001 —— 补挂失败不影响爬虫主链路
        logger.warning("种子人工列补挂失败 appid=%s：%s", appid, e)


# ── 冷启动导入 ────────────────────────────────────────────────────────


async def _local_games_count() -> int:
    """库内 games 行数——一次性导入判定输入（>0 = 已有数据的用户库）。

    独立成模块级函数：测试对真实开发库跑（合成 appid 隔离），须可 stub。
    """
    from sqlalchemy import text

    from app.core.database import get_session_factory

    async with get_session_factory()() as session:
        return (await session.execute(text("SELECT COUNT(*) FROM games"))).scalar_one()


def _key(cur, fetched_at) -> tuple[str, str]:
    """fx_rate_history 去重键：币种 + fetched_at 原文（对齐 fx_maintenance 币种+日期语义）。"""
    return (str(cur), "" if fetched_at is None else str(fetched_at))


async def import_seed(seed_path: Path | None = None) -> dict | None:
    """种子并入本地库（仅汇率档案）。返回统计 dict；跳过/无种子/已导过返回 None。

    一次性导入语义：库中已有数据（games 行数 > 0）→ 写跳过 marker 后
    永久不再导入（升级用户的库永远原样保留）。games 为空（全新安装）→
    首次导入，marker 记下种子 version。人工列名单不在本通道——它按种子
    版本对全体用户生效，见 merge_curated_seed。
    调用方（lifespan）负责 try/except 不阻塞启动；测试直接调用以拿到真实错误。
    """
    path = seed_path or seed_db_path()
    meta = read_seed_meta(path)
    version = str(meta.get("version", "")) if meta else ""
    if not path.is_file() or not version:
        return None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(MARKER_KEY)
    if marker == version or (isinstance(marker, str) and marker.startswith(SKIP_PREFIX)):
        # 已导过同版种子 / 已判定跳过：零开销直过
        return None

    # 一次性导入判定：库里有游戏数据 = 老用户库，跳过导入（含未来更新的种子）
    if await _local_games_count() > 0:
        await settings_service.set_value(MARKER_KEY, f"{SKIP_PREFIX}{version}")
        logger.info("[种子] 库已有数据，跳过随包种子导入（version=%s）", version)
        return None

    # 种子侧原文读出（sqlite3 直读无类型转换，保住 fetched_at 原格式）
    src = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        fx_rates = src.execute(
            "SELECT currency_code, rate_to_cny, fetched_at FROM fx_rates"
        ).fetchall()
        history = src.execute(
            "SELECT currency_code, rate_to_cny, source, fetched_at FROM fx_rate_history"
        ).fetchall()
    finally:
        src.close()

    async with get_session_factory()() as session:
        # 汇率历史：现有键集合读原文 → Python 侧差集 → 批量 INSERT（幂等）。
        # 不走 SQL 侧 NOT EXISTS：fx_rate_history 无 (currency, fetched_at) 索引，
        # 23 万行 × 逐行子查询会退化为 O(n²)。
        existing: set[tuple[str, str]] = set()
        for cur, fa in await session.execute(
            text("SELECT currency_code, fetched_at FROM fx_rate_history")
        ):
            existing.add(_key(cur, fa))
        new_rows = [
            (cur, rate, source, fa)
            for cur, rate, source, fa in history
            if _key(cur, fa) not in existing
        ]
        if new_rows:
            await session.execute(
                text(
                    "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
                    "VALUES (:c, :r, :s, :f)"
                ),
                [
                    {"c": cur, "r": rate, "s": source, "f": fa}
                    for cur, rate, source, fa in new_rows
                ],
            )

        # 汇率快照 upsert（原文 fetched_at 直存，绕开 DateTime 类型对象化）
        if fx_rates:
            await session.execute(
                text(
                    "INSERT OR REPLACE INTO fx_rates (currency_code, rate_to_cny, fetched_at) "
                    "VALUES (:c, :r, :f)"
                ),
                [{"c": c, "r": r, "f": f} for c, r, f in fx_rates],
            )

        await session.execute(
            sqlite_insert(AppSetting)
            .values(key=MARKER_KEY, value_json=version, updated_at=None)
            .on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value_json": sqlite_insert(AppSetting).excluded.value_json},
            )
        )
        await session.commit()

    summary = {
        "version": version,
        "fx_rates": len(fx_rates),
        "fx_history_new": len(new_rows),
    }
    logger.info(
        "[种子] 首次安装导入完成 v%s：汇率快照 %s 条 / 历史新增 %s 行",
        version, summary["fx_rates"], summary["fx_history_new"],
    )
    return summary


# ── 价格历史合并（独立通道：老用户库也并入）─────────────────────────────


def _merge_history_sync(db_file: Path, seed_file: Path, version: str) -> dict:
    """原生 sqlite3 执行合并（同步函数，调用方放线程池）。返回行数统计。

    走 ATTACH + 集合式 SQL 而非 ORM 逐行：两三百万行规模下 ORM 对象化开销
    不可接受；单事务 + synchronous=OFF 一次性写完，marker 与数据同事务落库
    （中断即整体回滚，下次启动重来）。种子以 URI 只读挂载，不支持 URI 的
    老构建退化为直挂——种子文件由本项目构建，只读是卫生要求而非安全边界。
    """
    con = sqlite3.connect(str(db_file), timeout=60.0, isolation_level=None)
    try:
        con.execute("PRAGMA busy_timeout=60000")
        con.execute("PRAGMA synchronous=OFF")
        con.execute("PRAGMA cache_size=-65536")
        try:
            con.execute(
                "ATTACH DATABASE ? AS seed", (f"file:{seed_file.as_posix()}?mode=ro",)
            )
        except sqlite3.OperationalError:
            con.execute("ATTACH DATABASE ? AS seed", (str(seed_file),))
        has_table = con.execute(
            "SELECT 1 FROM seed.sqlite_master "
            "WHERE type='table' AND name='game_price_history'"
        ).fetchone()
        if not has_table:
            return {}
        cols = ", ".join(GPH_COLS)
        seed_cols = ", ".join(f"s.{c}" for c in GPH_COLS)
        set_cols = ", ".join(f"{c} = s.{c}" for c in GPH_OVERWRITE_COLS)
        # 逻辑键与写入侧 OR REPLACE / ux_gph_snapshot 唯一索引同口径
        # （NULL 归一常量一致：sub_id→-1、is_gold→0）
        key_match = (
            "l.appid = s.appid AND l.region_code = s.region_code "
            "AND l.snapshot_at = s.snapshot_at "
            "AND IFNULL(l.sub_id, -1) = IFNULL(s.sub_id, -1) "
            "AND IFNULL(l.is_gold, 0) = IFNULL(s.is_gold, 0)"
        )
        con.execute("BEGIN IMMEDIATE")
        # 同键行覆盖价格列；IS NOT 哨兵让值完全一致的行不重写（重复启动零写放大）
        overwritten = con.execute(
            f"UPDATE main.game_price_history AS l SET {set_cols} "
            f"FROM seed.game_price_history AS s "
            f"WHERE {key_match} AND ("
            + " OR ".join(f"l.{c} IS NOT s.{c}" for c in GPH_OVERWRITE_COLS)
            + ")"
        ).rowcount
        # 缺键行插入。种子侧每逻辑键唯一（导出已去重），本地侧同键行已被上一步
        # 覆盖，走到这里的必是真缺行——不会撞 ux_gph_snapshot 唯一索引
        inserted = con.execute(
            f"INSERT INTO main.game_price_history ({cols}) "
            f"SELECT {seed_cols} FROM seed.game_price_history AS s "
            f"WHERE NOT EXISTS (SELECT 1 FROM main.game_price_history l WHERE {key_match})"
        ).rowcount
        con.execute(
            # value_json 列是 SQLAlchemy JSON 类型（读写自动编码），原生 SQL
            # 写入必须自己 json.dumps，否则 get_value 侧 json.loads 直接炸
            "INSERT INTO main.app_settings (key, value_json) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (HISTORY_MARKER_KEY, json.dumps(version)),
        )
        con.commit()
        return {"overwritten": overwritten, "inserted": inserted}
    finally:
        con.close()


async def merge_history_seed(seed_path: Path | None = None) -> dict | None:
    """价格历史种子并入本地库。返回统计 dict；无种子 / 已合并 / 无可并数据返回 None。

    独立于一次性导入：判定只看自己的 marker（按种子版本），不做 games 行数
    判定——老用户库是本通道的主要服务对象。调用方（lifespan）负责
    try/except 不阻塞启动；同步实现在线程池跑，不卡事件循环。
    """
    path = seed_path or seed_db_path()
    meta = read_seed_meta(path)
    version = str(meta.get("version", "")) if meta else ""
    if not path.is_file() or not version:
        return None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(HISTORY_MARKER_KEY)
    if marker == version:
        return None

    from app.core.database import sqlite_file_path

    db_file = sqlite_file_path()
    if db_file is None:  # 内存库 / 非文件库：无从合并（测试直接调 _merge_history_sync）
        return None

    stats = await asyncio.to_thread(_merge_history_sync, db_file, path, version)
    if not stats:
        # 种子不含价格历史表（schema 1 旧种子）：静默，待下个 schema 2+ 种子再并
        return None
    logger.info(
        "[种子] 价格历史合并完成 v%s：覆盖 %s 行 / 新增 %s 行",
        version, stats["overwritten"], stats["inserted"],
    )
    return stats


# ── 人工列名单合并（独立通道：老用户库也生效）─────────────────────────────


async def _seed_curated_rows(path: Path) -> dict[int, dict] | None:
    """种子 games_curated → {appid: {name + 人工列}}。

    返回 None = 种子没有该表或表结构过旧（schema 2 及以前的种子无 name 列，
    名单行落不成完整 games 行，视同无名单）。坏种子等同无名单。
    """
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            has = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='games_curated'"
            ).fetchone()
            if not has:
                return None
            cols = ", ".join((CURATED_IDENTITY_COL, *CURATED_COLS))
            rows = con.execute(
                f"SELECT appid, {cols} FROM games_curated"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None
    except Exception as e:  # noqa: BLE001 —— 坏种子等同无名单
        logger.warning("种子人工列名单读取失败（忽略）：%s —— %s", path, e)
        return None
    out: dict[int, dict] = {}
    for row in rows:
        out[int(row[0])] = dict(zip((CURATED_IDENTITY_COL, *CURATED_COLS), row[1:]))
    return out


async def merge_curated_seed(seed_path: Path | None = None) -> dict | None:
    """人工列名单并入本地库（按种子版本，全体用户）。返回统计；无名单/已并返回 None。

    - 本地已有 games 行 → 覆写人工列。种子是该数据的唯一权威（爬虫 upsert
      白名单不含这些列，本地无编辑 UI）；name 不覆写（本地爬来的行名更准）。
    - 本地缺行 → 直接落完整 games 行：name 取种子，updated_at 记种子版本。
      **防监控的关键**：孤儿补抓层只捡 `updated_at IS NULL` 且无价格行的行，
      名单行带非空 updated_at 永远不会被捡去爬；监控范围本身只由愿望单驱动，
      名单 ≠ 监控。用户日后主动愿望单/入库，爬虫 upsert 自然接管全字段。
    幂等由 marker 保证；数据操作本身也幂等，marker 写失败下次启动重做无副作用。
    """
    path = seed_path or seed_db_path()
    meta = read_seed_meta(path)
    version = str(meta.get("version", "")) if meta else ""
    if not path.is_file() or not version:
        return None

    curated = await _seed_curated_rows(path)
    if not curated:
        return None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(CURATED_MARKER_KEY)
    if marker == version:
        return None

    try:
        stamped = datetime.fromisoformat(version)
    except ValueError:
        stamped = datetime.now()

    async with get_session_factory()() as session:
        present = {
            int(a)
            for (a,) in await session.execute(
                select(Game.appid).where(Game.appid.in_(list(curated)))
            )
        }
        updated = 0
        if present:
            sets = ", ".join(f"{c} = :{c}" for c in CURATED_COLS)
            await session.execute(
                text(f"UPDATE games SET {sets} WHERE appid = :appid"),
                [
                    {**{c: row[c] for c in CURATED_COLS}, "appid": appid}
                    for appid, row in curated.items()
                    if appid in present
                ],
            )
            updated = len(present)

        inserted = 0
        missing = [a for a in curated if a not in present]
        if missing:
            values = [
                {"appid": appid, **curated[appid], "updated_at": stamped}
                for appid in missing
            ]
            await session.execute(sqlite_insert(Game).values(values))
            inserted = len(missing)

        await session.commit()

    await settings_service.set_value(CURATED_MARKER_KEY, version)
    logger.info(
        "[种子] 人工列名单合并完成 v%s：覆写 %s 款 / 落名单行 %s 款",
        version, updated, inserted,
    )
    return {"curated_updated": updated, "curated_inserted": inserted}


async def merge_seed_incremental(seed_path: Path | None = None) -> dict | None:
    """按种子版本对全体用户生效的增量通道统一入口：人工列名单 + 价格历史。

    两条子通道各自有 marker、各自幂等、互不拖累（任一失败只记日志，
    另一条照常执行，失败方下次启动自动重试）。两者都无事发生才返回 None。
    """
    curated = None
    history = None
    try:
        curated = await merge_curated_seed(seed_path)
    except Exception:  # noqa: BLE001
        logger.exception("人工列名单种子合并失败（不阻塞启动）")
    try:
        history = await merge_history_seed(seed_path)
    except Exception:  # noqa: BLE001
        logger.exception("价格历史种子合并失败（不阻塞启动）")
    if curated is None and history is None:
        return None
    return {"curated": curated, "history": history}
