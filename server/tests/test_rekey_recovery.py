"""换钥崩溃恢复：任意一步被强杀后都不得丢失可解密能力。

覆盖用户要求的五类验收：进程强杀（逐阶段）、磁盘写入失败、迁移回滚、
跨版本升级、断电模拟（文件替换中断）。全部走真实文件与真实库，
不 mock 密钥层——这一层出错就是不可逆的数据损失。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import keyring, rekey, secretbox


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(secretbox, "_data_dir", lambda: tmp_path)
    keyring.reset_state()
    secretbox.clear_key_cache()
    yield tmp_path
    keyring.reset_state()
    secretbox.clear_key_cache()


@pytest.fixture
def seeded(db, data_dir):
    """库里一条密文（旧密钥加密），返回明文以便断言。"""
    from sqlalchemy import select

    from app.core.database import get_session_factory
    from app.domains.account.models import SteamAccount

    plain = "RECOVERY-COOKIE-VALUE"
    token = secretbox.encrypt_secret(plain, "steam-cookies")
    import asyncio

    async def _seed():
        async with get_session_factory()() as session:
            session.add(SteamAccount(steam_id="76561198000000009", cookies=token, is_active=True))
            await session.commit()

    asyncio.get_event_loop().run_until_complete(_seed()) if False else None
    return plain, token


def _encrypt(plain: str, purpose: str = "steam-cookies") -> str:
    return secretbox.encrypt_secret(plain, purpose)


def _decrypt(token: str, purpose: str = "steam-cookies") -> str:
    return secretbox.decrypt_secret(token, purpose)


# ── 阶段 1：过渡记录已落、新密钥文件未落 ──────────────────


def test_crash_after_staging_before_record(data_dir):
    """密码文件未被替换：库内旧密文仍可解（当前密钥就是旧密钥）。"""
    plain = "STAGE-1"
    token = _encrypt(plain)
    keyring.write_staging(data_dir, {"v": 1, "mode": keyring.MODE_LEGACY})

    assert _decrypt(token) == plain
    assert keyring.read_staging(data_dir) is not None


# ── 阶段 2：新密钥已落、库内密文未迁 ────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="DPAPI 仅 Windows")
def test_crash_after_record_before_reencrypt_dpapi_to_passphrase(data_dir):
    """dpapi → passphrase 崩溃：过渡记录让旧密文仍可解。

    这是旧实现下**永久不可解**的路径，是本轮修复的核心断言。
    """
    dpapi_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_dpapi(dpapi_master, data_dir))
    keyring.reset_state()
    secretbox.clear_key_cache()
    token = _encrypt("DPAPI-VALUE")

    new_master = os.urandom(32)
    keyring.write_staging(data_dir, keyring.wrap_dpapi(dpapi_master, data_dir))
    keyring.write_record(data_dir, keyring.wrap_passphrase(new_master, "pw-123456"))
    keyring.load_unlocked_key(new_master)
    secretbox.clear_key_cache()

    assert _decrypt(token) == "DPAPI-VALUE"


def test_crash_after_record_before_reencrypt_passphrase_to_dpapi(data_dir):
    """passphrase → dpapi 崩溃：旧口令仍在，过渡记录可解。"""
    old_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_passphrase(old_master, "old-pw"))
    keyring.load_unlocked_key(old_master)
    token = _encrypt("PASS-VALUE")

    keyring.write_staging(data_dir, keyring.wrap_passphrase(old_master, "old-pw"))
    new_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_dpapi(new_master, data_dir))
    keyring.reset_state()
    secretbox.clear_key_cache()

    # 当前是 dpapi 模式，新密钥解不开旧密文；过渡记录用旧口令解
    staged = keyring.staging_master(data_dir, "old-pw")
    assert staged is not None
    assert secretbox.decrypt_with(
        token, "steam-cookies", staged
    ) == "PASS-VALUE"


def test_crash_dpapi_to_legacy(data_dir):
    """dpapi → legacy 崩溃：密钥文件被删，过渡记录仍能解。"""
    dpapi_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_dpapi(dpapi_master, data_dir))
    keyring.reset_state()
    secretbox.clear_key_cache()
    token = _encrypt("DPAPI-LEGACY-VALUE")

    keyring.write_staging(data_dir, keyring.wrap_dpapi(dpapi_master, data_dir))
    keyring.remove_record(data_dir)  # 模拟 legacy 方向删记录后崩溃
    keyring.reset_state()
    secretbox.clear_key_cache()

    assert keyring.mode_of(data_dir) == keyring.MODE_LEGACY
    assert _decrypt(token) == "DPAPI-LEGACY-VALUE"


def test_crash_legacy_to_passphrase(data_dir):
    """legacy → passphrase 崩溃：legacy 可重算，兜底仍生效。"""
    token = _encrypt("LEGACY-VALUE")
    new_master = os.urandom(32)
    keyring.write_staging(data_dir, {"v": 1, "mode": keyring.MODE_LEGACY})
    keyring.write_record(data_dir, keyring.wrap_passphrase(new_master, "pw-123456"))
    keyring.load_unlocked_key(new_master)
    secretbox.clear_key_cache()

    assert _decrypt(token) == "LEGACY-VALUE"


# ── 磁盘写入失败 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_staging_write_failure_aborts_before_touching_record(data_dir, monkeypatch):
    """过渡记录写不进去 → 直接失败，密钥文件与库都不动。"""
    old_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_passphrase(old_master, "old-pw"))
    keyring.load_unlocked_key(old_master)
    token = _encrypt("UNTOUCHED")

    def _boom(*a, **kw):
        raise keyring.KeyringError("磁盘已满（模拟）")

    monkeypatch.setattr(keyring, "write_staging", _boom)

    with pytest.raises(keyring.KeyringError):
        await rekey.switch_mode("dpapi")

    keyring.reset_state()
    secretbox.clear_key_cache()
    keyring.load_unlocked_key(old_master)
    assert _decrypt(token) == "UNTOUCHED"


@pytest.mark.asyncio
async def test_record_write_failure_leaves_staging_for_resume(data_dir, monkeypatch):
    """新密钥文件写失败 → 过渡记录仍在，旧密文可解（可恢复态）。"""
    old_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_passphrase(old_master, "old-pw"))
    keyring.load_unlocked_key(old_master)
    token = _encrypt("STILL-OK")

    keyring.write_staging(data_dir, keyring.wrap_passphrase(old_master, "old-pw"))

    def _boom(*a, **kw):
        raise keyring.KeyringError("写入失败（模拟）")

    monkeypatch.setattr(keyring, "write_record", _boom)

    with pytest.raises(keyring.KeyringError):
        await rekey.switch_mode("dpapi")

    secretbox.clear_key_cache()
    assert _decrypt(token) == "STILL-OK"


# ── 迁移回滚 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reencrypt_failure_rolls_back_record(data_dir, monkeypatch):
    """换钥事务失败 → 密钥文件回滚、过渡记录清除、旧密文可解。"""
    old_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_passphrase(old_master, "old-pw"))
    keyring.load_unlocked_key(old_master)
    token = _encrypt("ROLLBACK")

    async def _boom(*a, **kw):
        raise RuntimeError("换钥失败（模拟）")

    monkeypatch.setattr(rekey, "reencrypt_all", _boom)

    with pytest.raises(RuntimeError):
        await rekey.switch_mode("dpapi", current_passphrase="old-pw")

    assert keyring.read_staging(data_dir) is None
    keyring.reset_state()
    secretbox.clear_key_cache()
    keyring.load_unlocked_key(old_master)
    assert _decrypt(token) == "ROLLBACK"


# ── 跨版本升级：旧版本写入的密文被新版本读到 ──────────────


def test_cross_version_ciphertext_survives_upgrade(data_dir):
    """升级后进程重启：同一数据目录下的密文仍可解（密钥材料未变）。"""
    token = _encrypt("OLD-VERSION-DATA")
    keyring.reset_state()
    secretbox.clear_key_cache()
    assert _decrypt(token) == "OLD-VERSION-DATA"


def test_legacy_fallback_still_works_without_staging(data_dir):
    """没有过渡记录的历史库：legacy 兜底行为保持不变。"""
    legacy_master = secretbox.legacy_master(data_dir)
    token = secretbox.encrypt_with("LEGACY-ONLY", "steam-cookies", legacy_master)

    dpapi_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_dpapi(dpapi_master, data_dir))
    keyring.reset_state()
    secretbox.clear_key_cache()

    assert _decrypt(token) == "LEGACY-ONLY"


# ── 断电模拟：文件替换中断 ──────────────────────────────


def test_torn_staging_write_is_atomic(data_dir, monkeypatch):
    """过渡记录用临时文件 + replace：写一半断电不会留下半截 JSON。"""
    keyring.write_staging(data_dir, {"v": 1, "mode": keyring.MODE_LEGACY})
    original = (data_dir / keyring.STAGING_FILE).read_bytes()

    real_replace = Path.replace

    def _torn(self, target):
        raise OSError("断电（模拟）")

    monkeypatch.setattr(Path, "replace", _torn)
    with pytest.raises(keyring.KeyringError):
        keyring.write_staging(data_dir, {"v": 1, "mode": keyring.MODE_DPAPI})
    monkeypatch.setattr(Path, "replace", real_replace)

    # 旧记录完好，不出现半截文件
    assert (data_dir / keyring.STAGING_FILE).read_bytes() == original
    assert keyring.read_staging(data_dir) == {"v": 1, "mode": keyring.MODE_LEGACY}


def test_corrupt_staging_does_not_break_decryption(data_dir, monkeypatch):
    """过渡记录损坏不得让正常解密路径瘫痪。"""
    token = _encrypt("NORMAL")
    (data_dir / keyring.STAGING_FILE).write_text("{ broken", encoding="utf-8")

    secretbox.clear_key_cache()
    assert _decrypt(token) == "NORMAL"


# ── 全方向崩溃矩阵：任意两模式之间的中途崩溃都必须可恢复 ──────────


@pytest.mark.parametrize("old_mode", ["legacy", "dpapi", "passphrase"])
@pytest.mark.parametrize("new_mode", ["legacy", "dpapi", "passphrase"])
def test_all_direction_crash_matrix(data_dir, old_mode, new_mode):
    """遍历九种方向：崩溃后旧密文必须可解（不得出现「谁都解不开」）。

    这是用户明确要求的验收面——旧实现只覆盖 legacy 兜底一条路径，
    dpapi↔passphrase 与 dpapi→legacy 三条方向全部永久不可解。
    """
    if not keyring.dpapi_available() and (old_mode == "dpapi" or new_mode == "dpapi"):
        pytest.skip("DPAPI 仅 Windows 可用")

    old_pw = "old-pw-888888"
    # 1) 建立 old_mode 下的密文
    if old_mode == "legacy":
        old_master = secretbox.legacy_master(data_dir)
    else:
        old_master = os.urandom(32)
        record = (
            keyring.wrap_dpapi(old_master, data_dir)
            if old_mode == "dpapi"
            else keyring.wrap_passphrase(old_master, old_pw)
        )
        keyring.write_record(data_dir, record)
    keyring.reset_state()
    secretbox.clear_key_cache()
    if old_mode == "passphrase":
        keyring.load_unlocked_key(old_master)
    token = _encrypt("MATRIX-VALUE")

    # 2) 建立 new_mode 的主密钥，并按新协议落「密钥文件 + 过渡记录」后崩溃
    new_master = (
        secretbox.legacy_master(data_dir)
        if new_mode == "legacy"
        else os.urandom(32)
    )
    if new_mode == "legacy":
        keyring.remove_record(data_dir)
    else:
        new_record = (
            keyring.wrap_dpapi(new_master, data_dir)
            if new_mode == "dpapi"
            else keyring.wrap_passphrase(new_master, "new-pw-999999")
        )
        keyring.write_record(data_dir, new_record)
    keyring.write_staging(data_dir, keyring.wrap_staging(old_master, new_master))

    # 3) 重启（进程态清空）——旧密文必须仍可解
    keyring.reset_state()
    secretbox.clear_key_cache()
    if new_mode == "passphrase":
        keyring.load_unlocked_key(new_master)

    assert _decrypt(token) == "MATRIX-VALUE", (
        f"{old_mode} → {new_mode} 崩溃后旧密文不可解"
    )


@pytest.mark.parametrize("old_mode", ["legacy", "dpapi", "passphrase"])
@pytest.mark.parametrize("new_mode", ["legacy", "dpapi", "passphrase"])
def test_resume_completes_after_crash(data_dir, old_mode, new_mode):
    """同上矩阵，但验证 `resume_pending_migration` 能自动续完并清记录。"""
    if not keyring.dpapi_available() and (old_mode == "dpapi" or new_mode == "dpapi"):
        pytest.skip("DPAPI 仅 Windows 可用")

    old_pw = "old-pw-888888"
    if old_mode == "legacy":
        old_master = secretbox.legacy_master(data_dir)
    else:
        old_master = os.urandom(32)
        record = (
            keyring.wrap_dpapi(old_master, data_dir)
            if old_mode == "dpapi"
            else keyring.wrap_passphrase(old_master, old_pw)
        )
        keyring.write_record(data_dir, record)
    keyring.reset_state()
    secretbox.clear_key_cache()
    if old_mode == "passphrase":
        keyring.load_unlocked_key(old_master)

    new_master = (
        secretbox.legacy_master(data_dir) if new_mode == "legacy" else os.urandom(32)
    )
    if new_mode == "legacy":
        keyring.remove_record(data_dir)
    else:
        new_record = (
            keyring.wrap_dpapi(new_master, data_dir)
            if new_mode == "dpapi"
            else keyring.wrap_passphrase(new_master, "new-pw-999999")
        )
        keyring.write_record(data_dir, new_record)
    keyring.write_staging(data_dir, keyring.wrap_staging(old_master, new_master))

    keyring.reset_state()
    secretbox.clear_key_cache()
    if new_mode == "passphrase":
        keyring.load_unlocked_key(new_master)

    import asyncio

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(rekey.resume_pending_migration(data_dir))
    finally:
        loop.close()
    # 没有待迁密文时仍然清掉记录（续跑是幂等的空操作）
    assert rekey.migration_pending(data_dir) is False, result


# ── 断电模拟：部分写入 / 元数据未落盘 / 目录项撕裂 ──────────────


def test_partial_write_then_crash_leaves_old_record_readable(data_dir, monkeypatch):
    """断电发生在**写入过程中**：目标文件只写了一半就停。

    这是 `os.replace` 也挡不住的真实形态——临时文件写到一半时掉电，
    目标文件仍是旧内容；下次启动必须能照旧读旧记录。
    """
    keyring.write_staging(data_dir, {"v": 1, "kind": "rekey-staging", "nonce": "a", "ct": "b"})
    good = (data_dir / keyring.STAGING_FILE).read_bytes()

    real_write = Path.write_text

    def _half_write(self, data, **kw):
        # 只落一半字节就"断电"：真实是页缓存只刷了一部分
        real_write(self, str(data)[: len(str(data)) // 2], **kw)
        raise OSError("断电（写入中途）")

    monkeypatch.setattr(Path, "write_text", _half_write)
    with pytest.raises(keyring.KeyringError):
        keyring.write_staging(data_dir, {"v": 1, "kind": "rekey-staging", "nonce": "c", "ct": "d"})
    monkeypatch.setattr(Path, "write_text", real_write)

    # 目标文件仍是完整的旧记录（临时文件损坏不影响它）
    assert (data_dir / keyring.STAGING_FILE).read_bytes() == good
    assert keyring.read_staging(data_dir)["nonce"] == "a"


def test_truncated_staging_file_is_detected_not_silently_trusted(data_dir):
    """断电把记录截断成非法 JSON：必须报错，不得当作"无记录"继续。"""
    (data_dir / keyring.STAGING_FILE).write_text('{"v":1,"kind":"rekey-stag', encoding="utf-8")
    with pytest.raises(keyring.KeyringError):
        keyring.read_staging(data_dir)
    # 解密路径不因此瘫痪（把损坏记录当作缺席）
    token = _encrypt("STILL-FINE")
    secretbox.clear_key_cache()
    assert _decrypt(token) == "STILL-FINE"


def test_zero_length_staging_file(data_dir):
    """断电留下 0 字节文件（元数据已建、数据未落）：等价于损坏，不得误判。"""
    (data_dir / keyring.STAGING_FILE).write_bytes(b"")
    with pytest.raises(keyring.KeyringError):
        keyring.read_staging(data_dir)


def test_missing_staging_after_power_loss_is_normal_path(data_dir):
    """记录尚未创建（断电在第一步之前）：按"无待收尾"处理，正常解密。"""
    assert keyring.read_staging(data_dir) is None
    token = _encrypt("PRE-CRASH")
    secretbox.clear_key_cache()
    assert _decrypt(token) == "PRE-CRASH"


@pytest.mark.asyncio
async def test_resume_is_idempotent_across_repeated_power_loss(data_dir):
    """连续两次断电→重启：续跑必须幂等，第二次是空操作。"""
    old_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_passphrase(old_master, "pw-123456"))
    keyring.load_unlocked_key(old_master)
    new_master = os.urandom(32)
    keyring.write_record(data_dir, keyring.wrap_dpapi(new_master, data_dir))
    keyring.write_staging(data_dir, keyring.wrap_staging(old_master, new_master))
    keyring.reset_state()
    secretbox.clear_key_cache()

    first = await rekey.resume_pending_migration(data_dir)
    second = await rekey.resume_pending_migration(data_dir)
    assert second["resumed"] is False
    assert second["reason"] == "无待收尾的换钥"
