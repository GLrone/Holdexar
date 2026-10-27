"""settings 域服务：键值读写。

已知键：
- account.steam_id          SteamID64
- account.steam_api_key     Steam Web API Key（用户自己的，静态加密落库，
                            经 get_secret_value / set_secret_value 读写）

区服配置归 domains/regions（crawl_regions 表 + GET /api/v1/regions）。
"""
from __future__ import annotations

from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core import secretbox
from app.core.database import WritePriority, get_session_factory, write_session
from app.core.secretbox import SecretBoxError
from app.domains.settings.models import AppSetting
from app.crawler.utils import get_beijing_time_obj

# 凭据类设置键：落库即 AES-256-GCM 密文（secretbox 按机器派生密钥），
# 读取时解密；存量明文由启动加密步骤（seal_secret_values）封装
SECRET_KEYS = ("account.steam_api_key", "pilot.llm.api_key", "pilot.llm.provider_key.legacy-v1")


def _secret_purpose(key: str) -> str:
    """凭据键的加密用途串（每键独立派生密钥）。"""
    # 迁入的旧单配置密文保持原用途；已发布的 main 供应商仍用它自己的用途。
    if key == "pilot.llm.provider_key.legacy-v1":
        return "kv:pilot.llm.api_key"
    return f"kv:{key}"


async def get_value(key: str, default=None):
    async with get_session_factory()() as session:
        row = await session.get(AppSetting, key)
        return row.value_json if row else default


async def delete_value(key: str) -> None:
    """删除键（下架功能的设置项清账用，不存在时静默）。"""
    from sqlalchemy import delete

    async with write_session(WritePriority.INTERACTIVE, label="settings") as session:
        await session.execute(delete(AppSetting).where(AppSetting.key == key))
        await session.commit()


async def set_value(key: str, value) -> None:
    await set_values({key: value})


async def set_values(values: dict, *, secret_values: dict | None = None) -> None:
    """同一次配置操作批量提交；凭据在现有设置边界密封。"""
    values = dict(values)
    for key, value in (secret_values or {}).items():
        values[key] = secretbox.encrypt_secret(str(value), _secret_purpose(key)) if value not in (None, "") else value
    if not values:
        return
    now = get_beijing_time_obj().replace(tzinfo=None)
    async with write_session(WritePriority.INTERACTIVE, label="settings") as session:
        stmt = sqlite_insert(AppSetting).values([
            {"key": key, "value_json": value, "updated_at": now} for key, value in values.items()
        ])
        await session.execute(
            stmt.on_conflict_do_update(
                index_elements=[AppSetting.key],
                set_={"value_json": stmt.excluded.value_json, "updated_at": stmt.excluded.updated_at},
            )
        )
        await session.commit()


async def get_secret_value(key: str, default=None):
    """凭据键读取：密文解密后返回；解密失败按无凭据处理（返回 default）。

    存量明文（尚无 `enc1:` 前缀，启动加密步骤未跑到）原样返回，读取不因
    迁移时序中断。仅限 SECRET_KEYS 内的键使用。
    """
    stored = await get_value(key, None)
    if stored is None or stored == "":
        return default
    if not secretbox.is_encrypted(str(stored)):
        return stored
    try:
        return secretbox.decrypt_secret(str(stored), _secret_purpose(key))
    except SecretBoxError:
        return default


async def set_secret_value(key: str, value) -> None:
    """凭据键写入：非空值封装为密文落库；空值 / None 原样落库（清空语义）。"""
    await set_values({}, secret_values={key: value})


async def seal_secret_values() -> int:
    """存量明文凭据键封装为密文（启动幂等步骤；已带密文前缀的跳过）。"""
    sealed = 0
    for key in SECRET_KEYS:
        stored = await get_value(key, None)
        if stored is None or stored == "" or secretbox.is_encrypted(str(stored)):
            continue
        await set_value(key, secretbox.encrypt_secret(str(stored), _secret_purpose(key)))
        sealed += 1
    return sealed
