"""资产种子包（holdexar_seed.db）：公共数据切片（games 人工列 + 预设池清单 +
目录/现价 + 玩家标签 + 价格历史 + 捆绑包），不含凭据，不含汇率——汇率由 rates
域经免 Key Provider 现抓回填（rates.history）。六条独立 marker 通道：
merge_curated（人工列名单=种子唯一权威，全员覆写人工列、缺行落完整行）；
merge_preset（预设池，缺行落行、已有不动）；merge_current（目录缺行落行 +
现价按 appid 粒度整包——本地有任一观测即跳过，种子不回拨本地新价）；
merge_game_tags（玩家标签按 appid 粒度整包——本地该款已有标签即跳过）；
merge_history（价格历史按六段逻辑键补缺行 + 同键版本名补空，INSERT only
观测事实不回拨）；merge_bundles（捆绑包主档缺行落行 + 区价按包粒度让位本地
观测）。merge_seed_incremental 为统一入口（lifespan 调用）；爬虫入库新游戏时
apply_curated() 补挂人工列。
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
from app.core.database import WritePriority, get_session_factory, write_gate
from app.core.logging import log_event
from app.domains.games.models import Game

logger = logging.getLogger(__name__)

SEED_FILENAME = "holdexar_seed.db"

# games 人工策划列（与 export_seed.py 白名单一致；爬虫 upsert 不含这些列）
CURATED_COLS = ("xgp_tier", "epic_date", "is_epic", "is_hb", "hb_data", "series_id")
# 名单行身份列（与 export_seed.py 的 CURATED_IDENTITY_COL 同名同义）：
# 覆写人工列时不碰 name，INSERT 缺行时才用它
CURATED_IDENTITY_COL = "name"

# 人工列名单合并 marker：名单按种子版本对全体用户生效（详见模块 docstring）
CURATED_MARKER_KEY = "seed.curated_imported_version"

# 预设游戏池合并 marker：预设按种子版本对全体用户生效（详见模块 docstring）
PRESET_MARKER_KEY = "seed.preset_imported_version"

# 现价快照合并 marker：现价快照按种子版本对全体用户生效（详见模块 docstring）
CURRENT_MARKER_KEY = "seed.current_imported_version"

# 玩家标签合并 marker：标签按种子版本对全体用户生效（详见模块 docstring）
GAME_TAGS_MARKER_KEY = "seed.game_tags_imported_version"

# 价格历史合并 marker：观测事实按种子版本补缺（详见模块 docstring）
HISTORY_MARKER_KEY = "seed.history_imported_version"

# 捆绑包数据合并 marker（主档 + 区价一个事务一个 marker）
BUNDLES_MARKER_KEY = "seed.bundles_imported_version"

# game_price_history 种子列（与 export_seed.py 的 GPH_COLS 同序同集，不含自增
# id；不带 attach_browse_extras 的促销富化列）
GPH_COLS = (
    "appid", "region_code", "currency", "price", "original_price",
    "discount_percent", "sub_id", "is_gold", "version_suffix", "is_bundle",
    "price_status", "cny_fen", "snapshot_at", "discount_end_ts",
    "steam_event_key",
)
# 六段逻辑键与源库/种子侧 ux_gph_snapshot 唯一索引同口径（price 在键内：
# 同刻不同价是不同观测行，INSERT only 语义下不互相覆盖）
_GPH_KEY_MATCH = (
    "l.appid = s.appid AND l.region_code = s.region_code "
    "AND l.snapshot_at = s.snapshot_at "
    "AND IFNULL(l.sub_id, -1) = IFNULL(s.sub_id, -1) "
    "AND IFNULL(l.price, 0) = IFNULL(s.price, 0) "
    "AND IFNULL(l.is_gold, 0) = IFNULL(s.is_gold, 0)"
)
# 同键行可差异的派生列（价格在键内永相同，不列）
GPH_OVERWRITE_COLS = (
    "currency", "original_price", "discount_percent", "cny_fen", "price_status",
)
# 捆绑包主档种子列（与 export_seed.py 的 BUNDLE_COLS 同序同集，不含自增 id）
BUNDLE_COLS = (
    "bundle_id", "name", "must_purchase_as_set", "item_kind", "header_image",
    "is_lowest", "min_cny_fen", "diff_fen", "smart_score", "url", "app_ids",
    "view_count", "updated_at",
)
# 捆绑包区域价格种子列（与 export_seed.py 的 BRP_COLS 同序同集）
BRP_COLS = (
    "bundle_id", "region_code", "currency", "price", "original_price",
    "discount_percent", "bundle_base_discount", "price_status", "cny_fen",
    "discount_end_ts", "app_ids", "crawled_at",
)

# game_current_prices 种子列（与 export_seed.py 的 GCP_COLS 同序同集）；
# 列全集显式带出，合并侧直插不依赖库内默认值
GCP_COLS = (
    "appid", "region_code", "currency", "price", "original_price",
    "discount_percent", "sub_id", "price_status", "fail_count", "cny_fen",
    "discount_end_ts", "updated_at",
)
# games 主档种子列（现价快照通道自带目录行；与 export_seed.py 的 GC_COLS
# 同序同集）。排序缓存列由启动链标记三连重算，人工列走 games_curated 通道
GC_COLS = (
    "appid", "name", "name_en", "type", "header_image", "family_sharing",
    "trading_cards", "is_adult", "is_visual_novel", "release_date",
    "positive_rate", "positive_reviews", "review_count", "view_count",
    "removed_at", "free_kind",
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
        log_event(
            logger,
            "种子文件读取失败，按无种子处理",
            tag="忽略",
            level=logging.WARNING,
            detail={"路径": str(path), "原因": str(e)},
        )
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
        log_event(
            logger,
            "种子人工列索引加载失败，按无索引处理",
            tag="忽略",
            level=logging.WARNING,
            detail={"原因": str(e)},
        )
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
        from app.core.database import WritePriority, write_gate

        async with write_gate(
            WritePriority.BACKGROUND, label="seed_curated_apply"
        ), get_session_factory()() as session:
            await session.execute(
                text(f"UPDATE games SET {sets} WHERE appid = :appid"),
                {**row, "appid": int(appid)},
            )
            await session.commit()
    except Exception as e:  # noqa: BLE001 —— 补挂失败不影响爬虫主链路
        log_event(
            logger,
            "种子人工列补挂失败，不影响爬虫写库",
            level=logging.WARNING,
            detail={"AppID": appid, "原因": str(e)},
        )


async def apply_curated_batch(appids: list[int], seed_path: Path | None = None) -> int:
    """批量人工列补挂（收尾链聚合形态）：一次闸分块 executemany 覆盖整批。

    索引外的款零触碰。返回实际补挂款数；任何异常吞掉，
    不影响爬虫写库主链路。
    """
    ids = [int(a) for a in appids if a]
    if not ids:
        return 0
    try:
        index = _load_curated_index(seed_path or seed_db_path())
        hit = [a for a in dict.fromkeys(ids) if a in index]
        if not hit:
            return 0
        sets = ", ".join(f"{c} = :{c}" for c in CURATED_COLS)
        from app.core.database import WritePriority, write_gate

        async with write_gate(
            WritePriority.BACKGROUND, label="seed_curated_apply"
        ), get_session_factory()() as session:
            # 分块受 SQLite 变量上限约束（与目录/名单通道同型）
            for i in range(0, len(hit), 500):
                await session.execute(
                    text(f"UPDATE games SET {sets} WHERE appid = :appid"),
                    [{**index[a], "appid": a} for a in hit[i : i + 500]],
                )
            await session.commit()
        return len(hit)
    except Exception as e:  # noqa: BLE001 —— 补挂失败不影响爬虫主链路
        log_event(
            logger,
            "种子人工列批量补挂失败，不影响爬虫写库",
            level=logging.WARNING,
            detail={"款数": len(ids), "原因": str(e)},
        )
        return 0


# ── 现价快照合并（独立通道：老用户库也生效）─────────────────────────────


def _merge_current_prices_sync(db_file: Path, seed_file: Path, version: str) -> int:
    """原生 sqlite3 现价快照并入（同步函数，调用方放线程池）。

    ATTACH + 集合式 SQL（几十万行 ORM 对象化开销不可接受）；appid 粒度
    让位本地观测——本地没有该 appid 任何现价行才整包插入种子行，本地爬过
    的游戏一个区都不覆盖（现价是「最近一次观测」的派生态，种子快照不得
    回拨本地更新价）。GCP_COLS 是显式全集：本地库（含全新 create_all 建出
    的 NOT NULL 无默认列）不依赖列默认值即可直插。
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
        has_gcp = con.execute(
            "SELECT 1 FROM seed.sqlite_master "
            "WHERE type='table' AND name='game_current_prices'"
        ).fetchone()
        if not has_gcp:
            return -1
        gcp_cols = ", ".join(GCP_COLS)
        gcp_seed_cols = ", ".join(f"s.{c}" for c in GCP_COLS)
        con.execute("BEGIN IMMEDIATE")
        # 种子是导入基线而非抓取尝试：按快照状态派生观察章（ok/locked =
        # 成功观察并带 last_success_at；其余 = 失败态基线），旧格式种子文件
        # 无新列也照常并入
        inserted = con.execute(
            f"INSERT INTO main.game_current_prices "
            f"({gcp_cols}, attempt_outcome, steam_answer, last_success_at) "
            f"SELECT {gcp_seed_cols}, "
            "CASE WHEN s.price_status IN ('ok', 'locked') THEN 'success' "
            "ELSE 'failed' END, "
            "CASE s.price_status WHEN 'ok' THEN 'ok' "
            "WHEN 'locked' THEN 'locked' ELSE NULL END, "
            "CASE WHEN s.price_status IN ('ok', 'locked') THEN s.updated_at "
            "ELSE NULL END "
            "FROM seed.game_current_prices AS s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM main.game_current_prices l WHERE l.appid = s.appid)"
        ).rowcount
        con.commit()
        return inserted
    finally:
        con.close()


