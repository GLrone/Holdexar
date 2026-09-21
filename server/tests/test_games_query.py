"""games 列表查询的行为验收（三层兜底查表模式）。

以 priceMatrix（区服键控 {"CN": [formatted, cnyFen, cents, null]}）为独立事实源，
验证每条 SQL 筛选的语义正确性；不依赖实现细节。

⚠️ 用例针对**真实本地库**（只读）。库被清空（如重置数据）时前提不成立，
模块级 skip 而非误报失败——数据重新导入后自动恢复执行。

sort=top100 用例在 test_top100.py（热榜数据源 mock，不用空集占位）。
"""
import sqlite3

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.config import get_settings
from app.core.database import get_session_factory, init_db
from app.domains.games import service


TOLERANCE = 500  # 5 元（分）


@pytest.fixture(autouse=True)
def _require_populated_library():
    """games 表为空时跳过（真实库用例，同步夹具内 skip 兼容性最好）。"""
    settings = get_settings()
    db_path = settings.data_dir / settings.db_filename
    con = sqlite3.connect(str(db_path))
    try:
        count = con.execute("SELECT COUNT(*) FROM games").fetchone()[0]
    finally:
        con.close()
    if count < 100:
        # 行数过少视为库处于测试/半清空状态（并行测试会话种合成行），
        # 真实库前提不成立，skip 而非误报
        pytest.skip(f"本地库 games 仅 {count} 行（数据已清空/测试种子中），真实库用例跳过")


@pytest_asyncio.fixture(autouse=True)
async def _setup():
    await init_db()
    await service.refresh_sort_cache()  # 幂等，保证预计算列最新
    yield


def _matrix(item: dict) -> dict:
    return item["priceMatrix"]


def _cn(item: dict) -> dict | None:
    m = _matrix(item)
    return m.get("CN")


def _lowest_other(item: dict) -> int | None:
    """非 CN 各区最低 cnyFen（无则回落国区），复刻 _build_list_item 语义。"""
    cn = _cn(item)
    base = cn[1] if cn else None
    lowest = base
    for code, data in _matrix(item).items():
        if code == "CN":
            continue
        v = data[1]
        if v and (lowest is None or v < lowest):
            lowest = v
    return lowest


async def _fetch(**kw):
    kw.setdefault("limit", 40)
    return await service.list_games(**kw)


@pytest.mark.asyncio
async def test_default_shape():
    r = await _fetch()
    assert r["total"] > 0
    assert 0 < len(r["items"]) <= 40
    assert r["hasMore"] is True and r["nextCursor"]
    for item in r["items"]:
        assert isinstance(_matrix(item), dict)
        assert item["appid"] > 0 and item["name"]


@pytest.mark.asyncio
async def test_pagination_disjoint():
    p1 = await _fetch()
    p2 = await _fetch(after=p1["nextCursor"])
    ids1 = {i["appid"] for i in p1["items"]}
    ids2 = {i["appid"] for i in p2["items"]}
    assert not (ids1 & ids2)
    assert p2["total"] == p1["total"]


@pytest.mark.asyncio
async def test_is_lowest_semantics():
    """isLowest = 国区近似全区最低（±5 元容差）：最低价 >= 国区 - 500 分。"""
    r = await _fetch(is_lowest=True)
    assert r["items"], "isLowest 应有结果（基准 2352 款）"
    for item in r["items"]:
        cn = _cn(item)
        assert cn and cn[1] > 0
        lowest = _lowest_other(item)
        assert lowest is not None and lowest >= cn[1] - TOLERANCE


@pytest.mark.asyncio
async def test_region_cn_lowest():
    """region=CN = 国区最低（±5 元容差）。"""
    r = await _fetch(region="CN")
    for item in r["items"]:
        cn = _cn(item)
        assert cn and cn[1] > 0
        assert _lowest_other(item) >= cn[1] - TOLERANCE


@pytest.mark.asyncio
async def test_region_ua_global():
    """UA global = UA 比 CN 便宜 1 元以上，且 UA 近似全区最低（±5 元）。"""
    r = await _fetch(region="UA", filter_mode="global")
    assert r["items"], "UA global 应有结果"
    for item in r["items"]:
        m = _matrix(item)
        cn, ua = m.get("CN"), m.get("UA")
        assert cn and ua
        assert ua[1] < cn[1] - 100
        lowest = min((d[1] for k, d in m.items() if k != "CN" and d[1]), default=ua[1])
        assert ua[1] <= lowest + TOLERANCE


