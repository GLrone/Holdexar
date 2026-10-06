"""系统域：健康检查 / 运行信息 / 数据导出 / 旧库导入 / 数据备份 / 运行日志 / 应用更新。"""
from __future__ import annotations

import asyncio
import json
import platform
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.app_info import GITHUB_REPO
from app.core import data_export, keyring, rekey, secretbox
from app.core.keyring import KeyringError
from app.core.secretbox import SecretBoxError
from app.core.backup import (
    create_backup as create_backup_impl,
    list_backups as list_backups_impl,
    restore_backup as restore_backup_impl,
    verify_backup as verify_backup_impl,
    safe_backup_path,
    backup_dir,
)
from app.core.config import get_settings
from app.core.database import (
    SCHEMA_VERSION,
    WritePriority,
    get_session_factory,
    write_scheduler_diagnostics,
)
from app.core.database import write_gate
from app.core.logging import ring_log_handler
from app.domains.games.models import Game, GameCurrentPrice, GamePriceHistory

router = APIRouter(tags=["system"])

_STARTED_AT = time.time()


@router.get("/health")
async def health() -> dict:
    settings = get_settings()
    return {"status": "ok", "app": settings.app_name, "version": settings.version}


@router.get("/info")
async def info() -> dict:
    settings = get_settings()
    return {
        "app": settings.app_name,
        "version": settings.version,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "data_dir": str(settings.data_dir),
        "web_dist_ready": settings.web_dist_dir.exists(),
        "uptime_seconds": round(time.time() - _STARTED_AT, 1),
        # 发布仓库（owner/repo）：前端拼发布页/问题反馈链接的唯一来源
        "repo": GITHUB_REPO,
    }


@router.get("/system/write-scheduler")
async def write_scheduler() -> dict:
    """写调度器诊断：谁在持闸、谁在排队、等待/持闸耗时指标。

    排障第③层出口（前端消费归运行控制面阶段七）：写事务被堵时取证用，
    回答「是谁在堵、堵了多久」，不进用户面主流程。
    """
    return write_scheduler_diagnostics()


class LegacyImport(BaseModel):
    path: str  # 旧爬虫 steam-price.db 路径


@router.post("/import-legacy")
async def import_legacy(req: LegacyImport) -> dict:
    """从旧爬虫 SQLite（steam_games / steam_prices）导入最新快照。

    current 表并入 (appid, region) 最新快照；history 整段追加。
    同步阻塞操作，FastAPI 线程池执行。
    """
    src = Path(req.path)
    if not src.is_file():
        raise HTTPException(status_code=404, detail=f"文件不存在: {src}")

    conn = sqlite3.connect(str(src))
    conn.row_factory = sqlite3.Row
    try:
        games = conn.execute("SELECT appid, name FROM steam_games").fetchall()
        prices = conn.execute(
            "SELECT appid, region_code, currency, price, original_price, "
            "discount_percent, sub_id, is_gold, version_suffix, price_status, crawled_at "
            "FROM steam_prices ORDER BY crawled_at ASC"
        ).fetchall()
    finally:
        conn.close()

    games_upserted = 0
    async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
        for row in games:
            stmt = sqlite_insert(Game).values(
                appid=int(row["appid"]),
                name=row["name"] or f"AppID_{row['appid']}",
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Game.appid], set_={"name": stmt.excluded.name}
                )
            )
            games_upserted += 1

        # 每个 (appid, region) 取最新一条写 current，全部追加 history
        latest: dict[tuple[int, str], sqlite3.Row] = {}
        for row in prices:
            key = (int(row["appid"]), row["region_code"].upper())
            latest[key] = row  # ASC 排序，后者覆盖

        for (appid, region), row in latest.items():
            price = row["price"]
            status = row["price_status"] or "ok"
            # 从 history 重建属于人工修复动作：按快照状态补观察章
            # （ok/locked = 成功观察并推进 last_success_at；missing/blocked
            # = 失败态快照，last_success_at 留空待下次真实观察）
            outcome = "success" if status in ("ok", "locked") else "failed"
            answer = "locked" if status == "locked" else "ok" if status == "ok" else None
            success_at = datetime.now() if outcome == "success" else None
            stmt = sqlite_insert(GameCurrentPrice).values(
                appid=appid,
                region_code=region,
                currency=row["currency"] or "",
                price=int(price) if price is not None else None,
                original_price=int(row["original_price"]) if row["original_price"] is not None else None,
                discount_percent=row["discount_percent"] or 0,
                sub_id=row["sub_id"],
                price_status=status,
                attempt_outcome=outcome,
                steam_answer=answer,
                last_success_at=success_at,
                updated_at=datetime.now(),
            )
            await session.execute(
                stmt.on_conflict_do_update(
                    index_elements=[GameCurrentPrice.appid, GameCurrentPrice.region_code],
                    set_={
                        "currency": stmt.excluded.currency,
                        "price": stmt.excluded.price,
                        "original_price": stmt.excluded.original_price,
                        "discount_percent": stmt.excluded.discount_percent,
                        "sub_id": stmt.excluded.sub_id,
                        "price_status": stmt.excluded.price_status,
                        "attempt_outcome": stmt.excluded.attempt_outcome,
                        "steam_answer": stmt.excluded.steam_answer,
                        "last_success_at": stmt.excluded.last_success_at,
                        "updated_at": stmt.excluded.updated_at,
                    },
                )
            )
            session.add(
                GamePriceHistory(
                    appid=appid,
                    region_code=region,
                    currency=row["currency"] or "",
                    price=int(price) if price is not None else None,
                    original_price=int(row["original_price"]) if row["original_price"] is not None else None,
                    discount_percent=row["discount_percent"] or 0,
                    sub_id=row["sub_id"],
                    is_gold=bool(row["is_gold"]),
                    version_suffix=row["version_suffix"],
                    price_status=row["price_status"] or "ok",
                    snapshot_at=datetime.now(),
                )
            )
        await session.commit()

    return {
        "games": games_upserted,
        "priceRows": len(prices),
        "currentUpserted": len(latest),
    }


