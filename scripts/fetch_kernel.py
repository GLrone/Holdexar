"""内核资产获取：把 mihomo 内核与 GeoIP 数据补进 assets/clash/（随包资产）。

**为什么内核不进 git**：可执行文件约 45MB、GeoIP 数据约 30MB，且每次升版本都会
变——入库等于每个版本往仓库历史里永久塞一份二进制。所以它与资产种子同走
「随包资产」通道：发行包内置一份（spec datas 的 `clash/`），源码 clone 由 run.py
首次启动调用本脚本补齐；发布机靠本脚本把 assets/clash/ 备齐，打包时随包分发。

版本与地址常量在 `server/app/domains/proxies/kernel_release.py`（单一来源）：
随包内核与运行时的网络下载兜底共用同一个版本号。

用法：
    python scripts/fetch_kernel.py                    # 缺则补齐；已齐且有效则跳过
    python scripts/fetch_kernel.py --force            # 强制重下（升版本后用）
    python scripts/fetch_kernel.py --from-dir <目录>  # 从本机现成内核目录复制
                                                      #（离线场景；版本按原样，需自行核对）
"""
from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from app.domains.proxies.kernel_release import (  # noqa: E402
    GEO_ASSETS,
    LICENSE_ASSETS,
    MIHOMO_VERSION,
    MIRRORS,
    geo_url,
    kernel_filename,
    mihomo_asset,
    mihomo_url,
)

CLASH_DIR = ROOT / "assets" / "clash"
KERNEL_FILE = CLASH_DIR / kernel_filename()

# 有效性下限：挡半截下载与占位文件（mihomo 约 45MB，GeoIP 数据 4~17MB，许可约 35KB）
_MIN_KERNEL_BYTES = 5 * 1024 * 1024
_MIN_DATA_BYTES = 1024 * 1024
_MIN_LICENSE_BYTES = 8 * 1024
_TIMEOUT_S = 300


def _download(url: str, dest: Path) -> None:
    """流式下载到 `<dest>.part` 再原子改名——中断不会留下假冒可用的半截文件。"""
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Holdexar-kernel-fetcher"})
    with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp, tmp.open("wb") as f:
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
    tmp.replace(dest)


def _fetch(rel_url: str, dest: Path, min_bytes: int, label: str) -> tuple[bool, str]:
    """逐镜像下载单个资产；成功返回 (True, "")，全败返回 (False, 最后错误)。"""
    last_err = ""
    for mirror in MIRRORS:
        url = mirror + rel_url if mirror else rel_url
        label_of_channel = mirror or "直连"
        print(f"[内核] 下载 {label}（{label_of_channel}）…")
        try:
            _download(url, dest)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_err = f"{label_of_channel}：{e}"
            continue
        size = dest.stat().st_size
        if size < min_bytes:
            last_err = f"{label_of_channel}：内容过小（{size} 字节，疑似无效档）"
            dest.unlink(missing_ok=True)
            continue
        print(f"[内核] {label} 完成（{size / 1048576:.1f} MB）")
        return True, ""
    return False, last_err


def _fetch_mihomo() -> tuple[bool, str]:
    """下载 mihomo 发布资产并解出可执行文件（zip: 取 exe；gz: 单文件）。"""
    asset_name, shape = mihomo_asset()
    archive = CLASH_DIR / f".{asset_name}"
    ok, err = _fetch(mihomo_url(), archive, _MIN_KERNEL_BYTES, f"mihomo {MIHOMO_VERSION}")
    if not ok:
        return False, err
    try:
        if shape == "zip":
            with zipfile.ZipFile(archive) as z:
                exe_names = [n for n in z.namelist() if n.endswith(".exe")]
                if not exe_names:
                    return False, "压缩包内未找到可执行文件"
                KERNEL_FILE.write_bytes(z.read(exe_names[0]))
        else:
            KERNEL_FILE.write_bytes(gzip.decompress(archive.read_bytes()))
            KERNEL_FILE.chmod(0o755)
    finally:
        archive.unlink(missing_ok=True)
    print(f"[内核] 可执行文件就位：{KERNEL_FILE.name}")
    return True, ""


def _copy_from_dir(src_dir: Path) -> list[str]:
    """从本机现成内核目录复制内核与 GeoIP 数据（显式使用，不做任何自动检索）。"""
    CLASH_DIR.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in (kernel_filename(), *GEO_ASSETS):
        src = src_dir / name
        if src.is_file():
            shutil.copy2(src, CLASH_DIR / name)
            copied.append(name)
    if copied:
        print(f"[内核] 已从 {src_dir} 复制：{', '.join(copied)}")
    return copied


