"""bills 域服务：导入 + 汇率换算 + 统计口径 + 查询。

「实际消耗金额」口径：
- 游戏交易按**交易当日**汇率（rates 域统一历史出口的 observed 行，最多回溯
  15 天）折 CNY；
- 档案缺失（币种/日期无行）则 cny_fen 置空：不入任何总额，明细行标「汇率缺失」
  ——绝不用当前汇率快照折历史交易（隐性错误）；
- carried（历史 forward-fill 延续值）不是真实观测，不参与折算；
- 净支出 = 购买总额 - 退款合计（退款 sign=-1 冲回）；
- 赠礼额度 = 自购净额 - 送出礼物净额（退款归属 orig_is_gift 区分自购/送礼退款）；
- 钱包充值单独成流水（不入游戏支出，防双计）；账户价值 = 自购净额 + CDK 手动计价合计。

历史汇率修复（rates.history.repair_history_gaps）落库后会回调
`revalue_affected`：受影响交易重算 fx_rate/cny_fen + 汇总重算，账单不会
永久固化在首次导入时的错误汇率上。
"""
from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

from sqlalchemy import case, delete, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.database import get_session_factory
from app.crawler.utils import get_beijing_time_obj
from .models import BillCdkGame, BillGameTx, BillImport, BillTopupTx
from .parser import parse_report

logger = logging.getLogger(__name__)


def _fen(cny: float) -> int:
    """元 → 分（四舍五入，负数同样朝远离零取整——与 round 半偶无关的稳定口径）。"""
    return int(round(cny * 100))


async def _rate_for(session, currency: str, day: str, series_cache: dict) -> float | None:
    """汇率解析（observed 才有效）：CNY=1；其余当日 → 最多回溯 15 天。

    series_cache 按（币种, 年份）键控——同币种跨年交易各自加载对应年份窗口，
    窗口起点提前到上一年 12 月以覆盖跨年回溯。查不到 → None（明示缺失，不兜底）。
    数据经 rates 域统一历史出口获取（carried 行不参与）。
    """
    if currency == "CNY":
        return 1.0
    from app.domains.rates import history as rates_history

    year = day[:4]
    cache_key = (currency, year)
    if cache_key not in series_cache:
        series_cache[cache_key] = await rates_history.load_observed_series(
            session,
            currency,
            date(int(year) - 1, 12, 1),
            date(int(year), 12, 31),
        )
    return rates_history.pick_observed_rate(series_cache[cache_key], currency, day)


def _fx_note(tx: dict) -> str:
    notes = []
    if tx.get("tx_type") == "游戏内购买":
        notes.append("内购")
    if tx.get("is_refund"):
        notes.append("退款")
    if tx.get("is_gift"):
        notes.append("礼物")
    return " | ".join(notes)


