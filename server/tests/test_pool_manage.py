"""监控池管理单元测试：条目增删（池页/导入）+ 愿望单成员标记 +
同步免疫（excluded / manual_pool / board_pool）+ 榜单落池 +
爬取第一优先级（mock，不触网）。

监控池四来源语义：
- 监控条目 = wishlist_items 活跃行（池内所有游戏均为必须爬取的对象）；
- 愿望单（wishlisted）与星标关注（manual）是叠加其上的爬取第一优先级；
- 监控池管理（add_pool_items / remove_pool_items / ensure_board_pool）
  负责增删与批量操作，移除以 excluded 挡同步复活（Steam 名单仍在时也
  不会被 15min 同步洗回来）；榜单落池（board_pool）同理不被轮询洗回。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.wishlist import service as wishlist_service
from app.domains.wishlist.models import TrackedAccount, WishlistItem

PRIMARY = "76561198000000001"


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module
    import app.domains.crawl.service as crawl_service
    import app.domains.games.preset as preset_mod

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(wishlist_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(preset_mod, "get_session_factory", lambda: factory)

    # persona 拉取（miniprofile）与家庭组快照：不触网 / 不依赖未建表
    async def fake_persona(steamid):
        return {"persona_name": "", "avatar_url": "", "online": False, "in_game_name": ""}

    monkeypatch.setattr(wishlist_service, "_fetch_persona", fake_persona)

    async def no_family():
        return {}

    monkeypatch.setattr(wishlist_service, "_family_personas", no_family)

    # 首爬触发统一打桩（真实 start_job 会走代理/区服链路）；
    # 需要断言调用形状的用例用 _make_start_job_stub 再换一份记录账本
    monkeypatch.setattr(crawl_service, "start_job", _make_start_job_stub([]))
    return factory


def _make_start_job_stub(calls: list[dict]):
    async def fake_start(**kwargs):
        calls.append(kwargs)
        return {"id": 1, "scope": kwargs.get("scope"), "count": len(kwargs.get("appids") or [])}

    return fake_start


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.crawl.models  # noqa: F401
    import app.domains.games.models  # noqa: F401
    import app.domains.wishlist.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


# ── 种子与桩 ─────────────────────────────────────────────


async def _seed_account(db, steamid=PRIMARY, kinds=None):
    async with db() as session:
        session.add(
            TrackedAccount(
                steamid=steamid,
                label=None,
                kinds_json=kinds or {"wishlist": True, "owned": True},
                item_count=0,
            )
        )
        await session.commit()


async def _seed_item(
    db,
    appid,
    *,
    steamid=PRIMARY,
    owned=False,
    active=True,
    manual=False,
    wishlisted=False,
    manual_pool=False,
    excluded=False,
    board_pool=False,
):
    async with db() as session:
        session.add(
            WishlistItem(
                steamid=steamid,
                appid=appid,
                active=active,
                owned=owned,
                manual=manual,
                wishlisted=wishlisted,
                manual_pool=manual_pool,
                excluded=excluded,
                board_pool=board_pool,
            )
        )
        await session.commit()


async def _get_item(db, appid, steamid=PRIMARY) -> WishlistItem | None:
    async with db() as session:
        return await session.get(WishlistItem, (steamid, appid))


def _mock_primary(monkeypatch, primary: str | None):
    """主账户解析双通道都桩掉（账号表 + 设置键），不让测试读生产配置。"""
    import app.domains.account.service as account_service
    from app.domains.settings import service as settings_service

    async def fake_table_primary():
        return primary or ""

    async def fake_get(key, default=None):
        return primary if key == "account.steam_id" else default

    monkeypatch.setattr(account_service, "get_primary_steam_id", fake_table_primary)
    monkeypatch.setattr(settings_service, "get_value", fake_get)


def _mock_wishlist(monkeypatch, items):
    async def fake_fetch_wishlist(steamid):
        return items

    monkeypatch.setattr(wishlist_service, "fetch_wishlist", fake_fetch_wishlist)


def _mock_owned(monkeypatch, games=()):
    async def fake_fetch_owned(steamid):
        return list(games), "jwt"

    monkeypatch.setattr(wishlist_service, "fetch_owned_games", fake_fetch_owned)


# ── 添加（监控池管理）─────────────────────────────────────


@pytest.mark.asyncio
async def test_add_new_item_creates_manual_pool_row(db, monkeypatch):
    """无行添加：主账号下新建 manual_pool 条目（普通监控条目），计数刷新。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)

    out = await wishlist_service.add_pool_items([620])

    assert out["added"] == 1 and out["restored"] == 0 and out["exists"] == 0
    row = await _get_item(db, 620)
    assert row is not None
    assert row.active is True
    assert row.manual_pool is True
    assert row.wishlisted is False and row.manual is False and row.excluded is False
    async with db() as session:
        account = await session.get(TrackedAccount, PRIMARY)
    assert account.item_count == 1


