"""游戏系列识别：本地名称聚类，零网络开销。

输入 games.name（展示名）与 games.name_en，按名称相似启发式把「可能是
同一个系列」的游戏聚成组，写回 games.series_id（如 GTA / Resident Evil）。

语义约定：
- NULL = 未计算（新入库还没跑过识别）；'' = 已计算但落单（无同系列）；
  非空 = 系列标识（展示用名，取成员英文名的最长公共词前缀）。
  空串哨兵是给爬后链的「有未归组行吗」探测用的：全库扫过一遍后落单行
  不再是 NULL，探测就不会每轮空转重算。

算法：分桶（英文首词 / 次词 / 中文前两字）缩小两两比对范围 + 并查集聚类，
避免 O(N²) 全量比对。判定规则宁缺毋滥——并查集有传递性，一条错边会污染
整个簇，因此每条规则都带负面词表或长度门槛。
"""
from __future__ import annotations

import asyncio
import re
from collections import defaultdict
from difflib import SequenceMatcher

from sqlalchemy import and_, or_, select, update

from app.core.database import get_session_factory
from app.domains.games.models import Game

# 参与聚类的类型：正作才有「系列」语义；DLC/DEMO/MOD 等不聚（否则每款
# 带 DLC 的本作都会成「系列」，同系列区块会被自家 DLC 淹没）。
# 库内 type 存大写（GAME / DLC / ...），NULL = 未分类，按可聚类处理
_CLUSTERABLE_TYPES = ("GAME", None)


def _clusterable_conds():
    """可聚类行的 type 条件（OR 组）。"""
    return [Game.type == t if t is not None else Game.type.is_(None) for t in _CLUSTERABLE_TYPES]

# 分隔符前缀（冒号/连字符类）判定的负面中文前缀：二字通用词，撞车率高
_BAD_CN_BASES = frozenset(
    {"模拟", "我的", "传奇", "动作", "逃离", "星际", "少女", "勇者", "我是"}
)

# 公共词前缀判定的负面英文首词：冠词与泛用起手词，开头相同不构成系列证据；
# 另含两家以人名当前缀的品牌伞（Tom Clancy's / Sid Meier's 旗下是多条平行
# 系列，品牌相同不等于同系列——具体系列靠后段词自聚）
_BAD_EN_STARTS = frozenset(
    {"the", "a", "an", "i", "super", "world", "of", "simulator", "vr",
     "project", "tom", "sid"}
)

# 泛用次词：公共前缀恰好 2 词且次词是这类虚词（X of Y / How to Z）时，
# 前缀证据不足——「Call of Duty」与「Call of Juarez」共用 call of 却非同系列
_GENERIC_MINOR = frozenset({"of", "the", "a", "an", "and", "&", "to", "am"})

# 罗马数字 → 阿拉伯（只用于比对归一，不改动任何存储/展示数据）；
# 使 Civilization V 与 Civilization 5 一类写法差异可比
_ROMAN = {
    "ii": "2", "iii": "3", "iv": "4", "vi": "6", "vii": "7", "viii": "8",
    "ix": "9", "xi": "11", "xii": "12", "xiii": "13", "xiv": "14", "xv": "15",
}

_EN_SPLIT = re.compile(r"[^a-z0-9]+")
_CN_KEEP = re.compile(r"[^\u4e00-\u9fff]+")
_SEP = re.compile(r"[:：\-–—]")

_CLUST_MIN_MEMBER = 2
_SERIES_ID_MAX = 100


def _en_tokens(name: str) -> list[str]:
    """英文名 → 归一小写词序列（非字母数字断开，罗马数字归一）。

    撇号先行删除：Clancy's 归一为 clancys 单词——按非字母断开会把所有格
    拆成独立的 s，公共前缀长度凭空多一截（Tom Clancy's X 与 Y 因此假合并）。
    """
    raw = _EN_SPLIT.sub(" ", name.lower().replace("'", "").replace("’", "")).split()
    return [_ROMAN.get(t, t) for t in raw]


