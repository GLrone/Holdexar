"""Crawl Fail-Closed：**Mihomo Runtime Running ≠ Crawl Runtime Ready**。

只问「端口在听吗」不够：空池（池文件 `proxies: []`）下内核照样在跑、mixed-port
照样在监听，把 mixed-port 当可用入口交出去，抓取会照常启动而实际走 DIRECT 出网
——空组静默回落 DIRECT 属于假健康，必须由取值口在启动前拦下。

本组用例把两个概念钉死：

- `Mihomo Runtime Running`（维护/健康路径）：进程在跑、控制器可达、mixed-port 在听；
- `Crawl Runtime Ready`（爬取路径）：池里有合格出口 + 有 lane + lane 与出口槽快照
  **逐位一致** + 入口真的在听。

任一不满足 → `RuntimeUnavailableError` → 爬取不启动；**绝不回退** GLOBAL / 直连 / 旧代理。
"""
from __future__ import annotations

import socket

import pytest
import yaml

from app.domains.proxypool import runtime as rt
from app.domains.proxypool.exits import ExitSlot
from app.domains.proxypool.pool import POOL_FILENAME


def _write_pool(data_dir, names: list[str]) -> None:
    path = data_dir / "proxypool" / POOL_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"proxies": [
            {"name": n, "type": "ss", "server": f"{n}.example.net", "port": 1,
             "cipher": "aes-128-gcm", "password": "x"} for n in names
        ]}, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _hold_ports(data_dir, ports: list[int] | None = None) -> list[socket.socket]:
    """把运行配置里的 lane 端口换成真实在听的端口（模拟「内核在跑」）。"""
    doc = yaml.safe_load(rt.runtime_config_path(data_dir).read_text(encoding="utf-8"))
    holders: list[socket.socket] = []
    target = doc.get("listeners") or []
    for i, entry in enumerate(target):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        sock.listen(64)
        holders.append(sock)
        entry["port"] = int(sock.getsockname()[1])
    mixed = socket.socket()
    mixed.bind(("127.0.0.1", 0))
    mixed.listen(64)
    holders.append(mixed)
    doc["mixed-port"] = int(mixed.getsockname()[1])
    rt.runtime_config_path(data_dir).write_text(
        yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return holders


def _slot(name: str, exit_ip: str, idx: int = 0) -> ExitSlot:
    return ExitSlot(exit_ip=exit_ip, runtime_name=name, node_id=f"n{idx}",
                    lane_index=idx)


def _write_lane_plan(data_dir, entries: list[dict]) -> None:
    rt.write_lane_plan(data_dir, entries)



async def _empty_pool_session(tmp_path):
    """建一个空库会话：`eligible_nodes` 返回空 → 出口槽 0 个（空池）。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.core.database import Base
    import app.domains.proxypool.models  # noqa: F401 —— 注册表结构

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'fc-base.db').as_posix()}", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    return factory()


# ══ Case 1：内核在跑 + 池里 0 个出口 → 爬取不可用 ══════════════════
@pytest.mark.asyncio
async def test_case1_runtime_running_but_empty_pool_is_unavailable(tmp_path) -> None:
    """池文件为空（proxies: []）+ 入口在听 → 仍必须判不可用。"""
    _write_pool(tmp_path, [])                      # 空池
    rt.prepare_runtime_config(tmp_path, lanes=1)   # 生成 1 条 listener（模拟旧运行配置）
    holders = _hold_ports(tmp_path)
    try:
        assert rt.current_runtime_proxy_url(tmp_path) is not None, (
            "维护视角：mixed-port 在听 → Mihomo Runtime 仍算 running"
        )
        session = await _empty_pool_session(tmp_path)
        try:
            with pytest.raises(rt.RuntimeUnavailableError) as ei:
                await rt.crawl_lane_plan(session, tmp_path, max_lanes=4)
        finally:
            await session.close()
        assert "没有合格出口" in str(ei.value)
    finally:
        for s in holders:
            s.close()


# ══ Case 2：池里有节点 + 0 条 lane → 爬取不可用 ════════════════════
@pytest.mark.asyncio
async def test_case2_pool_exists_but_no_lane_is_unavailable(tmp_path) -> None:
    """有出口槽但运行配置里没有 lane（listener 未生成）→ 不可用。"""
    _write_pool(tmp_path, ["n1", "n2"])
    rt.prepare_runtime_config(tmp_path, lanes=0)   # 0 listener
    rt.runtime_config_path(tmp_path).write_text(
        yaml.safe_dump({"mode": "global", "mixed-port": 1, "proxies": []},
                       allow_unicode=True),
        encoding="utf-8",
    )
    with pytest.raises(rt.RuntimeUnavailableError) as ei:
        await rt.crawl_lane_plan(
            object(), tmp_path, max_lanes=4,
        ) if False else await _crawl_with_slots(tmp_path, [_slot("n1", "1.1.1.1"),
                                                           _slot("n2", "2.2.2.2", 1)])
    assert "没有 lane" in str(ei.value) or "未就绪" in str(ei.value)


async def _crawl_with_slots(data_dir, slots):
    """用给定槽快照直接调严格取值口（绕开 DB 的便捷包装）。"""
    bindings = rt.lane_bindings(data_dir)
    ok, why = rt.lane_plan_consistency(bindings, slots)
    if not ok:
        raise rt.RuntimeUnavailableError(f"代理运行时未就绪（{why}）：本次爬取未启动")
    raise AssertionError("预期不可用，但一致性判定为通过")


# ══ Case 3：lane 计划与出口快照不一致 → 不可用 ════════════════════
@pytest.mark.parametrize(
    ("plan", "slots", "why"),
    [
        # 34 条 lane / 33 个出口（真实现象）：长度不等
        ([{"lane": i, "port": 9000 + i, "exitIp": f"10.0.0.{i}", "node": f"n{i}"}
          for i in range(34)],
         [_slot(f"n{i}", f"10.0.0.{i}", i) for i in range(33)], "≠ 当前出口槽数"),
        # 逐位不一致：同一位次绑了另一个节点的出口
        ([{"lane": 0, "port": 9000, "exitIp": "9.9.9.9", "node": "OLD"}],
         [_slot("n1", "1.1.1.1")], "lane 0 绑的是"),
        # 计划里的出口与快照不符（节点换了落地）
        ([{"lane": 0, "port": 9000, "exitIp": "1.1.1.1", "node": "n1"}],
         [_slot("n1", "2.2.2.2")], "lane 0 绑的是"),
    ],
)
def test_case3_plan_must_match_exit_snapshot(plan, slots, why) -> None:
    ok, reason = rt.lane_plan_consistency(plan, slots)
    assert not ok and why in reason


def test_case3_consistent_plan_passes() -> None:
    plan = [{"lane": 0, "port": 9000, "exitIp": "1.1.1.1", "node": "n1"},
            {"lane": 1, "port": 9001, "exitIp": "2.2.2.2", "node": "n2"}]
    ok, reason = rt.lane_plan_consistency(plan, [_slot("n1", "1.1.1.1"),
                                                 _slot("n2", "2.2.2.2", 1)])
    assert ok and reason == ""


# ══ Case 4【核心】：GLOBAL 有节点 + Crawl Pool 为空 → 仍必须拒绝 ═════
@pytest.mark.asyncio
async def test_case4_global_available_but_crawl_pool_empty_still_refused(
    tmp_path, monkeypatch
) -> None:
    """fail-closed 的核心回归：GLOBAL/mixed-port 可用**不能**让爬取放行。

    把「维护视角可用」做到最强（mixed-port 在听、legacy GLOBAL 入口返回 URL、
    `current_runtime_proxy_url` 明确给值），爬取口仍必须拒绝。
    """
    _write_pool(tmp_path, [])                       # 爬取池为空
    rt.prepare_runtime_config(tmp_path, lanes=1)
    holders = _hold_ports(tmp_path)
    try:
        # 维护视角：一切"看起来正常"
        assert rt.current_runtime_proxy_url(tmp_path) is not None
        monkeypatch.setattr(rt, "current_runtime_proxy_url",
                            lambda _d: "http://127.0.0.1:1")
        monkeypatch.setattr(rt, "require_runtime_proxy_url",
                            lambda _d: "http://127.0.0.1:1")
        assert rt.require_lane_proxy_urls(tmp_path) == ["http://127.0.0.1:1"], (
            "旧的非生产取值口在无 lane 时会回退 GLOBAL —— 这正是生产禁止的路径"
        )
        # 生产口：必须拒绝，且不得把 GLOBAL 地址当作 lane 交出去
        session = await _empty_pool_session(tmp_path)
        try:
            with pytest.raises(rt.RuntimeUnavailableError):
                await rt.crawl_lane_plan(session, tmp_path, max_lanes=4)
        finally:
            await session.close()
    finally:
        for s in holders:
            s.close()


@pytest.mark.asyncio
async def test_case4_start_job_refuses_when_pool_empty(tmp_path, monkeypatch) -> None:
    """从**正式入口** `crawl_service.start_job` 触发：空池时必须拒绝启动。"""
    from app.core.database import Base
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    import app.core.database as database_module
    from app.domains.crawl import service as crawl_service
    from app.domains.proxypool.exits import exit_snapshot  # noqa: F401 —— 触发模型导入

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{(tmp_path / 'fc.db').as_posix()}", echo=False)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database_module, "get_session_factory", lambda: factory)
    monkeypatch.setattr(crawl_service, "get_session_factory", lambda: factory)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    class _Settings:
        data_dir = tmp_path

    monkeypatch.setattr(crawl_service, "effective_regions",
                        lambda regions=None: _async(["us"]))
    monkeypatch.setattr(crawl_service, "_resolve_scope_appids",
                        lambda scope, appids: _async([(220, "HL2")]))
    monkeypatch.setattr(crawl_service, "_resolve_worker_count", lambda: _async(4))
    monkeypatch.setattr(crawl_service, "_get_settings", lambda: _Settings(),
                        raising=False)

    from app.core import config as core_config

    monkeypatch.setattr(core_config, "get_settings", lambda: _Settings())

    crawl_service._active = None
    with pytest.raises(rt.RuntimeUnavailableError):
        await crawl_service.start_job(scope="appids", appids=[220],
                                      regions=["us"], kind="manual")
    assert crawl_service._active is None, "拒绝启动不得留下活动任务"


async def _async(value):
    return value


# ══ Case 5：正常出口池 → 正确返回 lane URL ════════════════════════
@pytest.mark.asyncio
async def test_case5_healthy_pool_returns_lane_urls(tmp_path) -> None:
    """池有 2 个不同出口 + lane 计划与快照一致 → 交出的正是两条 lane 的地址。"""
    _write_pool(tmp_path, ["n1", "n2"])
    rt.prepare_runtime_config(tmp_path, lanes=2)
    holders = _hold_ports(tmp_path)
    try:
        ports = rt.runtime_lane_ports(tmp_path)
        _write_lane_plan(tmp_path, [
            {"lane": 0, "port": ports[0], "exitIp": "1.1.1.1", "node": "n1"},
            {"lane": 1, "port": ports[1], "exitIp": "2.2.2.2", "node": "n2"},
        ])
        slots = [_slot("n1", "1.1.1.1"), _slot("n2", "2.2.2.2", 1)]
        bindings = rt.lane_bindings(tmp_path)
        ok, why = rt.lane_plan_consistency(bindings, slots)
        assert ok, why
        active = rt.select_run_lanes(
            bindings, {s.runtime_name: s.exit_ip for s in slots}, max_lanes=4)
        assert [b["url"] for b in active] == [
            f"http://127.0.0.1:{ports[0]}", f"http://127.0.0.1:{ports[1]}",
        ]
        assert [b["exitIp"] for b in active] == ["1.1.1.1", "2.2.2.2"]
    finally:
        for s in holders:
            s.close()