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
当月包链与 Epic 链例外：新 appid 允许落占位行（updated_at NULL，待后续首爬补全）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import aiohttp
from sqlalchemy import case, func, or_, select, update

from app.core.config import get_settings
from app.core.database import WritePriority, get_session_factory, write_session
from app.core.logging import log_event
from app.crawler.epic_free import (
    fetch_free_games,
    fetch_free_offers,
    fetch_mobile_breaker,
    fetch_mobile_freebie,
    resolve_mobile_freebie,
)
from app.domains.proxies.service import strategy_proxy_or_direct
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


class SourceDirRejected(ValueError):
    """请求给定的导入目录越界/不可用：统一文案，不回显请求路径。"""


def constrain_source_dir(source_dir: str | None) -> Path:
    """请求给定的导入目录 resolve 后必须收容在数据目录内，否则拒绝。

    环境变量默认目录是本机运营者的自有配置（与 API 攻击面不同信任级），不在此闸内。
    """
    if not source_dir:
        return DEFAULT_SOURCE_DIR
    try:
        resolved = Path(source_dir).resolve()
        resolved.relative_to(Path(get_settings().data_dir).resolve())
    except (OSError, ValueError):
        raise SourceDirRejected("导入目录不存在或不可用") from None
    return resolved


def resolve_source_path(source_dir: str | None, filename: str) -> Path:
    return constrain_source_dir(source_dir) / filename



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
    async with write_session(WritePriority.BACKGROUND, label="metadata_batch") as session:
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
    async with write_session(WritePriority.BACKGROUND, label="metadata_batch") as session:
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
    async with write_session(WritePriority.BACKGROUND, label="metadata_batch") as session:
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


_MEMBERSHIP_URL = "https://www.humblebundle.com/membership"
_STORESEARCH_URL = "https://store.steampowered.com/api/storesearch"
_HB_STATE_KEY = "hb_choice_last_month"
_CHOICE_TIMEOUT = 25

_FLIGHT_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')
_MONTH_DESC_RE = re.compile(r"Get ([A-Za-z]+) (\d{4}) Humble Choice")


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


def _flight_payload(page: str) -> str:
    """抽取并解码页面内嵌的 RSC flight 流（每段 push 是一个 JS 字符串字面量，
    按 JSON 转义解码；个别段解码失败跳过，数据在其余段仍可拼齐）。"""
    parts: list[str] = []
    for m in _FLIGHT_PUSH_RE.finditer(page):
        try:
            parts.append(json.loads(f'"{m.group(1)}"'))
        except ValueError:
            continue
    return "".join(parts)


