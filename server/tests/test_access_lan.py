"""局域网访问模式：Host 闸扩展、解锁通道、限速与默认模式回归。

默认模式必须与历史行为逐点一致（回环-only、非回环 421、解锁 404）；
局域网模式开启后仅多放行本机地址，DNS 重绑定防线不放宽。
"""
from __future__ import annotations

import asyncio
import secrets

from app.core import access, lan_access
from app.domains.system.session_router import (
    resolve_lan_password_verifier,
)
from lan_test_support import (
    LAN_HOST as _LAN_HOST,
    LAN_IP,
    LAN_PEER_IP,
    LOOPBACK_IP,
    access_app,
    enable_lan,
    isolate_lan_state,
    peer_client,
)

# 测试夹具密码：运行时生成，与真实凭据无关
_TEST_PASSWORD = "pw-" + secrets.token_hex(8)
_WRONG_PASSWORD = "wrong-" + secrets.token_hex(8)
_ENV_PASSWORD = "env-" + secrets.token_hex(8)


def _enable_lan(monkeypatch, *, password: str | None = _TEST_PASSWORD):
    """开启局域网模式并注入固定验证器（DB 无关路径；解锁走真实 PHC 校验）。"""
    enable_lan()
    if password is not None:
        phc = lan_access.hash_password(password)
        monkeypatch.setattr(
            "app.domains.system.session_router.resolve_lan_password_verifier",
            lambda: _async_of(phc),
        )


async def _async_of(value: str) -> str:
    return value


def test_default_mode_lan_host_still_rejected():
    """默认模式回归：非回环 Host 一律 421（历史行为不变）。"""
    access.session_token()
    app = access_app()
    async def run():
        async with peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST) as client:
            return await client.get("/api/v1/health")
    assert asyncio.run(run()).status_code == 421


def test_default_mode_rejects_non_loopback_peer():
    """P0 回归：默认模式（未开局域网）下局域网对端伪造 Host: 127.0.0.1
    也必须被对端判定拦截——Host 是客户端可控字段，不是信任事实源。"""
    access.session_token()
    app = access_app()
    client = peer_client(app, peer_ip=LAN_PEER_IP, host="127.0.0.1:28765")
    async def run():
        async with client:
            return await client.get(
                "/api/v1/health", headers={"Host": "127.0.0.1:28765"}
            )
    assert asyncio.run(run()).status_code == 421


def test_dev_channel_peer_gated(monkeypatch):
    """P0 回归：dev 会话通道按对端地址判定——局域网对端（伪造回环 Host）
    404，回环对端 200（Vite 调试工作流不受影响）。"""
    _enable_lan(monkeypatch)
    app = access_app()
    async def run(client):
        async with client:
            return await client.get("/api/v1/session/dev")
    lan_client = peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST)
    local_client = peer_client(app, peer_ip=LOOPBACK_IP, host="127.0.0.1:28765")
    assert asyncio.run(run(lan_client)).status_code == 404
    assert asyncio.run(run(local_client)).status_code == 200


def test_default_mode_unlock_disabled():
    """默认模式：解锁入口不存在（发行版无自助通道语义保持）。"""
    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LOOPBACK_IP, host="127.0.0.1:28765") as client:
            return await client.post(
                "/api/v1/session/unlock", json={"password": _WRONG_PASSWORD}
            )
    assert asyncio.run(run()).status_code == 404


def test_lan_state_never_echoes_password(monkeypatch):
    """P0-1 回归：局域网访问密码原文永不进入设置响应（PHC 哈希 KV 同样
    只暴露 has_password 布尔）。"""
    from app.domains.settings import router as settings_router
    from app.domains.settings import service as settings_service
    from app.domains.settings.router import LanAccessState

    phc = lan_access.hash_password(_TEST_PASSWORD)

    async def fake_get_value(key, default=None):
        return phc if key == settings_service.LAN_PASSWORD_HASH_KEY else default

    async def fake_get_secret(key, default=None):
        return default

    monkeypatch.setattr("app.domains.settings.service.get_value", fake_get_value)
    monkeypatch.setattr(
        "app.domains.settings.service.get_secret_value", fake_get_secret
    )
    state = asyncio.run(settings_router._lan_state())
    assert state.has_password is True
    # 模型级回归：响应模型不存在明文密码字段（防止后续把回显加回来）
    assert "password" not in LanAccessState.model_fields


