from datetime import datetime, timedelta

import pytest

from app.core.database import get_session_factory, write_session
from app.domains.games.derived import update_price_derived
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown", [False, True])
async def test_derived_history_uses_shared_product_identity(unknown):
    appid = 9900961
    now = datetime.now()
    async with write_session() as session:
        session.add(Game(appid=appid, name="History Identity"))
        session.add(GameCurrentPrice(appid=appid, region_code="CN", currency="CNY",
            price=10000, original_price=20000, cny_fen=10000, discount_percent=50,
            price_status="ok", attempt_outcome="success", updated_at=now))
        for days, price, original, product_unknown in (
            (3, 5000, 40000, unknown), (2, 30000, 20000, False), (1, 10000, 20000, False),
        ):
            session.add(GamePriceHistory(appid=appid, region_code="CN", currency="CNY",
                price=price, cny_fen=None, original_price=original, price_status="ok",
                source="community", product_unknown=product_unknown,
                snapshot_at=now-timedelta(days=days)))
        await update_price_derived([appid], session=session)
        await session.commit()
    async with get_session_factory()() as session:
        game = await session.get(Game, appid)
        assert (game.hl_flag, game.pp_flag) == ((1, 0) if unknown else (3, 1))
        assert (game.pp_changed_at is None) == unknown
