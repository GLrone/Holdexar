# -*- coding: utf-8 -*-
"""IStoreBrowseService 捆绑包抓取验证探针（只读，不写库）。

验证 IStoreBrowseService/GetItems/v1（批量 ≤400 × 单区）能否直查捆绑包、
信息是否比现行 bundles/refresh.py 通道（逐区 ajaxresolvebundles /
packagedetails，每包每区一发）更全。

三种用法：
  1. 单点探测（默认，胡闹厨房两个包，覆盖真 Bundle 与 bundle-as-sub 两种身份）：
       .venv/Scripts/python.exe scripts/store_browse_bundle_probe.py
  2. 全表回归：库内全部捆绑包合一发 × 各区，与生产 bundle_region_prices 对账：
       .venv/Scripts/python.exe scripts/store_browse_bundle_probe.py --all-bundles --compare
  3. 指定 id / 区服 / 直连：
       ... --ids bundle:13608,package:1066582 --regions us,cn --direct

只读产物：data/browse_test/bundle_probe_<ts>.json（原始响应，供人工核对）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import urllib.parse
from datetime import datetime
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SERVER_ROOT.parent
OUT_DIR = PROJECT_ROOT / "data" / "browse_test"
PROD_DB = PROJECT_ROOT / "data" / "holdexar.db"

sys.path.insert(0, str(SERVER_ROOT))

import aiohttp  # noqa: E402
from yarl import URL  # noqa: E402

from app.crawler import browse_store as bs  # noqa: E402
from app.crawler.config import CC_LIST  # noqa: E402
from app.crawler.http_client import SteamHttpClient  # noqa: E402

OLD_BUNDLE_URL = "https://store.steampowered.com/actions/ajaxresolvebundles"
OLD_PACKAGE_URL = "https://store.steampowered.com/api/packagedetails"

DATA_REQUEST = {**bs.DATA_REQUEST_BASE, **bs.DATA_REQUEST_EXTRAS}

# 默认探针目标：胡闹厨房（Overcooked! 2, appid 728880）的两个捆绑包，恰好覆盖
# 两种身份——真 Bundle（Complete the Set，可补齐）与 bundle-as-sub（Gourmet
# Edition，必须整包）。库内 must_purchase_as_set: 13608=0 / 1066582=1。
DEFAULT_IDS = "bundle:13608,package:1066582,app:728880"
DEFAULT_REGIONS = "us,cn"


class _Ctx:
    """复用生产 http_client 所需的最小 context。"""

    def __init__(self, http_client, session):
        self.http_client = http_client
        self.session = session


# ══════════════════════════════════════════════════════════════
# id / 区服装载
# ══════════════════════════════════════════════════════════════


def parse_id_specs(text: str) -> list[tuple[str, int]]:
    """'bundle:13608,package:1066582,728880' → [('bundleid',13608), ...]。

    裸数字默认 appid；kind 直接当 ids 数组里的键名（appid/bundleid/packageid）。
    """
    out: list[tuple[str, int]] = []
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        kind, sep, value = part.partition(":")
        if not sep:
            out.append(("appid", int(kind)))
            continue
        key = kind.strip().lower()
        key = key if key.endswith("id") else f"{key}id"
        out.append((key, int(value)))
    return out


def load_bundle_ids_from_db() -> list[tuple[str, int]]:
    """生产库 bundles → id 规格。must_purchase_as_set=1（Sub）用 packageid，
    其余用 bundleid（数字空间 bundle/sub 并存，库内身份是权威判据）。"""
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT bundle_id, must_purchase_as_set FROM bundles ORDER BY bundle_id"
        ).fetchall()
    finally:
        conn.close()
    specs: list[tuple[str, int]] = []
    for bid, mps in rows:
        specs.append(("packageid" if mps == 1 else "bundleid", int(bid)))
    return specs


def load_baseline() -> dict[int, dict[str, dict]]:
    """生产 bundle_region_prices → {bundle_id: {小写区: 行}}（同区取最新一条）。"""
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT bundle_id, region_code, currency, price, original_price, "
            "discount_percent, bundle_base_discount, price_status, app_ids, crawled_at "
            "FROM bundle_region_prices"
        ).fetchall()
    finally:
        conn.close()
    out: dict[int, dict[str, dict]] = {}
    for (bid, cc, cur, price, orig, disc, base_disc, status, app_ids, crawled) in rows:
        key = str(cc).lower()
        per = out.setdefault(int(bid), {})
        old = per.get(key)
        if old is None or (crawled or "") > (old.get("crawled_at") or ""):
            per[key] = {
                "currency": cur, "price": price, "original": orig,
                "discount": disc, "base_discount": base_disc, "status": status,
                "app_ids": json.loads(app_ids or "[]"), "crawled_at": crawled,
            }
    return out


# ══════════════════════════════════════════════════════════════
# 新接口
# ══════════════════════════════════════════════════════════════


def build_browse_url(id_specs: list[dict], cc: str, lang: str) -> URL:
    """与 browse_store.StoreBrowseAPI.build_url 同款编码（encoded=True 不可退）。"""
    body = {
        "ids": id_specs,
        "context": {"language": lang, "country_code": cc.upper(), "steam_realm": 1},
        "data_request": DATA_REQUEST,
    }
    qs = urllib.parse.urlencode(
        {"input_json": json.dumps(body, separators=(",", ":"))}, safe=bs._URL_SAFE
    )
    return URL(f"{bs.BROWSE_URL}?{qs}", encoded=True)


def extract_price(item: dict | None) -> dict:
    """条目 → 价格/身份摘要（bundle 轨与 sub 轨字段名不同，统一出口）。"""
    if not item:
        return {"present": False}
    opts = item.get("purchase_options") or []
    opt = item.get("best_purchase_option") or (opts[0] if opts else {}) or {}
    final = opt.get("final_price_in_cents")
    original = opt.get("original_price_in_cents") or opt.get("price_before_bundle_discount")
    try:
        price_i = int(final) if final not in (None, "") else None
    except (TypeError, ValueError):
        price_i = None
    try:
        original_i = int(original) if original not in (None, "") else None
    except (TypeError, ValueError):
        original_i = None
    return {
        "present": True,
        "success": item.get("success"),
        "visible": item.get("visible"),
        "item_type": item.get("item_type"),          # 1=sub 2=bundle
        "type": item.get("type"),
        "id": item.get("id"),
        "name": item.get("name") or (item.get("basic_info") or {}).get("name"),
        "store_url_path": item.get("store_url_path"),
        "included_appids": item.get("included_appids") or [],
        "included_types": item.get("included_types") or [],
        "must_purchase_as_set": opt.get("must_purchase_as_set"),
        "bundle_discount_pct": opt.get("bundle_discount_pct"),
        "included_game_count": opt.get("included_game_count"),
        "package_group": opt.get("package_group"),
        "price": price_i,
        "original": original_i,
        "discount_pct": opt.get("discount_pct"),
        "formatted_final": opt.get("formatted_final_price"),
        "price_before_bundle_discount": opt.get("price_before_bundle_discount"),
        "locked_cr": item.get("unvailable_for_country_restriction"),
        "keys": sorted(item.keys()),
    }


async def fetch_region(ctx, cc: str, specs: list[tuple[str, int]], lang: str) -> dict:
    """单区一发（≤400 条）。返回 {ident: 摘要}，位置映射（服务端可能回 id=0）。"""
    ids_json = [{k: v} for k, v in specs]
    url = build_browse_url(ids_json, cc, lang)
    data = await ctx.http_client.get_json(ctx.session, url)
    items = ((data or {}).get("response") or {}).get("store_items") or []
    out: dict[int, dict] = {}
    if len(items) == len(specs):
        for (_kind, ident), it in zip(specs, items):
            out[int(ident)] = extract_price(it)
    else:  # 位置错位兜底：按 id 映射
        for it in items:
            if it.get("id"):
                out[int(it["id"])] = extract_price(it)
    for _kind, ident in specs:
        out.setdefault(int(ident), {"present": False})
    return {"items": out, "n_items": len(items), "url_len": len(str(url)), "raw": data}


# ══════════════════════════════════════════════════════════════
# 旧接口对照（仅单点模式用；全表模式对照走生产库基线）
# ══════════════════════════════════════════════════════════════


async def probe_old(ctx, cc: str, specs: list[tuple[str, int]], raw_out: dict):
    for kind, ident in specs:
        if kind == "appid":
            continue
        if kind == "bundleid":
            url = URL(
                f"{OLD_BUNDLE_URL}?bundleids={ident}&cc={cc}"
                f"&l={'schinese' if cc == 'cn' else 'english'}"
            )
            label = "旧-ajaxresolvebundles"
        else:
            url = URL(f"{OLD_PACKAGE_URL}?packageids={ident}&cc={cc}")
            label = "旧-packagedetails"
        try:
            data = await ctx.http_client.get_json(ctx.session, url)
        except Exception as e:  # noqa: BLE001
            raw_out.setdefault(label, {})[f"{cc}:{ident}"] = {"error": str(e)}
            continue
        raw_out.setdefault(label, {})[f"{cc}:{ident}"] = data


# ══════════════════════════════════════════════════════════════
# 单点探测输出
# ══════════════════════════════════════════════════════════════


async def single_probe(ctx, args, specs, regions, raw_out) -> None:
    for cc in regions:
        res = await fetch_region(ctx, cc, specs, args.lang)
        raw_out.setdefault("new", {})[cc] = res["raw"]
        print(
            f"\n{'=' * 78}\n[新接口] cc={cc}  请求 {len(specs)} 条 → 返回 "
            f"{res['n_items']} 条 | URL {res['url_len']} 字节"
        )
        for kind, ident in specs:
            s = res["items"].get(int(ident)) or {}
            print(f"\n  ── {kind}={ident} ──")
            if not s.get("present"):
                print("    缺失")
                continue
            print(
                f"    success={s['success']} visible={s['visible']} "
                f"item_type={s['item_type']}(1=sub/2=bundle) type={s['type']} id={s['id']}"
            )
            print(f"    name={s['name']!r}")
            print(f"    store_url_path={s['store_url_path']!r}")
            print(f"    价格 final={s['price']} fmt={s['formatted_final']!r} "
                  f"原价={s['original']} 折={s['discount_pct']} 叠折={s['bundle_discount_pct']}")
            print(f"    must_purchase_as_set={s['must_purchase_as_set']} "
                  f"included_game_count={s['included_game_count']} 组={s['package_group']}")
            print(f"    included_appids={s['included_appids']} types={s['included_types']}")
            print(f"    顶层键({len(s['keys'])}): {s['keys']}")
        if not args.no_old:
            await probe_old(ctx, cc, specs, raw_out)
            for kind, ident in specs:
                if kind == "bundleid":
                    d = raw_out.get("旧-ajaxresolvebundles", {}).get(f"{cc}:{ident}")
                    if isinstance(d, list) and d:
                        k = sorted(d[0].keys())
                        print(f"\n[旧-ajaxresolvebundles] cc={cc} bundle={ident} 字段{len(k)}: {k}")
                elif kind == "packageid":
                    d = raw_out.get("旧-packagedetails", {}).get(f"{cc}:{ident}")
                    if isinstance(d, dict):
                        inner = (d.get(str(ident)) or {}).get("data") or {}
                        print(f"[旧-packagedetails] cc={cc} sub={ident} 字段"
                              f"{len(inner)}: {sorted(inner.keys())}")


# ══════════════════════════════════════════════════════════════
# 全表回归 + 对账输出
# ══════════════════════════════════════════════════════════════


async def regression(ctx, args, specs, regions, raw_out, baseline) -> None:
    sem = asyncio.Semaphore(args.concurrency)

    async def one(cc: str):
        async with sem:
            try:
                return cc, await fetch_region(ctx, cc, specs, args.lang), None
            except Exception as e:  # noqa: BLE001
                return cc, None, f"{type(e).__name__}: {e}"

    results = await asyncio.gather(*(one(cc) for cc in regions))
    raw_out.setdefault("new", {}).update(
        {cc: res["raw"] for cc, res, err in results if res is not None}
    )

    n_req = len(regions)
    print(f"\n{'=' * 78}\n[全表回归] {len(specs)} 个包 × {len(regions)} 区 = {n_req} 发请求"
          f"（旧链路等价 {len(specs) * len(regions)} 发）")
    for cc, res, err in results:
        if err:
            print(f"  {cc}: 请求失败 {err}")
            continue
        print(f"  {cc}: 返回 {res['n_items']}/{len(specs)} 条 | URL {res['url_len']} 字节")

    # ── 身份核对（item_type 2=bundle 1=sub ↔ 库内 must_purchase_as_set）──
    by_id: dict[int, list[tuple[str, dict]]] = {}
    for cc, res, err in results:
        if err:
            continue
        for ident, s in res["items"].items():
            by_id.setdefault(ident, []).append((cc, s))

    mps_of = {}
    conn = sqlite3.connect(f"file:{PROD_DB.as_posix()}?mode=ro", uri=True)
    try:
        mps_of = dict(conn.execute("SELECT bundle_id, must_purchase_as_set FROM bundles"))
    finally:
        conn.close()

    type_mismatch, not_found = [], []
    for ident, per in sorted(by_id.items()):
        sample = next((s for _cc, s in per if s.get("present")), None)
        if sample is None:
            not_found.append(ident)
            continue
        it = sample.get("item_type")
        db_mps = mps_of.get(ident)
        expect = "sub" if db_mps == 1 else "bundle"
        got = "sub" if it == 1 else ("bundle" if it == 2 else f"type={it}")
        if got != expect:
            type_mismatch.append((ident, db_mps, it, sample.get("store_url_path")))
    print(f"\n[身份] item_type 与库内 must_purchase_as_set 一致 "
          f"{len(by_id) - len(type_mismatch) - len(not_found)}/{len(by_id)}；"
          f"不一致 {len(type_mismatch)}；全部区不可见 {len(not_found)}")
    for ident, db_mps, it, path in type_mismatch[:10]:
        print(f"    包 {ident}: 库 mps={db_mps} 但接口 item_type={it} path={path}")

    # ── 逐区价格对账 ──
    stats = {k: 0 for k in ("cmp", "eq", "ne", "base_missing", "new_missing",
                            "disc_eq", "orig_new", "orig_base")}
    samples: list[str] = []
    for cc, res, err in results:
        if err:
            continue
        for ident, s in sorted(res["items"].items()):
            base = (baseline.get(ident) or {}).get(cc)
            if not s.get("present") or s.get("price") is None:
                if base and base.get("status") == "ok" and base.get("price"):
                    stats["new_missing"] += 1
                continue
            if not base or base.get("status") != "ok" or not base.get("price"):
                stats["base_missing"] += 1
                continue
            stats["cmp"] += 1
            if int(base["price"]) == int(s["price"]):
                stats["eq"] += 1
            else:
                stats["ne"] += 1
                if len(samples) < 15:
                    samples.append(
                        f"    包 {ident} {cc}: 基线 {base['price']} vs 新 {s['price']} "
                        f"（{base['currency']} 折{base['discount']} vs item_type={s['item_type']} "
                        f"折{s['discount_pct']}）"
                    )
            if (base.get("discount") or 0) == (s.get("discount_pct") or 0):
                stats["disc_eq"] += 1
            if s.get("original"):
                stats["orig_new"] += 1
            if base.get("original"):
                stats["orig_base"] += 1

    print(f"\n[价格对账] 可比 {stats['cmp']} | 一致 {stats['eq']} "
          f"({100 * stats['eq'] / max(stats['cmp'], 1):.1f}%) | 不一致 {stats['ne']}")
    print(f"  折扣一致 {stats['disc_eq']} | 基线缺价 {stats['base_missing']} | "
          f"新接口无价(基线有) {stats['new_missing']}")
    print(f"  原价字段：新接口有 {stats['orig_new']} / 基线有 {stats['orig_base']}")
    if samples:
        print("  不一致样例：")
        print("\n".join(samples))
    raw_out["regression"] = {
        "n_ids": len(specs), "n_regions": len(regions), **stats,
        "type_mismatch": type_mismatch, "not_found": not_found,
        "price_mismatch_samples": samples,
    }


# ══════════════════════════════════════════════════════════════
# 同时刻 A/B：新接口 vs 旧接口（消除基线时间漂移）
# ══════════════════════════════════════════════════════════════

# 旧链路取价的两处约定（与迁移前的 bundles/refresh.py 逐字一致）：
# 无小数货币按除数 1 解析格式化字符串（Bundle 轨落「元」），
# Sub 轨则直取 packagedetails 的 price.final（落「分」）——同列两套单位。
_OLD_MINOR_DIVISOR = {
    "IDR": 1, "JPY": 1, "KRW": 1, "VND": 1, "CLP": 1, "COP": 1, "PYG": 1, "HUF": 1,
}
_OLD_CC_CURRENCY = {cc: cur for cc, _, cur in CC_LIST}


def _parse_old_formatted_price(text: str, cc: str) -> tuple[int | None, str | None]:
    """旧 Bundle 轨的 formatted_final_price（"¥ 132.30" / "NT$ 1,180"）解析。"""
    if not text:
        return None, None
    cur = _OLD_CC_CURRENCY.get(cc)
    m = re.search(r"\d[\d.,\s]*", text)
    if not m:
        return None, cur
    num_raw = m.group(0).replace(" ", "")
    if re.search(r",\d{2}$", num_raw) and "." not in num_raw:
        num = num_raw.replace(",", ".")
    else:
        num = num_raw.replace(",", "")
    divisor = _OLD_MINOR_DIVISOR.get(cur, 100)
    try:
        if divisor == 1:
            value = int(num.replace(".", "").replace(",", ""))
        elif "." in num:
            value = round(float(num) * 100)
        else:
            value = int(num) * 100
    except ValueError:
        return None, cur
    return value, cur


async def _old_price(ctx, cc: str, kind: str, ident: int) -> dict | None:
    """旧链路单包单区取价（A/B 基线，独立复刻旧抓取层，不依赖生产代码）。

    Bundle 轨：ajaxresolvebundles 的整数 final_price **恒为 0**，只能用
    formatted_final_price 字符串解析（旧 refresh 的 _parse_formatted_price）；
    原价取 initial_price。Sub 轨：packagedetails 的 price.final/initial。
    """
    if kind == "bundleid":
        url = URL(
            f"{OLD_BUNDLE_URL}?bundleids={ident}&cc={cc}"
            f"&l={'schinese' if cc == 'cn' else 'english'}"
        )
        try:
            data = await ctx.http_client.get_json(ctx.session, url)
        except Exception as e:  # noqa: BLE001
            return {"error": f"{type(e).__name__}: {e}"}
        if not (isinstance(data, list) and data):
            return None
        it = data[0]
        price, _cur = _parse_old_formatted_price(it.get("formatted_final_price", ""), cc)
        orig, _ = _parse_old_formatted_price(it.get("formatted_orig_price", ""), cc)
        return {
            "price": price,
            "original": it.get("initial_price") or orig,
            "discount": it.get("discount_percent") or 0,
            "base_discount": it.get("bundle_base_discount") or 0,
            "appids": it.get("appids") or [],
        }
    url = URL(f"{OLD_PACKAGE_URL}?packageids={ident}&cc={cc}")
    try:
        data = await ctx.http_client.get_json(ctx.session, url)
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}"}
    inner = ((data or {}).get(str(ident)) or {}).get("data") or {}
    if not inner:
        return None
    p = inner.get("price") or {}
    return {
        "price": p.get("final"),
        "original": p.get("initial"),
        "discount": p.get("discount_percent") or 0,
        "base_discount": 0,
        "appids": [a.get("id") for a in (inner.get("apps") or [])],
    }


async def ab_mode(ctx, args, specs, regions, raw_out) -> None:
    """同区同时刻对新旧接口各打一发，逐区逐包比价（新=1 发/区，旧=1 发/包/区）。"""
    specs = [(k, v) for k, v in specs if k in ("bundleid", "packageid")]
    sem = asyncio.Semaphore(args.concurrency)

    async def one(cc: str):
        async with sem:
            try:
                new = await fetch_region(ctx, cc, specs, args.lang)
            except Exception as e:  # noqa: BLE001
                new = {"items": {}, "error": f"{type(e).__name__}: {e}"}
            old = {}
            for kind, ident in specs:
                old[(kind, ident)] = await _old_price(ctx, cc, kind, ident)
            return cc, new, old

    results = await asyncio.gather(*(one(cc) for cc in regions))
    raw_out["ab"] = {
        cc: {"new": new.get("raw"), "old": {f"{k[0]}:{k[1]}": v for k, v in old.items()}}
        for cc, new, old in results
    }

    n_old = len(specs) * len(regions)
    print(f"\n{'=' * 78}\n[同时刻 A/B] {len(specs)} 包 × {len(regions)} 区：新接口 "
          f"{len(regions)} 发 vs 旧接口 {n_old} 发")
    print(f"{'区':<4} {'目标':<18} {'新final':>9} {'旧final':>9} {'一致':>4} "
          f"{'新原价':>9} {'旧原价':>9} {'新折':>5} {'旧折':>5}")
    agg: dict[str, dict] = {}
    for cc, new, old in results:
        for kind, ident in specs:
            s = (new.get("items") or {}).get(int(ident)) or {}
            o = old.get((kind, ident))
            a = agg.setdefault(
                f"{kind}:{ident}",
                {"cmp": 0, "eq": 0, "ne": 0, "err": 0, "new_only": 0, "old_only": 0},
            )
            np_, op = s.get("price"), (o or {}).get("price")
            if not s.get("present") or np_ is None:
                if op is not None:
                    a["old_only"] += 1
                continue
            if o is None or op is None:
                a["new_only"] += 1
                continue
            if "error" in (o or {}):
                a["err"] += 1
                continue
            a["cmp"] += 1
            same = int(np_) == int(op)
            a["eq" if same else "ne"] += 1
            if not same or cc in ("us", "cn", "ru", "jp"):
                print(f"{cc:<4} {kind + ':' + str(ident):<18} {np_:>9} {op:>9} "
                      f"{'一致' if same else '差异':>4} {str(s.get('original')):>9} "
                      f"{str((o or {}).get('original')):>9} {str(s.get('discount_pct')):>5} "
                      f"{str((o or {}).get('discount')):>5}")
    print("\n[汇总]")
    for key, a in agg.items():
        rate = 100 * a["eq"] / max(a["cmp"], 1)
        print(f"  {key:<18} 可比 {a['cmp']:>4} | 一致 {a['eq']:>4} ({rate:.1f}%) | "
              f"差异 {a['ne']} | 新有旧无 {a['new_only']} | 旧有新无 {a['old_only']} | "
              f"旧请求失败 {a['err']}")
    raw_out["ab_summary"] = agg


# ══════════════════════════════════════════════════════════════
# 入口
# ══════════════════════════════════════════════════════════════


async def main_async(args) -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.all_bundles:
        specs = load_bundle_ids_from_db()
        regions = [cc for cc, _, _ in CC_LIST] if args.all_regions else [
            r.strip().lower() for r in args.regions.split(",") if r.strip()
        ]
    else:
        specs = parse_id_specs(args.ids)
        regions = [cc for cc, _, _ in CC_LIST] if args.all_regions else [
            r.strip().lower() for r in args.regions.split(",") if r.strip()
        ]
    if not specs:
        print("没有目标 id")
        return 1

    proxy = None if args.direct else args.proxy
    baseline = load_baseline() if args.compare else {}
    http_client = SteamHttpClient(timeout=args.timeout, max_retries=3, proxy_url=proxy)
    connector = aiohttp.TCPConnector(limit=max(args.concurrency, 2), ttl_dns_cache=60)
    raw_out: dict = {
        "probe_at": datetime.now().isoformat(timespec="seconds"),
        "ids": specs, "regions": regions, "proxy": proxy or "direct",
        "data_request": DATA_REQUEST, "mode": "all-bundles" if args.all_bundles else "single",
    }
    async with aiohttp.ClientSession(connector=connector) as session:
        ctx = _Ctx(http_client, session)
        if args.ab:
            await ab_mode(ctx, args, specs, regions, raw_out)
        elif args.all_bundles:
            await regression(ctx, args, specs, regions, raw_out, baseline)
        else:
            await single_probe(ctx, args, specs, regions, raw_out)

    out = OUT_DIR / f"bundle_probe_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps(raw_out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始响应已写出：{out}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        prog="store_browse_bundle_probe",
        description="IStoreBrowseService 捆绑包抓取验证探针（只读）",
    )
    p.add_argument("--ids", default=DEFAULT_IDS,
                   help="逗号分隔 id，kind:id（bundle/package/app），裸数字=appid")
    p.add_argument("--regions", default=DEFAULT_REGIONS, help="逗号分隔区服 cc")
    p.add_argument("--all-regions", action="store_true", help="用 CC_LIST 全区服")
    p.add_argument("--all-bundles", action="store_true",
                   help="目标 = 生产库全部 bundle_id（按 must_purchase_as_set 定身份）")
    p.add_argument("--compare", action="store_true", help="与生产 bundle_region_prices 对账")
    p.add_argument("--ab", action="store_true",
                   help="同时刻新旧接口 A/B 比价（只取 bundle/package 类 id）")
    p.add_argument("--concurrency", type=int, default=6, help="区并发（默认 6）")
    p.add_argument("--lang", default="english", help="语言")
    p.add_argument("--timeout", type=int, default=20, help="单请求超时秒")
    p.add_argument("--proxy", default="http://127.0.0.1:7897", help="代理（--direct 时忽略）")
    p.add_argument("--direct", action="store_true", help="强制直连")
    p.add_argument("--no-old", action="store_true", help="单点模式不打旧接口")
    args = p.parse_args()
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
