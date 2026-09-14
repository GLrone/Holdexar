"""Clash 订阅定时重拉测试：间隔门槛 / 「在跑哪条」判定 / 失败留待重试 /
重启判定去噪 / 调度接线。

不触真实网络与内核——download_subscription、runtime.status 全打桩；库隔离
到临时 sqlite（同 test_proxies_subscriptions.py 范式）。

范围说明：内核重启的进程操作（subprocess）不在本文件内真跑——只验证接线与
判定，重启分支沿用既有手动链路的行为。
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import scheduler as sched_mod
from app.core.database import Base
from app.crawler.utils import get_beijing_time_obj
from app.domains.proxies import clash_manager, service as proxies_service
from app.domains.proxies.models import ProxySubscription
from app.domains.settings import service as settings_service

SUB_URL = "https://example.com/sub?token=abc"
USERINFO = "upload=100; download=300; total=500; expire=2524608000"
GATE_KEY = "proxy.sub_last_refresh_at"


def _config_text(*, url: str | None = SUB_URL) -> str:
    """内核配置文件文本：订阅 URL 以内联值形式出现（机场面板常见形态）。"""
    hint = f"sub-url: {url}\n" if url else ""
    return f"mixed-port: 7890\n{hint}proxies:\n  - name: A\n  - name: B\n"


@pytest.fixture
def db(tmp_path, monkeypatch):
    import app.core.database as database_module

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(proxies_service, "get_session_factory", lambda: factory)
    monkeypatch.setattr(settings_service, "get_session_factory", lambda: factory)
    return factory


@pytest_asyncio.fixture(autouse=True)
async def _schema(db):
    import app.domains.proxies.models  # noqa: F401
    import app.domains.settings.models  # noqa: F401

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _seed_sub(db, *, url: str = SUB_URL, deprecated: bool = False) -> int:
    async with db() as session:
        sub = ProxySubscription(kind="clash", url=url, deprecated=deprecated)
        session.add(sub)
        await session.commit()
        return sub.id


def _stub_runtime(monkeypatch, tmp_path, *, startup_text: str | None) -> Path:
    """内核状态桩：磁盘配置 + 启动文本（running_subscription_is 的事实源）。"""
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_config_text(), encoding="utf-8")
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {
            "running": True, "port": 7890, "configPath": str(cfg),
            "controllerUrl": "http://127.0.0.1:19090",
        },
    )
    monkeypatch.setattr(clash_manager.runtime, "_startup_text", startup_text)
    # 内核重启分支要真起进程，测试里不碰——统一按「内核目录不可用」跳过
    monkeypatch.setattr(clash_manager, "detect_kernel", lambda d: {"found": False})
    return cfg


def _stub_download(monkeypatch, cfg: Path, calls: list) -> None:
    async def _fake_download(url, data_dir, proxy_url=None):
        calls.append(url)
        cfg.write_text(_config_text(), encoding="utf-8")
        return {
            "path": str(cfg), "title": None, "userinfo": USERINFO,
            "nodes": 2, "cached": False,
        }

    monkeypatch.setattr(clash_manager.runtime, "download_subscription", _fake_download)


async def _refresh_state() -> str:
    """只取状态字（订阅 id/节点数/重启标记另有断言处再取整体）。"""
    return (await proxies_service.maybe_refresh_active_clash_subscription())["state"]


# ─── 重启判定去噪（配置等价比较）────────────────────────────


def test_config_unchanged_ignores_controller_injection():
    """磁盘是「原始下载文本」、启动文本是「原始 + 注入 controller」——
    等价判定必须看穿注入，否则每次重拉都白重启一次内核。"""
    raw = _config_text()
    injected, _url, _secret = clash_manager.ensure_controller(raw)
    assert injected != raw  # 注入确实发生（前置条件）

    assert clash_manager.config_unchanged(injected, raw) is True
    assert clash_manager.config_unchanged(injected, injected) is True
    assert clash_manager.config_unchanged(injected, raw + "  - name: C\n") is False
    assert clash_manager.config_unchanged(None, raw) is False


# ─── 间隔门槛 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_throttled_inside_interval(db, monkeypatch, tmp_path):
    """门槛内（刚拉过）不重拉：连下载都不发起。"""
    await _seed_sub(db)
    calls: list = []
    cfg = _stub_runtime(monkeypatch, tmp_path, startup_text=_config_text())
    _stub_download(monkeypatch, cfg, calls)

    await settings_service.set_value(
        GATE_KEY, (get_beijing_time_obj().replace(tzinfo=None)).isoformat()
    )

    assert await _refresh_state() == "throttled"
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_runs_after_interval(db, monkeypatch, tmp_path):
    """门槛过期（>6h）→ 真重拉：配置下载、流水留痕、门槛消费。"""
    sub_id = await _seed_sub(db)
    calls: list = []
    cfg = _stub_runtime(monkeypatch, tmp_path, startup_text=_config_text())
    _stub_download(monkeypatch, cfg, calls)

    stale = get_beijing_time_obj().replace(tzinfo=None) - timedelta(hours=7)
    await settings_service.set_value(GATE_KEY, stale.isoformat())

    out = await proxies_service.maybe_refresh_active_clash_subscription()
    assert out["state"] == "refreshed"
    assert out["subscriptionId"] == sub_id
    assert out["nodes"] == 2
    assert out["restarted"] is False  # 测试不打桩内核进程，重启分支不入
    assert calls == [SUB_URL]

    async with db() as session:
        sub = await session.get(ProxySubscription, sub_id)
        assert sub.last_imported_at is not None
        assert sub.last_stats["traffic"] == USERINFO
        assert sub.last_stats["nodes"] == 2

    # 门槛已消费：紧接着再跑一次不再拉
    assert await _refresh_state() == "throttled"
    assert calls == [SUB_URL]


# ─── 「在跑哪条」判定 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_skips_when_kernel_not_running(db, monkeypatch, tmp_path):
    """内核没跑：不拉（下次启动内核本来就会重下），也不写门槛。"""
    await _seed_sub(db)
    calls: list = []
    cfg = tmp_path / "config.yaml"
    monkeypatch.setattr(
        clash_manager.runtime,
        "status",
        lambda: {"running": False, "port": None, "configPath": None, "controllerUrl": None},
    )
    _stub_download(monkeypatch, cfg, calls)

    assert await _refresh_state() == "clash_not_running"
    assert calls == []
    assert await settings_service.get_value(GATE_KEY) is None


@pytest.mark.asyncio
async def test_refresh_skips_unknown_subscription(db, monkeypatch, tmp_path):
    """认不出内核在跑哪条（启动文本里查不到任何订阅 URL）→ 不猜、不动。"""
    await _seed_sub(db)
    calls: list = []
    cfg = _stub_runtime(monkeypatch, tmp_path, startup_text="proxies:\n  - name: A\n")
    _stub_download(monkeypatch, cfg, calls)

    assert await _refresh_state() == "unknown_subscription"
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_skips_when_all_deprecated(db, monkeypatch, tmp_path):
    """订阅全被废弃（后端已不选用）：不拉，也不消费门槛。"""
    await _seed_sub(db, deprecated=True)
    calls: list = []
    cfg = _stub_runtime(monkeypatch, tmp_path, startup_text=_config_text())
    _stub_download(monkeypatch, cfg, calls)

    assert await _refresh_state() == "no_subscription"
    assert calls == []


@pytest.mark.asyncio
async def test_refresh_picks_subscription_actually_running(db, monkeypatch, tmp_path):
    """多订阅并存：拉的是启动文本里那条，不是列表最后一条。"""
    running_id = await _seed_sub(db, url=SUB_URL)
    idle_id = await _seed_sub(db, url="https://example.com/other")

    calls: list = []
    cfg = _stub_runtime(monkeypatch, tmp_path, startup_text=_config_text())
    _stub_download(monkeypatch, cfg, calls)

    assert await _refresh_state() == "refreshed"
    assert calls == [SUB_URL]

    async with db() as session:
        running = await session.get(ProxySubscription, running_id)
        assert running.last_imported_at is not None
        untouched = await session.get(ProxySubscription, idle_id)
        assert untouched.last_imported_at is None  # 没在跑的那条不动


# ─── 失败留待重试 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_failure_retries_next_tick(db, monkeypatch, tmp_path):
    """拉取失败：门槛不消费（下一拍继续试），错误不冒泡到调度。"""
    await _seed_sub(db)
    cfg = _stub_runtime(monkeypatch, tmp_path, startup_text=_config_text())
    attempts: list = []

    async def _failing(url, data_dir, proxy_url=None):
        attempts.append(url)
        raise ValueError("订阅下载失败: 全部通道不可用")

    monkeypatch.setattr(clash_manager.runtime, "download_subscription", _failing)

    assert await _refresh_state() == "failed"
    assert await settings_service.get_value(GATE_KEY) is None
    assert await _refresh_state() == "failed"
    assert len(attempts) == 2  # 门槛没消费，第二拍照试


# ─── 调度接线 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_job_skips_when_crawler_busy(monkeypatch):
    """爬虫占线让路：不调 service（重启内核会切断在跑连接）。"""
    calls: list = []

    async def _fake():
        calls.append("refresh")
        return {"state": "refreshed", "subscriptionId": 1, "restarted": True}

    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: False)
    monkeypatch.setattr(proxies_service, "maybe_refresh_active_clash_subscription", _fake)

    await sched_mod._job_subscription_refresh()
    assert calls == []


@pytest.mark.asyncio
async def test_job_first_checks_after_restart(monkeypatch):
    """新配置已生效（restarted）→ 接一次首检，新节点当场进账本。"""
    calls: list = []

    async def _fake():
        calls.append("refresh")
        return {"state": "refreshed", "subscriptionId": 7, "nodes": 3, "restarted": True}

    async def _fake_check(sub_id):
        calls.append(("check", sub_id))
        return {"total": 3, "alive": 2}

    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: True)
    monkeypatch.setattr(proxies_service, "maybe_refresh_active_clash_subscription", _fake)
    monkeypatch.setattr(proxies_service, "test_clash_nodes", _fake_check)

    await sched_mod._job_subscription_refresh()
    assert calls == ["refresh", ("check", 7)]


@pytest.mark.asyncio
async def test_job_no_first_check_when_config_unchanged(monkeypatch):
    """配置没变（内核沿用）：不做全量节点检测——那是 6h 体检的活。"""
    calls: list = []

    async def _fake():
        return {"state": "refreshed", "subscriptionId": 7, "nodes": 3, "restarted": False}

    async def _fake_check(sub_id):
        calls.append("check")
        return {}

    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: True)
    monkeypatch.setattr(proxies_service, "maybe_refresh_active_clash_subscription", _fake)
    monkeypatch.setattr(proxies_service, "test_clash_nodes", _fake_check)

    await sched_mod._job_subscription_refresh()
    assert calls == []


@pytest.mark.asyncio
async def test_job_swallows_errors(monkeypatch):
    """异常不外泄：重拉抛错、首检抛错都只记日志（job 不冒泡给调度器）。"""
    async def _boom_refresh():
        raise RuntimeError("boom")

    async def _ok_refresh():
        return {"state": "refreshed", "subscriptionId": 7, "nodes": 3, "restarted": True}

    async def _boom_check(sub_id):
        raise ValueError("Clash 未运行")

    monkeypatch.setattr(sched_mod, "_crawler_idle", lambda: True)
    monkeypatch.setattr(proxies_service, "maybe_refresh_active_clash_subscription", _boom_refresh)
    await sched_mod._job_subscription_refresh()  # 不抛出即通过

    monkeypatch.setattr(proxies_service, "maybe_refresh_active_clash_subscription", _ok_refresh)
    monkeypatch.setattr(proxies_service, "test_clash_nodes", _boom_check)
    await sched_mod._job_subscription_refresh()  # 首检失败同样不抛出


@pytest.mark.asyncio
async def test_start_scheduler_registers_refresh_job(monkeypatch):
    """注册接线：start_scheduler 里真有 subscription_refresh 这个 30min 拍 job。

    只拦 add_job / start 与启动期的三个异步探针，不起真调度器（不污染单例）。
    """
    added: dict = {}

    def _fake_add_job(func, trigger, **kw):
        added[kw.get("id")] = (func, trigger, kw)

    async def _noop():
        return None

    monkeypatch.setattr(sched_mod.scheduler, "add_job", _fake_add_job)
    monkeypatch.setattr(sched_mod.scheduler, "start", lambda: None)
    monkeypatch.setattr(sched_mod, "_anchor_probe_after_start", _noop)
    monkeypatch.setattr(sched_mod, "_job_backup_catchup", _noop)
    monkeypatch.setattr(sched_mod, "_job_bartervg_catchup", _noop)

    sched_mod.start_scheduler()

    func, trigger, kw = added["subscription_refresh"]
    assert func is sched_mod._job_subscription_refresh
    assert trigger == "interval"
    assert kw.get("minutes") == 30


# ─── 门槛时间戳解析防呆 ─────────────────────────────────────


@pytest.mark.asyncio
async def test_refresh_bad_gate_value_treated_as_expired(db, monkeypatch, tmp_path):
    """门槛值不是合法时间（手改/旧格式）：当作过期照拉，不炸。"""
    await _seed_sub(db)
    calls: list = []
    cfg = _stub_runtime(monkeypatch, tmp_path, startup_text=_config_text())
    _stub_download(monkeypatch, cfg, calls)

    await settings_service.set_value(GATE_KEY, "not-a-timestamp")

    assert await _refresh_state() == "refreshed"
    assert calls == [SUB_URL]


def test_now_is_naive_iso_roundtrip():
    """门槛写入格式（naive ISO）能被解析回来——写入侧与读取侧同一口径。"""
    now = get_beijing_time_obj().replace(tzinfo=None)
    assert datetime.fromisoformat(now.isoformat()) == now