async def _seed_catalog_rows(path: Path) -> list[dict] | None:
    """种子 games_catalog → 行字典列表；无该表（schema 4 及以前）返回 None。

    主档落行走 ORM（与人工列名单通道同型）：现役模型的 Mapped[bool]/int
    列在全新 create_all 建出的库上是 NOT NULL 且无库内默认值，Python 端
    默认值（hl_flag=0 等）必须由 ORM 补齐，裸 SQL 列名直插会撞约束。
    """
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            has = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='games_catalog'"
            ).fetchone()
            if not has:
                return None
            cols = ", ".join(GC_COLS)
            rows = con.execute(f"SELECT {cols} FROM games_catalog").fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None
    except Exception as e:  # noqa: BLE001 —— 坏种子等同无快照
        log_event(
            logger,
            "种子现价目录读取失败，按无快照处理",
            tag="忽略",
            level=logging.WARNING,
            detail={"原因": str(e)},
        )
        return None
    return [dict(zip(GC_COLS, row)) for row in rows]


async def merge_current_seed(seed_path: Path | None = None) -> dict | None:
    """现价快照种子并入本地库。返回统计 dict；无种子 / 已合并 / 无可并数据返回 None。

    独立于其他通道：判定只看自己的 marker（按种子版本），不做 games 行数
    判定——老用户库是本通道的主要服务对象（补齐本地从未爬过的游戏）。
    games 主档只补缺行（updated_at 记种子版本，本地行一个字段都不动）；
    现价按 appid 粒度并入（本地爬过的游戏不回拨）。新用户开箱即有全量
    目录与现价，找游戏页首屏就有数据。启动链序上先于标记三连：并完现价
    即被排序缓存重建覆盖。
    调用方（lifespan）负责 try/except 不阻塞启动。
    """
    path = seed_path or seed_db_path()
    meta = read_seed_meta(path)
    version = str(meta.get("version", "")) if meta else ""
    if not path.is_file() or not version:
        return None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(CURRENT_MARKER_KEY)
    if marker == version:
        return None

    try:
        stamped = datetime.fromisoformat(version)
    except ValueError:
        stamped = datetime.now()

    catalog = await _seed_catalog_rows(path)
    if catalog is None and not path.is_file():
        return None

    from app.core.database import sqlite_file_path

    db_file = sqlite_file_path()
    if db_file is None:  # 内存库 / 非文件库：无从合并
        return None

    # ── games 主档缺行落行（ORM，吃 Python 端列默认值）──
    games_inserted = 0
    if catalog:
        from sqlalchemy import select as _select

        async with write_gate(
            WritePriority.BACKGROUND, label="seed_current_merge"
        ), get_session_factory()() as session:
            present = {
                int(a)
                for (a,) in await session.execute(
                    _select(Game.appid).where(
                        Game.appid.in_([int(row["appid"]) for row in catalog])
                    )
                )
            }
            missing = [row for row in catalog if int(row["appid"]) not in present]
            if missing:
                # 分块受 SQLite 变量上限约束：每行 ~40 列，500 行 ≈ 2 万变量
                for i in range(0, len(missing), 500):
                    chunk = [
                        {**row, "updated_at": stamped} for row in missing[i:i + 500]
                    ]
                    await session.execute(sqlite_insert(Game).values(chunk))
                games_inserted = len(missing)
            await session.commit()

    # ── 现价快照并入（ATTACH 集合式）+ marker 同事务 ──
    # 线程池内的原生 sqlite 写事务也在写调度器管辖内：闸在事件循环侧持住
    # （与价格历史合并同理由），跑在线程池里的原生连接不再是无主写者。
    async with write_gate(WritePriority.BACKGROUND, label="seed_current_merge"):
        prices_inserted = await asyncio.to_thread(
            _merge_current_prices_sync, db_file, path, version
        )
    if prices_inserted < 0:
        # 种子不含现价快照表（schema 4 及以前）：静默，待下个 schema 5+ 种子再并
        return None

    from app.domains.settings import service as settings_service

    await settings_service.set_value(CURRENT_MARKER_KEY, version)
    log_event(
        logger,
        "现价快照种子已合并",
        tag="成功",
        detail={"种子版本": version, "目录行": games_inserted, "现价行": prices_inserted},
    )
    return {"games": games_inserted, "prices": prices_inserted}


