"""achievements 生涯域：称号 / 热力图 / 偏好画像 / 纪录的原始度量。

与汇总页（`service.get_summary`）的分工：这里**只吐数值**——称号阈值、
评分规则、全部文案留在前端词典与前端常量表（模块级常量存 key 不存译文，
渲染期 `t()`）。因此本模块新增判据时不需要动前端翻译以外的任何契约。

口径说明（都基于库里已有的三张表，无外部请求）：
- 时长：`achievement_games.playtime_min`（Steam `playtime_forever`，分钟）。
- 时间：`achievement_states.unlock_time`（个人页解锁时刻，本机时区 epoch 秒）。
  库里仅此一处有「玩家某天做了什么」的信号，热力图/时段/连续天数都由它推。
- 通关跨度：单游戏最早→最晚解锁的间隔（≥2 条解锁才算）。
- 白金用时：白金即全成就，取该游戏当前总时长近似（注释在前端文案里说明）。
- 类型偏好：一游戏多类型时按类型数均分时长（避免 A+B 双计把偏好读歪），
  游戏计数不拆分。
"""
from __future__ import annotations

import json
import secrets
import time
from collections import Counter, defaultdict
from datetime import date, datetime

from sqlalchemy import select

from app.core.database import get_session_factory
from app.domains.achievements.models import AchievementDef, AchievementGame, AchievementState
from app.domains.achievements.service import (
    RARITY_BANDS,
    _achieved_rows,
    _header_url,
    _read_snapshot,
    rarity_tier,
    resolve_credentials,
)
from app.domains.games.models import Game
from app.domains.games.series import _display_name_for
from app.domains.settings import service as settings_service

# ─── 评语墙（用户手写便利贴，落 settings KV，与同步快照同一持久化通道）──
WALL_KEY = "achievements.career_wall"
WALL_MAX = 60
WALL_TEXT_MAX = 140

# 时长分布分档（分钟左闭右开；hi=None 表示不设上界），档位文案由前端按序取 key
PLAYTIME_BUCKETS: tuple[tuple[int, int | None], ...] = (
    (0, 1), (1, 60), (60, 300), (300, 1200), (1200, 3000), (3000, None),
)
# 时段切分（本机时区）：深夜 0-4 / 清晨 5-8 / 白天 9-17 / 夜间 18-23
NIGHT_HOURS = range(0, 5)
MORNING_HOURS = range(5, 9)
DAY_HOURS = range(9, 18)
EVENING_HOURS = range(18, 24)
SPOTLIGHT_LIMIT = 18
TASTE_LIMIT = 12
# 通关跨度纪录的下限：零星几条解锁挤在几分钟里不算「通关用时」
SPAN_MIN_UNLOCKS = 5
SPAN_MIN_MINUTES = 60
# 里程碑节点（第 N 枚解锁）+ 箴言墙条目数
MILESTONE_STEPS = (1, 10, 50, 100, 250, 500, 1000)
QUOTE_LIMIT = 12
QUOTE_MIN_LEN = 8
# 未完待续墙 / 尘封角落 的展示条数
UNFINISHED_LIMIT = 12
DORMANT_LIMIT = 12
# 系列进度：只统计拥有 ≥2 款的系列（单款构不成「进度」），展示条数封顶
SERIES_MIN_OWNED = 2
SERIES_LIMIT = 18

# 类型 → 风格族。**中文等值匹配只允许放在后端**：前端 `.ts` 里出现中文字面量
# 会被双语红线（no-hardcoded-cjk）直接拦下，而「动作/冒险/角色扮演」是
# `games.genres` 的数据取值（不是可译文案），前端只认这里的 ASCII 族 id，
# 再由词典把族 id 译成「手感党 / 剧情党」这类画像标签。
# 取值域是 Steam 类型表的全集（21 个，另 9 个是软件/创作工具类）。
GENRE_FAMILIES: dict[str, tuple[str, ...]] = {
    "action": ("动作",),
    "adventure": ("冒险",),
    "rpg": ("角色扮演",),
    "indie": ("独立",),
    "sim": ("模拟",),
    "casual": ("休闲",),
    "strategy": ("策略",),
    "early": ("抢先体验",),
    "mmo": ("大型多人在线",),
    "sports": ("体育",),
    "racing": ("竞速",),
    "free": ("免费开玩",),
    # 库里确实混着一批非游戏条目（实用工具 / 教育 / 照片编辑…），
    # 单列一族而不是丢掉——「买了不少创作工具」本身就是一条画像。
    "software": (
        "实用工具", "设计和插画", "动画制作和建模", "教育",
        "网络出版", "游戏开发", "视频制作", "照片编辑",
    ),
}
FAMILY_OF: dict[str, str] = {
    genre: family for family, genres in GENRE_FAMILIES.items() for genre in genres
}