def _extract_json_array(payload: str, key: str) -> list | None:
    """key 后第一个完整 JSON 数组（配平引号/转义/括号），失败返回 None。"""
    i = payload.find(key)
    if i < 0:
        return None
    start = i + len(key)
    while start < len(payload) and payload[start] in " :":
        start += 1
    if start >= len(payload) or payload[start] != "[":
        return None
    depth = 0
    in_str = False
    esc = False
    for j in range(start, len(payload)):
        ch = payload[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    value = json.loads(payload[start:j + 1])
                except ValueError:
                    return None
                return value if isinstance(value, list) else None
    return None


def _parse_choice_page(html_text: str) -> dict:
    """membership 页（2026-11 Next.js RSC 改版）→ 结构化当月包。

    页面内容以 RSC flight 流内嵌（每段 self.__next_f.push 是一个 JS 字符串
    字面量，按 JSON 转义解码后拼接），清单取 subscriptionProducts 全量：
    Steam 交付走 appid 匹配入 games 账本；其余平台交付（blizzard/epic/
    ubisoft/…，平台名取交付方式原值，展示层本地化）按平台游戏保留（appid=0，
    不进 games 账本）；仅 external_key 的条目是订阅/权益类（如 IGN Plus），
    不是游戏，排除。期号取页内 meta 描述（"Get November 2026 Humble Choice"）。
    """
    payload = _flight_payload(html_text)
    products = _extract_json_array(payload, '"subscriptionProducts"')
    machine_m = _MONTH_DESC_RE.search(payload)

    steam_titles: list[str] = []
    platform_games: list[dict] = []
    seen: set[str] = set()
    end_date = ""
    for product in products or []:
        if not isinstance(product, dict):
            continue
        title = str(product.get("title") or "").strip()
        if not title or title in seen:
            continue
        delivery = product.get("deliveryMethods") or []
        if not end_date:
            end_date = str(product.get("endDate") or "")
        if "steam" in delivery:
            seen.add(title)
            steam_titles.append(title)
            continue
        platform = next((d for d in delivery if d not in ("steam", "external_key")), "")
        if not platform:
            continue
        seen.add(title)
        cents = ((product.get("displayPrice") or {}).get("currentPrice") or {}).get("centAmount")
        platform_games.append({
            "title": title,
            "platform": platform,
            "thumbnail": str(product.get("thumbnail") or ""),
            "retailFen": int(cents + 0.5) if isinstance(cents, (int, float)) and cents > 0 else None,
            "endDate": str(product.get("endDate") or ""),
        })

    machine = f"{machine_m.group(1).lower()}_{machine_m.group(2)}_choice" if machine_m else None
    product_name = f"{machine_m.group(1)} {machine_m.group(2)}" if machine_m else None
    return {
        "machineName": machine,
        "productName": product_name,
        "endDate": end_date,
        "steamTitles": steam_titles,
        "platformGames": platform_games,
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
    """Steam storesearch 等纯 JSON 端点用：文档导航形态的头会被对端断连。"""
    return {
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    }


def _membership_headers() -> dict:
    """membership 页头指纹：须携带完整 Sec-Fetch/sec-ch-ua 系列，
    缺失时服务端返回不含内嵌数据的降级壳页。"""
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "sec-ch-ua": '"Chromium";v="152", "Not?A_Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36",
    }


async def _fetch_membership_page(
    session: aiohttp.ClientSession, proxy: str | None
) -> str | None:
    try:
        async with session.get(
            _MEMBERSHIP_URL,
            headers=_membership_headers(),
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
    """游戏侧标记：缺行落占位（updated_at NULL），已有行补写标记。

    hb_data 多月逗号续写（"Choice (A), Choice (B)" 同形态），
    已含当月标记时不动（幂等）。占位行由后续首爬通道带全量元数据回补。
    """
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    async with write_session(WritePriority.BACKGROUND, label="metadata_batch") as session:
        from app.domains.games.derived import catalog_inputs, update_price_derived

        before = await catalog_inputs(session, [int(appid)])
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
        await update_price_derived([int(appid)], session=session, catalog_before=before)
        await session.commit()


async def refresh_hb_choice(force: bool = False) -> dict:
    """当月 HB Choice → games.is_hb/hb_data 标记（每日调度与手动触发同入口）。

    幂等判据：membership 页 activeContentMachineName 与 app_settings 记账值
    一致即跳过（新月包出现才走解析+打标）；force=True 绕过判重强制重打标
    （手动触发用，标记本身按月续写幂等）。返回摘要，失败的条目（storesearch
    未命中）记 unresolved 留待人工，不阻塞其余条目。
    """
    from app.domains.settings.service import get_value, set_value

    proxy = await strategy_proxy_or_direct()
    connector = aiohttp.TCPConnector(limit=4, ttl_dns_cache=60)
    async with aiohttp.ClientSession(connector=connector) as session:
        page = await _fetch_membership_page(session, proxy)
        if not page:
            return {"source": "hb-choice", "ok": False, "error": "membership 页抓取失败"}
        parsed = _parse_choice_page(page)
        machine = parsed["machineName"]
        if not machine or (not parsed["steamTitles"] and not parsed["platformGames"]):
            return {"source": "hb-choice", "ok": False, "error": "页面无当月包数据"}

        last = await get_value(_HB_STATE_KEY) or {}
        if last.get("machineName") == machine and not force:
            return {
                "source": "hb-choice", "ok": True, "skipped": True,
                "machineName": machine, "lastMarked": last.get("marked"),
            }

        label = _choice_month_label(machine, parsed["productName"])
        marked, unresolved = [], []
        for title in parsed["steamTitles"]:
            appid = await _resolve_appid(session, title, proxy)
            if appid is None:
                unresolved.append(title)
                log_event(
                    logger,
                    f"《{title}》未能匹配到 appid，留待人工处理",
                    tag="未完成",
                    detail={"条目": title},
                    level=logging.WARNING,
                )
            else:
                await _mark_game_hb(appid, title, label)
                marked.append({"appid": appid, "title": title})
            await asyncio.sleep(0.3)

    if unresolved:
        # 空数据/不完整结案不记账：记账后 machine_name 判重会把当月永久
        # 跳过（搜索侧故障一整天 = 整月漏标）。打标幂等，次日重跑零代价，
        # 未全量结案前每天重试直到全解析。
        log_event(
            logger,
            f"当月包有 {len(unresolved)} 款未解析出 appid，本轮不记账，次日重试",
            tag="未完成",
            detail={"月包标识": machine},
            level=logging.WARNING,
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
            "platformGames": parsed["platformGames"],
        },
    )
    from app.domains.notifications import facts as facts_service

    await facts_service.record_fact(
        source="hb_choice",
        kind="bundle_changed",
        fact_key=f"hb_choice:{machine}",
        data={
            "label": label,
            "productName": parsed["productName"],
            "count": len(marked) + len(parsed["platformGames"]),
            "url": _month_page_url(machine),
            "end": parsed.get("endDate"),
            "items": [{"appid": item["appid"], "name": item["title"],
                       "url": _month_page_url(machine)} for item in marked],
        },
    )
    log_event(
        logger,
        f"当月包打标完成，{len(marked)} 款已记入 Humble Choice 名单",
        tag="成功",
        detail={"月包标识": machine, "标签": label},
    )
    return {
        "source": "hb-choice", "ok": True, "machineName": machine,
        "productName": parsed["productName"], "label": label,
        "marked": marked, "unresolved": unresolved, "recorded": True,
    }



_SKIP_MONTH_URL = "https://www.humblebundle.com/user/skip-month"
_HB_SETTINGS_URL = "https://www.humblebundle.com/user/settings"


def _month_page_url(machine_name: str | None) -> str:
    """september_2026_choice → https://www.humblebundle.com/membership/September-2026。

    月份页 URL 与 activeContentMachineName 同构（英文月份名-年份，如
    August-2026）；解析不出退回 /membership 订阅主页（当月包同页可达）。
    """
    m = re.match(r"^([a-z]+)_(\d{4})_choice$", machine_name or "")
    if m and m.group(1).capitalize()[:3] in _MONTH_MAP:
        return f"https://www.humblebundle.com/membership/{m.group(1).capitalize()}-{m.group(2)}"
    return _MEMBERSHIP_URL


async def hb_choice_offers() -> dict:
    """当月 HB Choice 游戏清单（仪表盘卡片数据源，只读本地库零外网）。

    当月标签取记账游标（与打标链同一事实源）；无游标（全新安装/抓取链
    尚未结案）按北京时间猜当月，仍查不到行即 ok=False 空态。价格取国区
    ok 行，口径与站内其余卡片一致（分为单位，前端 formatCnyFen 渲染）。
    """
    from app.crawler.utils import get_beijing_time_obj
    from app.domains.games.models import GameCurrentPrice
    from app.domains.settings.service import get_value

    state = await get_value(_HB_STATE_KEY) or {}
    machine = str(state.get("machineName") or "")
    product = str(state.get("productName") or "")
    if machine:
        label = _choice_month_label(machine, product or None)
    else:
        now = get_beijing_time_obj()
        label = f"HB慈善包{now.year % 100:02d}年{now.month}月包"

    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Game.appid, Game.name, Game.header_image, Game.min_cny_fen)
                .where(Game.is_hb.is_(True), Game.hb_data.like(f"%{label}%"))
                .order_by(func.coalesce(Game.review_count, 0).desc(), Game.appid)
            )
        ).all()
        prices: dict[int, tuple] = {}
        if rows:
            price_rows = (
                await session.execute(
                    select(
                        GameCurrentPrice.appid, GameCurrentPrice.price,
                        GameCurrentPrice.original_price,
                        GameCurrentPrice.discount_percent,
                    ).where(
                        GameCurrentPrice.appid.in_([r.appid for r in rows]),
                        GameCurrentPrice.region_code == "CN",
                        GameCurrentPrice.price_status == "ok",
                    )
                )
            ).all()
            prices = {int(p.appid): (p.price, p.original_price, p.discount_percent)
                      for p in price_rows}

    if not rows and not state.get("platformGames"):
        return {
            "source": "hb-offers", "ok": False,
            "error": "当月包尚未入库（等待每日抓取或手动触发 /metadata/hb/refresh）",
        }

    games = [
        {
            "appid": r.appid,
            "name": r.name or str(r.appid),
            "headerImage": r.header_image,
            "priceFen": prices.get(int(r.appid), (None, None, 0))[0],
            "originalPriceFen": prices.get(int(r.appid), (None, None, 0))[1],
            "discount": prices.get(int(r.appid), (None, None, 0))[2] or 0,
            "lowestCnyFen": r.min_cny_fen,
        }
        for r in rows
    ]
    # 非 Steam 平台游戏：games 账本以 Steam appid 主导，平台游戏（appid=0）
    # 以状态记录直接下发，前端按平台徽章渲染，不参与归属/价格体系
    for pg in state.get("platformGames") or []:
        games.append({
            "appid": 0,
            "name": str(pg.get("title") or ""),
            "headerImage": str(pg.get("thumbnail") or "") or None,
            "priceFen": None,
            "originalPriceFen": pg.get("retailFen"),
            "discount": 0,
            "lowestCnyFen": None,
            "platform": str(pg.get("platform") or ""),
        })

    return {
        "source": "hb-offers", "ok": True,
        "machineName": machine or None,
        "productName": product or None,
        "label": label,
        "monthUrl": _month_page_url(machine or None),
        "skipUrl": _SKIP_MONTH_URL,
        "settingsUrl": _HB_SETTINGS_URL,
        "games": games,
    }


