"""alerts 域路由：规则 CRUD + 触发历史 + SMTP 配置 + 游戏搜索。

路由注册顺序：固定路径（/smtp, /search, /events）必须在路径参数（/{alert_id}）之前，
否则 "smtp" 会被匹配到 {alert_id} 导致 422 错误。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import service

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertAdd(BaseModel):
    appid: int
    region: str = "CN"
    targetType: str = "price"  # price | pct | historic_low
    targetValue: float | None = None


class AlertUpdate(BaseModel):
    active: bool | None = None
    targetValue: float | None = None
    targetType: str | None = None
    region: str | None = None


class SmtpUpdate(BaseModel):
    host: str = ""
    port: int = 465
    user: str = ""
    password: str = ""
    toAddr: str = ""
    useSsl: bool = True


# ─── 固定路径端点（必须注册在 {alert_id} 之前）───

@router.get("")
async def list_alerts():
    return await service.list_alerts()


@router.post("")
async def add_alert(req: AlertAdd):
    try:
        return await service.add_alert(req.appid, req.region, req.targetType, req.targetValue)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/events")
async def events(limit: int = 50):
    return await service.list_events(limit)


@router.get("/smtp")
async def get_smtp():
    return await service.get_smtp_config()


@router.put("/smtp")
async def update_smtp(req: SmtpUpdate):
    return await service.update_smtp_config(
        host=req.host,
        port=req.port,
        user=req.user,
        password=req.password,
        to_addr=req.toAddr,
        use_ssl=req.useSsl,
    )


@router.post("/smtp/test")
async def test_smtp(req: SmtpUpdate):
    """连通性测试：按表单当前值发一封主题化测试邮件（不落库）。

    password 留空时后端回退存量密码；配置缺失 / SMTP 失败均 400，
    detail 为可直接展示的中文原因。
    """
    try:
        return await service.test_smtp_config(
            host=req.host,
            port=req.port,
            user=req.user,
            password=req.password,
            to_addr=req.toAddr,
            use_ssl=req.useSsl,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/search")
async def search_games(q: str, region: str = "CN"):
    return await service.search_games(q, region)


# ─── 路径参数端点（放在最后）───

@router.put("/{alert_id}")
async def update_alert(alert_id: int, req: AlertUpdate):
    try:
        return await service.update_alert(
            alert_id,
            active=req.active,
            target_value=req.targetValue,
            target_type=req.targetType,
            region=req.region,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{alert_id}")
async def delete_alert(alert_id: int):
    removed = await service.delete_alert(alert_id)
    if not removed:
        raise HTTPException(status_code=404, detail="提醒不存在")
    return {"removed": True}
