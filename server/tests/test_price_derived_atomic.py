from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.core.database import get_session_factory, write_session
from app.crawler.db_writer import DbWriter
from app.domains.games.derived import update_price_derived
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory


@pytest.mark.asyncio
@pytest.mark.parametrize("commit", [False, True])
async def test_process_exit_preserves_whole_price_transaction(tmp_path, commit):
    import sqlite3
    import subprocess
    import sys
    from pathlib import Path
    from app.core.database import sqlite_file_path

    async with write_session() as session:
        session.add(Game(appid=9900920, name="Crash", diff_fen=500))
        session.add_all([
            GameCurrentPrice(appid=9900920, region_code="CN", price=1000,
                cny_fen=1000, price_status="ok", attempt_outcome="success"),
            GameCurrentPrice(appid=9900920, region_code="US", price=500,
                cny_fen=500, price_status="ok", attempt_outcome="success"),
        ])
        await session.commit()
    copy = tmp_path / sqlite_file_path().name
    with sqlite3.connect(sqlite_file_path()) as source, sqlite3.connect(copy) as target:
        source.backup(target)
    child = '''
import asyncio,os,sys
from pathlib import Path
os.environ['HOLDEXAR_DATA_DIR']=str(Path(sys.argv[1]).parent)
from sqlalchemy import text
from app.core.database import write_session
from app.domains.games.derived import update_price_derived
async def run():
    async with write_session() as session:
        await session.execute(text("UPDATE game_current_prices SET price=2000,cny_fen=2000 WHERE appid=9900920 AND region_code='CN'"))
        await update_price_derived([9900920],session=session)
        if sys.argv[2]=='True': await session.commit()
        os._exit(23)
asyncio.run(run())
'''
    result = subprocess.run([sys.executable, "-c", child, str(copy), str(commit)],
                            cwd=Path(__file__).resolve().parents[1], timeout=15)
    assert result.returncode == 23
    with sqlite3.connect(copy) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        price = connection.execute("SELECT price FROM game_current_prices WHERE appid=9900920 AND region_code='CN'").fetchone()[0]
        diff = connection.execute("SELECT diff_fen FROM games WHERE appid=9900920").fetchone()[0]
        assert (price, diff) == ((2000, 1500) if commit else (1000, 500))


@pytest.mark.asyncio
async def test_calendar_recomputes_only_changed_inputs_and_retries_rollback(monkeypatch):
    from app.domains.games import derived, scoring

    now = datetime(2026, 10, 24, 12)
    monkeypatch.setattr(derived, "get_beijing_time_obj", lambda: now)
    monkeypatch.setattr(scoring, "get_beijing_time_obj", lambda: now)
    async with write_session() as session:
        session.add_all([
            Game(appid=9900910, name="Calendar", review_count=0, release_date="2026-01-01"),
            Game(appid=9900911, name="Stable", review_count=100, smart_score=0.987),
        ])
        await update_price_derived([9900910], session=session)
        await derived.save_input_version(session, derived.input_version())
        await session.commit()
    async with get_session_factory()() as session:
        old = (await session.get(Game, 9900910)).smart_score
    await derived.maintain_derived()
    now += timedelta(days=1)
    actual = derived.update_member_bundles
    async def interrupted(*args):
        raise RuntimeError("cancel before calendar commit")
    monkeypatch.setattr(derived, "update_member_bundles", interrupted)
    with pytest.raises(RuntimeError):
        await derived.maintain_derived()
    async with get_session_factory()() as session:
        assert (await session.get(Game, 9900910)).smart_score == old
    monkeypatch.setattr(derived, "update_member_bundles", actual)
    await derived.maintain_derived()
    async with get_session_factory()() as session:
        assert (await session.get(Game, 9900910)).smart_score < old
        assert (await session.get(Game, 9900911)).smart_score == 0.987
    async def forbidden(*args, **kwargs):
        raise AssertionError("unchanged restart must not calculate")
    monkeypatch.setattr(derived, "update_price_derived", forbidden)
    await derived.maintain_derived()