@pytest.mark.asyncio
async def test_region_ua_cheaper():
    r = await _fetch(region="UA", filter_mode="cheaper")
    for item in r["items"]:
        m = _matrix(item)
        assert m["UA"][1] < m["CN"][1] - 100


@pytest.mark.asyncio
async def test_region_ua_highdiff():
    """highdiff = 国区未打折 且 国区-UA >= 50 元 且 UA 近似全区最低。"""
    r = await _fetch(region="UA", filter_mode="highdiff")
    for item in r["items"]:
        m = _matrix(item)
        cn, ua = m["CN"], m["UA"]
        assert cn[2] == cn[3] or cn[3] is None  # originalPrice == price → 未打折
        assert cn[1] - ua[1] >= 5000
        lowest = min((d[1] for k, d in m.items() if k != "CN" and d[1]), default=ua[1])
        assert ua[1] <= lowest + TOLERANCE


@pytest.mark.asyncio
async def test_region_locked_no_cn():
    """LOCKED = 无国区 ok 价格行（不能返回空集）。"""
    r = await _fetch(region="LOCKED")
    assert r["total"] > 0, "锁区列表不应为空"
    for item in r["items"]:
        assert "CN" not in _matrix(item)


@pytest.mark.asyncio
async def test_search():
    r = await _fetch(q="portal")
    assert 0 < r["total"] < 100
    assert all("portal" in (i["name"] or "").lower() or
               "portal" in (i.get("nameEn") or "").lower() for i in r["items"])


@pytest.mark.asyncio
async def test_filters():
    r = await _fetch(only_discounted=True, limit=20)
    assert all(i["discount"] > 0 for i in r["items"])
    r = await _fetch(only_hb=True, limit=20)
    assert all(i["isHb"] for i in r["items"])
    r = await _fetch(only_epic=True, limit=20)
    assert all(i["isEpic"] for i in r["items"])
    r = await _fetch(min_rating=90, limit=20)
    assert all((i["positiveRate"] or 0) >= 0.9 for i in r["items"])


@pytest.mark.asyncio
async def test_new2026():
    r = await _fetch(sort="new2026", limit=20)
    assert all((i["releaseDate"] or "").startswith("2026") for i in r["items"])


@pytest.mark.asyncio
async def test_flag_filter():
    """flag 标记过滤（降价动态 feed 数据源）：hl=新史低+平史低、pp=永降、any=并集。"""
    r = await _fetch(flag="hl", limit=40)
    assert r["total"] > 0, "真实库应有史低标记游戏（refresh_hl_flags 全库维护）"
    for item in r["items"]:
        assert item["hlFlag"] in (1, 2)
    r = await _fetch(flag="pp", limit=40)
    for item in r["items"]:
        assert item["ppFlag"] == 1
    r = await _fetch(flag="any", limit=40)
    for item in r["items"]:
        assert item["hlFlag"] in (1, 2) or item["ppFlag"] == 1


@pytest.mark.asyncio
async def test_updated_sort():
    """sort=updated：关注置顶前缀（通用排序第一优先级的产品语义）之下，
    updatedAt 严格降序（降价动态时间线口径）。"""
    r = await _fetch(sort="updated", limit=20)
    assert r["total"] > 0
    followed = await service._followed_appids()
    entries = [(i["appid"] in followed, i["updatedAt"] or "") for i in r["items"]]
    flags = [f for f, _ in entries]
    assert flags == sorted(flags, reverse=True), "关注块必须整体置顶"
    for block in (True, False):
        block_times = [t for f, t in entries if f == block]
        assert block_times == sorted(block_times, reverse=True), (
            "各块内 updatedAt 必须降序"
        )


@pytest.mark.asyncio
async def test_diff_range_absolute_min():
    """diffMin（absolute，分）= 与国区差价下限：已选地区时差值 = CN - 该区。"""
    r = await _fetch(region="UA", filter_mode="cheaper", diff_min_fen=2000)
    assert r["items"], "UA cheaper 且差价 >= 20 元应有结果"
    for item in r["items"]:
        m = _matrix(item)
        assert m["CN"][1] - m["UA"][1] >= 2000


