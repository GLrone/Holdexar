"""凭据静态加密：AES-256-GCM 密文落库 + HKDF-SHA256 派生用途密钥。

本地库里的登录凭据（Steam 登录 Cookie / Web API Key / 代理订阅链接）是长期
有效的敏感材料：落库一律经本模块封装为 `enc1:` 前缀密文，消费时才解密，日志
与接口响应不出现明文。

密钥分层：

- 数据主密钥（DMK，32 字节）：来源与防护模式由 `app.core.keyring` 决定
  （legacy 机器派生 / DPAPI 系统凭据 / 口令保护）；
- 用途子密钥：`HKDF(master, salt=固定，info="holdexar:secretbox:<用途>")`，
  各用途彼此计算独立，跨用途解密直接失败。

legacy 模式下 DMK 由两份本机材料派生：机器指纹（Windows MachineGuid → Linux
machine-id → macOS IOPlatformUUID，全无则数据目录内随机密钥文件）+ 安装盐
（数据目录 `secret.salt`）。库文件被单独拷走或整机迁移后密文不可解——凭据属主
语义，相关账号走「重新登录」路径重建，行内密文保留待覆盖。

切换 keyring 模式时（见 `app.core.rekey`）库内既有密文从旧 DMK 换钥到新 DMK；
换钥是单事务，读取侧另以 legacy 兜底覆盖崩溃窗口：新 DMK 解不开时回落 legacy
（legacy DMK 恒可由本机材料重建），迁移完成后库内不再有 legacy 密文，兜底失效。
"""
from __future__ import annotations

import base64
import secrets
import subprocess
import sys
from functools import lru_cache
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core import keyring

# 密文自描述前缀：读取侧据此区分密文与存量明文（明文由启动加密步骤封装）
PREFIX = "enc1:"

_NONCE_BYTES = 12
_KEY_BYTES = 32
_SALT_FILE = "secret.salt"
_SALT_BYTES = 16
_FALLBACK_KEY_FILE = "secret.machine.key"
_MASTER_INFO = b"holdexar:secretbox:master:v1"
_PURPOSE_SALT = b"holdexar:secretbox:purpose"


class SecretBoxError(RuntimeError):
    """凭据解密失败（密文被篡改、派生密钥不符、格式不识别或凭据处于锁定态）。"""


def is_encrypted(value: str) -> bool:
    """值是否为 `enc1:` 密文（存量明文为 False）。"""
    return bool(value) and value.startswith(PREFIX)


def _data_dir() -> Path:
    from app.core.config import get_settings

    return Path(get_settings().data_dir)


def _machine_fingerprint() -> bytes:
    """机器指纹（同机稳定）：Windows MachineGuid → Linux machine-id
    → macOS IOPlatformUUID；全部不可用返回空串（调用方回退密钥文件）。"""
    if sys.platform == "win32":
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography"
            ) as key:
                value, _ = winreg.QueryValueEx(key, "MachineGuid")
                if value:
                    return str(value).encode("utf-8")
        except OSError:
            pass
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            text = Path(path).read_text(encoding="ascii").strip()
        except OSError:
            continue
        if text:
            return text.encode("ascii")
    if sys.platform == "darwin":
        try:
            out = subprocess.run(
                ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
                capture_output=True,
                text=True,
                timeout=5,
                check=True,
            ).stdout
            for line in out.splitlines():
                if "IOPlatformUUID" in line:
                    return line.split("= ")[-1].strip('"').encode("utf-8")
        except (OSError, subprocess.SubprocessError):
            pass
    return b""


def _write_private(data_dir: Path, path: Path, data: bytes) -> None:
    """密钥材料落盘（POSIX 下收紧为 0600；目录不可写属致命配置，抛错上浮）。"""
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if sys.platform != "win32":
            path.chmod(0o600)
    except OSError as exc:
        raise SecretBoxError(f"密钥材料写入失败（数据目录不可写？）：{exc}") from exc


def _install_salt(data_dir: Path) -> bytes:
    """安装盐（每数据目录一份随机数，防同指纹克隆机共钥）。"""
    path = data_dir / _SALT_FILE
    try:
        data = path.read_bytes()
        if len(data) >= _SALT_BYTES:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(_SALT_BYTES)
    _write_private(data_dir, path, data)
    return data


def _fallback_fingerprint(data_dir: Path) -> bytes:
    """无机器指纹平台的兜底：数据目录内随机密钥文件（首次生成后复用）。"""
    path = data_dir / _FALLBACK_KEY_FILE
    try:
        data = path.read_bytes()
        if len(data) >= _KEY_BYTES:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(_KEY_BYTES)
    _write_private(data_dir, path, data)
    return data


# ── 主密钥与用途密钥 ──────────────────────────────────────


def legacy_master(data_dir: Path | str) -> bytes:
    """legacy 数据主密钥：机器指纹 + 安装盐 → HKDF-SHA256。"""
    root = Path(data_dir)
    fingerprint = _machine_fingerprint() or _fallback_fingerprint(root)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_install_salt(root),
        info=_MASTER_INFO,
    ).derive(fingerprint)


def derive_purpose_key(master: bytes, purpose: str) -> bytes:
    """主密钥 → 用途子密钥（各用途 info 不同，彼此计算独立）。"""
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_PURPOSE_SALT,
        info=f"holdexar:secretbox:{purpose}".encode("utf-8"),
    ).derive(master)