def _cn_chars(name: str) -> str:
    """名字 → 仅保留汉字（去书名号/商标符等装饰）。"""
    return _CN_KEEP.sub("", name)


def _sep_base(name: str) -> tuple[str, str] | None:
    """分隔符前缀：首个冒号/连字符/破折号前的部分，返回 (原文, 归一串)。"""
    m = _SEP.search(name)
    if not m:
        return None
    raw = name[: m.start()].strip()
    if not raw:
        return None
    return raw, " ".join(_en_tokens(raw))


def _common_prefix_len(a: list[str], b: list[str]) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def _is_sequel_number(tok: str) -> bool:
    # "0" 是版本标记（2.0 → [2, 0]）不是续作序号
    return (tok.isdigit() and tok != "0") or tok in _ROMAN.values()


_TAIL_VERSION = re.compile(r"\d{1,2}k\d{2}")


def _is_version_tail(tok: str) -> bool:
    """规则 5 的尾部「版本号」词：纯序号之外容年货式 2K21/2K26。"""
    return _is_sequel_number(tok) or bool(_TAIL_VERSION.fullmatch(tok))


def _base_wo_tail_num(base: str) -> str:
    """去分隔符前缀尾部的序号词（the witcher 3 → the witcher）。"""
    toks = base.split()
    if len(toks) > 1 and _is_sequel_number(toks[-1]):
        toks = toks[:-1]
    return " ".join(toks)


def _en_usable(toks: list[str]) -> bool:
    """这串英文 token 承载得起系列证据吗：裸序号名（"2" / "II"）是
    零证据——库里确实存在 name_en 就是个序号的行，它们两两相似度 1.0，
    会互连成枢纽把各自正确的对黏成一大簇。"""
    return bool(toks) and not (len(toks) == 1 and _is_sequel_number(toks[0]))


