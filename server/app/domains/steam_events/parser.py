"""Steamworks「Upcoming Steam Events」文档页解析器（纯函数，零 I/O）。

数据源是 Valve 面向开发者公开发布的活动日历文档页（无需登录）：
季节大促 / Steam Next Fest / 主题 Fest 的官方日期，日粒度、PT 时区口径。

解析按**内容级锚定**，不依赖标签层级与文本位置：
- 活动标题行扫全部 ``<h2>`` 块，文本放页首还是页尾不影响结果；
- 日期接受任意语序：``1 October, 2026`` / ``October 1, 2026`` /
  ``2026年10月1日`` / 共享月年的短尾形式（``June 14 - 21, 2027``、``- 21 日``）；
- 月名只认真实月份单词，避免把标题里的「Sale 2026」误读成日期。

``validate_events`` 是入库前的校验门：解析结果超出业务合理范围必须显式
报错，调用方据此保留旧数据，绝不把可疑结果静默写入。
"""
from __future__ import annotations

import html
import re
from datetime import date

# ── 标题行识别 ──────────────────────────────────────────────────────────
# 季节大促：英文章节词 + Sale，或中文「季节词 + 特卖」（标题年份可有可无，
# 历史版本出现过带年份与不带年份两种形态）
SEASON_NAME_RE = re.compile(
    r"(Spring|Summer|Autumn|Winter)\s+Sale|(春|夏|秋|冬)季?\s*特卖", re.IGNORECASE
)
NEXTFEST_NAME_RE = re.compile(r"Next\s*Fest|新品节", re.IGNORECASE)
# 含数字才可能是活动行（排除导航与纯说明文字）
_HAS_DIGIT_RE = re.compile(r"\d")

H2_RE = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
TABLE_RE = re.compile(r"<table\b[^>]*>(.*?)</table>", re.IGNORECASE | re.DOTALL)
ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
CELL_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)
SLUG_RE = re.compile(r"upcoming_events/(themed_sales/[a-z0-9_]+)", re.IGNORECASE)
# 表格年份标题：英文「2026 Fests」/ 中文「2026 年各游戏节」
YEAR_MARK_RE = re.compile(r"(\d{4})\s*年?\s*(?:Fests|各?游戏节)", re.IGNORECASE)
# 中文页导航/表格的节名锚文本黑名单（「更多信息」等链接文本不是节名）
_NAME_JUNK_RE = re.compile(
    r"^(?:更多信息|More info|View documentation|Registration details|注册详情)$", re.I
)

# ── 日期片段 ────────────────────────────────────────────────────────────
# 按具体度排序：年月日 > 月-日(/年) > 日-月(/年) > 日,年 > 中文短尾「日」
_MONTH_WORD = (
    r"(?:January|February|March|April|May|June|July|August"
    r"|September|October|November|December"
    r"|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
)
DATE_FRAG_RE = re.compile(
    r"20\d\d\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日"
    rf"|\d{{1,2}}\s+{_MONTH_WORD}(?:,?\s+20\d\d)?"
    rf"|{_MONTH_WORD}\s+\d{{1,2}}(?:,?\s+20\d\d)?"
    r"|\d{1,2},?\s+20\d\d"
    r"|\d{1,2}\s*日",
    re.IGNORECASE,
)

_MONTH_NAMES = (
    "January February March April May June July August "
    "September October November December"
).split()
MONTHS = {name: idx for idx, name in enumerate(_MONTH_NAMES, 1)}
_MONTH_LOOKUP: dict[str, int] = {name.lower(): idx for name, idx in MONTHS.items()}
_MONTH_LOOKUP.update({name[:3].lower(): idx for name, idx in _MONTH_LOOKUP.items()})

# 表格日期单元格两种语序都收：「Oct 12<br>Oct 19」或「12 Oct<br>19 Oct」
MD_RE = re.compile(
    rf"(?:(\d{{1,2}})\s+({_MONTH_WORD})|({_MONTH_WORD})\s+(\d{{1,2}}))",
    re.IGNORECASE,
)