def test_lan_mode_allows_local_ip_host(monkeypatch):
    """局域网模式：本机网卡地址过 Host 闸，落到会话闸（401 而非 421）。"""
    _enable_lan(monkeypatch)
    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST) as client:
            return await client.get("/api/v1/system/security")
    assert asyncio.run(run()).status_code == 401


def test_lan_mode_still_blocks_rebinding_domain(monkeypatch):
    """局域网模式不放宽重绑定防线：攻击者域名 Host 仍 421。"""
    _enable_lan(monkeypatch)
    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LAN_PEER_IP, host="evil.example:28765") as client:
            return await client.get("/api/v1/health")
    assert asyncio.run(run()).status_code == 421


def test_lan_mode_same_origin_lan_request_passes_gates(monkeypatch):
    """局域网直连形态：Host 与 Origin 同为本机地址 → 只差会话（401）。"""
    _enable_lan(monkeypatch)
    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST) as client:
            return await client.get(
                "/api/v1/system/security",
                headers={"Origin": f"http://{_LAN_HOST}"},
            )
    assert asyncio.run(run()).status_code == 401


def test_unlock_flow_wrong_password_then_limit(monkeypatch):
    """解锁：错密码 401 计数，连续失败达阈值后 429；正确密码放行并清零。"""
    _enable_lan(monkeypatch)
    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST) as client:
            codes = []
            for _ in range(5):
                r = await client.post(
                    "/api/v1/session/unlock", json={"password": _WRONG_PASSWORD}
                )
                codes.append(r.status_code)
            limited = await client.post(
                "/api/v1/session/unlock", json={"password": _TEST_PASSWORD}
            )
            lan_access.reset_unlock_state()
            ok = await client.post(
                "/api/v1/session/unlock", json={"password": _TEST_PASSWORD}
            )
            cookie = client.cookies.get(access.SESSION_COOKIE, "")
            protected = await client.get(
                "/api/v1/system/security",
                headers={access.SESSION_HEADER: cookie},
            )
            return codes, limited.status_code, ok.status_code, protected.status_code
    codes, limited, ok, protected = asyncio.run(run())
    assert codes == [401, 401, 401, 401, 401]
    assert limited == 429
    assert ok == 200
    assert protected == 200


def test_unlock_rate_limit_isolated_between_peers(monkeypatch):
    """错密码锁住攻击端后，另一台局域网设备仍可用正确密码解锁。"""
    _enable_lan(monkeypatch)
    app = access_app()

    async def run():
        attacker = peer_client(app, peer_ip="192.168.1.10", host=_LAN_HOST)
        victim = peer_client(app, peer_ip="192.168.1.11", host=_LAN_HOST)
        async with attacker, victim:
            failed = []
            for _ in range(5):
                response = await attacker.post(
                    "/api/v1/session/unlock", json={"password": _WRONG_PASSWORD}
                )
                failed.append(response.status_code)
            attacker_limited = await attacker.post(
                "/api/v1/session/unlock", json={"password": _TEST_PASSWORD}
            )
            victim_unlocked = await victim.post(
                "/api/v1/session/unlock", json={"password": _TEST_PASSWORD}
            )
            return failed, attacker_limited.status_code, victim_unlocked.status_code

    failed, attacker_limited, victim_unlocked = asyncio.run(run())
    assert failed == [401, 401, 401, 401, 401]
    assert attacker_limited == 429
    assert victim_unlocked == 200


def test_unlock_lockout_escalation():
    """P2 回归：锁窗过期后再穷举，锁时按连续轮次加倍（30s→120s，帽 600s）。"""
    for _ in range(5):
        lan_access.unlock_record_failure()
    first = lan_access.unlock_locked_seconds()
    assert 29 <= first <= 31  # 首轮 30s
    for _ in range(5):
        lan_access.unlock_record_failure()  # 锁窗内继续失败也计数
    second = lan_access.unlock_locked_seconds()
    assert second > first  # 升级轮：约 120s
    lan_access.unlock_record_success()
    assert lan_access.unlock_locked_seconds() == 0


