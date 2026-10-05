"""敏感数据加密导出：本机用户数据打包为口令加密的单文件。

**只导出用户侧数据**（解密后的凭据 / 游戏关注 / 监控与提醒 / 设置 / 代理订阅 /
账单与家庭组等），**不含随包种子与公共目录数据**（games / 价格快照与历史 /
捆绑包 / 汇率 / 区服表 / 成就定义等——这些由发布包与爬虫重建，不属于用户资产）。

导出文件为 JSON 信封，正文用导出口令经 scrypt 派生的密钥做 AES-256-GCM 加密：
拿到文件而没有口令，无法读出其中任何凭据。导出需当前主密钥可用（口令保护模式
下须先解锁），因为要解密库内密文后再重新封装。
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import sys
from datetime import datetime
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text

from app.core import keyring, secretbox
from app.core.app_info import APP_SLUG
from app.core.config import get_settings
from app.core.database import get_session_factory
from app.core.secretbox import SecretBoxError

logger = logging.getLogger(__name__)

FORMAT = "holdexar-secure-export"
FORMAT_VERSION = 1
EXPORT_DIR_NAME = "exports"
EXPORT_SUFFIX = ".hxexport"

_MIN_PASSWORD_LEN = 6
_SCRYPT_N = 1 << 15
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32
_SCRYPT_MAXMEM = 64 * 1024 * 1024  # OpenSSL 默认 32MiB 恰被 n=2^15/r=8 触顶，显式抬高
_SALT_BYTES = 16
_NONCE_BYTES = 12

# 用户侧数据表（导出白名单）。凭据类列（cookies / url）解密后再落盘。
_USER_TABLES = (
    "steam_accounts",
    "tracked_accounts",
    "wishlist_items",
    "monitor_targets",
    "monitor_sources",
    "monitor_exclusions",
    "price_alerts",
    "alert_events",
    "fact_notices",
    "proxies",
    "proxy_subscriptions",
    "clash_nodes",
    "steam_events",
    "achievement_states",
    "achievement_games",
    "family_groups",
    "family_library_snapshots",
    "bill_imports",
    "bill_game_txs",
    "bill_topup_txs",
    "bill_cdk_games",
)

# 随包种子 / 公共目录数据：不进导出（由发布包与爬虫重建，非用户资产）
_SEED_TABLES = (
    "games", "game_current_prices", "game_price_history",
    "bundles", "bundle_region_prices", "preset_games", "catalog_removals",
    "game_tags", "tags", "fx_rates", "fx_rate_history",
    "crawl_regions", "achievement_defs",
)

# 运行期派生 / 采集数据：不进导出（可重建，且含大量噪声）
_RUNTIME_TABLES = (
    "price_cycles", "crawl_jobs", "price_events", "subscription_snapshots",
    "proxy_job_runs", "proxy_events", "health_runs", "health_observations",
    "worker_leases", "proxy_run_exits", "proxy_nodes", "proxy_node_sources",
    "exit_groups", "lanes", "pool_generations", "orchestration_events",
    "notification_candidates", "agent_sessions", "agent_runs", "agent_events",
)

# app_settings 里属于「种子状态」的键（种子版本 marker），不进导出
_SEED_KEY_PREFIX = "seed."

_CONTAINS_LABELS = (
    "Steam 账号与登录凭据（Cookie / 续期令牌）",
    "Steam Web API Key 与自建 LLM 供应商密钥",
    "代理订阅链接与代理配置",
    "游戏关注（愿望单 / 监控池 / 排除项）",
    "价格提醒规则与提醒记录",
    "账单记录、家庭组、成就进度、在线事件",
    "界面偏好与其他用户设置",
)
_EXCLUDES_LABELS = (
    "游戏目录与名称等公共元数据",
    "价格现价快照与历史价格",
    "捆绑包目录与价格、汇率档案",
    "区服配置、成就定义、玩家标签",
)


def export_dir(data_dir: Path) -> Path:
    path = Path(data_dir) / EXPORT_DIR_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii")


def _decrypt_or_raw(token, purpose: str) -> str:
    """凭据列导出值：密文解密为明文；解不开 / 非密文原样带出（不丢数据）。"""
    value = token or ""
    if not value or not secretbox.is_encrypted(str(value)):
        return value
    try:
        return secretbox.decrypt_secret(str(value), purpose)
    except SecretBoxError:
        logger.warning("[导出] 凭据无法解密，按密文原样导出（purpose=%s）", purpose)
        return str(value)


async def _dump_table(session, table: str) -> list[dict]:
    try:
        rows = (
            await session.execute(text(f'SELECT * FROM "{table}"'))
        ).mappings().all()
    except Exception:  # noqa: BLE001 —— 表缺失（版本差异）跳过
        logger.debug("[导出] 表不存在，跳过：%s", table)
        return []
    return [dict(r) for r in rows]


async def build_payload() -> dict:
    """组装导出正文（凭据类列已解密）。口令保护锁定时抛 SecretBoxError。"""
    from app.domains.account.service import _CREDENTIAL_PURPOSE
    from app.domains.proxies.subscription_secret import PURPOSE as SUB_PURPOSE
    from app.domains.settings.service import _secret_purpose

    settings = get_settings()
    if keyring.is_locked(Path(settings.data_dir)):
        raise SecretBoxError("凭据处于锁定态，请先解锁后再导出")

    tables: dict[str, list[dict]] = {}
    async with get_session_factory()() as session:
        # app_settings：剔除种子 marker，凭据键解密
        settings_rows = await _dump_table(session, "app_settings")
        kept: list[dict] = []
        for row in settings_rows:
            key = str(row.get("key") or "")
            if key.startswith(_SEED_KEY_PREFIX):
                continue
            raw = row.get("value_json")
            try:
                value = json.loads(raw) if isinstance(raw, str) else raw
            except ValueError:
                value = raw
            if isinstance(value, str) and secretbox.is_encrypted(value):
                value = _decrypt_or_raw(value, _secret_purpose(key))
            kept.append({"key": key, "value": value, "updated_at": row.get("updated_at")})
        tables["app_settings"] = kept

        for table in _USER_TABLES:
            rows = await _dump_table(session, table)
            if table == "steam_accounts":
                for row in rows:
                    row["cookies"] = _decrypt_or_raw(row.get("cookies"), _CREDENTIAL_PURPOSE)
            elif table == "proxy_subscriptions":
                for row in rows:
                    row["url"] = _decrypt_or_raw(row.get("url"), SUB_PURPOSE)
            tables[table] = rows

    counts = {table: len(rows) for table, rows in tables.items() if rows}
    return {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "exportedAt": datetime.now().isoformat(),
        "app": {"name": settings.app_name, "version": settings.version},
        "contains": list(_CONTAINS_LABELS),
        "excludes": list(_EXCLUDES_LABELS),
        "counts": counts,
        "tables": tables,
    }


def encrypt_payload(payload: dict, password: str) -> dict:
    """正文 → 口令加密信封（JSON 可序列化）。"""
    salt = secrets.token_bytes(_SALT_BYTES)
    kek = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=_DKLEN, maxmem=_SCRYPT_MAXMEM,
    )
    nonce = secrets.token_bytes(_NONCE_BYTES)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    sealed = AESGCM(kek).encrypt(nonce, body, None)
    return {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "cipher": "AES-256-GCM",
        "kdf": {"name": "scrypt", "n": _SCRYPT_N, "r": _SCRYPT_R, "p": _SCRYPT_P,
                "dklen": _DKLEN, "salt": _b64(salt)},
        "nonce": _b64(nonce),
        "ciphertext": _b64(sealed),
    }


def decrypt_payload(envelope: dict, password: str) -> dict:
    """口令加密信封 → 正文；口令错误抛 SecretBoxError。"""
    from cryptography.exceptions import InvalidTag

    kdf = envelope.get("kdf") if isinstance(envelope.get("kdf"), dict) else {}
    try:
        salt = base64.urlsafe_b64decode(str(kdf["salt"]).encode("ascii"))
        nonce = base64.urlsafe_b64decode(str(envelope["nonce"]).encode("ascii"))
        sealed = base64.urlsafe_b64decode(str(envelope["ciphertext"]).encode("ascii"))
    except (KeyError, ValueError) as exc:
        raise SecretBoxError("导出文件格式不完整") from exc
    kek = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=int(kdf.get("n", _SCRYPT_N)), r=int(kdf.get("r", _SCRYPT_R)),
        p=int(kdf.get("p", _SCRYPT_P)), dklen=int(kdf.get("dklen", _DKLEN)),
        maxmem=_SCRYPT_MAXMEM,
    )
    try:
        plain = AESGCM(kek).decrypt(nonce, sealed, None)
    except InvalidTag as exc:
        raise SecretBoxError("口令不正确或文件被篡改") from exc
    return json.loads(plain.decode("utf-8"))


async def create_export(password: str) -> dict:
    """组装 → 加密 → 落盘，返回文件信息。口令过短抛 ValueError。"""
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise ValueError(f"导出口令至少 {_MIN_PASSWORD_LEN} 位")
    payload = await build_payload()
    envelope = encrypt_payload(payload, password)

    settings = get_settings()
    out_dir = export_dir(Path(settings.data_dir))
    name = f"{APP_SLUG}-secure-{datetime.now().strftime('%Y%m%d-%H%M%S')}{EXPORT_SUFFIX}"
    out_path = out_dir / name
    out_path.write_text(json.dumps(envelope, ensure_ascii=False), encoding="utf-8")
    if sys.platform != "win32":
        out_path.chmod(0o600)

    size = out_path.stat().st_size
    logger.info("[导出] 加密导出完成：%s（%.1f KB，%d 张表）",
                name, size / 1024, len(payload["counts"]))
    return {
        "path": str(out_path),
        "name": name,
        "sizeBytes": size,
        "createdAt": datetime.now().isoformat(),
        "counts": payload["counts"],
        "excludes": payload["excludes"],
    }


def list_exports() -> list[dict]:
    """已有导出文件（新→旧）。"""
    settings = get_settings()
    out_dir = Path(settings.data_dir) / EXPORT_DIR_NAME
    if not out_dir.is_dir():
        return []
    items = []
    for p in sorted(out_dir.glob(f"*{EXPORT_SUFFIX}"), key=lambda x: x.stat().st_mtime, reverse=True):
        st = p.stat()
        items.append({
            "name": p.name,
            "sizeBytes": st.st_size,
            "createdAt": datetime.fromtimestamp(st.st_mtime).isoformat(),
        })
    return items


def safe_export_path(name: str) -> Path:
    """路径穿越防护：导出文件名只允许合法字符且必须落在导出目录内。"""
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"非法导出文件名: {name!r}")
    if not all(c.isalnum() or c in "-_." for c in name) or not name.endswith(EXPORT_SUFFIX):
        raise ValueError(f"非法导出文件名: {name!r}")
    base = Path(get_settings().data_dir) / EXPORT_DIR_NAME
    path = base / name
    if path.parent != base:
        raise ValueError(f"非法导出文件名: {name!r}")
    return path
