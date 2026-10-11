"""钱包全员失败日志按「账号总数+冻结数」签名收敛：同形态只报一次 ERROR，
之后每小时注意级提醒；成功与无绑定账号清态。
"""
from __future__ import annotations

import logging

import pytest

from app.core import scheduler as sched

SCHED_LOGGER = "app.core.scheduler"


@pytest.fixture(autouse=True)
def _reset_state():
    sched._wallet_fail_log_state.clear()
    yield
    sched._wallet_fail_log_state.clear()


def _fail_result(total: int = 3, frozen: int = 1, reasons: dict | None = None) -> dict:
    return {
        "ok": False,
        "ok_count": 0,
        "skipped": 0,
        "total": total,
        "frozen": frozen,
        "reasons": reasons if reasons is not None else {"backoff": 2, "no_cookie": 1},
    }


def test_same_signature_logs_error_once(caplog):
    result = _fail_result()
    with caplog.at_level(logging.INFO, logger=SCHED_LOGGER):
        for _ in range(3):
            sched._log_wallet_fail_converged(result)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1, "同签名连调三次只允许一条 ERROR"
    assert errors[0].msg == "3 个账号的 Steam 钱包余额本轮全部同步失败"
    assert errors[0].hl_detail["失败原因"] == "2×已退避重试；1×未绑定 Cookie"


def test_unchanged_signature_digests_hourly(caplog):
    result = _fail_result()
    with caplog.at_level(logging.INFO, logger=SCHED_LOGGER):
        sched._log_wallet_fail_converged(result)
        sched._wallet_fail_log_state["last_at"] -= 3601.0
        sched._log_wallet_fail_converged(result)

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(errors) == 1, "形态不变不得新增 ERROR"
    assert len(warnings) == 1, "超过一小时应出一条注意级提醒"
    assert "已持续" in warnings[0].msg
    assert warnings[0].hl_detail["失败原因"] == "2×已退避重试；1×未绑定 Cookie"


def test_signature_change_reannounces(caplog):
    with caplog.at_level(logging.INFO, logger=SCHED_LOGGER):
        sched._log_wallet_fail_converged(_fail_result(total=3, frozen=1))
        first_at = sched._wallet_fail_log_state["first_at"]
        sched._wallet_fail_log_state["last_at"] -= 3601.0
        sched._log_wallet_fail_converged(_fail_result(total=4, frozen=1))

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2, "签名变化必须立即重新宣告"
    assert errors[1].msg == "4 个账号的 Steam 钱包余额本轮全部同步失败"
    assert sched._wallet_fail_log_state["first_at"] > first_at, "first_at 必须重置"


def test_all_frozen_digest_wording(caplog):
    with caplog.at_level(logging.INFO, logger=SCHED_LOGGER):
        sched._log_wallet_fail_converged(_fail_result(total=3, frozen=3))
        sched._wallet_fail_log_state["last_at"] -= 3601.0
        sched._log_wallet_fail_converged(_fail_result(total=3, frozen=3))
        frozen_msg = caplog.records[-1].msg

        caplog.clear()
        sched._log_wallet_fail_converged(_fail_result(total=3, frozen=1))
        sched._wallet_fail_log_state["last_at"] -= 3601.0
        sched._log_wallet_fail_converged(_fail_result(total=3, frozen=1))
        mixed_msg = caplog.records[-1].msg

    assert frozen_msg.startswith("3 个账号的钱包余额同步仍在冻结")
    assert mixed_msg.startswith("Steam 钱包余额同步仍未恢复")


def test_digest_detail_carries_current_round_reasons(caplog):
    with caplog.at_level(logging.INFO, logger=SCHED_LOGGER):
        sched._log_wallet_fail_converged(_fail_result(reasons={"backoff": 3}))
        sched._wallet_fail_log_state["last_at"] -= 3601.0
        sched._log_wallet_fail_converged(_fail_result(reasons={"no_cookie": 3}))

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert warnings[0].hl_detail["失败原因"] == "3×未绑定 Cookie"


def test_fmt_fail_duration():
    assert sched._fmt_fail_duration(1500) == "25 分钟"
    assert sched._fmt_fail_duration(5400) == "1 小时 30 分钟"
    assert sched._fmt_fail_duration(3600) == "1 小时"


@pytest.mark.asyncio
async def test_success_clears_state(monkeypatch, caplog):
    from app.domains.account import service as account_service

    async def ok_result():
        return {"ok": True, "ok_count": 3, "skipped": 0, "total": 3, "frozen": 0, "reasons": {}}

    monkeypatch.setattr(account_service, "sync_wallets_rotational", ok_result)
    with caplog.at_level(logging.INFO, logger=SCHED_LOGGER):
        sched._log_wallet_fail_converged(_fail_result())
        assert sched._wallet_fail_log_state, "失败后应有收敛状态"
        await sched._job_wallet_sync()
        assert sched._wallet_fail_log_state == {}, "成功必须清态"
        sched._log_wallet_fail_converged(_fail_result())

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 2, "成功清态后再失败必须立即重新 ERROR，不被节流吞掉"


@pytest.mark.asyncio
async def test_no_cookie_clears_state(monkeypatch):
    from app.domains.account import service as account_service

    async def no_cookie_result():
        return {"ok": False, "status": "no_cookie"}

    monkeypatch.setattr(account_service, "sync_wallets_rotational", no_cookie_result)
    sched._log_wallet_fail_converged(_fail_result())
    assert sched._wallet_fail_log_state
    await sched._job_wallet_sync()
    assert sched._wallet_fail_log_state == {}, "无绑定账号路径必须清态"