def _split_multi(raw: str | None) -> list[str]:
    """逗号分隔多值串 → 去空列表（`games.genres` 的原样格式）。"""
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _json_list(raw) -> list[str]:
    """JSON 数组列（developers / publishers）→ 字符串列表，脏数据静默跳过。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if str(x).strip()]


def _bucket_index(minutes: int) -> int:
    for i, (lo, hi) in enumerate(PLAYTIME_BUCKETS):
        if minutes < lo:
            continue
        if hi is None or minutes < hi:
            return i
    return len(PLAYTIME_BUCKETS) - 1


def _median(values: list[int]) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) // 2


def _streaks(days: list[date]) -> tuple[int, int, int]:
    """连续解锁天数 → (当前, 最长, 最长段结束日 epoch)。

    「当前」口径：止于最后一个活跃日的连续段（数据是历史快照，硬拿今天当
    终点会让所有账号都显示 0——最后一个活跃日之后的空档不算断签）。
    """
    if not days:
        return 0, 0, 0
    longest = 1
    longest_end = days[0]
    run = 1
    for prev, cur in zip(days, days[1:]):
        run = run + 1 if (cur - prev).days == 1 else 1
        if run > longest:
            longest = run
            longest_end = cur
    current = 1
    for i in range(len(days) - 1, 0, -1):
        if (days[i] - days[i - 1]).days == 1:
            current += 1
        else:
            break
    end_ts = int(datetime(longest_end.year, longest_end.month, longest_end.day).timestamp())
    return current, longest, end_ts


def _empty_career(steamid: str, has_credential: bool, last_synced: str | None) -> dict:
    return {
        "hasCredential": has_credential,
        "steamid": steamid,
        "lastSyncedAt": last_synced,
        "playtime": {
            "totalMin": 0, "playedGames": 0, "avgMin": 0, "medianMin": 0,
            "maxMin": 0, "maxGame": None, "over10h": 0, "over20h": 0,
            "over50h": 0, "over100h": 0, "over200h": 0, "idleGames": 0,
            "untouchedGames": 0, "histogram": [0] * len(PLAYTIME_BUCKETS),
        },
        "trophy": {
            "total": 0, "unlocked": 0, "rate": 0.0, "gamesWithAchievements": 0,
            "perfect": 0, "platinumRate": 0.0, "rareCount": 0, "rareShare": 0.0,
            "avgRarity": 0.0, "perHour": 0.0,
            "rarity": {t: 0 for t, _ in RARITY_BANDS} | {"common": 0, "unknown": 0},
        },
        "platinum": {
            "count": 0, "avgMin": 0, "medianMin": 0, "fastest": None,
            "slowest": None, "genres": [], "spanDays": 0, "perYear": [],
            "firstDate": 0, "lastDate": 0,
        },
        "activity": {
            "days": {}, "firstDate": "", "lastDate": "", "activeDays": 0,
            "totalUnlocks": 0, "currentStreak": 0, "longestStreak": 0,
            "longestStreakEnd": 0, "busiestDay": None, "hourHistogram": [0] * 24,
            "weekdayHistogram": [0] * 7, "nightUnlocks": 0, "morningUnlocks": 0,
            "dayUnlocks": 0, "eveningUnlocks": 0, "weekendUnlocks": 0,
            "firstUnlock": None, "spanDays": 0, "hourWeekday": [],
            "monthly": [], "maxGapDays": 0,
        },
        "yearly": [],
        "milestones": [],
        "quotes": [],
        "unfinished": [],
        "unfinishedCount": 0,
        "dormant": [],
        "library": {
            "valueFen": 0, "pricedGames": 0, "costPerHourFen": 0,
            "avgPositiveRate": 0.0, "topValue": None,
        },
        "taste": {
            "genres": [], "families": [], "developers": [], "publishers": [], "series": [],
            "decades": [], "chineseGames": 0, "freshGames": 0, "oldestGame": None,
            "newestGame": None, "avgReleaseYear": 0,
        },
        "series": {
            "rows": [], "seriesTotal": 0, "taggedTotal": 0, "perfected": 0,
        },
        "records": {
            "fastestComplete": None, "slowestComplete": None, "marathonDay": None,
            "busiestHour": 0, "mostUnlocksGame": None, "biggestPlatinum": None,
        },
        "spotlight": [],
        "completedGames": 0,
    }


async def get_career(target: str | None = None) -> dict:
    """游戏生涯全量原始度量（称号/热力图/画像/纪录共用一次读取）。

    `target` 为空取主账号；同步时间戳只认「本轮同步的那个账号」，切号后
    不会显示另一个账号的同步时间。
    """
    steamid, creds = await resolve_credentials(target)
    snapshot = await _read_snapshot()
    last_synced = (
        (snapshot.get("syncedAt") or None)
        if (snapshot.get("steamid") or steamid) == steamid
        else None
    )
    career = _empty_career(steamid, bool(steamid and creds), last_synced)
    if not steamid:
        return career

    async with get_session_factory()() as session:
        pairs = (
            await session.execute(
                select(AchievementGame, Game)
                .outerjoin(Game, Game.appid == AchievementGame.appid)
                .where(AchievementGame.steamid == steamid)
            )
        ).all()
        achieved = await _achieved_rows(session, steamid)
        quotes = await _quote_rows(session, steamid)

    owned_pairs = [(g, gm) for g, gm in pairs if (g.source or "owned") == "owned"]
    # 成就口径含库外（家庭共享等），时长/系列/库价值口径只看已购：
    # 库外行没有时长数据（Steam 不提供未拥有游戏的时长），混进时长分布与
    # 「系列拥有数」「库价值」会凭空多出条目
    _fill_playtime(career, owned_pairs)
    _fill_trophy(career, pairs, achieved)
    _fill_activity(career, achieved, pairs)
    _fill_taste(career, owned_pairs)
    _fill_series(career, owned_pairs)
    _fill_platinum(career, pairs, achieved)
    _fill_library(career, owned_pairs)
    career["quotes"] = quotes
    return career


async def _quote_rows(session, steamid: str) -> list[dict]:
    """成就箴言墙：挑描述有实义的最稀有成就（越稀有越靠前，去重同名描述）。

    社区站用「Trophy Comments」承载 UGC 留言；本模块没有社交数据，改用成就
    描述策展（研究结论里点名的低成本替代）。
    """
    rows = (
        await session.execute(
            select(AchievementDef.name, AchievementDef.description, AchievementDef.icon_url,
                   AchievementDef.global_percent, AchievementGame.appid, AchievementGame.name)
            .join(AchievementDef, (AchievementDef.appid == AchievementGame.appid))
            .outerjoin(
                AchievementState,
                (AchievementState.appid == AchievementDef.appid)
                & (AchievementState.image_name == AchievementDef.image_name)
                & (AchievementState.steamid == steamid),
            )
            .where(AchievementState.achieved.is_(True))
            .order_by(AchievementDef.global_percent.asc().nulls_last())
            .limit(160)
        )
    ).all()
    seen: set[str] = set()
    out: list[dict] = []
    for name, desc, icon, percent, appid, game_name in rows:
        text = _clean_text(desc)
        if len(text) < QUOTE_MIN_LEN or text in seen:
            continue
        seen.add(text)
        out.append({
            "name": name or "",
            "text": text,
            "icon": icon or "",
            "globalPercent": round(percent, 2) if percent is not None else None,
            "appid": appid,
            "gameName": game_name or "",
        })
        if len(out) >= QUOTE_LIMIT:
            break
    return out


def _clean_text(raw: str | None) -> str:
    """描述里带 `<br/>` 与全角空格，压成单行短句。"""
    if not raw:
        return ""
    text = raw.replace("<br/>", " ").replace("<br>", " ").replace("&nbsp;", " ")
    return " ".join(text.split()).strip()


def _fill_playtime(career: dict, pairs: list) -> None:
    playtime = career["playtime"]
    minutes = [g.playtime_min or 0 for g, _ in pairs]
    played = [(g, gm) for g, gm in pairs if (g.playtime_min or 0) > 0]
    total = sum(minutes)
    histogram = [0] * len(PLAYTIME_BUCKETS)
    for m in minutes:
        histogram[_bucket_index(m)] += 1

    top = max(pairs, key=lambda x: x[0].playtime_min or 0, default=None)
    playtime.update({
        "totalMin": total,
        "playedGames": len(played),
        "avgMin": total // len(played) if played else 0,
        "medianMin": _median([g.playtime_min or 0 for g, _ in played]),
        "maxMin": (top[0].playtime_min or 0) if top else 0,
        "maxGame": _game_brief(*top) if top else None,
        "over10h": sum(1 for m in minutes if m >= 600),
        "over20h": sum(1 for m in minutes if m >= 1200),
        "over50h": sum(1 for m in minutes if m >= 3000),
        "over100h": sum(1 for m in minutes if m >= 6000),
        "over200h": sum(1 for m in minutes if m >= 12000),
        "idleGames": sum(1 for m in minutes if 0 < m < 60),
        "untouchedGames": sum(1 for m in minutes if m == 0),
        "histogram": histogram,
    })
    # 生涯封面墙：时长榜前 N 款带封面陈列（「带游戏图片」的主体来源）
    ordered = sorted(pairs, key=lambda x: (-(x[0].playtime_min or 0), (x[0].name or "").lower()))
    career["spotlight"] = [_game_brief(g, gm) for g, gm in ordered[:SPOTLIGHT_LIMIT]]

    # 未完待续：开过坑但没拿满（差得越少越靠前，同缺口按进度高者优先）
    pending = [
        (g, gm) for g, gm in pairs
        if (g.total_achievements or 0) > 0 and 0 < (g.unlocked or 0) < (g.total_achievements or 0)
    ]
    career["unfinishedCount"] = len(pending)
    pending.sort(key=lambda x: (
        (x[0].total_achievements or 0) - (x[0].unlocked or 0),
        -(x[0].unlocked or 0) / max(1, x[0].total_achievements or 1),
    ))
    career["unfinished"] = [
        {**_game_brief(g, gm), "remaining": (g.total_achievements or 0) - (g.unlocked or 0)}
        for g, gm in pending[:UNFINISHED_LIMIT]
    ]

    # 尘封角落：有实际时长、但最后一次启动最早的那批（长期没再碰）
    stale = [(g, gm) for g, gm in pairs if (g.playtime_min or 0) > 0 and (g.last_played or 0) > 0]
    stale.sort(key=lambda x: x[0].last_played or 0)
    career["dormant"] = [
        {**_game_brief(g, gm), "lastPlayed": g.last_played or 0} for g, gm in stale[:DORMANT_LIMIT]
    ]


def _game_brief(row: AchievementGame, game: Game | None) -> dict:
    return {
        "appid": row.appid,
        "name": row.name or "",
        "headerImage": _header_url(game.header_image if game else None, row.appid),
        "playtimeMin": row.playtime_min or 0,
        "unlocked": row.unlocked or 0,
        "total": row.total_achievements or 0,
        "platinum": bool(row.platinum),
    }


def _fill_trophy(career: dict, pairs: list, achieved: list[dict]) -> None:
    trophy = [(g, gm) for g, gm in pairs if (g.total_achievements or 0) > 0]
    total = sum(g.total_achievements for g, _ in trophy)
    unlocked = sum(g.unlocked or 0 for g, _ in trophy)
    perfect = sum(1 for g, _ in trophy if g.platinum)

    rarity: dict[str, int] = {t: 0 for t, _ in RARITY_BANDS}
    rarity["common"] = 0
    rarity["unknown"] = 0
    for a in achieved:
        rarity[rarity_tier(a["globalPercent"])] += 1
    rare_count = rarity["ultra"] + rarity["very_rare"]

    rated = [a["globalPercent"] for a in achieved if a["globalPercent"] is not None]
    career["trophy"].update({
        "total": total,
        "unlocked": unlocked,
        "rate": round(unlocked * 100.0 / total, 1) if total else 0.0,
        "gamesWithAchievements": len(trophy),
        "perfect": perfect,
        "platinumRate": round(perfect * 100.0 / len(trophy), 1) if trophy else 0.0,
        "rareCount": rare_count,
        "rareShare": round(rare_count * 100.0 / len(achieved), 1) if achieved else 0.0,
        # 平均稀有度 = 已解锁成就全局解锁率均值（越低越硬核）
        "avgRarity": round(sum(rated) / len(rated), 1) if rated else 0.0,
        "perHour": round(unlocked / max(1.0, career["playtime"]["totalMin"] / 60.0), 2),
        "rarity": rarity,
    })
    career["completedGames"] = sum(
        1 for g, _ in trophy if (g.total_achievements or 0) > 0 and (g.unlocked or 0) >= g.total_achievements
    )


def _fill_activity(career: dict, achieved: list[dict], pairs: list) -> None:
    activity = career["activity"]
    timed = [a for a in achieved if a["unlockTime"] > 0]

    day_counts: Counter[str] = Counter()
    hours = [0] * 24
    weekdays = [0] * 7
    hour_weekday = [0] * (7 * 24)
    months: Counter[str] = Counter()
    per_game: dict[int, list[int]] = defaultdict(list)
    for a in timed:
        d = datetime.fromtimestamp(a["unlockTime"])
        day_counts[d.date().isoformat()] += 1
        hours[d.hour] += 1
        weekdays[d.weekday()] += 1
        hour_weekday[d.weekday() * 24 + d.hour] += 1
        months[f"{d.year:04d}-{d.month:02d}"] += 1
        per_game[a["appid"]].append(a["unlockTime"])

    days = sorted(date.fromisoformat(k) for k in day_counts)
    current, longest, longest_end = _streaks(days)
    busiest = max(day_counts.items(), key=lambda kv: kv[1], default=None)
    first = min(timed, key=lambda a: a["unlockTime"], default=None)
    last = max(timed, key=lambda a: a["unlockTime"], default=None)
    span_days = (days[-1] - days[0]).days + 1 if days else 0

    # 单游戏「通关跨度」：最早→最晚解锁。至少 5 条解锁、且时长过 1 小时才算——
    # 2~3 条零星解锁挤在几分钟里得出的「跨度」没有通关含义（社区常见的假纪录）。
    spans = []
    for appid, stamps in per_game.items():
        if len(stamps) < SPAN_MIN_UNLOCKS:
            continue
        row = next((g for g, _ in pairs if g.appid == appid), None)
        if row is None or (row.playtime_min or 0) < SPAN_MIN_MINUTES:
            continue
        lo, hi = min(stamps), max(stamps)
        spans.append((appid, hi - lo, lo, hi, len(stamps)))
    name_of = {g.appid: (g, gm) for g, gm in pairs}

    career["activity"].update({
        "days": dict(day_counts),
        "firstDate": days[0].isoformat() if days else "",
        "lastDate": days[-1].isoformat() if days else "",
        "activeDays": len(days),
        "totalUnlocks": len(timed),
        "currentStreak": current,
        "longestStreak": longest,
        "longestStreakEnd": longest_end,
        "busiestDay": {"date": busiest[0], "count": busiest[1]} if busiest else None,
        "hourHistogram": hours,
        "weekdayHistogram": weekdays,
        "nightUnlocks": sum(hours[h] for h in NIGHT_HOURS),
        "morningUnlocks": sum(hours[h] for h in MORNING_HOURS),
        "dayUnlocks": sum(hours[h] for h in DAY_HOURS),
        "eveningUnlocks": sum(hours[h] for h in EVENING_HOURS),
        "weekendUnlocks": weekdays[5] + weekdays[6],
        "hourWeekday": hour_weekday,
        # 月度序列（稀疏，只给有解锁的月份；前端补齐空档着色）
        "monthly": [{"month": k, "count": months[k]} for k in sorted(months)],
        # 最长空窗：相邻活跃日之间最大的间隔（断更天数）
        "maxGapDays": max(((b - a).days for a, b in zip(days, days[1:])), default=0),
        "firstUnlock": (
            {**first, "headerImage": _game_brief(*name_of[first["appid"]])["headerImage"]}
            if first and first["appid"] in name_of else first
        ),
        "spanDays": span_days,
    })

    career["yearly"] = _yearly(timed, per_game, pairs)

    fastest = min(spans, key=lambda x: x[1], default=None)
    slowest = max(spans, key=lambda x: x[1], default=None)
    unlock_counts = Counter(a["appid"] for a in timed)
    most = unlock_counts.most_common(1)
    career["records"].update({
        "fastestComplete": _span_brief(fastest, name_of),
        "slowestComplete": _span_brief(slowest, name_of),
        "marathonDay": {"date": busiest[0], "count": busiest[1]} if busiest else None,
        "busiestHour": hours.index(max(hours)) if timed else 0,
        "mostUnlocksGame": (
            {**_game_brief(*name_of[most[0][0]]), "unlocked": most[0][1]}
            if most and most[0][0] in name_of else None
        ),
    })
    career["milestones"] = _milestones(timed, name_of)


def _milestones(timed: list[dict], name_of: dict) -> list[dict]:
    """里程碑节点：第 N 枚解锁 + 已知的稀有度首杀（按解锁先后）。"""
    ordered = sorted(timed, key=lambda a: a["unlockTime"])
    out: list[dict] = []
    for step in MILESTONE_STEPS:
        if len(ordered) < step:
            break
        a = ordered[step - 1]
        out.append({
            "kind": "count",
            "index": step,
            "at": a["unlockTime"],
            "name": a["name"],
            "icon": a.get("icon") or "",
            "gameName": a.get("gameName") or "",
            "appid": a["appid"],
            "headerImage": (
                _game_brief(*name_of[a["appid"]])["headerImage"] if a["appid"] in name_of else ""
            ),
        })
    first_rare = next(
        (a for a in ordered if (a.get("globalPercent") or 100.0) < 5.0), None
    )
    if first_rare:
        out.append({
            "kind": "rarest",
            "index": 0,
            "at": first_rare["unlockTime"],
            "name": first_rare["name"],
            "icon": first_rare.get("icon") or "",
            "gameName": first_rare.get("gameName") or "",
            "appid": first_rare["appid"],
            "globalPercent": round(first_rare["globalPercent"], 2),
            "headerImage": (
                _game_brief(*name_of[first_rare["appid"]])["headerImage"]
                if first_rare["appid"] in name_of else ""
            ),
        })
    return sorted(out, key=lambda m: m["at"])


def _fill_series(career: dict, pairs: list) -> None:
    """系列进度：按 `games.series_id` 聚合「拥有 / 已玩 / 白金 / 成就完成度」。

    与 `taste.series`（时长排行榜）的分工：那边回答「哪个系列我玩得最多」，
    这里回答「每个系列我还差多少」——同一份分组数据，两个不同的问题。

    口径说明（两个分母都吐出来，前端只选一个画进度条，不做二次除法）：
    · `unlocked/total` —— 该系列**全部拥有作品**的成就账（未玩的成员也计入
      分母，故大系列看起来恒低）；
    · `playedUnlocked/playedTotal` —— 只算**已玩成员**的账，反映「真在刷的那部分
      刷到哪了」。真实数据上后者才有区分度（Persona 48.5% vs 含未玩成员时的
      整体口径被稀释）。
    `progress` 取后者（已玩口径），无已玩成员时为 0。

    展示名走 `games.series._display_name_for`（成员展示名的最长公共汉字前缀，
    否则回落系列标识）——**不另造一套**：游戏详情页的「同系列」区块用的是
    同一个函数，两处各写各的会漂移出两套系列名。
    """
    series = career["series"]
    groups: dict[str, dict] = {}
    for g, gm in pairs:
        sid = (gm.series_id or "").strip() if gm is not None else ""
        if not sid:
            continue
        row = groups.get(sid)
        if row is None:
            row = groups[sid] = {
                "seriesId": sid, "names": [],
                "owned": 0, "played": 0, "platinum": 0, "completed": 0,
                "unlocked": 0, "total": 0, "playedUnlocked": 0, "playedTotal": 0,
                "playtimeMin": 0, "top": None, "next": None,
            }
        name = (gm.name if gm is not None else None) or g.name or ""
        row["names"].append(name)

        minutes = g.playtime_min or 0
        total = g.total_achievements or 0
        unlocked = g.unlocked or 0
        row["owned"] += 1
        row["playtimeMin"] += minutes
        row["unlocked"] += unlocked
        row["total"] += total
        if g.platinum:
            row["platinum"] += 1
        if total > 0 and unlocked >= total:
            row["completed"] += 1
        if minutes > 0:
            row["played"] += 1
            if total > 0:
                row["playedUnlocked"] += unlocked
                row["playedTotal"] += total

        brief = _game_brief(g, gm)
        # 系列封面：取时长最高的一款（大系列里最能代表该系列的那作）
        if row["top"] is None or minutes > row["top"]["playtimeMin"]:
            row["top"] = brief
        # 「差几枚就全成就」里缺口最小的那款（最有行动价值的下一步）
        remaining = total - unlocked
        if 0 < unlocked < total and (
            row["next"] is None or remaining < row["next"]["remaining"]
        ):
            row["next"] = {**brief, "unlocked": unlocked, "total": total, "remaining": remaining}

    rows = [r for r in groups.values() if r["owned"] >= SERIES_MIN_OWNED]
    series["seriesTotal"] = len(rows)
    series["taggedTotal"] = len(groups)
    series["perfected"] = sum(
        1 for r in rows if r["platinum"] > 0 and r["platinum"] == r["owned"]
    )
    # 排序：先看「真的在玩」（已玩作品数），再看白金，最后看时长——
    # 一款没碰的大系列不该压过款数少但全程通关的系列。
    rows.sort(key=lambda r: (-r["played"], -r["platinum"], -r["playtimeMin"], r["names"][0]))
    out: list[dict] = []
    for r in rows[:SERIES_LIMIT]:
        out.append({
            "seriesId": r["seriesId"],
            "name": _display_name_for(r["names"], r["seriesId"]),
            "owned": r["owned"],
            "played": r["played"],
            "platinum": r["platinum"],
            "completed": r["completed"],
            "unlocked": r["unlocked"],
            "total": r["total"],
            "playedUnlocked": r["playedUnlocked"],
            "playedTotal": r["playedTotal"],
            "playtimeMin": r["playtimeMin"],
            "progress": (
                round(r["playedUnlocked"] / r["playedTotal"], 3)
                if r["playedTotal"] > 0 else 0.0
            ),
            "topGame": r["top"],
            "nextGame": r["next"],
        })
    series["rows"] = out


def _fill_library(career: dict, pairs: list) -> None:
    """库资产侧写：按现价合计库价值 + 每小时成本（价值层模块的最小集）。"""
    library = career["library"]
    priced = [(g, gm) for g, gm in pairs if gm is not None and (gm.min_cny_fen or 0) > 0]
    if not priced:
        return
    value = sum(gm.min_cny_fen for _, gm in priced)
    hours = career["playtime"]["totalMin"] / 60.0
    top = max(priced, key=lambda x: x[1].min_cny_fen or 0)
    rates = [gm.positive_rate for _, gm in pairs if gm is not None and (gm.positive_rate or 0) > 0]
    library.update({
        "valueFen": value,
        "pricedGames": len(priced),
        "costPerHourFen": round(value / hours, 1) if hours > 0 else 0,
        # games.positive_rate 是万分比 0-10000，这里统一转成百分数
        "avgPositiveRate": round(sum(rates) / len(rates) / 100.0, 1) if rates else 0.0,
        "topValue": {
            **_game_brief(*top),
            "priceFen": top[1].min_cny_fen or 0,
            "positiveRate": round((top[1].positive_rate or 0) / 100.0, 1),
        },
    })


def _span_brief(span, name_of: dict) -> dict | None:
    if not span:
        return None
    appid, delta, lo, hi, count = span
    if appid not in name_of:
        return None
    return {
        **_game_brief(*name_of[appid]),
        "spanMin": delta // 60,
        "spanSec": delta,
        "fromTime": lo,
        "toTime": hi,
        "unlocks": count,
    }


def _yearly(timed: list[dict], per_game: dict, pairs: list) -> list[dict]:
    """逐年生涯：解锁数 / 涉及游戏数 / 白金数（白金日期取该游戏最后一次解锁）。"""
    unlocks: Counter[int] = Counter()
    games: dict[int, set] = defaultdict(set)
    for a in timed:
        year = datetime.fromtimestamp(a["unlockTime"]).year
        unlocks[year] += 1
        games[year].add(a["appid"])
    plat_last: dict[int, int] = {}
    for a in timed:
        plat_last[a["appid"]] = max(plat_last.get(a["appid"], 0), a["unlockTime"])
    plat_rows = {g.appid: (g.total_achievements or 0) > 0 and bool(g.platinum) for g, _ in pairs}
    plats: Counter[int] = Counter()
    for appid, ts in plat_last.items():
        if plat_rows.get(appid):
            plats[datetime.fromtimestamp(ts).year] += 1
    years = sorted(set(unlocks) | set(plats))
    return [
        {"year": y, "unlocks": unlocks.get(y, 0), "games": len(games.get(y, ())), "platinum": plats.get(y, 0)}
        for y in years
    ]


def _fill_platinum(career: dict, pairs: list, achieved: list[dict]) -> None:
    plat_last: dict[int, int] = {}
    for a in achieved:
        plat_last[a["appid"]] = max(plat_last.get(a["appid"], 0), a["unlockTime"])

    rows = [(g, gm) for g, gm in pairs if g.platinum and (g.total_achievements or 0) > 0]
    if not rows:
        return
    entries = [{**_game_brief(g, gm), "date": plat_last.get(g.appid, 0)} for g, gm in rows]
    dated = [e for e in entries if e["date"] > 0]
    # 用时统计只算有时长的：库外白金（家庭共享等）playtime 恒为 0，
    # 不排除的话「最快白金」会被它们刷成 0 小时
    timed = [e for e in entries if e["playtimeMin"] > 0] or entries
    times = [e["playtimeMin"] for e in timed]

    genre_counter: Counter[str] = Counter()
    for g, gm in rows:
        for genre in _split_multi(gm.genres if gm else None):
            genre_counter[genre] += 1

    per_year: Counter[int] = Counter()
    for e in dated:
        per_year[datetime.fromtimestamp(e["date"]).year] += 1

    career["platinum"].update({
        "count": len(entries),
        "avgMin": sum(times) // len(times) if times else 0,
        "medianMin": _median(times),
        "fastest": min(timed, key=lambda e: e["playtimeMin"]),
        "slowest": max(timed, key=lambda e: e["playtimeMin"]),
        "genres": [{"genre": k, "count": v} for k, v in genre_counter.most_common(TASTE_LIMIT)],
        "spanDays": (
            (max(e["date"] for e in dated) - min(e["date"] for e in dated)) // 86400
            if len(dated) > 1 else 0
        ),
        "perYear": [{"year": y, "count": per_year[y]} for y in sorted(per_year)],
        "firstDate": min((e["date"] for e in dated), default=0),
        "lastDate": max((e["date"] for e in dated), default=0),
    })
    career["records"]["biggestPlatinum"] = max(entries, key=lambda e: e["total"], default=None)


def _fill_taste(career: dict, pairs: list) -> None:
    taste = career["taste"]
    played = [(g, gm) for g, gm in pairs if (g.playtime_min or 0) > 0]

    genres: dict[str, dict] = {}
    families: dict[str, dict] = {}
    developers: dict[str, dict] = {}
    publishers: dict[str, dict] = {}
    series: dict[str, dict] = {}
    decades: Counter[str] = Counter()
    decade_minutes: Counter[str] = Counter()
    years: list[int] = []
    chinese = 0
    fresh = 0
    current_year = datetime.now().year

    for g, gm in played:
        minutes = g.playtime_min or 0
        platinum = bool(g.platinum)
        genre_list = _split_multi(gm.genres if gm else None)
        dev_list = _json_list(gm.developers if gm else None)
        share = minutes / max(1, len(genre_list)) if genre_list else 0
        for genre in genre_list:
            row = genres.setdefault(genre, {"genre": genre, "games": 0, "playtimeMin": 0, "platinum": 0})
            row["games"] += 1
            row["playtimeMin"] += share
            row["platinum"] += 1 if platinum else 0
        # 风格族：同一款的多个类型常落进同一族（动作+抢先体验→action/early），
        # 按**去重后的族数**均分时长，避免一作把某个族撑成双倍权重。
        fam_list = {FAMILY_OF[g] for g in genre_list if g in FAMILY_OF}
        fam_share = minutes / max(1, len(fam_list)) if fam_list else 0
        for family in fam_list:
            row = families.setdefault(
                family, {"family": family, "games": 0, "playtimeMin": 0, "platinum": 0}
            )
            row["games"] += 1
            row["playtimeMin"] += fam_share
            row["platinum"] += 1 if platinum else 0
        for name in dev_list:
            row = developers.setdefault(name, {"name": name, "games": 0, "playtimeMin": 0, "platinum": 0})
            row["games"] += 1
            row["playtimeMin"] += minutes / max(1, len(dev_list))
            row["platinum"] += 1 if platinum else 0
        for name in _json_list(gm.publishers if gm else None):
            row = publishers.setdefault(name, {"name": name, "games": 0, "playtimeMin": 0, "platinum": 0})
            row["games"] += 1
            row["playtimeMin"] += minutes
            row["platinum"] += 1 if platinum else 0

        series_name = (gm.series_id or "").strip() if gm else ""
        if series_name:
            row = series.setdefault(
                series_name, {"name": series_name, "games": 0, "playtimeMin": 0, "platinum": 0}
            )
            row["games"] += 1
            row["playtimeMin"] += minutes
            row["platinum"] += 1 if platinum else 0

        release = (gm.release_date or "") if gm else ""
        if len(release) >= 4 and release[:4].isdigit():
            year = int(release[:4])
            years.append(year)
            decade = f"{year // 10 * 10}s"
            decades[decade] += 1
            decade_minutes[decade] += minutes
            if year >= current_year - 1:
                fresh += 1
        if (gm.chinese_support or "") if gm else "":
            chinese += 1

    def _rank(table: dict[str, dict], label: str) -> list[dict]:
        """按累计时长降序取前 N（同分按标签名稳定排序）。"""
        rows = sorted(table.values(), key=lambda r: (-r["playtimeMin"], r[label]))
        return [{**r, "playtimeMin": int(round(r["playtimeMin"]))} for r in rows[:TASTE_LIMIT]]

    oldest = min(played, key=lambda x: (x[1].release_date or "9999") if x[1] else "9999", default=None)
    newest = max(played, key=lambda x: (x[1].release_date or "") if x[1] else "", default=None)

    taste.update({
        "genres": _rank(genres, "genre"),
        "families": _rank(families, "family"),
        "developers": _rank(developers, "name"),
        "publishers": _rank(publishers, "name"),
        "series": _rank(series, "name"),
        "decades": [
            {"decade": d, "games": decades[d], "playtimeMin": decade_minutes[d]}
            for d in sorted(decades)
        ],
        "chineseGames": chinese,
        "freshGames": fresh,
        "avgReleaseYear": sum(years) // len(years) if years else 0,
        "oldestGame": (
            {**_game_brief(*oldest), "releaseDate": oldest[1].release_date if oldest[1] else ""}
            if oldest else None
        ),
        "newestGame": (
            {**_game_brief(*newest), "releaseDate": newest[1].release_date if newest[1] else ""}
            if newest else None
        ),
    })


# ─── 评语墙（手写便利贴）──────────────────────────────────────


def _clean_wall(raw) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        out.append({
            "id": str(item.get("id") or secrets.token_hex(6)),
            "text": text[:WALL_TEXT_MAX],
            "at": int(item.get("at") or 0),
        })
    return out[-WALL_MAX:]


async def list_wall() -> list[dict]:
    return _clean_wall(await settings_service.get_value(WALL_KEY, []))


async def add_wall(text: str) -> list[dict]:
    """追加一条便利贴（超长/空白拒绝，超出上限丢最旧）。"""
    body = " ".join(str(text or "").split())
    if not body:
        raise ValueError("评语不能为空")
    if len(body) > WALL_TEXT_MAX:
        raise ValueError(f"评语最长 {WALL_TEXT_MAX} 字")
    notes = await list_wall()
    notes.append({"id": secrets.token_hex(6), "text": body, "at": int(time.time())})
    notes = notes[-WALL_MAX:]
    await settings_service.set_value(WALL_KEY, notes)
    return notes


async def remove_wall(comment_id: str) -> list[dict]:
    notes = [n for n in await list_wall() if n["id"] != comment_id]
    await settings_service.set_value(WALL_KEY, notes)
    return notes