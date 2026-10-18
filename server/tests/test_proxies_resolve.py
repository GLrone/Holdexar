"""proxies 域策略解析测试：proxy_first 分支（Clash 在跑 → 内核端口）+ resolve 端点语义。

不触真实网络/内核——monkeypatch ClashRuntime 状态；settings 键值层隔离到临时库。
"""
import sys
import socket
from pathlib import Path

import pytest
import yaml
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.settings import service as settings_service


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()
@pytest.mark.asyncio
async def test_resolve_proxy_first_clash_running(db, monkeypatch):
    """proxy_first + 内核在跑且入口在听 → 返回内核混合端口（登录窗代理优先的取数源）。"""
    await settings_service.set_value("proxy.strategy", "proxy_first")
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": True, "port": 7890, "configPath": "x", "controllerUrl": "y"},
    )
    monkeypatch.setattr(clash_manager.runtime, "entry_listening", lambda: True)
    url = await proxies_service.resolve_proxy_url()
    assert url == "http://127.0.0.1:7890"


@pytest.mark.asyncio
async def test_resolve_proxy_first_clash_entry_dead_skips_kernel(db, monkeypatch):
    """proxy_first + 内核进程在跑但混合端口没在听 → 不交假地址，落到后续通道。

    进程活着 ≠ 入口可用：订阅没写端口（解析出的默认值无人监听）或端口被占
    时，交出去的地址必然 ConnectError。"""
    await settings_service.set_value("proxy.strategy", "proxy_first")
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": True, "port": 7890, "configPath": "x", "controllerUrl": "y"},
    )
    monkeypatch.setattr(clash_manager.runtime, "entry_listening", lambda: False)

    async def _no_fallback() -> int | None:
        return None

    monkeypatch.setattr(proxies_service, "_local_fallback_port", _no_fallback)
    assert await proxies_service.resolve_proxy_url() is None


@pytest.mark.asyncio
async def test_resolve_proxy_first_nothing_falls_back_direct(db, monkeypatch):
    """proxy_first + 无内核无池 + 本地混合端口死 → None（直连兜底，不报错）。

    端口探活 mock 为死：真实机器上用户自启的 Clash 可能活着（会让
    本用例假失败），测试语义是「真的一无所有时直连」。
    """
    await settings_service.set_value("proxy.strategy", "proxy_first")
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": False, "port": None, "configPath": None, "controllerUrl": None},
    )
    async def _no_fallback() -> int | None:
        return None

    monkeypatch.setattr(proxies_service, "_local_fallback_port", _no_fallback)
    assert await proxies_service.resolve_proxy_url() is None


@pytest.mark.asyncio
async def test_resolve_proxy_first_local_clash_port_alive(db, monkeypatch):
    """proxy_first + 无自管内核无池 + 用户自启 Verge 兜底口在听 → 走 7897。

    兜底探测与 proxy.clash_port 无关：配置的端口是程序内核的监听口，不是
    Verge 的探测目标；兜底固定走生态默认口 7897。
    """
    await settings_service.set_value("proxy.strategy", "proxy_first")
    await settings_service.set_value("proxy.clash_port", 7890)
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": False, "port": None, "configPath": None, "controllerUrl": None},
    )
    async def _alive() -> int | None:
        return 7897

    monkeypatch.setattr(proxies_service, "_local_fallback_port", _alive)
    assert await proxies_service.resolve_proxy_url() == "http://127.0.0.1:7897"


@pytest.mark.asyncio
async def test_resolve_direct_only_without_system_proxy(db, monkeypatch):
    """direct_only 策略 → 无可用系统代理时恒直连。"""
    await settings_service.set_value("proxy.strategy", "direct_only")

    async def _none():
        return None

    monkeypatch.setattr(proxies_service, "resolve_system_proxy_url", _none)
    assert await proxies_service.resolve_proxy_url() is None


@pytest.mark.asyncio
async def test_resolve_direct_only_rides_system_proxy(db, monkeypatch):
    """direct_only 策略 → 探活通过的系统代理即本机出口（托管语义）。"""
    await settings_service.set_value("proxy.strategy", "direct_only")

    async def _alive():
        return "http://127.0.0.1:7897"

    monkeypatch.setattr(proxies_service, "resolve_system_proxy_url", _alive)
    assert await proxies_service.resolve_proxy_url() == "http://127.0.0.1:7897"


@pytest.mark.asyncio
async def test_resolve_direct_first_stays_raw_direct(db, monkeypatch):
    """direct_first（本机优先，失败才换代理）不走系统代理——恒 None，
    failover 由 resolve_failover_proxy_url 单独负责。"""
    await settings_service.set_value("proxy.strategy", "direct_first")

    async def _alive():
        return "http://127.0.0.1:7897"

    monkeypatch.setattr(proxies_service, "resolve_system_proxy_url", _alive)
    assert await proxies_service.resolve_proxy_url() is None


# ── 系统代理解析器 ──────────────────────────────────────────