async def revalue_affected(touched: dict[str, set[date]]) -> dict:
    """历史汇率修复后重估受影响账单：行级 fx_rate/cny_fen 重算 + 汇总重算。

    touched = {币种: {被修复覆盖的日期}}。受影响交易 = 币种命中且交易日期落在
    [覆盖起点 - 15 天回溯窗, 覆盖终点]；用与导入同一规则重算（observed 精确日
    → 回溯 ≤ 15 天）——修复前落到 carried/缺失的交易在修复后自动获得真值，
    修复改变了值的交易自动纠偏。汇总按全表现值重算（同一账单的其余行参与总额）。
    幂等：重算结果只取决于当前 observed 数据。
    """
    if not touched:
        return {"bills": 0, "transactions": 0}
    currencies = sorted({str(c).upper() for c in touched})
    all_days = [d for days in touched.values() for d in days]
    lookback_start = (min(all_days) - timedelta(days=15)).isoformat()
    span_end = max(all_days).isoformat()

    async with get_session_factory()() as session:
        game_rows = (
            await session.execute(
                select(BillGameTx).where(
                    BillGameTx.currency.in_(currencies),
                    BillGameTx.date >= lookback_start,
                    BillGameTx.date <= span_end,
                )
            )
        ).scalars().all()
        topup_rows = (
            await session.execute(
                select(BillTopupTx).where(
                    BillTopupTx.currency.in_(currencies),
                    BillTopupTx.date >= lookback_start,
                    BillTopupTx.date <= span_end,
                )
            )
        ).scalars().all()
        if not game_rows and not topup_rows:
            return {"bills": 0, "transactions": 0}

        series_cache: dict = {}
        changed = 0
        import_ids: set[int] = set()
        for row in (*game_rows, *topup_rows):
            rate = await _rate_for(session, row.currency, row.date, series_cache)
            new_fen = None if rate is None else _fen(row.amount * row.sign * rate)
            if rate != row.fx_rate or new_fen != row.cny_fen:
                row.fx_rate = rate
                row.cny_fen = new_fen
                changed += 1
            import_ids.add(int(row.import_id))

        for imp_id in sorted(import_ids):
            agg = (
                await session.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                case(
                                    (BillGameTx.is_refund.is_(False),
                                     func.coalesce(BillGameTx.cny_fen, 0)),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(
                                case(
                                    (BillGameTx.is_refund.is_(True),
                                     func.abs(func.coalesce(BillGameTx.cny_fen, 0))),
                                    else_=0,
                                )
                            ),
                            0,
                        ),
                        func.coalesce(
                            func.sum(case((BillGameTx.cny_fen.is_(None), 1), else_=0)), 0
                        ),
                        func.coalesce(
                            func.sum(case((BillGameTx.is_refund.is_(False), 1), else_=0)), 0
                        ),
                    ).where(BillGameTx.import_id == imp_id)
                )
            ).one()
            spend, refund, fx_missing, orders = (int(v or 0) for v in agg)
            imp = await session.get(BillImport, imp_id)
            if imp is None:
                continue
            imp.game_spend_fen = spend
            imp.game_refund_fen = refund
            imp.game_net_fen = spend - refund
            imp.fx_missing = fx_missing
            imp.orders = orders
        await session.commit()

    logger.info(
        "账单历史汇率重估：覆盖币种 %s，重算 %d 笔交易 / %d 个账单",
        ",".join(currencies), changed, len(import_ids),
    )
    return {"bills": len(import_ids), "transactions": changed}


