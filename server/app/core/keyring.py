"""数据主密钥托管：凭据静态加密的密钥来源与防护模式。

三种模式（落点：数据目录 `secret.master`；文件不存在 = legacy）：

- legacy：主密钥由机器指纹 + 安装盐经 HKDF 派生。同机同数据目录恒定；库文件
  被单独拷走（缺安装盐）或整机迁移（指纹不同）后不可解，但**不防本机同用户
  进程**读取——本机材料（MachineGuid + secret.salt）对同机进程可见。
- dpapi（仅 Windows）：主密钥为随机数，用 DPAPI 用户级封装后落盘，运行时由
  系统凭据解封。转换 Windows 用户 / 换机器不可解，无需口令。
- passphrase：主密钥为随机数，用口令经 scrypt 派生的密钥封装后落盘。进程启动
  即处于锁定态，输入口令解封后才能读写凭据；忘记口令/换机不可解。

进程内不持久化主密钥：每次启动重新解封（passphrase 需重新输入口令）。

换钥中途崩溃由「legacy 兜底解密」覆盖（见 secretbox._candidate_keys）：legacy
主密钥恒可由本机材料重建，读取侧在新主密钥解不开时回落 legacy，迁移完成后库内
不再有任何 legacy 密文，兜底自然失效。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

MODE_LEGACY = "legacy"
MODE_DPAPI = "dpapi"
MODE_PASSPHRASE = "passphrase"
MODES = (MODE_LEGACY, MODE_DPAPI, MODE_PASSPHRASE)

# 密钥文件（数据目录内）：封装后的主密钥 + 模式元数据
MASTER_FILE = "secret.master"
_FILE_VERSION = 1

_KEY_BYTES = 32
_SCRYPT_N = 1 << 15  # CPU 成本：交互式解锁 <150ms 量级，抗离线爆破
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = _KEY_BYTES
# OpenSSL 默认 maxmem 恰为 32MiB，n=2^15/r=8 需要 128*n*r = 32MiB 会被判超限，
# 显式抬到 64MiB（够用且不放开到危险规模）
_SCRYPT_MAXMEM = 64 * 1024 * 1024
_SALT_BYTES = 16
_NONCE_BYTES = 12

# DPAPI 附加熵（第二因素）：同一 blob 即使被别的进程拿到也无法在白盒外解封
_DPAPI_ENTROPY = b"holdexar:keyring:dpapi:v1"
_CRYPTPROTECT_UI_FORBIDDEN = 0x01


class KeyringError(RuntimeError):
    """密钥托管操作失败（封装不可用 / 口令不正确 / 文件损坏）。"""


class KeyLockedError(RuntimeError):
    """口令保护模式处于锁定态：凭据不可读写。"""


# 进程内解封后的主密钥。legacy / dpapi 首次使用即自动填充；passphrase 只在
# 用户解锁后填充，重启即失效（不落盘、不进缓存文件）。
_unlocked_key: bytes | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text.encode("ascii"))


def reset_state() -> None:
    """清进程内主密钥缓存（测试 / 换数据目录用）。"""
    global _unlocked_key
    _unlocked_key = None


def load_unlocked_key(key: bytes) -> None:
    """把已解封的主密钥写入进程态（换钥成功后由调用方回填）。"""
    global _unlocked_key
    _unlocked_key = key


# ── 模式读写 ──────────────────────────────────────────────


def _master_path(data_dir: Path) -> Path:
    return Path(data_dir) / MASTER_FILE


def read_record(data_dir: Path) -> dict | None:
    """读取密钥文件（不存在返回 None；损坏抛 KeyringError）。"""
    path = _master_path(data_dir)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        record = json.loads(text)
    except (ValueError, UnicodeDecodeError) as exc:
        raise KeyringError(f"密钥文件损坏，无法解析：{path}") from exc
    if not isinstance(record, dict):
        raise KeyringError(f"密钥文件格式非法：{path}")
    return record


def mode_of(data_dir: Path) -> str:
    """当前防护模式；无密钥文件 = legacy。"""
    record = read_record(data_dir)
    if record is None:
        return MODE_LEGACY
    mode = str(record.get("mode") or "")
    if mode in (MODE_DPAPI, MODE_PASSPHRASE):
        return mode
    raise KeyringError(f"未知的密钥模式：{mode!r}")


def is_locked(data_dir: Path) -> bool:
    """口令模式且尚未解锁时为 True（其余模式恒 False）。"""
    return mode_of(data_dir) == MODE_PASSPHRASE and _unlocked_key is None


def dpapi_available() -> bool:
    return sys.platform == "win32"


def status(data_dir: Path) -> dict:
    """面向界面的状态投影（不泄露主密钥）。"""
    try:
        mode = mode_of(data_dir)
    except KeyringError as exc:
        return {
            "mode": "unknown",
            "locked": True,
            "dpapi_available": dpapi_available(),
            "error": str(exc),
        }
    return {
        "mode": mode,
        "locked": is_locked(data_dir),
        "dpapi_available": dpapi_available(),
        "error": "",
    }


# ── 主密钥解析 ────────────────────────────────────────────


def _legacy_master(data_dir: Path) -> bytes:
    """legacy 主密钥：委托 secretbox 用本机材料派生（延迟导入避免循环）。"""
    from app.core import secretbox

    return secretbox.legacy_master(Path(data_dir))


def resolve_dmk(data_dir: Path) -> bytes:
    """返回当前主密钥；口令模式未解锁抛 KeyLockedError。

    legacy / dpapi 的主密钥可随时重算（指纹 + 盐 / 系统凭据），故不进程级缓存
    ——派生结果由 secretbox 的用途密钥缓存接手，避免「换指纹 / 换数据目录后仍
    命中旧密钥」。只有口令模式需要把解封结果留在进程内（无法重算）。
    """
    mode = mode_of(data_dir)
    if mode == MODE_PASSPHRASE:
        if _unlocked_key is None:
            raise KeyLockedError("凭据处于口令保护锁定态，需先解锁")
        return _unlocked_key
    if mode == MODE_LEGACY:
        return _legacy_master(data_dir)
    return _unwrap_dpapi(read_record(data_dir) or {})


def unlock(data_dir: Path, passphrase: str) -> bool:
    """口令模式解锁：口令正确则写入进程态主密钥，返回是否成功。"""
    global _unlocked_key  # noqa: PLW0603 —— 口令解封结果唯一落点
    record = read_record(data_dir)
    if record is None or record.get("mode") != MODE_PASSPHRASE:
        raise KeyringError("当前不是口令保护模式")
    key = _unwrap_passphrase(record, passphrase)
    if key is None:
        return False
    _unlocked_key = key
    return True


def lock() -> None:
    """清空进程内主密钥（口令模式回到锁定态）。"""
    global _unlocked_key
    _unlocked_key = None


# ── 封装 / 解封 ───────────────────────────────────────────


def wrap_dpapi(master: bytes, data_dir: Path) -> dict:
    """随机主密钥 → DPAPI 封装记录（仅 Windows）。"""
    if not dpapi_available():
        raise KeyringError("系统凭据保护在当前平台不可用")
    blob = _dpapi_protect(master)
    record = {
        "v": _FILE_VERSION,
        "mode": MODE_DPAPI,
        "blob": _b64(blob),
    }
    return record


def _unwrap_dpapi(record: dict) -> bytes:
    if not dpapi_available():
        raise KeyringError("系统凭据保护在当前平台不可用")
    try:
        blob = _unb64(str(record["blob"]))
    except (KeyError, ValueError) as exc:
        raise KeyringError("DPAPI 密钥记录不完整") from exc
    try:
        return _dpapi_unprotect(blob)
    except OSError as exc:
        raise KeyringError(
            "系统凭据无法解封（换了 Windows 用户或机器？）"
        ) from exc


def wrap_passphrase(master: bytes, passphrase: str) -> dict:
    """随机主密钥 → 口令封装记录（scrypt 派生 KEK + AES-256-GCM）。"""
    if not passphrase:
        raise KeyringError("口令不能为空")
    salt = secrets.token_bytes(_SALT_BYTES)
    kek = _scrypt(passphrase, salt)
    nonce = secrets.token_bytes(_NONCE_BYTES)
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    sealed = AESGCM(kek).encrypt(nonce, master, None)
    return {
        "v": _FILE_VERSION,
        "mode": MODE_PASSPHRASE,
        "kdf": {"name": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
                "dklen": _SCRYPT_DKLEN},
        "salt": _b64(salt),
        "nonce": _b64(nonce),
        "ct": _b64(sealed),
    }


def _unwrap_passphrase(record: dict, passphrase: str) -> bytes | None:
    """口令封装 → 主密钥；口令不正确返回 None。"""
    kdf = record.get("kdf") if isinstance(record.get("kdf"), dict) else {}
    try:
        salt = _unb64(str(record["salt"]))
        nonce = _unb64(str(record["nonce"]))
        sealed = _unb64(str(record["ct"]))
    except (KeyError, ValueError) as exc:
        raise KeyringError("口令密钥记录不完整") from exc
    kek = hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=int(kdf.get("n", _SCRYPT_N)),
        r=int(kdf.get("r", _SCRYPT_R)),
        p=int(kdf.get("p", _SCRYPT_P)),
        dklen=int(kdf.get("dklen", _SCRYPT_DKLEN)),
        maxmem=_SCRYPT_MAXMEM,
    )
    from cryptography.exceptions import InvalidTag
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    try:
        return AESGCM(kek).decrypt(nonce, sealed, None)
    except InvalidTag:
        return None  # 口令错误


def _scrypt(passphrase: str, salt: bytes) -> bytes:
    return hashlib.scrypt(
        passphrase.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_SCRYPT_MAXMEM,
    )


# ── 密钥文件落盘 ──────────────────────────────────────────


def write_record(data_dir: Path, record: dict) -> None:
    """密钥文件落盘（POSIX 收紧 0600；Windows 走 ACL 继承）。"""
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    path = _master_path(data_dir)
    try:
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        if sys.platform != "win32":
            path.chmod(0o600)
    except OSError as exc:
        raise KeyringError(f"密钥文件写入失败（数据目录不可写？）：{exc}") from exc


def remove_record(data_dir: Path) -> None:
    """删除密钥文件（回到 legacy 模式）。"""
    _master_path(data_dir).unlink(missing_ok=True)


def snapshot_record(data_dir: Path) -> bytes | None:
    """读取密钥文件原始字节（换钥失败回滚用）。"""
    try:
        return _master_path(data_dir).read_bytes()
    except OSError:
        return None


def restore_record(data_dir: Path, raw: bytes | None) -> None:
    """按原始字节恢复密钥文件；None = 删除（回滚到 legacy）。"""
    path = _master_path(data_dir)
    if raw is None:
        path.unlink(missing_ok=True)
        return
    path.write_bytes(raw)
    if sys.platform != "win32":
        path.chmod(0o600)


# ── DPAPI（ctypes 直调，避免引入 pywin32 增大打包体积）──────


def _dpapi_protect(data: bytes) -> bytes:
    return _dpapi_call(data, protect=True)


def _dpapi_unprotect(blob: bytes) -> bytes:
    return _dpapi_call(blob, protect=False)


def _dpapi_call(data: bytes, *, protect: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class _Blob(ctypes.Structure):
        _fields_ = [
            ("cbData", wintypes.DWORD),
            ("pbData", ctypes.POINTER(ctypes.c_char)),
        ]

    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    buf = ctypes.create_string_buffer(bytes(data), len(data))
    blob_in = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    entropy = ctypes.create_string_buffer(_DPAPI_ENTROPY, len(_DPAPI_ENTROPY))
    entropy_blob = _Blob(
        len(_DPAPI_ENTROPY), ctypes.cast(entropy, ctypes.POINTER(ctypes.c_char))
    )
    blob_out = _Blob()

    fn = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    ok = fn(
        ctypes.byref(blob_in),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


__all__ = [
    "MODE_DPAPI",
    "MODE_LEGACY",
    "MODE_PASSPHRASE",
    "MODES",
    "KeyLockedError",
    "KeyringError",
    "dpapi_available",
    "is_locked",
    "load_unlocked_key",
    "lock",
    "mode_of",
    "read_record",
    "remove_record",
    "reset_state",
    "resolve_dmk",
    "restore_record",
    "snapshot_record",
    "status",
    "unlock",
    "wrap_dpapi",
    "wrap_passphrase",
    "write_record",
]
