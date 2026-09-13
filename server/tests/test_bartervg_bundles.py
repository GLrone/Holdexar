"""Barter.vg bundle 计数链验收（refresh_bundle_counts，只更新库内已有行）。

纯逻辑：档案解析（数字键 / 非正计数剔除 / 完整性哨兵）。链路：合成档案
monkeypatch 拉取层，走真实落库（合成 990xxx appid 播种 + 清理，不污染
真实库），验证：
- 库内已有行按差量写入（旧值覆写、同值零写），不在档案中的行保持 NULL；
- 只更新已存在的行，档案里多出的 appid 不落占位；
- 48h 新鲜度闸内跳过（记账于 app_settings），force=True 绕过；
- 档案疑似截断（哨兵门槛）→ 本轮丢弃，库内旧值保留。
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import delete, select

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import get_session_factory, init_db  # noqa: E402
from app.domains.games.models import Game  # noqa: E402
from app.domains.metadata import service as meta  # noqa: E402
from app.domains.settings.models import AppSetting  # noqa: E402

TEST_STATE_KEY = "bartervg_bundles_last_fetch_test"

APP_STALE = 990_501  # 库内旧计数 2 → 档案 5，应覆写
APP_SAME = 990_502  # 库内已是 3 → 零写入
APP_ABSENT = 990_503  # 档案无此 appid → 保持 NULL
APP_NOT_IN_DB = 990_504  # 档案有、库内无 → 不落占位行


def _archive() -> dict:
    return {
        str(APP_STALE): {"bundles": 5, "bundles_packages": 1},
        str(APP_SAME): {"bundles": 3},
        str(APP_NOT_IN_DB): {"bundles": 7},
        "not_a_number": {"bundles": 9},  # 非数字键剔除
        "666666": {"bundles": 0},  # 非正计数剔除
        "777777": {"bundles": "x"},  # 非整数剔除
    }


@pytest_asyncio.fixture(autouse=True)
async def _seed():
    await init_db()
    async with get_session_factory()() as session:
        session.add(Game(appid=APP_STALE, name="Stale Test", bundle_count=2))
        session.add(Game(appid=APP_SAME, name="Same Test", bundle_count=3))
        session.add(Game(appid=APP_ABSENT, name="Absent Test"))
        await session.commit()
    yield
    async with get_session_factory()() as session:
        await session.execute(
            delete(Game).where(Game.appid.in_([APP_STALE, APP_SAME, APP_ABSENT, APP_NOT_IN_DB]))
        )
        await session.execute(
            delete(AppSetting).where(AppSetting.key == TEST_STATE_KEY)
        )
        await session.commit()


# ─── 纯逻辑 ────────────────────────────────────────────────────────


def test_parse_bundles_map_filters_and_counts():
    assert meta._bundles_obj_to_map(_archive()) == {APP_STALE: 5, APP_SAME: 3, APP_NOT_IN_DB: 7}


def test_parse_bundles_map_sanity_gate():
    # 低于完整性哨兵门槛 → 视为截断响应，抛错由调用方丢弃本轮
    with pytest.raises(ValueError, match="截断"):
        meta._check_archive_integrity({"18500": {"bundles": 5}})

    with pytest.raises(ValueError, match="顶层"):
        meta._check_archive_integrity(["not", "an", "object"])


# ─── 链路（真实落库 + 合成档案）──────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_diff_writes_and_skips_placeholder(monkeypatch):
    monkeypatch.setattr(meta, "_BUNDLES_STATE_KEY", TEST_STATE_KEY)

    calls = []

    async def fake_fetch(proxy):
        calls.append(proxy)
        return meta._bundles_obj_to_map(_archive())

    monkeypatch.setattr(meta, "_fetch_bartervg_bundles", fake_fetch)

    result = await meta.refresh_bundle_counts(force=True)
    assert result["ok"] is True and result["skipped"] is False
    assert result["records"] == 3
    assert result["updated"] == 1  # 仅 APP_STALE 覆写；APP_SAME 同值零写

    async with get_session_factory()() as session:
        stale = await session.get(Game, APP_STALE)
        assert stale.bundle_count == 5
        same = await session.get(Game, APP_SAME)
        assert same.bundle_count == 3
        absent = await session.get(Game, APP_ABSENT)
        assert absent.bundle_count is None  # 不在档案中 → 保持 NULL
        # 档案里多出的 appid 不落占位行
        ghost = await session.scalar(
            select(Game.appid).where(Game.appid == APP_NOT_IN_DB)
        )
        assert ghost is None
        state = await session.get(AppSetting, TEST_STATE_KEY)
        assert state is not None and state.value_json["updated"] == 1

    # 48h 闸内跳过（不触达拉取层）；force=True 绕过且差量为零（幂等）
    skipped = await meta.refresh_bundle_counts()
    assert skipped["skipped"] is True
    again = await meta.refresh_bundle_counts(force=True)
    assert again["updated"] == 0
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_refresh_failure_keeps_old_values(monkeypatch):
    monkeypatch.setattr(meta, "_BUNDLES_STATE_KEY", TEST_STATE_KEY)

    async def broken_fetch(proxy):
        raise RuntimeError("HTTP 503")

    monkeypatch.setattr(meta, "_fetch_bartervg_bundles", broken_fetch)

    result = await meta.refresh_bundle_counts(force=True)
    assert result["ok"] is False

    async with get_session_factory()() as session:
        stale = await session.get(Game, APP_STALE)
        assert stale.bundle_count == 2  # 库内旧值保留
        state = await session.get(AppSetting, TEST_STATE_KEY)
        assert state is None  # 失败不记账
