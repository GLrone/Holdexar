"""游戏系列识别验收：判定规则正负例 + 聚类命名 + 落库 + 成员端点数据。

规则层用真实库校准时踩过的坑做断言样本（X of Y 撞车 / 品牌伞 / 裸序号
枢纽 / 版本号 2.0），落库与端点跑 tmp 库（refresh_series 是全库写，
不碰真实库）。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.games.models import Game, GameCurrentPrice  # noqa: E402
from app.domains.games.series import (  # noqa: E402
    build_clusters,
    has_unassigned,
    is_same_series,
    refresh_series,
    series_members,
)

APP_PORTAL = 990_301
APP_PORTAL2 = 990_302
APP_ALONE = 990_303
APP_W3 = 990_304
APP_W2 = 990_305
APP_DLC = 990_306
APP_NULLTYPE = 990_307
ALL_APPS = [APP_PORTAL, APP_PORTAL2, APP_ALONE, APP_W3, APP_W2, APP_DLC, APP_NULLTYPE]


# ─── 判定规则：正例 ────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "n1,e1,n2,e2",
    [
        # 规则 4：纯序号续作
        ("Portal", "Portal", "Portal 2", "Portal 2"),
        ("火炬之光", "Torchlight", "火炬之光2", "Torchlight II"),
        # 规则 3：≥3 词公共前缀
        ("Grand Theft Auto IV", "Grand Theft Auto IV",
         "Grand Theft Auto: San Andreas", "Grand Theft Auto: San Andreas"),
        # 规则 5：同前缀 + 尾部序号互异（年货）
        ("FIFA 22", "FIFA 22", "FIFA 23", "FIFA 23"),
        ("NBA 2K25", "NBA 2K25", "NBA 2K26", "NBA 2K26"),
        # 规则 1b：分隔符前缀只差尾部序号
        ("The Witcher: Enhanced Edition", "The Witcher: Enhanced Edition",
         "The Witcher 3: Wild Hunt", "The Witcher 3: Wild Hunt"),
        # 罗马数字归一
        ("Sid Meier's Civilization V", "Sid Meier's Civilization V",
         "Sid Meier's Civilization VI", "Sid Meier's Civilization VI"),
        # 规则 6：中文严格前缀
        ("辐射4", "Fallout 4", "辐射：新维加斯", "Fallout: New Vegas"),
        # 规则 7：中文一致 + 英文相似作保
        ("圣剑传说2 SECRET of MANA", "Secret of Mana",
         "圣剑传说3 TRIALS of MANA", "Trials of Mana"),
        # 撇号归一：all's → alls 单词
        ("Tony Hawk's Pro Skater 1 + 2", "Tony Hawk's Pro Skater 1 + 2",
         "Tony Hawk's Pro Skater 3 + 4", "Tony Hawk's Pro Skater 3 + 4"),
    ],
)
def test_same_series_positive(n1, e1, n2, e2):
    assert is_same_series(n1, e1, n2, e2)


# ─── 判定规则：负例（校准期真实踩过的误并形态） ────────────────────────

@pytest.mark.parametrize(
    "n1,e1,n2,e2",
    [
        # X of Y 前缀撞车（规则 3 的 2 词弱证据门槛）
        ("Call of Duty", "Call of Duty", "Call of Juarez", "Call of Juarez"),
        ("Call of Duty", "Call of Duty", "Call of Cthulhu", "Call of Cthulhu"),
        ("Age of Empires II", "Age of Empires II", "Age of Mythology", "Age of Mythology"),
        ("Tales of Zestiria", "Tales of Zestiria", "Tales of Symphonia", "Tales of Symphonia"),
        ("How to Survive", "How to Survive", "How to Raise a Wolf Girl", "How to Raise a Wolf Girl"),
        # The X of Y 三词虚词框架（冠词剥离后落回 2 词门槛）
        ("The Legend of Heroes: Trails in the Sky", "The Legend of Heroes: Trails in the Sky",
         "The Legend of Bum-Bo", "The Legend of Bum-Bo"),
        # 品牌伞不是系列
        ("Tom Clancy's Rainbow Six Vegas", "Tom Clancy's Rainbow Six Vegas",
         "Tom Clancy's Ghost Recon", "Tom Clancy's Ghost Recon"),
        ("Sid Meier's Civilization IV", "Sid Meier's Civilization IV",
         "Sid Meier's Pirates!", "Sid Meier's Pirates!"),
        # I Am X / 我是 X 起手词
        ("I Am Alive", "I Am Alive", "I Am Bread", "I Am Bread"),
        ("我是小鱼儿", "I Am Fish", "我是未来：悠闲末日生活", None),
        # 裸序号零证据：name_en 就是个 "2" 的行不得互连
        ("明星志愿2", "明星志願2", "铁道少女:梦想轨迹 2.0", "铁道少女:梦想轨迹 2.0 Railway To Dream"),
        ("懒人修仙传2", "懒人修仙传2", "幻想三国志2", "幻想三国志2"),
        # 通用词系列名排除
        ("星际战甲", "Warframe", "星际公民", "Star Citizen"),
        ("模拟人生", "The Sims", "模拟山羊", "Goat Simulator"),
        # 前缀后不是序号的续作证据不足
        ("Portal", "Portal", "Portal Stories: Mel", "Portal Stories: Mel"),
    ],
)
def test_same_series_negative(n1, e1, n2, e2):
    assert not is_same_series(n1, e1, n2, e2)


# ─── 聚类与命名 ────────────────────────────────────────────────────────

def _cluster_of(clusters, appid):
    for name, appids in clusters:
        if appid in appids:
            return name, appids
    return None, None


def test_cluster_transitive_and_naming():
    rows = [
        (1, "Portal", "Portal"),
        (2, "Portal 2", "Portal 2"),
        (3, "Portal 2 - The Final Hours", "Portal 2 - The Final Hours"),
        (4, "Fallout 4", "Fallout 4"),
        (5, "Fallout 76", "Fallout 76"),
        (6, "无系列单机", "Some Random Game"),
    ]
    clusters = build_clusters(rows)
    assert len(clusters) == 2
    name, members = _cluster_of(clusters, 1)
    assert name == "Portal" and set(members) == {1, 2, 3}
    name, members = _cluster_of(clusters, 4)
    assert name == "Fallout" and set(members) == {4, 5}
    assert _cluster_of(clusters, 6) == (None, None)


def test_cluster_naming_tolerates_offprefix_member():
    """众数前缀：个别成员英文名不带系列词（东方式同人命名）不打断命名。

    簇的枢纽形态取自真实库：中文名只剩「东方」两字的成员经中文严格前缀
    规则把全部东方 Project 同人游戏拉成一簇。"""
    rows = [
        (1, "东方New World", "Touhou: New World"),
        (2, "东方梦零魂", "TouHou Nil Soul"),
        (3, "东方雪莲华 ～ Abyss Soul Lotus.", "Abyss Soul Lotus"),
    ]
    clusters = build_clusters(rows)
    assert len(clusters) == 1
    assert clusters[0][0] == "Touhou"
    assert set(clusters[0][1]) == {1, 2, 3}


def test_cluster_bare_number_rows_do_not_glue():
    """name_en 是裸序号（"2" / 全中文）的行互不成簇、也不当枢纽。"""
    rows = [
        (1, "明星志愿2", "明星志願2"),
        (2, "懒人修仙传2", "懒人修仙传2"),
        (3, "幻想三国志2", "幻想三国志2"),
    ]
    assert build_clusters(rows) == []


def test_cluster_cn_only_members_pair_via_cn_name():
    """无英文名的行靠中文名成对（规则 7），系列名回落中文名。"""
    rows = [
        (1, "琉隐", None),
        (2, "琉隐九绝", None),
    ]
    clusters = build_clusters(rows)
    assert len(clusters) == 1
    assert set(clusters[0][1]) == {1, 2}


# ─── 落库与成员端点数据（tmp 库全隔离，不碰真实库——refresh_series 是
# ─── 全库写，打真实库会把 1.4 万行 series_id 连带洗掉） ────────────────

@pytest_asyncio.fixture()
async def _series_db(tmp_path, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    from app.domains.games import series as series_mod

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'series.db').as_posix()}", echo=False
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(series_mod, "get_session_factory", lambda: factory)
    yield factory
    await engine.dispose()


@pytest_asyncio.fixture()
async def _seed_and_clean(_series_db):
    async with _series_db() as session:
        session.add_all(
            [
                Game(appid=APP_PORTAL, name="Portal", name_en="Portal", type="GAME"),
                Game(appid=APP_PORTAL2, name="Portal 2", name_en="Portal 2", type="GAME"),
                Game(appid=APP_ALONE, name="战地无界", name_en="Some Battle Game", type="GAME"),
                Game(appid=APP_W3, name="巫师3", name_en="The Witcher 3: Wild Hunt", type="GAME"),
                Game(appid=APP_W2, name="The Witcher 2", name_en="The Witcher 2: Assassins of Kings", type="GAME"),
                Game(appid=APP_DLC, name="Portal 2 DLC", name_en="Portal 2 Sixense DLC", type="DLC"),
                Game(appid=APP_NULLTYPE, name="Portal Stories: Mel", name_en="Portal Stories: Mel"),
            ]
        )
        session.add(
            GameCurrentPrice(
                appid=APP_PORTAL, region_code="CN", currency="CNY",
                price=4800, original_price=4800, discount_percent=0,
                price_status="ok", cny_fen=4800,
            )
        )
        await session.commit()
    yield


@pytest.mark.asyncio
async def test_refresh_series_writes_groups_and_sentinel(_seed_and_clean, _series_db):
    assert await has_unassigned() is True
    changed = await refresh_series()
    assert changed == len(ALL_APPS) - 1  # 可聚类行首刷全动（DLC 行不参与）
    async with _series_db() as session:
        portal = await session.get(Game, APP_PORTAL)
        portal2 = await session.get(Game, APP_PORTAL2)
        alone = await session.get(Game, APP_ALONE)
        w3 = await session.get(Game, APP_W3)
        w2 = await session.get(Game, APP_W2)
        dlc = await session.get(Game, APP_DLC)
        assert portal.series_id == "Portal" and portal2.series_id == "Portal"
        # 落单行写空串哨兵（NULL=未计算，空串=已算无系列）
        assert alone.series_id == ""
        # 分隔符前缀去尾号：Witcher 2/3 归同簇
        assert w3.series_id == "Witcher" and w2.series_id == "Witcher"
        # DLC 不参与聚类，series_id 保持 NULL
        assert dlc.series_id is None
    # 幂等：无变化时零写入
    assert await has_unassigned() is False
    assert await refresh_series() == 0


@pytest.mark.asyncio
async def test_series_members_payload(_seed_and_clean):
    await refresh_series()
    data = await series_members(APP_PORTAL)
    assert data is not None
    assert data["seriesId"] == "Portal"
    assert data["seriesName"] == "Portal"
    self_members = [m for m in data["members"] if m["isSelf"]]
    assert len(self_members) == 1 and self_members[0]["appid"] == APP_PORTAL
    by_id = {m["appid"]: m for m in data["members"]}
    assert set(by_id) == {APP_PORTAL, APP_PORTAL2}
    # CN 价格行直通；无价成员价格字段为 None
    assert by_id[APP_PORTAL]["cnPriceFen"] == 4800
    assert by_id[APP_PORTAL2]["cnPriceFen"] is None

    # 落单 / 不存在 → None（路由层转 404）
    assert await series_members(APP_ALONE) is None
    assert await series_members(999_999_999) is None
