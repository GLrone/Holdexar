"""云端历史包导出：把公共目录库模板的价格历史按 appid 切成独立下载包。

产物（默认 release/cloud-packs/）：
  packs/{appid}.db.gz   单款游戏全部区服的价格历史（gzip 压缩的单表 SQLite）
  manifest.json         包清单：appid → 路径/字节数/sha256/行数，客户端按清单拉取

包内只有一张 game_price_history 表（列集与模板一致、不含自增 id）——包是
新建库写入的，结构上不可能携带任何用户数据表；manifest 附带列集摘要，
客户端合并前比对列集，不一致即拒收。

用法：
  python scripts/export_cloud_packs.py                 # 全量
  python scripts/export_cloud_packs.py --limit 300     # 前 300 款（试跑）
  python scripts/export_cloud_packs.py --appids 730,570 # 指定款（调试）
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TEMPLATE = ROOT / "release" / "holdexar_template.db"
DEFAULT_OUT = ROOT / "release" / "cloud-packs"
PACK_TABLE = "game_price_history"
# 包内列 = 模板列去掉自增 id（行号在每张包里独立无意义，客户端按业务唯一键合并）
EXCLUDED_COLUMNS = {"id"}


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _columns_of(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [r[1] for r in rows]


def export_pack(appid: int, src_uri: str, pack_dir: Path, columns: list[str]) -> dict:
    """导出单款游戏的包：新建库 → ATTACH 模板只读 → 拷行 → gzip → 摘要。"""
    raw = pack_dir / f"{appid}.db"
    dest = sqlite3.connect(f"file:{raw.as_posix()}?mode=rwc", uri=True, isolation_level=None)
    try:
        dest.execute("ATTACH DATABASE ? AS src", (src_uri,))
        col_list = ", ".join(f'"{c}"' for c in columns)
        dest.execute(
            f'CREATE TABLE {PACK_TABLE} ({col_list})'
        )
        dest.execute(
            f'INSERT INTO {PACK_TABLE} ({col_list}) '
            f'SELECT {col_list} FROM src.{PACK_TABLE} WHERE appid = ?',
            (appid,),
        )
        rows = dest.execute(f"SELECT COUNT(*) FROM {PACK_TABLE}").fetchone()[0]
        tables = {
            r[0] for r in dest.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        dest.execute("DETACH DATABASE src")
    finally:
        dest.close()
    if tables != {PACK_TABLE}:
        raw.unlink(missing_ok=True)
        raise RuntimeError(f"包 {appid} 表集合异常: {sorted(tables)}")

    gz = pack_dir / f"{appid}.db.gz"
    # mtime=0：同数据重导产出相同字节，manifest 的 sha256 跨轮次稳定
    with raw.open("rb") as fin, gz.open("wb") as fout_raw:
        with gzip.GzipFile(fileobj=fout_raw, mode="wb", compresslevel=6, mtime=0) as fout:
            shutil.copyfileobj(fin, fout, 1 << 20)
    raw.unlink(missing_ok=True)
    return {
        "path": f"packs/{appid}.db.gz",
        "bytes": gz.stat().st_size,
        "sha256": _sha256_file(gz),
        "rows": rows,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="按 appid 切分价格历史为云端下载包")
    ap.add_argument("--template", type=Path, default=DEFAULT_TEMPLATE, help="公共目录库模板路径")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="输出目录")
    ap.add_argument("--limit", type=int, default=0, help="只导前 N 款（0 = 全量）")
    ap.add_argument("--appids", type=str, default="", help="逗号分隔的指定 appid（调试用）")
    args = ap.parse_args()

    if not args.template.is_file():
        print(f"[错误] 模板不存在: {args.template}")
        print("       先跑 python server/scripts/export_template_db.py 生成模板")
        return 1

    pack_dir = args.out / "packs"
    pack_dir.mkdir(parents=True, exist_ok=True)

    src_uri = f"file:{args.template.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(src_uri, uri=True)
    columns = [c for c in _columns_of(conn, PACK_TABLE) if c not in EXCLUDED_COLUMNS]
    if not columns or "appid" not in columns:
        print(f"[错误] 模板缺少 {PACK_TABLE} 或列异常")
        return 1

    if args.appids:
        appids = [int(x) for x in args.appids.split(",") if x.strip()]
    else:
        appids = [r[0] for r in conn.execute(
            f"SELECT DISTINCT appid FROM {PACK_TABLE} ORDER BY appid"
        )]
        if args.limit > 0:
            appids = appids[: args.limit]
    total_rows = conn.execute(f"SELECT COUNT(*) FROM {PACK_TABLE}").fetchone()[0]
    games_n = conn.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    conn.close()

    print(f"[导出] 模板: {args.template.name}")
    print(f"[导出] 目标款数: {len(appids)}（模板历史总行数 {total_rows:,}，games {games_n:,}）")

    manifest_packs: dict[str, dict] = {}
    t0 = time.time()
    done_rows = 0
    for i, appid in enumerate(appids, 1):
        manifest_packs[str(appid)] = export_pack(appid, src_uri, pack_dir, columns)
        done_rows += manifest_packs[str(appid)]["rows"]
        if i % 500 == 0 or i == len(appids):
            rate = i / max(time.time() - t0, 0.001)
            print(f"[导出] {i}/{len(appids)}（{rate:.0f} 款/秒，已导行数 {done_rows:,}）")

    manifest = {
        "schemaVersion": 1,
        "packFormat": "sqlite-gz/1",
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "columns": columns,
        "source": {
            "template": args.template.name,
            "games": games_n,
            "historyRows": total_rows,
            "appidCount": len(appids),
        },
        "packs": manifest_packs,
    }
    mpath = args.out / "manifest.json"
    mpath.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )

    sizes = [p["bytes"] for p in manifest_packs.values()]
    print(
        f"[完成] {len(manifest_packs)} 包 → {pack_dir}\n"
        f"       总大小 {sum(sizes) / 1048576:.1f} MB，单包 {min(sizes) / 1024:.0f}~"
        f"{max(sizes) / 1024:.0f} KB（中位 {sorted(sizes)[len(sizes) // 2] / 1024:.0f} KB）\n"
        f"       清单: {mpath}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
