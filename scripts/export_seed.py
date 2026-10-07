"""资产种子导出：生产库公共切片 → assets/seed/holdexar_seed.db。

白名单制：只读六块公共数据（games 人工策划列（含 name 身份列）/ 预设游戏池
清单 / games 主档目录行 + game_current_prices 现价快照 / game_tags 玩家标签 /
game_price_history 全量价格历史 / bundles + bundle_region_prices 捆绑包），
结构上不可能带出凭据表。**不含汇率**：由 rates 域免 Key Provider 现抓回填。
独立分发（按需下载），启动时并入：各通道按种子版本独立合并（老用户库也并入）。

用法（发布机）：
    python scripts/export_seed.py                     # data/holdexar.db → assets/seed/
    python scripts/export_seed.py --db <源库> --out <目标>
    python scripts/build_release.py --refresh-seed    # 出包前自动重导

去重键：games_curated 只收任一人工列非空的行；game_current_prices 以本地主键
(appid, region_code) 原样带出；price_history 依赖源库 ux_gph_snapshot 唯一索引
（六段逻辑键含 COALESCE(price,0)）天然无重复，种子侧建同款索引。

人工列名单与预设池随种子下发完整 games 行：合并侧对本地缺行的 appid 直接
落行（name 取种子），新用户开箱即有初始目录；名单行 updated_at 非空，不被
孤儿补抓层捡去爬——监控范围只由愿望单驱动，名单 ≠ 监控。

现价快照（games_catalog + game_current_prices）：库内有现价的游戏整表带出
（主档最小列集 + 全区现价行），新用户开箱即有全量目录与现价，找游戏页
首屏就有数据；合并按 appid 粒度让位本地观测（本地爬过的游戏不回拨）。

价格历史整表带出（INSERT only 观测事实）；捆绑包主档 + 全区价格快照整表
带出——种子是发布库的完整业务镜像切片，随发布库增删同步（整表重导）。
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from app.core.paths import resolve_data_dir  # noqa: E402 —— 数据目录判定的单一来源

DEFAULT_OUT = ROOT / "assets" / "seed" / "holdexar_seed.db"

SEED_SCHEMA_VERSION = 9
CURATED_COLS = ("xgp_tier", "epic_date", "is_epic", "is_hb", "hb_data", "series_id")
# 人工列名单行的身份列：名单行要在用户库里落成完整 games 行（缺行时），
# name 是 games 表唯一 NOT NULL 的展示字段。与 CURATED_COLS 分列：覆写
# 人工列时不碰 name（本地爬来的行名更准），INSERT 缺行时才用它
CURATED_IDENTITY_COL = "name"

# game_current_prices 种子列（appid+region_code 是本地主键，同键库内不会重复；
# 列全集显式带出——合并侧按列名集合直插，不依赖库内默认值）。与
# seed_assets.GCP_COLS 同序同集
GCP_COLS = (
    "appid", "region_code", "currency", "price", "original_price",
    "discount_percent", "sub_id", "price_status", "fail_count", "cny_fen",
    "discount_end_ts", "updated_at",
)
# 现价快照自带 games 主档行：找游戏页展示/筛选所需最小集 + 库内 NOT NULL
# 无默认列（family_sharing/trading_cards/positive_reviews/review_count/
# view_count 必须显式给值）。排序缓存列（min_cny_fen/diff_fen/smart_score/
# hl_flag/pp_flag）由启动链标记三连按现价重算，不带；人工列走 games_curated
# 通道，不带。与 seed_assets.GC_COLS 同序同集
GC_COLS = (
    "appid", "name", "name_en", "type", "header_image", "family_sharing",
    "trading_cards", "is_adult", "is_visual_novel", "release_date",
    "positive_rate", "positive_reviews", "review_count", "view_count",
    "removed_at", "free_kind",
)
# 玩家标签种子列（appid+tagid 即主键；重量随行，顺序即热门度）。
# 与 seed_assets 侧的并入口径配对：只带「有现价的游戏」的标签，与目录行同域
GAME_TAGS_COLS = ("appid", "tagid", "weight")

# 价格历史种子列（与 seed_assets.GPH_COLS 同序同集，不含自增 id）。
# 不带 attach_browse_extras 的促销富化列（discount_desc/bundle_id/
# bundle_discount_pct）——每轮刷新由该通道重贴，非持久观测事实
GPH_COLS = (
    "appid", "region_code", "currency", "price", "original_price",
    "discount_percent", "sub_id", "is_gold", "version_suffix", "is_bundle",
    "price_status", "cny_fen", "snapshot_at", "discount_end_ts",
    "steam_event_key",
)
# 与源库 ux_gph_snapshot 唯一索引同款六段逻辑键（合并侧 key_match 同口径）
_GPH_KEY_INDEX_SQL = (
    "CREATE UNIQUE INDEX ux_seed_gph_snapshot ON game_price_history ("
    "appid, region_code, snapshot_at, COALESCE(sub_id, -1), "
    "COALESCE(price, 0), COALESCE(is_gold, 0))"
)
# 捆绑包主档种子列（与 seed_assets 同序同集，不含自增 id；排序缓存列
# min_cny_fen/diff_fen/smart_score 随快照原样带出，与种子价格行自洽）
BUNDLE_COLS = (
    "bundle_id", "name", "must_purchase_as_set", "item_kind", "header_image",
    "is_lowest", "min_cny_fen", "diff_fen", "smart_score", "url", "app_ids",
    "view_count", "updated_at",
)
# 捆绑包区域价格种子列（不含自增 id）
BRP_COLS = (
    "bundle_id", "region_code", "currency", "price", "original_price",
    "discount_percent", "bundle_base_discount", "price_status", "cny_fen",
    "discount_end_ts", "app_ids", "crawled_at",
)

_HISTORY_CHUNK = 50_000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_current_prices(src: sqlite3.Connection):
    """现价快照切片（流式）：本地主键 (appid, region_code) 原样带出，按块产出。"""
    cols = ", ".join(f"g.{c}" for c in GCP_COLS)
    cur = src.execute(f"SELECT {cols} FROM game_current_prices g")
    while True:
        rows = cur.fetchmany(_HISTORY_CHUNK)
        if not rows:
            return
        yield rows


def _iter_game_tags(src: sqlite3.Connection):
    """玩家标签切片（流式）：只带有现价的游戏（与目录行同域，避免孤立标签行）。"""
    cur = src.execute(
        "SELECT appid, tagid, weight FROM game_tags "
        "WHERE appid IN (SELECT DISTINCT appid FROM game_current_prices) "
        "ORDER BY appid, tagid"
    )
    while True:
        rows = cur.fetchmany(_HISTORY_CHUNK)
        if not rows:
            return
        yield rows


def _avail_cols(src: sqlite3.Connection, table: str) -> set[str]:
    """源库表的现有列集（老源库缺新列时导出侧按 NULL 带出，与合并侧同哲学）。"""
    return {r[1] for r in src.execute(f"PRAGMA table_info({table})")}


def _iter_history(src: sqlite3.Connection):
    """价格历史切片（流式）：整表原样带出（源库唯一索引保证逻辑键无重复）；
    老源库缺新列时该列按 NULL 补位。"""
    available = _avail_cols(src, "game_price_history")
    cols = [c for c in GPH_COLS if c in available]
    pad = (None,) * (len(GPH_COLS) - len(cols))
    cur = src.execute(
        f"SELECT {', '.join(cols)} FROM game_price_history ORDER BY appid, region_code"
    )
    while True:
        rows = cur.fetchmany(_HISTORY_CHUNK)
        if not rows:
            return
        yield rows if not pad else [tuple(r) + pad for r in rows]


def export(db_path: Path, out_path: Path) -> dict:
    if not db_path.is_file():
        sys.exit(f"[错误] 源库不存在：{db_path}")
    if out_path.resolve() == db_path.resolve():
        sys.exit("[错误] 源库与目标不能是同一文件")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if out_path.exists():
        out_path.unlink()

    # 源库只读连接（原文读出，fetched_at / snapshot_at 保持库内 TEXT 原格式）
    src = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        curated = src.execute(
            f"SELECT appid, {CURATED_IDENTITY_COL}, {', '.join(CURATED_COLS)} FROM games "
            "WHERE xgp_tier IS NOT NULL OR epic_date IS NOT NULL OR is_epic = 1 "
            "OR is_hb = 1 OR hb_data IS NOT NULL OR series_id IS NOT NULL"
        ).fetchall()
        # 预设游戏池（老源库缺表 = 切片为空）：无关名的行不进种子（并入侧落完整
        # games 行必须带 name）；名字以库内爬取结果为准、登记时带回的名字兜底
        has_preset = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='preset_games'"
        ).fetchone() is not None
        preset: list[tuple] = []
        if has_preset:
            preset = src.execute(
                "SELECT p.appid, COALESCE(g.name, p.name), p.source, p.added_at "
                "FROM preset_games p LEFT JOIN games g ON g.appid = p.appid "
                "WHERE TRIM(COALESCE(g.name, p.name, '')) != '' "
                "ORDER BY p.appid"
            ).fetchall()
        # 现价快照（同缺表语义）：有现价的游戏整表带出——主档目录行（最小
        # 列集，无名行不进，与预设池同守卫）+ 全区现价行
        has_gcp = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='game_current_prices'"
        ).fetchone() is not None
        games_catalog: list[tuple] = []
        if has_gcp:
            gc_cols = ", ".join(f"g.{c}" for c in GC_COLS)
            games_catalog = src.execute(
                f"SELECT {gc_cols} FROM games g "
                "WHERE g.appid IN (SELECT DISTINCT appid FROM game_current_prices) "
                "AND TRIM(COALESCE(g.name, '')) != ''"
            ).fetchall()
        # 玩家标签（老源库无该表 = 切片为空，与价格历史同守卫）
        has_tags = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='game_tags'"
        ).fetchone() is not None
        # 捆绑包（老源库缺表 = 切片为空）：主档 + 区域价格快照整表带出
        has_bundles = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='bundle_region_prices'"
        ).fetchone() is not None
        bundles: list[tuple] = []
        if has_bundles:
            bundle_avail = _avail_cols(src, "bundles")
            bundle_cols = [c for c in BUNDLE_COLS if c in bundle_avail]
            pad = tuple(None for _ in range(len(BUNDLE_COLS) - len(bundle_cols)))
            bundles = [
                tuple(r) + pad
                for r in src.execute(
                    f"SELECT {', '.join(bundle_cols)} FROM bundles"
                ).fetchall()
            ]

        exported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seed = sqlite3.connect(str(out_path))
        try:
            seed.execute("PRAGMA journal_mode=OFF")
            seed.execute("PRAGMA synchronous=OFF")
            seed.executescript(
                f"""
                CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE games_curated (
                    appid INTEGER PRIMARY KEY,
                    name TEXT,
                    xgp_tier TEXT,
                    epic_date TEXT,
                    is_epic INTEGER,
                    is_hb INTEGER,
                    hb_data TEXT,
                    series_id TEXT
                );
                CREATE TABLE preset_games (
                    appid INTEGER PRIMARY KEY,
                    name TEXT,
                    source TEXT,
                    added_at TEXT
                );
                CREATE TABLE games_catalog (
                    appid INTEGER PRIMARY KEY,
                    name TEXT,
                    name_en TEXT,
                    type TEXT,
                    header_image TEXT,
                    family_sharing INTEGER,
                    trading_cards INTEGER,
                    is_adult INTEGER,
                    is_visual_novel INTEGER,
                    release_date TEXT,
                    positive_rate INTEGER,
                    positive_reviews INTEGER,
                    review_count INTEGER,
                    view_count INTEGER,
                    removed_at TEXT,
                    free_kind TEXT
                );
                CREATE TABLE game_current_prices (
                    appid INTEGER,
                    region_code TEXT,
                    currency TEXT,
                    price INTEGER,
                    original_price INTEGER,
                    discount_percent INTEGER,
                    sub_id INTEGER,
                    price_status TEXT,
                    fail_count INTEGER,
                    cny_fen INTEGER,
                    discount_end_ts INTEGER,
                    updated_at TEXT
                );
                CREATE INDEX ix_seed_gcp_appid
                    ON game_current_prices (appid);
                CREATE TABLE game_tags (
                    appid INTEGER,
                    tagid INTEGER,
                    weight INTEGER,
                    PRIMARY KEY (appid, tagid)
                ) WITHOUT ROWID;
                CREATE TABLE game_price_history (
                    appid INTEGER,
                    region_code TEXT,
                    currency TEXT,
                    price INTEGER,
                    original_price INTEGER,
                    discount_percent INTEGER,
                    sub_id INTEGER,
                    is_gold INTEGER,
                    version_suffix TEXT,
                    is_bundle INTEGER,
                    price_status TEXT,
                    cny_fen INTEGER,
                    snapshot_at TEXT,
                    discount_end_ts INTEGER,
                    steam_event_key TEXT
                );
                CREATE INDEX ix_seed_gph_appid ON game_price_history (appid);
                {_GPH_KEY_INDEX_SQL};
                CREATE TABLE bundles (
                    bundle_id INTEGER PRIMARY KEY,
                    name TEXT,
                    must_purchase_as_set INTEGER,
                    item_kind INTEGER,
                    header_image TEXT,
                    is_lowest INTEGER,
                    min_cny_fen INTEGER,
                    diff_fen INTEGER,
                    smart_score REAL,
                    url TEXT,
                    app_ids TEXT,
                    view_count INTEGER,
                    updated_at TEXT
                );
                CREATE TABLE bundle_region_prices (
                    bundle_id INTEGER,
                    region_code TEXT,
                    currency TEXT,
                    price INTEGER,
                    original_price INTEGER,
                    discount_percent INTEGER,
                    bundle_base_discount INTEGER,
                    price_status TEXT,
                    cny_fen INTEGER,
                    discount_end_ts INTEGER,
                    app_ids TEXT,
                    crawled_at TEXT,
                    PRIMARY KEY (bundle_id, region_code)
                );
                """
            )
            seed.executemany(
                "INSERT INTO games_curated VALUES (?, ?, ?, ?, ?, ?, ?, ?)", curated
            )
            seed.executemany("INSERT INTO preset_games VALUES (?, ?, ?, ?)", preset)
            gc_placeholders = ", ".join("?" for _ in GC_COLS)
            seed.executemany(
                f"INSERT INTO games_catalog VALUES ({gc_placeholders})",
                games_catalog,
            )
            gcp_rows = 0
            gcp_placeholders = ", ".join("?" for _ in GCP_COLS)
            if has_gcp:
                for chunk in _iter_current_prices(src):
                    seed.executemany(
                        f"INSERT INTO game_current_prices VALUES ({gcp_placeholders})", chunk
                    )
                    gcp_rows += len(chunk)
            tags_rows = 0
            tags_placeholders = ", ".join("?" for _ in GAME_TAGS_COLS)
            if has_gcp and has_tags:
                for chunk in _iter_game_tags(src):
                    seed.executemany(
                        f"INSERT INTO game_tags VALUES ({tags_placeholders})", chunk
                    )
                    tags_rows += len(chunk)
            history_rows = 0
            gph_placeholders = ", ".join("?" for _ in GPH_COLS)
            for chunk in _iter_history(src):
                seed.executemany(
                    f"INSERT INTO game_price_history VALUES ({gph_placeholders})", chunk
                )
                history_rows += len(chunk)
            seed.executemany(
                f"INSERT INTO bundles VALUES ({', '.join('?' for _ in BUNDLE_COLS)})",
                bundles,
            )
            brp_rows = 0
            if has_bundles:
                brp_avail = _avail_cols(src, "bundle_region_prices")
                brp_cols = [c for c in BRP_COLS if c in brp_avail]
                pad = (None,) * (len(BRP_COLS) - len(brp_cols))
                brp_placeholders = ", ".join("?" for _ in BRP_COLS)
                cur = src.execute(f"SELECT {', '.join(brp_cols)} FROM bundle_region_prices")
                while True:
                    chunk = cur.fetchmany(_HISTORY_CHUNK)
                    if not chunk:
                        break
                    seed.executemany(
                        f"INSERT INTO bundle_region_prices VALUES ({brp_placeholders})",
                        [tuple(r) + pad for r in chunk],
                    )
                    brp_rows += len(chunk)
            seed.executemany(
                "INSERT INTO seed_meta VALUES (?, ?)",
                [
                    ("schema_version", str(SEED_SCHEMA_VERSION)),
                    ("version", exported_at),
                    ("exported_at", exported_at),
                    ("rows_curated", str(len(curated))),
                    ("rows_preset", str(len(preset))),
                    ("rows_games_catalog", str(len(games_catalog))),
                    ("rows_current_prices", str(gcp_rows)),
                    ("rows_game_tags", str(tags_rows)),
                    ("rows_price_history", str(history_rows)),
                    ("rows_bundles", str(len(bundles))),
                    ("rows_bundle_prices", str(brp_rows)),
                ],
            )
            seed.commit()
        finally:
            seed.close()
    finally:
        src.close()

    size = out_path.stat().st_size
    print(
        f"[种子] 人工列 {len(curated)} 款 / 预设池 {len(preset)} 款 / "
        f"现价快照 {len(games_catalog)} 款 {gcp_rows} 行 / 玩家标签 {tags_rows} 行"
    )
    print(
        f"[种子] 价格历史 {history_rows} 行 / 捆绑包 {len(bundles)} 款 "
        f"{brp_rows} 区价行"
    )
    print(f"[种子] {out_path}（{size / 1048576:.1f} MB）")
    print(f"[种子] sha256 {_sha256(out_path)[:16]}…  version={exported_at}")
    return {
        "curated": len(curated),
        "preset": len(preset),
        "games_catalog": len(games_catalog),
        "current_prices": gcp_rows,
        "game_tags": tags_rows,
        "price_history": history_rows,
        "bundles": len(bundles),
        "bundle_prices": brp_rows,
        "size": size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 资产种子导出")
    # 默认源库走数据目录判定（数据目录与仓库解耦，随安装位置而定）
    default_db = resolve_data_dir() / "holdexar.db"
    parser.add_argument("--db", default=str(default_db), help=f"源库路径（默认 {default_db}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"种子输出路径（默认 {DEFAULT_OUT}）")
    args = parser.parse_args()
    export(Path(args.db), Path(args.out))


if __name__ == "__main__":
    main()