# ── 玩家标签合并（独立通道：老用户库也生效）─────────────────────────────


def _merge_game_tags_sync(db_file: Path, seed_file: Path) -> int:
    """原生 sqlite3 玩家标签并入（同步函数，调用方放线程池）。

    ATTACH + 集合式 SQL；**appid 粒度**让位本地——本地该款已有任何标签行就整款
    跳过。标签集合是「最近一次抓取」的派生态，逐行 OR IGNORE 会与本地拼出
    「两地都没抓到过」的并集（Steam 摘掉的标签被种子复活）。
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
        has_tags = con.execute(
            "SELECT 1 FROM seed.sqlite_master "
            "WHERE type='table' AND name='game_tags'"
        ).fetchone()
        if not has_tags:
            return -1
        con.execute("BEGIN IMMEDIATE")
        inserted = con.execute(
            "INSERT INTO main.game_tags (appid, tagid, weight) "
            "SELECT s.appid, s.tagid, s.weight FROM seed.game_tags AS s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM main.game_tags l WHERE l.appid = s.appid)"
        ).rowcount
        con.commit()
        return inserted
    finally:
        con.close()


async def merge_game_tags_seed(seed_path: Path | None = None) -> dict | None:
    """玩家标签种子并入本地库。返回统计 dict；无种子 / 已合并 / 无可并数据返回 None。

    独立于其他通道：判定只看自己的 marker（按种子版本）。本地爬过的游戏整款
    让位（本地标签集合权威），本地从未爬到的游戏由种子补齐——新用户开箱即有
    标签，不必等第一轮价格爬取。调用方（lifespan）负责 try/except 不阻塞启动。
    """
    path = seed_path or seed_db_path()
    meta = read_seed_meta(path)
    version = str(meta.get("version", "")) if meta else ""
    if not path.is_file() or not version:
        return None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(GAME_TAGS_MARKER_KEY)
    if marker == version:
        return None

    from app.core.database import sqlite_file_path

    db_file = sqlite_file_path()
    if db_file is None:  # 内存库 / 非文件库：无从合并
        return None

    async with write_gate(WritePriority.BACKGROUND, label="seed_game_tags_merge"):
        inserted = await asyncio.to_thread(_merge_game_tags_sync, db_file, path)
    if inserted < 0:
        # 种子不含标签表（schema 6 及以前）：静默，待下个 schema 7+ 种子再并
        return None

    await settings_service.set_value(GAME_TAGS_MARKER_KEY, version)
    log_event(
        logger,
        "玩家标签种子已合并",
        tag="成功",
        detail={"种子版本": version, "标签行": inserted},
    )
    return {"tags": inserted}


# ── 价格历史合并（独立通道：老用户库也生效）─────────────────────────────

# 分块尺寸（种子侧 appid 数）：1746 万行 / 1.3 万款 ≈ 1300 行/款，200 款/块
# ≈ 26 万行，单块事务秒级提交——写者位在块间交还，不再出现分钟级持闸
_HISTORY_CHUNK_APPS = 200
# 断点 KV：{"version":…, "last_appid":…} 与块数据同事务落位，中断下次续跑
HISTORY_PROGRESS_KEY = "seed.history_progress"


def _merge_history_chunk(db_file: Path, seed_file: Path, version: str) -> dict | None:
    """合并下一个 appid 分块（同步函数，调用方放线程池；每调用一事务一闸）。

    返回 {"overwritten", "inserted", "done"}；种子无历史表返回 None（旧格式
    种子静默跳过）。done=True 表示无剩余分块，本次调用已落最终 marker 并
    清除断点。断点 KV（HISTORY_PROGRESS_KEY，记 {"version", "last_appid"}）
    与块数据同事务落位——中断只回滚当前块，下次启动从断点续跑。历史是
    INSERT only 观测事实：六段逻辑键（含 COALESCE(price,0)，与两侧
    ux_gph_snapshot 唯一索引同口径）缺键补插；同键行只做派生列差异覆写
    与版本名补空，价格永不回拨。
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
            return None

        # 断点：同版本续跑（last_appid 之后）；版本变了或无断点 → 从头
        last_appid = -1
        progress_row = con.execute(
            "SELECT value_json FROM main.app_settings WHERE key = ?",
            (HISTORY_PROGRESS_KEY,),
        ).fetchone()
        if progress_row:
            try:
                progress = json.loads(progress_row[0])
            except (TypeError, ValueError):
                progress = None
            if isinstance(progress, dict) and progress.get("version") == version:
                try:
                    last_appid = int(progress.get("last_appid", -1))
                except (TypeError, ValueError):
                    last_appid = -1

        chunk = [
            r[0]
            for r in con.execute(
                "SELECT DISTINCT appid FROM seed.game_price_history "
                "WHERE appid > ? ORDER BY appid LIMIT ?",
                (last_appid, _HISTORY_CHUNK_APPS),
            )
        ]
        if not chunk:
            # 全部块完成：落最终 marker + 清断点（同事务）
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "INSERT INTO main.app_settings (key, value_json) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
                (HISTORY_MARKER_KEY, json.dumps(version)),
            )
            con.execute(
                "DELETE FROM main.app_settings WHERE key = ?",
                (HISTORY_PROGRESS_KEY,),
            )
            con.commit()
            return {"overwritten": 0, "inserted": 0, "done": True}

        lo, hi = chunk[0], chunk[-1]
        cols = ", ".join(GPH_COLS)
        seed_cols = ", ".join(f"s.{c}" for c in GPH_COLS)
        set_cols = ", ".join(f"{c} = s.{c}" for c in GPH_OVERWRITE_COLS)
        # 版本名只补空不覆写：本地行的 version_suffix 可能因爬虫丢名而为空
        # （历史图混入版本价、现价选择顶替），种子的版本名是权威补档；
        # 本地已有名不回写——本地爬虫持续更新，不被旧种子快照拉回
        suffix_backfill = (
            "version_suffix = CASE WHEN (l.version_suffix IS NULL OR l.version_suffix = '')"
            " AND IFNULL(s.version_suffix, '') != ''"
            " THEN s.version_suffix ELSE l.version_suffix END"
        )
        suffix_gap = (
            "((l.version_suffix IS NULL OR l.version_suffix = '')"
            " AND IFNULL(s.version_suffix, '') != '')"
        )
        in_chunk = "s.appid BETWEEN ? AND ?"
        con.execute("BEGIN IMMEDIATE")
        overwritten = con.execute(
            f"UPDATE main.game_price_history AS l SET {set_cols}, {suffix_backfill} "
            f"FROM seed.game_price_history AS s "
            f"WHERE {in_chunk} AND {_GPH_KEY_MATCH} AND ("
            + " OR ".join(f"l.{c} IS NOT s.{c}" for c in GPH_OVERWRITE_COLS)
            + f" OR {suffix_gap}"
            + ")",
            (lo, hi),
        ).rowcount
        inserted = con.execute(
            f"INSERT INTO main.game_price_history ({cols}) "
            f"SELECT {seed_cols} FROM seed.game_price_history AS s "
            f"WHERE {in_chunk} AND NOT EXISTS "
            f"(SELECT 1 FROM main.game_price_history l WHERE {_GPH_KEY_MATCH})",
            (lo, hi),
        ).rowcount
        con.execute(
            "INSERT INTO main.app_settings (key, value_json) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (HISTORY_PROGRESS_KEY, json.dumps({"version": version, "last_appid": hi})),
        )
        con.commit()
        return {"overwritten": overwritten, "inserted": inserted, "done": False}
    finally:
        con.close()


