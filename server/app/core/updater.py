"""应用自更新：读更新清单 → 检查 → 下载暂存 → 校验 → 重启换装。

检查更新走**静态清单**（`latest.json`，挂在固定 tag `updater` 的 Release 资产下），
而不是每次打 api.github.com：
- 清单随版本发布更新，但地址恒定 → 检查更新永远是「读一个小 json」
- 免 GitHub API 的 60 次/小时限速，且 api.github.com 在国内比 release 资产更难可达
- 清单自带 sha256 与资产名，客户端不必再从 release body 里抠哈希（旧的兜底路径保留）

换装流程三段式（解压暂存 → 旧文件移备份目录 → 新文件落位），换装时机
放在**下次启动**（PyInstaller onedir 在 Windows 下运行中替换自身文件
句柄风险高；desktop/main.py --apply-update 在 uvicorn 线程起来之前完成
换装，无文件锁）。

数据安全（本项目核心承诺，优先于一切花哨功能）：
- 换装只动程序目录，data/ 等用户目录跳过（desktop 侧白名单）
- 下载产物落 data/update-staging/，sha256 校验不过不进换装链
- 旧程序目录整体移 __old__，失败可手工移回 = 永远有退路

网络通道：复用 clash_manager._download_attempts 的镜像 + 本地代理
发现链（国内网络直连 GitHub Releases 基本不可用）。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
import zipfile
from pathlib import Path

import httpx

from app.core.app_info import (
    APP_NAME,
    GITHUB_REPO,
    MANIFEST_ASSET,
    MANIFEST_TAG,
)
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# 仓库标识统一在 app.core.app_info.GITHUB_REPO（更新检查与种子获取共用）

# release 资产命名（build_release.py make_zip 产物）
_ASSET_PATTERN = re.compile(rf"^{re.escape(APP_NAME)}-win64-v[\w.-]+\.zip$", re.IGNORECASE)

# 更新清单的落点常量（MANIFEST_TAG / MANIFEST_ASSET）在 app.core.app_info，
# 与 scripts/build_manifest.py、scripts/publish_release.py 共用一份。
# 固定 tag 下挂 latest.json：地址与版本号解耦，检查更新只读这一个文件。

# 镜像前缀（空串 = 直连 GitHub）：同一份资产的不同入口，与下面代理列表
# 笛卡尔积成候选通道表。**全部并发探测**，谁先答且答得对就用谁。
# 与 scripts/fetch_seed.py 的 _MIRRORS 同源，改动请两边对齐。
_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "https://gh-proxy.net/", "")

STAGING_DIR = "update-staging"  # data/ 下的暂存目录
_PENDING_FILE = ".update-pending"  # 换装标记（staging 内 manifest.json 同目录）

# 探测：候选通道各发一笔 `Range: bytes=0-0`（只读响应头，几十字节代价），一次并发
# 同时判出「资产在不在」与「这条通道快不快」。旧实现是**串行试跑 20s 再换道**——
# 资产本身 404 也要把整条链蹚完才报错，用户看到的就是「点了没反应」。
_PROBE_TIMEOUT = 8.0
# 传输停滞判定：连续这么久一个字节都没到即换道（旧值 45s，镜像半死连接会让进度条
# 干等近一分钟才开始换道）。
_STALL_TIMEOUT = 15.0
# 速率考核：下载跑满 _TRIAL_SECONDS 后平均速率仍低于下限即换道；最后一个可用通道
# 不考核（全网都慢也得让它下完，不能把所有通道都试死）。
_MIN_CHANNEL_RATE = 64 * 1024  # B/s
_TRIAL_SECONDS = 15.0
# 续传落盘的临时名（.part：没校验通过前不得被当成完整包）
_PART_NAME = "update.zip.part"
# 安装包体积下限：小于它必然不是应用包（镜像对不存在的资产常回几百字节的
# 200 错误页——把它当下载成功，用户会看到「校验失败」而不是「没这个包」）。
_MIN_PACKAGE_BYTES = 1 * 1024 * 1024


def staging_dir() -> Path:
    d = get_settings().data_dir / STAGING_DIR
    return d


def manifest_url(mirror: str = "") -> str:
    """清单地址（mirror 为空 = 直连）。"""
    return (
        f"{mirror}https://github.com/{GITHUB_REPO}/releases/download/"
        f"{MANIFEST_TAG}/{MANIFEST_ASSET}"
    )


def _pick_asset(assets: list[dict], name: str | None = None) -> dict | None:
    """挑应用包资产：显式 name 精确匹配 → role=app（清单标记）→ 命名模式。

    一个函数同时吃两种输入：清单里的 assets（带 role）与 GitHub API 的
    release.assets（不带 role，只有文件名）。
    """
    if name:
        hit = next((a for a in assets if str(a.get("name")) == name), None)
        if hit:
            return hit
    hit = next((a for a in assets if str(a.get("role") or "") == "app"), None)
    if hit:
        return hit
    return next((a for a in assets if _ASSET_PATTERN.match(str(a.get("name", "")))), None)


async def _fetch_manifest() -> dict | None:
    """按镜像链取 latest.json。全部通道失败返回 None（调用方回落 GitHub API）。"""
    last_err: Exception | None = None
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        for mirror in _MIRRORS:
            try:
                resp = await client.get(manifest_url(mirror))
                if resp.status_code != 200:
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    continue
                data = resp.json()
                if isinstance(data, dict) and data.get("version"):
                    return data
                last_err = RuntimeError("清单缺 version 字段")
            except Exception as e:  # noqa: BLE001 —— 换下一镜像
                last_err = e
    logger.info("[更新] 清单不可用（%s），回落 GitHub API", last_err)
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

    asset = _pick_asset(data.get("assets") or [])
    if not asset:
        return {"available": False, "reason": "no_asset"}

    current = settings.version
    return {
        "available": _version_gt(latest, current),
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
        logger.info("[更新] 检查失败（网络）：%s", e)
        return {"available": False, "reason": "network", "error": str(e)[:200]}

    tag = str(release.get("tag_name") or "")
    asset = _pick_asset(release.get("assets") or [])
    if not tag or not asset:
        return {"available": False, "reason": "no_asset"}

    latest = tag.lstrip("vV")
    current = settings.version
    newer = _version_gt(latest, current)
    # GitHub 的 release 资产自带 digest（形如 "sha256:..."），有则用作校验值
    digest = str(asset.get("digest") or "")
    return {
        "available": newer,
        "current": current,
        "latest": latest,
        "tag": tag,
        "notes": _plain_notes(release.get("body") or ""),
        "sizeBytes": asset.get("size"),
        "sha256": digest.split(":", 1)[1] if digest.startswith("sha256:") else None,
        "asset": asset.get("name"),
        "publishedAt": release.get("published_at"),
        "source": "api",
    }


def _version_gt(a: str, b: str) -> bool:
    """点分数字比较：a > b 才更新（0.1.0 < 0.2.0 < 0.10.0）。

    非数字段按 0 处理；长度不齐补 0（0.2 == 0.2.0）。
    """
    def _parts(v: str) -> list[int]:
        out = []
        for seg in v.replace("-", ".").split("."):
            digits = re.sub(r"\D", "", seg)
            out.append(int(digits) if digits else 0)
        return out

    pa, pb = _parts(a), _parts(b)
    length = max(len(pa), len(pb))
    pa += [0] * (length - len(pa))
    pb += [0] * (length - len(pb))
    return pa > pb


def _plain_notes(body: str, limit: int = 1500) -> str:
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
    后者重试一万次还是 404（实测：清单已发布、release 还是草稿时，客户端
    对着 404 把整条通道链蹚完，用户看到的就是「下载永远下不动」）。
    """


