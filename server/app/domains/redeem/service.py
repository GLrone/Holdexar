"""redeem 域服务：Steam CDK 批量激活 + 免费产品领取（store 登录态通道）。

端点均为 Steam 商店页公开的内部接口（激活页/免费许可流程同款），此处
以已绑定 Cookie 的登录态调用，行为与浏览器一致：

- CDK 激活：POST https://store.steampowered.com/account/ajaxregisterkey/
  表单 {product_key, sessionid}。响应 success=1 时 purchase_receipt_info
  里 line_items[0].packageid 即 SubID（前端展示 + SteamDB 链接用）。
  常见错误码：9=已在库中（已拥有）、13=区域锁/不可用、14=速率限制、
  15=无效激活码、24=需补充年龄验证等（详情以 detail 字段原文回传）。
  Steam 官方限制：无论成败，30 分钟窗口内最多 10 次激活。


所有出网走代理策略引擎（proxy_first 默认）；Cookie 三件套含 sessionid。
"""
from __future__ import annotations

import asyncio
import logging

import httpx
from app.core.net import is_ssl_error

from app.core.database import WritePriority, get_session_factory, write_session
from app.core.logging import log_event
from app.domains.account import service as account_service
from app.domains.account.steam_wallet import parse_cookie_str
from app.domains.redeem.models import RAW_MAX, RedeemActivation

logger = logging.getLogger(__name__)

REGISTER_KEY_URL = "https://store.steampowered.com/account/ajaxregisterkey/"

# Steam 官方激活限制：30 分钟窗口内 10 次（无论成败）
ACT_LIMIT = 10
ACT_WINDOW_SECONDS = 30 * 60

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Referer": "https://store.steampowered.com/account/registerkey/",
}

# registerkey 错误码 → 中文语义。
# ️ 该表按社区语义，勿按"官方文档"纠正——53=次数上限（30 分钟 10 次
# 尝试窗口）不是锁区；配额用尽时 Steam 不查 key 直接返 53，任何码都一样。
_ERROR_CODES: dict[int, str] = {
    9: "已拥有",
    13: "地区限制",
    14: "无效激活码",
    15: "重复激活",
    24: "缺少主游戏",
    36: "需要PS3验证",
    50: "这是充值码",
    53: "次数上限（30 分钟内最多 10 次激活尝试，请稍后再试）",
}

# ── 激活次数记账（按账号独立：Steam 的 10 次/30 分钟限制作用于单个账号，
#    换绑 Cookie 即换号重计；非持久化——仅指引前端节奏，Steam 端才是真裁判）──
import time as _time

from app.domains.account.steam_wallet import steam_id_from_cookies

_ACT_LOG: dict[str, list[float]] = {}
"""steamid → 30 分钟窗口内的激活时间戳列表（进程内滑动窗口）。"""


def _account_usage(steamid: str) -> int:
    """该账号当前 30 分钟窗口内已用激活次数（过期时间戳顺手清理）。"""
    now = _time.monotonic()
    stamps = [t for t in _ACT_LOG.get(steamid, []) if now - t < ACT_WINDOW_SECONDS]
    if stamps:
        _ACT_LOG[steamid] = stamps
    else:
        _ACT_LOG.pop(steamid, None)
    return len(stamps)


def _record_activation(steamid: str) -> None:
    """记一次激活尝试（无论成败，Steam 端同款语义）。"""
    _ACT_LOG.setdefault(steamid, []).append(_time.monotonic())


async def _strategy_proxy() -> str | None:
    """按代理策略引擎解析出网代理；引擎不可用时 None（直连）。"""
    from app.domains.proxies.service import strategy_proxy_or_direct

    return await strategy_proxy_or_direct()


async def _login_session() -> tuple[dict, str, str]:
    """取**当前账号**登录 Cookie jar、sessionid 与账号 SteamID；未绑定/缺 sessionid 时 raise ValueError。

    多账号语义：经 account_service.get_cookies() 取当前账号（active）的 Cookie
    ——CDK 页切号即换 Cookie；激活计数按 SteamID 分桶，切号自然重计。
    """
    raw = await account_service.get_cookies()
    jar = parse_cookie_str(raw)
    if "steamLoginSecure" not in jar:
        raise ValueError("尚未绑定 Steam Cookie（请先在「我」页绑定）")
    sessionid = jar.get("sessionid", "")
    if not sessionid:
        raise ValueError("Cookie 中缺少 sessionid，请重新复制完整 Cookie")
    steamid = steam_id_from_cookies(raw) or "unknown"
    return jar, sessionid, steamid


