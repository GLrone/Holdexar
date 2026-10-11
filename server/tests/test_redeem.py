"""redeem 域单元测试：激活结果映射 / Cookie 前提 / 免费领取判定（不触真实网络）。

网络层通过 monkeypatch 替换 _post_steam 与 settings 键值层，隔离到临时库。
"""
import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.database import Base
from app.domains.account import service as account_service
from app.domains.redeem import service as redeem_service
from app.domains.redeem.models import RAW_MAX, RedeemActivation
from app.domains.settings import service as settings_service

PRIMARY = "76561198000000001"


@pytest.fixture
def db():
    from app.core.database import get_session_factory

    return get_session_factory()
@pytest_asyncio.fixture(autouse=True)
async def _clean_act_log():
    """清空模块级激活计数（进程内滑动窗口，跨用例会泄漏）。"""
    redeem_service._ACT_LOG.clear()
    yield
    redeem_service._ACT_LOG.clear()


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=None, location=None):
        import json as _json

        self.status_code = status_code
        self._payload = payload
        # 与真实 httpx.Response 一致：有 JSON 体时 text 即序列化结果
        if text is None:
            text = _json.dumps(payload) if payload is not None else ""
        self.text = text
        self.headers = {"location": location} if location else {}

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.mark.asyncio
async def test_activate_key_success_receipt(db, monkeypatch):
    """success=1 → ok，SubID/SubName 从回执 line_items 提取。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )

    async def fake_post(url, data, jar, referer=None):
        assert url.endswith("/account/ajaxregisterkey/")
        assert data["product_key"] == "AAAAA-BBBBB-CCCCC"
        return FakeResponse(payload={
            "success": 1,
            "purchase_receipt_info": {
                "line_items": [
                    {"packageid": 307574, "line_item_description": "Dark Envoy"}
                ]
            },
        })

    monkeypatch.setattr(redeem_service, "_post_steam", fake_post)
    r = await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    assert r["status"] == "ok"
    assert r["detail"] == "激活成功"
    assert r["subId"] == "307574"
    assert r["subName"] == "Dark Envoy"
    # raw = Steam 返回原文（前端"展开原文"核对用）
    assert "purchase_receipt_info" in r["raw"]


@pytest.mark.asyncio
async def test_activate_key_own_receipt_keeps_sub(db, monkeypatch):
    """已拥有（错误码 9）回执带 line_items → 同样提取 subId/subName 展示。

    失败分支也取 line_items[0].packageid + line_item_description
    ——"已拥有"能看到是哪个 sub 是原脚本一直有的信息，此前只认成功回执是遗漏。
    """
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(payload={
            "success": 2,
            "purchase_result_details": 9,
            "purchase_receipt_info": {
                "line_items": [
                    {"packageid": 52524, "line_item_description": "Counter-Strike 2"}
                ]
            },
        }))(),
    )
    r = await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    assert r["status"] == "own"
    assert r["subId"] == "52524"
    assert r["subName"] == "Counter-Strike 2"
    assert "purchase_result_details" in r["raw"]


@pytest.mark.asyncio
async def test_activate_key_already_owned(db, monkeypatch):
    """错误码 9 → own（已拥有，前端单列统计）。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        # 真实回执形态：success=2 失败标记，错误码在 purchase_result_details
        lambda *a, **k: _async(FakeResponse(payload={"success": 2, "purchase_result_details": 9}))(),
    )
    r = await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    assert r["status"] == "own"
    assert r["detail"] == "已拥有"


@pytest.mark.asyncio
async def test_activate_key_rate_limited(db, monkeypatch):
    """错误码 53 → fail，detail 为次数上限语义（错误码表：53=次数上限，勿按官方文档改）。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(payload={"success": 2, "purchase_result_details": 53}))(),
    )
    r = await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    assert r["status"] == "fail"
    assert "上限" in r["detail"]


@pytest.mark.asyncio
async def test_activate_key_invalid_code_passthrough(db, monkeypatch):
    """无映射错误码 → 回传原始语义（含错误码）。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(payload={"success": 2, "purchase_result_details": 87}))(),
    )
    r = await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    assert r["status"] == "fail"
    assert "87" in r["detail"]