@pytest.mark.asyncio
async def test_add_restores_excluded_item(db, monkeypatch):
    """已脱池条目添加：复活 + 清 excluded；愿望单成员标记原样保留。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    await _seed_item(
        db, 620, active=False, wishlisted=True, excluded=True, manual=True
    )

    out = await wishlist_service.add_pool_items([620])

    assert out["restored"] == 1
    row = await _get_item(db, 620)
    assert row.active is True and row.excluded is False
    assert row.wishlisted is True  # Steam 侧成员事实不因本地移除而改写
    assert row.manual is True


@pytest.mark.asyncio
async def test_add_existing_item_reports_exists(db, monkeypatch):
    """已在池条目：exists，不动来源标记。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    await _seed_item(db, 620, active=True, owned=True)

    out = await wishlist_service.add_pool_items([620])

    assert out["exists"] == 1 and out["added"] == 0
    row = await _get_item(db, 620)
    assert row.owned is True and row.manual_pool is False


@pytest.mark.asyncio
async def test_add_requires_bound_account(db, monkeypatch):
    """无绑定账户且存在新条目：拒绝（条目行的身份是 steamid），且不落行。"""
    _mock_primary(monkeypatch, None)
    with pytest.raises(ValueError):
        await wishlist_service.add_pool_items([620])
    assert await _get_item(db, 620) is None


@pytest.mark.asyncio
async def test_add_invalid_appid_classified(db, monkeypatch):
    """无效 appid 只分类（fail），不触发账户解析。"""
    out = await wishlist_service.add_pool_items([0, -5, "abc"])
    assert out["fail"] == 3
    assert all(r["status"] == "fail" for r in out["results"])


@pytest.mark.asyncio
async def test_add_triggers_first_crawl(db, monkeypatch):
    """添加（新增/恢复）自动触发首爬：scope=appids、kind=pool_add。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    import app.domains.crawl.service as crawl_service

    calls: list[dict] = []
    monkeypatch.setattr(crawl_service, "start_job", _make_start_job_stub(calls))

    out = await wishlist_service.add_pool_items([620, 570])

    assert out["crawlTriggered"] is True
    assert len(calls) == 1
    assert calls[0]["kind"] == "pool_add"
    assert calls[0]["appids"] == [620, 570]


@pytest.mark.asyncio
async def test_add_crawl_failure_is_silent(db, monkeypatch):
    """首爬触发失败（任务占用/无代理）不阻断添加结果。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    import app.domains.crawl.service as crawl_service

    async def failing_start(**kwargs):
        raise RuntimeError("已有爬取任务在运行")

    monkeypatch.setattr(crawl_service, "start_job", failing_start)
    out = await wishlist_service.add_pool_items([620])
    assert out["added"] == 1 and out["crawlTriggered"] is False


# ── 移除（监控池管理）─────────────────────────────────────


