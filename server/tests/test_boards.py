"""榜单发现源测试：抓取解析 / 三级缓存 / 反哺差集语义。

抓取层全 mock（不打真网、零配额消耗）：
- 三源参数（topsellers/popularnew/specials 的 query 参数）；
- 终止条件：凑满封顶 / 连续 empty_tolerance 批零新增（特惠 3 批）；
- 缓存三级策略（热 1h → miss 拉取 → stale 24h 兜底，分板隔离）；
- backfill_specs 差集语义：topsellers 补无行+挂名孤儿 vs specials 仅补无行。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.games import service
from app.domains.games import boards as boards_mod
from app.core.config import get_settings
from app.core.database import init_db


@pytest_asyncio.fixture(autouse=True)
async def _setup():
    boards_mod._reset_cache_for_tests()
    await init_db()
    yield
    boards_mod._reset_cache_for_tests()


def _steam_page(appids: list[int]) -> dict:
    """构造 search/results JSON 单批（logo URL 携带 appid，对齐真实响应形态）。"""
    return {
        "items": [
            {"logo": f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{a}/logo.png"}
            for a in appids
        ]
    }


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """按序返回预置响应；记录请求过的 params。支持 async with 协议。

    响应耗尽后返回空批（真实榜单末尾 items=[] 触发停止收集），
    夹具只需给出关心的批次。
    """

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, proxy=None):
        self.calls.append({"url": url, "params": params, "proxy": proxy})
        item = self.responses.pop(0) if self.responses else {"items": []}
        if isinstance(item, Exception):
            raise item
        return _FakeResponse(item) if isinstance(item, dict) else item

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _mock_fetch(monkeypatch, responses: list, proxy_url: str | None = None) -> _FakeSession:
    session = _FakeSession(responses)

    class _SessionFactory:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return session

        async def __aexit__(self, *exc):
            return False

    async def _fake_proxy():
        return proxy_url

    monkeypatch.setattr(boards_mod.aiohttp, "ClientSession", _SessionFactory)
    monkeypatch.setattr(boards_mod, "_strategy_proxy", _fake_proxy)
    return session


# ── 三源参数与终止条件 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_topsellers_params_and_dedup(monkeypatch):
    """topsellers：参数对齐 top100；拉满 5 页（500 条 = 初始游戏库发现面）即停。"""
    pages = [list(range(101 + i * 100, 201 + i * 100)) for i in range(5)]
    session = _mock_fetch(monkeypatch, [_steam_page(p) for p in pages])

    appids = await boards_mod.fetch_board("topsellers")

    expected = [a for p in pages for a in p]
    assert appids == expected
    assert len(session.calls) == 5  # 已凑满 500：第 6 批不再请求
    p = session.calls[0]["params"]
    assert p["filter"] == "topsellers"
    assert p["hidef2p"] == 1
    assert p["category1"] == 998


@pytest.mark.asyncio
async def test_popularnew_params_and_batching(monkeypatch):
    """popularnew：参数对齐 popular_new_supplement.py（filter + Released_DESC）。"""
    session = _mock_fetch(monkeypatch, [
        _steam_page(list(range(1, 101))),
        _steam_page(list(range(101, 201))),
        _steam_page([]),          # 第 3 批空 → 终止
    ])

    appids = await boards_mod.fetch_board("popularnew")

    assert appids == list(range(1, 201))
    p = session.calls[0]["params"]
    assert p["filter"] == "popularnew"
    assert p["sort_by"] == "Released_DESC"
    assert p["category1"] == 998


@pytest.mark.asyncio
async def test_specials_params_and_5000_cap(monkeypatch):
    """specials：封顶 5000（含在库）。"""
    # 每批 100 个新 appid；50 批给满 → 5000 封顶即停（不会发第 51 批）
    responses = [_steam_page(list(range(i * 100 + 1, (i + 1) * 100 + 1))) for i in range(60)]
    session = _mock_fetch(monkeypatch, responses)

    appids = await boards_mod.fetch_board("specials")

    assert len(appids) == 5000
    assert len(session.calls) == 50
    p = session.calls[0]["params"]
    assert p["specials"] == 1
    assert p["sort_by"] == "Global_Topsellers"
    assert p["l"] == "english"


@pytest.mark.asyncio
async def test_specials_three_empty_batches_stop(monkeypatch):
    """specials：连续 3 批零新增自动终止（不再空翻页刷请求）。

    注意「零新增」≠「items 空」：全重复批也算零新增，同样计数。
    """
    batch = list(range(1, 101))
    session = _mock_fetch(monkeypatch, [
        _steam_page(batch),        # 第 1 批 +100
        _steam_page(batch),        # 第 2 批全重复 → 零新增 ×1
        _steam_page(batch),        # 第 3 批全重复 → 零新增 ×2
        _steam_page(batch),        # 第 4 批全重复 → 零新增 ×3 → 终止
    ])

    appids = await boards_mod.fetch_board("specials")

    assert appids == batch
    assert len(session.calls) == 4  # 第 5 批不再请求


@pytest.mark.asyncio
async def test_partial_on_failure(monkeypatch):
    """中途批 HTTP 失败：返回已收集部分（break 语义），不炸。"""
    session = _mock_fetch(
        monkeypatch,
        [_steam_page([111, 222]), _FakeResponse({}, status=503)],
    )

    appids = await boards_mod.fetch_board("topsellers")

    assert appids == [111, 222]
    assert len(session.calls) == 2


@pytest.mark.asyncio
async def test_unknown_board_raises():
    with pytest.raises(ValueError, match="未知榜单源"):
        await boards_mod.fetch_board("nosuchboard")


# ── 三级缓存（分板隔离）──────────────────────────────────────


@pytest.mark.asyncio
async def test_cache_hit_and_isolation(monkeypatch):
    """热缓存命中：1h 内二次调用不再发请求；三板缓存互不污染。"""
    _mock_fetch(monkeypatch, [_steam_page([1, 2, 3])])

    first = await boards_mod.get_board("topsellers")
    second = await boards_mod.get_board("topsellers")
    assert first == second == [1, 2, 3]

    # popularnew 独立缓存：不受 topsellers 命中影响
    _mock_fetch(monkeypatch, [_steam_page([9])])
    assert await boards_mod.get_board("popularnew") == [9]


@pytest.mark.asyncio
async def test_cache_miss_refetches(monkeypatch):
    """热缓存过期（1h）→ miss 重新拉取写回。

    每次拉取序列以空批终止（collected<封顶时 fetch 会继续拉下一批，
    夹具须显式给空批切断，第二批留给过期后的重拉）。
    """
    _mock_fetch(monkeypatch, [
        _steam_page([1]), _steam_page([]),   # 第 1 次拉取 → [1]
        _steam_page([2]),                    # 过期后重拉 → [2]
    ])

    assert await boards_mod.get_board("topsellers") == [1]
    boards_mod._board_state["topsellers"]["cache_ts"] -= boards_mod.CACHE_TTL_SECONDS + 1
    assert await boards_mod.get_board("topsellers") == [2]


@pytest.mark.asyncio
async def test_stale_fallback_on_fetch_failure(monkeypatch):
    """拉取失败 → stale 旧值兜底（24h 内）；完全失败（无 stale）返回空。"""
    _mock_fetch(monkeypatch, [
        _steam_page([7, 8]),                      # 第一次成功
        _FakeResponse({}, status=503),            # 过期后拉取失败
    ])

    assert await boards_mod.get_board("topsellers") == [7, 8]
    st = boards_mod._board_state["topsellers"]
    st["cache_ts"] -= boards_mod.CACHE_TTL_SECONDS + 1
    assert await boards_mod.get_board("topsellers") == [7, 8]  # stale 兜底

    # stale 也过期（>24h）→ 空集
    st["stale_ts"] -= boards_mod.STALE_TTL_SECONDS + 1
    st["cache_ts"] -= boards_mod.STALE_TTL_SECONDS + 1
    assert await boards_mod.get_board("topsellers") == []


@pytest.mark.asyncio
async def test_refresh_keeps_cache_on_empty(monkeypatch):
    """定时预热拉到空结果：不动旧缓存（refresh 返回旧值兜底）。"""
    _mock_fetch(monkeypatch, [
        _steam_page([9]),
        _steam_page([]),  # 预热拉到空
    ])

    assert await boards_mod.get_board("topsellers") == [9]
    assert await boards_mod.refresh_board("topsellers") == [9]
    assert await boards_mod.get_board("topsellers") == [9]


# ── 反哺差集语义（topsellers 补孤儿 vs specials 仅补无行）──────────


@pytest.mark.asyncio
async def test_backfill_semantics_topsellers(monkeypatch):
    """topsellers：补 games 无行 + 挂名孤儿（updated_at NULL 且无价格行）。"""
    from datetime import datetime

    from sqlalchemy import delete

    from app.domains.games.models import Game

    from app.core.database import get_session_factory

    _mock_fetch(monkeypatch, [_steam_page([900001, 900002, 900003])])
    async with get_session_factory()() as session:
        # 幂等：清掉上次运行残留（真实库 + 多套件共跑时防 UNIQUE 冲突）
        await session.execute(
            delete(Game).where(Game.appid.in_([900001, 900002, 900003]))
        )
        # 900002 = 挂名孤儿（updated_at NULL 无价格行）→ topsellers 语义要补
        session.add(Game(appid=900002, name="orphan"))
        # 900003 = 已爬（updated_at 有值）→ 不补
        session.add(Game(appid=900003, name="crawled", updated_at=datetime.now()))
        await session.commit()

    try:
        specs = await boards_mod.backfill_specs("topsellers")

        appids = specs[0]["appids"] if specs else []
        assert set(appids) == {900001, 900002}   # 900003 已爬排除
        assert specs[0]["kind"] == "top100_backfill"
    finally:
        async with get_session_factory()() as session:
            await session.execute(
                delete(Game).where(Game.appid.in_([900001, 900002, 900003]))
            )
            await session.commit()


@pytest.mark.asyncio
async def test_backfill_semantics_specials_uncrawled_only(monkeypatch):
    """specials：仅补 games 无行的差集——挂名孤儿一律留给回补层。"""
    from datetime import datetime

    from sqlalchemy import delete

    from app.domains.games.models import Game

    from app.core.database import get_session_factory

    _mock_fetch(monkeypatch, [_steam_page([900011, 900012, 900013])])
    async with get_session_factory()() as session:
        # 幂等：清掉上次运行残留（真实库 + 多套件共跑时防 UNIQUE 冲突）
        await session.execute(
            delete(Game).where(Game.appid.in_([900011, 900012, 900013]))
        )
        # 900012 = 挂名孤儿——specials 语义不碰（回补层负责）
        session.add(Game(appid=900012, name="orphan"))
        # 900013 = 已爬
        session.add(Game(appid=900013, name="crawled", updated_at=datetime.now()))
        await session.commit()

    try:
        specs = await boards_mod.backfill_specs("specials")

        appids = specs[0]["appids"] if specs else []
        assert set(appids) == {900011}            # 仅无行差集
        assert specs[0]["kind"] == "specials_backfill"
    finally:
        async with get_session_factory()() as session:
            await session.execute(
                delete(Game).where(Game.appid.in_([900011, 900012, 900013]))
            )
            await session.commit()


@pytest.mark.asyncio
async def test_backfill_limit_and_empty(monkeypatch):
    """limit 封顶单轮反哺量；拉取失败（空榜）返回 [] 不产任务。"""
    _mock_fetch(monkeypatch, [_FakeResponse({}, status=503)])
    assert await boards_mod.backfill_specs("topsellers") == []

    _mock_fetch(monkeypatch, [_steam_page(list(range(910001, 910010)))])
    specs = await boards_mod.backfill_specs("topsellers", limit=3)
    assert specs[0]["appids"] == [910001, 910002, 910003]


# ── 反哺调度接线：发现源常开，不受自动价格总开关管 ──────────────


@pytest.mark.asyncio
async def test_scheduler_board_job_backfills_even_when_auto_price_off(monkeypatch):
    """crawl.auto_price 关闭 → 榜单反哺照常（监控队列发现源不是价格更新作业）。

    接线断言：预热 → backfill_specs → run_sequential(from_scheduler=True)，
    开关关闭不得让反哺停转（无代理时的拦截属代理闸门职责，另有用例）。
    """
    from app.core import scheduler as sched_mod
    from app.domains.crawl import service as crawl_service

    async def _refresh_board(key: str) -> list[int]:
        return [900001, 900002]

    async def _specs(key: str, limit: int = 100):
        return [{"scope": "appids", "appids": [900001, 900002], "kind": "top100_backfill"}]

    calls: list = []

    async def _run_sequential(specs, **kw):
        calls.append((specs, kw))
        return [{"id": 1}]

    async def _auto_off() -> bool:
        return False

    monkeypatch.setattr(boards_mod, "refresh_board", _refresh_board)
    monkeypatch.setattr(boards_mod, "backfill_specs", _specs)
    monkeypatch.setattr(crawl_service, "run_sequential", _run_sequential)
    monkeypatch.setattr(sched_mod, "price_auto_enabled", _auto_off)

    await sched_mod._make_board_job("topsellers")()

    assert calls, "开关关闭不得停掉榜单反哺（发现源常开）"
    assert calls[0][0][0]["kind"] == "top100_backfill"
    assert calls[0][1].get("from_scheduler") is True, "反哺仍是自动路径，须过代理闸门"


@pytest.mark.asyncio
async def test_scheduler_board_job_records_preset(monkeypatch):
    """record_preset=True（热销榜）：本轮榜整批登记进预设池清单。"""
    from app.core import scheduler as sched_mod
    from app.domains.games import preset as preset_mod

    async def _refresh_board(key: str) -> list[int]:
        return [900101, 900102]

    async def _specs(key: str, limit: int = 100):
        return []

    recorded: list = []

    async def _record_board(appids, source="topsellers"):
        recorded.append((list(appids), source))
        return len(appids)

    monkeypatch.setattr(boards_mod, "refresh_board", _refresh_board)
    monkeypatch.setattr(boards_mod, "backfill_specs", _specs)
    monkeypatch.setattr(preset_mod, "record_board", _record_board)

    await sched_mod._make_board_job("topsellers", record_preset=True)()

    assert recorded == [([900101, 900102], "topsellers")]


@pytest.mark.asyncio
async def test_scheduler_board_job_other_boards_skip_preset(monkeypatch):
    """默认（其他榜单）不登记预设池——登记只挂热销榜。"""
    from app.core import scheduler as sched_mod
    from app.domains.games import preset as preset_mod

    async def _refresh_board(key: str) -> list[int]:
        return [900103]

    async def _specs(key: str, limit: int = 100):
        return []

    recorded: list = []

    async def _record_board(appids, source="topsellers"):
        recorded.append(list(appids))
        return len(appids)

    monkeypatch.setattr(boards_mod, "refresh_board", _refresh_board)
    monkeypatch.setattr(boards_mod, "backfill_specs", _specs)
    monkeypatch.setattr(preset_mod, "record_board", _record_board)

    await sched_mod._make_board_job("popularnew")()

    assert recorded == []


# ── list_games 集成（真实本地库只读 + mock 热榜）──────────────


def _library_has_games() -> bool:
    import sqlite3

    settings = get_settings()
    con = sqlite3.connect(str(settings.data_dir / settings.db_filename))
    try:
        return con.execute("SELECT COUNT(*) FROM games").fetchone()[0] >= 100
    finally:
        con.close()


@pytest.mark.asyncio
async def test_list_top100_empty_board(monkeypatch):
    """热榜拉取失败（空榜）→ 列表返回空集而非随机游戏。"""
    _mock_fetch(monkeypatch, [_FakeResponse({}, status=503)])

    r = await service.list_games(sort="top100")

    assert r == {"items": [], "total": 0, "hasMore": False, "nextCursor": None}


@pytest.mark.asyncio
async def test_list_top100_order_and_pagination(monkeypatch):
    """榜序重排 + 切片分页：页内序=榜序；翻页不相交；total 恒定。"""
    if not _library_has_games():
        pytest.skip("本地库无数据，集成用例跳过")

    # 用例契约是「纯榜序」；关注置顶前缀（games 列表的排序第一位）读的是
    # 用户手工策展的真实库 manual 行，会让榜内被关注的款越到队首——
    # 桩成空集，保持本用例对用户态数据封闭。
    async def _no_follows() -> list[int]:
        return []

    monkeypatch.setattr(service, "_followed_appids", _no_follows)

    import sqlite3

    settings = get_settings()
    con = sqlite3.connect(str(settings.data_dir / settings.db_filename))
    try:
        # 榜单取库里真实存在且有国区 ok 价的 12 款（乱序构造，验证重排）
        rows = con.execute(
            "SELECT g.appid FROM games g "
            "JOIN game_current_prices cn ON cn.appid = g.appid AND cn.region_code='CN' "
            "AND cn.price_status='ok' AND cn.price > 0 "
            "WHERE g.name IS NOT NULL AND g.name != '' "
            "ORDER BY g.appid DESC LIMIT 12"
        ).fetchall()
    finally:
        con.close()
    board = [r[0] for r in rows]
    assert len(board) == 12

    _mock_fetch(monkeypatch, [_steam_page(board)])

    p1 = await service.list_games(sort="top100", limit=5)
    assert p1["total"] == 12
    assert [i["appid"] for i in p1["items"]] == board[:5]
    assert p1["hasMore"] and p1["nextCursor"]

    p2 = await service.list_games(sort="top100", limit=5, after=p1["nextCursor"])
    assert [i["appid"] for i in p2["items"]] == board[5:10]
    assert not ({i["appid"] for i in p1["items"]} & {i["appid"] for i in p2["items"]})

    p3 = await service.list_games(sort="top100", limit=5, after=p2["nextCursor"])
    assert [i["appid"] for i in p3["items"]] == board[10:]
    assert p3["hasMore"] is False and p3["nextCursor"] is None
