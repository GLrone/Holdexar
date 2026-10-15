"""按需获取资产种子：本地缺有效种子时从分发链拉取并立即并入。

包内不再内置种子（体积）；全新安装 / 升级后首次启动在后台下载 gzip 种子到
data/seed/，落位即跑一次 merge_seed_incremental 让数据当轮生效；失败静默等
下次启动重试（不阻塞启动、不重试风暴）。源码 clone 用户不走本模块（run.py
经 scripts/fetch_seed.py 落 assets/seed）。

通道复用 app.core.updater 的「出口 × 镜像」通道机（并发探测 / 断点续传 /
停滞换道）；候选表在自有分发直链与 GitHub 及其代理之间并列，谁先答且答得对
就用谁，用户无需选择。

完整性闸：清单哈希（权威，来自 latest.json 的 seed-archive 条目）→ gzip 解压
（CRC32/ISIZE 端到端）→ sqlite 打开 + seed_meta.version 非空。清单不可达时
退化为后两道——它们能挡住截断与坏档，但挡不住「内容被换成另一份自洽数据」，
所以哈希可得时一定用它。
"""
from __future__ import annotations

import asyncio
import gzip
import hashlib
import logging
import shutil
from pathlib import Path

from app.core.app_info import DATA_CDN_BASE, GITHUB_MIRRORS, GITHUB_REPO
from app.core.config import get_settings
from app.core.logging import log_event
from app.core.seed_assets import merge_seed_incremental, read_seed_meta, seed_db_path

logger = logging.getLogger(__name__)

SEED_GZ_NAME = "holdexar_seed.db.gz"
_PART_NAME = "holdexar_seed.db.gz.part"
# 体积下限：低于它必然不是种子（镜像对缺失资产常回几百字节的 200 错误页）
_MIN_SEED_BYTES = 1024 * 1024

# 下载进度（对齐 updater._PROGRESS 模式：协程写、端点读、dict 原子替换免锁）
_PROGRESS: dict = {}
_RUNNING = False


def _progress_reset() -> None:
    _PROGRESS.clear()
    _PROGRESS.update({
        "state": "idle", "received": 0, "total": None, "percent": None,
        "speed": 0, "error": None, "channel": None,
    })


def status() -> dict:
    if not _PROGRESS:
        _progress_reset()
    return dict(_PROGRESS)


async def status_snapshot() -> dict:
    """前端/运维状态：种子是否在位（含版本）+ 下载进度。"""
    path = seed_db_path()
    meta = read_seed_meta(path) if path.is_file() else {}
    version = str(meta.get("version") or "") if meta else ""
    snap = status()
    if version and not _RUNNING:
        snap["state"] = "ready"
    return {
        "present": bool(version),
        "version": version or None,
        "path": str(path) if version else None,
        **snap,
    }


def _data_seed_dir() -> Path:
    return get_settings().data_dir / "seed"


def _cdn_seed_url(cache_key: str) -> str:
    """自建分发上的种子地址；`cache_key` 非空时带上查询串做缓存区分。

    分发入口前面挂着一层 CDN，它按自己的规则缓存（边缘响应头给的缓存时长可能
    比对象自身的设置更长）。种子是**固定文件名、内容随版本变**的对象：不带区分
    串时，换版之后边缘会继续发旧档，客户端哈希校验失败、表现为反复重下同一份
    坏档。查询串取清单哈希前缀，换版即换地址，旧档自然过期。
    """
    base = f"{DATA_CDN_BASE.rstrip('/')}/seed/{SEED_GZ_NAME}"
    return f"{base}?v={cache_key}" if cache_key else base


def _mirrors() -> tuple[str, ...]:
    """GitHub 发布资产的镜像前缀链（单一事实源 app_info.GITHUB_MIRRORS）。"""
    return GITHUB_MIRRORS


