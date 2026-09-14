"""bills 域 Steam 抓取器：直接拉全量消费历史 + 许可记录。

Cookie 绑定后自动拉取，前端不再有导入键。

数据通道：
- history：GET /account/history/ 首屏 → 页内 g_historyCursor →
  POST AjaxLoadMoreHistory（**deep form-encode**：cursor[wallet_txnid]=...&...&sessionid=...）；
  传 JSON 游标返回 200 + 空 html —— 静默截断（实测根因）。
- licenses：服务端分页，GET /account/licenses/ 首屏 + 沿 a.license_paginator_next
  的 href 逐页翻（旧版只抓首屏——超 50 条丢失，这里补全）。
- avatar/nickname：store account 页头（miniprofile 公开通道）。
"""
from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import httpx

from app.domains.account.steam_wallet import (
    WalletFetchError,
    parse_cookie_str,
)

logger = logging.getLogger(__name__)

# 同步进度回调：接收 {'stage': ..., 'pages'?: ..., 'rows'?: ...} 增量快照
ProgressCallback = Callable[[dict], Awaitable[None]]

HISTORY_URL = "https://store.steampowered.com/account/history/"
HISTORY_MORE_URL = "https://store.steampowered.com/account/AjaxLoadMoreHistory/"
LICENSES_URL = "https://store.steampowered.com/account/licenses/"
ACCOUNT_URL = "https://store.steampowered.com/account/"

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_TIMEOUT = 30.0
# 翻页安全上限（deep-form 一页即到底，正常远小于此值）
_MAX_PAGES = 100
_PAGE_DELAY = 0.15  # 温和限速（秒）

# ── history 行提取（与 parseHistoryRows 同构）─────────────────

_CURSOR_RE = re.compile(r"g_historyCursor\s*=\s*(\{.*?\})\s*;", re.DOTALL)


class SteamFetchError(RuntimeError):
    """账单/许可抓取失败。"""


@dataclass(slots=True)
class SteamReportData:
    """Steam_Report_*.json 同构产物（可直接喂 parser.parse_report）。"""

    nickname: str = ""
    avatar_base64: str = ""
    history: list[dict] = field(default_factory=list)
    licenses: list[dict] = field(default_factory=list)

    def to_report_json(self) -> dict:
        return {
            "account": {"nickname": self.nickname, "avatar_base64": self.avatar_base64},
            "history": self.history,
            "licenses": self.licenses,
        }


def _has_ssl_error(message: str) -> bool:
    lowered = message.lower()
    return any(
        clue in lowered
        for clue in ("ssl", "certificate verify failed", "self-signed", "certifi")
    )


# ── 登录页守门（redeem 域 _redirect_chain_hits_login 同语义）────────────
# Cookie 过期/失效时 account/* 会被 302 到登录页（follow_redirects 终点即登录页）。
# 此前无守门：登录页没有账单/许可表格 → 解析得 0 行"空报告"静默落库，
# 空 import 按 id 倒序排最前，把真实旧快照整个遮住（实测
# 「Cookie 过期后账单/入库许可证全空」的根因）。
_LOGIN_TITLE_RE = re.compile(r"<title>(.*?)</title>", re.I | re.S)


def _is_login_page(resp: httpx.Response) -> bool:
    final_url = str(resp.url)
    if "login" in final_url or "signin" in final_url:
        return True
    m = _LOGIN_TITLE_RE.search(resp.text[:8192])
    return bool(m and ("sign in" in m.group(1).lower() or "登录" in m.group(1)))


def _ensure_account_page(resp: httpx.Response, where: str) -> None:
    if _is_login_page(resp):
        raise SteamFetchError(
            f"{where}返回登录页：Cookie 已过期/失效，请到「我」页重新绑定后再同步"
        )


async def _get_html(
    client: httpx.AsyncClient, url: str, **kwargs
) -> httpx.Response:
    resp = await client.get(url, headers=_HEADERS, **kwargs)
    resp.raise_for_status()
    return resp


