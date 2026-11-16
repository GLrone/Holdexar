import pytest


def test_explicit_rule_beats_manual_and_observation_currency_beats_mapping():
    from app.domains.notifications.region import resolve_region

    result = resolve_region(explicit="us", prefs={"manualRegion": "CN"},
                            observed={"region": "US", "currency": "EUR"})
    assert result == {"region": "US", "source": "rule", "currency": "EUR",
                      "currencySource": "observation"}


def test_manual_region_uses_existing_mapping_without_guessing_account_country():
    from app.domains.notifications.region import resolve_region

    result = resolve_region(prefs={"manualRegion": "jp"},
                            observed={"region": "CN", "currency": "CNY"})
    assert result == {"region": "JP", "source": "manual", "currency": "JPY",
                      "currencySource": "region_mapping"}


def test_no_manual_or_explicit_country_remains_unknown():
    from app.domains.notifications.region import resolve_region

    assert resolve_region(observed={"region": "US", "currency": "USD"}) == {
        "region": None, "source": "unknown", "currency": None,
        "currencySource": "unknown",
    }


def test_unknown_explicit_region_never_falls_back_to_usd_or_cny():
    from app.domains.notifications.region import resolve_region

    with pytest.raises(ValueError, match="region"):
        resolve_region(explicit="ZZ")


def test_wallet_fallback_region_used_when_no_explicit_or_manual():
    from app.domains.notifications.region import resolve_region

    result = resolve_region(prefs={}, fallback="jp",
                            observed={"region": "JP", "currency": "JPY"})
    assert result == {"region": "JP", "source": "account", "currency": "JPY",
                      "currencySource": "observation"}


def test_explicit_and_manual_beat_wallet_fallback():
    from app.domains.notifications.region import resolve_region

    assert resolve_region(explicit="us", fallback="jp")["region"] == "US"
    assert resolve_region(prefs={"manualRegion": "CN"}, fallback="jp")["region"] == "CN"
    assert resolve_region(prefs={"manualRegion": "CN"}, fallback="jp")["source"] == "manual"


def test_unmapped_wallet_fallback_is_unknown_not_error():
    from app.domains.notifications.region import resolve_region

    assert resolve_region(prefs={}, fallback="zz") == {
        "region": None, "source": "unknown", "currency": None,
        "currencySource": "unknown",
    }


@pytest.mark.asyncio
async def test_effective_region_shares_full_chain(monkeypatch):
    from app.domains.notifications import region as region_module
    from app.domains.account import service as account_service

    async def wallet():
        return "jp"

    monkeypatch.setattr(account_service, "primary_wallet_region", wallet)
    assert await region_module.effective_region(prefs={}) == {
        "region": "JP", "source": "account", "currency": "JPY",
        "currencySource": "region_mapping",
    }

    async def broken():
        raise RuntimeError("account domain down")

    monkeypatch.setattr(account_service, "primary_wallet_region", broken)
    assert (await region_module.effective_region(prefs={}))["source"] == "unknown"