def test_unlock_non_ascii_password_roundtrip(monkeypatch):
    """P2 回归：非 ASCII 口令的设置与解锁不再触发 TypeError 500——
    比较统一 bytes 化；错误猜测 401 且计入限速（不再是限速失效双杀）。"""
    _enable_lan(monkeypatch, password="口令测试-八字符")
    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST) as client:
            wrong = await client.post(
                "/api/v1/session/unlock", json={"password": "错密码-不是对的"}
            )
            wrong_code = wrong.status_code
            await client.post("/api/v1/session/unlock", json={"password": "错密码-不是对的"})
            lan_access.reset_unlock_state()
            ok = await client.post(
                "/api/v1/session/unlock", json={"password": "口令测试-八字符"}
            )
            return wrong_code, ok.status_code
    wrong_code, ok_code = asyncio.run(run())
    assert wrong_code == 401, "非 ASCII 错误口令应 401（修复前 TypeError→500）"
    assert ok_code == 200


def test_unlock_env_password_channel(monkeypatch):
    """无窗口通道：环境变量指定的密码直接生效。"""
    lan_access.set_enabled(True)
    enable_lan()
    monkeypatch.setenv(lan_access.ENV_ACCESS_PASSWORD, _ENV_PASSWORD)
    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST) as client:
            return await client.post(
                "/api/v1/session/unlock", json={"password": _ENV_PASSWORD}
            )
    assert asyncio.run(run()).status_code == 200


def test_refresh_extra_hosts_follows_toggle(monkeypatch):
    """开关与白名单联动：开＝本机地址集，关＝回环-only。"""
    monkeypatch.setattr(
        lan_access, "local_ips", lambda: ["127.0.0.1", "192.168.1.5"]
    )
    lan_access.set_enabled(True)
    lan_access.refresh_extra_hosts()
    assert access.host_allowed(_LAN_HOST)
    lan_access.set_enabled(False)
    lan_access.refresh_extra_hosts()
    assert not access.host_allowed(_LAN_HOST)
    assert access.host_allowed("127.0.0.1:28765")


def test_host_helpers_normalize_host_header():
    """Host 归一：端口剥离、IPv6 括号、大小写；额外地址不波及回环判定。"""
    access.set_extra_hosts(["192.168.1.5", "FE80::1"])
    assert access.host_allowed("192.168.1.5:28765")
    assert access.host_allowed("192.168.1.5")
    assert access.host_allowed("[fe80::1]:28765")
    assert access.host_allowed("LOCALHOST:9999")
    assert access.is_loopback_host("[::1]:28765")
    assert not access.is_loopback_host("192.168.1.5:28765")
    assert not access.host_allowed("")
    access.set_extra_hosts(())
    assert not access.host_allowed("192.168.1.5:28765")


# ── PHC 验证器与解析链（P0-1 终态：访问密码不可逆存储）────────────────────


def _patch_resolver_store(monkeypatch) -> dict[str, str]:
    """把 settings 服务的 KV 读写替换为进程内字典（DB 无关），返回字典。"""
    from app.domains.settings import service as settings_service

    stored: dict[str, str] = {}

    async def fake_get_value(key, default=None):
        return stored.get(key, default)

    async def fake_get_secret(key, default=None):
        return stored.get(key, default)

    async def fake_set_value(key, value):
        stored[key] = value

    async def fake_delete_value(key):
        stored.pop(key, None)

    monkeypatch.setattr(settings_service, "get_value", fake_get_value)
    monkeypatch.setattr(settings_service, "get_secret_value", fake_get_secret)
    monkeypatch.setattr(settings_service, "set_value", fake_set_value)
    monkeypatch.setattr(settings_service, "delete_value", fake_delete_value)
    return stored


def test_phc_hash_verify_roundtrip():
    """PHC 验证器：正确口令通过、错误口令拒绝、参数自包含于串内。"""
    phc = lan_access.hash_password("口令-测试-12345678")
    assert phc.startswith("$scrypt$")
    assert lan_access.verify_password("口令-测试-12345678", phc)
    assert not lan_access.verify_password("口令-测试-12345679", phc)
    assert not lan_access.verify_password("", phc)


def test_phc_salt_unique_and_params_in_bounds():
    """同口令两次哈希盐不同（防彩虹表）；参数内嵌且落在边界白名单内。"""
    a = lan_access.hash_password("same-password-8")
    b = lan_access.hash_password("same-password-8")
    assert a != b
    for phc in (a, b):
        parts = phc.split("$")
        assert parts[1] == "scrypt"
        n, r, p = (int(x) for x in parts[2].split(","))
        assert n % 2 == 0 and 1 << 12 <= n <= 1 << 22
        assert 1 <= r <= 64 and 1 <= p <= 8


