"""games 列表送礼分析筛选语义（giftMode out/in，判据 GIFT_TRACK_BAND）。

out=固定送礼方找可送目标（多目标任一命中）；in=固定收礼方，送礼方全区
自动遍历。合成 99xxxx 探针行（q= 隔离 + finally 清理），边界用例含
恰好 ×1.15 与「目标含送礼方自身不得自比恒真」防回归。
"""
import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.database import get_session_factory, init_db
from app.domains.games import service as games_service
from app.domains.games.models import Game, GameCurrentPrice

PROBE = [99700111, 99700112, 99700113, 99700114]
A, B, C, D = PROBE

# appid → [(region, cny_fen), ...]；C 的 JP 行恰好 = CN×1.15（边界含等号）
SEED_REGIONS: dict[int, list[tuple[str, int]]] = {
    A: [("CN", 10000), ("JP", 9000), ("KR", 11000), ("US", 20000)],
    B: [("CN", 10000), ("JP", 12000), ("KR", 10400)],
    C: [("CN", 10000), ("JP", 11500)],
    D: [("CN", 10000), ("JP", 12000), ("KR", 10400), ("US", 13000)],
}
NAMES = {A: "gift-probe-a", B: "gift-probe-b", C: "gift-probe-c", D: "gift-probe-d"}


@pytest_asyncio.fixture(autouse=True)
async def _setup_and_cleanup():
    await init_db()
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_(PROBE)))
        for appid, rows in SEED_REGIONS.items():
            s.add(Game(appid=appid, name=NAMES[appid]))
            for region, fen in rows:
                s.add(GameCurrentPrice(
                    appid=appid, region_code=region, price_status="ok",
                    currency="CNY", price=fen, original_price=fen,
                    discount_percent=0, cny_fen=fen,
                ))
        await s.commit()
    yield
    async with get_session_factory()() as s:
        await s.execute(delete(Game).where(Game.appid.in_(PROBE)))
        await s.execute(delete(GameCurrentPrice).where(GameCurrentPrice.appid.in_(PROBE)))
        await s.commit()


async def _probe_ids(**kw) -> set[int]:
    res = await games_service.list_games(limit=100, **kw)
    return {it["appid"] for it in res["items"]} & set(PROBE)


@pytest.mark.asyncio
async def test_gift_baseline_no_filter():
    """不开送礼筛选：四探针全在（确认过滤来自 gift 参数而非种子问题）。"""
    assert await _probe_ids(q="gift-probe-") == set(PROBE)


@pytest.mark.asyncio
async def test_gift_mode_out_sender_fixed():
    """out：送礼方 CN。JP 目标只有 A（90≤115）与 C（边界 115≤115）命中。"""
    ids = await _probe_ids(q="gift-probe-", gift_mode="out", gift_sender="cn",
                           gift_receivers="jp")
    assert ids == {A, C}


@pytest.mark.asyncio
async def test_gift_mode_out_multi_target_any_hit():
    """out 多目标任一命中即可：B/D 的 JP 超界但 KR 在带内 → 进入结果。"""
    ids = await _probe_ids(q="gift-probe-", gift_mode="out", gift_sender="cn",
                           gift_receivers="jp,kr")
    assert ids == set(PROBE)


@pytest.mark.asyncio
async def test_gift_mode_out_self_target_guard():
    """out 目标含送礼方自身不得自比恒真： JP 作送礼方时四探针（仅 JP 行）
    全部落空——自区行被排除，且排除靠 SQL 而非前端选项约束。"""
    ids = await _probe_ids(q="gift-probe-", gift_mode="out", gift_sender="jp",
                           gift_receivers="jp")
    assert ids == set()


@pytest.mark.asyncio
async def test_gift_mode_in_auto_traverse():
    """in：收礼方 JP，送礼方全区自动遍历。B 的全部候选送礼方都在带外
    （CN 120>115、KR 120>119.6）→ 排除；D 靠 US（120≤149.5）入选。"""
    ids = await _probe_ids(q="gift-probe-", gift_mode="in", gift_receivers="jp")
    assert ids == {A, C, D}