@pytest.mark.asyncio
async def test_diff_range_absolute_max():
    """diffMax（absolute，分）= 与国区差价上限。"""
    r = await _fetch(region="UA", filter_mode="cheaper", diff_max_fen=5000)
    assert r["items"], "UA cheaper 且差价 <= 50 元应有结果"
    for item in r["items"]:
        m = _matrix(item)
        assert m["CN"][1] - m["UA"][1] <= 5000


@pytest.mark.asyncio
async def test_diff_range_percent():
    """percent 模式：区间值按百分比解释（交叉相乘口径，(CN-区)*100 >= pct*CN）。"""
    r = await _fetch(region="UA", filter_mode="cheaper", diff_min_fen=10, diff_type="percent")
    assert r["items"], "UA cheaper 且差价 >= 10% 应有结果"
    for item in r["items"]:
        m = _matrix(item)
        cn, ua = m["CN"][1], m["UA"][1]
        assert (cn - ua) * 100 >= 10 * cn


@pytest.mark.asyncio
async def test_diff_fallback_no_region():
    """未选地区：差价回退预计算列 diff_fen = CN - 全区最低（下限 0）。"""
    r = await _fetch(diff_min_fen=1000)
    assert r["items"], "全区最低比国区便宜 >= 10 元应有结果"
    for item in r["items"]:
        cn = _cn(item)
        assert cn and cn[1] > 0
        lowest = _lowest_other(item)
        assert cn[1] - lowest >= 1000


@pytest.mark.asyncio
async def test_tolerance_fen_param():
    """toleranceFen 参数化：收紧容差后逐条满足新容差，且结果集单调收缩。"""
    r_strict = await _fetch(region="UA", filter_mode="global", tolerance_fen=100)
    for item in r_strict["items"]:
        m = _matrix(item)
        ua = m["UA"][1]
        lowest = min((d[1] for k, d in m.items() if k != "CN" and d[1]), default=ua)
        assert ua <= lowest + 100
    r_loose = await _fetch(region="UA", filter_mode="global")
    assert r_strict["total"] <= r_loose["total"], "收紧容差不应扩大结果集"


@pytest.mark.asyncio
async def test_strict_lowest():
    """绝对低价（strictLowest）：最低区价实质低于国区（diff_fen > 0）。"""
    r = await _fetch(strict_lowest=True)
    assert r["items"], "绝对低价应有结果（存在比国区便宜的区）"
    for item in r["items"]:
        cn = _cn(item)
        assert cn and cn[1] > 0
        lowest = _lowest_other(item)
        assert cn[1] - lowest > 0


@pytest.mark.asyncio
async def test_strict_lowest_tolerance():
    """绝对低价 + 容差 20 元：差价须严格超过容差值。"""
    r = await _fetch(strict_lowest=True, tolerance_fen=2000)
    assert r["items"], "差价 > 20 元应有结果"
    r_default = await _fetch(strict_lowest=True)
    assert r["total"] <= r_default["total"], "提高容差门槛不应扩大结果集"
    for item in r["items"]:
        cn = _cn(item)
        lowest = _lowest_other(item)
        assert cn[1] - lowest > 2000


# ─── 屏蔽家庭共享（hide_family_sharing）────────────────────────────────


PRIMARY_SID = "76561190000009901"
FRIEND_SID = "76561190000009902"
APP_OWNED = 990_711   # 主账户已拥有（归属 owned）
APP_FAMILY = 990_712  # 非主账户已拥有（归属 family = 家庭共享）
APP_NONE = 990_713    # 无归属