def is_same_series(
    name1: str, en1: str | None, name2: str, en2: str | None
) -> bool:
    """两个名字是否同系列（宁缺毋滥，任一规则命中即算）。"""
    t1 = _en_tokens(en1 or name1)
    t2 = _en_tokens(en2 or name2)
    c1 = _cn_chars(name1)
    c2 = _cn_chars(name2)
    en1_all = name1 if not (en1 or "").strip() else en1
    en2_all = name2 if not (en2 or "").strip() else en2
    # 结构规则（3/4/5）剥离开头冠词再比：冠词是装饰，剥掉后 The Witcher
    # 1/2/3 的前缀判定与 Witcher 1/2/3 同轨；The Legend of X 也不再凭
    # the legend of 三个虚词过 ≥3 词门槛
    s1 = t1[1:] if t1 and t1[0] in ("the", "a", "an") else t1
    s2 = t2[1:] if t2 and t2[0] in ("the", "a", "an") else t2

    # 1) 英文分隔符前缀一致（如 "GTA: ..." / "GTA- ..." 同前缀），≥4 字符；
    #    变体：前缀只差尾部序号也算（The Witcher: EE ↔ The Witcher 3: Wild
    #    Hunt 前缀同为 the witcher）——去尾号比较，仍需 ≥4 字符
    b1 = _sep_base(en1_all)
    b2 = _sep_base(en2_all)
    if b1 and b2:
        for x, y in ((b1[1], b2[1]), (_base_wo_tail_num(b1[1]), _base_wo_tail_num(b2[1]))):
            if x == y and len(x) >= 4:
                return True

    # 2) 中文分隔符前缀一致，≥2 字且不在负面词表
    cb1, cb2 = _sep_base(name1), _sep_base(name2)
    if cb1 and cb2:
        base1, base2 = _cn_chars(cb1[0]), _cn_chars(cb2[0])
        if (
            len(base1) >= 2
            and base1 == base2
            and base1 not in _BAD_CN_BASES
        ):
            return True

    # 3) 英文连续公共词前缀 ≥2（如 Call of Duty 全家）。前缀恰好 2 词时
    #    证据弱一档：首词不在负面词表，且次词不是虚词（X of Y 撞车高发，
    #    Call of Duty 与 Call of Juarez 共用 call of 却非同系列）；≥3 词的
    #    前缀（sid meiers civilization / age of empires）本身已够特异
    pre = _common_prefix_len(s1, s2)
    if pre >= 2 and (
        pre >= 3 or (s1[0] not in _BAD_EN_STARTS and s1[1] not in _GENERIC_MINOR)
    ):
        return True

    # 4) 纯序号续作：短名是长名的完整词前缀，且长名前缀后紧跟序号
    #   （Portal → Portal 2 / Diablo → Diablo III）。短名须承载得起证据
    #   （裸序号 "2" 与 "2.0" 开头的版本名前缀相等纯属巧合）
    short, long_ = (s1, s2) if len(s1) <= len(s2) else (s2, s1)
    if _en_usable(short) and long_[: len(short)] == short and len(long_) > len(short):
        nxt = long_[len(short)]
        if _is_sequel_number(nxt):
            return True

    # 5) 同前缀 + 尾部版本号互异（FIFA 22 / FIFA 23、NBA 2K25 / 2K26、
    #   Doom 3 / Doom 2016）。前缀需 ≥3 字符，防 "Room 13 / Room 66" 一类
    #   编号撞车；前缀 2 词时与规则 3 同样的弱证据门槛
    if pre >= 1:
        rest1, rest2 = s1[pre:], s2[pre:]
        strong = pre >= 3 or (
            s1[0] not in _BAD_EN_STARTS
            and (pre == 1 or s1[1] not in _GENERIC_MINOR)
        )
        if (
            strong
            and len(rest1) == 1
            and len(rest2) == 1
            and _is_version_tail(rest1[0])
            and _is_version_tail(rest2[0])
            and rest1[0] != rest2[0]
            and len("".join(s1[:pre])) >= 3
        ):
            return True

    # 6) 中文严格前缀（辐射4 → 辐射：新维加斯），≥2 字且不在负面词表
    if c1 != c2 and len(c1) >= 2 and len(c2) >= 2:
        short_c, long_c = (c1, c2) if len(c1) < len(c2) else (c2, c1)
        if (
            long_c.startswith(short_c)
            and short_c not in _BAD_CN_BASES
        ):
            return True

    # 7) 中文完全一致 + 英文有佐证（短中文名撞车最频繁，须英文作保）
    if c1 and c1 == c2:
        ratio = SequenceMatcher(None, en1_all.lower(), en2_all.lower()).ratio()
        if _common_prefix_len(s1, s2) >= 1 or (
            ratio >= 0.5 and _en_usable(s1) and _en_usable(s2)
        ):
            return True

    # 8) 高相似度兜底（容错重制版/版本差异的英文写法）
    if _en_usable(s1) and _en_usable(s2):
        if SequenceMatcher(None, " ".join(s1), " ".join(s2)).ratio() > 0.90:
            return True

    return False


class _Member:
    __slots__ = ("appid", "name", "name_en", "toks", "cn")

    def __init__(self, appid: int, name: str, name_en: str | None):
        self.appid = appid
        self.name = name
        self.name_en = name_en
        self.toks = _en_tokens(name_en or name)
        self.cn = _cn_chars(name)


def _bucket_keys(m: _Member) -> list[tuple[str, str]]:
    """分桶键：英文首词（冠词让位）/ 泛用次词 / 中文前两字。"""
    keys: list[tuple[str, str]] = []
    toks = m.toks
    if toks:
        first = toks[1] if toks[0] in ("the", "a", "an") and len(toks) > 1 else toks[0]
        keys.append(("EN", first))
        if len(toks) > 1 and toks[1] not in _GENERIC_MINOR and not toks[1].isdigit():
            keys.append(("EN2", toks[1]))
    if len(m.cn) >= 2:
        keys.append(("CN", m.cn[:2]))
    return keys


