"""bills 域：Steam 完整账单分析（解析器）。

解析 Steam_Report_*.json（Steam 消费历史导出件）：中英文双语、货币符号解析、
钱包充值/退款识别、去重、退款↔原单回溯匹配（orig_is_gift 归属）。
`server/tests/test_bills.py` 以真实转储为对账基准，故行为不可随意变动。
"""
from __future__ import annotations

import re
from collections import defaultdict

# ── 货币符号 → ISO（按符号长度降序，防止 "$" 抢先匹配 "CDN$"）──
SYMBOL_MAP = sorted(
    [
        ("CDN$", "CAD"),
        ("MX$", "MXN"),
        ("HK$", "HKD"),
        ("NT$", "TWD"),
        ("NZ$", "NZD"),
        ("A$", "AUD"),
        ("S$", "SGD"),
        ("R$", "BRL"),
        ("RM", "MYR"),
        ("Rp", "IDR"),
        ("CHF", "CHF"),
        ("AED", "AED"),
        ("QAR", "QAR"),
        ("QR", "QAR"),
        ("KD", "KWD"),
        ("SR", "SAR"),
        ("lei", "RON"),
        ("kr", "NOK"),
        ("Ft", "HUF"),
        ("Kč", "CZK"),
        ("zł", "PLN"),
        ("฿", "THB"),
        ("₫", "VND"),
        ("₩", "KRW"),
        ("€", "EUR"),
        ("£", "GBP"),
        ("¥", "CNY"),
        ("₪", "ILS"),
        ("₡", "CRC"),
        ("₸", "KZT"),
        ("₽", "RUB"),
        ("₴", "UAH"),
        ("₺", "TRY"),
        ("₹", "INR"),
        ("₱", "PHP"),
        ("CLP$", "CLP"),
        ("COL$", "COP"),
        ("$U", "UYU"),
        ("S/.", "PEN"),
        ("R ", "ZAR"),
        ("руб.", "RUB"),
        ("руб", "RUB"),
        ("TL", "TRY"),
        ("$ USD", "USD"),
        ("$", "USD"),
    ],
    key=lambda x: -len(x[0]),
)

GAME_TYPES = {
    # 中文
    "购买", "礼物购买", "游戏内购买", "退款",
    # 英文
    "Purchase", "Gift Purchase", "In-Game Purchase", "Refund",
}

# ── 类型规范化映射（英文 → 中文）──
TX_TYPE_NORM: dict[str, str] = {
    "Purchase":          "购买",
    "Gift Purchase":     "礼物购买",
    "In-Game Purchase":  "游戏内购买",
    "Refund":            "退款",
    # 市场/转换（用于过滤）
    "Market Transaction":        "市场交易",
    "Conversion":                "转换",
}

# 批量市场交易（如 "6 Market Transactions"）
_MARKET_BATCH_RE = re.compile(r"^\d+\s+Market\s+Transactions?$", re.I)

# ── 类型识别正则（与 Steam 消费历史分类器 classifyRow 同源，中英繁兼容）──
# 分类器原文顺序：refund → convert → ingame → market → gift → purchase
RE_TYPE = {
    "refund": re.compile(r"退款|Refund", re.I),
    "convert": re.compile(r"转换|轉換|Convert", re.I),
    "ingame": re.compile(r"游戏内(?:物品)?购买|遊戲內物品購買|In-Game\s*Purchase", re.I),
    "market": re.compile(r"市场交易|市集交易|Market\s*Transaction", re.I),
    "gift": re.compile(r"礼物购买|禮物購買|Gift\s*Purchase", re.I),
    "purchase": re.compile(r"购买|購買|Purchase", re.I),
    "wallet_pay": re.compile(r"钱包资金|錢包資金|Wallet", re.I),
}


def classify_type(tx_type: str, item_str: str = "", wallet_change: str = "") -> str:
    """分类器同源的类型判定（正则模糊匹配，Steam UI 文案变化时比精确匹配稳健）。

    返回语义类型：refund / convert / ingame / market_buy / market_sell /
    gift / store / other。market 按钱包变更正负分买入/卖出（分类器同款）。
    """
    text = tx_type or ""
    if not text:
        return "other"
    if RE_TYPE["refund"].search(text):
        return "refund"
    if RE_TYPE["convert"].search(text):
        return "convert"
    if RE_TYPE["ingame"].search(text):
        return "ingame"
    if RE_TYPE["market"].search(text):
        if "+" in (wallet_change or ""):
            return "market_sell"
        if "-" in (wallet_change or ""):
            return "market_buy"
        return "market_buy"  # 无变更信息时按买入（分类器判 other，但买入对账侧更合理）
    if RE_TYPE["gift"].search(text):
        return "gift"
    if RE_TYPE["purchase"].search(text):
        # 分类器：items 文本含钱包资金关键词 → 充值（convert）
        if RE_TYPE["wallet_pay"].search(item_str):
            return "convert"
        return "store"
    return "other"