@lru_cache(maxsize=128)
def _purpose_key(purpose: str, data_dir: str) -> bytes:
    """当前主密钥派生的用途密钥（缓存键含数据目录，多实例互不串钥）。

    主密钥解析失败（口令模式未解锁 / 密钥文件损坏）一律抛 SecretBoxError，
    消费侧据此降级为「无凭据」，不把锁定态当成数据损坏。换钥后调用
    clear_key_cache。
    """
    try:
        master = keyring.resolve_dmk(Path(data_dir))
    except (keyring.KeyringError, keyring.KeyLockedError) as exc:
        raise SecretBoxError(str(exc)) from exc
    return derive_purpose_key(master, purpose)


def _candidate_keys(purpose: str, data_dir: str) -> list[bytes]:
    """解密候选密钥：当前主密钥优先，non-legacy 模式追加 legacy 兜底。

    兜底只用于覆盖换钥崩溃窗口（新 DMK 已落盘、旧密文尚未迁完）。口令模式
    未解锁时 `_purpose_key` 直接抛错，不会走到 legacy——锁定态不得绕过。
    """
    keys = [_purpose_key(purpose, data_dir)]
    try:
        mode = keyring.mode_of(Path(data_dir))
    except keyring.KeyringError as exc:
        raise SecretBoxError(str(exc)) from exc
    if mode != keyring.MODE_LEGACY:
        keys.append(derive_purpose_key(legacy_master(Path(data_dir)), purpose))
    return keys


def clear_key_cache() -> None:
    """清空用途密钥缓存（换钥 / 换数据目录 / 测试隔离后调用）。"""
    _purpose_key.cache_clear()


# ── 封装 / 解封 ───────────────────────────────────────────


def encrypt_with(plaintext: str, purpose: str, master: bytes) -> str:
    """用指定主密钥封装（换钥重加密用；空串原样返回）。"""
    if not plaintext:
        return ""
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(derive_purpose_key(master, purpose)).encrypt(
        nonce, plaintext.encode("utf-8"), None
    )
    return PREFIX + ":".join(
        (
            base64.urlsafe_b64encode(nonce).decode("ascii"),
            base64.urlsafe_b64encode(sealed).decode("ascii"),
        )
    )


def decrypt_with(token: str, purpose: str, master: bytes) -> str:
    """用指定主密钥解封（换钥重加密用）。失败抛 SecretBoxError。"""
    nonce, sealed = _parse_token(token)
    try:
        plain = AESGCM(derive_purpose_key(master, purpose)).decrypt(nonce, sealed, None)
    except InvalidTag as exc:
        raise SecretBoxError("凭据解密失败（密文被篡改或派生密钥不符）") from exc
    return _decode_plain(plain)


def encrypt_secret(plaintext: str, purpose: str) -> str:
    """明文 → `enc1:` 密文：`enc1:<b64(nonce)>:<b64(密文+认证标签)>`。

    每次随机 nonce，同一明文两次加密得到不同密文；空串原样返回（无凭据
    不产生密文）。数据目录不可写（盐无法生成）或凭据处于锁定态时抛
    SecretBoxError。
    """
    return encrypt_with(plaintext, purpose, _current_master())


def decrypt_secret(token: str, purpose: str) -> str:
    """`enc1:` 密文 → 明文。

    非密文格式 / base64 损坏 / 认证标签不符 / 密钥不符一律抛
    SecretBoxError——调用方据此降级（存量明文在读取侧先按 is_encrypted
    分流，不会进入本函数）。
    """
    if not token:
        return ""
    if not token.startswith(PREFIX):
        raise SecretBoxError("凭据值不是密文格式（缺 enc1: 前缀）")
    nonce, sealed = _parse_token(token)
    last: SecretBoxError | None = None
    for key in _candidate_keys(purpose, str(_data_dir())):
        try:
            plain = AESGCM(key).decrypt(nonce, sealed, None)
        except InvalidTag as exc:
            last = SecretBoxError("凭据解密失败（密文被篡改或派生密钥不符）")
            last.__cause__ = exc
            continue
        return _decode_plain(plain)
    raise last or SecretBoxError("凭据解密失败")


def current_master() -> bytes:
    """当前主密钥（换钥编排用）；锁定 / 损坏时抛 SecretBoxError。"""
    return _current_master()


def _current_master() -> bytes:
    try:
        return keyring.resolve_dmk(_data_dir())
    except (keyring.KeyringError, keyring.KeyLockedError) as exc:
        raise SecretBoxError(str(exc)) from exc


def _parse_token(token: str) -> tuple[bytes, bytes]:
    nonce_b64, _, sealed_b64 = token[len(PREFIX):].partition(":")
    if not nonce_b64 or not sealed_b64:
        raise SecretBoxError("密文格式不完整")
    try:
        nonce = base64.urlsafe_b64decode(nonce_b64.encode("ascii"))
        sealed = base64.urlsafe_b64decode(sealed_b64.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretBoxError("密文 base64 解析失败") from exc
    return nonce, sealed


def _decode_plain(plain: bytes) -> str:
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretBoxError("凭据明文编码异常") from exc


__all__ = [
    "PREFIX",
    "SecretBoxError",
    "clear_key_cache",
    "current_master",
    "decrypt_secret",
    "decrypt_with",
    "derive_purpose_key",
    "encrypt_secret",
    "encrypt_with",
    "is_encrypted",
    "legacy_master",
]
