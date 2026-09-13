"""metadata 域路由：外部元数据（Epic/XGP/HB）导入与状态。"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.domains.metadata import service

router = APIRouter(prefix="/metadata", tags=["metadata"])


class ImportRequest(BaseModel):
    source: str = Field(description="epic | xgp | hb | all")
    source_dir: str | None = Field(
        default=None, description="原项目数据目录（默认 E:\\STEAM-price）"
    )


@router.post("/import")
async def import_metadata(req: ImportRequest) -> dict:
    source = req.source.strip().lower()
    if source == "all":
        return {"results": await service.import_all(req.source_dir)}
    importer = service.IMPORTERS.get(source)
    if importer is None:
        return {"error": f"未知来源: {source}（可选 epic/xgp/hb/all）"}
    return await importer(req.source_dir)


@router.get("/status")
async def metadata_status(source_dir: str | None = None) -> dict:
    return await service.metadata_status(source_dir)


@router.post("/hb/refresh")
async def refresh_hb_choice() -> dict:
    """当月 HB Choice 游戏侧标记（调度器每日自动跑同一入口，可手动触发）。"""
    return await service.refresh_hb_choice()


class EpicListItem(BaseModel):
    appid: int = Field(description="Steam AppID（数字或数字字符串）")
    free_start: str | None = Field(
        default=None, description="赠送起始日（YYYY-M-D 或常见变体；缺省只打标）"
    )
    title: str | None = Field(default=None, description="条目名（缺行占位用）")


class EpicListRequest(BaseModel):
    games: list[EpicListItem] = Field(description="外部名单条目（浏览器侧脚本抓取结果）")


@router.post("/epic/refresh")
async def refresh_epic_free() -> dict:
    """Epic 促销端点增量标记（调度器每日同一入口，可手动触发）。"""
    return await service.refresh_epic_free()


@router.get("/epic/offers")
async def epic_free_offers(force: bool = False) -> dict:
    """当期 + 预告白送元素（封面/商店页/原价），仪表盘卡片数据源。

    进程内缓存 30 分钟；force=1 跳过缓存强制拉取（调试用）。
    """
    return await service.epic_free_offers(force=force)


@router.post("/bundles/refresh")
async def refresh_bundle_counts() -> dict:
    """Barter.vg bundle 计数刷新（调度器自动跑同一入口；手动触发强制拉取，
    不受 48h 新鲜度闸限制）。"""
    return await service.refresh_bundle_counts(force=True)


@router.post("/epic/list")
async def import_epic_list(req: EpicListRequest) -> dict:
    """Epic 外部名单推送导入（浏览器侧脚本抓取的 appid/日期对）。"""
    items = [
        {"appid": g.appid, "free_start": g.free_start, "title": g.title}
        for g in req.games
    ]
    return await service.import_epic_list(items)