async def unpriced_hb_appids(limit: int = 60) -> list[int]:
    """已标记 HB 但一行价格都没有的 appid（补价通道候选集）。

    通道判据：**无任何价格行**的才归本通道首爬；
    已有价格行（missing/blocked 状态）的归补抓账本，这里再抓属双通道
    重复。下架与永久免费（价格事实已定）不补。按 appid 升序限量。
    """
    from app.domains.games.models import GameCurrentPrice

    has_price = (
        select(GameCurrentPrice.appid)
        .where(GameCurrentPrice.appid == Game.appid)
        .exists()
    )
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Game.appid)
                .where(
                    Game.is_hb.is_(True),
                    ~has_price,
                    Game.removed_at.is_(None),
                    or_(Game.free_kind.is_(None), Game.free_kind != "f2p"),
                )
                .order_by(Game.appid)
                .limit(limit)
            )
        ).scalars().all()
    return [int(a) for a in rows]


async def backfill_hb_prices() -> dict:
    """已标记未取价的 HB 游戏补一次首爬（标记 ≠ 取价）。

    对齐导入/目录恢复语义：目录行 + 一次性首爬。走既有爬取管线
    （run_sequential 单任务模型）：撞锁 / 池未就绪由链式层跳过留日志，
    不阻塞标记链，下一轮（每日调度或手动刷新）重试；首爬落价后不再
    进候选集（幂等）。
    """
    from app.domains.crawl import service as crawl_service

    appids = await unpriced_hb_appids()
    if not appids:
        return {"source": "hb-backfill", "ok": True, "pending": 0, "started": False}
    results = await crawl_service.run_sequential(
        [{"scope": "appids", "appids": appids, "kind": "hb_backfill"}], trigger="automatic"
    )
    return {
        "source": "hb-backfill", "ok": True, "pending": len(appids),
        "started": bool(results), "jobId": results[0]["id"] if results else None,
    }


