"""version_suffix 回填（幂等，可重复运行；自动先备份数据库）。

game_price_history 有两批空版本名行：
1. 历史档案导入行（snapshot_at 为整点日期，2021-2024）：导入路径不走
   extract_version_suffix，天生无名；
2. 爬取链路偶发丢名：browse 响应的 purchase option name 缺失时提取返回空。

空名行的危害——被「标准版判据」（非 gold ∧ 无后缀 ∧ 非捆绑包）误收：
- 历史图表把版本价混进标准版序列（实测：女神异闻录４ 黄金版 CN 区
  ¥156 豪华版混进 ¥125 本体走势，73 行豪华版里 67 行无名）；
- db_writer 的 current 选择在标准版候选里按 min(sub_id) 挑——丢名的
  豪华版（376686）比本体（447601）编号更小，会顶替本体现价。

同一 sub_id 是 Steam 恒定 SKU，版本名不随时间变：按 (appid, sub_id)
分组，组内任一行有非空 version_suffix 即用它回填组内全部空名行。
没有参照名的组（从无名行）保持原样——运行时防再丢锚（db_writer
known_suffix）会随后续爬取逐步补齐。

用法：python scripts/backfill_version_suffix.py
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))
from app.core.paths import resolve_data_dir  # noqa: E402 —— 数据目录判定的单一来源


def main() -> None:
    db_path = resolve_data_dir() / "holdexar.db"
    if not db_path.is_file():
        sys.exit(f"[错误] 数据库不存在：{db_path}")

    backup = db_path.with_name(
        f"holdexar_backup_suffix_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
    )
    shutil.copy2(db_path, backup)
    print(f"[备份] {db_path} → {backup}")

    conn = sqlite3.connect(db_path)
    try:
        # 每个 (appid, sub_id) 的最新非空版本名（sub_id>0 才是真实 SKU，
        # 0 是无 sub 游戏的伪占位，不参与）
        known: dict[tuple[int, int], str] = {
            (int(appid), int(sub)): str(suffix)
            for appid, sub, suffix in conn.execute(
                "SELECT appid, sub_id, version_suffix FROM ("
                "  SELECT appid, sub_id, version_suffix,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY appid, sub_id"
                "           ORDER BY snapshot_at DESC, id DESC) AS rn"
                "  FROM game_price_history"
                "  WHERE sub_id > 0 AND version_suffix IS NOT NULL"
                "        AND version_suffix != ''"
                ") WHERE rn = 1"
            ).fetchall()
        }
        total = conn.execute(
            "SELECT COUNT(*) FROM game_price_history"
        ).fetchone()[0]
        cur = conn.cursor()
        cur.executemany(
            "UPDATE game_price_history SET version_suffix = :suffix"
            " WHERE appid = :appid AND sub_id = :sub"
            "   AND (version_suffix IS NULL OR version_suffix = '')",
            [
                {"appid": appid, "sub": sub, "suffix": name}
                for (appid, sub), name in known.items()
            ],
        )
        conn.commit()
        print(
            f"[完成] 全表 {total} 行，回填 {cur.rowcount} 行"
            f"（参照 {len(known)} 个 (appid, sub_id) 组的已知版本名）"
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
