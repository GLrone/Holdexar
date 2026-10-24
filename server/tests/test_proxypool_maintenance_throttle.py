"""体检节流：`maintenance_due` 四态判据 + 周期内的跳过接线 + 收尾签名。

判据 = 「最近一次成功收尾的 scheduled 体检在间隔内」且「合格集签名未变」才跳过；
无历史 run、超间隔、签名变化、interval≤0、读取异常一律按到期处理（宁多跑不漏跑）。
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.database import get_session_factory, init_db
from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription
from app.domains.proxypool.models import HealthRun, ProxyNode, node_fingerprint
from app.domains.proxypool.scheduling import (
    MAINTENANCE_INTERVAL_HOURS_KEY,
    MAINTENANCE_LAST_SIG_KEY,
    _eligible_signature,
    maintenance_due,
)
from app.domains.settings.service import get_value, set_value

NOW = datetime(2026, 9, 21, 10, 0, 0)


async def _seed_eligible_nodes() -> None:
    """两个合格节点（ACTIVE + 来源 + 配置完整）——签名判据的事实底座。"""
    for i, name in enumerate(("1|a", "1|b"), start=1):
        config = {"name": name.split("|", 1)[-1], "type": "http",
                  "server": "127.0.0.1", "port": 9000 + i}
        fp = node_fingerprint(config)
        async with get_session_factory()() as s:
            if await s.get(ProxySubscription, 1) is None:
                s.add(ProxySubscription(id=1, kind="clash", url="https://source.invalid/1",
                                        admission_status=ADMISSION_ACTIVE))
            s.add(ProxyNode(node_id=fp, fingerprint=fp, runtime_name=name,
                            proxy_type="http", server="127.0.0.1",
                            normalized_config=config, state="ACTIVE",
                            first_seen=NOW, last_seen=NOW, last_source_seen=NOW))
            await s.execute(update(ProxyNode)
                .where(ProxyNode.node_id == fp)
                .values(subscription_id=1, original_name="x"))
            await s.commit()


async def _add_successful_run(started_at: datetime, *, end_reason: str | None = None) -> None:
    async with get_session_factory()() as s:
        s.add(HealthRun(channel="scheduled", started_at=started_at,
                        finished_at=started_at + timedelta(minutes=5),
                        total=2, steam_ok=2, ip_known=2, failed=0,
                        end_reason=end_reason, duration_ms=300000))
        await s.commit()


async def _current_signature() -> str:
    async with get_session_factory()() as s:
        from app.domains.proxypool.pool import eligible_runtime_names
        return _eligible_signature(await eligible_runtime_names(s))


# ── 四态判据 ─────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_due_without_any_history(tmp_data_dir) -> None:
    await init_db()
    await _seed_eligible_nodes()
    async with get_session_factory()() as s:
        assert await maintenance_due(s, now=NOW), "无历史 run 必须到期"


@pytest.mark.asyncio
async def test_not_due_within_interval_with_same_signature(tmp_data_dir) -> None:
    await init_db()
    await _seed_eligible_nodes()
    await _add_successful_run(NOW - timedelta(minutes=30))
    await set_value(MAINTENANCE_LAST_SIG_KEY, await _current_signature())
    async with get_session_factory()() as s:
        assert not await maintenance_due(s, now=NOW), "间隔内 + 签名未变 → 跳过"


@pytest.mark.asyncio
async def test_due_within_interval_when_signature_changed(tmp_data_dir) -> None:
    await init_db()
    await _seed_eligible_nodes()
    await _add_successful_run(NOW - timedelta(minutes=30))
    await set_value(MAINTENANCE_LAST_SIG_KEY, "stale-signature")
    async with get_session_factory()() as s:
        assert await maintenance_due(s, now=NOW), "间隔内但合格集变了 → 到期"


@pytest.mark.asyncio
async def test_due_when_interval_elapsed(tmp_data_dir) -> None:
    await init_db()
    await _seed_eligible_nodes()
    await _add_successful_run(NOW - timedelta(hours=3))
    await set_value(MAINTENANCE_LAST_SIG_KEY, await _current_signature())
    async with get_session_factory()() as s:
        assert await maintenance_due(s, now=NOW), "超间隔 → 到期"


@pytest.mark.asyncio
async def test_interval_zero_always_due(tmp_data_dir) -> None:
    await init_db()
    await _seed_eligible_nodes()
    await _add_successful_run(NOW - timedelta(minutes=1))
    await set_value(MAINTENANCE_LAST_SIG_KEY, await _current_signature())
    await set_value(MAINTENANCE_INTERVAL_HOURS_KEY, 0)
    async with get_session_factory()() as s:
        assert await maintenance_due(s, now=NOW), "interval=0 → 每拍都跑"


@pytest.mark.asyncio
async def test_failed_run_does_not_count_as_success(tmp_data_dir) -> None:
    await init_db()
    await _seed_eligible_nodes()
    await _add_successful_run(NOW - timedelta(minutes=30), end_reason="l2_error")
    await set_value(MAINTENANCE_LAST_SIG_KEY, await _current_signature())
    async with get_session_factory()() as s:
        assert await maintenance_due(s, now=NOW), "失败收尾的 run 不构成节流依据"


# ── 周期接线：未到期跳过维护位，到期照跑 ─────────────────────────
@pytest.mark.asyncio
async def test_cycle_skips_maintenance_when_not_due(tmp_data_dir, monkeypatch) -> None:
    from app.domains.proxypool import scheduling as sched

    await init_db()
    await _seed_eligible_nodes()
    await _add_successful_run(NOW - timedelta(minutes=30))
    await set_value(MAINTENANCE_LAST_SIG_KEY, await _current_signature())

    calls: list[str] = []

    async def _l0(session, **kw):
        calls.append("l0")
        return ()

    async def _rebuild(session, **kw):
        calls.append("rebuild")
        return None

    async def _maint(session, **kw):
        calls.append("maintenance")
        return None

    monkeypatch.setattr(sched, "run_l0_cycle", _l0)
    monkeypatch.setattr(sched, "run_pending_rebuild", _rebuild)
    monkeypatch.setattr(sched, "run_maintenance_cycle", _maint)
    monkeypatch.setattr(sched, "crawler_busy", lambda: False)

    async with get_session_factory()() as s:
        result = await sched.run_proxypool_cycle(
            s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
            secret="s", runtime=object(), exe_path="x", now=NOW,
        )
        await s.commit()

    assert calls == ["l0", "rebuild"], "未到期：L0 与重建照跑，维护不占位"
    assert result.busy is False and result.maintenance is None


@pytest.mark.asyncio
async def test_cycle_runs_maintenance_when_due(tmp_data_dir, monkeypatch) -> None:
    from app.domains.proxypool import scheduling as sched

    await init_db()
    await _seed_eligible_nodes()
    await _add_successful_run(NOW - timedelta(hours=3))
    await set_value(MAINTENANCE_LAST_SIG_KEY, await _current_signature())

    calls: list[str] = []

    async def _l0(session, **kw):
        return ()

    async def _rebuild(session, **kw):
        return None

    async def _maint(session, **kw):
        calls.append("maintenance")
        return None

    monkeypatch.setattr(sched, "run_l0_cycle", _l0)
    monkeypatch.setattr(sched, "run_pending_rebuild", _rebuild)
    monkeypatch.setattr(sched, "run_maintenance_cycle", _maint)
    monkeypatch.setattr(sched, "crawler_busy", lambda: False)

    async with get_session_factory()() as s:
        await sched.run_proxypool_cycle(
            s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
            secret="s", runtime=object(), exe_path="x", now=NOW,
        )
        await s.commit()

    assert calls == ["maintenance"], "到期（超间隔）→ 维护照常执行"


# ── 成功收尾写签名 ───────────────────────────────────────────────
@pytest.mark.asyncio
async def test_successful_maintenance_records_signature(tmp_data_dir, monkeypatch) -> None:
    from app.domains.proxypool import scheduling as sched

    await init_db()
    await _seed_eligible_nodes()
    names = ("1|a", "1|b")

    async def _probe(session, **kw):
        return sched.MaintenanceResult(previous_selection=None, selection=None, l1=(), l2=())

    monkeypatch.setattr(sched, "crawler_busy", lambda: False)
    monkeypatch.setattr(sched, "_maintenance_probe", _probe)

    async with get_session_factory()() as s:
        result = await sched.run_maintenance_cycle(
            s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
            secret="s", now=NOW,
        )
        await s.commit()

    assert result is not None
    assert await get_value(MAINTENANCE_LAST_SIG_KEY) == _eligible_signature(names), (
        "成功体检收尾必须记录当前合格集签名"
    )


# ── 探测通道故障轮不锚定节流 ─────────────────────────────────────
@pytest.mark.asyncio
async def test_all_failed_run_does_not_anchor_throttle(tmp_data_dir) -> None:
    """整轮 0 成功（如重建后控制器认证失配）不是「体检过且健康」的锚：
    间隔内也必须重跑，否则一次通道故障让节点结论静默 2 小时。"""
    await init_db()
    await _seed_eligible_nodes()
    async with get_session_factory()() as s:
        s.add(HealthRun(channel="scheduled", started_at=NOW - timedelta(minutes=30),
                        finished_at=NOW - timedelta(minutes=25),
                        total=233, steam_ok=0, ip_known=0, failed=233,
                        end_reason=None, duration_ms=300000))
        await s.commit()
    await set_value(MAINTENANCE_LAST_SIG_KEY, await _current_signature())
    async with get_session_factory()() as s:
        assert await maintenance_due(s, now=NOW), "全失败轮（0 成功）不构成节流依据"


def test_summary_without_node_evidence_has_no_numbers() -> None:
    """整轮全是探测通道失败（选不中节点）时，收场结论不得携带
    「正常 0 · 异常 N」假数字；有节点结论时照常携带。"""
    from app.domains.proxypool.health import BusinessResult
    from app.domains.proxypool.scheduling import MaintenanceResult, _health_run_summary

    infra_fail = MaintenanceResult(
        previous_selection=None, selection=None, l1=(),
        l2=(BusinessResult("n1", False, None, None,
                           "NODE_PROBE_FAILED: 选择节点失败（HTTP 401）", None),
            BusinessResult("n2", False, None, None,
                           "NODE_PROBE_FAILED: 控制器不可达（ConnectError）", None)),
    )
    assert _health_run_summary(infra_fail) == {"total": 2}

    real_fail = MaintenanceResult(
        previous_selection=None, selection=None, l1=(),
        l2=(BusinessResult("n1", True, 200, 999, "BUSINESS_OK", None),
            BusinessResult("n2", False, 502, None,
                           "TARGET_SERVICE_FAILED: HTTP 502", None)),
    )
    assert _health_run_summary(real_fail) == {"total": 2, "ok": 1, "failed": 1}
