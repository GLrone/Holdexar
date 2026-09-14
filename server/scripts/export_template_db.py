"""导出公共目录库（模版）：从当前项目所用数据库拷贝全部「游戏商店 / 捆绑包商店」目录数据，剔除所有用户红线数据。

用法：
    python server/scripts/export_template_db.py [输出路径] [源库路径]

- 源库默认 = 当前项目解析出的数据目录库（app.core.paths.resolve_data_dir），
  即「本项目目前使用的数据库」；可传第二参数覆盖。
- 输出默认 = assets/seed/holdexar_template.db。

导出范围（白名单，仅公共目录数据，无任何凭据 / 用户 PII）：
    games, game_current_prices, game_price_history,
    bundles, bundle_region_prices,
    fx_rates, fx_rate_history,
    crawl_regions

刻意排除（用户红线 / 凭据 / 财务 / 行为）：
    steam_accounts, tracked_accounts, wishlist_items, price_alerts, alert_events,
    family_groups, family_library_snapshots,
    proxies, proxy_subscriptions, proxy_events, clash_nodes,
    bill_cdk_games, bill_game_txs, bill_imports, bill_topup_txs,
    app_settings, crawl_jobs  （用户态配置 / 爬虫行为，非目录数据）

附带写入 seed_meta(version=导出时刻) 以便后续可直接作为随包种子并入通道使用。
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # server/scripts -> Holdexar/

# ── 白名单：仅公共目录数据 ──
INCLUDE_TABLES = {
    "games",
    "game_current_prices",
    "game_price_history",
    "bundles",
    "bundle_region_prices",
    "fx_rates",
    "fx_rate_history",
    "crawl_regions",
}


def resolve_source() -> Path:
    """复用项目的数据目录解析，拿到「当前所用」库路径。"""
    sys.path.insert(0, str(PROJECT_ROOT / "server"))
    from app.core.app_info import APP_SLUG
    from app.core.paths import resolve_data_dir

    return resolve_data_dir(APP_SLUG) / f"{APP_SLUG}.db"


def main() -> None:
    out_arg = sys.argv[1] if len(sys.argv) > 1 else None
    src_arg = sys.argv[2] if len(sys.argv) > 2 else None

    src = Path(src_arg) if src_arg else resolve_source()
    if not src.is_file():
        raise SystemExit(f"[导出失败] 源库不存在：{src}")

    out = (
        Path(out_arg)
        if out_arg
        else (PROJECT_ROOT / "assets" / "seed" / "holdexar_template.db")
    )
    out = out.resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()

    print(f"[导出] 源库：{src}")
    print(f"[导出] 目标：{out}")

    src_con = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    try:
        all_tables = [
            r[0]
            for r in src_con.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        ]
    finally:
        src_con.close()

    included = [t for t in all_tables if t in INCLUDE_TABLES]
    excluded = [t for t in all_tables if t not in INCLUDE_TABLES]
    print(f"[导出] 包含表：{', '.join(sorted(included))}")
    print(f"[导出] 排除表（用户红线）：{', '.join(sorted(excluded))}")

    dst = sqlite3.connect(str(out))
    dst.execute("PRAGMA journal_mode=MEMORY")
    dst.execute("PRAGMA synchronous=OFF")
    try:
        # 挂源库只读，按表整表拷贝（含索引）
        try:
            dst.execute(
                "ATTACH DATABASE ? AS src", (f"file:{src.as_posix()}?mode=ro",)
            )
        except sqlite3.OperationalError:
            dst.execute("ATTACH DATABASE ? AS src", (str(src),))

        # 1) 表结构
        for (name, sql) in dst.execute(
            "SELECT name, sql FROM src.sqlite_master "
            "WHERE type='table' AND name IN (%s)"
            % ",".join(f"'{t}'" for t in included)
        ):
            if sql:
                dst.execute(sql)

        # 2) 数据整表拷贝
        for t in included:
            n = dst.execute(
                f"INSERT INTO main.'{t}' SELECT * FROM src.'{t}'"
            ).rowcount
            print(f"  - {t}: {n} 行")

        # 3) 索引（独立于表的 CREATE INDEX）
        idx_rows = dst.execute(
            "SELECT sql FROM src.sqlite_master "
            "WHERE type='index' AND sql IS NOT NULL AND tbl_name IN (%s)"
            % ",".join(f"'{t}'" for t in included)
        ).fetchall()
        for (sql,) in idx_rows:
            if sql:
                dst.execute(sql)

        # 4) seed_meta（兼容随包种子并入通道）
        dst.execute(
            "CREATE TABLE IF NOT EXISTS seed_meta "
            "(key TEXT PRIMARY KEY, value TEXT)"
        )
        version = datetime.now().isoformat(timespec="seconds")
        dst.execute(
            "INSERT OR REPLACE INTO seed_meta (key, value) VALUES (?, ?)",
            ("version", version),
        )
        dst.commit()
    finally:
        dst.close()

    # 5) 收尾 VACUUM（整理 + 生成紧凑单文件）
    final = sqlite3.connect(str(out))
    try:
        final.execute("VACUUM")
    finally:
        final.close()

    size_mb = out.stat().st_size / 1024 / 1024
    print(f"[导出完成] {out} （{size_mb:.1f} MB，version={version}）")


if __name__ == "__main__":
    main()