async def _seed_manifest_entry() -> dict:
    """清单里声明的种子分发档条目（含 sha256 与 size）；不可达返回空字典。

    只认 `seed-archive`：`seed` 那条声明的是**未压缩的库**，拿它的哈希去比对
    下载到的 gz 必然不符。
    """
    try:
        from app.core.updater import _fetch_manifest

        data = await _fetch_manifest()
    except Exception as e:  # noqa: BLE001 —— 清单是增强项，取不到不影响下载
        log_event(
            logger,
            "种子清单不可用，退化为结构校验",
            tag="降级",
            detail={"原因": str(e)},
        )
        return {}
    for asset in (data or {}).get("assets") or []:
        if str(asset.get("role") or "") == "seed-archive":
            return {
                "sha256": str(asset.get("sha256") or "").lower(),
                "size": int(asset.get("size") or 0),
            }
    return {}


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _validate_seed(db_file: Path) -> str:
    """种子有效性：sqlite 可只读打开 + seed_meta.version 非空。返回 version。"""
    meta = read_seed_meta(db_file)
    version = str(meta.get("version") or "")
    if not version:
        raise RuntimeError("种子缺少 seed_meta.version（坏档）")
    return version


def _gunzip(src: Path, dest: Path) -> None:
    with gzip.open(src, "rb") as fin, dest.open("wb") as fout:
        shutil.copyfileobj(fin, fout, length=1 << 20)


