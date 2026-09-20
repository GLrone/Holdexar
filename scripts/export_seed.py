"""资产种子导出：生产库公共切片 → assets/seed/holdexar_seed.db。

白名单制：只读五块公共数据（汇率快照 / 汇率历史 / games 人工策划列（含
name 身份列）/ 预设游戏池清单 / game_price_history 切片），结构上不可能
带出凭据表。随包分发，启动时并入：汇率走一次性导入，人工列名单、预设池
与价格历史走按种子版本的独立合并通道（老用户库也并入）。

用法（发布机）：
    python scripts/export_seed.py                     # data/holdexar.db → assets/seed/
    python scripts/export_seed.py --db <源库> --out <目标>
    python scripts/export_seed.py --history-days 0    # 价格历史不设窗口（全量）
    python scripts/build_release.py --refresh-seed    # 出包前自动重导

去重键（同键保留 id 最大一行，对齐写入侧幂等口径）：fx_rate_history 按
(currency_code, fetched_at 原文)；game_price_history 按 (appid, region_code,
sub_id, is_gold, snapshot_at 原文)，price 不进键（同键不同价 = 同一次快照的
价格修正）；games_curated 只收任一人工列非空的行。

人工列名单与预设池随种子下发完整 games 行：合并侧对本地缺行的 appid 直接
落行（name 取种子），新用户开箱即有初始目录；名单行 updated_at 非空，不被
孤儿补抓层捡去爬——监控范围只由愿望单驱动，名单 ≠ 监控。

价格历史窗口：默认全量时间切片（0）；--history-days 可按天裁窗，合并通道
按逻辑键幂等去重，老用户每次升级都并入最新切片。
"""
from __future__ import annotations

import argparse
import hashlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from app.core.paths import resolve_data_dir  # noqa: E402 —— 数据目录判定的单一来源

DEFAULT_OUT = ROOT / "assets" / "seed" / "holdexar_seed.db"

SEED_SCHEMA_VERSION = 4
CURATED_COLS = ("xgp_tier", "epic_date", "is_epic", "is_hb", "hb_data", "series_id")
# 人工列名单行的身份列：名单行要在用户库里落成完整 games 行（缺行时），
# name 是 games 表唯一 NOT NULL 的展示字段。与 CURATED_COLS 分列：覆写
# 人工列时不碰 name（本地爬来的行名更准），INSERT 缺行时才用它
CURATED_IDENTITY_COL = "name"
# 价格历史窗口默认值（天）；0 = 全量时间切片（发行种子带完整历史，不为包体砍窗口）
HISTORY_DAYS_DEFAULT = 0

# game_price_history 种子列（不含自增 id：合并侧本地表自增）。列序即插入序，
# 与 seed_assets.GPH_COLS 保持一致
GPH_COLS = (
    "appid", "region_code", "currency", "price", "original_price",
    "discount_percent", "sub_id", "is_gold", "version_suffix", "is_bundle",
    "price_status", "cny_fen", "snapshot_at",
)

_HISTORY_CHUNK = 50_000


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _iter_history(src: sqlite3.Connection, history_days: int):
    """价格历史切片（流式）：同逻辑键保留 id 最大一行，按块产出。

    逻辑键 (appid, region_code, sub_id, is_gold, snapshot_at) 与写入侧
    OR REPLACE 的去重口径一致（sub_id 归一用 -1、is_gold 用 0，对齐
    ux_gph_snapshot 唯一索引的 COALESCE 常量）；price 不进键——同键不同价
    视为同一次快照的价格修正，保留最新写入。
    """
    where = "WHERE snapshot_at >= datetime('now', ?) " if history_days > 0 else ""
    params: tuple = (f"-{history_days} days",) if history_days > 0 else ()
    cur = src.execute(
        "SELECT g.appid, g.region_code, g.currency, g.price, g.original_price, "
        "g.discount_percent, g.sub_id, g.is_gold, g.version_suffix, g.is_bundle, "
        "g.price_status, g.cny_fen, g.snapshot_at "
        "FROM game_price_history g JOIN ("
        "  SELECT MAX(id) AS id FROM game_price_history "
        f" {where} "
        "  GROUP BY appid, region_code, IFNULL(sub_id, -1), IFNULL(is_gold, 0), snapshot_at"
        ") m ON g.id = m.id",
        params,
    )
    while True:
        rows = cur.fetchmany(_HISTORY_CHUNK)
        if not rows:
            return
        yield rows


