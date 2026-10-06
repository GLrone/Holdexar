"""按需获取资产种子：本地缺有效种子时从 GitHub Release 拉取并立即并入。

包内不再内置种子（体积）；全新安装 / 升级后首次启动在后台下载 gzip 种子到
data/seed/，落位即跑一次 merge_seed_incremental 让数据当轮生效；失败静默等
下次启动重试（不阻塞启动、不重试风暴）。源码 clone 用户不走本模块（run.py
经 scripts/fetch_seed.py 落 assets/seed）。

通道复用 app.core.updater 的「出口 × 镜像」通道机（并发探测 / 断点续传 /
停滞换道）；种子资产挂 releases/latest/download（固定文件名，地址恒定）。
完整性闸：gzip 解压 + sqlite 打开 + seed_meta.version 非空（种子由本项目
构建，gzip CRC + 结构校验即可，无需外部 sha256）。
"""
from __future__ import annotations

import asyncio
import gzip
import logging
import shutil
from pathlib import Path

from app.core.app_info import GITHUB_REPO
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


def _mirrors() -> tuple[str, ...]:
    from app.core.updater import _MIRRORS

    return _MIRRORS


def _tail_url() -> str:
    return f"https://github.com/{GITHUB_REPO}/releases/latest/download/{SEED_GZ_NAME}"


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
        from app.core.updater import _MIN_PACKAGE_BYTES, _probe_channels, _stream_to_file
        from app.domains.proxies import clash_manager

        seed_dir = _data_seed_dir()
        seed_dir.mkdir(parents=True, exist_ok=True)
        part = seed_dir / _PART_NAME
        gz_path = seed_dir / SEED_GZ_NAME
        db_tmp = seed_dir / "holdexar_seed.db.tmp"
        for stale in (part, gz_path, db_tmp):
            stale.unlink(missing_ok=True)

        attempts = clash_manager.runtime._download_attempts()
        tail = _tail_url()
        channels = [
            {"label": f"{label}{'·镜像' if mirror else ''}", "url": mirror + tail,
             "proxy": proxy, "direct": not mirror and proxy is None}
            for proxy, label in attempts
            for mirror in _mirrors()
        ]
        probes = await _probe_channels(channels, None)
        usable = [p for p in probes if not (p["missing"] or p["bogus"])] or probes
        total = next((p["total"] for p in usable if p["total"]), None)
        _PROGRESS.update({"total": total, "percent": 0 if total else None})
        last_err: Exception | None = None
        for idx, probe in enumerate(usable):
            try:
                await _stream_to_file(
                    probe["url"], probe["proxy"], part, total,
                    min_rate_bps=0 if idx == len(usable) - 1 else 64 * 1024,
                    label=probe["label"],
                )
                got = part.stat().st_size if part.is_file() else 0
                if got < max(_MIN_SEED_BYTES, _MIN_PACKAGE_BYTES):
                    raise RuntimeError(f"通道返回内容不完整（{got} 字节），换下一通道")
                break
            except Exception as e:  # noqa: BLE001 —— 换下一通道
                log_event(
                    logger,
                    "下载通道不可用，改试下一通道",
                    detail={"通道": probe["label"], "原因": e or type(e).__name__},
                )
                last_err = e
        else:
            raise RuntimeError(f"所有下载通道均失败：{last_err}")

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
            detail={"版本": version, "落位": str(db_final)},
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
        _data_seed_dir().joinpath(_PART_NAME).unlink(missing_ok=True)
