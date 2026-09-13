"""爬虫通用工具（时间处理与字符串清洗，标准库 zoneinfo）。"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .config import EDITION_DICT

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def get_beijing_time_obj() -> datetime:
    return datetime.now(BEIJING_TZ)


def strip_html_tags(text: str | None) -> str:
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text, flags=re.IGNORECASE)
    clean = re.sub(r"<.*?>", "", text)
    return clean.replace("\u00A0", " ").replace("&nbsp;", " ").strip()


def detect_chinese_support(supported_text: str | None) -> str:
    if not supported_text:
        return "无中文"
    text = supported_text.lower()
    if "简体" in text or "simplified chinese" in text:
        return "简体中文"
    if "繁体" in text or "traditional chinese" in text:
        return "繁体中文"
    return "无中文"


def parse_formatted_price(formatted_price: str | None, currency: str = "USD") -> int | None:
    """把 Steam 格式化价格串（"$53.99" / "¥ 398" / "29,102 ₸"）解析为分（int）。"""
    if not formatted_price:
        return None

    cleaned = re.sub(r"[^\d.,]", "", formatted_price)
    if not cleaned:
        return None

    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")

    if last_dot >= 0 and last_comma >= 0:
        # 两者都在 —— 靠后的那个是小数分隔符
        if last_dot > last_comma:
            value_str = cleaned.replace(",", "")
        else:
            value_str = cleaned.replace(".", "").replace(",", ".")
    elif last_dot >= 0:
        parts = cleaned.split(".")
        if len(parts) == 2 and len(parts[1]) <= 2:
            value_str = cleaned
        else:
            value_str = cleaned.replace(".", "")
    elif last_comma >= 0:
        parts = cleaned.split(",")
        if len(parts) == 2 and len(parts[1]) <= 2:
            value_str = cleaned.replace(",", ".")
        else:
            value_str = cleaned.replace(",", "")
    else:
        value_str = cleaned

    try:
        value = float(value_str)
    except Exception:
        return None

    return int(round(value * 100))


def is_gold_edition(package_name: str | None) -> bool:
    if not package_name:
        return False
    name_lower = package_name.lower()
    if "commercial license" in name_lower:
        return False
    return "gold edition" in name_lower or "黄金版" in package_name


def is_pacific_dst(now_utc: datetime | None = None) -> bool:
    """判断当前太平洋时间是否处于夏令时（PDT）。"""
    if now_utc is None:
        now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    year = now_utc.year
    march1 = datetime(year, 3, 1)
    days_to_first_sunday = (6 - march1.weekday()) % 7
    second_sunday_march = march1 + timedelta(days=days_to_first_sunday + 7)
    dst_start = second_sunday_march.replace(hour=2)
    nov1 = datetime(year, 11, 1)
    days_to_first_sunday = (6 - nov1.weekday()) % 7
    first_sunday_nov = nov1 + timedelta(days=days_to_first_sunday)
    dst_end = first_sunday_nov.replace(hour=2)
    return dst_start <= now_utc < dst_end


def get_steam_refresh_hour_beijing() -> int:
    """Steam 折扣刷新时间对应的北京时间小时数。"""
    return 1 if is_pacific_dst() else 2


def is_near_steam_refresh(window_minutes: int = 30) -> bool:
    """当前北京时间是否处于 Steam 折扣刷新窗口内。"""
    now_beijing = datetime.now(BEIJING_TZ)
    refresh_hour = get_steam_refresh_hour_beijing()
    refresh_time = now_beijing.replace(hour=refresh_hour, minute=0, second=0, microsecond=0)
    diff_seconds = abs((now_beijing - refresh_time).total_seconds())
    return diff_seconds <= window_minutes * 60


def contains_chinese(text: str | None) -> bool:
    if not text:
        return False
    return bool(re.search(r"[\u4e00-\u9fff]", text))


# ── 价格段判定（41 区真实 option_text 尾巴全量枚举）──
# 实证形态全集（appid 620 逐区抓取，勿按记忆补——按这个清单对齐）：
#   ¥ 37.00 | 259 руб. | 1 850₸ | 169₴ | $5.49 USD | CLP$ 4.400 | P289.95
#   1.95 KD | 21.95 SR | R 79.00 | 24.99 QR | RM23.50 | ฿189.00 | S/.22.00
#   Mex$ 113.99 | S$10.00 | 29.00 AED | $U229 | COL$ 18.500 | ₩ 10,500
#   NZ$ 12.39 | 35,99 zł | ₡4.600 | CDN$ 11.49 | 8,19€ | 72,00 kr | Rp 69 999
#   120.000₫ | ₪36.95 | CHF 10.50 | HK$/NT$/A$/NZ$/R$ 变体 …
#   瑞士速记 "CHF 25.--"（728880 CH E2E 实证，-- = 无零头；判非价会被
#   strip(" -–—") 吃成 "CHF 25." 漏成版本名）
_PRICE_TAIL_TOKENS = (
    r"руб\.?|р\.|kr|zł|Kč|Ft|дин|lei|лв|kn|ман\.?|Rp|KD|SR|QR|RM|"
    r"P(?=\d)|R(?=[\s\d])|S/\.?|\$U|CHF"
    r"|USD|EUR|GBP|RUB|KZT|UAH|TRY|INR|BRL|IDR|JPY|TWD|HKD|SGD|CNY|CLP|COP|"
    r"PEN|AED|SAR|QAR|KWD|BHD|OMR|JOD|ILS|ZAR|MYR|THB|PHP|VND|NZD|AUD|CAD|"
    r"DKK|NOK|PLN|SEK|ARS|MXN|UYU|KRW|HUF"
    r"|[¥$€£₽₸₴₺₹₩₫₪₦₭₼₡฿]"
)
_PRICE_SEGMENT_RE = re.compile(
    rf"""^\s*
    (?:[A-Za-z]{{1,4}}\$|[A-Za-z]{{1,2}}(?=[\s\d/])|(?:{_PRICE_TAIL_TOKENS}))?\s*  # 前缀货币（可无）
    \d[\d.,\s]*-{{0,2}}                                   # 数字（千位空格/点/逗号；瑞士速记 "CHF 25.--"）
    (?:{_PRICE_TAIL_TOKENS})?\s*$               # 后缀货币（可无）
    """,
    re.VERBOSE | re.IGNORECASE,
)


def _is_price_segment(seg: str) -> bool:
    """段是否为价格（含数字且带货币标记）。纯数字段不算——防误伤版本名。"""
    if not seg or not any(ch.isdigit() for ch in seg):
        return False
    if not _PRICE_SEGMENT_RE.match(seg):
        return False
    # 前后至少一个货币标记（符号/ISO/本地缩写；Unicode Sc 类兜底未知符号）
    if any(unicodedata.category(ch) == "Sc" for ch in seg):
        return True
    return bool(re.search(rf"(?:{_PRICE_TAIL_TOKENS})", seg, re.IGNORECASE))


def extract_version_suffix(option_text: str | None, name_en: str | None) -> str:
    if not option_text:
        return ""
    # 剥全部价格 span（旧逻辑只剥 original，打折态 final span 残留成第二段价格）
    cleaned = re.sub(r"<span[^>]*>.*?</span>", "", option_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = cleaned.replace("\u00A0", " ").replace("&nbsp;", " ").strip()
    # 按段剥价格尾巴（生产实证 "NAME - Edition - ¥ 448.00"；打折双价循环去）。
    # 旧逻辑 rsplit(" - ")[-1] 把价格串当包名，版本提取 100% 静默失效
    segments = [s.strip() for s in cleaned.split(" - ")]
    while len(segments) > 1 and _is_price_segment(segments[-1]):
        segments.pop()
    package_name = " - ".join(s for s in segments if s).strip()
    if not package_name:
        return ""
    if not name_en or package_name.lower() == name_en.lower():
        return ""
    if package_name.lower().startswith(name_en.lower() + " "):
        suffix = package_name[len(name_en) :].strip()
        return suffix.strip(" -–—").strip()
    if package_name.lower().startswith(name_en.lower() + " - "):
        suffix = package_name[len(name_en) + 3 :].strip()
        return suffix.strip(" -–—").strip()

    # 兜底：关键词搜索
    edition_patterns = sorted(EDITION_DICT.keys(), key=len, reverse=True)
    pkg_lower = package_name.lower()
    for edition_key in edition_patterns:
        if edition_key in pkg_lower:
            return " ".join(
                w.capitalize() if w.lower() not in ("of", "the") else w.lower()
                for w in edition_key.split()
            )
    if is_gold_edition(package_name):
        return "Gold Edition"
    return ""
