"""爬虫通用工具（时间处理与字符串清洗，标准库 zoneinfo）。"""
from __future__ import annotations

import re
import unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

from .config import EDITION_DICT

BEIJING_TZ = ZoneInfo("Asia/Shanghai")


def get_beijing_time_obj() -> datetime:
    return datetime.now(BEIJING_TZ)


def is_gold_edition(package_name: str | None) -> bool:
    if not package_name:
        return False
    name_lower = package_name.lower()
    if "commercial license" in name_lower:
        return False
    return "gold edition" in name_lower or "黄金版" in package_name


# ── 价格段判定（各区 option_text 尾巴形态）──
# 形态全集（勿按记忆补，按这个清单对齐）：
#   ¥ 37.00 | 259 руб. | 1 850₸ | 169₴ | $5.49 USD | CLP$ 4.400 | P289.95
#   1.95 KD | 21.95 SR | R 79.00 | 24.99 QR | RM23.50 | ฿189.00 | S/.22.00
#   Mex$ 113.99 | S$10.00 | 29.00 AED | $U229 | COL$ 18.500 | ₩ 10,500
#   NZ$ 12.39 | 35,99 zł | ₡4.600 | CDN$ 11.49 | 8,19€ | 72,00 kr | Rp 69 999
#   120.000₫ | ₪36.95 | CHF 10.50 | HK$/NT$/A$/NZ$/R$ 变体 …
#   瑞士速记 "CHF 25.--"（-- = 无零头；判非价会被
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


# 选项名尾巴的多语种标注（"(English/Chinese/Japanese/Korean)"，≥2 个斜杠
# 分隔的单词）是语言覆盖说明，不是版本名
_LANG_TAG_TAIL_RE = re.compile(r"\s*\([A-Za-z]+(?:\s*/\s*[A-Za-z]+)+\)\s*$")


def extract_version_suffix(option_text: str | None, name_en: str | None) -> str:
    if not option_text:
        return ""
    # 剥全部价格 span（只剥 original 会让打折态 final span 残留成第二段价格）
    cleaned = re.sub(r"<span[^>]*>.*?</span>", "", option_text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<br\s*/?>", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = cleaned.replace("\u00A0", " ").replace("&nbsp;", " ").strip()
    # 按段剥价格尾巴（生产样本 "NAME - Edition - ¥ 448.00"；打折双价循环去）。
    # 不能用 rsplit(" - ")[-1] 取包名——那会把价格串当包名，版本提取静默失效
    segments = [s.strip() for s in cleaned.split(" - ")]
    while len(segments) > 1 and _is_price_segment(segments[-1]):
        segments.pop()
    package_name = " - ".join(s for s in segments if s).strip()
    if not package_name:
        return ""
    if not name_en:
        return ""
    # 尾巴语言标注（"(English/Chinese/Japanese/Korean)" 式多语种斜杠列表）
    # 不是版本：比对两侧都剥掉——否则带 CJK 标注命名的本体被误判成版本款
    # 出局（三国志14 等）；返回的版本名保留原标注，历史标签不丢信息
    pkg_lower = _LANG_TAG_TAIL_RE.sub("", package_name).strip().lower()
    name_lower = _LANG_TAG_TAIL_RE.sub("", name_en).strip().lower()
    if pkg_lower == name_lower:
        return ""
    # 官方名 + 连接符（空格 / 半全角冒号）之后的片段即版本名。两种连接形态
    # 并存于 Steam 选项名，只认空格会让冒号连接的 sub 提取落空，被「无后缀」
    # 判成标准版混进标准版序列（加购 SKU 的低价点因此污染历史走势）
    for sep in (" ", ":", "："):
        if pkg_lower.startswith(name_lower + sep):
            return package_name[len(name_en) + len(sep) :].strip(" -–—:：").strip()

    # 兜底：关键词搜索
    edition_patterns = sorted(EDITION_DICT.keys(), key=len, reverse=True)
    for edition_key in edition_patterns:
        if edition_key in pkg_lower:
            return " ".join(
                w.capitalize() if w.lower() not in ("of", "the") else w.lower()
                for w in edition_key.split()
            )
    if is_gold_edition(package_name):
        return "Gold Edition"
    return ""


def clean_appids(values) -> list[int]:
    """入参规整：正整数、去重、保序（域边界批量入口共用单实现）。"""
    clean: list[int] = []
    seen: set[int] = set()
    for a in values or []:
        try:
            appid = int(a)
        except (TypeError, ValueError):
            continue
        if appid > 0 and appid not in seen:
            seen.add(appid)
            clean.append(appid)
    return clean