def test_system_proxy_candidates_parse_and_filter(monkeypatch):
    """https 优先、无 scheme 补 http://、socks 形态剔除、重复去重。"""
    import urllib.request

    def _env_proxies():
        return {
            "https": "127.0.0.1:7897",
            "http": "http://127.0.0.1:7897",
            "socks": "socks5://127.0.0.1:1080",
        }

    monkeypatch.setattr(urllib.request, "getproxies", _env_proxies)
    assert proxies_service._system_proxy_candidates() == ["http://127.0.0.1:7897"]

    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    assert proxies_service._system_proxy_candidates() == []


@pytest.mark.asyncio
async def test_resolve_system_proxy_alive_and_dead(db, monkeypatch):
    """候选逐个探活，首个活端口胜出；全死 → None。"""
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "getproxies",
        lambda: {"https": "127.0.0.1:7897", "http": "127.0.0.1:7890"},
    )
    probed: list[int] = []

    async def _alive(host, port, timeout=1.0):
        probed.append(port)
        return port == 7890

    monkeypatch.setattr(proxies_service, "_tcp_alive", _alive)
    monkeypatch.setattr(proxies_service, "_system_proxy_cache", None)
    assert await proxies_service.resolve_system_proxy_url() == "http://127.0.0.1:7890"
    assert probed == [7897, 7890]

    monkeypatch.setattr(proxies_service, "_system_proxy_cache", None)

    async def _dead(host, port, timeout=1.0):
        return False

    monkeypatch.setattr(proxies_service, "_tcp_alive", _dead)
    assert await proxies_service.resolve_system_proxy_url() is None


@pytest.mark.asyncio
async def test_resolve_system_proxy_cached(db, monkeypatch):
    """30s 缓存：命中期内不重探，调用方高频轮询不反复探活。"""
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"https": "127.0.0.1:7897"}
    )
    calls: list[int] = []

    async def _alive(host, port, timeout=1.0):
        calls.append(port)
        return True

    monkeypatch.setattr(proxies_service, "_tcp_alive", _alive)
    monkeypatch.setattr(proxies_service, "_system_proxy_cache", None)
    first = await proxies_service.resolve_system_proxy_url()
    # 换掉候选源也不影响缓存命中
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    second = await proxies_service.resolve_system_proxy_url()
    assert first == second == "http://127.0.0.1:7897"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_removed_strategies_migrate_to_proxy_first(db):
    """下架策略迁移：pinned/clash 存量库值被 get_strategy 一次性迁移到
    proxy_first（含返回体不再带 pinnedId），pinned_id 设置键清账。"""
    for removed in ("pinned", "clash"):
        await settings_service.set_value("proxy.strategy", removed)
        await settings_service.set_value("proxy.pinned_id", 42)
        info = await proxies_service.get_strategy()
        assert info["strategy"] == "proxy_first"
        assert "pinnedId" not in info  # 字段随策略下架移除
        assert await settings_service.get_value("proxy.pinned_id") is None


@pytest.mark.asyncio
async def test_set_strategy_rejects_removed_strategy(db):
    """下架后 set_strategy 对 pinned/clash 报未知策略（前端已无入口，API 层兜底）。"""
    for removed in ("pinned", "clash"):
        with pytest.raises(ValueError, match="未知策略"):
            await proxies_service.set_strategy(removed)


# ─── 端口语义 ────────────────────────────────────────────


def test_pin_mixed_port_overrides_subscription_port():
    """钉端口：配置里写的端口不再决定监听口，工作端口说了算（幂等）。"""
    text = yaml.safe_dump(
        {"mixed-port": 17890, "proxies": [dict(name="a", type="http", server="10.0.0.1", port=1)]}
    )
    pinned = clash_manager.pin_mixed_port(text, 7890)
    assert clash_manager.parse_mixed_port(pinned) == 7890
    assert clash_manager.pin_mixed_port(pinned, 7890) == pinned


def test_pin_mixed_port_adds_missing_key():
    """订阅没写端口的旧缺陷（解析默认值无人监听）在此根除：钉端口补上键。"""
    text = yaml.safe_dump({"proxies": [dict(name="a", type="http", server="10.0.0.1", port=1)]})
    pinned = clash_manager.pin_mixed_port(text, 7890)
    assert clash_manager.parse_mixed_port(pinned) == 7890


def test_pin_mixed_port_unparseable_returns_raw():
    """不可解析的配置原样返回（校验与入口就绪检查会拦住起不来的形态）。"""
    raw = "not: [valid"
    assert clash_manager.pin_mixed_port(raw, 7890) == raw


def test_subscription_view_strips_mixed_port():
    """端口归程序管：订阅内容等价比较剥掉 mixed-port，端口漂移不触发重启。"""
    a = clash_manager._subscription_view(yaml.safe_dump({"mixed-port": 17890, "proxies": []}))
    b = clash_manager._subscription_view(yaml.safe_dump({"mixed-port": 7890, "proxies": []}))
    assert a == b


def test_fallback_ports_cover_external_proxy_default_only():
    assert clash_manager.LOCAL_MIXED_PORTS == (7897,)