async def _post_steam(url: str, data: dict, jar: dict, *, referer: str | None = None) -> httpx.Response:
    """商店端点 POST：代理优先 + SSL 证书降级重试（经 Clash vless 链路的既有规则）。"""
    headers = dict(_HEADERS)
    if referer:
        headers["Referer"] = referer
    proxy = await _strategy_proxy()
    try:
        async with httpx.AsyncClient(timeout=20, proxy=proxy) as client:
            resp = await client.post(url, data=data, cookies=jar, headers=headers)
    except httpx.HTTPError as e:
        if not is_ssl_error(e):
            raise
        log_event(
            logger,
            "商店端点证书校验失败（经代理场景），降级跳过校验重试",
            tag="降级",
        )
        async with httpx.AsyncClient(timeout=20, proxy=proxy, verify=False) as client:
            resp = await client.post(url, data=data, cookies=jar, headers=headers)
    return resp


def _summary_from_receipt(receipt: dict) -> tuple[str, str]:
    """从 purchase_receipt_info 提取 (subId, subName)。"""
    items = (receipt or {}).get("line_items") or []
    if not items:
        return "", ""
    first = items[0]
    sub_id = str(first.get("packageid") or "")
    sub_name = str(first.get("line_item_description") or "")
    return sub_id, sub_name


def _mask_key(key: str) -> str:
    """激活码日志掩码：留首段与尾 2 位，中段以 * 代替。"""
    if len(key) <= 7:
        return "*" * len(key)
    return f"{key[:5]}{'*' * (len(key) - 7)}{key[-2:]}"


async def _record_ledger(
    *,
    kind: str,
    status: str,
    product_key: str = "",
    subid: str = "",
    sub_name: str = "",
    detail: str = "",
    raw: str = "",
    steamid: str = "",
) -> None:
    """激活结果落激活账本（redeem_activations，成功/已拥有/失败都记）+ 业务日志（key 只留掩码）。"""
    row = RedeemActivation(
        kind=kind,
        status=status,
        product_key=product_key[:120],
        subid=str(subid)[:40],
        sub_name=sub_name[:300],
        detail=detail[:300],
        raw=(raw or "")[:RAW_MAX],
        steamid=str(steamid)[:32],
    )
    async with write_session(WritePriority.INTERACTIVE) as session:
        session.add(row)
        await session.commit()
    verdict = {"ok": "成功", "own": "已拥有"}.get(status, "失败")
    log_event(
        logger,
        f"激活记录：{verdict}",
        tag={"ok": "成功", "own": "信息"}.get(status, "失败"),
        detail={
            "类型": "激活" if kind == "cdk" else "免费领取",
            "激活码": _mask_key(product_key) if product_key else f"SubID {subid}",
            "内容": sub_name[:80],
            "说明": detail[:80],
            "账号": steamid,
        },
    )


async def list_activations(limit: int = 50, offset: int = 0) -> dict:
    """激活历史（时间倒序分页；不含 raw 回执原文，控制载荷）。"""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    from sqlalchemy import func, select

    async with get_session_factory()() as session:
        total = await session.scalar(select(func.count()).select_from(RedeemActivation))
        stmt = (
            select(RedeemActivation)
            .order_by(RedeemActivation.created_at.desc(), RedeemActivation.id.desc())
            .limit(limit)
            .offset(offset)
        )
        rows = (await session.execute(stmt)).scalars().all()
        return {
            "total": int(total or 0),
            "rows": [
                {
                    "id": r.id,
                    "createdAt": r.created_at.isoformat(sep=" ", timespec="seconds") if r.created_at else "",
                    "kind": r.kind,
                    "status": r.status,
                    "key": r.product_key,
                    "subId": r.subid,
                    "subName": r.sub_name,
                    "detail": r.detail,
                    "steamId": r.steamid,
                }
                for r in rows
            ],
        }