# ── 许可分类（商店 / 零售 / 免费 / 礼物四类）──
# 四类关键词（正则，命中即归类，顺序：store → retail → free → gift）：
#   store  Steam 商店 / Steam Store
#   retail 零售 / Retail
#   free   免费赠送 / 贄品 / Complimentary
#   gift   礼物 / 玩家通行证 / 禮物 / 招待券 / Gift / Guest Pass
_LICENSE_CATS: list[tuple[str, re.Pattern, str]] = [
    ("store", re.compile(r"Steam\s*商店|Steam\s*Store", re.I), "Steam 商店"),
    ("retail", re.compile(r"零售|Retail", re.I), "零售"),
    ("free", re.compile(r"免费赠送|贈品|Complimentary", re.I), "免费赠送"),
    ("gift", re.compile(r"礼物|玩家通行证|禮物|招待券|Gift|Guest\s*Pass", re.I), "礼物/玩家通行证"),
]


def classify_license(method_raw: str) -> tuple[str, str]:
    """许可获取方式 → (分类 id, 展示名)。未命中 → ("other", 原文)。

    分类器 MATCH_CFG 同款正则兜底：Steam 文案变体（如「Complimentary」
    大小写、Guest Pass 全角空格）不再漏分类。
    """
    text = normalize_text(method_raw)
    for cat, pattern, label in _LICENSE_CATS:
        if pattern.search(text):
            return cat, label
    return "other", text


# 兼容旧出口：store/other 之外的许可都落库（免费只展示不计价，store 类
# 本来就有账单流水、无需重复建行）。

_EN_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def _num(text: str) -> float | None:
    text = (text or "").strip()
    text = re.sub(r",-+\s*$", ".00", text)
    text = text.replace(" ", "").replace("\xa0", "").replace("\u202f", "")
    if "," in text and "." not in text:
        parts = text.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            text = parts[0] + "." + parts[1]
        else:
            text = text.replace(",", "")
    else:
        text = text.replace(",", "")
    text = re.sub(r"[^\d.]", "", text)
    try:
        return float(text) if text else None
    except Exception:
        return None


def parse_money(text: str) -> tuple[float | None, str | None]:
    """解析含货币符号的金额串 → (数值, ISO)。兼容 "960,70₸ 资金" / "$0.02 USD Credit"。"""
    if not text:
        return None, None
    text = str(text).replace("\xa0", " ").replace("\u202f", " ").strip()
    text = re.sub(r"\s*钱包资金\s*$", "", text)
    sign = -1.0 if text.startswith("-") else 1.0
    text = text.lstrip("+-").strip()
    # "$5.99 USD" / "20,--€" 之外的 "Credit" 后缀（不剥 "钱包资金"，见上）
    text = re.sub(r"\s*Credit$", "", text)
    for sym, code in SYMBOL_MAP:
        esc_sym = re.escape(sym)
        match = re.match(rf"^{esc_sym}\s*([\d\s,.\-]+)", text) or re.match(
            rf"^([\d\s,.\-]+)\s*{esc_sym}", text
        )
        if not match:
            continue
        value = _num(match.group(1))
        if value is not None:
            return sign * value, code
    return None, None


def parse_date_cn(text: str) -> str:
    """多种日期格式统一为 YYYY-MM-DD（中文 / 英文 / ISO）。"""
    text = str(text or "").strip()
    match = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return match.group(0)
    match = re.match(r"(\d{1,2})\s+([A-Za-z]{3}),?\s+(\d{4})", text)
    if match:
        day = int(match.group(1))
        mon = _EN_MONTHS.get(match.group(2).capitalize(), 0)
        year = int(match.group(3))
        if mon:
            return f"{year}-{mon:02d}-{day:02d}"
    return text


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


# Steam 新 UI：礼物行 item 形如
# "A 礼物已发送给 X B 礼物已发送给 Y 退款"（受赠人并入物品文本，受赠人名
# 可含空格/任意字符，退款行再叠 " 退款" 尾缀）。
# 受赠人段边界 = 下一个「礼物已发送给」标记或行尾（末段先剥退款尾缀再取）。
_GIFT_SENT_SPLIT = "礼物已发送给"
_REFUND_TAIL_RE = re.compile(r"\s*退款\s*$")


