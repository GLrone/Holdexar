import pytest

from app.core.database import get_session_factory, write_session
from app.domains.games.models import Game, GameCurrentPrice
from app.domains.rates.models import FxRate
from app.domains.system import router as system


@pytest.mark.asyncio
async def test_import_commits_currency_conversion_and_derived_snapshot(tmp_path, monkeypatch):
    source = tmp_path / "legacy.db"
    source.touch()
    rows = [{"appid": 9900951, "region_code": region, "currency": currency,
        "price": price, "original_price": price, "discount_percent": 0, "sub_id": 1,
        "price_status": "ok", "is_gold": False, "version_suffix": ""}
        for region, currency, price in (("cn", "CNY", 10000), ("us", "USD", 500))]
    monkeypatch.setattr(system, "_fetch_legacy_snapshot", lambda _: (
        [{"appid": 9900951, "name": "Legacy Import"}], rows, 2))
    async with write_session() as session:
        session.add(FxRate(currency_code="USD", rate_to_cny=7))
        await session.commit()
    result = await system.import_legacy(system.LegacyImport(path=str(source)))
    assert result["currentUpserted"] == 2
    async with get_session_factory()() as session:
        game = await session.get(Game, 9900951)
        us = await session.get(GameCurrentPrice, (9900951, "US"))
        assert us.cny_fen == game.min_cny_fen == 3500
        assert game.diff_fen == 6500 and game.smart_score is not None
