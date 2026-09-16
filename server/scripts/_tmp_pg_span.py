# -*- coding: utf-8 -*-
"""临时：PG game_price_history 跨度统计 → 需补抓 appid 名单"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
HERE = Path(__file__).resolve().parent


def load_dsn() -> str:
    f = HERE.parent.parent / "secrets" / "heybox_pg.env"
    if f.is_file():
        for line in f.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                if k.strip() == "HEYBOX_PG_DSN":
                    return v.strip()
    return ""


import psycopg2  # noqa: E402

conn = psycopg2.connect(load_dsn(), connect_timeout=10)
cur = conn.cursor()
t0 = time.time()

# 组合级（appid, sub_id, region_code）跨度
cur.execute("""
    create temporary table _span as
    select appid, sub_id, region_code,
           count(*) as pts,
           min(snapshot_at) as t0, max(snapshot_at) as t1,
           (max(snapshot_at) - min(snapshot_at)) as span
    from game_price_history
    group by 1,2,3
""")
print(f"组合聚合完成 {time.time()-t0:.1f}s")

cur.execute("select count(*) from _span")
n_combos = cur.fetchone()[0]
cur.execute("select count(*) from _span where pts = 1")
n_single = cur.fetchone()[0]
cur.execute("select count(*) from _span where span < interval '30 days'")
n_short = cur.fetchone()[0]
cur.execute("select count(distinct appid) from _span where span < interval '30 days'")
n_apps_short = cur.fetchone()[0]
cur.execute("select count(distinct appid) from _span")
n_apps_all = cur.fetchone()[0]

print(f"\n=== PG game_price_history 跨度 ===")
print(f"  组合(appid,sub_id,region) : {n_combos}")
print(f"  单点(仅1条)               : {n_single}")
print(f"  跨度 <30 天               : {n_short}")
print(f"  >>> 涉及游戏(appid)       : {n_apps_short}  (库内游戏共 {n_apps_all})")

print(f"\n--- 各区 <30天 组合数 ---")
cur.execute("""
    select region_code, count(*),
           count(*) filter (where pts=1),
           count(distinct appid) filter (where span < interval '30 days')
    from _span group by 1 order by 1
""")
for r in cur.fetchall():
    print(f"  {r[0]:<5} 组合={r[1]:<7} 单点={r[2]:<7} <30天游戏={r[3]}")

# 导出 appid 名单
cur.execute("select distinct appid from _span where span < interval '30 days' order by 1")
appids = [int(r[0]) for r in cur.fetchall()]
out = Path(r"E:\STEAM-price\backfill_appids_from_pg.json")
out.write_text(json.dumps({
    "generated_at": "2026-09-17",
    "source": "PG steamhl_db.game_price_history",
    "criteria": "任一 (appid, sub_id, region_code) 走势跨度 < 30 天 → 整游戏补抓",
    "count": len(appids),
    "appids": appids,
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\n名单已导出: {out} ({len(appids)} 个 appid)")
print(f"总耗时 {time.time()-t0:.1f}s")
conn.close()
