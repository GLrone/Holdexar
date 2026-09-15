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

# 通道链：镜像优先，直连兜底（GitHub 资产直连在国内基本不可用）。
# 与 scripts/fetch_seed.py 的 _MIRRORS 同源，改动请两边对齐。
_MIRRORS = ("https://ghfast.top/", "https://gh-proxy.com/", "")

STAGING_DIR = "update-staging"  # data/ 下的暂存目录
_PENDING_FILE = ".update-pending"  # 换装标记（staging 内 manifest.json 同目录）

# 通道速率考核：镜像/直连的存在感差异极大（实测同一个 118MB 包，某镜像 40KB/s、
# 直连 1.2MB/s——慢通道会让整包拖近一小时，用户看到的就是「进度条不动、像卡死」）。
# 试跑期过后平均速率低于下限即判该通道为「慢」并换下一个；最后一个通道不做考核
# （全网都慢时也得让它下完，不能把所有通道都试死）。
_MIN_CHANNEL_RATE = 128 * 1024  # B/s
_TRIAL_SECONDS = 20.0


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


def _progress_reset() -> None:
    _PROGRESS.clear()
    _PROGRESS.update({
        "running": False, "phase": None, "percent": None,
        "received": 0, "total": None, "error": None, "ok": False,
    })


def download_progress() -> dict:
    if not _PROGRESS:
        _progress_reset()
    return dict(_PROGRESS)


async def download_update(
    tag: str,
    expected_sha256: str | None = None,
    asset_name: str | None = None,
    expected_size: int | None = None,
) -> dict:
    """下载 release zip 到 data/update-staging/ 并解包 + 校验。

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
    # 开头就把总量挂上：通道试跑期（最长 ~20s）还没吐数据，进度条若没有分母
    # 就完全不动——用户看到的是「点了没反应」。清单体积兜出 0% 起步即可。
    _PROGRESS.update({
        "running": True, "phase": "download", "received": 0,
        "total": expected_size or None,
        "percent": 0 if expected_size else None,
    })

    staging = staging_dir()
    try:
        if asset_name:
            asset_url = (
                f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/{asset_name}"
            )
        else:
            # 无确切名：先按模式猜一个，再从 release 资产列表反查真名覆盖
            asset_url = (
                f"https://github.com/{GITHUB_REPO}/releases/download/{tag}/"
                f"{APP_NAME}-win64-v{tag.lstrip('vV')}.zip"
            )
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.get(
                    f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{tag}"
                )
                if resp.status_code == 200:
                    asset = _pick_asset(resp.json().get("assets") or [])
                    if asset:
                        asset_url = asset["browser_download_url"]

        # 通道链：复用应用运行中的内核代理（runtime_port）+ 本地混合端口 + 镜像，
        # 直连 GitHub 国内基本不可用——更新下载必须用应用自身可用的出口，否则全应用
        # 只有这一处网络操作不走代理，表现为「检查更新能拿到清单、下载却卡死/失败」
        attempts = clash_manager.runtime._download_attempts()

        zip_path = staging / "update.zip"
        staging.mkdir(parents=True, exist_ok=True)
        # 通道链平铺成 (代理, 镜像) 组合；**最后一个不做速率考核**（全慢也得下完）
        channels = [
            (proxy, mirror) for proxy, _label in attempts for mirror in _MIRRORS
        ]
        last_err: Exception | None = None
        for idx, (proxy, mirror) in enumerate(channels):
            url = mirror + asset_url if mirror else asset_url
            try:
                await _stream_to_file(
                    url, proxy, zip_path, expected_size,
                    min_rate_bps=0 if idx == len(channels) - 1 else _MIN_CHANNEL_RATE,
                )
                break
            except Exception as e:  # noqa: BLE001 —— 换下一通道
                logger.info(
                    "[更新] 通道不可用（%s）：%s",
                    mirror or "直连",
                    e or type(e).__name__,  # httpx 超时的 str() 常为空，退回异常类型
                )
                last_err = e
        else:
            raise RuntimeError(f"所有下载通道均失败：{last_err}")

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
    except Exception as e:  # noqa: BLE001
        _PROGRESS.update({"running": False, "error": str(e)})
        raise
    finally:
        _PROGRESS["running"] = False


async def _stream_to_file(
    url: str,
    proxy: str | None,
    dest: Path,
    expected_total: int | None = None,
    min_rate_bps: int = 0,
) -> None:
    """流式下载 + 进度上报。失败抛错（调用方换通道）。

    读超时 45s：镜像常见「连上了但几乎不吐数据」的半死通道，300s 的读超时意味着
    用户对着 0% 干等五分钟才轮到下一个通道（实测）。
    min_rate_bps > 0 时做速率考核：试跑 _TRIAL_SECONDS 后平均速率仍低于下限即
    判「过慢」抛错换道——只靠读超时抓不住「慢但在动」的通道（实测某镜像 40KB/s
    能一直动，整包要一小时）。
    """
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(45, connect=15), proxy=proxy, follow_redirects=True
    ) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            # 总量：优先响应头；镜像分块响应不给 Content-Length 时回落清单体积
            # （否则 percent 恒 None，前端进度条不动，看着像卡死）
            total = int(resp.headers.get("content-length") or 0) or (expected_total or 0)
            _PROGRESS.update({"total": total or None})
            received = 0
            started = time.monotonic()
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(1 << 20):
                    f.write(chunk)
                    received += len(chunk)
                    _PROGRESS.update({
                        "received": received,
                        "percent": round(received * 100 / total) if total else None,
                    })
                    if min_rate_bps:
                        elapsed = time.monotonic() - started
                        if elapsed >= _TRIAL_SECONDS and received / elapsed < min_rate_bps:
                            raise RuntimeError(
                                f"通道过慢（{received / elapsed / 1024:.0f} KB/s < "
                                f"{min_rate_bps / 1024:.0f} KB/s），换下一通道"
                            )


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
    """放弃本次更新：清暂存目录（用户点「暂不更新」/换装失败回退）。"""
    staging = staging_dir()
    shutil.rmtree(staging, ignore_errors=True)
    return {"cleared": True}
