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
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.core.app_info import APP_SLUG, GITHUB_REPO
from app.core.backup import (
    create_backup as create_backup_impl,
    list_backups as list_backups_impl,
    restore_backup as restore_backup_impl,
    verify_backup as verify_backup_impl,
    safe_backup_path,
    backup_dir,
)
from app.core.config import get_settings
from app.core.database import get_session_factory
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
    async with get_session_factory()() as session:
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
            stmt = sqlite_insert(GameCurrentPrice).values(
                appid=appid,
                region_code=region,
                currency=row["currency"] or "",
                price=int(price) if price is not None else None,
                original_price=int(row["original_price"]) if row["original_price"] is not None else None,
                discount_percent=row["discount_percent"] or 0,
                sub_id=row["sub_id"],
                price_status=row["price_status"] or "ok",
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


@router.post("/export")
async def export_data() -> dict:
    """导出全库为 JSON（data/exports/）。"""
    settings = get_settings()
    export_dir = settings.data_dir / "exports"
    export_dir.mkdir(parents=True, exist_ok=True)
    out_path = export_dir / f"{APP_SLUG}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"

    async with get_session_factory()() as session:
        games = (await session.execute(select(Game))).scalars().all()
        prices = (await session.execute(select(GameCurrentPrice))).scalars().all()

    payload = {
        "exportedAt": datetime.now().isoformat(),
        "games": [
            {
                "appid": int(g.appid), "name": g.name, "nameEn": g.name_en,
                "type": g.type, "positiveRate": g.positive_rate,
                "reviewCount": g.review_count, "releaseDate": g.release_date,
            }
            for g in games
        ],
        "currentPrices": [
            {
                "appid": int(p.appid), "region": p.region_code, "currency": p.currency,
                "price": int(p.price) if p.price else None,
                "original": int(p.original_price) if p.original_price else None,
                "discount": p.discount_percent, "status": p.price_status,
            }
            for p in prices
        ],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return {"path": str(out_path), "games": len(games), "prices": len(prices)}


# ─────────────────────────── 数据备份（VACUUM INTO 在线快照）────────────────────


class BackupCreate(BaseModel):
    label: str | None = None


@router.post("/system/backup")
async def backup_create(req: BackupCreate) -> dict:
    """立即备份（在线快照：不打断写入、含 WAL 已提交事务、产出独立自洽文件）。"""
    try:
        return await create_backup_impl(req.label)
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


@router.post("/system/update-cancel")
async def update_cancel() -> dict:
    """放弃本次更新：清暂存目录（正在下载的进度由下次覆盖）。"""
    from app.core import updater

    if updater.download_progress().get("running"):
        raise HTTPException(status_code=409, detail="下载进行中，请稍后再试")
    return updater.clear_staging()