@pytest.mark.asyncio
async def test_new_install_uses_auto_runtime_port(db, monkeypatch):
    values = {"proxy.strategy": "direct_only"}
    writes = {}

    async def get_value(key, default=None):
        return values.get(key, default)

    async def set_value(key, value):
        writes[key] = value
        values[key] = value

    monkeypatch.setattr("app.domains.settings.service.get_value", get_value)
    monkeypatch.setattr("app.domains.settings.service.set_value", set_value)
    monkeypatch.setattr(clash_manager.runtime, "status", lambda: {
        "running": False, "port": None, "entryReady": False,
    })

    result = await proxies_service.get_strategy()

    assert result["clashPortMode"] == "auto"
    assert result["clashPort"] is None
    assert writes["proxy.clash_port_mode"] == "auto"


@pytest.mark.asyncio
async def test_existing_saved_port_migrates_to_fixed_mode(db, monkeypatch):
    values = {"proxy.strategy": "direct_only", "proxy.clash_port": 7890}
    writes = {}

    async def get_value(key, default=None):
        return values.get(key, default)

    async def set_value(key, value):
        writes[key] = value
        values[key] = value

    monkeypatch.setattr("app.domains.settings.service.get_value", get_value)
    monkeypatch.setattr("app.domains.settings.service.set_value", set_value)
    monkeypatch.setattr(clash_manager.runtime, "status", lambda: {
        "running": False, "port": None, "entryReady": False,
    })

    result = await proxies_service.get_strategy()

    assert result["clashPortMode"] == "fixed"
    assert result["clashPort"] == 7890
    assert writes["proxy.clash_port_mode"] == "fixed"


@pytest.mark.asyncio
async def test_switch_to_auto_keeps_running_port_and_does_not_restart(db, monkeypatch):
    values = {"proxy.clash_port_mode": "fixed", "proxy.clash_port": 7891}
    writes = {}
    restarted = []

    async def get_value(key, default=None):
        return values.get(key, default)

    async def set_value(key, value):
        writes[key] = value
        values[key] = value

    async def restart(port):
        restarted.append(port)

    monkeypatch.setattr("app.domains.settings.service.get_value", get_value)
    monkeypatch.setattr("app.domains.settings.service.set_value", set_value)
    monkeypatch.setattr(proxies_service, "_restart_kernel_for_port", restart)

    await proxies_service.set_strategy(clash_port_mode="auto")

    assert values["proxy.clash_port_mode"] == "auto"
    assert values["proxy.clash_port"] == 7891
    assert not restarted


def test_dynamic_port_start_retries_when_listener_is_not_owned(tmp_path, monkeypatch):
    runtime = clash_manager.ClashRuntime()
    config = tmp_path / "runtime.yaml"
    config.write_text(yaml.safe_dump({"proxies": [{
        "name": "one", "type": "http", "server": "127.0.0.1", "port": 1,
    }]}), encoding="utf-8")
    ports = iter([43123, 43124])
    owners = iter([False, True, True])
    processes = []

    class Process:
        def __init__(self, *args, **kwargs):
            self.pid = 5000 + len(processes)
            self.returncode = None
            processes.append(self)

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(clash_manager, "_free_local_port", lambda: next(ports))
    monkeypatch.setattr(clash_manager, "_port_owned_by_process", lambda p, pid: next(owners))
    monkeypatch.setattr(clash_manager, "ensure_kernel_files", lambda _p: None)
    monkeypatch.setattr(clash_manager, "validate_config", lambda *_a: None)
    monkeypatch.setattr(runtime, "_kill_orphans", lambda *_a: 0)
    monkeypatch.setattr(runtime, "_wait_mixed_port", lambda: True)
    monkeypatch.setattr(clash_manager, "_port_listening", lambda _port: True)
    monkeypatch.setattr(clash_manager, "resolve_controller", lambda _text: (
        "http://127.0.0.1:45000", "secret",
    ))
    monkeypatch.setattr(clash_manager.subprocess, "Popen", Process)

    status = runtime.start(
        "mihomo", str(config), inject_lanes=False, allocate_mixed_port=True,
    )

    assert status["port"] == 43124
    assert status["entryReady"] is True
    assert len(processes) == 2


@pytest.mark.asyncio
async def test_set_strategy_port_change_restarts_kernel(db, monkeypatch):
    """工作端口改了就要用上：内核在跑时按新端口重启；同值不重启。"""
    called: list[int] = []

    async def fake_restart(port: int) -> None:
        called.append(port)

    values = {}

    async def get_value(key, default=None):
        return values.get(key, default)

    async def set_value(key, value):
        values[key] = value

    monkeypatch.setattr(proxies_service, "_restart_kernel_for_port", fake_restart)
    monkeypatch.setattr("app.domains.settings.service.get_value", get_value)
    monkeypatch.setattr("app.domains.settings.service.set_value", set_value)
    await proxies_service.set_strategy(clash_port=7891)
    assert called == [7891]
    called.clear()
    await proxies_service.set_strategy(clash_port=7891)
    assert called == []