async def steam_free_offers() -> dict:
    """正在赠送中的 Steam 限时免费清单（仪表盘卡片数据源，纯本地库零外网）。"""
    from app.crawler.utils import get_beijing_time_obj
    from app.domains.games.models import GameCurrentPrice

    now_ts = int(get_beijing_time_obj().timestamp())
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(Game.appid, Game.name, Game.header_image, Game.promo_end_at)
                .where(
                    Game.free_kind == "promo",
                    Game.promo_end_at.is_not(None),
                    Game.promo_end_at > now_ts,
                )
                .order_by(Game.promo_end_at, Game.appid)
            )
        ).all()
        prices: dict[int, int | None] = {}
        if rows:
            price_rows = (
                await session.execute(
                    select(
                        GameCurrentPrice.appid, GameCurrentPrice.original_price
                    ).where(
                        GameCurrentPrice.appid.in_([r.appid for r in rows]),
                        GameCurrentPrice.region_code == "CN",
                        GameCurrentPrice.price_status == "ok",
                    )
                )
            ).all()
            prices = {int(p.appid): p.original_price for p in price_rows}

    return {
        "source": "steam-free",
        "ok": True,
        "fetchedAt": get_beijing_now_iso(),
        "offers": [
            {
                "appid": r.appid,
                "name": r.name or str(r.appid),
                "headerImage": r.header_image,
                "originalPriceFen": prices.get(int(r.appid)),
                "endTs": int(r.promo_end_at),
            }
            for r in rows
        ],
    }