def build_clusters(
    rows: list[tuple[int, str, str | None]],
) -> list[tuple[str, list[int]]]:
    """名称行 → 系列簇列表 [(系列名, [appid...])]，只含 ≥2 成员的簇。"""
    members = [_Member(a, n or "", e) for a, n, e in rows if (n or "").strip()]
    parent = list(range(len(members)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    buckets: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, m in enumerate(members):
        for key in _bucket_keys(m):
            buckets[key].append(i)

    for indices in buckets.values():
        if len(indices) < 2:
            continue
        for a in range(len(indices)):
            i = indices[a]
            for b in range(a + 1, len(indices)):
                j = indices[b]
                if find(i) == find(j):
                    continue
                mi, mj = members[i], members[j]
                if is_same_series(mi.name, mi.name_en, mj.name, mj.name_en):
                    union(i, j)

    groups: dict[int, list[_Member]] = defaultdict(list)
    for i, m in enumerate(members):
        groups[find(i)].append(m)

    out: list[tuple[str, list[int]]] = []
    for grp in groups.values():
        if len(grp) < _CLUST_MIN_MEMBER:
            continue
        grp.sort(key=lambda m: m.appid)
        out.append((series_name(grp), [m.appid for m in grp]))
    out.sort(key=lambda x: x[0].lower())
    return out


def series_name(grp: list[_Member]) -> str:
    """系列标识名：成员英文名的逐位众数前缀（剥离开头冠词后计算）。

    众数而非严格交集：东方 Project 簇里混着 Abyss Soul Lotus 这类不带
    touhou 首词的成员，严格交集会整段打断、系列名回落成单个成员名；
    逐位取「半数以上成员一致」的词即可稳定停在 Touhou / Total War /
    Assassin's Creed 这一档。纯中文名成员没有英文 token，不参与。
    """
    token_lists = []
    for m in grp:
        toks = m.toks
        if toks and toks[0] in ("the", "a", "an"):
            toks = toks[1:]
        if toks:
            token_lists.append(toks)
    prefix: list[str] = []
    i = 0
    while token_lists and i < 6:
        counts: dict[str, int] = defaultdict(int)
        for toks in token_lists:
            if i < len(toks):
                counts[toks[i]] += 1
        if not counts:
            break
        # 位 0 只求半数一致（定锚系列词，容忍个别成员写法跑偏）；位 ≥1
        # 须全员一致——有成员在该位提前耗尽或取词不同即分歧证据，否则
        # Portal 簇会被多数派的 2 拖成 "Portal 2"
        tok, cnt = max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))
        need = max(2, (len(token_lists) + 1) // 2) if i == 0 else len(token_lists)
        if cnt < need:
            break
        prefix.append(tok)
        i += 1

    def _display(tokens: list[str]) -> str:
        joined = " ".join(t for t in tokens if len(t) > 1 or t.isdigit())
        return joined.title() if joined.strip() else ""

    shortest = min(grp, key=lambda m: len(m.name_en or m.name))
    fallback = (shortest.name_en or shortest.name).strip()
    if prefix:
        return (_display(prefix) or _display(token_lists[0]) or fallback)[
            :_SERIES_ID_MAX
        ]
    return fallback[:_SERIES_ID_MAX]


def _display_name_for(names: list[str], fallback: str) -> str:
    """展示名：成员展示名的最长公共汉字前缀（≥2 字）；否则回落系列标识。"""
    cn_lists = [_cn_chars(n) for n in names]
    prefix: list[str] = []
    i = 0
    while True:
        cand = None
        ok = True
        for cs in cn_lists:
            if i >= len(cs):
                ok = False
                break
            if cand is None:
                cand = cs[i]
            elif cs[i] != cand:
                ok = False
                break
        if not ok or cand is None:
            break
        prefix.append(cand)
        i += 1
    if len(prefix) >= 2:
        return "".join(prefix)
    return fallback


async def has_unassigned() -> bool:
    """还有没跑过识别的可聚类行吗（爬后链的空转闸）。"""
    q = select(1).where(Game.series_id.is_(None), or_(*_clusterable_conds())).limit(1)
    async with get_session_factory()() as session:
        return bool((await session.execute(q)).scalar())


async def refresh_series() -> int:
    """全库重算 series_id（差异写回），返回更新的行数。

    series_id 语义：NULL=未计算；''=已算落单；非空=系列标识。落单行写
    空串而非 NULL，has_unassigned 才不会对同一批行每轮空转重算。
    CPU 聚类放线程池，不占事件循环；写回按主键批量 executemany 单事务
    提交。幂等：结果不变的行零写入。
    """
    async with get_session_factory()() as session:
        rows = (
            (
                await session.execute(
                    select(Game.appid, Game.name, Game.name_en, Game.series_id).where(
                        or_(*_clusterable_conds())
                    )
                )
            ).all()
        )
        if not rows:
            return 0

        clusters = await asyncio.to_thread(
            build_clusters, [(r.appid, r.name, r.name_en) for r in rows]
        )
        new_map: dict[int, str] = {}
        for name, appids in clusters:
            for a in appids:
                new_map[a] = name
        for r in rows:
            new_map.setdefault(r.appid, "")

        updates = [
            {"appid": r.appid, "series_id": new_map[r.appid]}
            for r in rows
            # None ≠ ''：NULL（未计算）→ ''（已算落单）必须落库，否则
            # has_unassigned 对落单行永远为真，爬后链每轮空转重算
            if r.series_id != new_map[r.appid]
        ]
        if updates:
            await session.execute(update(Game), updates)
            await session.commit()
        return len(updates)


async def series_members(appid: int) -> dict | None:
    """同系列成员（GPW「同系列」区块数据源）。无系列 / 游戏不存在 → None。"""
    from app.domains.games.models import GameCurrentPrice

    async with get_session_factory()() as session:
        game = await session.get(Game, appid)
        if game is None or not (game.series_id or ""):
            return None
        sid = game.series_id
        rows = (
            (
                await session.execute(
                    select(
                        Game.appid,
                        Game.name,
                        Game.header_image,
                        Game.type,
                        Game.min_cny_fen,
                        Game.diff_fen,
                        GameCurrentPrice.price,
                        GameCurrentPrice.original_price,
                        GameCurrentPrice.discount_percent,
                        GameCurrentPrice.price_status,
                    )
                    .outerjoin(
                        GameCurrentPrice,
                        and_(
                            GameCurrentPrice.appid == Game.appid,
                            GameCurrentPrice.region_code == "CN",
                        ),
                    )
                    .where(Game.series_id == sid)
                    .order_by(Game.appid)
                )
            )
            .all()
        )

    def _row(r, self_appid: int) -> dict:
        cn_ok = r.price_status == "ok" and r.price is not None
        cn_fen = int(r.price) if cn_ok else None
        lowest = (
            r.min_cny_fen
            if r.min_cny_fen is not None
            else cn_fen
        )
        return {
            "appid": int(r.appid),
            "name": r.name,
            "headerImage": r.header_image,
            "isSelf": int(r.appid) == self_appid,
            "type": r.type or "game",
            "cnPriceFen": cn_fen,
            "cnOriginalFen": int(r.original_price) if cn_ok and r.original_price is not None else None,
            "cnDiscount": int(r.discount_percent or 0) if cn_ok else 0,
            "lowestPriceFen": int(lowest) if lowest is not None else None,
            "savingsFen": max(int(r.diff_fen or 0), 0),
        }

    self_row = next((r for r in rows if int(r.appid) == appid), None)
    members = (
        [_row(self_row, appid)]
        + [_row(r, appid) for r in rows if int(r.appid) != appid]
        if self_row
        else [_row(r, appid) for r in rows]
    )
    series_id = sid.strip()[:_SERIES_ID_MAX]
    return {
        "seriesId": series_id,
        "seriesName": _display_name_for([m["name"] for m in members], series_id),
        "members": members,
    }