@pytest.mark.asyncio
async def test_remove_deactivates_and_shields_sync(db, monkeypatch):
    """移除：脱池 + excluded 挡同步复活 + 清星标；愿望单成员事实保留。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    await _seed_item(db, 620, active=True, wishlisted=True, manual=True)

    out = await wishlist_service.remove_pool_items([620])
    assert out["removed"] == 1

    row = await _get_item(db, 620)
    assert row.active is False
    assert row.excluded is True
    assert row.manual is False
    assert row.wishlisted is True

    # 15min 同步（Steam 愿望单里仍在）不能把它洗回来
    _mock_wishlist(monkeypatch, [{"appid": 620, "added_at": None}])
    _mock_owned(monkeypatch, [])
    await wishlist_service.sync_account(PRIMARY, auto_crawl=False)

    row = await _get_item(db, 620)
    assert row.active is False, "excluded 条目不得被同步复活"
    assert row.wishlisted is True

    # 重新添加：复活并恢复第一优先级资格
    out = await wishlist_service.add_pool_items([620])
    assert out["restored"] == 1
    row = await _get_item(db, 620)
    assert row.active is True and row.excluded is False


@pytest.mark.asyncio
async def test_remove_missing_item(db, monkeypatch):
    """不在池的 appid：missing，不报错。"""
    await _seed_account(db)
    await _seed_item(db, 620, active=False, excluded=True, manual_pool=True)
    out = await wishlist_service.remove_pool_items([620, 570])
    assert out["removed"] == 0 and out["missing"] == 2
    row = await _get_item(db, 620)
    assert row.active is False and row.excluded is True and row.manual_pool is True


# ── 同步与成员标记 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_marks_and_clears_wishlist_member(db, monkeypatch):
    """愿望单成员资格覆写：在名单 → wishlisted=True；移出名单 → 清零并停用。"""
    await _seed_account(db)
    await _seed_item(db, 570, active=True, wishlisted=False)

    _mock_wishlist(monkeypatch, [{"appid": 570, "added_at": None}])
    _mock_owned(monkeypatch, [])
    await wishlist_service.sync_account(PRIMARY, auto_crawl=False)
    row = await _get_item(db, 570)
    assert row.wishlisted is True and row.active is True

    # 移出愿望单（非空名单，反向核对生效）
    _mock_wishlist(monkeypatch, [{"appid": 99801, "added_at": None}])
    await wishlist_service.sync_account(PRIMARY, auto_crawl=False)
    row = await _get_item(db, 570)
    assert row.wishlisted is False
    assert row.active is False


@pytest.mark.asyncio
async def test_manual_pool_immune_to_deactivation(db, monkeypatch):
    """手动入池条目：不在 Steam 愿望单也不被同步停用（对照组普通条目照停）。"""
    await _seed_account(db)
    await _seed_item(db, 620, active=True, manual_pool=True)
    await _seed_item(db, 570, active=True)  # 对照：普通条目

    _mock_wishlist(monkeypatch, [{"appid": 99801, "added_at": None}])
    _mock_owned(monkeypatch, [])
    await wishlist_service.sync_account(PRIMARY, auto_crawl=False)

    assert (await _get_item(db, 620)).active is True, "manual_pool 条目应免疫停用"
    assert (await _get_item(db, 570)).active is False


# ── 爬取第一优先级（愿望单 + 关注）────────────────────────


@pytest.mark.asyncio
async def test_pool_priority_wishlist_and_follow_first(db, monkeypatch):
    """第一优先级 = 关注 + 愿望单（关注 > 愿望单）；
    第二优先级（已购/手动入池）内部 hot 优先、appid 殿后。"""
    from datetime import datetime

    from app.domains.crawl import service as crawl_service
    from app.domains.games.models import Game, GameCurrentPrice

    now = datetime.now()
    async with db() as session:
        session.add(Game(appid=350, name="hot-owned", created_at=now, updated_at=now))
        session.add(
            GameCurrentPrice(
                appid=350, region_code="CN", currency="CNY", price=1000,
                discount_percent=50, price_status="ok", updated_at=now,
            )
        )
        await session.commit()

    await _seed_item(db, 400, active=True, manual_pool=True)  # 第二优先级（手动入池）
    await _seed_item(db, 300, active=True, owned=True)        # 第二优先级（已购）
    await _seed_item(db, 350, active=True, owned=True)        # 第二优先级 + hot
    await _seed_item(db, 200, active=True, wishlisted=True)   # 第一优先级（愿望单）
    await _seed_item(db, 100, active=True, manual=True)       # 第一优先级（关注）

    ordered = [a for a, _ in await crawl_service._resolve_scope_appids("wishlist", None)]
    assert ordered == [100, 200, 350, 300, 400], (
        "期望 [关注 100] → [愿望单 200] → [第二优先级 hot 350 → 300 → 400]，"
        f"实际 {ordered}"
    )


# ── 导入入池（任务页两个导入通道）──────────────────────────


@pytest.mark.asyncio
async def test_import_appids_adds_to_pool(db, monkeypatch):
    """导入 = 入池 + 分类：合法 appid 落 manual_pool 条目，分类口径不变。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    from app.domains.crawl import service as crawl_service

    out = await crawl_service.import_appids([620, 570, 0])

    assert out["fail"] == 1
    assert out["poolAdded"] == 2
    row = await _get_item(db, 620)
    assert row is not None and row.active is True and row.manual_pool is True
    row = await _get_item(db, 570)
    assert row is not None and row.active is True


# ── 导入文件来源登记（预设池清单：随资产种子分发的出厂游戏集）──────


@pytest.mark.asyncio
async def test_add_pool_items_records_preset_source(db, monkeypatch):
    """池页「导入文件」带 source：appid 登记进 preset_games，名字随库内主档带回。"""
    from sqlalchemy import select

    from app.domains.games.models import Game, PresetGame

    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    async with db() as session:
        session.add(Game(appid=620, name="Portal 2"))
        await session.commit()

    out = await wishlist_service.add_pool_items(
        [620, 570], source="上传优先.json"
    )
    assert out["added"] == 2

    async with db() as session:
        rows = (
            (await session.execute(select(PresetGame).order_by(PresetGame.appid)))
            .scalars()
            .all()
        )
    assert [(r.appid, r.source) for r in rows] == [
        (570, "上传优先.json"),
        (620, "上传优先.json"),
    ]
    assert rows[1].name == "Portal 2"  # 已入库的带名；未爬过的留空（导出侧再兜底）
    assert rows[0].name is None