async def ensure_seed_online() -> dict:
    """缺种子则下载（断点续传 / 多通道换道）→ 解压落位 → 立即并入。

    已有有效种子零开销直返；全程不抛（调用方是启动链尾部，失败只留状态）。
    """
    global _RUNNING
    if _RUNNING:
        return status()
    existing = seed_db_path()
    if existing.is_file():
        try:
            _validate_seed(existing)
            return await status_snapshot()
        except Exception:  # noqa: BLE001 —— 坏档等同无种子，走下载
            log_event(
                logger,
                "本地种子校验未通过，改走后端下载",
                tag="降级",
                detail={"种子路径": str(existing)},
                level=logging.WARNING,
            )
    _RUNNING = True
    _progress_reset()
    _PROGRESS.update({"state": "downloading"})
    try:
        from app.core.updater import (
            _MIN_PACKAGE_BYTES,
            _probe_channels,
            _stream_to_file,
            _truncate_to,
        )
        from app.domains.proxies import clash_manager

        seed_dir = _data_seed_dir()
        seed_dir.mkdir(parents=True, exist_ok=True)
        part = seed_dir / _PART_NAME
        gz_path = seed_dir / SEED_GZ_NAME
        db_tmp = seed_dir / "holdexar_seed.db.tmp"
        gz_path.unlink(missing_ok=True)
        db_tmp.unlink(missing_ok=True)

        entry = await _seed_manifest_entry()
        expected_sha = str(entry.get("sha256") or "")
        # 上一轮留下的半截档只在「还能接着下」时才留：它已经不小于清单声明的
        # 目标体积，说明那不是可续传的半截，而是坏档或换版残留，直接丢弃从头来。
        expected_size = int(entry.get("size") or 0)
        if part.is_file() and expected_size and part.stat().st_size >= expected_size:
            log_event(
                logger,
                "残留的种子下载临时档体积异常，丢弃重下",
                tag="清理",
                detail={"已有字节": part.stat().st_size, "目标字节": expected_size},
            )
            part.unlink(missing_ok=True)

        attempts = clash_manager.runtime._download_attempts()
        # 候选地址两类，**不交叉**：自建分发直链原样访问（GitHub 代理镜像对它
        # 没有意义，拼上去只会得到一个必然 404 的地址）；GitHub 尾部地址才逐
        # 个镜像前缀各出一份。再与出口（直连/内核代理）做笛卡尔积。
        endpoints: list[tuple[str, str]] = []
        if DATA_CDN_BASE:
            endpoints.append(
                ("·自建分发", _cdn_seed_url(expected_sha[:12]))
            )
        gh_tail = f"https://github.com/{GITHUB_REPO}/releases/latest/download/{SEED_GZ_NAME}"
        for mirror in _mirrors():
            endpoints.append(("·镜像" if mirror else "", mirror + gh_tail))
        channels = [
            {
                "label": f"{label}{suffix}",
                "url": url,
                "proxy": proxy,
                "direct": not suffix and proxy is None,
            }
            for proxy, label in attempts
            for suffix, url in endpoints
        ]
        probes = await _probe_channels(channels, None)
        usable = [p for p in probes if not (p["missing"] or p["bogus"])] or probes
        total = next((p["total"] for p in usable if p["total"]), None)
        _PROGRESS.update({"total": total, "percent": 0 if total else None})
        last_err: Exception | None = None
        for idx, probe in enumerate(usable):
            channel_start_bytes = part.stat().st_size if part.is_file() else 0
            try:
                await _stream_to_file(
                    probe["url"], probe["proxy"], part, total,
                    min_rate_bps=0 if idx == len(usable) - 1 else 64 * 1024,
                    label=probe["label"],
                    progress_dict=_PROGRESS,
                )
                got = part.stat().st_size if part.is_file() else 0
                if got < max(_MIN_SEED_BYTES, _MIN_PACKAGE_BYTES):
                    _truncate_to(part, channel_start_bytes)
                    raise RuntimeError(f"通道返回内容不完整（{got} 字节），换下一通道")

                # 若清单哈希可用，必须在通道循环内即刻校验：
                # 哈希不符说明镜像返回了错误内容（如错误页或旧版），
                # 必须清空临时文件并换下一通道，绝不能在循环结束后才抛错中断。
                if expected_sha:
                    actual = await asyncio.to_thread(_sha256_of, part)
                    if actual != expected_sha:
                        part.unlink(missing_ok=True)
                        channel_start_bytes = 0
                        raise RuntimeError(
                            f"种子哈希与清单不符（实际 {actual[:16]}…，期望 {expected_sha[:16]}…），换下一通道"
                        )
                break
            except Exception as e:  # noqa: BLE001 —— 换下一通道
                if part.is_file() and part.stat().st_size != channel_start_bytes:
                    _truncate_to(part, channel_start_bytes)
                log_event(
                    logger,
                    "下载通道不可用，改试下一通道",
                    detail={"通道": probe["label"], "原因": str(e) or type(e).__name__},
                )
                last_err = e
        else:
            raise RuntimeError(f"所有下载通道均失败：{last_err}")

        # 清单不可达时日志留痕，提示仅做结构校验
        if not expected_sha:
            log_event(
                logger,
                "清单哈希不可得，种子仅做结构与压缩校验",
                tag="降级",
                level=logging.WARNING,
            )

        part.replace(gz_path)
        _PROGRESS.update({"state": "extracting", "percent": 100})
        await asyncio.to_thread(_gunzip, gz_path, db_tmp)
        version = await asyncio.to_thread(_validate_seed, db_tmp)
        db_final = seed_dir / "holdexar_seed.db"
        db_final.unlink(missing_ok=True)
        db_tmp.replace(db_final)
        gz_path.unlink(missing_ok=True)
        log_event(
            logger,
            "按需获取种子已完成，开始并入数据",
            tag="成功",
            detail={
                "版本": version,
                "落位": str(db_final),
                "校验": "清单哈希" if expected_sha else "结构校验",
            },
        )

        _PROGRESS.update({"state": "merging"})
        await merge_seed_incremental()
        _PROGRESS.update({"state": "ready"})
        return await status_snapshot()
    except Exception as e:  # noqa: BLE001 —— 失败静默等下次启动
        log_event(
            logger,
            "按需获取种子失败，下次启动重试",
            tag="未完成",
            detail={"原因": str(e)},
            level=logging.WARNING,
        )
        _PROGRESS.update({"state": "failed", "error": str(e)[:200]})
        return status()
    finally:
        _RUNNING = False
        # 半截档**故意保留**：下一次启动的断点续传靠它接着下。它不被任何
        # 「种子是否在位」的判据认领（判据只看 holdexar_seed.db 的 seed_meta），
        # 所以留在盘上不会让程序误判数据已就绪。