def strip_gift_marks(item_str: str) -> tuple[str, list[str]]:
    """剥离新 UI 的礼物/退款文本标记，返回 (净化 item, 受赠人列表)。

    礼物行: "A 礼物已发送给 X B 礼物已发送给 Y 退款" → ("A B", ["X", "Y"])
    退款行: "Dead Estate 退款" → ("Dead Estate", [])（尾缀在 type 列已有）
    旧格式（无标记）原样返回。
    """
    text = item_str or ""
    if _GIFT_SENT_SPLIT not in text:
        # 无礼物标记：仍需剥退款尾缀（新 UI 退款行 item 末尾带 " 退款"）
        cleaned = _REFUND_TAIL_RE.sub("", text)
        return normalize_text(cleaned), []
    # 末段的「退款」尾缀先剥（只影响最后一位受赠人名的截取）
    text = _REFUND_TAIL_RE.sub("", text)
    parts = text.split(_GIFT_SENT_SPLIT)
    cleaned = parts[0]
    recipients = []
    for seg in parts[1:]:
        # 受赠人名 = 本段全文（到下一个标记之间），保留内部空格
        name = seg.strip()
        recipients.append(name)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,、")
    return cleaned, recipients


def split_items(item_str: str) -> list[str]:
    clean = normalize_text(item_str)
    if not clean:
        return []
    items = [part.strip() for part in re.split(r"[,\n，]+", clean) if part.strip()]
    return items or [clean]


def build_summary_key(item_str: str, items: list[str]) -> str:
    base = " | ".join(items) if items else normalize_text(item_str)
    return re.sub(r"\s+", " ", base).strip().lower()


def is_wallet_keyword(item_str: str) -> bool:
    """识别钱包充值关键词（中英文兼容）。"""
    text = normalize_text(item_str).lower()
    # 中文
    if "钱包资金" in text or ("钱包" in text and ("资金" in text or "wallet" in text)):
        return True
    # 英文: "Purchased $XX Wallet Credit" / "XX Wallet Credit"
    if "wallet credit" in text or "wallet fund" in text:
        return True
    # 英文 item 以 "Purchased" 开头且含 "Credit"（充值场景）
    if text.startswith("purchased") and "credit" in text:
        return True
    return False


def parse_discount_pct(raw) -> str | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return f"{raw * 100:.0f}%" if -1.0 <= raw <= 1.0 else f"{raw:.0f}%"
    return str(raw)


def parse_original_price(raw) -> float | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    parsed, _ = parse_money(str(raw))
    if parsed is not None:
        return abs(parsed)
    return _num(str(raw))


