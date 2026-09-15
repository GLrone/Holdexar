"""启动链「先开门，再收拾」行为验收。

约束（AGENTS.md 启动链红线）：lifespan 监听前只允许 setup_logging +
init_db（表结构/迁移不收拾完请求会踩混存 schema）；其余全部后台收拾
（`_post_startup_chain`），且：

1. **顺序有语义**：种子合并先于史低/永降/排序刷新（种子价格历史是标记
   的输入）；内核就位先于 Clash 自启；`start_scheduler` 收尾（种子历史
   合并单事务持锁十几秒，定时任务先跑会撞锁失败）。
2. **健壮性**：链中任一步抛异常只留日志，后续步骤照常执行、调度器照常启动。
3. **监听不被收拾阻塞**：lifespan yield 前不得 await 任何收拾步骤。

隔离：全部依赖打桩（tmp 库 / 无网络 / 无内核复制），只验证链的编排语义，
不验证各步骤自身行为（各域已有各自测试）。
"""
import asyncio
import sys
import types
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.core.database as database_module
import app.core.scheduler as sched_mod
from app.domains.account import service as account_service
from app.domains.bundles import service as bundles_service
from app.domains.crawl import service as crawl_service
from app.domains.family import service as family_service
from app.domains.games import service as games_service
from app.domains.proxies import service as proxies_service
from app.domains.rates import service as rates_service
from app.core import seed_assets

import app.main as main_mod


@pytest.fixture
def db(tmp_path, monkeypatch):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}", echo=False
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    for mod in (crawl_service, family_service, games_service, proxies_service,
                rates_service, account_service):
        monkeypatch.setattr(mod, "get_session_factory", lambda: factory)
    return factory


@pytest.fixture
def chain_calls(monkeypatch):
    """把收拾链的全部依赖换成记录桩，返回调用序列与放行闸。"""
    calls: list[str] = []
    gates: dict[str, asyncio.Event] = {}
    errors: dict[str, Exception] = {}

    def step(name):
        async def _impl(*args, **kwargs):
            calls.append(name)
            gate = gates.get(name)
            if gate is not None:
                await gate.wait()
            err = errors.get(name)
            if err is not None:
                raise err
        return _impl

    for name, fn in [
        ("import_seed", seed_assets.import_seed),
        ("merge_seeds", seed_assets.merge_seed_incremental),
        ("family_warm", family_service.cached_family_library),
        ("orphan_cleanup", crawl_service.cleanup_orphan_jobs),
        ("rates_cleanup", rates_service.cleanup_disallowed),
        ("legacy_sub_migrate", proxies_service.migrate_legacy_subscription),
        ("legacy_kv_migrate", account_service.migrate_legacy_kv),
        ("hl_flags", games_service.refresh_hl_flags),
        ("pp_flags", games_service.refresh_pp_flags),
        ("sort_cache", games_service.refresh_sort_cache),
        ("bundles_sort", bundles_service.refresh_bundle_sort_cache),
        ("bundles_warm", bundles_service.warmup),
    ]:
        # main 里是函数内局部 import 再以模块属性调用：桩必须挂在源模块上
        target = {"import_seed": seed_assets, "merge_seeds": seed_assets,
                  "family_warm": family_service, "orphan_cleanup": crawl_service,
                  "rates_cleanup": rates_service, "legacy_sub_migrate": proxies_service,
                  "legacy_kv_migrate": account_service, "hl_flags": games_service,
                  "pp_flags": games_service, "sort_cache": games_service,
                  "bundles_sort": bundles_service, "bundles_warm": bundles_service}[name]
        monkeypatch.setattr(target, fn.__name__, step(name))

    monkeypatch.setattr(main_mod, "_autostart_clash", step("clash_autostart"))
    monkeypatch.setattr(
        proxies_service, "maybe_run_clash_health_check", step("clash_health")
    )
    async def fake_backfill(dry_run: bool = False):
        calls.append("rates_backfill")
        return {"inserted": 0, "scanned": 0}

    monkeypatch.setattr(rates_service, "refresh_if_stale", step("rates_stale"))
    monkeypatch.setattr(rates_service, "backfill_history", fake_backfill)

    # 内核就位：真实实现是同步函数（链内走 asyncio.to_thread），桩保持同步签名
    def fake_ensure_kernel(data_dir):
        calls.append("kernel_ensure")
        return {"kernelReady": True, "copied": [], "missing": [],
                "upgraded": None, "bundleDir": str(data_dir)}

    from app.domains.proxies import clash_manager

    monkeypatch.setattr(clash_manager, "ensure_kernel", fake_ensure_kernel)

    started = asyncio.Event()

    def fake_start_scheduler():
        calls.append("scheduler_start")
        started.set()

    monkeypatch.setattr(sched_mod, "start_scheduler", fake_start_scheduler)
    # main 以 `from app.core.scheduler import start_scheduler` 拿的名字引用
    monkeypatch.setattr(main_mod, "start_scheduler", fake_start_scheduler)

    probe = types.SimpleNamespace(
        calls=calls, gates=gates, errors=errors, started=started
    )
    return probe


