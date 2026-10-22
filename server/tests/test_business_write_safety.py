import asyncio
from datetime import datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_session_factory, write_session
from app.domains.account import service as accounts
from app.domains.account.models import SteamAccount
from app.domains.family import service as family
from app.domains.family.models import FamilyGroup


OWNER = "76561198000000001"
MEMBER = "76561198000000002"


@pytest.mark.asyncio
async def test_family_absorption_rolls_back_with_destination_failure(monkeypatch):
    async with write_session() as session:
        session.add(FamilyGroup(steamid=family.LOCAL_GROUP_SID,
                               members_json=[{"steamid": MEMBER, "origin": "manual"}]))
        await session.commit()
    commit = AsyncSession.commit

    async def reject_destination(session):
        if any(isinstance(row, FamilyGroup) and row.steamid == OWNER for row in session.new):
            raise RuntimeError("destination commit rejected")
        await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", reject_destination)
    with pytest.raises(RuntimeError, match="destination commit"):
        await family.add_family_member(MEMBER, group_steamid=OWNER)
    async with get_session_factory()() as session:
        local = await session.get(FamilyGroup, family.LOCAL_GROUP_SID)
        assert [row["steamid"] for row in local.members_json] == [MEMBER]
        assert await session.get(FamilyGroup, OWNER) is None


@pytest.mark.asyncio
async def test_family_duplicate_commits_local_absorption_once():
    async with write_session() as session:
        for sid in [family.LOCAL_GROUP_SID, OWNER]:
            session.add(FamilyGroup(steamid=sid, members_json=[{"steamid": MEMBER}]))
        await session.commit()
    for _ in range(2):
        assert await family.add_family_member(MEMBER, group_steamid=OWNER) == {"ok": True, "duplicate": True}
    async with get_session_factory()() as session:
        assert (await session.get(FamilyGroup, family.LOCAL_GROUP_SID)).members_json == []
        assert len((await session.get(FamilyGroup, OWNER)).members_json) == 1


@pytest.mark.asyncio
async def test_profile_fill_uses_latest_name_after_waiting_for_writer(monkeypatch):
    async with write_session() as session:
        session.add(SteamAccount(steam_id=OWNER, persona_name="", cookies="", is_active=True))
        await session.commit()
    fetched = asyncio.Event()
    read = asyncio.Event()
    get = AsyncSession.get
    profile_task = None

    async def profile(*args, **kwargs):
        fetched.set()
        return {"persona_name": "old remote name", "avatar_url": "new-avatar", "online": True,
                "in_game_name": "Game"}

    async def proxy():
        return None

    async def observed_get(session, entity, *args, **kwargs):
        row = await get(session, entity, *args, **kwargs)
        if entity is SteamAccount and asyncio.current_task() is profile_task:
            read.set()
        return row

    monkeypatch.setattr(accounts, "fetch_profile", profile)
    monkeypatch.setattr(accounts, "_strategy_proxy", proxy)
    monkeypatch.setattr(AsyncSession, "get", observed_get)
    begin = asyncio.Event()

    async def sync_profile():
        await begin.wait()
        await accounts._sync_profile_row(OWNER, "", datetime.now())

    profile_task = asyncio.create_task(sync_profile())
    async with write_session() as session:
        begin.set()
        await fetched.wait()
        try:
            await asyncio.wait_for(read.wait(), 0.1)
        except TimeoutError:
            pass
        row = await session.get(SteamAccount, OWNER)
        row.persona_name = "new user name"
        await session.commit()
    await profile_task
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, OWNER)
        assert row.persona_name == "new user name"
        assert (row.avatar_url, row.is_online, row.in_game) == ("new-avatar", True, "Game")


@pytest.mark.asyncio
async def test_slow_batch_commit_failure_only_rejects_rolled_back_segment(monkeypatch):
    from app.crawler import db_writer
    from app.domains.games.models import Game
    from app.domains.steam_events import service as events

    async def no_event(_):
        return None

    monkeypatch.setattr(events, "active_event_key_at", no_event)
    monkeypatch.setattr(db_writer, "_BATCH_WRITE_CHUNK", 1)
    commit = AsyncSession.commit
    commits = 0

    async def reject_second(session):
        nonlocal commits
        commits += 1
        if commits == 2:
            raise RuntimeError("segment commit rejected")
        await commit(session)

    monkeypatch.setattr(AsyncSession, "commit", reject_second)
    entries = [({"appid": aid, "name": str(aid), "updated_at": datetime.now()}, [])
               for aid in [990001, 990002, 990003]]
    assert await db_writer.DbWriter()._upsert_task_batch_slow(entries) == [True, False, True]
    async with get_session_factory()() as session:
        assert list((await session.execute(select(Game.appid).where(Game.appid >= 990001))).scalars()) == [990001, 990003]


@pytest.mark.asyncio
async def test_profile_commit_failure_preserves_previous_fields(monkeypatch):
    async with write_session() as session:
        session.add(SteamAccount(steam_id=OWNER, persona_name="", avatar_url="before", cookies="",
                                 is_active=True, is_online=False, in_game=""))
        await session.commit()

    async def profile(*args, **kwargs):
        return {"persona_name": "remote", "avatar_url": "after", "online": True, "in_game_name": "Game"}

    async def proxy():
        return None

    async def reject(_):
        raise RuntimeError("profile commit rejected")

    monkeypatch.setattr(accounts, "fetch_profile", profile)
    monkeypatch.setattr(accounts, "_strategy_proxy", proxy)
    monkeypatch.setattr(AsyncSession, "commit", reject)
    with pytest.raises(RuntimeError, match="profile commit"):
        await accounts._sync_profile_row(OWNER, "", datetime.now())
    async with get_session_factory()() as session:
        row = await session.get(SteamAccount, OWNER)
        assert (row.persona_name, row.avatar_url, row.is_online, row.in_game) == ("", "before", False, "")
