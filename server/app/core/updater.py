"""应用自更新：读更新清单 → 检查 → 下载暂存 → 校验 → 重启换装。

检查更新走**静态清单**（`latest.json`，挂在固定 tag `updater` 的 Release 资产下），
而不是每次打 api.github.com：
- 清单随版本发布更新，但地址恒定 → 检查更新永远是「读一个小 json」
- 免 GitHub API 的 60 次/小时限速，且 api.github.com 在国内比 release 资产更难可达
- 清单自带 sha256 与资产名，客户端不必再从 release body 里抠哈希（旧的兜底路径保留）

换装流程三段式（解压暂存 → 旧文件移备份目录 → 新文件落位），换装时机
放在**下次启动**（PyInstaller onedir 在 Windows 下运行中替换自身文件
句柄风险高）：桌面壳把换装交给暂存包内的独立更新宿主进程完成。

数据安全（本项目核心承诺，优先于一切花哨功能）：
- 更新清单必须经内嵌公钥验签（core/update_signing）；验不过的清单不进
  检查/下载/换装链——镜像只当字节运输，不产生任何信任
- 换装只动程序目录，data/ 等用户目录跳过（desktop 侧白名单）
- 下载产物落 data/update-staging/，sha256 校验不过不进换装链（校验值
  缺失同样拒绝，不存在「无校验值放行」）
- 旧程序目录整体移 __old__，失败可手工移回 = 永远有退路

网络通道：复用 clash_manager._download_attempts 的镜像 + 本地代理
发现链（国内网络直连 GitHub Releases 基本不可用）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from pathlib import Path

import httpx

from app.core.app_info import (
    APP_NAME,
    APP_VERSION,
    ENV_PREFIX,
    GITHUB_MIRRORS,
    GITHUB_REPO,
    HANDOFF_LEDGER_NAME,
    HANDOFF_UNSUPPORTED_MARK,
    MANIFEST_ASSET,
    MANIFEST_TAG,
    STAGING_DIR_NAME,
    STAGING_MANIFEST_NAME,
    SWAP_FAILED_FLAG,
    version_gt,
)
from app.core.config import get_settings
from app.core.logging import log_event
from app.core import update_signing

logger = logging.getLogger(__name__)

# 仓库标识统一在 app.core.app_info.GITHUB_REPO（更新检查与种子获取共用）

# release 资产命名（build_release.py make_zip 产物）
_ASSET_PATTERN = re.compile(rf"^{re.escape(APP_NAME)}-win64-v[\w.-]+\.zip$", re.IGNORECASE)

# 更新清单的落点常量（MANIFEST_TAG / MANIFEST_ASSET）在 app.core.app_info，
# 与 scripts/build_manifest.py、scripts/publish_release.py 共用一份。
# 固定 tag 下挂 latest.json：地址与版本号解耦，检查更新只读这一个文件。

# 镜像前缀（单一来源 app.core.app_info.GITHUB_MIRRORS，空串 = 直连 GitHub）。
_MIRRORS = GITHUB_MIRRORS

STAGING_DIR = STAGING_DIR_NAME  # data/ 下的暂存目录（desktop 侧读同名目录）
_PENDING_FILE = ".update-pending"  # 换装标记（staging 内 manifest.json 同目录）

# 探测：候选通道各发一笔 `Range: bytes=0-0`（只读响应头，几十字节代价），一次并发
# 同时判出「资产在不在」与「这条通道快不快」。
_PROBE_TIMEOUT = 8.0
# 传输停滞判定：连续这么久一个字节都没到即换道（不让进度条在半死连接上干等）。
_STALL_TIMEOUT = 15.0
# 速率考核：下载跑满 _TRIAL_SECONDS 后平均速率仍低于下限即换道；最后一个可用通道
# 不考核（全网都慢也得让它下完，不能把所有通道都试死）。
_MIN_CHANNEL_RATE = 64 * 1024  # B/s
_TRIAL_SECONDS = 15.0
# 续传落盘的临时名（.part：没校验通过前不得被当成完整包）
_PART_NAME = "update.zip.part"
DOWNLOAD_META_NAME = "download_meta.json"
# 安装包体积下限：小于它必然不是应用包（镜像对不存在的资产常回几百字节的
# 200 错误页——把它当下载成功，用户会看到「校验失败」而不是「没这个包」）。
_MIN_PACKAGE_BYTES = 1 * 1024 * 1024


def staging_dir() -> Path:
    from app.core.paths import staging_dir as get_staging_dir

    d = get_staging_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def manifest_url(mirror: str = "") -> str:
    """清单地址（mirror 为空 = 直连）。"""
    base = os.environ.get(f"{ENV_PREFIX}UPDATE_URL", "").rstrip("/")
    if base:
        return f"{base}/{MANIFEST_ASSET}"
    return (
        f"{mirror}https://github.com/{GITHUB_REPO}/releases/download/"
        f"{MANIFEST_TAG}/{MANIFEST_ASSET}"
    )


def _pick_asset(assets: list[dict], name: str | None = None) -> dict | None:
    """挑应用包资产：显式 name 精确匹配 → role=app → 命名模式。"""
    if name:
        hit = next((a for a in assets if str(a.get("name")) == name), None)
        if hit:
            return hit
    hit = next((a for a in assets if str(a.get("role") or "") == "app"), None)
    if hit:
        return hit
    return next((a for a in assets if _ASSET_PATTERN.match(str(a.get("name", "")))), None)



async def _fetch_manifest() -> dict | None:
    """按镜像链取 latest.json。全通道并发取回，按镜像优先级取**第一个验签通过**者；全失败返回 None。

    信任模型：镜像只是运输——清单必须经内嵌公钥验签（update_signing），
    伪造/篡改/无签名清单按通道失败处理。显式 HOLDEXAR_UPDATE_URL 指向的
    本地测试源豁免验签（本机自控调试面，能设该变量的进程本就控制本机）。
    """
    base = os.environ.get(f"{ENV_PREFIX}UPDATE_URL", "").rstrip("/")
    if base:
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
                resp = await client.get(f"{base}/{MANIFEST_ASSET}")
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("version"):
                        return data
        except Exception:  # noqa: BLE001
            return None
        return None

    def _trusted(data: dict) -> bool:
        ok, _reason = update_signing.verify_manifest(data)
        return ok

    async def _try_one(mirror: str) -> dict | None:
        try:
            async with httpx.AsyncClient(timeout=5, follow_redirects=True) as client:
                resp = await client.get(manifest_url(mirror))
                if resp.status_code != 200:
                    return None
                data = resp.json()
                if isinstance(data, dict) and data.get("version") and _trusted(data):
                    return data
        except Exception:  # noqa: BLE001 —— 单通道失败不影响其余
            return None
        return None

    results = await asyncio.gather(*(_try_one(m) for m in _MIRRORS))
    for data in results:
        if data is not None:
            return data
    log_event(
        logger,
        "更新清单不可用，回落 GitHub API",
        tag="降级",
        detail={"原因": "全部镜像均未返回有效签名清单"},
    )
    return None


# ── 更新检查（清单优先，GitHub API 兜底）─────────────────────────────


async def check_update() -> dict:
    """对比最新版本与当前版本。返回前端展示 payload。

    - 清单可达：一次请求拿全（版本/资产/校验值/说明），不打 GitHub API
    - 清单不可达：回落 GitHub API（老路径保留，兼容尚未发清单的仓库）
    - 两者皆不可达（国内网络常态）：available=False + error 说明，绝不抛——
      检查失败 ≠ 更新失败，用户还能手动下载
    """
    data = await _fetch_manifest()
    if data is not None:
        return _payload_from_manifest(data)
    return await _check_update_via_api()


def _payload_from_manifest(data: dict) -> dict:
    settings = get_settings()
    latest = str(data.get("version") or "").lstrip("vV")
    if not latest:
        return {"available": False, "reason": "bad_manifest"}

    current = settings.version
    assets = data.get("assets") or []
    asset = _pick_asset(assets)
    if not asset:
        return {"available": False, "reason": "no_asset"}
    # 资产缺 sha256 = 发布侧残缺：有更新也不能进「无校验下载」的口子
    if not str(asset.get("sha256") or "").strip():
        return {"available": False, "reason": "bad_manifest"}

    return {
        "available": version_gt(latest, current),
        "current": current,
        "latest": latest,
        "tag": str(data.get("tag") or f"v{latest}"),
        "notes": _plain_notes(str(data.get("notes") or "")),
        "sizeBytes": asset.get("size"),
        # 清单自带校验值 → 下载端不必再解 release body；资产名同理由此确定
        "sha256": asset.get("sha256"),
        "asset": asset.get("name"),
        "publishedAt": data.get("publishedAt"),
        "source": "manifest",
    }



async def _check_update_via_api() -> dict:
    settings = get_settings()
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest",
                headers={"Accept": "application/vnd.github+json"},
            )
        if resp.status_code == 404:
            return {"available": False, "reason": "no_releases"}
        resp.raise_for_status()
        release = resp.json()
    except Exception as e:  # noqa: BLE001 —— 网络失败降级为「暂不可查」
        log_event(
            logger,
            "检查更新失败（网络不可达）",
            tag="降级",
            detail={"原因": str(e)},
        )
        return {"available": False, "reason": "network", "error": str(e)[:200]}

    tag = str(release.get("tag_name") or "")
    asset = _pick_asset(release.get("assets") or [])
    if not tag or not asset:
        return {"available": False, "reason": "no_asset"}

    latest = tag.lstrip("vV")
    current = settings.version
    newer = version_gt(latest, current)
    # GitHub 的 release 资产自带 digest（形如 "sha256:..."）——API 路径的
    # 完整性锚点。缺它就不宣告可用更新：宁可「暂不可更新」，不造「有更新
    # 却在下载端被拒」的悬空态。
    digest = str(asset.get("digest") or "")
    if not digest.startswith("sha256:"):
        return {"available": False, "reason": "bad_manifest", "error": "发布资产缺少校验值"}
    return {
        "available": newer,
        "current": current,
        "latest": latest,
        "tag": tag,
        "notes": _plain_notes(release.get("body") or ""),
        "sizeBytes": asset.get("size"),
        "sha256": digest.split(":", 1)[1],
        "asset": asset.get("name"),
        "publishedAt": release.get("published_at"),
        "source": "api",
    }


# 版本比较统一在 app_info.version_gt（零依赖模块，桌面壳/发布脚本共用一份）
_version_gt = version_gt


def _plain_notes(body: str, limit: int = 8000) -> str:
    """release body 转可展示纯文本（去 markdown 语法噪音，截断保形）。"""
    text = re.sub(r"```[\s\S]*?```", "（代码块略）", body)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*?([^*]+)\*\*?", r"\1", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:limit]


# ── 下载暂存（staging）──────────────────────────────────────────────

# 下载进度（模块级单例，对齐 clash_manager._KERNEL_PROGRESS 模式：
# 下载协程写，轮询端点读，dict 原子替换免锁）
_PROGRESS: dict = {}
# 取消旗标（用户点「取消」置位；下载循环在分片边界自查退出）
_CANCEL: dict = {"flag": False}


def _progress_reset() -> None:
    _PROGRESS.clear()
    _PROGRESS.update({
        "running": False, "phase": None, "percent": None,
        "received": 0, "total": None, "error": None, "ok": False,
        # code：机器可读的失败归因（前端按它给不同文案，不靠解析错误字符串）
        "code": None, "speed": 0, "channel": None,
    })


def download_progress() -> dict:
    if not _PROGRESS:
        _progress_reset()
    return dict(_PROGRESS)


class AssetMissing(RuntimeError):
    """发布资产不存在（HTTP 404）：版本尚未真正发布，或资产被撤下。

    单独成类是为了把「网络不通」与「这个版本压根没包」分开——前者该重试，
    后者重试一万次还是 404（清单已发布、release 还是草稿时即如此：客户端
    对着 404 把整条通道链蹚完，用户看到的就是「下载永远下不动」）。
    """


class _Cancelled(RuntimeError):
    """用户主动取消下载（内部信号，不冒泡到前端当故障展示）。"""


def mark_download_starting(tag: str, expected_size: int | None = None) -> bool:
    """原子化置位下载中状态，防止并发调用竞争。"""
    if _PROGRESS.get("running"):
        return False
    _progress_reset()
    _CANCEL["flag"] = False
    _PROGRESS.update({
        "running": True, "phase": "probe", "received": 0,
        "total": expected_size or None,
        "percent": 0 if expected_size else None,
    })
    return True


async def download_update(
    tag: str,
    expected_sha256: str | None = None,
    asset_name: str | None = None,
    expected_size: int | None = None,
    notes: str | None = None,
) -> dict:
    """下载 release zip 到 data/update-staging/ 并解包 + 校验。

    三段式（先探、再下、后校验）：
      ① 并发向所有候选通道发 `Range: bytes=0-0`，一次判出「资产在不在」与
         「通道快不快」；全通道 404 立即抛 AssetMissing（不串行蹚链，避免
         几十秒无反馈）；
      ② 按探测延迟从快到慢下载，**带断点续传**（换通道不丢已下部分）；
      ③ 停滞（_STALL_TIMEOUT 无字节）与速率考核双闸换道。

    asset_name：清单/检查结果给出的确切资产名。给了就直连下载，省掉一次
    API 反查（资产名含时间戳，猜不出来，只能反查或由清单提供）。
    expected_size：清单里的体积。通道不给 Content-Length（镜像分块响应）时
    用它当总量——否则百分比恒为空，前端进度条看着像卡死。
    expected_sha256：完整性校验值，**必填**——清单路径下来自验签清单、API
    路径下来自 GitHub 资产 digest；缺失即拒绝下载（不存在无校验放行）。
    校验不过 → 清 staging 抛错，绝不进换装链。
    """
    from app.domains.proxies import clash_manager

    if not expected_sha256 or not str(expected_sha256).strip():
        raise RuntimeError("更新源未提供完整性校验值，已拒绝下载（更新源不可信或残缺）")

    if not _PROGRESS.get("running"):
        _progress_reset()
        _CANCEL["flag"] = False
        _PROGRESS.update({
            "running": True, "phase": "probe", "received": 0,
            "total": expected_size or None,
            "percent": 0 if expected_size else None,
        })

    staging = staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    (staging / STAGING_MANIFEST_NAME).unlink(missing_ok=True)
    part = staging / _PART_NAME
    meta_path = staging / DOWNLOAD_META_NAME

    # 续传基线与资产绑定：
    # 仅当历史元数据与当前下载的目标资产（tag / asset / size / sha256）完全一致时，
    # 并且已下载字节数不超过目标大小时，才保留 .part 接着下；
    # 任何一项不符均视为其他版本或损坏残留，丢弃从头开始。
    meta_valid = False
    if part.is_file() and meta_path.is_file():
        try:
            old_meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if (
                old_meta.get("tag") == tag
                and old_meta.get("asset") == (asset_name or "")
                and old_meta.get("size") == (expected_size or 0)
                and old_meta.get("sha256") == (expected_sha256 or "")
            ):
                meta_valid = True
        except Exception:
            meta_valid = False

    if part.is_file():
        if not meta_valid or (expected_size and part.stat().st_size > expected_size):
            part.unlink(missing_ok=True)

    try:
        meta_path.write_text(
            json.dumps({
                "tag": tag,
                "asset": asset_name or "",
                "size": expected_size or 0,
                "sha256": expected_sha256 or "",
            }),
            encoding="utf-8",
        )
    except Exception:
        pass

    _PROGRESS.update({"received": part.stat().st_size if part.is_file() else 0})

    try:
        asset_url = await _resolve_asset_url(tag, asset_name)

        # 通道链：复用应用运行中的内核代理（runtime_port）+ 本地混合端口 + 镜像，
        # 直连 GitHub 国内基本不可用——更新下载必须用应用自身可用的出口，否则全应用
        # 只有这一处网络操作不走代理，表现为「检查更新能拿到清单、下载却卡死/失败」
        attempts = clash_manager.runtime._download_attempts()
        channels = _channel_plan(asset_url, attempts)
        probes = await _probe_channels(channels, expected_size)

        # 「资产不存在」的两种成因一起判：**明确 404**（直连 GitHub 的判定）与
        # **回了别的体积**（镜像对不存在的资产常回 200 + 几百字节的错误页，
        # 体积远小于真实资产，可据体积差提前判缺）。
        def _unusable(p: dict) -> bool:
            return bool(p["missing"] or p["bogus"])

        # 权威判定优先：GitHub 直连说 404 = 这个版本真没发（清单可能先于 release
        # 发布）。镜像说 404 不作数——它可能只是没同步。
        if any(p.get("direct") and p["missing"] for p in probes):
            raise AssetMissing(f"版本 {tag} 的发布资产不存在（GitHub 返回 404）")
        if probes and all(_unusable(p) for p in probes):
            raise AssetMissing(f"版本 {tag} 的发布资产不可用（各通道 404 或体积不符）")

        usable = [p for p in probes if not _unusable(p)] or probes
        total = next((p["total"] for p in usable if p["total"]), expected_size)
        _PROGRESS.update({
            "phase": "download", "total": total or None,
            "percent": round(part.stat().st_size * 100 / total) if total and part.is_file() else (0 if total else None),
        })

        last_err: Exception | None = None
        for idx, probe in enumerate(usable):
            baseline = part.stat().st_size if part.is_file() else 0
            try:
                await _stream_to_file(
                    probe["url"], probe["proxy"], part, total,
                    min_rate_bps=0 if idx == len(usable) - 1 else _MIN_CHANNEL_RATE,
                    label=probe["label"],
                )
                # 落盘后必过的完整性闸：拿到的字节数对不上（少了 / 明显不像
                # 一个安装包）就当这条通道没干成活——**不进校验阶段**，否则
                # 用户看到的是「校验失败」，真因（通道返回了垃圾）被盖住。
                got = part.stat().st_size if part.is_file() else 0
                if got < _MIN_PACKAGE_BYTES or (expected_size and got != expected_size):
                    _truncate_to(part, baseline)
                    raise RuntimeError(
                        f"通道返回内容不完整（{got} 字节"
                        + (f"/期望 {expected_size}" if expected_size else "")
                        + "），换下一通道"
                    )

                # SHA256 校验闸（在通道循环内即刻校验）：
                # 镜像可能完整返回错误内容（如错误页或旧版），此时若直接退出循环
                # 会导致整体失败；必须立即清理损坏文件并尝试下一可用通道。
                if expected_sha256:
                    actual = await asyncio.to_thread(_sha256_of, part)
                    if actual.lower() != expected_sha256.strip().lower():
                        part.unlink(missing_ok=True)
                        baseline = 0
                        raise RuntimeError(
                            f"SHA256 校验失败（实际 {actual[:16]}… 期望 {expected_sha256[:16]}…），换下一通道"
                        )
                break
            except _Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 —— 换下一通道
                if part.is_file() and part.stat().st_size != baseline:
                    _truncate_to(part, baseline)
                log_event(
                    logger,
                    "更新下载通道不可用，换下一通道",
                    detail={
                        "通道": probe["label"],
                        # httpx 超时的 str() 常为空，退回异常类型
                        "原因": str(e) or type(e).__name__,
                    },
                )
                last_err = e
        else:
            raise RuntimeError(f"所有下载通道均失败：{last_err}")

        zip_path = staging / "update.zip"
        part.replace(zip_path)

        # sha256 校验（安全兜底防线）
        if expected_sha256:
            _PROGRESS.update({"phase": "verify"})
            actual = await asyncio.to_thread(_sha256_of, zip_path)
            if actual.lower() != expected_sha256.strip().lower():
                shutil.rmtree(staging, ignore_errors=True)
                raise RuntimeError(
                    f"SHA256 校验失败：期望 {expected_sha256[:16]}… 实得 {actual[:16]}…"
                )

        # 解包到 staging/holdexar/（zip 内根目录即 Holdexar/）
        _PROGRESS.update({"phase": "extract"})
        await asyncio.to_thread(_extract, zip_path, staging)
        meta_path.unlink(missing_ok=True)

        # 逐文件校验值入清单：换装宿主据此核验暂存载荷——解包后到换装前
        # 的窗口期内文件缺失/被改（含杀软隔离）都能在换装前被拒
        payload_root = staging / APP_NAME
        files = await asyncio.to_thread(_payload_hashes, payload_root)
        manifest = {
            "tag": tag,
            "sha256": expected_sha256,
            "ready": True,
            "attempt_id": str(uuid.uuid4()),
            "source_version": APP_VERSION,
            "files": files,
            "notes": notes or "",
        }
        (staging / STAGING_MANIFEST_NAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        # 新暂存包重新具备交接资格：上一包的「不支持换装」标记随包作废，
        # 上次换装失败的安装状态标记与交接账本一并解除——否则重新下载同一版本
        # 会被旧标记/旧账一直挡在自动换装之外。
        (staging / HANDOFF_UNSUPPORTED_MARK).unlink(missing_ok=True)
        # 换装失败标记与交接账本落在数据目录（与桌面壳写入侧同一路径判定，
        # 不是安装根——staging 在安装根下，staging.parent 对错文件）
        from app.core.paths import resolve_data_dir
        (resolve_data_dir() / SWAP_FAILED_FLAG).unlink(missing_ok=True)
        (staging.parent / HANDOFF_LEDGER_NAME).unlink(missing_ok=True)
        _PROGRESS.update({"ok": True, "running": False, "phase": "done", "percent": 100})
        log_event(
            logger,
            "更新包已下载暂存，重启后换装",
            tag="成功",
            detail={"版本": tag, "暂存目录": str(staging)},
        )
        return {"ok": True, "staging": str(staging), "tag": tag}
    except _Cancelled:
        # 用户主动取消：保留已下载的 .part（受 download_meta.json 保护），下次可接着下
        _PROGRESS.update({"running": False, "phase": None, "code": "cancelled",
                          "error": "已取消下载"})
        return {"ok": False, "cancelled": True}
    except AssetMissing as e:
        _PROGRESS.update({"running": False, "code": "asset_missing", "error": str(e)})
        raise
    except Exception as e:  # noqa: BLE001
        _PROGRESS.update({"running": False, "code": _error_code(e), "error": str(e)})
        raise
    finally:
        _PROGRESS["running"] = False


def _error_code(err: Exception) -> str:
    """失败归因（机器可读）：前端据此给不同文案，不靠解析错误字符串。"""
    text = str(err) or type(err).__name__
    if "SHA256" in text or "校验值" in text:
        return "verify_failed"
    if "所有下载通道" in text:
        return "network"
    return "error"


async def _resolve_asset_url(tag: str, asset_name: str | None) -> str:
    """确定资产下载地址：清单给了确切名就直连，否则回落到 API 反查。"""
    base = os.environ.get(f"{ENV_PREFIX}UPDATE_URL", "").rstrip("/")
    if base and asset_name:
        return f"{base}/{asset_name}"
    if asset_name:
        return f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{asset_name}"
    guessed = (
        f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/"
        f"{APP_NAME}-win64-v{tag.lstrip('vV')}.zip"
    )
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
            )
            if resp.status_code == 200:
                asset = _pick_asset(resp.json().get("assets") or [])
                if asset:
                    return str(asset["browser_download_url"])
    except Exception as e:  # noqa: BLE001 —— 反查失败按猜名继续（探测阶段会给出结论）
        log_event(
            logger,
            "更新资产列表反查失败，按命名规则直连",
            tag="降级",
            detail={"原因": str(e)},
        )
    return guessed


def _channel_plan(asset_url: str, attempts: list[tuple]) -> list[dict]:
    """候选通道表 = 出口（直连/内核代理/本地混合端口）× 镜像前缀。"""
    if "127.0.0.1" in asset_url or "localhost" in asset_url:
        return [{
            "label": "本地测试更新源",
            "url": asset_url,
            "proxy": None,
            "direct": True,
        }]
    plan: list[dict] = []
    for proxy, label in attempts:
        for mirror in _MIRRORS:
            plan.append({
                "label": f"{label}{'·镜像' if mirror else ''}",
                "url": mirror + asset_url if mirror else asset_url,
                "proxy": proxy,
                "direct": not mirror and proxy is None,
            })
    return plan


async def _probe_channels(channels: list[dict], fallback_total: int | None) -> list[dict]:
    """并发探测全部候选通道：判资产存不存在 + 量延迟，可用者按延迟升序返回。

    用 `Range: bytes=0-0` 而不是 HEAD：部分镜像对 HEAD 返回 403/405，Range
    请求则是它们代理真实下载时走的那条路，探测结论与真实下载一致。
    探不到的通道**不丢弃**，排到最后——全部探不到时总得有条路可试。
    """
    if not channels:
        return []

    async def one(channel: dict) -> dict:
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(_PROBE_TIMEOUT, connect=5),
                proxy=channel["proxy"], follow_redirects=True,
            ) as client:
                async with client.stream(
                    "GET", channel["url"], headers={"Range": "bytes=0-0"}
                ) as resp:
                    total = _total_from_headers(resp.headers, resp.status_code)
                    # bogus：回了 200 但体积根本不是这个包（镜像的「没有此资产」
                    # 错误页就是这样）——算作不可用，别拿它下。
                    bogus = bool(
                        (total and total < _MIN_PACKAGE_BYTES)
                        or (fallback_total and total and total != fallback_total)
                    )
                    return {
                        **channel,
                        "status": resp.status_code,
                        "total": total or fallback_total,
                        # 404/410 = 资产不存在（不是通道问题）：快失败的依据
                        "missing": resp.status_code in (404, 410),
                        "bogus": bogus,
                        "latency": time.monotonic() - started,
                    }
        except Exception as e:  # noqa: BLE001 —— 探不到按「慢」处理，排最后
            return {
                **channel, "status": None, "total": fallback_total,
                "missing": False, "bogus": False, "latency": 9e9, "error": e,
            }

    results = await asyncio.gather(*(one(c) for c in channels))
    return sorted(results, key=lambda r: r["latency"])


def _truncate_to(path: Path, size: int) -> None:
    """把续传文件截回某个字节数（本轮这条通道写进去的都作废）。

    换通道后新内容会**追加**在同一文件上，若不先把坏字节截掉，几条通道的
    响应会拼成一个永远校验不过的缝合怪。
    """
    try:
        if not path.is_file():
            return
        current = path.stat().st_size
        if current > size:
            with path.open("r+b") as fh:
                fh.truncate(size)
        elif current < size:
            # 文件小于基线（如重头下载中断），禁止 truncate 补零膨胀
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 —— 截不动就整个删掉，宁可重下
        path.unlink(missing_ok=True)


def _total_from_headers(headers, status: int) -> int | None:
    """从响应头取**整包**体积。

    206（续传/范围响应）的总量在 content-range 的 `/total` 段；200 的总量才是
    content-length。两者混用会把「1 字节探测响应」当成整包体积。
    """
    content_range = headers.get("content-range") or ""
    if "/" in content_range:
        tail = content_range.rsplit("/", 1)[1]
        return int(tail) if tail.isdigit() else None
    if status == 206:
        return None
    raw = headers.get("content-length")
    return int(raw) if raw and raw.isdigit() else None


async def _stream_to_file(
    url: str,
    proxy: str | None,
    dest: Path,
    expected_total: int | None = None,
    min_rate_bps: int = 0,
    label: str | None = None,
    progress_dict: dict | None = None,
) -> None:
    """流式下载 + 断点续传 + 停滞/速率双闸换道。失败抛错（调用方换通道）。

    progress_dict：进度写入的目标字典。默认 None = 写本模块的 _PROGRESS。
    外部调用方传入各自私有字典，避免并发下载污染彼此进度。

    - **续传**：带上已有字节数发 `Range: bytes=N-`；服务端回 200（不认 Range）
      先下载到副产物文件，全包完整后再原子替换——避免中途失败抹掉已有续传进度，
      更避免向后回滚导致 truncate 补零膨胀成坏包。
    - **停滞闸**：任一字节到达间隔超过 _STALL_TIMEOUT 即判死（长读超时意味着
      「连上了但不吐数据」的半死通道能让进度条干等近一分钟）。
    - **速率闸**：试跑 _TRIAL_SECONDS 后平均速率低于下限判「过慢」——只靠停滞
      闸抓不住「慢但在动」的通道（曾有镜像 40KB/s 能一直动，整包要一小时）。
    """
    prog = progress_dict if progress_dict is not None else _PROGRESS
    resumed = dest.stat().st_size if dest.is_file() else 0
    headers = {"Range": f"bytes={resumed}-"} if resumed else {}
    timeout = httpx.Timeout(_STALL_TIMEOUT, connect=15)
    async with httpx.AsyncClient(
        timeout=timeout, proxy=proxy, follow_redirects=True
    ) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code not in (200, 206):
                resp.raise_for_status()
            use_sidecar = bool(resumed and resp.status_code == 200)
            if resp.status_code == 200:
                resumed = 0  # 服务端不认 Range：必须从头下
            total = _total_from_headers(resp.headers, resp.status_code) or expected_total
            received = resumed
            prog.update({"total": total or None, "channel": label})

            started = time.monotonic()
            window_at = started
            window_bytes = 0
            target_path = dest.with_name(dest.name + ".full") if use_sidecar else dest
            try:
                with target_path.open("wb" if use_sidecar or not received else "ab") as f:
                    chunks = resp.aiter_bytes(1 << 20)
                    while True:
                        if _CANCEL["flag"]:
                            raise _Cancelled("已取消下载")
                        try:
                            chunk = await asyncio.wait_for(
                                chunks.__anext__(), timeout=_STALL_TIMEOUT
                            )
                        except StopAsyncIteration:  # noqa: PERF203 —— 流结束的正常出口
                            break
                        except asyncio.TimeoutError as e:
                            raise RuntimeError(
                                f"通道停滞（{_STALL_TIMEOUT:.0f}s 无数据），换下一通道"
                            ) from e
                        f.write(chunk)
                        received += len(chunk)
                        window_bytes += len(chunk)
                        now = time.monotonic()
                        if now - window_at >= 1.0:
                            prog.update({
                                "speed": int(window_bytes / (now - window_at)),
                            })
                            window_at, window_bytes = now, 0
                        prog.update({
                            "received": received,
                            "percent": round(received * 100 / total) if total else None,
                        })
                        if min_rate_bps:
                            elapsed = now - started
                            if elapsed >= _TRIAL_SECONDS and (
                                (received - resumed) / elapsed < min_rate_bps
                            ):
                                raise RuntimeError(
                                    f"通道过慢（{(received - resumed) / elapsed / 1024:.0f} KB/s < "
                                    f"{min_rate_bps / 1024:.0f} KB/s），换下一通道"
                                )
                if use_sidecar:
                    target_path.replace(dest)
            finally:
                if use_sidecar and target_path.is_file():
                    target_path.unlink(missing_ok=True)


def cancel_download() -> dict:
    """中止进行中的下载（前端「取消」）：置位旗标，下载循环下一个分片即退出。

    不用 task.cancel()：协程被取消时中间态的 .part 会留成半截且无从收拾，
    旗标让下载循环自己走到安全点再退出（已下部分保留，下次可续传）。
    """
    _CANCEL["flag"] = True
    return {"cancelled": True}


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(zip_path: Path, staging: Path) -> None:
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(staging)
    zip_path.unlink(missing_ok=True)


def _payload_hashes(payload_root: Path) -> dict[str, str]:
    """暂存载荷逐文件 sha256（相对载荷根，posix 路径键）。"""
    return {
        path.relative_to(payload_root).as_posix(): _sha256_of(path)
        for path in sorted(payload_root.rglob("*")) if path.is_file()
    }


# ── 换装状态查询 ─────────────────────────────────────────────────────


def _completed_update_transaction(transaction: dict | None) -> bool:
    return bool(
        isinstance(transaction, dict) and transaction.get("attemptId")
        and transaction.get("stage") == "committed"
        and transaction.get("currentVersion") == transaction.get("targetVersion") == APP_VERSION
        and (transaction.get("dataAccepted") is True or (
            transaction.get("dataHadDatabase") is False and transaction.get("dataPrepared") is not True
        ))
    )


def _read_update_diagnosis() -> dict:
    from app.core import layout
    from app.core.paths import install_root, logs_dir

    root = install_root()
    transaction = layout.read_transaction(root) or {}
    records = []
    for path in (root / "logs/update-diagnosis.json", logs_dir() / "update-diagnosis.json",
                 root / "data/logs/update-diagnosis.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            records.append(data)
    if transaction.get("attemptId"):
        data = next((r for r in records if r.get("attemptId") == transaction["attemptId"]
                     and r.get("targetVersion") == transaction.get("targetVersion")), {})
    else:
        data = records[0] if records else {}
    if _completed_update_transaction(transaction):
        acknowledged = (data.get("stage") == "committed" and data.get("success") is True
                        and data.get("acknowledged") is True)
        data = dict(data, attemptId=transaction["attemptId"], targetVersion=APP_VERSION,
                    currentVersion=APP_VERSION, sourceVersion=transaction.get("sourceVersion"),
                    stage="committed", success=True, acknowledged=acknowledged)
    return data


def just_updated_status() -> dict | None:
    """投影已提交的更新事实及同次尚未确认的成功提示。"""
    return _just_updated_status(_read_update_diagnosis())


def _just_updated_status(data: dict) -> dict | None:
    from app.core.paths import install_root

    if not (data.get("success") is True and data.get("stage") == "committed"):
        return None
    if data.get("acknowledged") is True:
        return None
    target_ver = data.get("targetVersion") or ""
    if target_ver and target_ver != APP_VERSION:
        return None
    prev_ver = (
        data.get("sourceVersion")
        or data.get("previousVersion")
        or data.get("currentVersion")
        or ""
    )
    if not prev_ver:
        from app.core import layout
        try:
            st = layout.read_current_state(install_root())
            prev_ver = st.get("lastKnownGood") or st.get("previousVersion") or ""
        except Exception:
            pass

    notes = data.get("notes") or ""
    if not notes:
        for notes_cand in [
            install_root() / "release" / "latest.json",
            install_root() / "latest.json",
            install_root() / "RELEASE_NOTES.md",
            install_root() / "release" / "RELEASE_NOTES.md",
        ]:
            if notes_cand.is_file():
                try:
                    if notes_cand.suffix == ".json":
                        j = json.loads(notes_cand.read_text(encoding="utf-8"))
                        if j.get("notes"):
                            notes = _plain_notes(str(j["notes"]))
                            break
                    elif notes_cand.suffix == ".md":
                        notes = _plain_notes(notes_cand.read_text(encoding="utf-8"))
                        break
                except Exception:
                    pass

    return {
        "previousVersion": prev_ver,
        "currentVersion": target_ver or APP_VERSION,
        "notes": notes,
    }


def acknowledge_update() -> dict:
    """在更新互斥内持久化当前成功提示的确认，写入失败保留未确认状态。"""
    from app.core import update_host
    from app.core.update_process import update_mutex
    from app.core.paths import install_root

    root = install_root()
    with update_mutex():
        data = _read_update_diagnosis()
        if not (data.get("success") is True and data.get("stage") == "committed"
                and data.get("targetVersion", APP_VERSION) == APP_VERSION):
            return {"acknowledged": False, "updated": False}
        if data.get("acknowledged") is True:
            return {"acknowledged": True, "updated": False}
        data["acknowledged"] = True
        update_host._write_diagnosis(root, data, strict=True)
    return {"acknowledged": True, "updated": True}


def pending_status() -> dict:
    """staging 就绪状态（前端「重启以完成更新」提示的依据）。

    swapFailed 投影换装失败标记：换装失败只有原生弹窗一个出口，前端拿不到
    这个事实就没法给「重新下载」的出路（账本同源：标记是唯一事实源，这里只读）。
    justUpdated 投影换装成功首次启动事实，前端据此弹出更新成功报告。
    """
    from app.core import layout
    from app.core.paths import install_root

    transaction = layout.read_transaction(install_root())
    diagnosis = _read_update_diagnosis()
    installing = bool(
        isinstance(transaction, dict)
        and transaction.get("attemptId")
        and transaction.get("targetVersion") == APP_VERSION
        and transaction.get("stage") in (layout.STAGE_VERIFYING, layout.STAGE_COMMITTED)
        and diagnosis.get("attemptId") == transaction["attemptId"]
        and diagnosis.get("targetVersion") == APP_VERSION
        and diagnosis.get("stage") == layout.STAGE_SWITCHING
        and not diagnosis.get("error")
    )
    staging = staging_dir()
    manifest = staging / "manifest.json"
    from app.core.paths import resolve_data_dir
    swap_failed = (resolve_data_dir() / SWAP_FAILED_FLAG).is_file()
    result = {
        "pending": False,
        "installing": installing,
        "swapFailed": swap_failed,
        "justUpdated": _just_updated_status(diagnosis),
    }
    if not manifest.is_file():
        return result
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 坏 manifest 视为无暂存
        return result
    tag = data.get("tag")
    completed = (diagnosis.get("stage") == "committed" and diagnosis.get("success") is True
                 and diagnosis.get("targetVersion") == APP_VERSION)
    installed_target = (installing or completed) and str(tag or "").removeprefix("v") == APP_VERSION
    result.update(pending=bool(data.get("ready")) and not installed_target, tag=tag)
    return result


def clear_staging() -> dict:
    """放弃本次更新：清暂存目录（用户点「暂不更新」/换装失败回退）。

    正在下载时同时置取消旗标——否则目录被清掉后下载协程还在往里写，
    会在已删除的路径上重新造出半截 .part（下一次又启动成「有续传基线」）。
    """
    if _PROGRESS.get("running"):
        cancel_download()
    staging = staging_dir()
    shutil.rmtree(staging, ignore_errors=True)
    _progress_reset()
    return {"cleared": True}
