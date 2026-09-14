"""外部元数据域：Epic 喜加一 / XGP 收录 / HB 慈善包。

数据源格式：
- Epic：`epic_free_games_clean_final.json`（{"applist":{"apps":[{appid, free_period}]}}）
  → is_epic=true, epic_date=free_period
- XGP：`xgp_tiered_steam.json`（{"PC Collection"/"Console Collection": {档位: [{name, appid?}]}}）
  → xgp_tier=档位名，PC Collection 优先；缺 appid 的条目用 `xgp_steam_links.json`
  做名称→AppID 匹配（exact → 清洗 → 罗马数字互换三级匹配）
- HB：`steam_historical_lows.db` game_lows.humble_choice（"Humble Choice (Feb 2022)"）
  → is_hb=true, hb_data="HB慈善包22年2月包"
- HB 当月包（refresh_hb_choice）：membership 页公开 JSON → 游戏侧标记，
  条目 appid 由 storesearch 按名解析；无登录态拿不到往期页，当月错过即漏，
  由每日调度幂等补位（历史月份走上方静态库导入或人工补）。
- Epic 自动链（refresh_epic_free）：促销端点（当期+未来两周窗口）→
  storesearch 匹配 → 游戏侧标记；未匹配条目记 unresolved 不阻塞。
- Epic 外部名单（import_epic_list）：浏览器侧脚本推送的 appid/日期对，
  日期变体归一为 YYYY-M-D，与自动链同标记语义。
- Barter.vg（refresh_bundle_counts）：第三方渠道 bundle 计数全量档案
  （{"appid": {"bundles": n, "bundles_packages": m}}，键为 appid 字符串，
  计数为站点预聚合值）→ games.bundle_count；只取 bundles 字段，只更新
  库内已存在的行（不落占位），差量写入保证幂等。

写库一律 UPDATE 已存在的 games 行（不创建新行）；重复导入幂等。
当月包链与 Epic 链例外：新 appid 允许落占位行（updated_at NULL → 回补池消化）。
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from sqlalchemy import case, func, select, update

from app.core.database import get_session_factory
from app.crawler.epic_free import (
    fetch_free_games,
    fetch_free_offers,
    fetch_mobile_breaker,
    fetch_mobile_freebie,
)
from app.domains.games.models import Game

logger = logging.getLogger(__name__)

# 外部数据目录（外部数据管道的落盘文件所在；个人盘符布局不进 git，
# 本机用环境变量 METADATA_DATA_DIR 覆盖）
DEFAULT_SOURCE_DIR = Path(os.environ.get("METADATA_DATA_DIR", ""))

_EPIC_FILE = "epic_free_games_clean_final.json"
_XGP_TIERED_FILE = "xgp_tiered_steam.json"
_XGP_LINKS_FILE = "xgp_steam_links.json"
_HB_DB_FILE = "steam_historical_lows.db"

_MONTH_MAP = {
    "Jan": "1", "Feb": "2", "Mar": "3", "Apr": "4",
    "May": "5", "Jun": "6", "Jul": "7", "Aug": "8",
    "Sep": "9", "Oct": "10", "Nov": "11", "Dec": "12",
}

_HB_RE = re.compile(r"Humble Choice\s*\(([A-Za-z]{3})\s*(\d{4})\)", re.IGNORECASE)


def resolve_source_path(source_dir: str | None, filename: str) -> Path:
    base = Path(source_dir) if source_dir else DEFAULT_SOURCE_DIR
    return base / filename


# ─── XGP 名称匹配（名称清洗 + 三级查找）───────────────

_TRADEMARK_RE = re.compile(r"\s*\-\s*pc edition|\s*\(pc\)|\s*\(windows\)|\s*windows edition|\s*xbox one|\s*xbox series x\|s")
_EDITION_RE = re.compile(
    r"\s*standard edition|\s*game of the year edition|\s*anniversary edition|\s*complete edition"
    r"|\s*deluxe edition|\s*definitive edition|\s*remastered|\s*director's cut|\s*ultimate edition",
    re.IGNORECASE,
)


def _clean_name(n: str) -> str:
    if not n:
        return ""
    n = n.lower()
    n = n.replace("™", "").replace("®", "").replace("©", "")
    n = n.replace("’", "'").replace("–", "-").replace("—", "-")
    n = _TRADEMARK_RE.sub("", n)
    n = _EDITION_RE.sub("", n)
    n = n.replace("ea sports fc", "fc").replace("ea sports ", "")
    n = re.sub(r"[^\w\s]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _build_links_map(links: list[dict]) -> dict[str, dict[str, str]]:
    """name→appid 三级查找：exact / clean / 罗马数字互换。"""
    m: dict[str, dict[str, str]] = {"exact": {}, "clean": {}, "alt": {}}
    for item in links:
        name = item.get("game_name", "")
        appid = str(item.get("steam_appid", ""))
        if not name or not appid:
            continue
        cleaned = _clean_name(name)
        alt1 = cleaned.replace(" 2", " ii").replace(" 3", " iii").replace(" 4", " iv")
        alt2 = cleaned.replace(" ii", " 2").replace(" iii", " 3").replace(" iv", " 4")
        m["exact"][name.lower().strip()] = appid
        m["clean"][cleaned] = appid
        m["alt"][alt1] = appid
        m["alt"][alt2] = appid
    return m


def _match_appid(name: str, links_map: dict[str, dict[str, str]]) -> str | None:
    exact = name.lower().strip()
    cleaned = _clean_name(name)
    if exact in links_map["exact"]:
        return links_map["exact"][exact]
    if cleaned in links_map["clean"]:
        return links_map["clean"][cleaned]
    if cleaned in links_map["alt"]:
        return links_map["alt"][cleaned]
    return None


def _collect_xgp_tiers(data: dict) -> dict[int, str]:
    """扁平化 tiered JSON；PC Collection 优先（对齐 import_metadata.py）。"""
    xgp_map: dict[int, str] = {}
    for collection_type, tiers in data.items():
        for tier_name, games in tiers.items():
            if not isinstance(games, list):
                continue
            for g in games:
                aid_str = str(g.get("appid", "") or "")
                if aid_str.isdigit():
                    aid = int(aid_str)
                    if aid not in xgp_map or collection_type == "PC Collection":
                        xgp_map[aid] = tier_name
    return xgp_map


def _collect_xgp_with_links(data: dict, links_map: dict[str, dict[str, str]]) -> dict[int, str]:
    """tiered 数据缺 appid 的条目走名称匹配；匹配到的 (tier, name) 先记档。"""
    xgp_map: dict[int, str] = {}
    matched = 0
    for collection_type, tiers in data.items():
        for tier_name, games in tiers.items():
            if not isinstance(games, list):
                continue
            for g in games:
                aid_str = str(g.get("appid", "") or "")
                if aid_str.isdigit():
                    aid = int(aid_str)
                else:
                    appid_str = _match_appid(g.get("name", ""), links_map)
                    if appid_str is None or not appid_str.isdigit():
                        continue
                    aid = int(appid_str)
                    matched += 1
                if aid not in xgp_map or collection_type == "PC Collection":
                    xgp_map[aid] = tier_name
    return xgp_map, matched


# ─── 三个导入器 ─────────────────────────────────────────────────

def _fmt_hb_data(raw: str) -> str:
    m = _HB_RE.search(raw or "")
    if m:
        month_num = _MONTH_MAP.get(m.group(1).capitalize()[:3], "1")
        year_short = m.group(2)[-2:]
        return f"HB慈善包{year_short}年{month_num}月包"
    return "HB慈善包"


async def import_epic(source_dir: str | None = None) -> dict:
    path = resolve_source_path(source_dir, _EPIC_FILE)
    if not path.exists():
        return {"source": "epic", "ok": False, "error": f"找不到数据文件: {path}"}
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    epic_map: dict[int, str | None] = {}
    for a in data.get("applist", {}).get("apps", []):
        appid_str = str(a.get("appid", ""))
        if appid_str.isdigit():
            epic_map[int(appid_str)] = a.get("free_period") or None

    updated = 0
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Game.appid).where(Game.appid.in_(list(epic_map)))
            )
        ).all()
        existing_ids = [r[0] for r in rows]
        if existing_ids:
            values = {aid: epic_map[aid] for aid in existing_ids}
            await session.execute(
                update(Game)
                .where(Game.appid.in_(existing_ids))
                .values(is_epic=True, epic_date=case(values, value=Game.appid))
            )
            updated = len(existing_ids)
        await session.commit()
    return {"source": "epic", "ok": True, "file": str(path), "inSource": len(epic_map), "updated": updated}


async def import_xgp(source_dir: str | None = None) -> dict:
    tiered_path = resolve_source_path(source_dir, _XGP_TIERED_FILE)
    if not tiered_path.exists():
        return {"source": "xgp", "ok": False, "error": f"找不到数据文件: {tiered_path}"}
    with open(tiered_path, encoding="utf-8") as f:
        data = json.load(f)

    links_path = resolve_source_path(source_dir, _XGP_LINKS_FILE)
    name_matched = 0
    if links_path.exists():
        with open(links_path, encoding="utf-8") as f:
            links_map = _build_links_map(json.load(f))
        xgp_map, name_matched = _collect_xgp_with_links(data, links_map)
    else:
        xgp_map = _collect_xgp_tiers(data)

    updated = 0
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(Game.appid).where(Game.appid.in_(list(xgp_map))))
        ).all()
        existing_ids = [r[0] for r in rows]
        if existing_ids:
            values = {aid: xgp_map[aid] for aid in existing_ids}
            await session.execute(
                update(Game)
                .where(Game.appid.in_(existing_ids))
                .values(xgp_tier=case(values, value=Game.appid))
            )
            updated = len(existing_ids)
        await session.commit()
    return {
        "source": "xgp", "ok": True, "file": str(tiered_path),
        "inSource": len(xgp_map), "updated": updated, "nameMatched": name_matched,
    }


async def import_hb(source_dir: str | None = None) -> dict:
    path = resolve_source_path(source_dir, _HB_DB_FILE)
    if not path.exists():
        return {"source": "hb", "ok": False, "error": f"找不到数据文件: {path}"}

    hl = sqlite3.connect(str(path))
    try:
        columns = [col[1] for col in hl.execute("PRAGMA table_info(game_lows)").fetchall()]
        target_col = "humble_choice" if "humble_choice" in columns else None
        if target_col is None:
            return {"source": "hb", "ok": False, "error": "game_lows 无 humble_choice 列"}
        hb_map: dict[int, str] = {}
        for appid, val in hl.execute(
            f"SELECT appid, {target_col} FROM game_lows WHERE {target_col} LIKE '%Humble Choice%'"
        ):
            try:
                hb_map[int(appid)] = _fmt_hb_data(val or "")
            except (TypeError, ValueError):
                continue
    finally:
        hl.close()

    updated = 0
    async with get_session_factory()() as session:
        rows = (
            await session.execute(select(Game.appid).where(Game.appid.in_(list(hb_map))))
        ).all()
        existing_ids = [r[0] for r in rows]
        if existing_ids:
            values = {aid: hb_map[aid] for aid in existing_ids}
            await session.execute(
                update(Game)
                .where(Game.appid.in_(existing_ids))
                .values(is_hb=True, hb_data=case(values, value=Game.appid))
            )
            updated = len(existing_ids)
        await session.commit()
    return {"source": "hb", "ok": True, "file": str(path), "inSource": len(hb_map), "updated": updated}


# ─── HB 当月包：membership 页 → 游戏侧标记（无账本表）─────────────
# 为什么不做月包表：月包的持久语义只有「哪些游戏进过哪个月的包」，
# 落 games.is_hb/hb_data 即完备；整包存档属易失数据（往期页对未登录
# 404），表化反而诱导依赖一份注定不全的存档。

_MEMBERSHIP_URL = "https://www.humblebundle.com/membership"
_STORESEARCH_URL = "https://store.steampowered.com/api/storesearch"
_HB_STATE_KEY = "hb_choice_last_month"
_CHOICE_TIMEOUT = 25

_ITEM_MARK_RE = re.compile(r'"([a-z0-9_]+)": \{"recommendation_copy_dict"')


def _choice_month_label(machine_name: str | None, product_name: str | None) -> str:
    """September 2026 Humble Choice / september_2026_choice → HB慈善包26年9月包。

    与 import_hb 的 _fmt_hb_data 同一格式约定；两路都解析不出月份时退回
    无月后缀的通用标记（宁错标月份形态不错过打标）。
    """
    m = re.search(r"([A-Za-z]+)\s+(\d{4})", product_name or "")
    if m and m.group(1).capitalize()[:3] in _MONTH_MAP:
        return f"HB慈善包{m.group(2)[-2:]}年{_MONTH_MAP[m.group(1).capitalize()[:3]]}月包"
    m = re.search(r"([a-z]+)_(\d{4})", machine_name or "")
    if m and m.group(1).capitalize()[:3] in _MONTH_MAP:
        return f"HB慈善包{m.group(2)[-2:]}年{_MONTH_MAP[m.group(1).capitalize()[:3]]}月包"
    return "HB慈善包"


def _parse_choice_page(html_text: str) -> dict:
    """membership 页原文 → {machineName, productName, endDate, items}。

    条目以内嵌转义 JSON 形态存在（引号是 &#34;），页面按区块（货币/地区
    轮播）重复同一份清单，按 machineName 去重取首见。recommendation_copy_dict
    是条目对象的稳定首键，以此为锚做 JSONDecoder.raw_decode（条目正文含
    花括号，手工数括号不可靠）。
    """
    page = html.unescape(html_text)

    def _field(name: str) -> str | None:
        m = re.search(rf'"{re.escape(name)}"\s*:\s*"([^"]+)"', page)
        return m.group(1) if m else None

    machine = _field("activeContentMachineName")
    product = _field("productHumanName")
    end_date = _field("activeContentEndDate|datetime")

    items: dict[str, dict] = {}
    dec = json.JSONDecoder()
    for m in _ITEM_MARK_RE.finditer(page):
        name = m.group(1)
        if name in items:
            continue
        brace = page.rfind("{", m.start(), m.end())
        try:
            obj, _ = dec.raw_decode(page, brace)
        except ValueError:
            continue
        if isinstance(obj, dict) and obj.get("title"):
            items[name] = obj
    return {
        "machineName": machine,
        "productName": product,
        "endDate": end_date,
        "items": items,
    }


def _pick_appid(title: str, hits: list[dict]) -> int | None:
    """storesearch 命中列表 → appid：exact → 清洗名 → playtest 归一 三级匹配
    （前两级与 XGP 链同口径）。

    两轮扫描而非逐条短路：副标题变体（如 Deluxe Edition 被清洗层剥掉）
    不能抢在 exact 命中之前。playtest 层：Humble 偶发给测试键（页内标题
    带 Playtest），商店侧无该条目——剥掉 playtest 词后与商店本体同形即认。
    """
    want = title.lower().strip()
    want_clean = _clean_name(title)
    want_pt = " ".join(t for t in want_clean.split() if t != "playtest")
    exact = clean = playtest = None
    for hit in hits:
        name = str(hit.get("name") or "")
        hit_id = str(hit.get("id") or "")
        if not name or not hit_id.isdigit():
            continue
        name_clean = _clean_name(name)
        if exact is None and name.lower().strip() == want:
            exact = int(hit_id)
        if clean is None and want_clean and name_clean == want_clean:
            clean = int(hit_id)
        if playtest is None and want_pt and want_pt != want_clean \
                and name_clean == want_pt:
            playtest = int(hit_id)
    return exact if exact is not None else (clean if clean is not None else playtest)


def _hb_headers() -> dict:
    return {
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }


async def _strategy_proxy() -> str | None:
    try:
        from app.domains.proxies import service as proxies_service

        return await proxies_service.resolve_proxy_url()
    except Exception:  # noqa: BLE001
        return None


async def _fetch_membership_page(
    session: aiohttp.ClientSession, proxy: str | None
) -> str | None:
    try:
        async with session.get(
            _MEMBERSHIP_URL,
            headers=_hb_headers(),
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=_CHOICE_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.text(errors="replace")
    except Exception:  # noqa: BLE001
        return None


async def _resolve_appid(
    session: aiohttp.ClientSession, title: str, proxy: str | None
) -> int | None:
    """条目标题 → Steam appid（storesearch，HTTP 层失败返回 None 由调用方记账）。

    查询词先剥符号（Keylocker | …、Pocket Mirror ~ … 这类标题里的
    竖线/波浪号会让 storesearch 返回空），空结果再降为前 2 词/首词——
    命中判定在返回侧按名匹配，查询词短不影响对准。"""
    base = re.sub(r"[^\w\s]", " ", title)
    tokens = base.split()
    candidates = [" ".join(tokens)]
    if len(tokens) > 2:
        candidates.append(" ".join(tokens[:2]))
    if len(tokens) > 1:
        candidates.append(tokens[0])
    try:
        for term in candidates:
            async with session.get(
                _STORESEARCH_URL,
                params={"term": term, "cc": "us", "l": "en"},
                headers=_hb_headers(),
                proxy=proxy,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json(content_type=None)
            hits = (data or {}).get("items") or []
            if hits:
                return _pick_appid(title, hits)
        return None
    except Exception:  # noqa: BLE001
        return None


async def _mark_game_hb(appid: int, title: str, label: str) -> None:
    """游戏侧标记：缺行落占位（updated_at NULL → 回补池），已有行补写标记。

    hb_data 多月逗号续写（与外部史低库 "Choice (A), Choice (B)" 同形态），
    已含当月标记时不动（幂等）。占位行的元数据/中文名/价格由孤儿回补层消化。
    """
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    async with get_session_factory()() as session:
        row = await session.get(Game, int(appid))
        if row is None:
            session.add(
                Game(
                    appid=int(appid), name=title,
                    is_hb=True, hb_data=label,
                    created_at=now,
                )
            )
        else:
            old = row.hb_data or ""
            if label not in old:
                row.hb_data = f"{old}, {label}" if old else label
            row.is_hb = True
        await session.commit()


async def refresh_hb_choice() -> dict:
    """当月 HB Choice → games.is_hb/hb_data 标记（每日调度与手动触发同入口）。

    幂等判据：membership 页 activeContentMachineName 与 app_settings 记账值
    一致即跳过（新月包出现才走解析+打标）。返回摘要，失败的条目（storesearch
    未命中）记 unresolved 留待人工，不阻塞其余条目。
    """
    from app.domains.settings.service import get_value, set_value

    proxy = await _strategy_proxy()
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        page = await _fetch_membership_page(session, proxy)
        if not page:
            return {"source": "hb-choice", "ok": False, "error": "membership 页抓取失败"}
        parsed = _parse_choice_page(page)
        machine = parsed["machineName"]
        if not machine or not parsed["items"]:
            return {"source": "hb-choice", "ok": False, "error": "页面无当月包数据"}

        last = await get_value(_HB_STATE_KEY) or {}
        if last.get("machineName") == machine:
            return {
                "source": "hb-choice", "ok": True, "skipped": True,
                "machineName": machine, "lastMarked": last.get("marked"),
            }

        label = _choice_month_label(machine, parsed["productName"])
        steam_items = {
            name: item for name, item in parsed["items"].items()
            if "steam" in (item.get("delivery_methods") or [])
        }
        marked, unresolved = [], []
        for name, item in steam_items.items():
            title = str(item.get("title") or "")
            appid = await _resolve_appid(session, title, proxy)
            if appid is None:
                unresolved.append(title)
                logger.warning("[hb-choice] 条目 %s（%s）未能解析 appid，留待人工", name, title)
            else:
                await _mark_game_hb(appid, title, label)
                marked.append({"appid": appid, "title": title})
            await asyncio.sleep(0.3)

    if unresolved:
        # 空数据/不完整结案不记账：记账后 machine_name 判重会把当月永久
        # 跳过（搜索侧故障一整天 = 整月漏标）。打标幂等，次日重跑零代价，
        # 未全量结案前每天重试直到全解析。
        logger.warning(
            "[hb-choice] %s 有 %d 条未解析，不记账（次日重试）",
            machine, len(unresolved),
        )
        return {
            "source": "hb-choice", "ok": True, "skipped": False,
            "machineName": machine,
            "productName": parsed["productName"], "label": label,
            "marked": marked, "unresolved": unresolved, "recorded": False,
        }

    await set_value(
        _HB_STATE_KEY,
        {
            "machineName": machine,
            "productName": parsed["productName"],
            "marked": len(marked),
        },
    )
    logger.info(
        "[hb-choice] %s 打标完成：%d 款（%s），已记账",
        machine, len(marked), label,
    )
    return {
        "source": "hb-choice", "ok": True, "machineName": machine,
        "productName": parsed["productName"], "label": label,
        "marked": marked, "unresolved": unresolved, "recorded": True,
    }


# ─── Epic 喜加一：促销端点自动链 + 外部名单推送 ──────────────────────
# 促销端点窗口只含当期+未来两周，无历史——历史深度由静态档案导入
# （import_epic）与外部名单（import_epic_list，浏览器侧脚本推送）补足；
# 三路汇合于 games.is_epic/epic_date，标记语义一致：已标记行不覆写
# epic_date（先到的日期视为首次赠送事实，后到不回写）。

_EPIC_DATE_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")


def _norm_epic_date(raw: str) -> str | None:
    """外部名单日期 → 库内格式 YYYY-M-D（无前导零）。

    兼容形态：2026-5-28 / 2026-05-08 / 2026.5.28 / 2026/5/28 /
    2026年5月28日。解析失败返回 None（只打标不写日期）。
    """
    m = _EPIC_DATE_RE.search(str(raw or ""))
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1 <= mo <= 12 and 1 <= d <= 31):
        return None
    return f"{y}-{mo}-{d}"


async def _mark_game_epic(appid: int, title: str, free_start: str | None) -> bool:
    """游戏侧标记：缺行落占位（updated_at NULL → 回补池），已有行补缺。

    与当月包链同例外；已标记行只补空白 epic_date，不覆写既有值
    （静态档案/首见日期优先）。返回是否有写入（幂等判据）。
    """
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    async with get_session_factory()() as session:
        row = await session.get(Game, int(appid))
        if row is None:
            session.add(
                Game(
                    appid=int(appid), name=title,
                    is_epic=True, epic_date=free_start,
                    created_at=now,
                )
            )
            changed = True
        else:
            changed = False
            if not row.is_epic:
                row.is_epic = True
                changed = True
            if free_start is not None and not row.epic_date:
                row.epic_date = free_start
                changed = True
        await session.commit()
    return changed


async def refresh_epic_free() -> dict:
    """Epic 促销端点（当期+预告窗口）→ storesearch 匹配 → 游戏侧标记。

    每日调度与手动触发同入口，幂等（已标记跳过）；未匹配条目
    （storesearch 未命中）记 unresolved 留待次日重试，不阻塞其余。
    """
    proxy = await _strategy_proxy()
    games = await fetch_free_games(proxy=proxy)
    if not games:
        return {"source": "epic-free", "ok": False, "error": "促销端点本轮无白送元素"}

    marked, unresolved = [], []
    for g in games:
        if g.appid is None:
            unresolved.append({"title": g.title, "freeStart": g.free_start})
            logger.warning("[epic-free] 条目 %s 未能解析 appid，留待次日", g.title)
            continue
        if await _mark_game_epic(g.appid, g.title_cn or g.title, g.free_start):
            marked.append({"appid": g.appid, "title": g.title, "freeStart": g.free_start})

    logger.info(
        "[epic-free] 窗口标记完成：窗口 %d 款，新标 %d 款，未解析 %d 款",
        len(games), len(marked), len(unresolved),
    )
    return {
        "source": "epic-free", "ok": True, "window": len(games),
        "marked": marked, "unresolved": unresolved,
    }


async def import_epic_list(items: list[dict]) -> dict:
    """外部名单推送导入：浏览器侧脚本抓取的 appid/日期对 → 游戏侧标记。

    与自动链同标记语义；appid 非数字跳过，日期归一失败只打标。
    用于补自动链窗口之外的历史缺口（名单帖数据人工核对更全）。
    """
    marked, skipped = [], 0
    for it in items:
        aid = str(it.get("appid", "") or "")
        if not aid.isdigit():
            skipped += 1
            continue
        free_start = _norm_epic_date(str(it.get("free_start") or ""))
        title = str(it.get("title") or "") or f"appid {aid}"
        if await _mark_game_epic(int(aid), title, free_start):
            marked.append({"appid": int(aid), "freeStart": free_start})

    logger.info(
        "[epic-list] 外部名单导入：收到 %d 条，新标 %d 条，跳过 %d 条",
        len(items), len(marked), skipped,
    )
    return {
        "source": "epic-list", "ok": True, "received": len(items),
        "marked": marked, "skipped": skipped,
    }


# ─── Epic 白送展示链（仪表盘卡片）：促销端点 → 只读缓存 ──────────────
# 与标记链（refresh_epic_free）分离：本链不做 storesearch、不落库，
# 进程内缓存 30 分钟——前端每小时轮询打不穿促销端点，周四轮换后
# 最多滞后缓存 TTL + 前端轮询间隔显现。

_EPIC_OFFERS_TTL_SECONDS = 30 * 60
_epic_offers_cache: dict = {"at": 0.0, "payload": None}


async def epic_free_offers(force: bool = False) -> dict:
    """当期 + 预告白送元素（含封面/商店页/原价），仪表盘卡片数据源。

    只读链：命中缓存直接返回（cached=True）；拉取失败不缓存、
    返回 ok=False，前端保持上一份数据或落空态。
    """
    now = time.monotonic()
    cached = _epic_offers_cache["payload"]
    if cached is not None and not force and now - _epic_offers_cache["at"] < _EPIC_OFFERS_TTL_SECONDS:
        return {**cached, "cached": True}

    proxy = await _strategy_proxy()
    games = await fetch_free_offers(proxy=proxy)
    # 移动白送与 PC 列表**独立取数**（任一成功即出卡；Epic 促销端点偶发
    # 连接失败不该连累移动卡），GamerPower 自动源优先，失败降级 breaker。
    # 立绘恒用 Epic 自家 breaker 图：高清且无防盗链（GamerPower 缩图在
    # 站外 referer 下加载失败）；breaker 缺席时回落 GamerPower 图。
    raw_mobile = await fetch_mobile_freebie(proxy=proxy)
    breaker = await fetch_mobile_breaker(proxy=proxy)
    if raw_mobile:
        mobile = {**raw_mobile, "source": "gamerpower"}
        # 封面优先用游戏自己的官方 keyImage（resolve 链带回）；缺图才落
        # breaker 营销图兜底
        if not mobile.get("image") and breaker and breaker.get("image"):
            mobile["image"] = breaker["image"]
    elif breaker:
        mobile = {
            "title": None, "image": breaker["image"], "url": breaker["url"],
            "end": None, "worth": None, "source": "breaker",
        }
    else:
        mobile = None
    if not games and mobile is None:
        return {"source": "epic-offers", "ok": False, "offers": [], "mobile": None, "fetchedAt": None}

    payload = {
        "source": "epic-offers", "ok": True,
        "offers": [
            {
                "title": g.title,
                "titleCn": g.title_cn,
                "appid": g.appid,
                "start": g.free_start,
                "end": g.free_end,
                "upcoming": g.upcoming,
                "image": g.image,
                "url": g.url,
                "priceOriginal": g.price_original,
            }
            for g in games
        ],
        "mobile": mobile,
        "fetchedAt": get_beijing_now_iso(),
    }
    # PC 列表失败时只出移动卡，不写缓存——下轮轮询重试 Epic
    if games:
        _epic_offers_cache["payload"] = payload
        _epic_offers_cache["at"] = now
    return dict(payload)


def get_beijing_now_iso() -> str:
    """北京时间 ISO 文案（卡片「更新于」展示用）。"""
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().isoformat(timespec="seconds")


# ─── Barter.vg 第三方 bundle 计数：全量档案 → games.bundle_count ────
# 单文件全量查表（非逐游戏 API）：一次下载、按 appid 对准库内行。
# bundles_packages 是 Steam 自家捆绑包口径，与站内 bundles 域重叠，
# 不取用——本字段只表达「第三方渠道进包史」。

_BARTERVG_BUNDLES_URL = "https://bartervg.com/browse/bundles/json/"
# 完整性哨兵：全量档案当前 3 万条级，低于此门槛视为截断/错误页响应，
# 丢弃本轮（保留库内旧值），不写入。
_BARTERVG_MIN_RECORDS = 10000
_BUNDLES_STATE_KEY = "bartervg_bundles_last_fetch"
_BUNDLES_TIMEOUT = 90
# 新鲜度闸：计数只增不减，档案 48h 内拉过即跳过（调度与启动补跑共用；
# 手动触发传 force=True 绕过）
_BUNDLES_TTL_HOURS = 48


def _check_archive_integrity(data: object) -> None:
    """完整性哨兵：全量档案当前 3 万条级，低于门槛视为截断/错误页响应，
    丢弃本轮（保留库内旧值），不写入。"""
    if not isinstance(data, dict):
        raise ValueError("bundles 档案顶层不是对象")
    if len(data) < _BARTERVG_MIN_RECORDS:
        raise ValueError(
            f"bundles 档案疑似截断（记录数 {len(data)} < {_BARTERVG_MIN_RECORDS}）"
        )


def _bundles_obj_to_map(data: dict) -> dict[int, int]:
    """档案对象 → {appid: bundles 计数}。非数字键/非正计数剔除。"""
    out: dict[int, int] = {}
    for key, entry in data.items():
        if not str(key).isdigit() or not isinstance(entry, dict):
            continue
        count = entry.get("bundles")
        if isinstance(count, int) and count > 0:
            out[int(key)] = count
    return out


def _parse_bundles_map(raw: bytes | str) -> dict[int, int]:
    data = json.loads(raw)
    _check_archive_integrity(data)
    return _bundles_obj_to_map(data)


async def _fetch_bartervg_bundles(proxy: str | None) -> dict[int, int]:
    connector = aiohttp.TCPConnector(limit=2, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        async with session.get(
            _BARTERVG_BUNDLES_URL,
            proxy=proxy,
            timeout=aiohttp.ClientTimeout(total=_BUNDLES_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status}")
            return _parse_bundles_map(await resp.read())


async def refresh_bundle_counts(force: bool = False) -> dict:
    """Barter.vg bundle 计数 → games.bundle_count（每日调度与手动触发同入口）。

    幂等判据：差量写入——只更新与库内现值不同的行，未变化时零写入。
    只更新库内已存在的行（与 import_epic/xgp/hb 同语义，不为档案里的
    3 万 appid 落占位）；不在档案中的行保持 NULL（前端不展示）。
    """
    from app.crawler.utils import get_beijing_time_obj
    from app.domains.settings.service import get_value, set_value

    if not force:
        last = await get_value(_BUNDLES_STATE_KEY) or {}
        fetched_at = str(last.get("fetchedAt") or "")
        if fetched_at:
            age_h = (
                get_beijing_time_obj().replace(tzinfo=None)
                - datetime.fromisoformat(fetched_at)
            ).total_seconds() / 3600
            if age_h < _BUNDLES_TTL_HOURS:
                return {
                    "source": "bartervg-bundles", "ok": True, "skipped": True,
                    "fetchedAt": fetched_at, "lastUpdated": last.get("updated"),
                }

    proxy = await _strategy_proxy()
    try:
        bundles_map = await _fetch_bartervg_bundles(proxy)
    except Exception as e:  # noqa: BLE001
        logger.warning("[bartervg] bundles 档案拉取失败：%s", e)
        return {"source": "bartervg-bundles", "ok": False, "error": str(e)}

    updated = 0
    ids = sorted(bundles_map)
    async with get_session_factory()() as session:
        # SQLite 变量数上限分片查交集；逐片差量更新
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            rows = (
                await session.execute(
                    select(Game.appid, Game.bundle_count).where(Game.appid.in_(chunk))
                )
            ).all()
            values = {aid: bundles_map[aid] for aid, cur in rows if cur != bundles_map[aid]}
            if values:
                await session.execute(
                    update(Game)
                    .where(Game.appid.in_(list(values)))
                    .values(bundle_count=case(values, value=Game.appid))
                )
                updated += len(values)
        await session.commit()

    now = get_beijing_time_obj().replace(tzinfo=None)
    await set_value(
        _BUNDLES_STATE_KEY,
        {"fetchedAt": now.isoformat(), "records": len(bundles_map), "updated": updated},
    )
    logger.info(
        "[bartervg] bundle 计数刷新完成：档案 %d 条，库内命中更新 %d 行",
        len(bundles_map), updated,
    )
    return {
        "source": "bartervg-bundles", "ok": True, "skipped": False,
        "records": len(bundles_map), "updated": updated,
    }


IMPORTERS = {
    "epic": import_epic,
    "xgp": import_xgp,
    "hb": import_hb,
}


async def import_all(source_dir: str | None = None) -> list[dict]:
    results = []
    for importer in IMPORTERS.values():
        results.append(await importer(source_dir))
    return results


async def metadata_status(source_dir: str | None = None) -> dict:
    """各源文件存在性 + 库内标记计数。"""
    from app.domains.settings.service import get_value

    base = Path(source_dir) if source_dir else DEFAULT_SOURCE_DIR
    async with get_session_factory()() as session:
        epic_count = await session.scalar(
            select(func.count()).select_from(Game).where(Game.is_epic.is_(True))
        )
        xgp_count = await session.scalar(
            select(func.count()).select_from(Game).where(Game.xgp_tier.is_not(None))
        )
        hb_count = await session.scalar(
            select(func.count()).select_from(Game).where(Game.is_hb.is_(True))
        )
        bundle_count = await session.scalar(
            select(func.count()).select_from(Game).where(Game.bundle_count.is_not(None))
        )
    last_bundles = await get_value(_BUNDLES_STATE_KEY) or {}
    return {
        "sourceDir": str(base),
        "files": {
            "epic": str(base / _EPIC_FILE),
            "xgpTiered": str(base / _XGP_TIERED_FILE),
            "xgpLinks": str(base / _XGP_LINKS_FILE),
            "hb": str(base / _HB_DB_FILE),
        },
        "counts": {
            "isEpic": epic_count or 0,
            "xgpTier": xgp_count or 0,
            "isHb": hb_count or 0,
            "bundleCount": bundle_count or 0,
        },
        "bartervg": {
            "fetchedAt": last_bundles.get("fetchedAt"),
            "records": last_bundles.get("records"),
        },
    }