@pytest.mark.asyncio
async def test_activate_batch_aggregation(db, monkeypatch):
    """批量激活汇总：ok/own/fail 计数与逐码结果。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    results = iter([
        {"status": "ok", "detail": "激活成功", "subId": "1", "subName": "A"},
        {"status": "own", "detail": "已拥有", "subId": "", "subName": ""},
        {"status": "fail", "detail": "激活码无效或已被使用", "subId": "", "subName": ""},
    ])
    async def fake_activate(key):
        return next(results)
    monkeypatch.setattr(redeem_service, "activate_key", fake_activate)

    out = await redeem_service.activate_batch(["K1", "K2", "K3"])
    assert out["ok"] == 1 and out["own"] == 1 and out["fail"] == 1
    assert [r["code"] for r in out["results"]] == ["K1", "K2", "K3"]


@pytest.mark.asyncio
async def test_activate_key_no_cookie(db):
    """未绑 Cookie → ValueError（路由层 400 诚实引导）。"""
    with pytest.raises(ValueError, match="尚未绑定"):
        await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")


@pytest.mark.asyncio
async def test_activate_key_no_sessionid(db):
    """缺 sessionid → ValueError（激活端点必需）。"""
    await settings_service.set_value(
        "account.steam_cookies", "steamLoginSecure=76561198000000001%7C%7Cjwt-token"
    )
    with pytest.raises(ValueError, match="sessionid"):
        await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")


@pytest.mark.asyncio
async def test_free_license_claim(db, monkeypatch):
    """免费领取：302 → ok；JSON success → ok；JSON error 24 → own；登录页 HTML → fail（不冒领）。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    # 302 跳转（重定向链终点非登录页）= 受理
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(status_code=302, location="/account"))(),
    )
    monkeypatch.setattr(redeem_service, "_redirect_chain_hits_login", _async(False))
    r = await redeem_service.add_free_license(927875)
    assert r["status"] == "ok"

    # 302 但重定向链终点是登录页 = 页面层拒绝（checkout 域两跳后拒绝）→ 不冒领
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(
            status_code=302, location="https://checkout.steampowered.com/login/?redir=x"))(),
    )
    monkeypatch.setattr(redeem_service, "_redirect_chain_hits_login", _async(True))
    r = await redeem_service.add_free_license(927875)
    assert r["status"] == "fail"
    assert "登录" in r["detail"]

    # JSON error 24 = 已拥有
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(payload={"success": 2, "error": 24}))(),
    )
    r = await redeem_service.add_free_license(927875)
    assert r["status"] == "own"

    # 200 + 登录页 HTML = 页面层未认登录态 → fail（诚实，不冒领）
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(status_code=200, text="<html>login</html>"))(),
    )
    r = await redeem_service.add_free_license(927875)
    assert r["status"] == "fail"
    assert "页面层" in r["detail"]

    # 401 = Cookie 失效
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(status_code=401))(),
    )
    r = await redeem_service.add_free_license(927875)
    assert r["status"] == "fail"
    assert "失效" in r["detail"]


@pytest.mark.asyncio
async def test_quota_status(db):
    """quota_status：Cookie 三件套可用性 + 账号维度激活计数。"""
    out = await redeem_service.quota_status()
    assert out == {"hasCookie": False, "hasSessionId": False, "steamId": "", "used": 0, "limit": 10}

    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    out = await redeem_service.quota_status()
    assert out["hasCookie"] is True and out["hasSessionId"] is True
    assert out["steamId"] == PRIMARY
    assert out["used"] == 0 and out["limit"] == 10


@pytest.mark.asyncio
async def test_activation_count_per_account(db, monkeypatch):
    """激活计数按账号独立：换绑 Cookie（换 steamid）计数归零——Steam 的
    10 次/30 分钟限制作用于单个账号，不是对整个工具的限制。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(payload={"success": 2, "purchase_result_details": 9}))(),
    )
    redeem_service._ACT_LOG.clear()
    await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    await redeem_service.activate_key("BBBBB-CCCCC-DDDDD")
    out = await redeem_service.quota_status()
    assert out["steamId"] == PRIMARY and out["used"] == 2

    # 换绑另一个账号的 Cookie → 计数从 0 重新开始
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=xyz; steamLoginSecure=76561198000000002%7C%7Cjwt-token-b",
    )
    out = await redeem_service.quota_status()
    assert out["steamId"] == "76561198000000002" and out["used"] == 0
    redeem_service._ACT_LOG.clear()


@pytest.mark.asyncio
async def test_activation_ledger_rows_for_each_status(db, monkeypatch):
    """激活结果落账：ok/own/fail 各一行，key/subId/说明/账号随行。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    cases = [
        (FakeResponse(payload={
            "success": 1,
            "purchase_receipt_info": {"line_items": [
                {"packageid": 307574, "line_item_description": "Dark Envoy"}]},
        }), "ok", "307574"),
        (FakeResponse(payload={"success": 2, "purchase_result_details": 9}), "own", ""),
        (FakeResponse(payload={"success": 2, "purchase_result_details": 15}), "fail", ""),
    ]
    for resp, status, subid in cases:
        monkeypatch.setattr(redeem_service, "_post_steam", _async(resp))
        await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    async with db() as session:
        rows = (await session.execute(
            select(RedeemActivation).order_by(RedeemActivation.id)
        )).scalars().all()
    assert [r.status for r in rows] == ["ok", "own", "fail"]
    assert all(r.kind == "cdk" for r in rows)
    assert all(r.product_key == "AAAAA-BBBBB-CCCCC" for r in rows)
    assert rows[0].subid == "307574" and rows[0].sub_name == "Dark Envoy"
    assert rows[0].detail == "激活成功"
    assert rows[0].steamid == PRIMARY
    assert "purchase_receipt_info" in rows[0].raw
    # 日志侧 key 只留掩码（账本表存原文，日志不整码明文）
    assert redeem_service._mask_key("ABCDE-FGHIJ-KLMNP") == "ABCDE**********NP"