def _strip_tags(fragment: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip()


def _frag_to_ymd(
    frag: str, carry_month: int | None, year_hint: int | None
) -> tuple[int, int, int] | None:
    """单个日期片段 → (year, month, day)；缺省部件用 0 占位。非日期片段返回 None。"""
    frag = frag.strip()
    m = re.fullmatch(r"(?:(20\d\d)\s*年)?\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", frag)
    if m:
        return (int(m.group(1) or year_hint or 0), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{1,2})\s+([A-Za-z]+)(?:,?\s+(20\d\d))?", frag)
    if m:
        month = _MONTH_LOOKUP.get(m.group(2).lower())
        if month is None:
            return None
        return (int(m.group(3) or year_hint or 0), month, int(m.group(1)))
    m = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2})(?:,?\s+(20\d\d))?", frag)
    if m:
        month = _MONTH_LOOKUP.get(m.group(1).lower())
        if month is None:
            return None
        return (int(m.group(3) or year_hint or 0), month, int(m.group(2)))
    m = re.fullmatch(r"(\d{1,2}),?\s+(20\d\d)", frag)
    if m:
        # 英文短尾「21, 2027」：日自带，月继承前文
        return (int(m.group(2)), carry_month or 0, int(m.group(1)))
    m = re.fullmatch(r"(\d{1,2})\s*日", frag)
    if m:
        # 中文短尾「21 日」：月/年继承前文
        return (year_hint or 0, carry_month or 0, int(m.group(1)))
    return None


def parse_date_range(text: str) -> tuple[date, date] | None:
    """从一段文本提取起止日期区间；语序无关，缺省部件就近继承。

    解析不出完整区间（片段不足 / 缺年缺月 / 起 > 止）返回 None。
    """
    frags = DATE_FRAG_RE.findall(text)
    if len(frags) < 2:
        return None
    years = re.findall(r"20\d\d", text)
    year_hint = int(years[-1]) if years else None
    carry_month: int | None = None
    parsed: list[tuple[int, int, int]] = []
    for frag in frags:
        ymd = _frag_to_ymd(frag, carry_month, year_hint)
        if ymd is None:
            return None
        parsed.append(ymd)
        if ymd[1]:
            carry_month = ymd[1]
    y1, m1, d1 = parsed[0]
    y2, m2, d2 = parsed[-1]
    if not (y1 and m1 and d1 and y2 and m2 and d2):
        return None
    try:
        start, end = date(y1, m1, d1), date(y2, m2, d2)
    except ValueError:
        return None
    return (start, end) if start <= end else None


def _table_days(cell: str, year: int) -> list[tuple[int, int]]:
    """表格日期单元格 → [(month, day), ...]，英文（Oct 12）与中文（8 月 31 日）
    两种形态都认；月份统一归一成整数。"""
    days: list[tuple[int, int]] = []
    for day_a, mon_a, mon_b, day_b in MD_RE.findall(cell):
        word = mon_a if day_a else mon_b
        month = _MONTH_LOOKUP.get(word.lower())
        if month is None:
            continue
        days.append((month, int(day_a if day_a else day_b)))
    if len(days) >= 2:
        return days
    # 中文单元格「8 月 31 日 - 9 月 7 日（PT）」：年份不在单元格里，由表标题提供
    for frag in DATE_FRAG_RE.findall(cell):
        ymd = _frag_to_ymd(frag, None, year)
        if ymd and ymd[0] == year and ymd[1]:
            days.append((ymd[1], ymd[2]))
    return days


def parse_zh_fest_names(raw: str) -> dict[str, str]:
    """中文页导航/表格里的主题 Fest 官方译名，按文档子页 slug 归集。

    同名 slug 取首个有效文本；「更多信息」等链接文本不属于节名，过滤。
    """
    names: dict[str, str] = {}
    for m in re.finditer(
        r'<a[^>]+href="[^"]*upcoming_events/(themed_sales/[a-z0-9_]+)"[^>]*>(.*?)</a>',
        raw,
        re.IGNORECASE | re.DOTALL,
    ):
        slug, text = m.group(1).lower(), _strip_tags(m.group(2))
        if not text or _NAME_JUNK_RE.fullmatch(text):
            continue
        names.setdefault(slug, text)
    return names


