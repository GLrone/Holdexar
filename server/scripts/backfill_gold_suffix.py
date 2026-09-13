"""存量 game_price_history.version_suffix 回填（纯计算，零外呼）。

背景：extract_version_suffix 的 rsplit 价格尾巴 bug 修复前，
入库的 history 行 suffix 全为空。可离线还原的只有 gold 行
（is_gold=1 → "Gold Edition"）——其余形态的 option_text 未入库、
无法离线还原，靠 12h STALE 轮转新爬自愈。

用法（server/ 下）：
    python scripts/backfill_gold_suffix.py            # 实际执行
    python scripts/backfill_gold_suffix.py --dry-run  # 只统计不写入
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="gold 行 version_suffix 回填")
    parser.add_argument("--dry-run", action="store_true", help="只统计不写入")
    args = parser.parse_args()

    db_path = get_settings().data_dir / "holdexar.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    try:
        total = cur.execute(
            "SELECT COUNT(*) FROM game_price_history "
            "WHERE is_gold=1 AND (version_suffix IS NULL OR version_suffix='')"
        ).fetchone()[0]
        print(f"待回填 gold 行: {total}")
        if total == 0 or args.dry_run:
            print("（dry-run 或无待回填，结束）")
            return 0
        cur.execute(
            "UPDATE game_price_history SET version_suffix='Gold Edition' "
            "WHERE is_gold=1 AND (version_suffix IS NULL OR version_suffix='')"
        )
        conn.commit()
        print(f"已回填 {cur.rowcount} 行（version_suffix='Gold Edition'）")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