async def merge_history_seed(seed_path: Path | None = None) -> dict | None:
    """价格历史种子并入本地库。返回统计 dict；无种子 / 已合并 / 无该表返回 None。

    判定只看自己的 marker（按种子版本）。千万行按 appid 分块（每块 200 款、
    单块事务秒级），**写闸按块获取**——块间交还写者位，交互写不再等整条
    合并；断点 KV 与块数据同事务落位，中断下次启动续跑。每块在线程池跑
    （写闸在事件循环侧持住，线程池里的原生连接不是无主写者）；调用方
    （lifespan）负责 try/except 不阻塞启动。
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
    if db_file is None:  # 内存库 / 非文件库：无从合并
        return None

    total = {"overwritten": 0, "inserted": 0}
    while True:
        async with write_gate(WritePriority.BACKGROUND, label="seed_history_merge"):
            stats = await asyncio.to_thread(_merge_history_chunk, db_file, path, version)
        if stats is None:
            # 种子不含历史表（旧格式种子）：静默，待下个新格式种子再并
            return None
        total["overwritten"] += stats["overwritten"]
        total["inserted"] += stats["inserted"]
        if stats.get("done"):
            break
    log_event(
        logger,
        "价格历史种子已合并",
        tag="成功",
        detail={
            "种子版本": version,
            "覆盖行": total["overwritten"],
            "新增行": total["inserted"],
        },
    )
    return total


# ── 捆绑包数据合并（独立通道：老用户库也生效）─────────────────────────────


def _merge_bundles_sync(db_file: Path, seed_file: Path, version: str) -> dict:
    """原生 sqlite3 捆绑包主档 + 区价并入（同步函数，调用方放线程池）。

    主档按 bundle_id 缺行落行（本地已有行不动——爬虫刷新持续更新，不被旧
    快照拉回）；区价按**包粒度**让位本地观测：本地有该包任何区价行即整包
    跳过（本地爬过的包以本地为准），本地从未爬到的包整包插入——捆绑包刷新
    只覆盖关注集，未关注的包本地永远不会有行，种子是唯一来源。
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
        has_tables = con.execute(
            "SELECT count(*) FROM seed.sqlite_master WHERE type='table' "
            "AND name IN ('bundles', 'bundle_region_prices')"
        ).fetchone()[0]
        if has_tables < 2:
            return {}
        bundle_cols = ", ".join(BUNDLE_COLS)
        bundle_seed_cols = ", ".join(f"s.{c}" for c in BUNDLE_COLS)
        brp_cols = ", ".join(BRP_COLS)
        brp_seed_cols = ", ".join(f"s.{c}" for c in BRP_COLS)
        con.execute("BEGIN IMMEDIATE")
        bundles_inserted = con.execute(
            f"INSERT INTO main.bundles ({bundle_cols}) "
            f"SELECT {bundle_seed_cols} FROM seed.bundles AS s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM main.bundles l WHERE l.bundle_id = s.bundle_id)"
        ).rowcount
        prices_inserted = con.execute(
            f"INSERT INTO main.bundle_region_prices ({brp_cols}) "
            f"SELECT {brp_seed_cols} FROM seed.bundle_region_prices AS s "
            "WHERE NOT EXISTS ("
            "  SELECT 1 FROM main.bundle_region_prices l WHERE l.bundle_id = s.bundle_id)"
        ).rowcount
        con.execute(
            "INSERT INTO main.app_settings (key, value_json) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json",
            (BUNDLES_MARKER_KEY, json.dumps(version)),
        )
        con.commit()
        return {"bundles": bundles_inserted, "bundle_prices": prices_inserted}
    finally:
        con.close()