def parse_report(data: dict) -> dict:
    """解析外部工具导出的 Steam_Report_*.json。

    返回 {account, game_txs, topup_txs, cdk_games, warnings}：
    - game_txs：游戏交易（购买/礼物购买/内购/退款，去重 + 退款匹配后）
    - topup_txs：钱包充值/充值退款流水
    - cdk_games：零售 CDK / 礼物入库（手动计价用）
    - warnings：混合订单告警（无法拆分单价，按全额赠礼计）
    """
    account = data.get("account", {}) or {}
    history_raw = data.get("history", []) or []
    licenses_raw = data.get("licenses", []) or []

    game_candidates: list[dict] = []
    topup_txs: list[dict] = []
    warnings: list[str] = []

    for idx, entry in enumerate(history_raw):
        tx_type = normalize_text(entry.get("type", ""))
        item_str = normalize_text(entry.get("item", ""))
        total_str = normalize_text(entry.get("total", ""))
        payment = normalize_text(entry.get("payment", ""))
        wallet_change = normalize_text(entry.get("wallet_change", ""))
        wallet_balance = normalize_text(entry.get("wallet_balance", ""))
        base_price = normalize_text(entry.get("base_price", ""))
        payment_parts = entry.get("payment_parts") or []
        date_raw = normalize_text(entry.get("date", ""))
        date = parse_date_cn(date_raw)

        # 英文类型规范化为中文；正则模糊兜底（分类器 classifyRow 同源）——
        # Steam UI 文案微调（如「游戏内物品购买」）不再漏分类
        tx_type_raw = tx_type
        tx_type = TX_TYPE_NORM.get(tx_type, tx_type)
        if tx_type not in GAME_TYPES:
            sema = classify_type(tx_type_raw, item_str, wallet_change)
            if sema in ("refund", "convert", "ingame", "gift", "store") and not (
                sema == "convert" and not RE_TYPE["wallet_pay"].search(item_str)
            ):
                tx_type = {
                    "refund": "退款",
                    "convert": "转换",
                    "ingame": "游戏内购买",
                    "gift": "礼物购买",
                    "store": "购买",
                }[sema]
        # 过滤批量市场交易（如 "6 Market Transactions"）——
        # 新导出格式带 type_count（批量行笔数），旧格式仅 type 前缀数字
        if _MARKET_BATCH_RE.match(normalize_text(entry.get("type", ""))):
            continue
        if "市场交易" in tx_type or tx_type in ("转换", "Conversion"):
            continue

        amount, currency = parse_money(total_str)
        amount_abs = abs(amount) if amount is not None else None
        # 新 UI：礼物行 item 含「礼物已发送给 X」内嵌段、退款行带「 退款」尾缀。
        # 分类判定用**原始文本**（含标记），展示/对账键用**净化文本**（去标记后）。
        # 受赠人优先取导出的独立字段（DOM 层提取，名字可含空格），
        # 无字段时文本剥离兜底（旧格式/异常数据）。
        clean_item, text_recipients = strip_gift_marks(item_str)
        gift_recipients = [
            normalize_text(r) for r in (entry.get("gift_recipients") or []) if normalize_text(r)
        ] or text_recipients
        items = split_items(clean_item)
        summary_key = build_summary_key(clean_item, items)
        wallet_kw = is_wallet_keyword(item_str)

        is_wallet_recharge = (
            (tx_type == "购买" and wallet_kw)
            or (not tx_type and (amount_abs or 0) > 0)
        )
        # 新 UI 充值行 total / wallet_change 均空，金额在 base_price 列
        # （「已购买 A$ 35.00 钱包资金」，DOM 的 wht_base_price td = "A$ 35.00"）。
        # 金额来源优先级：total > wallet_change > base_price。
        if is_wallet_recharge and amount_abs is None:
            for source in (wallet_change, base_price):
                wc_amount, wc_currency = parse_money(source)
                if wc_amount is not None:
                    amount_abs = abs(wc_amount)
                    if not currency:
                        currency = wc_currency
                    break
        if is_wallet_recharge and (amount_abs or 0) <= 0:
            is_wallet_recharge = False
        is_wallet_refund = tx_type == "退款" and wallet_kw
        if is_wallet_refund and amount_abs is None:
            for source in (wallet_change, base_price):
                wc_amount, wc_currency = parse_money(source)
                if wc_amount is not None:
                    amount_abs = abs(wc_amount)
                    if not currency:
                        currency = wc_currency
                    break
        if is_wallet_refund and (amount_abs or 0) <= 0:
            is_wallet_refund = False
        if is_wallet_recharge or is_wallet_refund:
            final_amount = amount_abs or 0.0
            final_currency = currency or "CNY"
            if final_amount == 0.0 or final_currency is None:
                for source in (wallet_change, base_price):
                    wc_amount2, wc_currency2 = parse_money(source)
                    if wc_amount2:
                        final_amount = abs(wc_amount2)
                        final_currency = wc_currency2 or final_currency
                        break
            topup_txs.append(
                {
                    "raw_idx": idx,
                    "date": date,
                    "desc": item_str or tx_type or "钱包充值",
                    "amount": final_amount,
                    "sign": -1 if is_wallet_refund else 1,
                    "currency": final_currency,
                    "payment": payment,
                    "tx_type": "充值退款" if is_wallet_refund else "钱包充值",
                    "is_refund": is_wallet_refund,
                }
            )
            continue

        # tx_type 已被规范化为中文，GAME_TYPES 包含中文值
        if tx_type not in GAME_TYPES:
            continue

        if amount_abs is None or amount_abs <= 0:
            continue

        is_refund = tx_type == "退款"
        is_gift_sent = tx_type == "礼物购买" and not is_refund and amount_abs > 0

        game_candidates.append(
            {
                "raw_idx": idx,
                "date": date,
                "tx_type": tx_type,
                "payment": payment,
                "items": items,
                "summary_key": summary_key,
                "amount": amount_abs,
                "sign": -1 if is_refund else 1,
                "currency": currency or "CNY",
                "discount_pct": parse_discount_pct(entry.get("discount")),
                "orig_price": parse_original_price(entry.get("original_price")),
                # 新导出字段（分类器对齐）：余额列/基准价/混合支付拆分
                "wallet_balance": wallet_balance or None,
                "base_price": base_price or None,
                "payment_parts": payment_parts or None,
                "is_gift": is_gift_sent,
                "is_refund": is_refund,
                "orig_is_gift": False,
                # 新 UI 礼物行的受赠人（可能多个）；旧导出为空列表
                "gift_recipients": gift_recipients,
                "warn_mixed": is_gift_sent and len(items) > 1,
                "dedupe_key": (date, round(amount_abs, 4), summary_key),
            }
        )

    grouped: dict[tuple, list[dict]] = defaultdict(list)
    for row in game_candidates:
        grouped[row["dedupe_key"]].append(row)

    deduped_rows: list[dict] = []
    for _, rows in grouped.items():
        rows.sort(key=lambda x: x["raw_idx"])
        refunds = [row for row in rows if row["is_refund"]]
        purchases = [row for row in rows if not row["is_refund"]]

        chosen_purchase = None
        if purchases:
            gift_rows = [row for row in purchases if row["is_gift"]]
            normal_rows = [row for row in purchases if not row["is_gift"]]
            chosen_purchase = (gift_rows[0] if gift_rows else normal_rows[0]).copy()
            deduped_rows.append(chosen_purchase)

        if refunds:
            chosen_refund = refunds[0].copy()
            if chosen_purchase is not None:
                chosen_refund["orig_is_gift"] = chosen_purchase["is_gift"]
            deduped_rows.append(chosen_refund)

    purchase_pool: dict[tuple, list[dict]] = defaultdict(list)
    for row in sorted(
        [row for row in deduped_rows if not row["is_refund"]],
        key=lambda x: (x["date"], x["raw_idx"]),
    ):
        purchase_pool[(round(row["amount"], 4), row["summary_key"])].append(row)

    for row in sorted(
        [row for row in deduped_rows if row["is_refund"]],
        key=lambda x: (x["date"], x["raw_idx"]),
    ):
        key = (round(row["amount"], 4), row["summary_key"])
        bucket = purchase_pool.get(key, [])
        match_index = None
        for idx in range(len(bucket) - 1, -1, -1):
            if bucket[idx]["date"] <= row["date"]:
                match_index = idx
                break
        if match_index is None and bucket:
            match_index = len(bucket) - 1
        if match_index is not None:
            matched = bucket.pop(match_index)
            row["orig_is_gift"] = matched["is_gift"]

    game_txs: list[dict] = []
    for row in sorted(deduped_rows, key=lambda x: (x["date"], x["raw_idx"]), reverse=True):
        if row["warn_mixed"] and row["is_gift"] and not row["is_refund"]:
            preview = row["items"][0] + ("等" if len(row["items"]) > 1 else "")
            warnings.append(
                f"混合订单（{row['date']} · {preview}）无法拆分单价，已按全额赠礼计算"
            )
        for key in ("date_raw", "item_str", "dedupe_key", "warn_mixed"):
            row.pop(key, None)
        game_txs.append(row)

    for row in topup_txs:
        row.pop("raw_idx", None)
    topup_txs.sort(key=lambda x: x["date"], reverse=True)

    cdk_games: list[dict] = []
    license_stats: dict[str, int] = {"store": 0, "retail": 0, "free": 0, "gift": 0, "other": 0}
    cdk_idx = 0
    for entry in licenses_raw:
        method_raw = normalize_text(entry.get("method", ""))
        # 分类器 MATCH_CFG 同源正则分类（文案变体兜底）；
        # free（免费赠送/Complimentary）一并落库——只展示不计价
        cat, method = classify_license(method_raw)
        license_stats[cat] = license_stats.get(cat, 0) + 1
        if cat not in ("retail", "gift", "free"):
            continue
        name = normalize_text(entry.get("item", ""))
        # appid：在线抓取路径由 steam_fetch 从名称列商店链接提取（sub 行为
        # None）；外部导出器无此字段时同样 None，前端退回名字粗匹配
        try:
            appid = int(entry.get("appid")) if entry.get("appid") else None
        except (TypeError, ValueError):
            appid = None
        cdk_games.append(
            {
                "id": cdk_idx,
                "name": name,
                "date": parse_date_cn(entry.get("date", "")),
                "acq": "cdk" if cat == "retail" else cat,
                "acq_label": method,
                "appid": appid,
            }
        )
        cdk_idx += 1

    return {
        "account": {
            "nickname": normalize_text(account.get("nickname", "")),
            "avatar_base64": str(account.get("avatar_base64", "") or ""),
        },
        "game_txs": game_txs,
        "topup_txs": topup_txs,
        "cdk_games": cdk_games,
        "license_stats": license_stats,
        "warnings": warnings,
    }