def export(db_path: Path, out_path: Path, history_days: int = HISTORY_DAYS_DEFAULT) -> dict:
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
        fx_rates = src.execute(
            "SELECT currency_code, rate_to_cny, fetched_at FROM fx_rates"
        ).fetchall()
        # 同键（币种 + 汇率代表日，canonical 语义）保留最新（按 id 升序遍历，
        # dict 覆盖即末次胜出）；源库缺 canonical 列时回退旧列并从 fetched_at
        # 推导（与 v8 迁移口径一致）
        src_cols = {row[1] for row in src.execute("PRAGMA table_info(fx_rate_history)")}
        if {"rate_date", "source_kind"} <= src_cols:
            history_iter = src.execute(
                "SELECT currency_code, rate_to_cny, source, fetched_at, rate_date, source_kind"
                " FROM fx_rate_history ORDER BY id"
            )
        else:
            history_iter = (
                (cur, rate, source, fa, None, None)
                for cur, rate, source, fa in src.execute(
                    "SELECT currency_code, rate_to_cny, source, fetched_at"
                    " FROM fx_rate_history ORDER BY id"
                )
            )
        history: dict[tuple, tuple] = {}
        for cur, rate, source, fa, rd, kind in history_iter:
            day = str(rd)[:10] if rd is not None else (str(fa)[:10] if fa is not None else "")
            if not kind:
                kind = (
                    "carried"
                    if str(source or "").lower() == "backfill"
                    else "observed"
                )
            history[(str(cur), day)] = (cur, rate, source, fa, day or None, kind)
        curated = src.execute(
            f"SELECT appid, {CURATED_IDENTITY_COL}, {', '.join(CURATED_COLS)} FROM games "
            "WHERE xgp_tier IS NOT NULL OR epic_date IS NOT NULL OR is_epic = 1 "
            "OR is_hb = 1 OR hb_data IS NOT NULL OR series_id IS NOT NULL"
        ).fetchall()
        # 老源库可能还没建价格历史表（schema 演进前的副本）：缺表 = 切片为空
        has_gph = src.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='game_price_history'"
        ).fetchone() is not None
        # 预设游戏池（同缺表语义）：无关名的行不进种子（并入侧落完整
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

        exported_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        seed = sqlite3.connect(str(out_path))
        try:
            seed.execute("PRAGMA journal_mode=OFF")
            seed.execute("PRAGMA synchronous=OFF")
            seed.executescript(
                """
                CREATE TABLE seed_meta (key TEXT PRIMARY KEY, value TEXT);
                CREATE TABLE fx_rates (
                    currency_code TEXT PRIMARY KEY,
                    rate_to_cny REAL,
                    fetched_at TEXT
                );
                CREATE TABLE fx_rate_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    currency_code TEXT,
                    rate_to_cny REAL,
                    source TEXT,
                    fetched_at TEXT,
                    rate_date TEXT,
                    source_kind TEXT
                );
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
                    snapshot_at TEXT
                );
                CREATE INDEX ix_seed_gph_key
                    ON game_price_history (appid, region_code, snapshot_at);
                """
            )
            seed.executemany("INSERT INTO fx_rates VALUES (?, ?, ?)", fx_rates)
            seed.executemany(
                "INSERT INTO fx_rate_history"
                " (currency_code, rate_to_cny, source, fetched_at, rate_date, source_kind)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                list(history.values()),
            )
            seed.executemany(
                "INSERT INTO games_curated VALUES (?, ?, ?, ?, ?, ?, ?, ?)", curated
            )
            seed.executemany("INSERT INTO preset_games VALUES (?, ?, ?, ?)", preset)
            gph_rows = 0
            placeholders = ", ".join("?" for _ in GPH_COLS)
            if has_gph:
                for chunk in _iter_history(src, history_days):
                    seed.executemany(
                        f"INSERT INTO game_price_history VALUES ({placeholders})", chunk
                    )
                    gph_rows += len(chunk)
            seed.executemany(
                "INSERT INTO seed_meta VALUES (?, ?)",
                [
                    ("schema_version", str(SEED_SCHEMA_VERSION)),
                    ("version", exported_at),
                    ("exported_at", exported_at),
                    ("rows_fx_rates", str(len(fx_rates))),
                    ("rows_fx_history", str(len(history))),
                    ("rows_curated", str(len(curated))),
                    ("rows_preset", str(len(preset))),
                    ("rows_history", str(gph_rows)),
                    ("history_days", str(history_days)),
                ],
            )
            seed.commit()
        finally:
            seed.close()
    finally:
        src.close()

    size = out_path.stat().st_size
    window = f"近{history_days}天" if history_days > 0 else "全量"
    print(
        f"[种子] 汇率快照 {len(fx_rates)} 条 / 汇率历史 {len(history)} 行 / "
        f"人工列 {len(curated)} 款 / 预设池 {len(preset)} 款 / "
        f"价格历史（{window}）{gph_rows} 行"
    )
    print(f"[种子] {out_path}（{size / 1048576:.1f} MB）")
    print(f"[种子] sha256 {_sha256(out_path)[:16]}…  version={exported_at}")
    return {
        "fx_rates": len(fx_rates),
        "fx_history": len(history),
        "curated": len(curated),
        "preset": len(preset),
        "history": gph_rows,
        "size": size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 资产种子导出")
    # 默认源库走数据目录判定（数据目录与仓库解耦，随安装位置而定）
    default_db = resolve_data_dir() / "holdexar.db"
    parser.add_argument("--db", default=str(default_db), help=f"源库路径（默认 {default_db}）")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"种子输出路径（默认 {DEFAULT_OUT}）")
    parser.add_argument(
        "--history-days",
        type=int,
        default=HISTORY_DAYS_DEFAULT,
        help=f"价格历史窗口（天），0 = 全量（默认 {HISTORY_DAYS_DEFAULT}）",
    )
    args = parser.parse_args()
    export(Path(args.db), Path(args.out), args.history_days)


if __name__ == "__main__":
    main()