async def import_report(data: dict, source_file: str = "") -> dict:
    """解析 + 换算 + 落库一份 Steam_Report_*.json。返回导入摘要。

    同昵称的旧账单会被覆盖（外部工具导出的是全量历史快照，新导入天然取代旧账）。
    """
    parsed = parse_report(data)

    async with get_session_factory()() as session:
        # 同昵称旧账单替换（昵称为空时跳过——避免误清其他匿名账单）
        nickname = parsed["account"]["nickname"]
        replaced = 0
        if nickname:
            old_ids = [
                r[0]
                for r in (
                    await session.execute(select(BillImport.id).where(BillImport.nickname == nickname))
                ).fetchall()
            ]
            if old_ids:
                for model in (BillGameTx, BillTopupTx, BillCdkGame):
                    await session.execute(delete(model).where(model.import_id.in_(old_ids)))
                await session.execute(delete(BillImport).where(BillImport.id.in_(old_ids)))
                replaced = len(old_ids)

        now = get_beijing_time_obj().replace(tzinfo=None)
        imp = BillImport(
            nickname=parsed["account"]["nickname"],
            avatar_base64=parsed["account"]["avatar_base64"],
            source_file=source_file,
            imported_at=now,
            warnings_json=json.dumps(parsed["warnings"], ensure_ascii=False),
        )
        session.add(imp)
        await session.flush()  # 拿 import_id

        # 涉及的币种 → 各自年份的日线档案一次性预载（series_cache 复用）
        series_cache: dict[str, list[tuple[str, float]]] = {}

        fx_missing = 0
        topup_fx_missing = 0
        spend_fen = 0
        refund_fen = 0
        for tx in parsed["game_txs"]:
            rate = await _rate_for(session, tx["currency"], tx["date"], series_cache)
            if rate is None:
                fx_missing += 1
                cny_fen = None
            else:
                cny = tx["amount"] * tx["sign"] * rate
                cny_fen = _fen(cny)
                if tx["is_refund"]:
                    refund_fen += abs(cny_fen)
                else:
                    spend_fen += cny_fen
            session.add(
                BillGameTx(
                    import_id=imp.id,
                    date=tx["date"],
                    tx_type=tx["tx_type"],
                    items_json=json.dumps(tx["items"], ensure_ascii=False),
                    currency=tx["currency"],
                    amount=tx["amount"],
                    sign=tx["sign"],
                    cny_fen=cny_fen,
                    fx_rate=rate,
                    fx_note=_fx_note(tx),
                    payment=tx.get("payment", ""),
                    discount_pct=tx.get("discount_pct") or "",
                    orig_price=tx.get("orig_price"),
                    wallet_balance=tx.get("wallet_balance") or "",
                    base_price=tx.get("base_price") or "",
                    payment_parts_json=(
                        json.dumps(tx["payment_parts"], ensure_ascii=False)
                        if tx.get("payment_parts")
                        else None
                    ),
                    gift_to_json=(
                        json.dumps(tx["gift_recipients"], ensure_ascii=False)
                        if tx.get("gift_recipients")
                        else None
                    ),
                    is_gift=tx["is_gift"],
                    is_refund=tx["is_refund"],
                    orig_is_gift=tx["orig_is_gift"],
                    raw_idx=tx.get("raw_idx", 0),
                )
            )

        topup_spend_fen = 0
        topup_refund_fen = 0
        for tx in parsed["topup_txs"]:
            rate = await _rate_for(session, tx["currency"], tx["date"], series_cache)
            cny_fen = None if rate is None else _fen(tx["amount"] * tx["sign"] * rate)
            if rate is None:
                topup_fx_missing += 1
            elif tx["is_refund"]:
                topup_refund_fen += abs(cny_fen or 0)
            else:
                topup_spend_fen += cny_fen or 0
            session.add(
                BillTopupTx(
                    import_id=imp.id,
                    date=tx["date"],
                    desc=tx["desc"],
                    currency=tx["currency"],
                    amount=tx["amount"],
                    sign=tx["sign"],
                    cny_fen=cny_fen,
                    fx_rate=rate,
                    fx_note="退款" if tx["is_refund"] else "",
                    payment=tx.get("payment", ""),
                    tx_type=tx["tx_type"],
                    is_refund=tx["is_refund"],
                )
            )

        for game in parsed["cdk_games"]:
            session.add(
                BillCdkGame(
                    import_id=imp.id,
                    name=game["name"],
                    appid=game.get("appid"),
                    date=game["date"],
                    acq=game["acq"],
                    acq_label=game["acq_label"],
                )
            )

        imp.game_net_fen = spend_fen - refund_fen
        imp.game_spend_fen = spend_fen
        imp.game_refund_fen = refund_fen
        imp.orders = sum(1 for t in parsed["game_txs"] if not t["is_refund"])
        imp.fx_missing = fx_missing
        await session.commit()

    logger.info(
        "账单导入完成：%s 游戏 %d 笔 / 充值 %d 笔 / CDK %d 个（汇率缺失 %d+=%d，覆盖旧账 %d 份）",
        parsed["account"]["nickname"],
        len(parsed["game_txs"]),
        len(parsed["topup_txs"]),
        len(parsed["cdk_games"]),
        fx_missing,
        topup_fx_missing,
        replaced,
    )
    return {
        "importId": imp.id,
        "nickname": imp.nickname,
        "gameTxs": len(parsed["game_txs"]),
        "topupTxs": len(parsed["topup_txs"]),
        "cdkGames": len(parsed["cdk_games"]),
        "fxMissing": fx_missing + topup_fx_missing,
        "warnings": parsed["warnings"],
        "replaced": replaced,
    }


# 同步状态快照（settings 键）——前端展示「自动同步」最近一次结果
KEY_BILLS_SYNC = "bills.sync_snapshot"

# 首屏探测结果键（settings 键）：True=上轮探测无新交易跳过。有新交易时清除
KEY_BILLS_PROBE_SKIPPED = "bills.probe_skipped"


async def _latest_known_tx_date() -> str | None:
    """库内最新一笔交易日期（YYYY-MM-DD；无任何账单返回 None）。

    history 页倒序，首行即 Steam 侧最新——与库内最新比对即可判断
    是否值得跑全量翻页（首屏探测的比对基准）。
    """
    async with get_session_factory()() as session:
        for model in (BillGameTx, BillTopupTx, BillCdkGame):
            row = (
                await session.execute(
                    select(model.date).order_by(model.date.desc()).limit(1)
                )
            ).first()
            if row and row[0]:
                return row[0]
    return None


# 陈旧锁判定：同步周期 30min（调度器 bills_sync interval），超过即视为
# 上次进程中途被杀留下的死锁——running 永久卡死会让后续所有定时触发
# 都被判 busy 跳过（本地服务频繁重启时的实际故障模式）。
_BILLS_SYNC_STALE_AFTER = timedelta(minutes=30)