# 随包资产自带的来源/许可说明：与二进制同目录分发，让包内文件自己说清出处。
# （合规义务的完整表述在 docs/THIRD_PARTY_NOTICES.md，随包放在程序根目录。）
_SOURCE_NOTE = """Holdexar 随包内核资产（bundle assets）
==================================================
mihomo         {mihomo_version}
               来源 https://github.com/MetaCubeX/mihomo
               许可 GPL-3.0（原文见同目录 LICENSE-mihomo.txt）
GeoIP/GeoSite 数据
               来源 https://github.com/MetaCubeX/meta-rules-dat（滚动 tag: latest）
               许可 GPL-3.0（原文见同目录 LICENSE-meta-rules-dat.txt）
Country.mmdb   数据来源 MaxMind GeoLite2
               This product includes GeoLite2 data created by MaxMind,
               available from https://www.maxmind.com

mihomo 以独立进程运行（非链接、非派生），本项目自身以 MIT 分发；
对应源码即上游仓库上述地址，二进制未做任何修改。
"""


def _write_source_note() -> None:
    (CLASH_DIR / "README.txt").write_text(
        _SOURCE_NOTE.format(mihomo_version=MIHOMO_VERSION), encoding="utf-8"
    )


def _missing() -> list[str]:
    """尚缺（或明显不完整）的资产清单；空 = 随包资产已齐。"""
    checks = (
        [(KERNEL_FILE.name, _MIN_KERNEL_BYTES)]
        + [(name, _MIN_DATA_BYTES) for name in GEO_ASSETS]
        + [(name, _MIN_LICENSE_BYTES) for name in LICENSE_ASSETS]
    )
    missing = []
    for name, min_bytes in checks:
        target = CLASH_DIR / name
        if not target.is_file() or target.stat().st_size < min_bytes:
            missing.append(name)
    return missing


def fetch(force: bool = False, from_dir: Path | None = None) -> int:
    """补齐随包内核资产。返回进程退出码（0 成功/已就绪，1 失败）。"""
    CLASH_DIR.mkdir(parents=True, exist_ok=True)
    _write_source_note()
    if from_dir is not None:
        _copy_from_dir(from_dir)

    if not force:
        missing = _missing()
        if not missing:
            print(f"[内核] 随包资产已就绪（{CLASH_DIR}），跳过")
            return 0

    try:
        if force or not KERNEL_FILE.is_file():
            ok, err = _fetch_mihomo()
            if not ok:
                print(f"[内核] 可执行文件获取失败：{err}")
                return 1
        for name, asset in GEO_ASSETS.items():
            target = CLASH_DIR / name
            if target.is_file() and not force:
                continue
            ok, err = _fetch(geo_url(asset), target, _MIN_DATA_BYTES, name)
            if not ok:
                print(f"[内核] GeoIP 数据 {name} 获取失败：{err}")
                return 1
        for name, url in LICENSE_ASSETS.items():
            target = CLASH_DIR / name
            if target.is_file() and not force:
                continue
            ok, err = _fetch(url, target, _MIN_LICENSE_BYTES, name)
            if not ok:
                print(f"[内核] 许可原文 {name} 获取失败：{err}")
                return 1
    except Exception as e:  # noqa: BLE001 —— 兜底：任何意外都不留半截状态
        print(f"[内核] 获取失败：{e}")
        return 1

    still_missing = _missing()
    if still_missing:
        print(f"[内核] 仍缺：{', '.join(still_missing)}")
        return 1
    total = sum(p.stat().st_size for p in CLASH_DIR.iterdir() if p.is_file())
    print(f"[内核] 完成：{CLASH_DIR}（共 {total / 1048576:.1f} MB，随包分发）")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 随包内核资产获取")
    parser.add_argument("--force", action="store_true", help="已有资产也强制重下")
    parser.add_argument(
        "--from-dir", type=Path, default=None,
        help="从本机现成内核目录复制内核与 GeoIP 数据（离线场景；版本按原样复制）",
    )
    args = parser.parse_args()
    if args.from_dir is not None and not args.from_dir.is_dir():
        sys.exit(f"[内核] 目录不存在：{args.from_dir}")
    sys.exit(fetch(force=args.force, from_dir=args.from_dir))


if __name__ == "__main__":
    main()