class _Cancelled(RuntimeError):
    """用户主动取消下载（内部信号，不冒泡到前端当故障展示）。"""


async def download_update(
    tag: str,
    expected_sha256: str | None = None,
    asset_name: str | None = None,
    expected_size: int | None = None,
) -> dict:
    """下载 release zip 到 data/update-staging/ 并解包 + 校验。

    三段式（先探、再下、后校验），与旧实现的最大区别是**先探**：
      ① 并发向所有候选通道发 `Range: bytes=0-0`，一次判出「资产在不在」与
         「通道快不快」；全通道 404 立即抛 AssetMissing（旧实现要串行试跑
         整条链才报错，几十秒无反馈）；
      ② 按探测延迟从快到慢下载，**带断点续传**（换通道不丢已下部分）；
      ③ 停滞（_STALL_TIMEOUT 无字节）与速率考核双闸换道。

    asset_name：清单/检查结果给出的确切资产名。给了就直连下载，省掉一次
    API 反查（资产名含时间戳，猜不出来，只能反查或由清单提供）。
    expected_size：清单里的体积。通道不给 Content-Length（镜像分块响应）时
    用它当总量——否则百分比恒为空，前端进度条看着像卡死。
    expected_sha256：清单内的校验值优先；API 路径下取 release body 的
    `SHA256: <hex>` 行。两者皆无则跳过校验（前端展示「未提供校验值」）。
    校验不过 → 清 staging 抛错，绝不进换装链。
    """
    from app.domains.proxies import clash_manager

    _progress_reset()
    _CANCEL["flag"] = False
    # 开头就把总量挂上：探测期（最长 ~8s）还没吐数据，进度条若没有分母
    # 就完全不动——用户看到的是「点了没反应」。清单体积兜出 0% 起步即可。
    _PROGRESS.update({
        "running": True, "phase": "probe", "received": 0,
        "total": expected_size or None,
        "percent": 0 if expected_size else None,
    })

    staging = staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    part = staging / _PART_NAME
    # 续传基线：上次没下完的 .part 直接接着用（换通道/重开应用都不必从 0 开始）。
    # 体积对不上说明是别的版本的残留，弃掉重来。
    if part.is_file() and expected_size and part.stat().st_size > expected_size:
        part.unlink(missing_ok=True)
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
        # **回了别的体积**（镜像对不存在的资产常回 200 + 几百字节的错误页——
        # 实测 ghfast 对未发布资产返回 573 字节的 200，旧逻辑会把它当下载成功，
        # 最后死在 SHA256 校验上，报的却是「校验失败」，真因被盖住）。
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
                break
            except _Cancelled:
                raise
            except Exception as e:  # noqa: BLE001 —— 换下一通道
                logger.info(
                    "[更新] 通道不可用（%s）：%s",
                    probe["label"],
                    e or type(e).__name__,  # httpx 超时的 str() 常为空，退回异常类型
                )
                last_err = e
        else:
            raise RuntimeError(f"所有下载通道均失败：{last_err}")

        zip_path = staging / "update.zip"
        part.replace(zip_path)

        # sha256 校验（有期望值才校验）
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

        # 换装 manifest（desktop 启动编排读）
        manifest = {
            "tag": tag,
            "sha256": expected_sha256,
            "ready": True,
        }
        (staging / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        _PROGRESS.update({"ok": True, "running": False, "phase": "done", "percent": 100})
        logger.info("[更新] %s 已暂存至 %s（重启后换装）", tag, staging)
        return {"ok": True, "staging": str(staging), "tag": tag}
    except _Cancelled:
        part.unlink(missing_ok=True)
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
    if "SHA256" in text:
        return "verify_failed"
    if "所有下载通道" in text:
        return "network"
    return "error"


async def _resolve_asset_url(tag: str, asset_name: str | None) -> str:
    """确定资产下载地址：清单给了确切名就直连，否则回落到 API 反查。"""
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
        logger.info("[更新] 资产列表反查失败（%s），按命名规则直连", e)
    return guessed


def _channel_plan(asset_url: str, attempts: list[tuple]) -> list[dict]:
    """候选通道表 = 出口（直连/内核代理/本地混合端口）× 镜像前缀。

    `direct` 标出「既不经镜像、也不经代理」的那一条——它的判定是**权威的**：
    GitHub 自己对它说 404，就是真没有这个资产（镜像说 404 可能只是没同步）。
    """
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
        with path.open("r+b") as fh:
            fh.truncate(size)
    except Exception:  # noqa: BLE001 —— 截不动就整个删掉，宁可重下
        path.unlink(missing_ok=True)


def _total_from_headers(headers, status: int) -> int | None:
    """从响应头取**整包**体积。

    206（续传/范围响应）的总量在 content-range 的 `/total` 段；200 的总量才是
    content-length。两者混用会把「1 字节探测响应」当成整包体积（实测踩过）。
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
) -> None:
    """流式下载 + 断点续传 + 停滞/速率双闸换道。失败抛错（调用方换通道）。

    - **续传**：带上已有字节数发 `Range: bytes=N-`；服务端回 200（不认 Range）
      才从头下——不清零会把新流追加到旧文件后面，拼出一个校验必挂的坏包。
    - **停滞闸**：任一字节到达间隔超过 _STALL_TIMEOUT 即判死（旧实现的 45s
      读超时意味着「连上了但不吐数据」的半死通道能让进度条干等近一分钟）。
    - **速率闸**：试跑 _TRIAL_SECONDS 后平均速率低于下限判「过慢」——只靠停滞
      闸抓不住「慢但在动」的通道（实测某镜像 40KB/s 能一直动，整包要一小时）。
    """
    resumed = dest.stat().st_size if dest.is_file() else 0
    headers = {"Range": f"bytes={resumed}-"} if resumed else {}
    timeout = httpx.Timeout(_STALL_TIMEOUT, connect=15)
    async with httpx.AsyncClient(
        timeout=timeout, proxy=proxy, follow_redirects=True
    ) as client:
        async with client.stream("GET", url, headers=headers) as resp:
            if resp.status_code not in (200, 206):
                resp.raise_for_status()
            if resp.status_code == 200:
                resumed = 0  # 服务端不认 Range：必须从头下
            total = _total_from_headers(resp.headers, resp.status_code) or expected_total
            received = resumed
            _PROGRESS.update({"total": total or None, "channel": label})

            started = time.monotonic()
            window_at = started
            window_bytes = 0
            with dest.open("ab" if received else "wb") as f:
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
                        _PROGRESS.update({
                            "speed": int(window_bytes / (now - window_at)),
                        })
                        window_at, window_bytes = now, 0
                    _PROGRESS.update({
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


# ── 换装状态查询 ─────────────────────────────────────────────────────


def pending_status() -> dict:
    """staging 就绪状态（前端「重启以完成更新」提示的依据）。"""
    staging = staging_dir()
    manifest = staging / "manifest.json"
    if not manifest.is_file():
        return {"pending": False}
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 坏 manifest 视为无暂存
        return {"pending": False}
    return {"pending": bool(data.get("ready")), "tag": data.get("tag")}


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