def test_phc_verify_rejects_tampered_verifier():
    """篡改验证器（N 抬出边界 / 坏格式 / 短哈希）一律失败，不触发大内存重算。"""
    phc = lan_access.hash_password("tamper-check-12345")
    assert not lan_access.verify_password("tamper-check-12345", "not-a-phc")
    assert not lan_access.verify_password(
        "tamper-check-12345", "$scrypt$1,1,1$AAAA$AAAA"
    )
    evil = phc.replace(str(1 << 15), str(1 << 30), 1)  # N 越界
    assert not lan_access.verify_password("tamper-check-12345", evil)


def test_resolve_verifier_prefers_hash_kv(monkeypatch):
    """验证器解析：哈希 KV 存在时直接采用，不读旧密文。"""
    monkeypatch.delenv(lan_access.ENV_ACCESS_PASSWORD, raising=False)
    stored = _patch_resolver_store(monkeypatch)
    phc = lan_access.hash_password(_TEST_PASSWORD)
    stored["access.lan_password_hash"] = phc

    async def run():
        return await resolve_lan_password_verifier()

    assert asyncio.run(run()) == phc


def test_resolve_verifier_migrates_legacy_secret(monkeypatch):
    """一次性迁移：旧可逆密文解密→重哈希落库→清密文；迁移后验证器校验
    旧口令通过。"""
    monkeypatch.delenv(lan_access.ENV_ACCESS_PASSWORD, raising=False)
    stored = _patch_resolver_store(monkeypatch)
    stored["access.lan_password"] = "legacy-pw-123456"  # 模拟已解密明文

    async def run():
        return await resolve_lan_password_verifier()

    verifier = asyncio.run(run())
    assert verifier.startswith("$scrypt$")
    assert lan_access.verify_password("legacy-pw-123456", verifier)
    assert "access.lan_password" not in stored  # 明文事实已清账
    assert stored["access.lan_password_hash"] == verifier


def test_resolve_verifier_user_hash_wins(monkeypatch):
    """用户设置口令恒优先于环境口令（界面改密即吊销运维通道，不静默遮蔽）。"""
    monkeypatch.setenv(lan_access.ENV_ACCESS_PASSWORD, _ENV_PASSWORD)
    stored = _patch_resolver_store(monkeypatch)
    stored["access.lan_password_hash"] = lan_access.hash_password(_TEST_PASSWORD)

    async def run():
        return await resolve_lan_password_verifier()

    verifier = asyncio.run(run())
    assert lan_access.verify_password(_TEST_PASSWORD, verifier)
    assert not lan_access.verify_password(_ENV_PASSWORD, verifier)


def test_resolve_verifier_env_fallback(monkeypatch):
    """库内无用户口令时环境口令兜底（无窗口运维场景保持可用）。"""
    monkeypatch.setenv(lan_access.ENV_ACCESS_PASSWORD, _ENV_PASSWORD)
    _patch_resolver_store(monkeypatch)

    async def run():
        return await resolve_lan_password_verifier()

    verifier = asyncio.run(run())
    assert lan_access.verify_password(_ENV_PASSWORD, verifier)


def test_resolve_verifier_empty_when_unset(monkeypatch):
    """库内无哈希无旧密文：返回空串（解锁入口据此明确拒绝）。"""
    monkeypatch.delenv(lan_access.ENV_ACCESS_PASSWORD, raising=False)
    _patch_resolver_store(monkeypatch)

    async def run():
        return await resolve_lan_password_verifier()

    assert asyncio.run(run()) == ""


def test_unlock_without_password_rejected(monkeypatch):
    """未设访问密码：解锁 401 且计入限速，不产生会话。"""
    _enable_lan(monkeypatch, password=None)
    monkeypatch.setattr(
        "app.domains.system.session_router.resolve_lan_password_verifier",
        lambda: _async_of(""),
    )

    async def run():
        app = access_app()
        async with peer_client(app, peer_ip=LAN_PEER_IP, host=_LAN_HOST) as client:
            r = await client.post(
                "/api/v1/session/unlock", json={"password": _WRONG_PASSWORD}
            )
            return r.status_code

    assert asyncio.run(run()) == 401