async def sync_bills(*, force: bool = False) -> dict:
    """从 Steam 在线拉全量账单 + 许可并落库（后端常驻在线拉取）。

    未绑 Cookie / 无变化静默跳过（force=False 供定时任务）；
    force=True 供手动同步与 Cookie 绑定后首拉。
    同步失败不重试：错误写回快照，等下一个调度周期再拉。

    首屏探测（force=False 时）：history 页倒序，首行即 Steam 侧最新交易
    ——与库内最新日期比对，无新交易则整轮跳过（数十页全量翻页缩成
    1 个 GET）。探测失败/无法判断时保守全量。force=True 永远全量。
    """
    from app.domains.account import service as account_service
    from app.domains.settings import service as settings_service
    from . import steam_fetch

    # 多账号语义：账单跟随**主账号**（第一个绑定的）——bills 表无 steamid
    # 维度，跟随切换会混流两份账单。主账号不存在时回退当前账号（单账号等价）。
    cookies = await account_service.get_primary_cookies()
    if not cookies or "steamLoginSecure" not in cookies:
        cookies = await account_service.get_cookies()
    if not cookies or "steamLoginSecure" not in cookies:
        return {"ok": False, "status": "no_cookie"}

    snapshot: dict | None = await settings_service.get_value(KEY_BILLS_SYNC, None)
    now = get_beijing_time_obj().replace(tzinfo=None)
    if not force and snapshot and snapshot.get("running"):
        # running 卡死超过一个同步周期 → 进程中途被杀的死锁，放行重试；
        # 周期内则保持 busy 语义（上一轮还在跑，不叠加第二份全量翻页）。
        since = snapshot.get("since")
        stale = False
        if since:
            try:
                since_dt = datetime.fromisoformat(since)
                stale = (now - since_dt) > _BILLS_SYNC_STALE_AFTER
            except ValueError:
                stale = True
        else:
            stale = True
        if not stale:
            return {"ok": False, "status": "busy"}

    # 首屏探测：无新交易 → 本轮跳过（写探测跳过快照，等下一周期再探）
    if not force:
        try:
            has_new = await steam_fetch.probe_new_transactions(
                cookies, known_latest_date=await _latest_known_tx_date(),
                proxy_url=await account_service._strategy_proxy(),
            )
        except steam_fetch.SteamFetchError as exc:
            # Cookie 失效等硬错误：写错误快照（与全量失败同语义），等下一轮
            await settings_service.set_value(KEY_BILLS_SYNC, {
                "running": False, "error": str(exc), "syncedAt": now.isoformat(),
            })
            return {"ok": False, "status": "error", "error": str(exc)}
        if not has_new:
            await settings_service.set_value(KEY_BILLS_SYNC, {
                "running": False, "probedSkip": True, "syncedAt": now.isoformat(),
            })
            await settings_service.set_value(KEY_BILLS_PROBE_SKIPPED, True)
            return {"ok": True, "status": "probed_skip", "historyRows": 0, "licenseRows": 0}
        await settings_service.set_value(KEY_BILLS_PROBE_SKIPPED, False)

    await settings_service.set_value(KEY_BILLS_SYNC, {"running": True, "since": now.isoformat()})

    async def _progress(info: dict) -> None:
        # 阶段进度写进快照 KV：GET /bills/sync 的轮询方（前端进度气泡）实时可读；
        # 写失败只丢进度显示，不影响同步本体（_emit_progress 已兜一层，双保险）。
        try:
            await settings_service.set_value(
                KEY_BILLS_SYNC, {"running": True, "since": now.isoformat(), **info}
            )
        except Exception:  # noqa: BLE001
            logger.debug("账单同步进度写入失败", exc_info=True)

    try:
        report = await steam_fetch.fetch_full_report(
            cookies, proxy_url=await account_service._strategy_proxy(),
            on_progress=_progress,
        )
        # 空报告门禁：0 账单 + 0 许可说明解析层根本没拿到数据（登录页守门
        # 漏网/Steam 改版清空表格），落库会生成空 import 遮住真实旧快照。
        # 属失败语义：写错误快照等下一轮，DB 零接触。
        if not report.history and not report.licenses:
            raise steam_fetch.SteamFetchError(
                "抓取结果为空（0 账单 + 0 许可），拒落空快照——疑似登录态失效或 Steam 页面改版"
            )
        await _progress({"stage": "import"})
        result = await import_report(
            report.to_report_json(), source_file=f"auto-sync {now.strftime('%Y-%m-%d')}"
        )
        result.update(
            {
                "ok": True,
                "historyRows": len(report.history),
                "licenseRows": len(report.licenses),
            }
        )
        snapshot = {**result, "running": False, "syncedAt": now.isoformat()}
        await settings_service.set_value(KEY_BILLS_SYNC, snapshot)
        return result
    except Exception as exc:  # noqa: BLE001
        logger.warning("账单自动同步失败：%s", exc)
        error_snapshot = {
            "ok": False,
            "running": False,
            "error": str(exc),
            "syncedAt": now.isoformat(),
        }
        await settings_service.set_value(KEY_BILLS_SYNC, error_snapshot)
        return {"ok": False, "status": "error", "error": str(exc)}


