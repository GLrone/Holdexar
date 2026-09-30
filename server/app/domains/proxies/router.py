"""proxies 域路由：代理池 / 健康检查 / 策略 / Clash 模式 / 走线日志。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.core.config import get_settings
from . import clash_manager, service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/proxies", tags=["proxies"])


async def _auto_promote_after_check(sub_id: int, alive: int) -> None:
    from app.core.database import WritePriority
    from app.core.database import write_gate
    """首检健康（可用节点 ≥ 1）即转正——与同步侧的自动准入同一把尺子。
    快照尚未落库（刚导入、同步拍还没轮到）时晋升会失败：账本里的可用数
    已写好，下一拍同步落快照时自动转正，这里只记日志不重试。"""

    from app.domains.proxypool.admission import promote_to_active

    if alive < 1:
        return
    try:
        async with write_gate(WritePriority.BACKGROUND), get_session_factory()() as session:
            result = await promote_to_active(
                session, subscription_id=sub_id,
                data_dir=get_settings().data_dir, now=datetime.now(),
            )
            await session.commit()
        if result.promoted:
            logger.info(
                "[准入] 订阅 %s 首检健康（可用 %d）→ 自动转正进池", sub_id, alive
            )
    except Exception as e:  # noqa: BLE001 —— 转正不成本身不构成首检失败
        logger.info(
            "[准入] 订阅 %s 首检健康（可用 %d）但暂不能转正（%s）；等同步落快照后自动转正",
            sub_id, alive, e,
        )


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
    # 内核随服务自启开关（None = 不变）
    autostart: bool | None = None
    # 自动节点体检开关（None = 不变；手动检测不受闸）
    healthAuto: bool | None = None


class ClashStart(BaseModel):
    subscriptionId: int | None = None  # proxy_subscriptions.kind=clash；缺省用最近一条


class ClashSwitch(BaseModel):
    subscriptionId: int


class ClashSelect(BaseModel):
    subscriptionId: int


def _spawn_first_check(sub_id: int, *, probe_all: bool = True) -> None:
    """后台首检：订阅内容变化（启动/切换/重拉生效）后检测一次写账本
    （订阅废弃判定/selector 自愈都在里面）；缺省全量探测（probe_all）——
    首检要给「这条订阅现在到底能不能用」的完整结论；串行锁与手动检测/
    定时体检互斥。账本在复用窗口内已有全量结论时跳过（来回切换订阅
    不重复探测），手动检测与定时路径不受此门约束。"""

    async def _first_check() -> None:
        try:
            if await service.clash_nodes_fresh(sub_id):
                logger.info(
                    "[订阅首检] Clash 订阅 %s：账本 %d 分钟内已检测，沿用现有结论",
                    sub_id, service.FIRST_CHECK_REUSE_MINUTES,
                )
                return
            r = await service.test_clash_nodes(sub_id, probe_all=probe_all)
            logger.info(
                "[订阅首检] Clash 订阅 %s：共 %s 节点，可用 %s",
                sub_id, r.get("total"), r.get("alive"),
            )
            await _auto_promote_after_check(sub_id, int(r.get("alive") or 0))
        except Exception:  # noqa: BLE001 —— 首检失败不影响内核已生效的事实
            logger.exception("[订阅首检] Clash 节点检测失败（可稍后手动检测）")

    asyncio.get_running_loop().create_task(_first_check())


class SubscriptionAdd(BaseModel):
    kind: str  # clash | plain
    url: str
    label: str | None = None


class SubscriptionUpdate(BaseModel):
    label: str | None = None  # 订阅名称；空串清名
    url: str | None = None  # 订阅链接；变更时 clash 订阅自动重拉
    autoRefresh: bool | None = None  # 自动更新开关；False = 定时刷新跳过，只手动重拉


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
        await service.set_strategy(
            req.strategy, req.clashPort, req.autostart, req.healthAuto
        )
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
        "selectedSubscriptionId": await service.remembered_clash_sub_id(),
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

    # 取配置按候选遍历：请求的订阅取不到（链接失效且无本地缓存）时依次
    # 降级到其余可用订阅，内核起不来不允许是「下载失败」一个原因
    try:
        picked = await service.resolve_startable_clash(
            settings.data_dir, req.subscriptionId,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    sub = picked["subscription"]
    sub_id = sub["id"]
    sub_url = sub["url"]

    try:
        status = await clash_manager.runtime.ensure_running(
            detect["path"], picked["configPath"], subscription_url=sub_url
        )
    except Exception as e:  # noqa: BLE001 —— 配置已到手，起不来是内核侧问题
        raise HTTPException(status_code=502, detail=f"内核启动失败: {e}")

    # 流量/节点数落库（启动即顺手回填）；订阅名只补空位——已有名称不覆写
    if picked.get("title"):
        await service._apply_subscription_name(sub_id, str(picked["title"]))
    if picked.get("userinfo"):
        await service.mark_imported(
            sub_id,
            {"traffic": picked["userinfo"], "nodes": picked.get("nodes"),
             "cached": picked.get("cached", False)},
        )
    # 这次启动用的订阅落为「用户最近一次显式选中」——前端回显与下次启动
    # 缺省同源（降级启到别的订阅时也以实际结果为准）
    await service.remember_selected_clash_sub(sub_id)
    await service.record_event(
        kind="clash", target="subscription", proxy_label=f"clash:{status.get('port')}"
    )

    # ── 启动即首检：后台全量检测写账本，不阻塞启动响应 ──
    _spawn_first_check(sub_id)
    fallback_from = None
    if picked["attempts"]:
        first = picked["attempts"][0]
        fallback_from = {"id": first["id"], "label": first["label"]}
    return {
        **status,
        "nodes": picked.get("nodes"),
        "usedCache": picked.get("cached", False),
        "subscription": {"id": sub_id, "label": sub["label"]},
        "fallbackFrom": fallback_from,
    }


@router.post("/clash/switch")
async def clash_switch(req: ClashSwitch):
    """切换运行中内核的订阅：控制器热重载生效，内核进程不动（与
    Clash Verge Rev 的换配置路径同款）。内核未运行时无需切换——「启动」
    会直接使用当前选中的订阅；废弃订阅不可切换。切换成功后后台首检。"""
    status = clash_manager.runtime.status()
    if not status["running"]:
        raise HTTPException(
            status_code=400,
            detail="Clash 未运行：点「启动」拉起内核，启动时将使用选中的订阅",
        )
    subs = await service.list_subscriptions("clash")
    sub = next((s for s in subs if s["id"] == req.subscriptionId), None)
    if sub is None:
        raise HTTPException(status_code=404, detail="指定的订阅不存在")
    if sub["deprecated"]:
        raise HTTPException(
            status_code=400,
            detail=f"该订阅已废弃（{sub['deprecatedReason'] or '不可用节点超过 95%'}），后端不再使用",
        )
    if clash_manager.runtime.running_subscription_is(sub["url"]):
        return {"switched": False, **status}

    settings = get_settings()
    detect = clash_manager.detect_kernel(settings.data_dir)
    if not detect["found"]:
        raise HTTPException(status_code=400, detail="未找到内核，请先安装")
    # 每订阅各存一份配置缓存：有缓存直接热重载（秒级），无缓存才下载；
    # 热重载持检测串行锁——切换若打断正在跑的首检，旧首检会对着新配置
    # 探测、整轮误判失败污染账本。
    sub_cache = clash_manager.subscription_config_path(settings.data_dir, sub["id"])
    try:
        if not sub_cache.is_file():
            await clash_manager.runtime.download_subscription(
                sub["url"], settings.data_dir, await service._saved_proxy_candidates(),
                target_path=sub_cache,
            )
        async with service._clash_test_lock():
            new_status = await clash_manager.runtime.ensure_running(
                detect["path"], str(sub_cache), subscription_url=sub["url"]
            )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"切换失败: {e}")

    _spawn_first_check(sub["id"], probe_all=True)
    # 切换成功即「用户最近一次显式选中」，前端回显与下次启动缺省同源
    await service.remember_selected_clash_sub(sub["id"])
    return {"switched": True, **new_status}


@router.post("/clash/select")
async def clash_select(req: ClashSelect):
    """内核未运行时记录订阅行点选：落库跨页面与重启保留，供「启动」缺省
    与前端回显。内核在跑时点选走 /clash/switch（选中即切换），不经这里。"""
    subs = await service.list_subscriptions("clash")
    if not any(s["id"] == req.subscriptionId for s in subs):
        raise HTTPException(status_code=404, detail="指定的订阅不存在")
    await service.remember_selected_clash_sub(req.subscriptionId)
    return {"selected": req.subscriptionId}


@router.post("/clash/test")
async def clash_test():
    """启动 Clash 订阅节点检测：后台逐节点探测（状态机落库/废弃判定/
    selector 自愈不变），立即返回会话进度快照；进度经 GET /clash/test/progress 轮询。"""
    return service.clash_test_start()


@router.get("/clash/test/progress")
async def clash_test_progress():
    """节点检测进度快照（后台会话；无会话时 phase=idle）。"""
    return service.clash_test_progress() or {"phase": "idle"}


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
    """编辑订阅：改名 + 换链接 + 自动更新开关（换链接的 clash 订阅保存即自动重拉）。"""
    try:
        return await service.update_subscription(
            sub_id, req.label, req.url, req.autoRefresh
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


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
                r = await service.test_clash_nodes(sub_id, probe_all=True)
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

