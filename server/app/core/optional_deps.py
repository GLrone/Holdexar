"""可选依赖剔除与运行时兜底。

随包剔除的库（见 PYINSTALLER_EXCLUDES）一旦被真实运行路径导入，meta_path
尾位兜底按锁定版本从 PyPI 取对应 wheel 解压进数据目录并即时装载，导入方
无感知；补装失败则照常抛 ModuleNotFoundError，不影响其余功能。

名单是单一来源：holdexar.spec 的 excludes 直接引用 PYINSTALLER_EXCLUDES，
与本模块的兜底名单同源，改一处两端生效。
"""
from __future__ import annotations

import importlib.machinery
import io
import json
import logging
import platform
import sys
import threading
import urllib.request
import zipfile
from pathlib import Path

from app.core.logging import log_event
from app.core.paths import resolve_data_dir

logger = logging.getLogger(__name__)

# 顶层 import 名 → (PyPI 项目名, 锁定版本)。锁定版本 = 构建环境依赖的版本，
# 运行时补装同版本，避免补装引入行为差异。
OPTIONAL_DEPS: dict[str, tuple[str, str]] = {
    "PIL": ("pillow", "12.3.0"),
    "pygments": ("pygments", "2.21.0"),
    "openpyxl": ("openpyxl", "3.1.5"),
    "et_xmlfile": ("et-xmlfile", "2.0.0"),
    "asyncpg": ("asyncpg", "0.31.0"),
    "watchfiles": ("watchfiles", "1.2.0"),
    "setuptools": ("setuptools", "84.0.0"),
    "pkg_resources": ("setuptools", "84.0.0"),
    "packaging": ("packaging", "26.3"),
}

# 打包排除清单（Analysis.excludes）。et_xmlfile 只被 openpyxl 引用，
# openpyxl 剔除后不可达，无需显式排除；pkg_resources 是 setuptools 子模块，
# 排除 setuptools 即连带覆盖。
PYINSTALLER_EXCLUDES: list[str] = sorted(
    {top for top in OPTIONAL_DEPS} - {"et_xmlfile", "pkg_resources"}
)

_PYPI_JSON_URL = "https://pypi.org/pypi/{name}/{version}/json"
_DOWNLOAD_TIMEOUT = 60.0
_lock = threading.RLock()


def _accepted_platform_tags() -> set[str]:
    """当前发布形态可接受的 wheel 平台段；非 Windows 发布形态不兜底。"""
    if platform.system() != "Windows":
        return set()
    return {"any", "win_amd64" if sys.maxsize > 2**32 else "win32"}


def _wheel_matches(filename: str) -> bool:
    """wheel 文件名尾三段（python-abi-platform）是否适配当前解释器。"""
    stem = filename[:-4] if filename.endswith(".whl") else filename
    parts = stem.split("-")
    if len(parts) < 3:
        return False
    accepted_plats = _accepted_platform_tags()
    if not accepted_plats:
        return False
    cp = f"cp{sys.version_info.major}{sys.version_info.minor}"
    py_tags = set(parts[-3].split("."))
    abi_tags = set(parts[-2].split("."))
    plat_tags = set(parts[-1].split("."))
    return (
        bool(py_tags & {"py3", "py2.py3", cp})
        and bool(abi_tags & {"none", "abi3", cp})
        and bool(plat_tags & accepted_plats)
    )


def _download_wheel(pypi_name: str, version: str) -> bytes:
    """从 PyPI 取锁定版本里适配当前解释器的 wheel，返回字节流。"""
    url = _PYPI_JSON_URL.format(name=pypi_name, version=version)
    request = urllib.request.Request(
        url, headers={"User-Agent": "Holdexar optional-deps resolver"}
    )
    with urllib.request.urlopen(request, timeout=_DOWNLOAD_TIMEOUT) as resp:
        release = json.load(resp)
    wheels = [
        item
        for item in release.get("urls", [])
        if item.get("packagetype") == "bdist_wheel" and _wheel_matches(item["filename"])
    ]
    if not wheels:
        raise RuntimeError(f"PyPI 上无适配 wheel（{pypi_name} {version}）")
    # 有平台 wheel 时优先平台 wheel（二进制扩展必须匹配平台），纯 Python 取 any
    wheels.sort(key=lambda item: item["filename"].split("-")[-1] == "any")
    with urllib.request.urlopen(
        urllib.request.Request(
            wheels[0]["url"], headers={"User-Agent": "Holdexar optional-deps resolver"}
        ),
        timeout=_DOWNLOAD_TIMEOUT,
    ) as resp:
        return resp.read()


def _ensure_installed(pypi_name: str, version: str) -> Path | None:
    """补装目标包到数据目录并返回装载根目录；已装过直接复用。"""
    dest = resolve_data_dir() / "pylibs" / f"{pypi_name}-{version}"
    if dest.is_dir() and any(dest.iterdir()):
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    payload = _download_wheel(pypi_name, version)
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for member in archive.namelist():
            # zip-slip 一行闸：wheel 正常不含绝对路径与上跳段，出现即拒
            if member.startswith("/") or ".." in Path(member).parts:
                raise RuntimeError(f"wheel 内含非法路径成员：{member}")
        archive.extractall(dest)
    return dest


class _OptionalDepResolver:
    """meta_path 尾位兜底：常规导入全部落空后，对名单内的包按需补装。"""

    def __init__(self) -> None:
        self._resolving: set[str] = set()

    def find_spec(self, fullname, path=None, target=None):
        top = fullname.split(".")[0]
        entry = OPTIONAL_DEPS.get(top)
        if entry is None:
            return None
        pypi_name, version = entry
        with _lock:
            if top in self._resolving:
                return None
            self._resolving.add(top)
            try:
                location = _ensure_installed(pypi_name, version)
            except Exception as e:  # noqa: BLE001 —— 补装失败按「真缺」处理
                log_event(
                    logger,
                    f"依赖 {pypi_name} 自动补装失败，该功能不可用但不影响其余功能",
                    tag="失败",
                    detail={"原因": str(e), "版本": version},
                    level=logging.WARNING,
                )
                return None
            finally:
                self._resolving.discard(top)
        log_event(
            logger,
            f"依赖 {pypi_name} 已按需补装",
            tag="成功",
            detail={"版本": version, "位置": str(location)},
        )
        return importlib.machinery.PathFinder().find_spec(fullname, [str(location)])


def install_optional_dep_resolver() -> None:
    """把兜底 finder 挂到 meta_path 末尾（幂等）；常规导入路径不受影响。"""
    for finder in sys.meta_path:
        if isinstance(finder, _OptionalDepResolver):
            return
    sys.meta_path.append(_OptionalDepResolver())
