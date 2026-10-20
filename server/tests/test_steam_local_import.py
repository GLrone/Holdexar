"""本机 Steam 账号发现与导入测试。"""
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi import HTTPException

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.domains.settings import router as settings_router
from app.domains.settings import steam_local


VDF_SAMPLE = r'''
"users"
{
    "76561198000000001"
    {
        "AccountName" "private-login-name"
        "PersonaName" "Display \"Name\""
        "MostRecent" "1"
    }
    "76561198000000002"
    {
        "AccountName" "another-private-login"
        "PersonaName" "Second User"
    }
    "not-a-steam-id"
    {
        "PersonaName" "Ignore Me"
    }
}
'''


def test_parse_loginusers_returns_only_safe_identity_fields():
    accounts = steam_local.parse_loginusers(VDF_SAMPLE)

    assert accounts == [
        {"steam_id": "76561198000000001", "persona_name": 'Display "Name"'},
        {"steam_id": "76561198000000002", "persona_name": "Second User"},
    ]
    assert "private-login-name" not in repr(accounts)
    assert "another-private-login" not in repr(accounts)


def test_registered_install_path_can_be_on_a_non_c_drive(monkeypatch):
    class RegistryKey:
        def __init__(self, key_name):
            self.key_name = key_name

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def open_key(hive, key_name, _reserved, _access):
        return RegistryKey(key_name)

    def query_value(key, value_name):
        if "App Paths" in key.key_name:
            return r"Z:\Steam\Portable\steam.exe", 1
        if value_name == "SteamExe":
            return r"Z:\Steam\Portable\steam.exe", 1
        return r"Z:\Steam\Portable", 1

    fake_winreg = SimpleNamespace(
        HKEY_CURRENT_USER=1,
        HKEY_LOCAL_MACHINE=2,
        KEY_READ=0x20019,
        KEY_WOW64_64KEY=0x100,
        KEY_WOW64_32KEY=0x200,
        OpenKey=open_key,
        QueryValueEx=query_value,
    )
    monkeypatch.setattr(steam_local.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "winreg", fake_winreg)

    assert Path(r"Z:\Steam\Portable") in steam_local._registered_steam_roots()


def test_discovery_reads_registered_install_root_without_default_path_assumption(tmp_path, monkeypatch):
    steam_root = tmp_path / "library" / "Steam"
    config_dir = steam_root / "config"
    config_dir.mkdir(parents=True)
    (steam_root / "steam.exe").touch()
    (config_dir / "loginusers.vdf").write_text(VDF_SAMPLE, encoding="utf-8")
    monkeypatch.setattr(steam_local, "_registered_steam_roots", lambda: [steam_root])

    accounts = steam_local.discover_local_steam_accounts()

    assert [account["steam_id"] for account in accounts] == [
        "76561198000000001",
        "76561198000000002",
    ]


@pytest.mark.asyncio
async def test_import_saves_selected_local_identity_to_existing_settings(monkeypatch):
    accounts = [
        {"steam_id": "76561198000000001", "persona_name": "Display Name"},
    ]
    saved = []
    monkeypatch.setattr(steam_local, "discover_local_steam_accounts", lambda: accounts)

    async def save_value(key, value):
        saved.append((key, value))

    monkeypatch.setattr(settings_router.service, "set_value", save_value)

    result = await settings_router.import_local_steam_account(
        settings_router.LocalSteamAccountImport(steam_id="76561198000000001")
    )

    assert result == {"steam_id": "76561198000000001", "persona_name": "Display Name"}
    assert saved == [("account.steam_id", "76561198000000001")]


@pytest.mark.asyncio
async def test_import_rejects_id_not_discovered_locally(monkeypatch):
    saved = []
    monkeypatch.setattr(steam_local, "discover_local_steam_accounts", lambda: [])

    async def save_value(key, value):
        saved.append((key, value))

    monkeypatch.setattr(settings_router.service, "set_value", save_value)

    with pytest.raises(HTTPException) as exc:
        await settings_router.import_local_steam_account(
            settings_router.LocalSteamAccountImport(steam_id="76561198000000001")
        )

    assert exc.value.status_code == 404
    assert saved == []
