"""proxies 域路由：代理池 / 健康检查 / 策略 / Clash 模式 / 走线日志。"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from . import clash_manager, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxies", tags=["proxies"])


class ProxyAdd(BaseModel):
    url: str | None = None  # scheme://user:pass@host:port 或 host:port（批量用 \n 分隔）
    scheme: str = "http"
    host: str = ""
    port: int = 0
    username: str | None = None
    password: str | None = None
    label: str | None = None


class ProxyUpdate(BaseModel):
    enabled: bool | None = None
    label: str | None = None


class StrategyUpdate(BaseModel):
    strategy: str | None = None
    clashPort: int | None = None


class ClashStart(BaseModel):
    subscriptionId: int | None = None  # proxy_subscriptions.kind=clash；缺省用最近一条


class SubscriptionAdd(BaseModel):
    kind: str  # clash | plain
    url: str
    label: str | None = None


class SubscriptionUpdate(BaseModel):
    label: str  # 手动改名；空串清名


@router.get("")
async def list_proxies():
    proxies = await service.list_proxies()
    strategy = await service.get_strategy()
    return {
        "items": [service._proxy_dict(p) for p in proxies],
        "strategy": strategy,
        "subscriptions": await service.list_subscriptions(),
    }


@router.post("")
async def add_proxy(req: ProxyAdd):
    added = []
    try:
        if req.url and "\n" in req.url:
            for line in req.url.splitlines():
                line = line.strip()
                if line:
                    added.append(await service.add_proxy(line))
        else:
            added.append(
                await service.add_proxy(
                    req.url,
                    scheme=req.scheme,
                    host=req.host,
                    port=req.port,
                    username=req.username,
                    password=req.password,
                    label=req.label,
                )
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"added": len(added), "items": added}


@router.post("/test_all")
async def test_all():
    return {"items": await service.test_all()}


@router.get("/resolve")
async def resolve():
    """当前策略下解析出的代理 URL（None=直连）——桌面登录窗等外部消费方用。"""
    try:
        return {"proxyUrl": await service.resolve_proxy_url()}
    except RuntimeError as e:
        return {"proxyUrl": None, "message": str(e)}


@router.get("/stats")
async def proxy_stats():
    """仪表盘口径统计：手动池逐条计数，Clash 按出口 IP 去重（一个出口 IP = 一个代理）。"""
    return await service.pool_stats()


@router.put("/strategy")
async def set_strategy(req: StrategyUpdate):
    try:
        await service.set_strategy(req.strategy, req.clashPort)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return await service.get_strategy()


# ─── Clash 模式 ──────────────────────────────────────────────
# ⚠️ 固定路径必须注册在动态路径 /{proxy_id} 系列之前，否则
#    POST /clash/test 会被 POST /{proxy_id}/test 抢先匹配（422）——犯过


@router.get("/clash")
async def clash_status():
    settings = get_settings()
    detect = clash_manager.detect_kernel(settings.data_dir)
    version = (
        clash_manager.kernel_version(detect["path"]) if detect["found"] else None
    )
    return {
        **clash_manager.runtime.status(),
        "kernel": detect,
        "version": version,
        "kernelDir": str(clash_manager.kernel_dir(settings.data_dir)),
    }


@router.post("/clash/install")
async def clash_install():
    """安装 mihomo 内核：随包资产优先（本地复制，瞬时），随包缺失才走网络下载。

    网络下载那一支会阻塞较久，前端用 loading 态 + 进度弹窗呈现。
    """
    settings = get_settings()
    result = await asyncio.to_thread(clash_manager.install_kernel, settings.data_dir)
    if result.get("ok"):
        result["version"] = clash_manager.kernel_version(result["path"])
    return result


@router.post("/clash/start")
async def clash_start(req: ClashStart):
    settings = get_settings()
    detect = clash_manager.detect_kernel(settings.data_dir)
    if not detect["found"]:
        raise HTTPException(status_code=400, detail="未找到内核，请先安装")

    # 订阅从持久化订阅表取（可存多条，按 kind=clash 区分）；
    # 废弃订阅（不可用 >95%）跳过——后端不再选用，但保留给用户手动处理
    subs = await service.list_subscriptions("clash")
    if not subs:
        raise HTTPException(status_code=400, detail="尚未保存 Clash 订阅链接，请先添加")
    usable = [s for s in subs if not s["deprecated"]]
    if req.subscriptionId is not None:
        sub = next((s for s in subs if s["id"] == req.subscriptionId), None)
        if sub is None:
            raise HTTPException(status_code=404, detail="指定的订阅不存在")
        if sub["deprecated"]:
            raise HTTPException(
                status_code=400,
                detail=f"该订阅已废弃（{sub['deprecatedReason'] or '不可用节点超过 95%'}），"
                "后端不再使用；如需恢复请先检测节点确认恢复达标，或手动删除该订阅",
            )
    else:
        if not usable:
            raise HTTPException(
                status_code=400,
                detail="全部 Clash 订阅已废弃（不可用节点超过 95%），请在订阅区手动删除或更换订阅",
            )
        sub = usable[-1]
    sub_id = sub["id"]
    sub_url = sub["url"]

    try:
        meta = await clash_manager.runtime.download_subscription(
            sub_url, settings.data_dir
        )
        status = clash_manager.runtime.start(detect["path"], meta["path"])
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"订阅下载失败: {e}")

    # 流量/节点数落库（启动即顺手回填）；订阅名不再自动回填——手动改名
    if meta.get("userinfo"):
        await service.mark_imported(
            sub_id,
            {"traffic": meta["userinfo"], "nodes": meta.get("nodes"),
             "cached": meta.get("cached", False)},
        )
    await service.record_event(
        kind="clash", target="subscription", proxy_label=f"clash:{status.get('port')}"
    )

    # ── 启动即首检（新订阅入库自动校验标记可用节点）──
    # 后台任务跑全量节点检测（71 节点实测约 1-2 分钟），不阻塞启动响应；
    # 结果写 clash_nodes 账本（订阅废弃判定/selector 自愈都在里面）。
    # 串行锁保证与手动「检测节点」/定时体检互斥。
    async def _first_check() -> None:
        try:
            r = await service.test_clash_nodes(sub_id)
            logger.info(
                "[启动首检] Clash 订阅 %s：共 %s 节点，可用 %s",
                sub_id, r.get("total"), r.get("alive"),
            )
        except Exception:  # noqa: BLE001 —— 首检失败不影响内核已启动的事实
            logger.exception("[启动首检] Clash 节点检测失败（可稍后手动检测）")

    asyncio.get_running_loop().create_task(_first_check())
    return {**status, "nodes": meta.get("nodes"), "usedCache": meta.get("cached", False)}


@router.post("/clash/test")
async def clash_test():
    """检测 Clash 订阅节点：节点状态机落库 + 订阅废弃判定 + selector 自愈。"""
    try:
        return await service.test_clash_nodes()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/clash/health_check")
async def clash_health_check(force: bool = False):
    """手动触发体检（默认走 6h 门槛，force=true 跳过）。"""
    state = await service.maybe_run_clash_health_check(force=force)
    return {"state": state, "intervalHours": service.HEALTH_INTERVAL_HOURS}


@router.post("/clash/stop")
async def clash_stop():
    return clash_manager.runtime.stop()



@router.put("/{proxy_id}")
async def update_proxy(proxy_id: int, req: ProxyUpdate):
    try:
        return await service.update_proxy(proxy_id, enabled=req.enabled, label=req.label)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete("/{proxy_id}")
async def delete_proxy(proxy_id: int):
    removed = await service.delete_proxy(proxy_id)
    if not removed:
        raise HTTPException(status_code=404, detail="代理不存在")
    return {"removed": True}


@router.post("/{proxy_id}/test")
async def test_proxy(proxy_id: int):
    try:
        return await service.test_proxy(proxy_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/events")
async def events(limit: int = 200):
    return await service.recent_events(limit)


# ─── 订阅链接（clash / plain）────────────────────────────────


@router.get("/subscriptions")
async def list_subscriptions(kind: str | None = None):
    return {"items": await service.list_subscriptions(kind)}


@router.post("/subscriptions")
async def add_subscription(req: SubscriptionAdd):
    try:
        return await service.add_subscription(req.kind, req.url, req.label)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/clash/install/progress")
async def clash_install_progress():
    """内核下载进度（前端 1s 轮询；订阅保存流程自动下载内核时同用此端点）。"""
    return clash_manager.kernel_download_progress()


@router.delete("/subscriptions/{sub_id}")
async def delete_subscription(sub_id: int):
    removed = await service.delete_subscription(sub_id)
    if not removed:
        raise HTTPException(status_code=404, detail="订阅不存在")
    return {"removed": True}


@router.put("/subscriptions/{sub_id}")
async def update_subscription(sub_id: int, req: SubscriptionUpdate):
    """订阅手动改名（仅本地显示；面板 profile-title 不再自动回填）。"""
    try:
        return await service.update_subscription_label(sub_id, req.label)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.post("/subscriptions/{sub_id}/refresh")
async def refresh_subscription(sub_id: int):
    """流量统计实时回填：轻量拉 subscription-userinfo 头，不动内核。"""
    try:
        return await service.refresh_subscription_traffic(sub_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"订阅刷新失败: {e}")


@router.post("/subscriptions/{sub_id}/sync")
async def sync_subscription(sub_id: int):
    """重新拉取订阅：下载新配置 + 账本收敛 + 内核配置变化自动重启。

    重启生效（restarted=true，可能带新节点）时后台首检——新节点入库即
    校验，避免下个 6h 体检窗口前的未验证态（串行锁与手动检测互斥）。"""
    try:
        result = await service.refresh_clash_subscription(sub_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"订阅拉取失败: {e}")

    if result.get("restarted"):

        async def _post_sync_check() -> None:
            try:
                r = await service.test_clash_nodes(sub_id)
                logger.info(
                    "[刷新首检] Clash 订阅 %s：共 %s 节点，可用 %s",
                    sub_id, r.get("total"), r.get("alive"),
                )
            except Exception:  # noqa: BLE001 —— 首检失败不影响刷新事实
                logger.exception("[刷新首检] Clash 节点检测失败（可稍后手动检测）")

        asyncio.get_running_loop().create_task(_post_sync_check())
    return result


@router.post("/subscriptions/{sub_id}/import")
async def import_subscription(sub_id: int):
    """拉取明文代理订阅并导入节点池。"""
    try:
        stats = await service.import_plain_subscription(sub_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"订阅拉取失败: {e}")
    return stats