class ExportRequest(BaseModel):
    password: str  # 导出口令：加密正文的密钥来源（不落库、不保存）


@router.post("/export")
async def export_data(req: ExportRequest) -> dict:
    """敏感数据加密导出：用户侧数据（凭据/关注/设置/订阅…）口令加密为单文件。

    不含随包种子与公共目录/价格数据（由发布包与爬虫重建）。需当前主密钥可用
    （口令保护模式下先解锁），凭据类列解密后随正文重新加密封装。
    """
    try:
        return await data_export.create_export(req.password)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SecretBoxError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.get("/export/list")
async def export_list() -> dict:
    """已有加密导出文件（新→旧）。"""
    return {"items": data_export.list_exports()}


@router.get("/export/download/{name}")
async def export_download(name: str) -> FileResponse:
    """下载加密导出文件。"""
    try:
        path = data_export.safe_export_path(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="导出文件不存在")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


@router.delete("/export/{name}")
async def export_delete(name: str) -> dict:
    """删除某份导出文件。"""
    try:
        path = data_export.safe_export_path(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="导出文件不存在")
    path.unlink()
    return {"removed": True}


# ─────────────────────────── 密钥保护（凭据加密的密钥托管）────────────────────


class SecurityModeRequest(BaseModel):
    mode: str  # legacy（关闭）| dpapi（系统凭据）| passphrase（口令）
    passphrase: str | None = None  # 设为口令模式时的新口令
    current_passphrase: str | None = None  # 从口令模式切换出去时的当前口令


class SecurityUnlock(BaseModel):
    passphrase: str


@router.get("/system/security")
async def security_status() -> dict:
    """当前密钥保护状态（模式 / 是否锁定 / 系统凭据是否可用）。"""
    return keyring.status(get_settings().data_dir)


@router.post("/system/security/mode")
async def security_set_mode(req: SecurityModeRequest) -> dict:
    """切换密钥保护模式：全库密文换钥重加密（失败不改动库与密钥文件）。"""
    try:
        result = await rekey.switch_mode(
            req.mode,
            passphrase=req.passphrase,
            current_passphrase=req.current_passphrase,
        )
    except KeyringError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "counts": result["counts"],
            "security": keyring.status(get_settings().data_dir)}


@router.post("/system/security/unlock")
async def security_unlock(req: SecurityUnlock) -> dict:
    """口令模式解锁：口令正确后本进程可读写凭据。"""
    data_dir = get_settings().data_dir
    try:
        ok = keyring.unlock(data_dir, req.passphrase)
    except KeyringError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not ok:
        raise HTTPException(status_code=400, detail="口令不正确")
    secretbox.clear_key_cache()
    return {"ok": True, "security": keyring.status(data_dir)}