async def get_sync_snapshot() -> dict:
    """最近一次自动同步状态（running / 结果 / 错误）。"""
    from app.domains.settings import service as settings_service

    snap = await settings_service.get_value(KEY_BILLS_SYNC, None) or {}
    return {**snap, "running": bool(snap.get("running"))}


async def list_imports() -> list[dict]:
    """已导入账单列表（新→旧）。"""
    async with get_session_factory()() as session:
        rows = (await session.execute(select(BillImport).order_by(BillImport.id.desc()))).scalars().all()
    return [
        {
            "id": r.id,
            "nickname": r.nickname,
            "sourceFile": r.source_file,
            "importedAt": r.imported_at.isoformat() if r.imported_at else None,
            "gameNetFen": r.game_net_fen,
            "gameSpendFen": r.game_spend_fen,
            "gameRefundFen": r.game_refund_fen,
            "orders": r.orders,
            "fxMissing": r.fx_missing,
        }
        for r in rows
    ]


async def delete_import(import_id: int) -> dict:
    """删除一份账单及其全部行级数据。"""
    async with get_session_factory()() as session:
        for model in (BillGameTx, BillTopupTx, BillCdkGame):
            await session.execute(delete(model).where(model.import_id == import_id))
        res = await session.execute(delete(BillImport).where(BillImport.id == import_id))
        await session.commit()
        return {"deleted": (res.rowcount or 0) > 0, "importId": import_id}


def _tx_to_dict(r) -> dict:
    return {
        "id": r.id,
        "date": r.date,
        "txType": r.tx_type,
        "items": json.loads(r.items_json or "[]"),
        "currency": r.currency,
        "amount": r.amount,
        "sign": r.sign,
        "cnyFen": r.cny_fen,
        "fxRate": r.fx_rate,
        "fxNote": r.fx_note,
        "payment": r.payment,
        "discountPct": r.discount_pct,
        "origPrice": r.orig_price,
        "isGift": r.is_gift,
        "isRefund": r.is_refund,
        "origIsGift": r.orig_is_gift,
    }


def _topup_to_dict(r) -> dict:
    return {
        "id": r.id,
        "date": r.date,
        "desc": r.desc,
        "currency": r.currency,
        "amount": r.amount,
        "sign": r.sign,
        "cnyFen": r.cny_fen,
        "fxRate": r.fx_rate,
        "payment": r.payment,
        "txType": r.tx_type,
        "isRefund": r.is_refund,
    }


def _cdk_to_dict(r) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "appid": r.appid,
        "date": r.date,
        "acq": r.acq,
        "acqLabel": r.acq_label,
        "manualFen": r.manual_fen,
    }


def _calc_quota(game_rows: list[dict]) -> dict:
    """赠礼额度口径（对齐 calc_gift_quota）：自购净额 - 送出礼物净额。"""
    buy_total = buy_refund = gift_total = gift_refund = 0
    for tx in game_rows:
        fen = tx["cnyFen"]
        if fen is None:
            continue
        if tx["isRefund"]:
            if tx["origIsGift"]:
                gift_refund += abs(fen)
            else:
                buy_refund += abs(fen)
        else:
            if tx["isGift"]:
                gift_total += fen
            else:
                buy_total += fen
    self_net = buy_total - buy_refund
    gift_net = gift_total - gift_refund
    return {
        "buyTotalFen": buy_total,
        "buyRefundFen": buy_refund,
        "giftTotalFen": gift_total,
        "giftRefundFen": gift_refund,
        "selfNetFen": self_net,
        "giftNetFen": gift_net,
        "quotaFen": self_net - gift_net,
    }


