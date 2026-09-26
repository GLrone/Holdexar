"""凭据静态加密：AES-256-GCM 密文落库 + HKDF-SHA256 派生密钥。

本地库里的登录凭据（Steam 登录 Cookie / Web API Key）是长期有效的敏感
材料：落库一律经本模块封装为 `enc1:` 前缀密文，消费时才解密，日志与
接口响应不出现明文。

密钥不在盘上存明文，由两份本机材料经 HKDF-SHA256 派生：

- 机器指纹：Windows MachineGuid → Linux machine-id → macOS
  IOPlatformUUID，全部不可用时回退数据目录内的随机密钥文件；
- 安装盐：数据目录 `secret.salt`（首次使用生成 16 字节随机数）。

同一台机器上的同一数据目录密钥恒定（重启 / 升级无感）。库文件被单独
拷走（缺安装盐）或整机迁移（机器指纹不同）后密文不可解——凭据属主
语义，相关账号走「重新登录」路径重建，行内密文保留待覆盖。
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
    """凭据解密失败（密文被篡改、派生密钥不符或格式不识别）。"""


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


@lru_cache(maxsize=128)
def _purpose_key(purpose: str, data_dir: str) -> bytes:
    """用途密钥：机器指纹 + 安装盐 → HKDF 主密钥 → HKDF 用途子密钥。

    各用途派生 info 不同，密钥彼此计算独立；缓存键含数据目录（测试隔离
    与多实例互不串钥）。换机器指纹 / 数据目录后调用 clear_key_cache。
    """
    root = Path(data_dir)
    fingerprint = _machine_fingerprint() or _fallback_fingerprint(root)
    master = HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_install_salt(root),
        info=_MASTER_INFO,
    ).derive(fingerprint)
    return HKDF(
        algorithm=hashes.SHA256(),
        length=_KEY_BYTES,
        salt=_PURPOSE_SALT,
        info=f"holdexar:secretbox:{purpose}".encode("utf-8"),
    ).derive(master)


def clear_key_cache() -> None:
    """清空用途密钥缓存（测试环境换数据目录 / 指纹后调用）。"""
    _purpose_key.cache_clear()


def encrypt_secret(plaintext: str, purpose: str) -> str:
    """明文 → `enc1:` 密文：`enc1:<b64(nonce)>:<b64(密文+认证标签)>`。

    每次随机 nonce，同一明文两次加密得到不同密文；空串原样返回（无凭据
    不产生密文）。数据目录不可写（盐无法生成）时抛 SecretBoxError。
    """
    if not plaintext:
        return ""
    key = _purpose_key(purpose, str(_data_dir()))
    nonce = secrets.token_bytes(_NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
    return PREFIX + ":".join(
        (
            base64.urlsafe_b64encode(nonce).decode("ascii"),
            base64.urlsafe_b64encode(sealed).decode("ascii"),
        )
    )


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
    nonce_b64, _, sealed_b64 = token[len(PREFIX):].partition(":")
    if not nonce_b64 or not sealed_b64:
        raise SecretBoxError("密文格式不完整")
    try:
        nonce = base64.urlsafe_b64decode(nonce_b64.encode("ascii"))
        sealed = base64.urlsafe_b64decode(sealed_b64.encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretBoxError("密文 base64 解析失败") from exc
    key = _purpose_key(purpose, str(_data_dir()))
    try:
        plain = AESGCM(key).decrypt(nonce, sealed, None)
    except InvalidTag as exc:
        raise SecretBoxError("凭据解密失败（密文被篡改或派生密钥不符）") from exc
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SecretBoxError("凭据明文编码异常") from exc