@router.post("/system/security/lock")
async def security_lock() -> dict:
    """锁定：清空进程内主密钥（仅口令模式有意义）。"""
    keyring.lock()
    secretbox.clear_key_cache()
    return {"ok": True, "security": keyring.status(get_settings().data_dir)}


# ─────────────────────────── 数据备份（VACUUM INTO 在线快照）────────────────────


class BackupCreate(BaseModel):
    label: str | None = None


@router.post("/system/backup")
async def backup_create(req: BackupCreate) -> dict:
    """立即备份（手动档：在线快照不打断写入，独立轮转仅保留最新一份）。"""
    try:
        return await create_backup_impl(req.label, manual=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system/backup")
async def backup_list() -> dict:
    return {"items": list_backups_impl()}


@router.post("/system/backup/{name}/verify")
async def backup_verify(name: str) -> dict:
    """校验备份自洽性（integrity_check + 独立打开，不动任何文件）。"""
    try:
        return await verify_backup_impl(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/system/backup/{name}/restore")
async def backup_restore(name: str) -> dict:
    """从备份恢复（三段式：校验 → 停写入面 → 轮换替换，失败自动回滚）。

    恢复期间调度器与爬取暂停，完成后自动重启调度器。
    """
    try:
        return await restore_backup_impl(name)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/system/backup/{name}")
async def backup_delete(name: str) -> dict:
    try:
        path = safe_backup_path(backup_dir(), name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="备份不存在")
    path.unlink()
    return {"removed": True}


@router.get("/system/backup/{name}/download")
async def backup_download(name: str) -> FileResponse:
    """下载备份文件（另存到本机其他位置）。"""
    try:
        path = safe_backup_path(backup_dir(), name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not path.is_file():
        raise HTTPException(status_code=404, detail="备份不存在")
    return FileResponse(path, filename=path.name, media_type="application/octet-stream")


# ─────────────────────────── 运行日志（日志页）───────────────────────────


@router.get("/system/logs")
async def logs(limit: int = Query(200, ge=1, le=2000)) -> dict:
    """内存环形缓冲的最近日志行（进程启动以来）。"""
    return {"lines": ring_log_handler.snapshot(limit)}


@router.get("/system/logs/stream")
async def logs_stream(request: Request) -> StreamingResponse:
    """SSE：实时推送新日志行（日志页订阅）。

    连接即先回放缓冲内最近 200 行（进入页面即见上下文），再续播增量。
    """
    ring_log_handler.bind_loop(asyncio.get_running_loop())
    queue = ring_log_handler.subscribe()

    async def generator():
        try:
            for line in ring_log_handler.snapshot(200):
                yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
            yield ": synced\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    line = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"data: {json.dumps(line, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
        finally:
            ring_log_handler.unsubscribe(queue)

    return StreamingResponse(
        generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────── 应用更新（GitHub Releases）────────────────────


@router.get("/system/update-check")
async def update_check() -> dict:
    """检查 GitHub 最新 release（网络不可达时 available=False，不抛错）。"""
    from app.core import updater

    return await updater.check_update()


class UpdateDownload(BaseModel):
    tag: str
    sha256: str | None = None  # 清单内的校验值；缺省跳过校验
    asset: str | None = None  # 清单给出的确切资产名（省掉一次 API 反查）
    size: int | None = None  # 清单体积：通道不给 Content-Length 时兜底百分比


@router.post("/system/update-download")
async def update_download(req: UpdateDownload) -> dict:
    """下载 release zip → data/update-staging/ 解包校验；重启后由桌面壳换装。

    sha256 期望值缺省时跳过校验（前端展示「未提供校验值」）。
    """
    from app.core import updater

    if updater.download_progress().get("running"):
        raise HTTPException(status_code=409, detail="已有更新下载进行中")
    # fire-and-forget：下载在后台跑，前端轮询 /system/update-progress
    asyncio.get_running_loop().create_task(
        updater.download_update(req.tag, req.sha256, req.asset, req.size)
    )
    return {"started": True}


@router.get("/system/update-progress")
async def update_progress() -> dict:
    """下载/校验/解包进度（模块级单例，最近一次状态）。"""
    from app.core import updater

    return updater.download_progress()


@router.get("/system/update-pending")
async def update_pending() -> dict:
    """staging 是否就绪（前端「重启以完成更新」提示依据）。"""
    from app.core import updater

    return updater.pending_status()


@router.get("/system/seed/status")
async def seed_status() -> dict:
    """公共数据种子状态：是否在位（含版本）+ 按需下载进度。"""
    from app.core import seed_fetch

    return await seed_fetch.status_snapshot()


@router.post("/system/seed/fetch")
async def seed_fetch_now() -> dict:
    """手动触发种子按需获取（已就绪时空转返回；进行中 409）。"""
    from app.core import seed_fetch

    if seed_fetch.status().get("state") in ("downloading", "extracting", "merging"):
        raise HTTPException(status_code=409, detail="种子获取已在进行中")
    asyncio.get_running_loop().create_task(seed_fetch.ensure_seed_online())
    return {"started": True}


@router.post("/system/update-cancel")
async def update_cancel() -> dict:
    """取消更新：下载中 → 中止下载（保留已下的续传基线，下次接着下）；
    已就绪/无任务 → 清暂存目录。

    本端点在下载中即中止下载（保留已下的续传基线，下次接着下），不清暂存；
    不从下载中直接拒绝——下载 125MB 时用户唯一的出路会变成等它失败。
    """
    from app.core import updater

    if updater.download_progress().get("running"):
        return {**updater.cancel_download(), "cleared": False}
    return updater.clear_staging()

# ─────────────────────── 结构化诊断（机器排障读证据面）───────────────────────

# 写账等写锁预警阈值（毫秒）：BACKGROUND 持闸进入饥饿区间的经验线，
# 超过它才值得把「写锁竞争」列为嫌疑
_WRITE_LOCK_WAIT_WARN_MS = 30_000

# 写账样本条数：环形缓冲里最近 N 条「写库分段完成」
_WRITE_LEDGER_SAMPLES = 8


def _parse_write_ledger(lines: list[str], limit: int) -> list[dict]:
    """环形日志 → 写账样本（写库分段完成的分段毫秒）。

    行格式（HumanFormatter）：`HH:MM:SS [结果词] 模块 · 主句 │ 键=值 键=值`。
    解析只取诊断要用的三个数：款数 / 总计 / 等写锁（值形如 "12毫秒"）。
    """
    out: list[dict] = []
    for line in reversed(lines):  # 旧→新扫，取最近 limit 条
        if "写库分段完成" not in line:
            continue
        entry: dict = {"time": line[:8], "message": None, "entries": None, "total_ms": None, "lockwait_ms": None}
        head, _, tail = line.partition(" │ ")
        entry["message"] = head.split(" · ", 1)[-1]
        for pair in tail.split():
            key, _, value = pair.partition("=")
            if key in ("款数", "总计", "等写锁"):
                digits = "".join(ch for ch in value if ch.isdigit())
                if digits:
                    entry[{"款数": "entries", "总计": "total_ms", "等写锁": "lockwait_ms"}[key]] = int(digits)
        out.append(entry)
        if len(out) >= limit:
            break
    return out


async def _diagnostics_section(name: str, fn):
    """单面故障不拖垮整体：一面异常就降级为 {"error": 原文}。"""
    try:
        return await fn() if asyncio.iscoroutinefunction(fn) else fn()
    except Exception as e:  # noqa: BLE001 —— 诊断面自身必须可用
        return {"error": str(e)[:200]}


@router.get("/system/diagnostics")
async def diagnostics() -> dict:
    """机器排障读证据面：把散在四个账本里的疑难杂症证据收拢成一份快照。

    聚合（全部只读，一面失败不拖垮整体）：
    - 身份面：版本 / schema / 数据目录 / 策略（含是否显式选择）/ 内核态 /
      系统代理 / 池可用出口数——回答「这个实例是谁、现在走什么通道」；
    - 价格轮面：最近 5 轮 Cycle（终态 + 拒因 + 覆盖统计）——回答「上一轮
      跑没跑、为什么失败」；
    - 作业面：最近 10 次生产作业台账（proxy_job_runs，状态/出口/错误枚举）；
    - 写账面：环形缓冲最近 8 条「写库分段完成」（款数/总计/等写锁）——
      回答「写库是否在饥饿」；
    - 派生信号 signals：机器可读的问题清单（code + 人话 + 级别），消费方
      （领航员/运维/人）从「翻日志」变「读证据」。
    """
    from app.domains.crawl import cycle as cycle_service
    from app.domains.crawl.service import has_pending_missing
    from app.domains.proxies import clash_manager
    from app.domains.proxies import service as proxies_service
    from app.domains.proxypool import jobruns

    settings = get_settings()

    async def _identity() -> dict:
        strategy_info = await proxies_service.get_strategy()
        strategy = strategy_info["strategy"]
        from app.domains.settings import service as settings_service

        explicit = bool(
            await settings_service.get_value(proxies_service.STRATEGY_EXPLICIT_KEY, False)
        )
        status = clash_manager.runtime.status()
        pool = await proxies_service.pool_stats()
        try:
            system_proxy = await proxies_service.resolve_system_proxy_url()
        except Exception:  # noqa: BLE001 —— 探活失败按无系统代理记
            system_proxy = None
        return {
            "app_version": settings.version,
            "schema_version": SCHEMA_VERSION,
            "data_dir": str(settings.data_dir),
            "strategy": strategy,
            "strategy_explicit": explicit,
            "clash_running": bool(status.get("running")),
            "system_proxy": system_proxy,
            "pool_available_exits": int(pool.get("available") or 0),
        }

    async def _cycles() -> dict:
        cycles = await cycle_service.list_cycles(5)
        return {"last": cycles[0] if cycles else None, "recent": cycles}

    async def _job_runs() -> dict:
        async with get_session_factory()() as session:
            return await jobruns.list_runs(session, limit=10)

    def _write_ledger() -> dict:
        samples = _parse_write_ledger(
            ring_log_handler.snapshot(2000), _WRITE_LEDGER_SAMPLES
        )
        return {"samples": samples}

    async def _backlog() -> dict:
        return {"missing_pending": bool(await has_pending_missing())}

    identity = await _diagnostics_section("identity", _identity)
    cycles = await _diagnostics_section("cycles", _cycles)
    job_runs = await _diagnostics_section("job_runs", _job_runs)
    write_ledger = _write_ledger()
    backlog = await _diagnostics_section("backlog", _backlog)

    # ── 派生信号：机器可读的嫌疑清单（有证据才产信号，不臆测）──
    signals: list[dict] = []
    last_cycle = (cycles.get("last") or {}) if isinstance(cycles, dict) else {}
    if last_cycle.get("status") == "failed":
        signals.append({
            "code": "last_cycle_failed",
            "severity": "error",
            "text": f"上一轮价格刷新失败：{last_cycle.get('error') or '原因未随轮留痕'}",
        })
    samples = write_ledger.get("samples") if isinstance(write_ledger, dict) else []
    lockwaits = [s["lockwait_ms"] for s in samples if s.get("lockwait_ms")]
    if lockwaits and max(lockwaits) > _WRITE_LOCK_WAIT_WARN_MS:
        signals.append({
            "code": "write_lock_wait_high",
            "severity": "warning",
            "text": f"最近写账等写锁最高 {max(lockwaits)} 毫秒（预警线 {_WRITE_LOCK_WAIT_WARN_MS}），写库可能存在竞争",
        })
    items = (job_runs.get("items") or []) if isinstance(job_runs, dict) else []
    bad_runs = [r for r in items if r.get("status") in ("failed", "interrupted")]
    if bad_runs:
        signals.append({
            "code": "job_runs_failed",
            "severity": "warning",
            "text": f"最近 {len(items)} 次生产作业 {len(bad_runs)} 次失败/中断（作业台账面）",
        })
    if backlog.get("missing_pending"):
        signals.append({
            "code": "missing_backlog",
            "severity": "info",
            "text": "存在待补抓欠账（补抓通道按冷却自动消化）",
        })
    if isinstance(identity, dict) and "error" not in identity:
        needs_pool = identity["strategy"] in ("proxy_first", "proxy_only")
        if needs_pool and identity["pool_available_exits"] == 0:
            signals.append({
                "code": "proxy_pool_unavailable",
                "severity": "error",
                "text": "当前策略需要代理池但无可用出口，价格轮会被拒绝（到网络页确认订阅与节点）",
            })

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "identity": identity,
        "cycles": cycles,
        "job_runs": job_runs,
        "write_ledger": write_ledger,
        "backlog": backlog,
        "signals": signals,
    }
