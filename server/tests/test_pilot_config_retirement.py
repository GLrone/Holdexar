import asyncio

import pytest
from sqlalchemy import select, text

from app.core import keyring, rekey, secretbox
from app.core.database import get_session_factory, init_db, write_session
from app.domains.pilot import config
from app.domains.settings import service as settings
from app.domains.settings.models import AppSetting


async def _old_config(*, explicit_empty=False, broken_key=False):
    await init_db()
    for key, value in {
        "protocol": "anthropic", "base_url": "https://gateway.example.test",
        "models": ["model-a", "model-b"], "models_disabled": ["model-b"],
        "context_window": 123456,
    }.items():
        await settings.set_value(f"pilot.llm.{key}", value)
    if broken_key:
        await settings.set_value("pilot.llm.api_key", "enc1:invalid")
    else:
        await settings.set_secret_value("pilot.llm.api_key", "fixture-secret")
    await settings.set_value("pilot.llm.model", "model-a")
    await settings.set_value("pilot.llm.enabled", True)
    if explicit_empty:
        await settings.set_value("pilot.llm.providers", [])
    async with write_session() as session:
        await session.execute(text("PRAGMA user_version=24"))
        await session.commit()


async def _keys():
    async with get_session_factory()() as session:
        return set((await session.execute(select(AppSetting.key))).scalars())


LEGACY_KEYS = {f"pilot.llm.{key}" for key in (
    "protocol", "base_url", "models", "models_disabled", "api_key", "context_window",
)}


@pytest.mark.asyncio
async def test_upgrade_moves_provider_and_preserves_cipher_identity(tmp_data_dir):
    await _old_config()
    original_cipher = await settings.get_value("pilot.llm.api_key")
    await init_db()
    await init_db()
    current = await config.load_config()
    assert current["base_url"] == "https://gateway.example.test"
    assert current["protocol"] == "anthropic"
    assert current["models"] == ["model-a", "model-b"]
    assert current["models_disabled"] == ["model-b"]
    assert current["context_window"] == 123456
    assert current["api_key"] == "fixture-secret"
    assert current["model"] == "model-a" and current["enabled"] is True
    assert not LEGACY_KEYS & await _keys()
    sealed = await settings.get_value(f"pilot.llm.provider_key.{current['active']}")
    assert sealed == original_cipher
    assert secretbox.decrypt_secret(sealed, "kv:pilot.llm.api_key") == "fixture-secret"
    with pytest.raises(secretbox.SecretBoxError):
        secretbox.decrypt_secret(sealed, "kv:pilot.llm.provider_key.other")
    await config.delete_provider(current["active"])
    await init_db()
    assert await config.load_providers() == []
    assert (await config.load_config())["api_key"] == ""


@pytest.mark.asyncio
async def test_explicit_empty_provider_list_does_not_resurrect_legacy(tmp_data_dir):
    await _old_config(explicit_empty=True)
    await init_db()
    assert await config.load_providers() == []
    assert not LEGACY_KEYS & await _keys()


@pytest.mark.asyncio
async def test_migration_failure_rolls_back_legacy_configuration(tmp_data_dir):
    await _old_config(broken_key=True)
    async with write_session() as session:
        await session.execute(text(
            "CREATE TRIGGER block_provider BEFORE INSERT ON app_settings "
            "WHEN NEW.key='pilot.llm.providers' BEGIN SELECT RAISE(ABORT,'fixture blocked'); END"
        ))
        await session.commit()
    with pytest.raises(Exception, match="fixture blocked"):
        await init_db()
    assert await settings.get_value("pilot.llm.base_url") == "https://gateway.example.test"
    assert await settings.get_value("pilot.llm.api_key") == "enc1:invalid"
    assert await settings.get_value("pilot.llm.providers") is None
    async with get_session_factory()() as session:
        assert (await session.execute(text("PRAGMA user_version"))).scalar_one() == 24


@pytest.mark.asyncio
async def test_flat_config_api_only_writes_canonical_provider_keys(tmp_data_dir):
    await init_db()
    await config.save_config({
        "base_url": "https://new.example.test", "protocol": "openai-responses",
        "models": ["current-model"], "models_disabled": [], "model": "current-model",
        "context_window": 654321, "api_key": "new-secret", "enabled": True,
    })
    await init_db()
    current = await config.load_config()
    assert current["base_url"] == "https://new.example.test"
    assert current["api_key"] == "new-secret"
    assert current["context_window"] == 654321
    assert current["protocol"] == "openai-responses"
    assert current["model"] == "current-model"
    assert len(current["providers"]) == 1
    assert not LEGACY_KEYS & await _keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [None, "invalid"])
async def test_non_list_provider_state_preserves_legacy_config(tmp_data_dir, stored):
    await _old_config()
    await settings.set_value("pilot.llm.providers", stored)
    await init_db()
    assert (await config.load_config())["api_key"] == "fixture-secret"


@pytest.mark.asyncio
async def test_locked_passphrase_upgrade_can_start_and_unlock(tmp_data_dir):
    keyring.reset_state()
    secretbox.clear_key_cache()
    try:
        await _old_config()
        await rekey.switch_mode(keyring.MODE_PASSPHRASE, passphrase="fixture-passphrase")
        keyring.lock()
        secretbox.clear_key_cache()
        await init_db()
        assert keyring.is_locked(tmp_data_dir)
        assert keyring.unlock(tmp_data_dir, "fixture-passphrase")
        secretbox.clear_key_cache()
        assert (await config.load_config())["api_key"] == "fixture-secret"
        await rekey.switch_mode(keyring.MODE_LEGACY, current_passphrase="fixture-passphrase")
        assert (await config.load_config())["api_key"] == "fixture-secret"
    finally:
        keyring.reset_state()
        secretbox.clear_key_cache()


@pytest.mark.asyncio
async def test_existing_main_provider_keeps_its_published_cipher_purpose(tmp_data_dir):
    await _old_config()
    await settings.set_values({
        "pilot.llm.providers": [{"id": "main", "name": "existing", "base_url": "https://main.example.test"}],
        "pilot.llm.active": "main",
        "pilot.llm.provider_key.main": secretbox.encrypt_secret("main-secret", "kv:pilot.llm.provider_key.main"),
    })
    await init_db()
    current = await config.load_config()
    assert current["active"] == "main"
    assert current["api_key"] == "main-secret"


@pytest.mark.asyncio
async def test_concurrent_first_saves_keep_one_provider_and_both_patches(tmp_data_dir):
    await init_db()
    await asyncio.gather(
        config.save_config({"base_url": "https://one.example.test", "api_key": "one-secret"}),
        config.save_config({"models": ["one-model"]}),
    )
    current = await config.load_config()
    assert len(current["providers"]) == 1
    assert current["base_url"] == "https://one.example.test"
    assert current["api_key"] == "one-secret"
    assert current["models"] == ["one-model"]


@pytest.mark.asyncio
async def test_flat_save_key_failure_does_not_commit_provider_or_globals(tmp_data_dir):
    await init_db()
    async with write_session() as session:
        await session.execute(text(
            "CREATE TRIGGER block_secret BEFORE INSERT ON app_settings "
            "WHEN NEW.key LIKE 'pilot.llm.provider_key.%' BEGIN SELECT RAISE(ABORT,'fixture key blocked'); END"
        ))
        await session.commit()
    with pytest.raises(Exception, match="fixture key blocked"):
        await config.save_config({"base_url": "https://one.example.test", "api_key": "secret", "enabled": True})
    assert await config.load_providers() == []
    assert await settings.get_value("pilot.llm.enabled") is None