# 促销端点窗口只含当期+未来两周，无历史——历史深度由静态档案导入
# （import_epic）与外部名单（import_epic_list，浏览器侧脚本推送）补足；
# 三路汇合于 games.is_epic/epic_date，标记语义一致：已标记行不覆写
# epic_date（先到的日期视为首次赠送事实，后到不回写）。

_EPIC_DATE_RE = re.compile(r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?")


def _offer_identity(offer: dict) -> str | None:
    """当期条目键：appid 优先，无 appid 用标题小写（与邮件通道同口径）。"""
    appid = offer.get("appid")
    if appid:
        return f"appid:{int(appid)}"
    title = str(offer.get("title") or "").strip().lower()
    return f"title:{title}" if title else None


def _current_offer_identities(payload: dict | None) -> set[str]:
    """payload 的当期（非预告）条目键集。"""
    if not isinstance(payload, dict):
        return set()
    out: set[str] = set()
    for offer in payload.get("offers") or []:
        if not isinstance(offer, dict) or offer.get("upcoming"):
            continue
        identity = _offer_identity(offer)
        if identity:
            out.add(identity)
    return out


async def _publish_epic_rotation(old_payload: dict | None, new_payload: dict) -> None:
    """当期集合与上一份快照比对 → 轮换事实（灵动岛消费）。

    新增条目非空才落行，fact_key = 新增键集——同一批新增重复刷新只落一行；
    首份快照不落（没有「之前」就没有「变了」）。落行失败由 record_fact
    自行兜底，不影响快照链。
    """
    old_keys = _current_offer_identities(old_payload)
    if not old_keys:
        return
    fresh = [
        offer
        for offer in new_payload.get("offers") or []
        if isinstance(offer, dict)
        and not offer.get("upcoming")
        and (identity := _offer_identity(offer)) is not None
        and identity not in old_keys
    ]
    if not fresh:
        return
    identities = sorted(_offer_identity(offer) or "" for offer in fresh)
    appid = int(fresh[0]["appid"]) if len(fresh) == 1 and fresh[0].get("appid") else None
    from app.domains.notifications import facts as facts_service

    await facts_service.record_fact(
        source="epic_free",
        kind="free_rotation",
        fact_key=f"epic_free:{'|'.join(identities)}",
        data={
            "count": len(fresh),
            **({"appid": appid} if appid is not None else {}),
            "titles": [
                str(offer.get("titleCn") or offer.get("title") or "").strip()
                for offer in fresh
            ][:8],
            "items": [{"appid": offer.get("appid"),
                       "name": str(offer.get("titleCn") or offer.get("title") or "").strip(),
                       "url": offer.get("url"), "image": offer.get("image"),
                       "start": offer.get("start"), "end": offer.get("end")}
                      for offer in fresh],
        },
    )



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
    """游戏侧标记：缺行落占位（updated_at NULL），已有行补缺。

    与当月包链同例外；已标记行只补空白 epic_date，不覆写既有值
    （静态档案/首见日期优先）。返回是否有写入（幂等判据）。
    """
    from app.crawler.utils import get_beijing_time_obj

    now = get_beijing_time_obj().replace(tzinfo=None)
    async with write_session(WritePriority.BACKGROUND, label="metadata_batch") as session:
        from app.domains.games.derived import catalog_inputs, update_price_derived

        before = await catalog_inputs(session, [int(appid)])
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
        await update_price_derived([int(appid)], session=session, catalog_before=before)
        await session.commit()
    return changed


async def refresh_epic_free() -> dict:
    """Epic 促销端点（当期+预告窗口）→ storesearch 匹配 → 游戏侧标记。

    每日调度与手动触发同入口，幂等（已标记跳过）；未匹配条目
    （storesearch 未命中）记 unresolved 留待次日重试，不阻塞其余。
    """
    proxy = await strategy_proxy_or_direct()
    games = await fetch_free_games(proxy=proxy)
    if not games:
        return {"source": "epic-free", "ok": False, "error": "促销端点本轮无白送元素"}

    marked, unresolved = [], []
    for g in games:
        if g.appid is None:
            unresolved.append({"title": g.title, "freeStart": g.free_start})
            log_event(
                logger,
                f"《{g.title}》未能匹配到 appid，留待次日重试",
                tag="未完成",
                level=logging.WARNING,
            )
            continue
        if await _mark_game_epic(g.appid, g.title_cn or g.title, g.free_start):
            marked.append({"appid": g.appid, "title": g.title, "freeStart": g.free_start})

    log_event(
        logger,
        f"Epic 免费窗口标记完成，新标记 {len(marked)} 款",
        tag="成功",
        detail={"窗口": len(games), "未解析": len(unresolved)},
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

    log_event(
        logger,
        f"Epic 外部名单导入完成，新标记 {len(marked)} 款",
        tag="成功",
        detail={"收到": len(items), "跳过": skipped},
    )
    return {
        "source": "epic-list", "ok": True, "received": len(items),
        "marked": marked, "skipped": skipped,
    }


_EPIC_OFFERS_TTL_SECONDS = 30 * 60
# 落库键（app_settings KV；value = {"fetched_at": epoch 秒, "payload": {...}}）
_EPIC_OFFERS_CACHE_KEY = "metadata.epic_offers_cache"
_epic_offers_cache: dict = {"at": 0.0, "payload": None}
_epic_offers_refreshing = False
# 后台刷新任务引用（测试可 await 收尾；生产侧不需要持有）
_epic_offers_refresh_task: asyncio.Task | None = None


async def _read_offers_snapshot() -> dict | None:
    """落库快照 → {"at": epoch, "payload": ...}；缺失/坏值返回 None。"""
    from app.domains.settings.service import get_value

    try:
        row = await get_value(_EPIC_OFFERS_CACHE_KEY)
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "Epic 免费游戏快照读取失败，本轮按无缓存处理",
            detail={"原因": e},
            level=logging.WARNING,
        )
        return None
    if not isinstance(row, dict):
        return None
    payload, at = row.get("payload"), row.get("fetched_at")
    if not isinstance(payload, dict) or not isinstance(at, (int, float)):
        return None
    return {"at": float(at), "payload": payload}


async def _write_offers_snapshot(payload: dict) -> None:
    """写落库快照（失败只警告：本轮照常返回，下次启动重新现拉）。"""
    from app.domains.settings.service import set_value

    try:
        await set_value(
            _EPIC_OFFERS_CACHE_KEY,
            {"fetched_at": time.time(), "payload": payload},
        )
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "Epic 免费游戏快照写入失败，本次忽略",
            tag="忽略",
            detail={"原因": e},
            level=logging.WARNING,
        )


