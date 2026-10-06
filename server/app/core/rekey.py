"""全库密文换钥重加密 + 密钥防护模式切换编排。

切换 keyring 模式时，库内既有敏感密文必须从旧主密钥迁到新主密钥，否则新模式下
一律解不开。**唯一事实源清单**（新增密文列必须登记到这里，否则换钥后读不出）：

- `steam_accounts.cookies`        用途 steam-cookies
- `app_settings.value_json`       用途 kv:<key>（account.steam_api_key 及
                                  pilot.llm.provider_key.<id> 等动态键）
- `proxy_subscriptions.url`       用途 proxy-subscription
- `subscription_snapshots.url`    用途 proxy-subscription

换钥在**单事务**内完成：中途失败整体回滚，密钥文件同时回滚，不存在半迁移。
崩溃窗口（新密钥已落盘、旧密文尚未迁完）由 secretbox 的 legacy 兜底解密覆盖。
"""
from __future__ import annotations

import logging
import secrets

from app.core import keyring, secretbox
from app.core.database import WritePriority, get_session_factory, write_gate
from app.core.logging import log_event

logger = logging.getLogger(__name__)

_MODE_LABEL = {
    keyring.MODE_LEGACY: "关闭（机器绑定）",
    keyring.MODE_DPAPI: "系统凭据保护",
    keyring.MODE_PASSPHRASE: "口令保护",
}


def _migrate(token: str | None, purpose: str, old: bytes, new: bytes) -> str | None:
    """单个密文值换钥；非密文 / 空值返回 None（调用方跳过，不写回）。"""
    if not token or not secretbox.is_encrypted(token):
        return None
    plain = secretbox.decrypt_with(token, purpose, old)
    return secretbox.encrypt_with(plain, purpose, new)


async def reencrypt_all(old_master: bytes, new_master: bytes) -> dict:
    """库内全部敏感密文从 old_master 迁到 new_master（单事务）。返回各表条数。"""
    from app.domains.account.models import SteamAccount
    from app.domains.account.service import _CREDENTIAL_PURPOSE
    from app.domains.proxies.models import ProxySubscription
    from app.domains.proxies.subscription_secret import PURPOSE as SUB_PURPOSE
    from app.domains.proxypool.models import SubscriptionSnapshot
    from app.domains.settings.models import AppSetting
    from app.domains.settings.service import _secret_purpose
    from sqlalchemy import select

    counts = {"accountCookies": 0, "settingSecrets": 0, "subscriptions": 0, "snapshots": 0}
    async with write_gate(
        WritePriority.INTERACTIVE, label="rekey"
    ), get_session_factory()() as session:
        for row in (await session.execute(select(SteamAccount))).scalars().all():
            migrated = _migrate(row.cookies, _CREDENTIAL_PURPOSE, old_master, new_master)
            if migrated is not None:
                row.cookies = migrated
                counts["accountCookies"] += 1

        for row in (await session.execute(select(AppSetting))).scalars().all():
            value = row.value_json
            if isinstance(value, str) and secretbox.is_encrypted(value):
                purpose = _secret_purpose(row.key)
                row.value_json = secretbox.encrypt_with(
                    secretbox.decrypt_with(value, purpose, old_master), purpose, new_master
                )
                counts["settingSecrets"] += 1

        for row in (await session.execute(select(ProxySubscription))).scalars().all():
            migrated = _migrate(row.url, SUB_PURPOSE, old_master, new_master)
            if migrated is not None:
                row.url = migrated
                counts["subscriptions"] += 1

        for row in (await session.execute(select(SubscriptionSnapshot))).scalars().all():
            migrated = _migrate(row.url, SUB_PURPOSE, old_master, new_master)
            if migrated is not None:
                row.url = migrated
                counts["snapshots"] += 1

        await session.commit()

    log_event(
        logger,
        "全库密钥换装完成",
        tag="成功",
        detail={
            "账号Cookie": counts["accountCookies"],
            "设置凭据": counts["settingSecrets"],
            "订阅": counts["subscriptions"],
            "快照": counts["snapshots"],
        },
    )
    return counts


async def switch_mode(
    mode: str,
    *,
    passphrase: str | None = None,
    current_passphrase: str | None = None,
) -> dict:
    """切换密钥防护模式并完成全库换钥。

    - mode=legacy：换钥到机器派生的主密钥，之后删除密钥文件；
    - mode=dpapi：新随机主密钥用 DPAPI 封装（仅 Windows）；
    - mode=passphrase：新随机主密钥用口令封装；返回后进程处于已解锁态。

    当前为口令模式且锁定时，须提供 current_passphrase 解锁后才能换钥。
    失败（口令错 / DPAPI 不可用 / 换钥异常）时不改变库与密钥文件。
    """
    if mode not in keyring.MODES:
        raise keyring.KeyringError(f"未知的密钥模式：{mode!r}")

    data_dir = secretbox._data_dir()
    old_mode = keyring.mode_of(data_dir)
    if old_mode == keyring.MODE_PASSPHRASE and keyring.is_locked(data_dir):
        if not current_passphrase or not keyring.unlock(data_dir, current_passphrase):
            raise keyring.KeyringError("当前口令不正确或未提供，无法切换保护方式")

    old_master = keyring.resolve_dmk(data_dir)
    old_record = keyring.snapshot_record(data_dir)

    if mode == keyring.MODE_LEGACY:
        new_master = secretbox.legacy_master(data_dir)
        counts = await reencrypt_all(old_master, new_master)
        keyring.remove_record(data_dir)
        keyring.reset_state()
        secretbox.clear_key_cache()
        log_event(logger, "密钥保护已关闭，回到机器绑定模式")
        return {"mode": mode, "counts": counts}

    new_master = secrets.token_bytes(32)
    if mode == keyring.MODE_DPAPI:
        record = keyring.wrap_dpapi(new_master, data_dir)
    else:
        if not passphrase:
            raise keyring.KeyringError("口令不能为空")
        record = keyring.wrap_passphrase(new_master, passphrase)

    # 先落新密钥文件、再换钥：崩溃窗口由 legacy 兜底解密覆盖；换钥失败回滚文件
    keyring.write_record(data_dir, record)
    try:
        counts = await reencrypt_all(old_master, new_master)
    except Exception:
        keyring.restore_record(data_dir, old_record)
        secretbox.clear_key_cache()
        log_event(
            logger,
            "密钥换装失败，密钥文件已回滚",
            level=logging.ERROR,
            exc_info=True,
        )
        raise

    if mode == keyring.MODE_PASSPHRASE:
        keyring.load_unlocked_key(new_master)  # 刚设的口令即当前会话口令
    secretbox.clear_key_cache()
    log_event(
        logger,
        "密钥保护已启用",
        tag="成功",
        detail={"保护方式": _MODE_LABEL.get(mode, mode)},
    )
    return {"mode": mode, "counts": counts}