async def overview(import_id: int) -> dict:
    """总览：账户 + 全部口径 + 年度分组 + 月度序列（图表用）。"""
    async with get_session_factory()() as session:
        imp = await session.get(BillImport, import_id)
        if imp is None:
            raise KeyError(f"账单 {import_id} 不存在")
        game_rows = (
            await session.execute(
                select(BillGameTx).where(BillGameTx.import_id == import_id).order_by(BillGameTx.date.desc(), BillGameTx.id)
            )
        ).scalars().all()
        topup_rows = (
            await session.execute(
                select(BillTopupTx).where(BillTopupTx.import_id == import_id).order_by(BillTopupTx.date.desc(), BillTopupTx.id)
            )
        ).scalars().all()
        cdk_rows = (
            await session.execute(
                select(BillCdkGame).where(BillCdkGame.import_id == import_id).order_by(BillCdkGame.date.desc(), BillCdkGame.id)
            )
        ).scalars().all()

        avatar = imp.avatar_base64
        if avatar and not avatar.startswith("data:"):
            avatar = f"data:image/png;base64,{avatar}"

        game_dicts = [_tx_to_dict(r) for r in game_rows]
        topup_dicts = [_topup_to_dict(r) for r in topup_rows]
        cdk_dicts = [_cdk_to_dict(r) for r in cdk_rows]

        # ── 年度/月度分组 ──
        years: dict[str, dict] = {}
        months: dict[str, dict] = {}
        for tx in game_dicts:
            fen = tx["cnyFen"]
            y = tx["date"][:4]
            m = tx["date"][:7]
            years.setdefault(y, {"netFen": 0, "spendFen": 0, "refundFen": 0, "count": 0, "months": {}})
            months.setdefault(m, {"netFen": 0, "spendFen": 0, "refundFen": 0, "count": 0})
            yst = years[y]
            mst = months[m]
            if fen is not None:
                yst["netFen"] += fen
                mst["netFen"] += fen
                if tx["isRefund"]:
                    yst["refundFen"] += abs(fen)
                    mst["refundFen"] += abs(fen)
                else:
                    yst["spendFen"] += fen
                    mst["spendFen"] += fen
            yst["count"] += 1
            mst["count"] += 1
            # 年内月度细分
            ym = yst["months"].setdefault(m[5:7], {"netFen": 0, "count": 0})
            if fen is not None:
                ym["netFen"] += fen
            ym["count"] += 1

        # 月度序列（升序，给图表）
        month_series = [
            {"month": m, **stats} for m, stats in sorted(months.items())
        ]
        year_list = [
            {"year": y, **{k: v for k, v in stats.items() if k != "months"}, "months": stats["months"]}
            for y, stats in sorted(years.items(), reverse=True)
        ]

        # ── 充值口径 ──
        topup_spend = sum(t["cnyFen"] for t in topup_dicts if t["cnyFen"] is not None and not t["isRefund"])
        topup_refund = sum(abs(t["cnyFen"]) for t in topup_dicts if t["cnyFen"] is not None and t["isRefund"])
        topup_by_currency: dict[str, dict] = {}
        for t in topup_dicts:
            c = topup_by_currency.setdefault(t["currency"], {"total": 0.0, "cnyFen": 0, "count": 0})
            c["total"] += t["amount"] * t["sign"]
            c["count"] += 1
            if t["cnyFen"] is not None:
                c["cnyFen"] += t["cnyFen"]

        # ── CDK / 礼物口径（free 行只展示不计价，不进任何金额）──
        cdk_priced = [
            c for c in cdk_dicts if c["manualFen"] is not None and c["acq"] != "free"
        ]
        cdk_total_fen = sum(c["manualFen"] or 0 for c in cdk_priced)

        quota = _calc_quota(game_dicts)
        self_net_fen = quota["selfNetFen"]
        account_value_fen = self_net_fen + cdk_total_fen

        valid = [t for t in game_dicts if t["cnyFen"] is not None]
        spend_fen = sum(t["cnyFen"] for t in valid if not t["isRefund"])
        refund_fen = sum(abs(t["cnyFen"]) for t in valid if t["isRefund"])

        return {
            "id": imp.id,
            "nickname": imp.nickname,
            "avatar": avatar,
            "sourceFile": imp.source_file,
            "importedAt": imp.imported_at.isoformat() if imp.imported_at else None,
            "warnings": json.loads(imp.warnings_json or "[]"),
            "summary": {
                "netFen": spend_fen - refund_fen,
                "spendFen": spend_fen,
                "refundFen": refund_fen,
                "orders": sum(1 for t in game_dicts if not t["isRefund"]),
                "txCount": len(game_dicts),
                "fxMissing": imp.fx_missing,
                **quota,
                "accountValueFen": account_value_fen,
                "cdkPriced": len(cdk_priced),
                "cdkTotalFen": cdk_total_fen,
                "topupNetFen": topup_spend - topup_refund,
                "topupSpendFen": topup_spend,
                "topupRefundFen": topup_refund,
            },
            "years": year_list,
            "monthSeries": month_series,
            "topupByCurrency": [
                {"currency": cur, **stats} for cur, stats in sorted(topup_by_currency.items())
            ],
            "counts": {
                "gameTxs": len(game_dicts),
                "topupTxs": len(topup_dicts),
                "cdkGames": len(cdk_dicts),
                "cdk": sum(1 for c in cdk_dicts if c["acq"] == "cdk"),
                "gift": sum(1 for c in cdk_dicts if c["acq"] == "gift"),
                "free": sum(1 for c in cdk_dicts if c["acq"] == "free"),
            },
        }