async def activate_key(product_key: str) -> dict:
    """激活单个 CDK。返回 {status: ok|own|fail, detail, subId, subName, raw}。

    - 成功：status=ok，subId/subName 来自回执 line_items；
    - 错误码 9：status=own（已拥有，前端单列统计）；
    - 失败回执若带 purchase_receipt_info.line_items（已拥有/重复激活等常见形态），
      同样提取 subId/subName 展示（失败回执也取
      line_items[0].packageid + line_item_description，此前只认成功回执是遗漏）；
    - raw：Steam 返回原文（JSON 序列化），供前端"展开原文"核对；
    - 每次尝试记入账号维度 30 分钟计数（Steam 端才是真裁判，本端只做节奏指引）；
    - 结果无论成败落激活账本（redeem_activations）一行；传输层失败同样记 fail
      （Steam 未收到的不计激活尝试次数）。
    """
    jar, sessionid, steamid = await _login_session()
    resp: httpx.Response | None
    try:
        resp = await _post_steam(
            REGISTER_KEY_URL, {"product_key": product_key, "sessionid": sessionid}, jar
        )
    except httpx.HTTPError as e:
        resp = None
        result: dict = {"status": "fail", "detail": f"激活请求失败：{e}",
                        "subId": "", "subName": "", "raw": ""}

    if resp is not None:
        _record_activation(steamid)

        raw_text = ""
        try:
            raw_text = resp.text[:2000]
        except Exception:  # noqa: BLE001
            pass

        if resp.status_code == 401:
            result = {"status": "fail", "detail": "Cookie 已失效，请重新绑定",
                      "subId": "", "subName": "", "raw": raw_text}
        elif resp.status_code == 429:
            result = {"status": "fail", "detail": _ERROR_CODES[14], "subId": "", "subName": "",
                      "raw": raw_text}
        else:
            try:
                data = resp.json()
            except ValueError:
                # store 页面层可能对脚本客户端返回登录页 HTML（钱包模块同款行为），
                # 或节点级 429 页面——都归为"页面层未认登录态"，指引换节点/稍后再试。
                if resp.status_code == 429:
                    result = {"status": "fail", "detail": "激活节点被限流（429），请稍后再试或切换 Clash 节点",
                              "subId": "", "subName": "", "raw": raw_text}
                else:
                    result = {"status": "fail",
                              "detail": "Steam 页面层未认登录态（返回非 JSON）——可能节点被限流或会话校验拒绝，"
                                        "请切换 Clash 节点重试；持续失败参考「完整账单」同款页面层限制",
                              "subId": "", "subName": "", "raw": raw_text}
            else:
                if data.get("success") == 1:
                    sub_id, sub_name = _summary_from_receipt(data.get("purchase_receipt_info") or {})
                    result = {"status": "ok", "detail": "激活成功", "subId": sub_id, "subName": sub_name, "raw": raw_text}
                else:
                    # 错误码永远取 purchase_result_details（真 key 回执里 success=2 是"失败"标记
                    # 不是错误码——{"success":2, "purchase_result_details":53}；只判
                    # success==1，错误分类全靠 purchase_result_details）。receipt.result_detail 兜底。
                    detail = data.get("purchase_result_details")
                    receipt = data.get("purchase_receipt_info") or {}
                    code = 0
                    try:
                        code = int(detail)
                    except (TypeError, ValueError):
                        try:
                            code = int(receipt.get("result_detail") or 0)
                        except (TypeError, ValueError):
                            code = 0
                    # 失败/已拥有回执若带 line_items（失败分支同样提取），一并展示
                    # subId/版本名——"已拥有"能看到是哪个 sub 是原脚本一直有的信息。
                    sub_id, sub_name = _summary_from_receipt(receipt)
                    if code == 9:
                        result = {"status": "own", "detail": "已拥有", "subId": sub_id, "subName": sub_name, "raw": raw_text}
                    else:
                        zh = _ERROR_CODES.get(code)
                        text = zh or (f"激活失败（错误码 {code}）" if code else str(detail)[:120] or "激活失败")
                        result = {"status": "fail", "detail": text, "subId": sub_id, "subName": sub_name, "raw": raw_text}

    await _record_ledger(
        kind="cdk",
        status=result["status"],
        product_key=product_key,
        subid=result["subId"],
        sub_name=result["subName"],
        detail=result["detail"],
        raw=result["raw"],
        steamid=steamid,
    )
    return result


async def activate_batch(keys: list[str]) -> dict:
    """批量激活（并发）。同批 9 个同时提交（ajax 并发），
    批间 20s 节奏由前端驱动（Steam 30 分钟 10 次限制由 Steam 端强制 + 前端计数）。

    单批 >9 时仍按 9 一组并发、组间串行（防误操作一把梭 50 个 key 打爆限额）。
    """
    if not keys:
        raise ValueError("激活码列表为空")

    results: list[dict] = []
    ok = own = fail = 0
    for start in range(0, len(keys), 9):
        chunk = keys[start:start + 9]
        chunk_results = await asyncio.gather(
            *(activate_key(k) for k in chunk)
        )
        for key, r in zip(chunk, chunk_results):
            r = {"code": key, **r}
            results.append(r)
            if r["status"] == "ok":
                ok += 1
            elif r["status"] == "own":
                own += 1
            else:
                fail += 1
    return {"results": results, "ok": ok, "own": own, "fail": fail}


async def quota_status() -> dict:
    """激活前提条件（Cookie 是否可用）——供前端在激活前给引导提示。

    附账号维度的激活计数（used/limit/steamid）：Steam 的 10 次/30 分钟限制
    作用于单个账号，切换绑定 Cookie（换号）即重计——前端以此驱动节奏 UI，
    不能用全局计数（跨账号共享计数是错误语义）。
    """
    raw = await account_service.get_cookies()
    jar = parse_cookie_str(raw)
    has_cookie = "steamLoginSecure" in jar
    steamid = steam_id_from_cookies(raw) if has_cookie else ""
    return {
        "hasCookie": has_cookie,
        "hasSessionId": has_cookie and "sessionid" in jar,
        "steamId": steamid,
        "used": _account_usage(steamid) if steamid else 0,
        "limit": ACT_LIMIT,
    }