async def _fetch_offers_payload() -> dict | None:
    """现拉一轮完整展示链 → payload；PC 列表与移动端全失败返回 None。

    三路**并行独立取数**（促销端点 + GamerPower 移动白送链 + CMS breaker
    兜底图；Epic 促销端点偶发连接失败不该连累移动卡），各拉取函数自带
    网络容错返回空/None，gather 不需要异常兜底。移动白送主发现链 =
    GamerPower 真名 → 持久化查询解析 sandbox → 双端 0 元 Claim → 结账
    直链；该链失败退探测 PC 白送元素池，再退 breaker 立绘卡。封面优先
    游戏自己的官方 keyImage，缺图才落 breaker 营销图兜底。
    """
    proxy = await strategy_proxy_or_direct()
    games, freebie, breaker = await asyncio.gather(
        fetch_free_offers(proxy=proxy),
        fetch_mobile_freebie(proxy=proxy),
        fetch_mobile_breaker(proxy=proxy),
    )
    raw_mobile = freebie or await resolve_mobile_freebie(games, proxy)
    if raw_mobile:
        mobile = {**raw_mobile, "source": "epic"}
        mobile.setdefault("direct", True)
        if not mobile.get("image") and breaker and breaker.get("image"):
            mobile["image"] = breaker["image"]
    elif breaker:
        mobile = {
            "title": None, "image": breaker["image"], "url": breaker["url"],
            "end": None, "worth": None, "source": "breaker", "direct": False,
        }
    else:
        mobile = None
    if not games and mobile is None:
        return None
    return {
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


def _start_offers_refresh() -> None:
    """后台拉新（单飞：已在刷则跳过）。成功替换两层缓存；失败保留旧快照。"""
    global _epic_offers_refreshing, _epic_offers_refresh_task

    if _epic_offers_refreshing:
        return
    _epic_offers_refreshing = True

    async def _bg() -> None:
        global _epic_offers_refreshing

        try:
            payload = await _fetch_offers_payload()
            if payload is None:
                log_event(
                    logger,
                    "Epic 免费游戏后台刷新全部失败，保留旧快照",
                    tag="未完成",
                )
                return
            # 快照替换即当期集合可能变化：先比对落事实，再覆盖缓存
            await _publish_epic_rotation(_epic_offers_cache["payload"], payload)
            _epic_offers_cache["payload"] = payload
            _epic_offers_cache["at"] = time.time()
            if payload["offers"]:
                await _write_offers_snapshot(payload)
            log_event(
                logger,
                "Epic 免费游戏后台刷新完成",
                tag="成功",
                detail={"PC 白送": len(payload["offers"])},
            )
        except Exception as e:  # noqa: BLE001
            log_event(
                logger,
                "Epic 免费游戏后台刷新异常，保留旧快照",
                tag="未完成",
                detail={"原因": e},
            )
        finally:
            _epic_offers_refreshing = False

    _epic_offers_refresh_task = asyncio.create_task(_bg())


async def epic_free_offers(force: bool = False) -> dict:
    """当期 + 预告白送元素（含封面/商店页/原价），仪表盘卡片数据源。

    快照优先（stale-while-revalidate）：冷启动立即回上一份落库快照
    （stale=True）并触发后台刷新，前端短轮询自动覆盖；无快照才现拉。
    拉取失败不写缓存、返回 ok=False，前端保持上一份数据或落空态。
    """
    now = time.time()
    await _ensure_offers_cache_hydrated()
    cached = _epic_offers_cache["payload"]
    if not force and not await _epic_fetch_enabled():
        if cached is not None:
            return {**cached, "cached": True, "stale": True}
        return {"source": "epic-offers", "ok": False, "offers": [], "mobile": None, "fetchedAt": None}
    if cached is not None and not force:
        if now - _epic_offers_cache["at"] < _EPIC_OFFERS_TTL_SECONDS:
            return {**cached, "cached": True, "stale": False}
        _start_offers_refresh()
        return {**cached, "cached": True, "stale": True}

    payload = await _fetch_offers_payload()
    if payload is None:
        return {"source": "epic-offers", "ok": False, "offers": [], "mobile": None, "fetchedAt": None}
    await _publish_epic_rotation(_epic_offers_cache["payload"], payload)
    _epic_offers_cache["payload"] = payload
    _epic_offers_cache["at"] = time.time()
    if payload["offers"]:
        await _write_offers_snapshot(payload)
    return dict(payload)


async def _ensure_offers_cache_hydrated() -> None:
    """进程内存缓存为空时读落库快照补水（幂等；读失败按无缓存处理）。"""
    if _epic_offers_cache["payload"] is None:
        snapshot = await _read_offers_snapshot()
        if snapshot:
            _epic_offers_cache.update(snapshot)


async def _epic_fetch_enabled() -> bool:
    """「Epic 免费游戏」抓取开关（KV `fetch.epic_free`，默认开）。"""
    from app.domains.settings.service import get_value

    return bool(await get_value("fetch.epic_free", True))




def get_beijing_now_iso() -> str:
    """北京时间 ISO 文案（卡片「更新于」展示用）。"""
    from app.crawler.utils import get_beijing_time_obj

    return get_beijing_time_obj().isoformat(timespec="seconds")


# 单文件全量查表（非逐游戏 API）：一次下载、按 appid 对准库内行。
# bundles_packages 是 Steam 自家捆绑包口径，与站内 bundles 域重叠，
# 不取用——本字段只表达「第三方渠道进包史」。

_BARTERVG_BUNDLES_URL = "https://bartervg.com/browse/bundles/json/"
_BARTERVG_MIN_RECORDS = 10000
_BUNDLES_STATE_KEY = "bartervg_bundles_last_fetch"
_BUNDLES_TIMEOUT = 90
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

    proxy = await strategy_proxy_or_direct()
    try:
        bundles_map = await _fetch_bartervg_bundles(proxy)
    except Exception as e:  # noqa: BLE001
        log_event(
            logger,
            "Barter.vg 捆绑包档案拉取失败",
            detail={"原因": e},
            level=logging.WARNING,
        )
        return {"source": "bartervg-bundles", "ok": False, "error": str(e)}

    updated = 0
    ids = sorted(bundles_map)
    async with write_session(WritePriority.BACKGROUND, label="metadata_batch") as session:
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
    log_event(
        logger,
        "Barter.vg 捆绑包计数刷新完成",
        tag="成功",
        detail={"档案": len(bundles_map), "更新行": updated},
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

    base = constrain_source_dir(source_dir)
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