# ── history：DOM 行解析 ────────────────────────────────────────
# 顶层 div 深度计数（wht_items 的顶层 div = 物品行；wth_payment/wth_item_refunded 排除）
_TD_RE = {
    "date": re.compile(r'<td[^>]*class="wht_date[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
    "items": re.compile(r'<td[^>]*class="wht_items[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
    "type": re.compile(r'<td[^>]*class="wht_type[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
    "total": re.compile(r'<td[^>]*class="wht_total[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
    "wallet_change": re.compile(r'<td[^>]*class="wht_wallet_change[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
    "wallet_balance": re.compile(r'<td[^>]*class="wht_wallet_balance[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
    "base_price": re.compile(r'<td[^>]*class="wht_base_price[^"]*"[^>]*>(.*?)</td>', re.DOTALL),
}
_ROW_RE = re.compile(
    r'<tr[^>]*class="wallet_table_row[^"]*"[^>]*>(.*?)</tr>', re.DOTALL
)


def _strip_tags(fragment: str) -> str:
    import html as html_lib

    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    text = re.sub(r"[\t\n\r]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip()


def _top_divs(td_html: str) -> list[tuple[str, str]]:
    """td 内顶层 div 块 → [(class 属性, 块内 HTML)]（深度计数，跳过嵌套）。"""
    out: list[tuple[str, str]] = []
    depth = 0
    start: re.Match | None = None
    for tok in re.finditer(r"<div\b[^>]*>|</div>", td_html):
        if tok.group(0).startswith("<div"):
            if depth == 0:
                start = tok
            depth += 1
        else:
            depth -= 1
            if depth == 0 and start is not None:
                cls_m = re.search(r'class="([^"]*)"', start.group(0))
                out.append(
                    (cls_m.group(1) if cls_m else "", td_html[start.end():tok.start()])
                )
                start = None
    return out


def _remove_top_divs_by_class(td_html: str, class_keywords: tuple[str, ...]) -> str:
    """剔除 td 内**顶层**指定类的 div 块（含内部），返回剩余 HTML。

    供直文本兜底：剔除 payment/refunded 块后，剩余即 td 的直接文本
    （如充值行「已购买 XX 钱包资金」——新 UI 该文本无 div 包裹）。
    """
    spans: list[tuple[int, int]] = []
    depth = 0
    start: re.Match | None = None
    for tok in re.finditer(r"<div\b[^>]*>|</div>", td_html):
        if tok.group(0).startswith("<div"):
            if depth == 0:
                start = tok
            depth += 1
        else:
            depth -= 1
            if depth == 0 and start is not None:
                cls_m = re.search(r'class="([^"]*)"', start.group(0))
                cls = cls_m.group(1) if cls_m else ""
                if any(kw in cls for kw in class_keywords):
                    spans.append((start.start(), tok.end()))
                start = None
    out = td_html
    for s, e in reversed(spans):
        out = out[:s] + out[e:]
    return out


def parse_history_rows_from_html(html_fragment: str) -> list[dict]:
    """从 wallet_history_table tbody（或 AJAX 追加块）HTML 提取行（同构口径）。

    字段口径（与导出件字段一一对应）：
    item 排除 wth_payment/wth_item_refunded；受赠人独立 gift_recipients；
    type/type_count/payment/total/wallet_change/wallet_balance/base_price/
    original_price/discount 全字段。
    """
    import html as html_lib

    rows: list[dict] = []
    for row_m in _ROW_RE.finditer(html_fragment):
        row_html = row_m.group(1)
        date_m = _TD_RE["date"].search(row_html)
        date = _strip_tags(date_m.group(1)) if date_m else ""
        if not date:
            continue

        # items 顶层 div（排除支付/退款标记 div）；受赠人 a[data-miniprofile]
        items_td_m = _TD_RE["items"].search(row_html)
        items: list[str] = []
        gift_recipients: list[str] = []
        if items_td_m:
            items_html = items_td_m.group(1)
            for a_m in re.finditer(
                r"<a\b[^>]*data-miniprofile[^>]*>([^<]*)</a>", items_html
            ):
                name = _strip_tags(a_m.group(1))
                if name and name not in gift_recipients:
                    gift_recipients.append(name)
            top = _top_divs(items_html)
            for cls, block in top:
                if "wth_payment" in cls or "wth_item_refunded" in cls:
                    continue
                text = _strip_tags(block)
                if text and text not in items:
                    items.append(text)
            if not items:
                # 无有效顶层 div：直文本兜底。
                # - 充值行：文本直接在 td（无 div），如「已购买 A$ 35.00 钱包资金」
                # - 退款行：物品 div 之后只有 wth_item_refunded 块 → 剔除后取直文本
                remaining = _remove_top_divs_by_class(
                    items_html, ("wth_payment", "wth_item_refunded")
                )
                text = _strip_tags(remaining)
                if text:
                    items.append(text)
        item_str = ", ".join(items)

        # type：首个顶层 div；type_count（"6 市场交易" 批量行）
        type_td_m = _TD_RE["type"].search(row_html)
        type_text = ""
        payment_segs: list[str] = []
        if type_td_m:
            type_divs = _top_divs(type_td_m.group(1))
            if type_divs:
                type_text = _strip_tags(type_divs[0][1])
            for cls, block in type_divs[1:]:
                if "wth_payment" not in cls:
                    seg = _strip_tags(block)
                    if seg:
                        payment_segs.append(seg)
        type_count = 1
        cnt_m = re.match(r"^(\d+)\s+(?:市场交易|Market Transactions?)$", type_text, re.I)
        if cnt_m:
            type_count = int(cnt_m.group(1)) or 1

        def _field(key: str) -> str:
            m = _TD_RE[key].search(row_html)
            return _strip_tags(m.group(1)) if m else ""

        total_td = _field("total")
        # 原价/折扣兜底：total 列内的删除线元素
        original_price = ""
        op_m = re.search(r'class="wht_original_price"[^>]*>([^<]*)<', row_html)
        if op_m:
            original_price = html_lib.unescape(op_m.group(1)).strip()
        if not original_price:
            strike_m = re.search(r"<(?:s|del)>([^<]+)</(?:s|del)>", total_td)
            if strike_m:
                original_price = _strip_tags(strike_m.group(1))
        discount = ""
        dp_m = re.search(r'class="wht_discount_pct"[^>]*>([^<]*)<', row_html)
        if dp_m:
            discount = html_lib.unescape(dp_m.group(1)).strip()
        if not discount:
            dis_m = re.search(r'class="wht_discount"[^>]*>([^<]*)<', row_html)
            if dis_m:
                discount = html_lib.unescape(dis_m.group(1)).strip()

        rows.append(
            {
                "date": date,
                "item": item_str,
                "type": type_text,
                "type_count": type_count,
                "payment": " + ".join(payment_segs),
                "payment_parts": None,
                "total": _field("total"),
                "wallet_change": _field("wallet_change"),
                "wallet_balance": _field("wallet_balance"),
                "base_price": _field("base_price") or None,
                "original_price": original_price or None,
                "discount": discount or None,
                "gift_recipients": gift_recipients or None,
            }
        )
    return rows


# ── licenses：DOM 行解析（分类器 markRows/getExportData 同构）────

_LICENSE_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.DOTALL)
_LICENSE_DATE_RE = re.compile(
    r'<td[^>]*class="license_date_col"[^>]*>(.*?)</td>', re.DOTALL
)
_LICENSE_ACQ_RE = re.compile(
    r'<td[^>]*class="license_acquisition_col"[^>]*>(.*?)</td>', re.DOTALL
)
_PAGINATOR_NEXT_RE = re.compile(
    r'<a[^>]*class="[^"]*license_paginator_next[^"]*"[^>]*href="([^"]+)"', re.DOTALL
)


def parse_license_rows_from_html(page_html: str) -> list[dict]:
    """从 licenses 页 HTML 提取 (date, item, method)（分类器口径）。

    - 跳过表头行（日期列无数字）
    - 游戏名列 = 排除 date/acquisition/steamdb 之外的首列，剥「移除」按钮文字
    - 跳过「未找到许可」无效行
    """
    # 只处理 account_table 的 tbody（脚本注入 UI 不在原始 HTML，无干扰）
    tb_m = re.search(r'<table[^>]*class="[^"]*account_table[^"]*"[^>]*>(.*?)</table>', page_html, re.DOTALL)
    if not tb_m:
        return []
    rows: list[dict] = []
    for row_m in _LICENSE_ROW_RE.finditer(tb_m.group(1)):
        row_html = row_m.group(1)
        if "<th" in row_html:
            continue
        date_m = _LICENSE_DATE_RE.search(row_html)
        acq_m = _LICENSE_ACQ_RE.search(row_html)
        if not date_m or not acq_m:
            continue
        date = _strip_tags(date_m.group(1))
        if not date or not any(ch.isdigit() for ch in date):
            continue  # 表头兜底（分类器 isHdr）
        # 名称列：排除 date/acquisition/steamdb 的首个 td
        name_html = ""
        for td_m in re.finditer(r"<td\b[^>]*>(.*?)</td>", row_html, re.DOTALL):
            cls_m = re.search(r'class="([^"]*)"', td_m.group(0))
            cls = cls_m.group(1) if cls_m else ""
            if any(
                key in cls
                for key in ("license_date_col", "license_acquisition_col", "steamdb_license_id_col")
            ):
                continue
            name_html = td_m.group(1)
            break
        # 剥「移除」按钮文字（分类器 free_license_remove_link 同款）
        name_html = re.sub(
            r'<a[^>]*free_license_remove_link[^>]*>.*?</a>', "", name_html, flags=re.DOTALL
        )
        # 许可 appid：名称列的商店链接（/app/{id}）——前端与家庭库做精确关联的锚；
        # 无链接的行（sub 通行证 / 文案变体）为 None，前端退回名字粗匹配
        appid_m = re.search(r"/app/(\d+)", name_html)
        item = _strip_tags(name_html)
        method = _strip_tags(acq_m.group(1))
        if any(
            kw in item
            for kw in ("未找到许可", "找不到許可", "No licenses found", "No license found")
        ):
            continue
        if date and item:
            rows.append(
                {
                    "date": date,
                    "item": item,
                    "method": method,
                    "appid": int(appid_m.group(1)) if appid_m else None,
                }
            )
    return rows


def extract_next_page_url(page_html: str) -> str | None:
    """licenses 分页器 next 链接（无 → 最后一页）。"""
    m = _PAGINATOR_NEXT_RE.search(page_html)
    return m.group(1) if m else None


# ── 抓取主体 ────────────────────────────────────────────────────

_ACCOUNT_PULLDOWN_RE = re.compile(r'id="account_pulldown"[^>]*>([^<]*)<')


# 头像域名字典：akamaized（旧）/（fastly.）steamstatic（现行）
# ——账号页已迁 fastly.steamstatic.com，旧正则只认 akamaized
# 导致 auto-sync 头像恒为空（昵称能抓到、头像静默跳过）
_AVATAR_URL_RE = re.compile(r'<img[^>]*src="(https://avatars\.[a-z.]+/[^"]+)"')


def _avatar_mime(url: str) -> str:
    return "image/png" if url.lower().endswith(".png") else "image/jpeg"


async def _fetch_account_identity(client: httpx.AsyncClient) -> tuple[str, str]:
    """nickname + avatar base64（store account 页；公开通道，失败不阻断）。"""
    nickname = ""
    avatar = ""
    try:
        resp = await _get_html(client, ACCOUNT_URL)
        html = resp.text
        m = _ACCOUNT_PULLDOWN_RE.search(html)
        if m:
            nickname = _strip_tags(m.group(1))
        # 头像：页内首个头像 img（即本人 full 尺寸）→ 下载转 base64
        avatar_url_m = _AVATAR_URL_RE.search(html)
        if avatar_url_m:
            avatar_url = avatar_url_m.group(1)
            avatar_resp = await client.get(avatar_url, headers=_HEADERS)
            if avatar_resp.status_code == 200:
                import base64

                avatar = (
                    f"data:{_avatar_mime(avatar_url)};base64,"
                    + base64.b64encode(avatar_resp.content).decode("ascii")
                )
    except Exception as exc:  # noqa: BLE001
        logger.info("账单抓取：账户身份提取失败（不阻断）：%s", exc)
    return nickname, avatar


async def _emit_progress(on_progress: ProgressCallback | None, **info) -> None:
    """进度上报：回调异常只降级为日志，绝不拖垮同步本体。"""
    if on_progress is None:
        return
    try:
        await on_progress(info)
    except Exception:  # noqa: BLE001
        logger.debug("账单同步进度回调失败", exc_info=True)


async def _fetch_history(
    client: httpx.AsyncClient, on_progress: ProgressCallback | None = None
) -> list[dict]:
    """history 首屏 + deep-form 翻页（fetchHistory 同构口径）。

    Steam 不预告总页数（游标驱动），故进度只有「第 N 页 · 已获 M 条」
    的实数，没有百分比——上报给快照供前端实时气泡显示。
    """
    resp = await _get_html(client, HISTORY_URL)
    _ensure_account_page(resp, "消费明细页")
    html = resp.text
    rows: list[dict] = []
    tb_m = re.search(
        r'<table[^>]*class="[^"]*wallet_history_table[^"]*"[^>]*>(.*?)</table>', html, re.DOTALL
    )
    if tb_m:
        rows.extend(parse_history_rows_from_html(tb_m.group(1)))
    await _emit_progress(on_progress, stage="history", pages=0, rows=len(rows))

    cursor_match = _CURSOR_RE.search(html)
    cursor: dict | None = None
    if cursor_match:
        try:
            import json as _json

            cursor = _json.loads(cursor_match.group(1))
        except ValueError:
            cursor = None
    session_id = client.cookies.get("sessionid", "")

    pages = 0
    while cursor:
        pages += 1
        if pages > _MAX_PAGES:
            logger.warning("账单抓取：history 翻页达安全上限 %d，提前停止", _MAX_PAGES)
            break
        form = {"sessionid": session_id}
        for key, value in cursor.items():
            form[f"cursor[{key}]"] = str(value)
        resp = await client.post(
            HISTORY_MORE_URL,
            data=form,
            headers={**_HEADERS, "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
        )
        resp.raise_for_status()
        payload = resp.json()
        html_fragment = payload.get("html") or ""
        next_cursor = payload.get("cursor") or None
        if html_fragment:
            rows.extend(parse_history_rows_from_html(html_fragment))
            await _emit_progress(on_progress, stage="history", pages=pages, rows=len(rows))
        # 防重投喂/死循环：响应游标与请求相同 = Steam 原样重发 → 停
        if next_cursor and next_cursor == cursor:
            logger.warning("账单抓取：history 游标未推进（重投喂），停止翻页")
            break
        cursor = next_cursor
        if not html_fragment:
            break
        import asyncio

        await asyncio.sleep(_PAGE_DELAY)

    # 不做行级去重：同日多笔相同交易（市场批量/连续充值）是真实数据，
    # 四元组合并会错杀（实测 531 行中 163 行页内真实重复、页间零重叠）。
    # 翻页侧防重投喂由 cursor 链保证：响应 cursor 与请求相同 → 停（防死循环）。
    return rows


async def probe_history_first_screen(
    client: httpx.AsyncClient, known_latest_date: str | None
) -> bool:
    """首屏探测：history 最新一行日期 > 库内最新日期 → 有新交易，值得全量拉。

    Steam history 页倒序排列，首屏第一行即最新交易——一次 GET 就能判断
    是否有必要跑全量翻页（无新交易时整轮同步从数十页请求缩到 1 个）。

    返回 True = 需要全量；False = 无新交易可跳过。无法判断（解析失败/
    首行为空）时保守返回 True（宁可多拉不漏账）。
    """
    if not known_latest_date:
        return True  # 库里没账单：首次拉取语义
    try:
        resp = await _get_html(client, HISTORY_URL)
    except Exception as exc:  # noqa: BLE001 探测失败不阻断主流程
        logger.info("账单探测：history 首屏拉取失败（按需全量处理）：%s", exc)
        return True
    try:
        _ensure_account_page(resp, "消费明细页")
    except SteamFetchError:
        raise  # Cookie 失效信号要冒泡给主流程（不是"无新交易"）
    tb_m = re.search(
        r'<table[^>]*class="[^"]*wallet_history_table[^"]*"[^>]*>(.*?)</table>', resp.text, re.DOTALL
    )
    if not tb_m:
        return True  # 页面结构异常：保守全量
    rows = parse_history_rows_from_html(tb_m.group(1))
    if not rows:
        return True  # 解析不到行（或库里有但 Steam 侧清空）：保守全量
    from .parser import parse_date_cn

    first_date = parse_date_cn(rows[0].get("date", ""))
    logger.info(
        "账单探测：Steam 最新交易 %s vs 库内最新 %s → %s",
        first_date or "?", known_latest_date,
        "发现新交易，全量拉取" if first_date > known_latest_date else "无新交易，跳过",
    )
    return first_date > known_latest_date


async def _fetch_licenses(client: httpx.AsyncClient) -> list[dict]:
    """licenses 全量翻页（沿 license_paginator_next；旧版漏翻页，这里补全）。"""
    resp = await _get_html(client, LICENSES_URL)
    _ensure_account_page(resp, "游戏入库方式页")
    rows = parse_license_rows_from_html(resp.text)
    next_url = extract_next_page_url(resp.text)
    pages = 1
    while next_url:
        pages += 1
        if pages > _MAX_PAGES:
            logger.warning("账单抓取：licenses 翻页达安全上限 %d", _MAX_PAGES)
            break
        # 分页器 href 是相对路径（/?continuationToken=...）——对 licenses 页绝对化
        from urllib.parse import urljoin

        url = urljoin(LICENSES_URL, next_url)
        resp = await _get_html(client, url)
        page_rows = parse_license_rows_from_html(resp.text)
        if not page_rows:
            break
        rows.extend(page_rows)
        next_url = extract_next_page_url(resp.text)
        import asyncio

        await asyncio.sleep(_PAGE_DELAY)
    logger.info("账单抓取：licenses 共 %d 页 %d 条", pages, len(rows))
    return rows


async def fetch_full_report(
    cookies_raw: str,
    *,
    verify: bool | None = None,
    proxy_url: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> SteamReportData:
    """拉全量账单 + 许可（同构 JSON）。供 Cookie 绑定/定时任务/手动同步调用。

    verify 语义与 steam_wallet.fetch_wallet 一致：默认严格校验，
    SSL 失败（本机加速器场景）降级跳过校验重试一次。
    on_progress：阶段进度回调（identity / history[pages,rows] / licenses），
    供上层写快照驱动前端实时气泡；回调异常在 _emit_progress 内降级。
    """
    if "steamLoginSecure" not in parse_cookie_str(cookies_raw).keys():
        raise WalletFetchError("Steam Cookie 未配置或缺少 steamLoginSecure")

    cookies = parse_cookie_str(cookies_raw)
    cookies.setdefault("birthtime", "283993201")
    cookies.setdefault("wants_mature_content", "1")

    async def _run(verify_flag: bool) -> SteamReportData:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, verify=verify_flag, follow_redirects=True,
            proxy=proxy_url, cookies=cookies,
        ) as client:
            nickname, avatar = await _fetch_account_identity(client)
            await _emit_progress(on_progress, stage="identity")
            history = await _fetch_history(client, on_progress)
            licenses = await _fetch_licenses(client)
            await _emit_progress(on_progress, stage="licenses")
            if not nickname:
                # 身份兜底：history 页账户名（账户级 cookie 必有）
                nickname = f"Steam 账户 {cookies.get('steamLoginSecure', '')[:17]}"[:40]
            return SteamReportData(
                nickname=nickname or "Steam 账户",
                avatar_base64=avatar,
                history=history,
                licenses=licenses,
            )

    if verify is not None:
        try:
            return await _run(verify)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            raise SteamFetchError(str(exc)) from exc
    try:
        return await _run(True)
    except (httpx.HTTPStatusError, httpx.RequestError, SteamFetchError) as exc:
        if not _has_ssl_error(str(exc)):
            raise SteamFetchError(str(exc)) from exc
        logger.info("账单抓取：Steam 证书校验失败（疑似本机加速器），降级跳过校验重试")
        try:
            return await _run(False)
        except (httpx.HTTPStatusError, httpx.RequestError) as exc2:
            raise SteamFetchError(str(exc2)) from exc2


async def probe_new_transactions(
    cookies_raw: str, *, known_latest_date: str | None, proxy_url: str | None = None
) -> bool:
    """账单首屏探测（对外出口）：True = 有新交易值得全量拉，False = 可跳过。

    verify 语义与 fetch_full_report 一致：默认严格校验，SSL 失败（本机
    加速器场景）降级跳过校验重试一次。Cookie 失效抛 SteamFetchError
    （登录页守门冒泡），由调用方写错误快照。
    """
    if "steamLoginSecure" not in parse_cookie_str(cookies_raw).keys():
        raise WalletFetchError("Steam Cookie 未配置或缺少 steamLoginSecure")

    cookies = parse_cookie_str(cookies_raw)
    cookies.setdefault("birthtime", "283993201")
    cookies.setdefault("wants_mature_content", "1")

    async def _probe(verify_flag: bool) -> bool:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT, verify=verify_flag, follow_redirects=True,
            proxy=proxy_url, cookies=cookies,
        ) as client:
            return await probe_history_first_screen(client, known_latest_date)

    try:
        return await _probe(True)
    except (httpx.HTTPStatusError, httpx.RequestError, SteamFetchError) as exc:
        if not _has_ssl_error(str(exc)):
            raise
        logger.info("账单探测：Steam 证书校验失败（疑似本机加速器），降级跳过校验重试")
        return await _probe(False)
