"""全库密文换钥重加密 + 密钥防护模式切换编排。

切换 keyring 模式时，库内既有敏感密文必须从旧主密钥迁到新主密钥，否则新模式下
一律解不开。**唯一事实源清单**（新增密文列必须登记到这里，否则换钥后读不出）：

- `steam_accounts.cookies`        用途 steam-cookies
- `app_settings.value_json`       用途 kv:<key>（account.steam_api_key 及
                                  pilot.llm.provider_key.<id> 等动态键）
- `proxy_subscriptions.url`       用途 proxy-subscription
- `subscription_snapshots.url`    用途 proxy-subscription
- `proxies.username` / `.password` 用途 proxy-credential

换钥在**单事务**内完成：中途失败整体回滚，密钥文件同时回滚，不存在半迁移。
崩溃窗口由**换钥过渡记录**覆盖（见 `switch_mode` docstring 的协议说明）：
迁移前把旧主密钥按旧模式同等强度封装落盘，任意一步被强杀后旧密文仍可解；
迁移成功即删除该记录。启动时 `resume_pending_migration` 做收尾判定。
"""
from __future__ import annotations

import logging
import secrets

from app.core import keyring, secretbox
from app.core.database import WritePriority, write_session
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
    try:
        from app.domains.proxies.credentials import PURPOSE as CRED_PURPOSE
    except ImportError:
        CRED_PURPOSE = None
    from app.domains.proxies.models import Proxy, ProxySubscription
    from app.domains.proxies.subscription_secret import PURPOSE as SUB_PURPOSE
    from app.domains.proxypool.models import SubscriptionSnapshot
    from app.domains.settings.models import AppSetting
    from app.domains.settings.service import _secret_purpose
    from sqlalchemy import select

    counts = {
        "accountCookies": 0, "settingSecrets": 0,
        "subscriptions": 0, "snapshots": 0, "proxyCredentials": 0,
    }
    async with write_session(WritePriority.INTERACTIVE, label="rekey") as session:
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

        if CRED_PURPOSE:
            for row in (await session.execute(select(Proxy))).scalars().all():
                for field in ("username", "password"):
                    migrated = _migrate(
                        getattr(row, field), CRED_PURPOSE, old_master, new_master
                    )
                    if migrated is not None:
                        setattr(row, field, migrated)
                        counts["proxyCredentials"] += 1

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

    **崩溃恢复协议**（顺序不可调换）：

    1. 落过渡记录 `secret.master.staging`（旧主密钥按旧模式同等强度封装）；
    2. 落新密钥文件；
    3. 换钥（单事务）；
    4. 成功后删过渡记录；失败则回滚密钥文件并删过渡记录。

    任意一步被强杀，库内密文要么是旧的（过渡记录可解）要么是新的
    （密钥文件可解）——不存在两边都解不开的中间态。
    """
    if mode not in keyring.MODES:
        raise keyring.KeyringError(f"未知的密钥模式：{mode!r}")

    data_dir = secretbox._data_dir()
    old_mode = keyring.mode_of(data_dir)
    if old_mode == keyring.MODE_PASSPHRASE and keyring.is_locked(data_dir):
        if not current_passphrase or not keyring.unlock(data_dir, current_passphrase):
            raise keyring.KeyringError("当前口令不正确或未提供，无法切换保护方式")

    old_master = keyring.resolve_dmk(data_dir)

    if mode == keyring.MODE_LEGACY:
        new_master = secretbox.legacy_master(data_dir)
        counts = await _reencrypt_with_staging(data_dir, old_master, new_master)
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

    counts = await _reencrypt_with_staging(
        data_dir, old_master, new_master, record=record
    )

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


def _staging_record(old_master: bytes, new_master: bytes) -> dict:
    """用新主密钥封装旧主密钥。

    这样任何后续进程只要拿到当前主密钥就能解出旧密钥，不需要用户口令、
    也不依赖旧模式——从口令模式切出去后崩溃也能自愈。
    """
    return keyring.wrap_staging(old_master, new_master)


async def _reencrypt_with_staging(
    data_dir,
    old_master: bytes,
    new_master: bytes,
    *,
    record: dict | None = None,
) -> dict:
    """按崩溃恢复协议执行：新密钥文件 → 过渡记录 → 换钥 → 清理。

    次序与「过渡记录在前」的直觉相反：过渡记录用**新主密钥**封装，新密钥
    文件没落盘之前不存在能封装它的密钥。因此必须先落新密钥文件，再落过渡
    记录——两步都是 `os.replace` 原子替换，之间只留文件系统级最短间隔。

    `record` 为新密钥文件内容；`mode=legacy` 时为 None（该方向不落密钥文件，
    此时新主密钥可由本机材料重算，legacy 兜底恒可用）。
    """
    old_record = keyring.snapshot_record(data_dir)
    staged = _staging_record(old_master, new_master)
    try:
        if record is not None:
            keyring.write_record(data_dir, record)
        keyring.write_staging(data_dir, staged)
        secretbox.clear_key_cache()
        counts = await reencrypt_all(old_master, new_master)
    except Exception:
        keyring.restore_record(data_dir, old_record)
        keyring.remove_staging(data_dir)
        secretbox.clear_key_cache()
        log_event(
            logger,
            "密钥换装失败，密钥文件已回滚",
            level=logging.ERROR,
            exc_info=True,
        )
        raise
    keyring.remove_staging(data_dir)
    return counts


def migration_pending(data_dir) -> bool:
    """是否存在未收尾的换钥（崩溃遗留的过渡记录）。"""
    try:
        return keyring.read_staging(data_dir) is not None
    except keyring.KeyringError:
        return True


async def resume_pending_migration(data_dir, passphrase: str | None = None) -> dict:
    """启动时收尾被打断的换钥。

    判定归属：过渡记录存在 ⇒ 上次换钥没跑完。此时库内密文只可能是**旧**
    的（换钥事务是单事务，提交即全部新、回滚即全部旧），所以用过渡记录
    里的旧主密钥重跑一次换钥到当前密钥即可。重跑本身是幂等的：已是新密文
    的行会在解密阶段抛错并被跳过（`_migrate` 对非密文返回 None，
    对解不开的密文上抛——这里显式按单行失败容忍）。

    口令模式下旧密钥若也用当前口令封装，可直接续跑；解不出就保留记录
    等用户解锁后再续（不做任何破坏性动作）。
    """
    if not migration_pending(data_dir):
        return {"resumed": False, "reason": "无待收尾的换钥"}

    old_master = keyring.staging_master(data_dir, passphrase)
    if old_master is None:
        log_event(
            logger,
            "存在未完成的密钥换装，需解锁后才能继续",
            level=logging.WARNING,
        )
        return {"resumed": False, "reason": "过渡记录需要口令解锁"}

    try:
        new_master = keyring.resolve_dmk(data_dir)
    except (keyring.KeyringError, keyring.KeyLockedError):
        return {"resumed": False, "reason": "当前密钥不可用"}

    if old_master == new_master:
        # 密钥文件根本没被替换（崩溃在写文件之前）——直接清记录即可
        keyring.remove_staging(data_dir)
        log_event(logger, "上次密钥换装未生效，已清理过渡记录")
        return {"resumed": True, "counts": {}}

    counts = await _resume_reencrypt(data_dir, old_master, new_master)
    keyring.remove_staging(data_dir)
    secretbox.clear_key_cache()
    log_event(
        logger,
        "上次中断的密钥换装已续完",
        tag="成功",
        detail={"迁移条数": sum(counts.values())},
    )
    return {"resumed": True, "counts": counts}


async def _resume_reencrypt(data_dir, old_master: bytes, new_master: bytes) -> dict:
    """续跑换钥：逐行容忍「已经是新密文」的行。

    与 `reencrypt_all` 的差别只在错误处理——正常换钥要求全有或全无，
    续跑时部分行可能已被上次写成功，用旧密钥解它们必然失败，属预期。
    """
    from app.domains.account.models import SteamAccount
    from app.domains.account.service import _CREDENTIAL_PURPOSE
    try:
        from app.domains.proxies.credentials import PURPOSE as CRED_PURPOSE
    except ImportError:
        CRED_PURPOSE = None
    from app.domains.proxies.models import Proxy, ProxySubscription
    from app.domains.proxies.subscription_secret import PURPOSE as SUB_PURPOSE
    from app.domains.proxypool.models import SubscriptionSnapshot
    from app.domains.settings.models import AppSetting
    from app.domains.settings.service import _secret_purpose
    from sqlalchemy import select

    counts = {
        "accountCookies": 0, "settingSecrets": 0,
        "subscriptions": 0, "snapshots": 0, "proxyCredentials": 0,
    }

    def _try(token: str | None, purpose: str) -> str | None:
        if not token or not secretbox.is_encrypted(token):
            return None
        try:
            plain = secretbox.decrypt_with(token, purpose, old_master)
        except secretbox.SecretBoxError:
            return None  # 已迁移过：旧密钥解不开属预期
        return secretbox.encrypt_with(plain, purpose, new_master)

    async with write_session(WritePriority.INTERACTIVE, label="rekey-resume") as session:
        for row in (await session.execute(select(SteamAccount))).scalars().all():
            migrated = _try(row.cookies, _CREDENTIAL_PURPOSE)
            if migrated is not None:
                row.cookies = migrated
                counts["accountCookies"] += 1

        for row in (await session.execute(select(AppSetting))).scalars().all():
            value = row.value_json
            if isinstance(value, str) and secretbox.is_encrypted(value):
                purpose = _secret_purpose(row.key)
                migrated = _try(value, purpose)
                if migrated is not None:
                    row.value_json = migrated
                    counts["settingSecrets"] += 1

        for row in (await session.execute(select(ProxySubscription))).scalars().all():
            migrated = _try(row.url, SUB_PURPOSE)
            if migrated is not None:
                row.url = migrated
                counts["subscriptions"] += 1

        for row in (await session.execute(select(SubscriptionSnapshot))).scalars().all():
            migrated = _try(row.url, SUB_PURPOSE)
            if migrated is not None:
                row.url = migrated
                counts["snapshots"] += 1

        if CRED_PURPOSE:
            for row in (await session.execute(select(Proxy))).scalars().all():
                for field in ("username", "password"):
                    migrated = _try(getattr(row, field), CRED_PURPOSE)
                    if migrated is not None:
                        setattr(row, field, migrated)
                        counts["proxyCredentials"] += 1

        await session.commit()
    return counts