@pytest.mark.asyncio
async def test_chain_order_and_scheduler_last(db, chain_calls):
    """全链顺序：种子 → … → 标记三连 → 内核 → 自启 → 汇率 → 捆绑包预热 → 调度器收尾。"""
    await main_mod._post_startup_chain()
    expected = [
        "import_seed", "merge_seeds", "family_warm", "orphan_cleanup",
        "rates_cleanup", "legacy_sub_migrate", "legacy_kv_migrate",
        "hl_flags", "pp_flags", "sort_cache",
        "kernel_ensure", "clash_autostart", "clash_health",
        "rates_stale", "rates_backfill", "bundles_sort", "bundles_warm",
        "scheduler_start",
    ]
    assert chain_calls.calls == expected


@pytest.mark.asyncio
async def test_chain_survives_step_failure(db, chain_calls):
    """种子合并炸了不拖垮后续：标记刷新照常、调度器照常启动。"""
    chain_calls.errors["merge_seeds"] = RuntimeError("种子库损坏")
    chain_calls.errors["pp_flags"] = RuntimeError("窗口函数不兼容")
    await main_mod._post_startup_chain()
    assert "scheduler_start" in chain_calls.calls
    assert "hl_flags" in chain_calls.calls
    assert "sort_cache" in chain_calls.calls
    assert "clash_autostart" in chain_calls.calls


@pytest.mark.asyncio
async def test_lifespan_listens_without_waiting_for_chain(db, chain_calls, monkeypatch):
    """监听不被收拾阻塞：lifespan yield 返回时收拾链仍在跑（闸未放行）。"""
    from app.core.database import Base

    async with db.kw["bind"].begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 种子导入后放闸不放行 → 链停在第一步
    chain_calls.gates["import_seed"] = asyncio.Event()

    # init_db 是监听前的合法阻塞项，原样执行（tmp 库建表很快）
    monkeypatch.setattr(main_mod, "init_db", database_module.init_db)

    started = asyncio.Event()
    real_create_task = asyncio.create_task

    def tracked_create_task(coro, **kw):
        task = real_create_task(coro, **kw)
        if coro.__name__ == "_post_startup_chain":
            # 捕获链任务供收尾，避免事件循环结束时报「任务未等待」
            test_scope.tasks.append(task)
            started.set()
        return task

    test_scope = types.SimpleNamespace(tasks=[])
    monkeypatch.setattr(asyncio, "create_task", tracked_create_task)

    async with main_mod.lifespan(None):
        # lifespan 已开门（yield 返回）：此刻链只应刚起步，一步都没跑完
        assert started.is_set()
        assert chain_calls.calls == []
        await asyncio.sleep(0.05)
        # 闸不放行，链仍卡在第一步——证明 yield 没等它
        assert chain_calls.calls == ["import_seed"] or chain_calls.calls == []
        # 放行收尾
        chain_calls.gates["import_seed"].set()
        await asyncio.wait_for(test_scope.tasks[0], timeout=5)
    assert "scheduler_start" in chain_calls.calls
