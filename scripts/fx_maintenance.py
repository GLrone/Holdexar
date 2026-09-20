"""汇率维护脚本：16 年档案导入 + 缺口补齐 + 实时更新。

三个子命令（可独立跑，`all` 串联；全部幂等，重跑安全）：

    python scripts/fx_maintenance.py import-pg   # PG 汇率档案 → 本地 fx_rate_history
                                                 # （2010-04-09 → 2026-07-07，16 币种）
    python scripts/fx_maintenance.py import-fxjson  # fx_rates.json 档案 → 本地
                                                 # （38 币种全量 2010-04-09 → 2026-04-07；
                                                 #   PG 不可用时的档案源，纯 json 读入）
    python scripts/fx_maintenance.py update      # 实时刷新（augmentedsteam → er-api 容灾）
                                                 # 写 fx_rates 快照 + 今日历史行
    python scripts/fx_maintenance.py backfill    # 补齐 fx_rate_history 缺失日期
    python scripts/fx_maintenance.py all         # import-pg → update → backfill

解释器：import-pg 需要 psycopg2（系统 Python）；update/backfill 走后端
服务层，需 server/.venv。venv 无 psycopg2 时用系统 Python 跑 import-pg、
venv 跑其余子命令即可，互不影响。import-fxjson 只用标准库，两解释器均可。

补齐语义（backfill；语义单一实现在服务层 backfill_history，本脚本只做 CLI 包装）：
- 只补**周一~周五**（周六日休市不造行，"节假日除外"）；
- 值 = 该币种**上一交易日汇率延续**（forward-fill），节假日不产生新价——
  外汇市场节假日休市、最近报价延续，与源档案（每天有行、周末=周五值）口径一致；
- 缺口范围 = 该币种最早已有日期 → 今天；已有任意行的日期一律跳过；
- 完全没有历史行的币种无法 forward-fill（无锚点），跳过并报告——
  由 update 的实时通道从当天起建立序列。

PG 连接参数：--dsn 或环境变量 FX_ARCHIVE_PG_DSN。
示例：导入后挂 Windows 计划任务每日跑 `update`（桌面应用运行时调度器
也会每 24h 自动刷，脚本用于应用未开时的兜底）。
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "server"))

from sqlalchemy import delete  # noqa: E402

from app.core.database import get_session_factory, init_db  # noqa: E402


def _load_local_env() -> None:
    """本机私密配置（secrets/fx_maintenance.env，gitignored）：KEY=VALUE 逐行，
    只设尚未存在的环境变量，不覆盖显式传入的。"""
    env_file = Path(__file__).resolve().parents[1] / "secrets" / "fx_maintenance.env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_local_env()
from app.domains.rates.models import FxRate, FxRateHistory  # noqa: E402
from app.domains.rates.service import (  # noqa: E402
    ALLOWED_CURRENCIES,
    backfill_history,
    refresh_rates,
)

# 旧归档库 DSN（含密码不进 git；本机用 ~/.holdexar_fx.env 或环境变量提供）
DEFAULT_DSN = os.environ.get(
    "FX_ARCHIVE_PG_DSN",
    "postgresql://postgres:<本地旧库密码>@localhost:5432/holdexar_archive",
)

IMPORT_SOURCE = "archive_pg"
FXDB_SOURCE = "fx_archive"
FXJSON_SOURCE = "archive_json"

# 外部档案路径（由环境变量提供：本机 secrets/fx_maintenance.env 自动加载）
DEFAULT_FXJSON = os.environ.get("FX_ARCHIVE_FXJSON", "")

DEFAULT_FXDB = os.environ.get("FX_ARCHIVE_DB", "")


# ── 公共小件 ──────────────────────────────────────────────


def _sqlite_path() -> Path:
    from app.core.config import get_settings

    settings = get_settings()
    return settings.data_dir / settings.db_filename


def _to_date(v) -> date:
    """fetched_at（datetime/str/None）→ date。"""
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.fromisoformat(str(v)).date()


# ── import-pg：PG 汇率档案 → 本地 ─────────────────────
# 纯 sqlite3 写入（不经 SQLAlchemy）：psycopg2 装在系统 Python 而 venv 没有，
# 本段保持双解释器可跑。


def _sqlite_connect() -> "sqlite3.Connection":
    import sqlite3

    con = sqlite3.connect(str(_sqlite_path()), timeout=30)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def import_pg(dsn: str) -> None:
    import psycopg2

    # 白名单直接由 CC_LIST 派生（与 rates 域同口径），避免拖入 SQLAlchemy 依赖链
    from app.crawler.config import CC_LIST

    allowed = {cur for _, _, cur in CC_LIST} | {"TRY", "ARS"}

    con = _sqlite_connect()
    existing: dict[str, set[date]] = {}
    for code, fetched in con.execute("SELECT currency_code, fetched_at FROM fx_rate_history"):
        if fetched:
            existing.setdefault(code, set()).add(_to_date(fetched))

    pg = psycopg2.connect(dsn, connect_timeout=5)
    pg.set_session(readonly=True)
    inserted = skipped = 0
    try:
        cur = pg.cursor(name="fx_stream")
        cur.itersize = 5000
        cur.execute(
            "SELECT currency_code, date_str, cny_rate FROM fx_rates"
        )
        batch: list[tuple] = []
        while rows := cur.fetchmany(5000):
            for code, date_str, rate in rows:
                code = (code or "").upper()
                if code not in allowed or rate is None:
                    skipped += 1
                    continue
                day = date.fromisoformat(str(date_str))
                if day in existing.get(code, set()):
                    skipped += 1
                    continue
                existing.setdefault(code, set()).add(day)
                batch.append(
                    (code, float(rate), IMPORT_SOURCE, f"{day.isoformat()} 00:00:00")
                )
                inserted += 1
            if batch:
                con.executemany(
                    "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
                    "VALUES (?, ?, ?, ?)",
                    batch,
                )
                con.commit()
                print(f"[import-pg] ... 已写 {inserted}")
                batch = []
        cur.close()
    finally:
        pg.close()
        con.close()
    print(f"[import-pg] 完成：插入 {inserted} 行，跳过（白名单外/已有）{skipped} 行")


# ── import-fxdb：外部汇率 sqlite 档案 → 本地 ─────
# 账单折算（bills 域）依赖逐日汇率档案；本库恰好补齐 import-pg 未覆盖的
# 币种-年份（AUD/GBP/MYR/THB 等 2015 前后日线）。幂等：只补缺失的
# （币种, 日），已有数据一律不动。纯 sqlite3 双库直读，venv/系统 Python 均可跑。


def import_fxdb(src: str = DEFAULT_FXDB) -> None:
    import sqlite3

    from app.crawler.config import CC_LIST

    allowed = {cur for _, _, cur in CC_LIST} | {"TRY", "ARS"}

    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    con = _sqlite_connect()

    existing: dict[str, set[date]] = {}
    for code, fetched in con.execute("SELECT currency_code, fetched_at FROM fx_rate_history"):
        if fetched:
            existing.setdefault(code, set()).add(_to_date(fetched))

    inserted = skipped = 0
    batch: list[tuple] = []
    rows = src_con.execute("SELECT currency, date, rate_cny FROM exchange_rates ORDER BY currency, date")
    for code, day_str, rate in rows:
        code = (code or "").upper()
        if code not in allowed or rate is None:
            skipped += 1
            continue
        day = date.fromisoformat(str(day_str))
        if day in existing.get(code, set()):
            skipped += 1
            continue
        existing.setdefault(code, set()).add(day)
        batch.append((code, float(rate), FXDB_SOURCE, f"{day.isoformat()} 12:00:00"))
        inserted += 1
        if len(batch) >= 5000:
            con.executemany(
                "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
                "VALUES (?, ?, ?, ?)",
                batch,
            )
            con.commit()
            print(f"[import-fxdb] ... 已写 {inserted}")
            batch = []
    if batch:
        con.executemany(
            "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )
        con.commit()
    src_con.close()
    con.close()
    print(f"[import-fxdb] 完成：插入 {inserted} 行，跳过（白名单外/已有）{skipped} 行")


# ── import-fxjson：fx_rates.json 导出档案 → 本地 ─────
# pg 档案的定期导出覆盖：币种比 PG 通道全、不依赖 PG 存活。
# 幂等：只补缺失的（币种, 日），已有数据一律不动。
# 纯标准库（json + sqlite3），系统 Python / venv 均可跑。


def import_fxjson(src: str = DEFAULT_FXJSON) -> None:
    import json
    import sqlite3

    from app.crawler.config import CC_LIST

    allowed = {cur for _, _, cur in CC_LIST} | {"TRY", "ARS"}

    with open(src, encoding="utf-8") as f:
        data: dict[str, dict[str, float]] = json.load(f)

    con = _sqlite_connect()
    existing: dict[str, set[date]] = {}
    for code, fetched in con.execute("SELECT currency_code, fetched_at FROM fx_rate_history"):
        if fetched:
            existing.setdefault(code, set()).add(_to_date(fetched))

    inserted = skipped = 0
    batch: list[tuple] = []
    for code, series in data.items():
        code = (code or "").upper()
        if code not in allowed:
            skipped += len(series)
            continue
        for day_str, rate in series.items():
            if rate is None or rate <= 0:
                skipped += 1
                continue
            day = date.fromisoformat(str(day_str))
            if day in existing.get(code, set()):
                skipped += 1
                continue
            existing.setdefault(code, set()).add(day)
            batch.append((code, float(rate), FXJSON_SOURCE, f"{day.isoformat()} 00:00:00"))
            inserted += 1
            if len(batch) >= 5000:
                con.executemany(
                    "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
                    "VALUES (?, ?, ?, ?)",
                    batch,
                )
                con.commit()
                print(f"[import-fxjson] ... 已写 {inserted}")
                batch = []
    if batch:
        con.executemany(
            "INSERT INTO fx_rate_history (currency_code, rate_to_cny, source, fetched_at) "
            "VALUES (?, ?, ?, ?)",
            batch,
        )
        con.commit()
    con.close()
    print(f"[import-fxjson] 完成：插入 {inserted} 行，跳过（白名单外/已有）{skipped} 行")


# ── update：实时刷新（复用服务层多源容灾） ─────────────────


async def update() -> None:
    await init_db()
    result = await refresh_rates()
    print(f"[update] source={result['source']} 币种={result['count']} 时间={result['fetchedAt']}")


# ── backfill：缺口补齐（语义单一实现在服务层 backfill_history，此处只做 CLI 包装）──


async def backfill(dry_run: bool = False) -> None:
    await init_db()
    stats = await backfill_history(dry_run=dry_run)
    for code, info in sorted(stats["currencies"].items()):
        print(
            f"[backfill] {code}: 缺 {info['count']} 个交易日（{info['first']} → {info['last']}）"
        )
    if dry_run:
        print(f"[backfill] dry-run：共需插入 {stats['inserted']} 行（未写库）")
    elif stats["inserted"]:
        print(f"[backfill] 完成：插入 {stats['inserted']} 行")
    else:
        print("[backfill] 无需补齐")


# ── 附带：白名单清洗（与服务层 cleanup_disallowed 同语义） ──


async def cleanup() -> None:
    await init_db()
    allowed = sorted(ALLOWED_CURRENCIES)
    async with get_session_factory()() as session:
        h = await session.execute(delete(FxRateHistory).where(FxRateHistory.currency_code.not_in(allowed)))
        s = await session.execute(delete(FxRate).where(FxRate.currency_code.not_in(allowed)))
        await session.commit()
        print(f"[cleanup] 删除白名单外历史 {h.rowcount or 0} 行 / 快照 {s.rowcount or 0} 行")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "command",
        choices=["import-pg", "import-fxdb", "import-fxjson", "update", "backfill", "cleanup", "all"],
        help="见模块 docstring",
    )
    parser.add_argument("--dsn", default=DEFAULT_DSN)
    parser.add_argument("--src", default=DEFAULT_FXDB, help="外部汇率 sqlite 档案路径（import-fxdb）")
    parser.add_argument("--fxjson", default=DEFAULT_FXJSON, help="fx_rates.json 路径（import-fxjson）")
    parser.add_argument("--dry-run", action="store_true", help="backfill 只打印计划不写库")
    args = parser.parse_args()

    async def run_all() -> None:
        try:
            await asyncio.to_thread(import_pg, args.dsn)
        except ModuleNotFoundError:
            print("[import-pg] 当前解释器无 psycopg2，跳过档案导入（用系统 Python 单独跑 import-pg）")
        await update()
        await backfill(args.dry_run)

    if args.command == "import-pg":
        import_pg(args.dsn)
    elif args.command == "import-fxdb":
        import_fxdb(args.src)
    elif args.command == "import-fxjson":
        import_fxjson(args.fxjson)
    elif args.command == "update":
        asyncio.run(update())
    elif args.command == "backfill":
        asyncio.run(backfill(args.dry_run))
    elif args.command == "cleanup":
        asyncio.run(cleanup())
    else:
        asyncio.run(run_all())


if __name__ == "__main__":
    main()
