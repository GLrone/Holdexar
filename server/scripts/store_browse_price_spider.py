"""IStoreBrowseService 价格抓取层 · 隔离库测试爬虫（生产核心在 app/crawler/browse_store.py）。

本脚本只做「测试壳」：隔离库落地、生产快照种子、对账报告、CLI。
抓取/解析/落库/**编排**零复刻——run() 把目标 appid 与代理配置交给生产
runner.run_crawl，多协程 worker 池（CrawlerScheduler）、IP 循环（每任务独立
Session 迫使 Clash 换出口 + 失败换代理）、429 全局熔断、元数据/CIS 预取
全部继承生产实现；对账通过即代表生产链路可用。捆绑包抓取不在 run_crawl
内（链尾全量刷新独立通道），本脚本不覆盖。

用法（在 server/ 目录下）:
    .venv/Scripts/python.exe scripts/store_browse_price_spider.py --fresh          # 清库重跑全量
    .venv/Scripts/python.exe scripts/store_browse_price_spider.py --limit 100      # 抽样
    .venv/Scripts/python.exe scripts/store_browse_price_spider.py --appids 620,570
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER_ROOT.parent
sys.path.insert(0, str(SERVER_ROOT))

from app.core.paths import resolve_data_dir  # noqa: E402 —— 数据目录判定的单一来源

# ══ 隔离数据库：必须在 import app.* 之前定好 —— get_settings() 是 lru_cache，
#    读一次即定死，之后再改环境变量不会再生效 ══
# 顺序约束：**先**解析生产数据目录（此时 HOLDEXAR_DATA_DIR 还是原值），**再**把它
# 覆盖成隔离目录。颠倒的话 resolve_data_dir() 会解析到隔离库自己——基线库变成
# 被测对象，对账永远「通过」。
_BASELINE_DIR = resolve_data_dir()
DATA_DIR = Path(os.environ.get("BROWSE_TEST_DATA_DIR") or (_BASELINE_DIR / "browse_test"))
(DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
os.environ["HOLDEXAR_DATA_DIR"] = str(DATA_DIR)
TEST_DB = DATA_DIR / "holdexar.db"
# 对账基线（只读）：生产库
BASELINE_DB = Path(
    os.environ.get("BROWSE_TEST_BASELINE_DB") or (_BASELINE_DIR / "holdexar.db")
)

from app.core.config import get_settings  # noqa: E402
from app.core.database import init_db  # noqa: E402
from app.crawler import browse_store as bs  # noqa: E402
from app.crawler.config import DEFAULT_WORKER_COUNT, HTTP_TIMEOUT  # noqa: E402
from app.crawler.runner import CrawlRunConfig, run_crawl  # noqa: E402

logger = logging.getLogger("browse_test")


# ══════════════════════════════════════════════════════════════
# 隔离库：生产快照
# ══════════════════════════════════════════════════════════════

# 从生产库搬过来的表（区服启用集 / 汇率 / 元数据底子）
SEED_TABLES = ("crawl_regions", "fx_rates", "games")
# app_settings 只搬这两族键：既让策略引擎与 worker 数一致，又不把 SMTP 口令 /
# Steam Cookie / API Key 复制到第二份库里
SEED_SETTING_PREFIXES = ("proxy.", "crawl.")


def seed_from_prod() -> None:
    """生产快照 → 隔离库（生产库全程只读），并落对账基线表 baseline_prices。"""
    if not BASELINE_DB.is_file():
        logger.warning("[seed] 生产库不存在，跳过快照：%s", BASELINE_DB)
        return
    src = sqlite3.connect(f"file:{BASELINE_DB.as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(TEST_DB.as_posix())
    try:
        for table in SEED_TABLES:
            src_cols = [r[1] for r in src.execute(f"PRAGMA table_info({table})")]
            dst_cols = {r[1] for r in dst.execute(f"PRAGMA table_info({table})")}
            cols = [c for c in src_cols if c in dst_cols]
            if not cols:
                logger.warning("[seed] %s 无公共列，跳过", table)
                continue
            col_list = ",".join(cols)
            rows = src.execute(f"SELECT {col_list} FROM {table}").fetchall()
            dst.execute(f"DELETE FROM {table}")
            dst.executemany(
                f"INSERT OR REPLACE INTO {table} ({col_list}) "
                f"VALUES ({','.join('?' * len(cols))})",
                rows,
            )
            logger.info("[seed] %s ← 生产 %d 行", table, len(rows))

        keys = [k for (k,) in src.execute("SELECT key FROM app_settings").fetchall()
                if any(str(k).startswith(p) for p in SEED_SETTING_PREFIXES)]
        if keys:
            marks = ",".join("?" * len(keys))
            rows = src.execute(
                f"SELECT key,value_json FROM app_settings WHERE key IN ({marks})", keys
            ).fetchall()
            dst.execute(f"DELETE FROM app_settings WHERE key IN ({marks})", keys)
            dst.executemany(
                "INSERT OR REPLACE INTO app_settings (key, value_json) VALUES (?,?)", rows
            )
            logger.info("[seed] app_settings ← %s", [k for k, _ in rows])

        # 对账基线：生产 game_current_prices 原样拷一份（测试写入不动它）
        dst.execute("DROP TABLE IF EXISTS baseline_prices")
        cols = [r[1] for r in src.execute("PRAGMA table_info(game_current_prices)")]
        col_list = ",".join(cols)
        dst.execute(f"CREATE TABLE baseline_prices AS SELECT {col_list} "
                    f"FROM game_current_prices WHERE 0")
        rows = src.execute(f"SELECT {col_list} FROM game_current_prices").fetchall()
        dst.executemany(
            f"INSERT INTO baseline_prices ({col_list}) VALUES ({','.join('?' * len(cols))})",
            rows,
        )
        logger.info("[seed] baseline_prices ← 生产 %d 行（对账基线）", len(rows))
        dst.commit()
    finally:
        src.close()
        dst.close()


def override_clash_port(port: int) -> None:
    """测试期覆盖隔离库的 proxy.clash_port（生产快照可能指向已停用的端口）。

    只动隔离库的 app_settings，不改代码路径：策略引擎随后照常读这张表。
    """
    conn = sqlite3.connect(TEST_DB.as_posix())
    try:
        row = conn.execute(
            "SELECT value_json FROM app_settings WHERE key='proxy.clash_port'"
        ).fetchone()
        old = row[0] if row else None
        conn.execute(
            "INSERT OR REPLACE INTO app_settings (key, value_json, updated_at) "
            "VALUES ('proxy.clash_port', ?, datetime('now'))",
            (json.dumps(port),),
        )
        conn.commit()
        logger.info("[override] proxy.clash_port: %s → %s", old, port)
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════
# 对账报告
# ══════════════════════════════════════════════════════════════


def build_report() -> dict:
    conn = sqlite3.connect(TEST_DB.as_posix())
    try:
        has_baseline = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='baseline_prices'"
        ).fetchone()
        rows = conn.execute(
            "SELECT region_code, price_status, COUNT(*) FROM game_current_prices "
            "GROUP BY region_code, price_status"
        ).fetchall()
        report: dict = {
            "new_rows_by_region_status": [
                {"region": r, "status": s, "count": c} for r, s, c in sorted(rows)
            ],
            "regions": [],
            "no_options_locked": bs.NO_OPTIONS_COUNT,
            "parent_followed": bs.PARENT_FOLLOWED,
            "failed_tasks": list(bs.FAILED_TASKS),
        }
        if not has_baseline:
            return report

        pairs = conn.execute(
            """
            SELECT b.region_code, b.appid,
                   b.price, b.original_price, b.discount_percent, b.sub_id,
                   n.price, n.original_price, n.discount_percent, n.sub_id
            FROM baseline_prices b
            JOIN game_current_prices n
              ON n.appid = b.appid AND n.region_code = b.region_code
            WHERE b.price_status = 'ok' AND b.price IS NOT NULL AND b.price > 0
              AND n.price_status = 'ok' AND n.price IS NOT NULL AND n.price > 0
            """
        ).fetchall()

        agg: dict[str, dict] = {}
        fixes: list[dict] = []
        for rg, appid, bp, bo, bd, bsid, np_, no, nd, ns in pairs:
            a = agg.setdefault(
                rg,
                {"region": rg, "n": 0, "price_eq": 0, "price_ne": 0, "disc_eq": 0,
                 "sub_eq": 0, "orig_fixed": 0, "orig_diff_sum": 0, "orig_diff_max": 0},
            )
            a["n"] += 1
            a["price_eq" if bp == np_ else "price_ne"] += 1
            a["disc_eq"] += int((bd or 0) == (nd or 0))
            a["sub_eq"] += int((bsid or 0) == (ns or 0))
            if (bo or 0) != (no or 0):
                a["orig_fixed"] += 1
                diff = abs((no or 0) - (bo or 0))
                a["orig_diff_sum"] += diff
                a["orig_diff_max"] = max(a["orig_diff_max"], diff)
                if len(fixes) < 20:
                    fixes.append(
                        {"appid": appid, "region": rg, "sub_id": ns,
                         "old_original": bo, "new_original": no,
                         "price": np_, "discount": nd}
                    )
        for a in agg.values():
            a["orig_diff_avg"] = (
                round(a["orig_diff_sum"] / a["orig_fixed"]) if a["orig_fixed"] else 0
            )
        report["regions"] = sorted(agg.values(), key=lambda x: x["region"])
        report["original_price_fixes_sample"] = fixes
        report["total"] = {
            "compared": sum(a["n"] for a in agg.values()),
            "price_equal": sum(a["price_eq"] for a in agg.values()),
            "price_diff": sum(a["price_ne"] for a in agg.values()),
            "original_fixed": sum(a["orig_fixed"] for a in agg.values()),
        }
        return report
    finally:
        conn.close()


def print_report(report: dict) -> Path:
    lines = ["════════ 对账报告 ════════"]
    for r in report["new_rows_by_region_status"]:
        lines.append(f"  {r['region']:<4} {r['status']:<8} {r['count']} 行")
    if report.get("no_options_locked"):
        lines.append(f"  「可见但无购买选项」{report['no_options_locked']} 次 → 判 locked（该区不售，不进补抓账本）")
    if report.get("parent_followed"):
        lines.append(f"  子 app 跟父补齐 {report['parent_followed']} 次（type=14 子条目自身无购买选项，取父的价格）")
    if report.get("failed_tasks"):
        lines.append(f"  ⚠ 失败批次：{report['failed_tasks']}")
    for a in report.get("regions", []):
        pct = 100 * a["price_eq"] / a["n"] if a["n"] else 0
        lines.append(
            f"  {a['region']:<4} 对比 {a['n']:>5} | 现价一致 {a['price_eq']:>5} / 不一致 {a['price_ne']:>3}"
            f" | 折扣一致 {a['disc_eq']:>5} | subid 一致 {a['sub_eq']:>5}"
            f" | 原价修正 {a['orig_fixed']:>4}（均差 {a['orig_diff_avg']} 分 / 最大 {a['orig_diff_max']} 分）"
        )
    t = report.get("total", {})
    if t:
        pct = 100 * t["price_equal"] / t["compared"] if t["compared"] else 0
        lines.append(
            f"  合计：对比 {t['compared']}，现价一致 {t['price_equal']}（{pct:.1f}%），"
            f"原价修正 {t['original_fixed']}（{100 * t['original_fixed'] / t['compared']:.1f}%）"
        )
    for line in lines:
        logger.info(line)
    out = DATA_DIR / f"report_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("报告已写出：%s", out)
    return out


# ══════════════════════════════════════════════════════════════
# CLI / 运行
# ══════════════════════════════════════════════════════════════


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="store_browse_price_spider",
        description="IStoreBrowseService 价格抓取 · 隔离库测试爬虫",
    )
    p.add_argument("--appids", default="", help="逗号分隔 appid；缺省取生产启用区有价集")
    p.add_argument("--regions", default="", help="逗号分隔 cc（小写）；缺省为启用区集")
    p.add_argument("--limit", type=int, default=0, help="只取前 N 个 appid（试跑）")
    p.add_argument("--scope", choices=("priced", "all"), default="priced",
                   help="取样口径：priced=生产库启用区有 ok 价（默认）；all=games 全量")
    p.add_argument("--workers", type=int, default=8,
                   help=f"并发 worker 数（worker 池模型同 app/crawler；默认 8，"
                        f"上限参考 {DEFAULT_WORKER_COUNT}）")
    p.add_argument("--batch-size", type=int, default=bs.DEFAULT_BATCH_SIZE,
                   help="单请求 appid 条数（上限 400）")
    p.add_argument("--timeout", type=int, default=HTTP_TIMEOUT, help="单请求总超时秒（默认 20）")
    p.add_argument("--no-extras", action="store_true",
                   help="只请求基础项（关掉 release/assets/reviews/platforms/coming_soon）")
    p.add_argument("--no-follow-parent", action="store_true",
                   help="不跟子 app 的父 appid（type=14 子条目将判 locked，价格会丢）")
    p.add_argument("--proxy", default=None, help="显式代理；缺省走策略引擎（同服务端 crawl）")
    p.add_argument("--direct", action="store_true", help="强制直连（不读策略引擎）")
    p.add_argument("--clash-port", type=int, default=None,
                   help="覆盖隔离库 proxy.clash_port（生产快照端口可能已停用，如 7890→7897）")
    p.add_argument("--fresh", action="store_true", help="删除隔离库重来")
    p.add_argument("--no-seed", action="store_true", help="不搬生产快照（空库跑）")
    p.add_argument("--dry-run", action="store_true", help="只抓不写库")
    p.add_argument("--debug", action="store_true", help="DEBUG 日志")
    return p


def setup_logging(debug: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(DATA_DIR / "logs" / "browse_test.log", encoding="utf-8"),
        ],
    )
    for noisy in ("aiosqlite", "sqlalchemy.engine", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if not debug:
        # 复活/清标逐游戏打 INFO，全量跑会把日志刷爆（ERROR 级仍照常输出）
        logging.getLogger("app.crawler.db_writer").setLevel(logging.WARNING)


async def resolve_proxy(args) -> tuple[str | None, object]:
    """代理解析与服务端 crawl service 同源：策略引擎优先 → 环境变量兜底。"""
    if args.direct:
        return None, None
    if args.proxy:
        return args.proxy, None

    from app.crawler.proxy import resolve_proxy_url as resolve_env_proxy

    try:
        from app.domains.proxies.service import (
            get_strategy,
            resolve_failover_proxy_url,
            resolve_proxy_url,
        )

        proxy_url = await resolve_proxy_url()
        if proxy_url is None:
            proxy_url = resolve_env_proxy()
        failover = None
        strategy = (await get_strategy())["strategy"]
        if strategy in ("direct_first", "proxy_first", "proxy_only"):

            async def _failover() -> str | None:
                return await resolve_failover_proxy_url()

            failover = _failover
        logger.info("代理策略=%s url=%s", strategy, proxy_url or "直连")
        return proxy_url, failover
    except Exception as e:  # noqa: BLE001 —— 策略不可读时回落环境变量
        logger.warning("策略引擎不可用（%s），回落环境变量代理", e)
        return resolve_env_proxy(), None


def load_target_appids(args, regions: list[str]) -> list[int]:
    if args.appids:
        return list(
            dict.fromkeys(int(a) for a in args.appids.split(",") if a.strip().isdigit())
        )
    if not BASELINE_DB.is_file():
        logger.error("基线库不存在且未指定 --appids：%s", BASELINE_DB)
        return []
    conn = sqlite3.connect(f"file:{BASELINE_DB.as_posix()}?mode=ro", uri=True)
    try:
        if args.scope == "all":
            rows = conn.execute("SELECT appid FROM games ORDER BY appid").fetchall()
        else:
            marks = ",".join("?" * len(regions))
            rows = conn.execute(
                f"SELECT DISTINCT appid FROM game_current_prices "
                f"WHERE region_code IN ({marks}) AND price_status='ok' AND price > 0 "
                f"ORDER BY appid",
                [r.upper() for r in regions],
            ).fetchall()
    finally:
        conn.close()
    appids = [int(r[0]) for r in rows]
    return appids[: args.limit] if args.limit else appids


async def run(args) -> int:
    bs.EXTRAS_ENABLED = not args.no_extras
    bs.DRY_RUN = args.dry_run
    bs.FOLLOW_PARENT = not args.no_follow_parent
    bs.DEFAULT_BATCH_SIZE = args.batch_size
    bs.reset_run_state()

    # ── 隔离库落地 ──
    await init_db()  # 建表 + 区服/汇率种子（HOLDEXAR_DATA_DIR 已指向 DATA_DIR）
    if get_settings().data_dir.resolve() != DATA_DIR.resolve():
        logger.error("隔离库未生效（%s），拒绝运行", get_settings().data_dir)
        return 2
    if not args.no_seed:
        seed_from_prod()
    if args.clash_port is not None:
        override_clash_port(args.clash_port)
    db = bs.BrowseDbWriter()
    await db.connect()
    await db.ensure_extra_columns()

    from app.domains.regions.service import effective_regions

    explicit = [r.strip().lower() for r in args.regions.split(",") if r.strip()] or None
    regions = await effective_regions(explicit)
    appids = load_target_appids(args, regions)
    if not appids:
        logger.error("没有目标 appid")
        return 1
    logger.info(
        "目标：%d 个 appid × %d 区（%s）| 隔离库 %s | 基线 %s",
        len(appids), len(regions), ",".join(regions), TEST_DB, BASELINE_DB,
    )

    proxy_url, failover = await resolve_proxy(args)

    # ── 编排全权委托生产 runner.run_crawl（与 app.crawler CLI / 服务端 crawl
    #    service 同一条链路）：任务切批、元数据与 CIS 预取、多协程 worker 池、
    #    Per-任务 Session 换出口 IP、429 熔断/退避/换代理、
    #    PRESERVED 原值装载全在对面——本壳不复刻任何一处。PRESERVED 不许
    #    在这里抢先灌：run_crawl 开头的 reset_run_state 会清空，随后由它
    #    自己从隔离库重装。 ──
    t0 = time.monotonic()
    stats = await run_crawl(
        [(int(a), "") for a in appids],
        config=CrawlRunConfig(
            regions=regions, workers=args.workers, proxy_url=proxy_url,
            timeout=args.timeout, failover_proxy_resolver=failover,
        ),
    )
    elapsed = time.monotonic() - t0

    logger.info(
        "抓取完成：初始 %d | 处理 %d | 成功 %d / 失败 %d | %.0fs",
        stats["total"], stats["processed"], stats["success"],
        stats["failed"], elapsed,
    )

    if not bs.DRY_RUN:
        print_report(build_report())
    return 0


def main() -> int:
    args = build_arg_parser().parse_args()
    setup_logging(args.debug)

    logger.info("隔离数据目录：%s", DATA_DIR)
    if args.fresh and TEST_DB.exists():
        for suffix in ("", "-wal", "-shm"):
            Path(str(TEST_DB) + suffix).unlink(missing_ok=True)
        logger.info("已清空隔离库：%s", TEST_DB)

    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        logger.warning("用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