@pytest.mark.asyncio
async def test_add_pool_items_preset_source_only_first_wins(db, monkeypatch):
    """重复导入（换文件重导）不覆盖首次来源——预设清单首见即定档。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    from app.domains.games.models import PresetGame

    await wishlist_service.add_pool_items([620], source="上传优先.json")
    await wishlist_service.add_pool_items([620], source="visual_novel_synced.json")

    async with db() as session:
        row = await session.get(PresetGame, 620)
    assert row is not None and row.source == "上传优先.json"


@pytest.mark.asyncio
async def test_add_pool_items_without_source_no_preset(db, monkeypatch):
    """不带 source 的普通添加（粘贴/池页添加）不写预设清单。"""
    from sqlalchemy import select

    from app.domains.games.models import PresetGame

    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    await wishlist_service.add_pool_items([620])

    async with db() as session:
        total = (
            await session.execute(select(PresetGame))
        ).scalars().all()
    assert total == []


# ── 榜单发现源落池（ensure_board_pool）─────────────────────


@pytest.mark.asyncio
async def test_ensure_board_pool_lands_new_items(db, monkeypatch):
    """无行：主账号下新建 board_pool 条目（普通监控条目），计数刷新。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)

    out = await wishlist_service.ensure_board_pool([620, 570])

    assert out == {"added": 2, "exists": 0, "skipped": 0}
    row = await _get_item(db, 620)
    assert row is not None
    assert row.active is True
    assert row.board_pool is True
    assert row.manual_pool is False and row.manual is False and row.wishlisted is False
    async with db() as session:
        account = await session.get(TrackedAccount, PRIMARY)
    assert account.item_count == 2


@pytest.mark.asyncio
async def test_ensure_board_pool_skips_existing_and_removed(db, monkeypatch):
    """活跃行 exists 跳过（来源标记不动）；已移除（excluded）不复活。"""
    await _seed_account(db)
    _mock_primary(monkeypatch, PRIMARY)
    await _seed_item(db, 620, active=True, owned=True)  # 已在池（已购来源）
    await _seed_item(db, 570, active=False, excluded=True, manual_pool=True)

    out = await wishlist_service.ensure_board_pool([620, 570, 99901])

    assert out == {"added": 1, "exists": 1, "skipped": 1}
    row = await _get_item(db, 620)
    assert row.owned is True and row.board_pool is False, "既有来源标记不被覆写"
    row = await _get_item(db, 570)
    assert row.active is False and row.excluded is True, "手动移除的条目不得被轮询洗回"
    row = await _get_item(db, 99901)
    assert row is not None and row.active is True and row.board_pool is True


@pytest.mark.asyncio
async def test_ensure_board_pool_requires_account(db, monkeypatch):
    """无绑定账户且有新条目：拒绝且不落行（调用方按「跳过落池」处理）。"""
    _mock_primary(monkeypatch, None)
    with pytest.raises(ValueError):
        await wishlist_service.ensure_board_pool([620])
    assert await _get_item(db, 620) is None


@pytest.mark.asyncio
async def test_board_pool_immune_to_deactivation(db, monkeypatch):
    """榜单落池条目：不在 Steam 愿望单也不被同步停用（对照组普通条目照停）。"""
    await _seed_account(db)
    await _seed_item(db, 620, active=True, board_pool=True)
    await _seed_item(db, 570, active=True)  # 对照：普通条目

    _mock_wishlist(monkeypatch, [{"appid": 99801, "added_at": None}])
    _mock_owned(monkeypatch, [])
    await wishlist_service.sync_account(PRIMARY, auto_crawl=False)

    assert (await _get_item(db, 620)).active is True, "board_pool 条目应免疫停用"
    assert (await _get_item(db, 570)).active is False


@pytest.mark.asyncio
async def test_list_items_reports_board_pool_source(db, monkeypatch):
    """监控条目列表带 boardPool 来源标记（池页类别分类的数据源）。"""
    await _seed_account(db)
    await _seed_item(db, 620, active=True, board_pool=True)
    await _seed_item(db, 570, active=True, manual_pool=True)

    out = await wishlist_service.list_items()

    by_appid = {it["appid"]: it for it in out}
    assert by_appid[620]["boardPool"] is True and by_appid[620]["manualPool"] is False
    assert by_appid[570]["manualPool"] is True and by_appid[570]["boardPool"] is False