async def merge_bundles_seed(seed_path: Path | None = None) -> dict | None:
    """捆绑包数据种子并入本地库。返回统计 dict；无种子 / 已合并 / 无该表返回 None。

    判定只看自己的 marker（按种子版本）；同步实现在线程池跑，调用方
    （lifespan）负责 try/except 不阻塞启动。
    """
    path = seed_path or seed_db_path()
    meta = read_seed_meta(path)
    version = str(meta.get("version", "")) if meta else ""
    if not path.is_file() or not version:
        return None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(BUNDLES_MARKER_KEY)
    if marker == version:
        return None

    from app.core.database import sqlite_file_path

    db_file = sqlite_file_path()
    if db_file is None:  # 内存库 / 非文件库：无从合并
        return None

    async with write_gate(WritePriority.BACKGROUND, label="seed_bundles_merge"):
        stats = await asyncio.to_thread(_merge_bundles_sync, db_file, path, version)
    if not stats:
        return None
    log_event(
        logger,
        "捆绑包种子已合并",
        tag="成功",
        detail={
            "种子版本": version,
            "主档款数": stats["bundles"],
            "区价行": stats["bundle_prices"],
        },
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
        log_event(
            logger,
            "种子人工列名单读取失败，按无名单处理",
            tag="忽略",
            level=logging.WARNING,
            detail={"路径": str(path), "原因": str(e)},
        )
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

    from app.core.database import WritePriority, write_gate

    async with write_gate(
        WritePriority.BACKGROUND, label="seed_curated_merge"
    ), get_session_factory()() as session:
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
            # 分块受 SQLite 变量上限约束：schema 9 起名单达 1.8 万款，整批
            # 直插必撞 too many SQL variables（与现价通道同型）
            for i in range(0, len(values), 500):
                await session.execute(sqlite_insert(Game).values(values[i:i + 500]))
            inserted = len(missing)

        await session.commit()

    await settings_service.set_value(CURATED_MARKER_KEY, version)
    log_event(
        logger,
        "人工列名单种子已合并",
        tag="成功",
        detail={"种子版本": version, "覆写款数": updated, "落名单行": inserted},
    )
    return {"curated_updated": updated, "curated_inserted": inserted}


# ── 预设游戏池合并（独立通道：老用户库也生效）─────────────────────────────


async def _seed_preset_rows(path: Path) -> dict[int, str] | None:
    """种子 preset_games → {appid: name}；无该表（schema 3 及以前）返回 None。

    无名行（登记时库内还没爬到、文件也未带名）不进——并入侧落完整 games
    行必须带 name。坏种子等同无预设。
    """
    if not path.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            rows = con.execute(
                "SELECT appid, name FROM preset_games "
                "WHERE name IS NOT NULL AND TRIM(name) != ''"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.OperationalError:
        return None  # 旧 schema 无该表：非错误，待下一个 schema 4+ 种子再并
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "种子预设池读取失败，按无预设处理",
            tag="忽略",
            level=logging.WARNING,
            detail={"原因": str(e)},
        )
        return None
    return {int(a): n for a, n in rows}


async def merge_preset_seed(seed_path: Path | None = None) -> dict | None:
    """预设游戏池并入本地库（按种子版本，全体用户）。返回统计；无预设/已并返回 None。

    - 本地已有 games 行 → 不动（名字与字段以本地爬取为准）；
    - 本地缺行 → 落完整 games 行（appid + name，updated_at 记种子版本）：
      预设开箱即查（游戏库有内容）；行落即被全池价格主轮（6h 网格）纳入
      刷新——预设监控不写监控池（那是账户态数据，种子不碰）。
    - updated_at 记版本与人工列名单同理：孤儿补抓层只捡 updated_at IS NULL
      的行，预设行不会被它重复捡（价格刷新由全池层负责）。
    幂等由 marker 保证；数据操作本身也幂等，marker 写失败下次启动重做无副作用。
    """
    path = seed_path or seed_db_path()
    meta = read_seed_meta(path)
    version = str(meta.get("version", "")) if meta else ""
    if not path.is_file() or not version:
        return None

    preset = await _seed_preset_rows(path)
    if not preset:
        return None

    from app.domains.settings import service as settings_service

    marker = await settings_service.get_value(PRESET_MARKER_KEY)
    if marker == version:
        return None

    try:
        stamped = datetime.fromisoformat(version)
    except ValueError:
        stamped = datetime.now()

    from app.core.database import WritePriority, write_gate

    async with write_gate(
        WritePriority.BACKGROUND, label="seed_preset_merge"
    ), get_session_factory()() as session:
        present = {
            int(a)
            for (a,) in await session.execute(
                select(Game.appid).where(Game.appid.in_(list(preset)))
            )
        }
        missing = [a for a in preset if a not in present]
        inserted = 0
        if missing:
            values = [
                {"appid": appid, "name": preset[appid], "updated_at": stamped}
                for appid in missing
            ]
            await session.execute(sqlite_insert(Game).values(values))
            inserted = len(missing)
        await session.commit()

    await settings_service.set_value(PRESET_MARKER_KEY, version)
    log_event(
        logger,
        "预设游戏池种子已合并",
        tag="成功",
        detail={"种子版本": version, "落库款数": inserted},
    )
    return {"preset_inserted": inserted}


async def merge_seed_incremental(seed_path: Path | None = None) -> dict | None:
    """按种子版本对全体用户生效的增量通道统一入口：人工列名单 + 预设池 +
    现价快照 + 玩家标签 + 价格历史 + 捆绑包。

    六条子通道各自有 marker、各自幂等、互不拖累（任一失败只记日志，
    其余照常执行，失败方下次启动自动重试）。全部无事发生才返回 None。
    """
    curated = None
    preset = None
    current = None
    tags = None
    history = None
    bundles = None
    try:
        curated = await merge_curated_seed(seed_path)
    except Exception:  # noqa: BLE001
        log_event(logger, "人工列名单种子合并失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        preset = await merge_preset_seed(seed_path)
    except Exception:  # noqa: BLE001
        log_event(logger, "预设池种子合并失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        current = await merge_current_seed(seed_path)
    except Exception:  # noqa: BLE001
        log_event(logger, "现价快照种子合并失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        tags = await merge_game_tags_seed(seed_path)
    except Exception:  # noqa: BLE001
        log_event(logger, "玩家标签种子合并失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        history = await merge_history_seed(seed_path)
    except Exception:  # noqa: BLE001
        log_event(logger, "价格历史种子合并失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    try:
        bundles = await merge_bundles_seed(seed_path)
    except Exception:  # noqa: BLE001
        log_event(logger, "捆绑包种子合并失败，不阻塞启动", level=logging.ERROR, exc_info=True)
    if (
        curated is None and preset is None and current is None
        and tags is None and history is None and bundles is None
    ):
        return None
    return {
        "curated": curated,
        "preset": preset,
        "current": current,
        "tags": tags,
        "history": history,
        "bundles": bundles,
    }
