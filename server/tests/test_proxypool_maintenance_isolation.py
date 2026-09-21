"""维护事务隔离：L2 探针程序异常不得回滚本轮已产生的 L0/L1 遥测。

探针的程序异常（如 `StoreBrowseAPI` 缺方法）若逃出 `run_maintenance_cycle`，
调用方的 `session.commit()` 会被跳过 → 本轮 L0/L1 的写入一起回滚（真实生产表现：
`health_observations=0`、`exit_ip=0`、state 全 NEW）。本文件钉住修复后的行为：
异常只记日志、该层本轮无结果，L1 的落库照常提交。
"""
from __future__ import annotations

import os

from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxypool import scheduling as sched
from app.domains.proxypool.models import HealthObservation, ProxyNode

NOW = datetime(2026, 9, 20, 20, 0, 0)


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    # teardown 先还原环境再清缓存：monkeypatch 的还原发生在本夹具之后，
    # 否则「缓存库 ≠ 当前配置库」判据判定相等，临时引擎会留给后续文件
    os.environ.pop("HOLDEXAR_DATA_DIR", None)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


@pytest.fixture(autouse=True)
def _reset_pending():
    sched.take_rebuild_pending()
    yield
    sched.take_rebuild_pending()


@pytest.mark.asyncio
async def test_l2_exception_keeps_l1_telemetry(tmp_data_dir, monkeypatch, caplog):
    await init_db()
    async with get_session_factory()() as s:
        s.add(ProxyNode(
            node_id="fp-l1", fingerprint="fp-l1", runtime_name="1|l1-node",
            proxy_type="http", server="10.0.0.1", normalized_config={"name": "l1-node"},
            state="NEW", first_seen=NOW, last_seen=NOW, last_source_seen=NOW,
        ))
        await s.commit()

    async def fake_l1(session, **kw):  # noqa: ANN001 —— 模拟 L1 真实写入
        node = (await session.execute(
            select(ProxyNode).where(ProxyNode.node_id == "fp-l1")
        )).scalar_one()
        node.exit_ip = "203.0.113.9"
        node.last_l1_at = NOW
        session.add(HealthObservation(node_id="fp-l1", level="L1", ok=True,
                                      latency_ms=12, detail="exit=203.0.113.9",
                                      observed_at=NOW))
        return ("l1-outcome",)

    async def boom_l2(session, **kw):  # noqa: ANN001
        raise AttributeError("type object 'StoreBrowseAPI' has no attribute 'probe_url'")

    async def _none(*a, **kw):
        return None

    async def _noop(*a, **kw):
        return None

    async def _names(*a, **kw):
        return ("1|l1-node",)

    monkeypatch.setattr(sched, "crawler_busy", lambda: False)
    monkeypatch.setattr(sched, "exit_ip_check_pool", fake_l1)
    monkeypatch.setattr(sched, "business_check_pool", boom_l2)
    monkeypatch.setattr(sched, "current_global_selection", _none)
    monkeypatch.setattr(sched, "apply_global_selection", _noop)
    monkeypatch.setattr(sched, "eligible_runtime_names", _names)

    with caplog.at_level("ERROR"):
        async with get_session_factory()() as s:
            result = await sched.run_maintenance_cycle(
                s, data_dir=tmp_data_dir, controller_url="http://127.0.0.1:9",
                secret="x", now=NOW,
            )
            await s.commit()          # 探针异常若逃出，这一句不会执行

    assert result is not None
    assert result.l1 == ("l1-outcome",), "L1 必须已执行"
    assert result.l2 == (), "L2 程序异常 → 本轮无结果，不得伪装成功"
    assert "业务探针程序异常" in caplog.text, "必须有明确错误日志"

    async with get_session_factory()() as s:
        l1_rows = await s.scalar(
            select(func.count()).select_from(HealthObservation)
            .where(HealthObservation.level == "L1")
        )
        node = (await s.execute(
            select(ProxyNode).where(ProxyNode.node_id == "fp-l1")
        )).scalar_one()
    assert l1_rows == 1, "L1 观测必须已提交（不被 L2 异常回滚）"
    assert node.exit_ip == "203.0.113.9", "L1 的 exit_ip 必须已提交"
