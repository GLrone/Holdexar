"""从 Steam 客户端本地账号列表读取可导入的 Steam 身份。"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

_TOKEN = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|[{}]|[^\s{}"]+')
_STEAM_ID64_BASE = 76561197960265728
_STEAM_ID64_MAX = _STEAM_ID64_BASE + (1 << 32) - 1
_MAX_LOGINUSERS_BYTES = 2 * 1024 * 1024


def _decode_token(token: str) -> str:
    if not (len(token) >= 2 and token[0] == '"' and token[-1] == '"'):
        return token
    value = token[1:-1]
    return re.sub(
        r"\\(.)",
        lambda match: {"n": "\n", "r": "\r", "t": "\t"}.get(
            match.group(1), match.group(1)
        ),
        value,
    )


def _is_steam_id64(value: str) -> bool:
    if not value.isdecimal():
        return False
    number = int(value)
    return _STEAM_ID64_BASE <= number <= _STEAM_ID64_MAX


def _skip_block(tokens: list[str], position: int) -> int:
    depth = 1
    while position < len(tokens) and depth:
        if tokens[position] == "{":
            depth += 1
        elif tokens[position] == "}":
            depth -= 1
        position += 1
    if depth:
        raise ValueError("incomplete VDF block")
    return position


def _read_account(tokens: list[str], position: int) -> tuple[str, int]:
    persona_name = ""
    while position < len(tokens):
        key = _decode_token(tokens[position])
        if key == "}":
            return persona_name, position + 1
        if position + 1 >= len(tokens):
            break
        value = tokens[position + 1]
        if value == "{":
            position = _skip_block(tokens, position + 2)
        else:
            if key == "PersonaName":
                persona_name = _decode_token(value)
            position += 2
    raise ValueError("incomplete Steam account entry")


def parse_loginusers(contents: str) -> list[dict[str, str]]:
    tokens = [token for token in _TOKEN.findall(contents) if not token.startswith("//")]
    for position in range(len(tokens) - 1):
        if _decode_token(tokens[position]) != "users" or tokens[position + 1] != "{":
            continue
        accounts: list[dict[str, str]] = []
        cursor = position + 2
        try:
            while cursor < len(tokens):
                key = _decode_token(tokens[cursor])
                if key == "}":
                    return accounts
                if cursor + 1 >= len(tokens):
                    return []
                if tokens[cursor + 1] != "{":
                    cursor += 2
                    continue
                cursor += 2
                if _is_steam_id64(key):
                    persona_name, cursor = _read_account(tokens, cursor)
                    accounts.append({"steam_id": key, "persona_name": persona_name})
                else:
                    cursor = _skip_block(tokens, cursor)
        except ValueError:
            return []
        return []
    return []


def _registered_steam_roots() -> list[Path]:
    if sys.platform != "win32":
        return []
    try:
        import winreg
    except ImportError:
        return []

    roots: list[Path] = []
    locations = (
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\steam.exe"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\steam.exe"),
    )
    views = tuple(
        dict.fromkeys(
            flag
            for flag in (
                getattr(winreg, "KEY_WOW64_64KEY", 0),
                getattr(winreg, "KEY_WOW64_32KEY", 0),
                0,
            )
        )
    )
    for hive, key_name in locations:
        for view in views:
            try:
                with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                    value_names = (None,) if "App Paths" in key_name else (
                        "SteamPath",
                        "InstallPath",
                        "SteamExe",
                    )
                    for value_name in value_names:
                        try:
                            value, _ = winreg.QueryValueEx(key, value_name)
                        except OSError:
                            continue
                        value = os.path.expandvars(str(value).strip().strip('"'))
                        candidate = Path(value).expanduser()
                        if candidate.suffix.lower() == ".exe":
                            candidate = candidate.parent
                        roots.append(candidate)
            except OSError:
                continue

    unique: dict[str, Path] = {}
    for root in roots:
        unique.setdefault(os.path.normcase(str(root)), root)
    return list(unique.values())


def discover_local_steam_accounts() -> list[dict[str, str]]:
    accounts: dict[str, dict[str, str]] = {}
    for root in _registered_steam_roots():
        if not (root / "steam.exe").is_file():
            continue
        loginusers = root / "config" / "loginusers.vdf"
        try:
            if loginusers.stat().st_size > _MAX_LOGINUSERS_BYTES:
                continue
            contents = loginusers.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        for account in parse_loginusers(contents):
            accounts.setdefault(account["steam_id"], account)
    return list(accounts.values())