def parse_upcoming_events(raw: str) -> list[dict]:
    """解析文档页 → 事件列表。

    每条：``category``（seasonal_sale / next_fest / themed_fest）、``name``、
    ``start`` / ``end``（date）、``info_slug``（主题 Fest 的文档子页 slug，可空）。
    """
    events: list[dict] = []
    seen: set[tuple] = set()

    def _add(category: str, name: str, start: date, end: date,
             info_slug: str | None = None) -> None:
        key = (category, name.casefold(), start, end)
        if key in seen:
            return
        seen.add(key)
        events.append(
            {"category": category, "name": name, "start": start, "end": end,
             "info_slug": info_slug}
        )

    # 标题行扫描（位置无关）：h2 内容命中季节词 / 新品节词且可解析出日期区间
    for m in H2_RE.finditer(raw):
        text = _strip_tags(m.group(1))
        if SEASON_NAME_RE.search(text):
            rng = parse_date_range(text)
            if rng:
                _add("seasonal_sale", text.split("|")[0].strip(" |　"), *rng)
        elif NEXTFEST_NAME_RE.search(text) and _HAS_DIGIT_RE.search(text):
            rng = parse_date_range(text)
            if rng:
                _add("next_fest", text.split("|")[0].strip(" |　"), *rng)

    # 主题 Fest 表：年份取自表前的「YYYY Fests」标题
    marks = [(mm.start(), int(mm.group(1))) for mm in YEAR_MARK_RE.finditer(raw)]
    for tm in TABLE_RE.finditer(raw):
        year = max((y for pos, y in marks if pos < tm.start()), default=None)
        if year is None:
            continue
        for rm in ROW_RE.finditer(tm.group(1)):
            cells = CELL_RE.findall(rm.group(1))
            if len(cells) < 2:
                continue
            days = _table_days(cells[0], year)
            if len(days) < 2:
                continue
            slug_m = SLUG_RE.search(rm.group(1))
            try:
                start = date(year, days[0][0], days[0][1])
                end = date(year, days[1][0], days[1][1])
            except (KeyError, ValueError):
                continue
            if start > end:
                continue
            name = _strip_tags(cells[1])
            if not name:
                continue
            _add("themed_fest", name, start, end,
                 slug_m.group(1) if slug_m else None)
    return events


def validate_events(
    events: list[dict], today: date | None = None
) -> list[str]:
    """校验门：返回问题列表，空列表 = 通过。

    官方页一年内漂移过日期语序与标题命名，此门是「解析失明 / 解析错位」
    的显式报警通道——命中即整批拒绝入库，不部分采纳。
    """
    today = today or date.today()
    problems: list[str] = []
    seasonal = sorted(
        (e for e in events if e["category"] == "seasonal_sale"),
        key=lambda e: e["start"],
    )
    # 0 条 = 解析失明（最危险形态）；上限放宽是因为历史版本会同时列出已结束场次
    if not 1 <= len(seasonal) <= 6:
        problems.append(f"seasonal_sale count={len(seasonal)} out of range 1-6")
    for e in seasonal:
        dur = (e["end"] - e["start"]).days
        if not 2 <= dur <= 20:
            problems.append(f"{e['name']} duration {dur}d abnormal (2-20)")
        if e["start"] < today.replace(year=today.year - 1):
            problems.append(f"{e['name']} starts more than a year ago")
    for a, b in zip(seasonal, seasonal[1:]):
        if b["start"] <= a["end"]:
            problems.append(f"{a['name']} overlaps {b['name']}")
    for e in events:
        if e["end"] < e["start"]:
            problems.append(f"{e['name']} end < start")
    return problems
