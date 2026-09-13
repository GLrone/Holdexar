"""bundle 数据修复脚本（一次性运行，处理 refresh 双轨混写脏数据）。

三组问题：
1. refresh 写入的 42 区小写行：_parse_formatted_price 旧实现把无小数
   货币的千位分隔当小数（"¥6,237" → 6237 而非 623700），price/cny_fen 全错，
   且与既有大写行大小写并存 → 整组删除；
2. 三个真 Sub（597332/1066582/1252652，mps=1）在老库时期留下的
   4 行/区污染行（每区 4 行重复、价格 dp 带错）→ 删除仅保留正确的一行；
3. bundle 61597 主档被 sub 61597 数据覆盖（name/mps/app_ids）→ 按老库真值恢复：
   Total War 25th Anniversary Collection / mps=0 / 34 appids；其 refresh 批次
   的 sub 行（347440）整组删除，保留早期批次的 16 行真 bundle 区域价。

用法：python scripts/fix_bundle_dirty_data.py   （自动先备份 data/holdexar.db）
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import os
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from app.core.paths import resolve_data_dir  # noqa: E402 —— 数据目录判定的单一来源

# 旧项目库（路径由环境变量 BUNDLE_ARCHIVE_DATA_DIR 提供）
OLD_DB = Path(os.environ.get("BUNDLE_ARCHIVE_DATA_DIR", "")) / "bundle_data.db"

# bundle 61597 老库真值（Total War 25th Anniversary Collection，mps=0）
B61597_NAME = "Total War 25th Anniversary Collection"
B61597_APPIDS = None  # 从老库 cn 行读取
SUB_IDS = (597332, 1066582, 1252652)  # 真 Sub：老库时期污染行


def main() -> None:
    # 数据目录与仓库解耦后不能再硬编码 `<仓库>/data/holdexar.db`（会静默操作一个
    # 不存在的库，或更糟：在旧路径重建一个空库并「修」它）。
    DB = resolve_data_dir() / "holdexar.db"
    if not DB.exists():
        sys.exit(f"DB not found: {DB}")
    backup = DB.with_name(
        f"holdexar-before-bundle-fix-{datetime.now().strftime('%Y%m%d-%H%M%S')}.db"
    )
    shutil.copy2(DB, backup)
    print(f"[backup] {backup.name}")

    conn = sqlite3.connect(str(DB))
    conn.execute("PRAGMA journal_mode=WAL")
    c = conn.cursor()

    # ── 1. 删 refresh 批次的全部小写行（错误解析 + 大小写并存） ──
    cur = c.execute(
        "DELETE FROM bundle_region_prices "
        "WHERE crawled_at LIKE '2026-09-03%' AND region_code = LOWER(region_code)"
    )
    print(f"[1] refresh 小写行（错误解析）删除 {cur.rowcount}")

    # ── 2. 真 Sub 的老库污染行：同区保留一行 ──
    #    污染特征：同一 (bundle_id, UPPER(region_code)) 存在 >1 行。
    #    保留 crawled_at 较新且 price 非空的一行（refresh 的 sub 行为正确来源）。
    removed = 0
    rows = c.execute(
        "SELECT bundle_id, UPPER(region_code) AS rc, COUNT(*) "
        "FROM bundle_region_prices GROUP BY bundle_id, UPPER(region_code) HAVING COUNT(*) > 1"
    ).fetchall()
    for bid, rc, _n in rows:
        keep = c.execute(
            "SELECT id FROM bundle_region_prices WHERE bundle_id = ? AND UPPER(region_code) = ? "
            "AND price IS NOT NULL ORDER BY crawled_at DESC, id DESC LIMIT 1",
            (bid, rc),
        ).fetchone()
        if keep is None:
            keep = c.execute(
                "SELECT id FROM bundle_region_prices WHERE bundle_id = ? AND UPPER(region_code) = ? "
                "ORDER BY id DESC LIMIT 1",
                (bid, rc),
            ).fetchone()
        cur = c.execute(
            "DELETE FROM bundle_region_prices WHERE bundle_id = ? AND UPPER(region_code) = ? AND id != ?",
            (bid, rc, keep[0]),
        )
        removed += cur.rowcount
    print(f"[2] 大小写并存/重复区行去重删除 {removed}")

    # ── 3. bundle 61597 主档恢复 + sub 行清除 ──
    old = sqlite3.connect(str(OLD_DB))
    oc = old.cursor()
    cn_row = oc.execute(
        "SELECT appids, name FROM bundle_data WHERE bundleid = '61597' AND region = 'cn'"
    ).fetchone()
    assert cn_row is not None, "老库无 61597 cn 行"
    appids_json = cn_row[0]
    c.execute(
        "UPDATE bundles SET name = ?, must_purchase_as_set = 0, app_ids = ? WHERE bundle_id = 61597",
        (cn_row[1] or B61597_NAME, appids_json),
    )
    old.close()
    cur = c.execute(
        "DELETE FROM bundle_region_prices WHERE bundle_id = 61597 AND region_code = LOWER(region_code)"
    )
    print(f"[3] 61597 主档恢复为「{cn_row[1]}」(mps=0)；残留小写行再清 {cur.rowcount}")

    conn.commit()

    # ── 自检 ──
    dup = c.execute(
        "SELECT bundle_id, UPPER(region_code), COUNT(*) FROM bundle_region_prices "
        "GROUP BY bundle_id, UPPER(region_code) HAVING COUNT(*) > 1"
    ).fetchall()
    assert not dup, f"仍有重复区行: {dup}"
    for bid in (61597, *SUB_IDS):
        n = c.execute(
            "SELECT COUNT(*) FROM bundle_region_prices WHERE bundle_id = ?", (bid,)
        ).fetchone()[0]
        name, mps = c.execute(
            "SELECT name, must_purchase_as_set FROM bundles WHERE bundle_id = ?", (bid,)
        ).fetchone()
        print(f"[check] {bid}: {n} 行 | name={name[:30]} | mps={mps}")
    conn.close()
    print("[done] OK")


if __name__ == "__main__":
    main()
