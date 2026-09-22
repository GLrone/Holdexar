"""捆绑包刷新随价格链测试（链尾段接线语义，不出网不触库）。

规则：捆绑包抓取只有一个自动通道——价格刷新链（6h 锚点网格）的三层
串行 missing → pool（监控层，来源优先级排前）→ catalog（目录层，减去
监控层）在 run_sequential 内逐个 await 跑完后，紧跟
bundles.refresh_bundles() 全量刷包；发现的捆绑包（无价桩）就在全量表内，
随同一批请求首抓。其他爬取运行不再触发捆绑包抓取，开关关闭时整条链
（含链尾）都不跑。

隔离：run_sequential / refresh_bundles / 重锚 / 总开关全部打桩，
只验证接线、顺序、异常隔离与开关门禁。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import scheduler as sched_mod
from app.domains.bundles import refresh as bundles_refresh
from app.domains.crawl import service as crawl_service


def _stub_chain_env(monkeypatch, *, events, auto=True):
    """价格链出网/重锚/开关全打桩；run_sequential 与
    refresh_bundles 把调用顺序记录进 events（"chain" 元组 / "bundles"）。"""

    async def _auto_enabled():
        return auto

    monkeypatch.setattr(sched_mod, "price_auto_enabled", _auto_enabled)

    async def _no_reanchor(reason):
        return None

    monkeypatch.setattr(sched_mod, "_reanchor_price_refresh", _no_reanchor)

    async def _run_sequential(specs, **kwargs):
        events.append(("chain", [s.get("kind", s.get("scope")) for s in specs]))
        return [{"id": 1}]

    monkeypatch.setattr(crawl_service, "run_sequential", _run_sequential)

    # 价格周期记账打桩：本文件不触库，Cycle 有独立出口（create 返回 None =
    # 本轮不挂 Cycle，抓取照常）
    import app.domains.crawl.cycle as cycle_mod

    async def _no_cycle(*args, **kwargs):
        return None

    monkeypatch.setattr(cycle_mod, "create", _no_cycle)

    async def _refresh_bundles():
        events.append("bundles")
        return {
            "ok": True, "updated": 3, "total": 3, "regionPrices": 126,
            "failed": [], "droppedSingletons": 0,
        }

    monkeypatch.setattr(bundles_refresh, "refresh_bundles", _refresh_bundles)


@pytest.fixture
def _idle():
    """任务表空 + busy 复位（用例前后各置一次，防泄漏影响别的测试）。"""
    crawl_service._active = None
    sched_mod._price_cycle_busy = False
    yield
    crawl_service._active = None
    sched_mod._price_cycle_busy = False


@pytest.mark.asyncio
async def test_bundle_refresh_follows_game_chain(monkeypatch, _idle):
    """游戏侧三层链跑完 → 紧跟捆绑包全量刷新（顺序 + 接线）。"""
    events: list = []
    _stub_chain_env(monkeypatch, events=events)

    await sched_mod._job_price_refresh()

    assert events == [("chain", ["missing", "pool", "catalog"]), "bundles"]
    assert sched_mod._price_cycle_busy is False


@pytest.mark.asyncio
async def test_bundle_refresh_failure_does_not_break_job(monkeypatch, _idle):
    """捆绑包刷新抛异常：主链已完成、busy 正常复位、异常不外溢。"""
    events: list = []
    _stub_chain_env(monkeypatch, events=events)

    async def _boom():
        events.append("bundles")
        raise RuntimeError("抓取炸了")

    monkeypatch.setattr(bundles_refresh, "refresh_bundles", _boom)

    await sched_mod._job_price_refresh()  # 不抛
    assert events == [("chain", ["missing", "pool", "catalog"]), "bundles"]
    assert sched_mod._price_cycle_busy is False


@pytest.mark.asyncio
async def test_bundle_refresh_respects_auto_price_switch(monkeypatch, _idle):
    """crawl.auto_price 关：整条链（含捆绑包尾段）都不跑。"""
    events: list = []
    _stub_chain_env(monkeypatch, events=events, auto=False)

    await sched_mod._job_price_refresh()

    assert events == []

# ── 通知层不在本文件范围（有独立测试）：不打真库、不发真邮件 ──
import app.domains.notifications.service as _notification_service


@pytest.fixture(autouse=True)
def _no_notifications(monkeypatch):
    async def _noop(cycle_id):
        return {"created": 0, "sent": 0, "deferred": 0, "failed": 0}

    monkeypatch.setattr(_notification_service, "dispatch", _noop)