@pytest.mark.asyncio
async def test_free_claim_ledger_row_kind_free(db, monkeypatch):
    """免费领取落账：来源类型 free、subid 随行、key 列为空。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(status_code=302, location="/account"))(),
    )
    monkeypatch.setattr(redeem_service, "_redirect_chain_hits_login", _async(False))
    r = await redeem_service.add_free_license(927875)
    assert r["status"] == "ok"
    async with db() as session:
        row = (await session.execute(select(RedeemActivation))).scalars().one()
    assert row.kind == "free" and row.status == "ok"
    assert row.subid == "927875" and row.product_key == ""
    assert row.steamid == PRIMARY


@pytest.mark.asyncio
async def test_list_activations_order_and_paging(db, monkeypatch):
    """激活历史读取：时间倒序（同秒按 id 倒序）+ limit/offset 分页 + total。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )
    keys = ["AAAAA-BBBBB-CCCC1", "AAAAA-BBBBB-CCCC2", "AAAAA-BBBBB-CCCC3"]
    monkeypatch.setattr(
        redeem_service, "_post_steam",
        lambda *a, **k: _async(FakeResponse(payload={"success": 2, "purchase_result_details": 15}))(),
    )
    for k in keys:
        await redeem_service.activate_key(k)
    page1 = await redeem_service.list_activations(limit=2, offset=0)
    assert page1["total"] == 3
    assert [r["key"] for r in page1["rows"]] == list(reversed(keys))[:2]
    page2 = await redeem_service.list_activations(limit=2, offset=2)
    assert [r["key"] for r in page2["rows"]] == [keys[0]]
    # limit 夹取：超上限收 200，非法值不炸
    out = await redeem_service.list_activations(limit=99999, offset=-5)
    assert len(out["rows"]) == 3


@pytest.mark.asyncio
async def test_ledger_raw_truncated(db):
    """超长回执原文落账被截断到 RAW_MAX：不炸、不整段入库。"""
    big = "x" * (RAW_MAX * 3)
    await redeem_service._record_ledger(
        kind="cdk", status="fail", product_key="AAAAA-BBBBB-CCCCC",
        detail="测试截断", raw=big, steamid=PRIMARY,
    )
    async with db() as session:
        row = (await session.execute(select(RedeemActivation))).scalars().one()
    assert len(row.raw) == RAW_MAX
    # 正常激活链路的 raw 先按前端展示口径截到 2000，再受 RAW_MAX 兜底
    await redeem_service._record_ledger(
        kind="cdk", status="fail", product_key="BBBBB-CCCCC-DDDDD",
        detail="测试上限", raw="y" * 5000, steamid=PRIMARY,
    )
    async with db() as session:
        rows = (await session.execute(
            select(RedeemActivation).order_by(RedeemActivation.id)
        )).scalars().all()
    assert len(rows[1].raw) == 5000
    assert rows[0].product_key == "AAAAA-BBBBB-CCCCC"


@pytest.mark.asyncio
async def test_transport_failure_ledger_row(db, monkeypatch):
    """传输层失败（网络不通/超时）同样落 fail 一行；Steam 未收到，不消耗激活次数。"""
    await settings_service.set_value(
        "account.steam_cookies",
        "sessionid=abc123; steamLoginSecure=76561198000000001%7C%7Cjwt-token",
    )

    def boom(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(redeem_service, "_post_steam", boom)
    r = await redeem_service.activate_key("AAAAA-BBBBB-CCCCC")
    assert r["status"] == "fail" and "激活请求失败" in r["detail"]
    out = await redeem_service.quota_status()
    assert out["used"] == 0
    async with db() as session:
        row = (await session.execute(select(RedeemActivation))).scalars().one()
    assert row.status == "fail"
    assert row.product_key == "AAAAA-BBBBB-CCCCC" and row.raw == ""


def _async(value):
    """返回每次调用都产出 value 的新 async 函数（fake 端点可反复 await）。"""
    async def _wrapper(*args, **kwargs):
        return value
    return _wrapper