async def game_txs(
    import_id: int,
    year: str | None = None,
    month: str | None = None,
    tx_type: str | None = None,
    search: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    """游戏交易明细分页（含类型/年月/搜索过滤）。"""
    stmt = select(BillGameTx).where(BillGameTx.import_id == import_id)
    if year:
        stmt = stmt.where(BillGameTx.date.startswith(year))
    if month:
        stmt = stmt.where(BillGameTx.date.startswith(month))
    if tx_type:
        stmt = stmt.where(BillGameTx.tx_type == tx_type)
    if search:
        stmt = stmt.where(BillGameTx.items_json.contains(search))
    rows_stmt = stmt.order_by(BillGameTx.date.desc(), BillGameTx.id.desc()).limit(limit).offset(offset)
    async with get_session_factory()() as session:
        rows = (await session.execute(rows_stmt)).scalars().all()
        # HAVING 语义计数：SQLite 无 contains 过滤后的 count 便利，直接复用 stmt
        from sqlalchemy import func

        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
    return {
        "total": total,
        "rows": [_tx_to_dict(r) for r in rows],
        "limit": limit,
        "offset": offset,
    }


async def topup_txs(import_id: int, limit: int = 200, offset: int = 0) -> dict:
    """充值流水分页。"""
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                select(BillTopupTx)
                .where(BillTopupTx.import_id == import_id)
                .order_by(BillTopupTx.date.desc(), BillTopupTx.id.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        from sqlalchemy import func

        total = (
            await session.execute(
                select(func.count())
                .select_from(BillTopupTx)
                .where(BillTopupTx.import_id == import_id)
            )
        ).scalar_one()
    return {"total": total, "rows": [_topup_to_dict(r) for r in rows], "limit": limit, "offset": offset}


async def cdk_games(
    import_id: int, acq: str | None = None, search: str | None = None, limit: int = 500, offset: int = 0
) -> dict:
    """CDK/礼物/免费入库列表（手动计价；free 行只展示）。"""
    stmt = select(BillCdkGame).where(BillCdkGame.import_id == import_id)
    if acq:
        stmt = stmt.where(BillCdkGame.acq == acq)
    if search:
        stmt = stmt.where(BillCdkGame.name.contains(search))
    async with get_session_factory()() as session:
        rows = (
            await session.execute(
                stmt.order_by(BillCdkGame.date.desc(), BillCdkGame.id.desc()).limit(limit).offset(offset)
            )
        ).scalars().all()
        from sqlalchemy import func

        total = (
            await session.execute(select(func.count()).select_from(stmt.subquery()))
        ).scalar_one()
    return {"total": total, "rows": [_cdk_to_dict(r) for r in rows], "limit": limit, "offset": offset}


async def set_cdk_price(cdk_id: int, manual_fen: int | None) -> dict:
    """录入/清除一个 CDK/礼物的手动实付价（分）。"""
    async with get_session_factory()() as session:
        row = await session.get(BillCdkGame, cdk_id)
        if row is None:
            raise KeyError(f"CDK 条目 {cdk_id} 不存在")
        row.manual_fen = manual_fen
        await session.commit()
        return {"id": row.id, "name": row.name, "manualFen": row.manual_fen}