@pytest_asyncio.fixture
async def _seed_family_filter(monkeypatch):
    """三形态归属合成行（owned / family / 无归属）+ CN 现价；收尾清理。

    主账户经 monkeypatch 提供（对准 list_games 的 _primary_steamid 取数口）。
    """
    from datetime import datetime

    from app.domains.games.models import Game, GameCurrentPrice
    from app.domains.wishlist.models import WishlistItem

    now = datetime(2026, 9, 1)
    async with get_session_factory()() as session:
        session.add_all([
            Game(appid=a, name=n, created_at=now, updated_at=now)
            for a, n in (
                (APP_OWNED, "FamTestOwned"),
                (APP_FAMILY, "FamTestFamily"),
                (APP_NONE, "FamTestNone"),
            )
        ])
        session.add_all([
            GameCurrentPrice(
                appid=a, region_code="CN", currency="CNY", price=1000,
                original_price=1000, discount_percent=0, sub_id=0,
                price_status="ok", cny_fen=1000, updated_at=now,
            )
            for a in (APP_OWNED, APP_FAMILY, APP_NONE)
        ])
        session.add_all([
            WishlistItem(steamid=PRIMARY_SID, appid=APP_OWNED, active=True, owned=True),
            WishlistItem(steamid=FRIEND_SID, appid=APP_FAMILY, active=True, owned=True),
        ])
        await session.commit()
    await service.refresh_sort_cache()

    async def _primary():
        return PRIMARY_SID

    monkeypatch.setattr(service, "_primary_steamid", _primary)
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(WishlistItem).where(
                WishlistItem.appid.in_([APP_OWNED, APP_FAMILY])
            )
        )
        await session.execute(
            delete(GameCurrentPrice).where(
                GameCurrentPrice.appid.in_([APP_OWNED, APP_FAMILY, APP_NONE])
            )
        )
        await session.execute(
            delete(Game).where(Game.appid.in_([APP_OWNED, APP_FAMILY, APP_NONE]))
        )
        await session.commit()


@pytest.mark.asyncio
async def test_hide_family_sharing(_seed_family_filter, monkeypatch):
    """屏蔽家庭共享 = 排除非主账户已拥有（family 归属）；与隐藏已拥有互为补集。

    - hide_family_sharing：family 行排除，主账户 owned 与无归属保留；
    - hide_owned 反向对照：只排主账户行，family 行保留；
    - 两者同开：只剩无归属行；
    - 未配置主账户：无 family 归属可排除（同 ownership() 判定），条件不生效。
    """

    async def _ids(**kw):
        r = await _fetch(q="FamTest", **kw)
        return {i["appid"] for i in r["items"]}

    ids = await _ids(hide_family_sharing=True)
    assert APP_FAMILY not in ids
    assert {APP_OWNED, APP_NONE} <= ids

    ids = await _ids(hide_owned=True)
    assert APP_OWNED not in ids
    assert {APP_FAMILY, APP_NONE} <= ids

    ids = await _ids(hide_owned=True, hide_family_sharing=True)
    assert ids == {APP_NONE}

    async def _no_primary():
        return ""

    monkeypatch.setattr(service, "_primary_steamid", _no_primary)
    ids = await _ids(hide_family_sharing=True)
    assert {APP_OWNED, APP_FAMILY, APP_NONE} <= ids


@pytest.mark.asyncio
async def test_flag_hl_three_states():
    """flag=new/flat/nonhl 史低三态细分（实验池对照用；既有 hl/pp/any 语义不动）。

    三条断言：各态返回项标记纯净、三态两两不重叠、三态总数恰为全库
    （划分全集——漏一边或重复计入都会让总数对不上）。
    """
    new = await _fetch(flag="new", limit=100)
    flat = await _fetch(flag="flat", limit=100)
    nonhl = await _fetch(flag="nonhl", limit=100)

    assert all(it["hlFlag"] == 1 for it in new["items"])
    assert all(it["hlFlag"] == 2 for it in flat["items"])
    assert all(it["hlFlag"] not in (1, 2) for it in nonhl["items"])

    ids_new = {it["appid"] for it in new["items"]}
    ids_flat = {it["appid"] for it in flat["items"]}
    ids_non = {it["appid"] for it in nonhl["items"]}
    assert not (ids_new & ids_flat)
    assert not (ids_new & ids_non)
    assert not (ids_flat & ids_non)

    whole = await _fetch(limit=1)
    assert new["total"] + flat["total"] + nonhl["total"] == whole["total"]

    # 既有语义回归：hl 总数 = new + flat 总数（两态不相交，见上）
    both = await _fetch(flag="hl", limit=1)
    assert both["total"] == new["total"] + flat["total"]
