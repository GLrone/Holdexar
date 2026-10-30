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