@pytest.mark.asyncio
async def test_catalog_type_change_removes_old_series_in_same_transaction():
    from app.domains.games.derived import catalog_inputs

    async with write_session() as session:
        session.add_all([Game(appid=9900912, name="Alpha Story 1", type="GAME"),
                         Game(appid=9900913, name="Alpha Story 2", type="GAME")])
        await update_price_derived([9900912, 9900913], session=session, catalog_before={})
        await session.commit()
    async with get_session_factory()() as session:
        assert (await session.get(Game, 9900912)).series_id
    async with write_session() as session:
        before = await catalog_inputs(session, [9900912])
        await session.execute(text("UPDATE games SET type='DLC' WHERE appid=9900912"))
        await update_price_derived([9900912], session=session, catalog_before=before)
        await session.commit()
    async with get_session_factory()() as session:
        assert (await session.get(Game, 9900912)).series_id == ""
        assert (await session.get(Game, 9900913)).series_id == ""


@pytest.mark.asyncio
async def test_catalog_rename_matches_full_series_result():
    from app.domains.games.derived import catalog_inputs
    from app.domains.games.series import refresh_series

    async with write_session() as session:
        session.add_all([Game(appid=aid, name=name, type="GAME") for aid, name in (
            (9900930, "Alpha Story 1"), (9900931, "Alpha Story 2"),
            (9900932, "Beta World 1"), (9900933, "Beta World 2"))])
        await update_price_derived([9900930, 9900931, 9900932, 9900933], session=session, catalog_before={})
        await session.commit()
    async with write_session() as session:
        before = await catalog_inputs(session, [9900930])
        await session.execute(text("UPDATE games SET name='Beta World 3' WHERE appid=9900930"))
        await update_price_derived([9900930], session=session, catalog_before=before)
        incremental = list((await session.execute(text("SELECT appid,series_id FROM games ORDER BY appid"))).all())
        assert await refresh_series(session=session) == 0
        assert list((await session.execute(text("SELECT appid,series_id FROM games ORDER BY appid"))).all()) == incremental
        await session.commit()


@pytest.mark.asyncio
async def test_snapshot_clears_last_foreign_price_and_empty_targets_do_nothing():
    async with write_session() as session:
        session.add(Game(appid=9900901, name="Snapshot", min_cny_fen=500, diff_fen=500))
        session.add(GameCurrentPrice(appid=9900901, region_code="CN", price=1000,
            cny_fen=1000, price_status="ok", attempt_outcome="success"))
        await session.flush()
        await update_price_derived([], session=session)
        await session.commit()
    async with get_session_factory()() as session:
        assert (await session.get(Game, 9900901)).diff_fen == 500
    async with write_session() as session:
        await update_price_derived([9900901], session=session)
        await session.commit()
    async with get_session_factory()() as session:
        game = await session.get(Game, 9900901)
        assert game.min_cny_fen is None
        assert game.diff_fen == 0


@pytest.mark.asyncio
async def test_facts_and_derived_roll_back_together():
    with pytest.raises(RuntimeError):
        async with write_session() as session:
            session.add(Game(appid=9900902, name="Rollback"))
            session.add(GameCurrentPrice(appid=9900902, region_code="CN", price=1000,
                cny_fen=1000, price_status="ok", attempt_outcome="success"))
            await session.flush()
            await update_price_derived([9900902], session=session)
            raise RuntimeError("interrupt before commit")
    async with get_session_factory()() as session:
        assert await session.get(Game, 9900902) is None


@pytest.mark.asyncio
async def test_writer_commits_sort_snapshot_without_background_work(caplog):
    writer = DbWriter()
    await writer.connect()
    ok = await writer.upsert_game_and_prices({"appid": 9900903, "name": "Atomic", "type": "GAME"}, [
        {"appid": 9900903, "region_code": "CN", "currency": "CNY", "price": 1000,
         "original_price": 2000, "discount_percent": 50, "price_status": "ok"},
    ])
    assert ok, [getattr(record, "hl_detail", None) for record in caplog.records if record.levelno >= 40]
    async with get_session_factory()() as session:
        game = await session.get(Game, 9900903)
        assert game.smart_score is not None
        assert game.diff_fen == 0
