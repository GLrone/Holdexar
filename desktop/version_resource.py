"""Windows PE 版本资源生成与校验。

三个 EXE（Launcher / UpdateHost / Desktop）统一复用本模块，
以 app.core.app_info.APP_VERSION 为唯一事实源。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from app.core.app_info import APP_NAME, APP_VERSION  # noqa: E402

COMPANY_NAME = f"{APP_NAME} Contributors"
LEGAL_COPYRIGHT = f"Copyright (c) {APP_NAME} contributors. Licensed under GPL-3.0."

ROLE_META: dict[str, dict[str, str]] = {
    "desktop": {
        "description": f"{APP_NAME} Desktop",
        "internal_name": APP_NAME,
        "original_filename": f"{APP_NAME}.exe",
    },
    "launcher": {
        "description": f"{APP_NAME} Launcher",
        "internal_name": f"{APP_NAME}Launcher",
        "original_filename": f"{APP_NAME}.exe",
    },
    "updater": {
        "description": f"{APP_NAME} Update Host",
        "internal_name": "UpdateHost",
        "original_filename": "UpdateHost.exe",
    },
}


def numeric_version(text: str) -> tuple[int, int, int, int]:
    """语义化版本转四段数字（0.3.5-beta -> (0, 3, 5, 0)）。"""
    parts = [int(n) for n in re.findall(r"\d+", text)[:4]]
    return tuple((parts + [0, 0, 0, 0])[:4])  # type: ignore[return-value]


def numeric_version_str(text: str) -> str:
    """四段数字版本字符串（0.3.5-beta -> 0.3.5.0）。"""
    return ".".join(str(n) for n in numeric_version(text))


def generate_version_info_text(role: str, version: str | None = None) -> str:
    """生成供 PyInstaller --version-file 使用的 VSVersionInfo 文本。"""
    ver = version or APP_VERSION
    meta = ROLE_META.get(role, ROLE_META["desktop"])
    num_ver = numeric_version(ver)
    file_ver_str = numeric_version_str(ver)

    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={num_ver!r},
    prodvers={num_ver!r},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [
          StringStruct('CompanyName', '{COMPANY_NAME}'),
          StringStruct('FileDescription', '{meta["description"]}'),
          StringStruct('FileVersion', '{file_ver_str}'),
          StringStruct('InternalName', '{meta["internal_name"]}'),
          StringStruct('LegalCopyright', '{LEGAL_COPYRIGHT}'),
          StringStruct('OriginalFilename', '{meta["original_filename"]}'),
          StringStruct('ProductName', '{APP_NAME}'),
          StringStruct('ProductVersion', '{ver}'),
        ],
      ),
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])]),
  ],
)
"""


def write_version_file(dest_path: Path, role: str, version: str | None = None) -> Path:
    """将版本资源写入指定路径供构建命令使用。"""
    content = generate_version_info_text(role, version)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_text(content, encoding="utf-8")
    return dest_path


def get_vs_version_info(role: str, version: str | None = None) -> Any:
    """返回供 .spec 文件直接使用的 VSVersionInfo 对象。"""
    try:
        from PyInstaller.utils.win32.versioninfo import (
            FixedFileInfo,
            StringFileInfo,
            StringStruct,
            StringTable,
            VarFileInfo,
            VarStruct,
            VSVersionInfo,
        )
    except ImportError:
        return None

    ver = version or APP_VERSION
    meta = ROLE_META.get(role, ROLE_META["desktop"])
    num_ver = numeric_version(ver)
    file_ver_str = numeric_version_str(ver)

    return VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=num_ver,
            prodvers=num_ver,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable("040904B0", [
                    StringStruct("CompanyName", COMPANY_NAME),
                    StringStruct("FileDescription", meta["description"]),
                    StringStruct("FileVersion", file_ver_str),
                    StringStruct("InternalName", meta["internal_name"]),
                    StringStruct("LegalCopyright", LEGAL_COPYRIGHT),
                    StringStruct("OriginalFilename", meta["original_filename"]),
                    StringStruct("ProductName", APP_NAME),
                    StringStruct("ProductVersion", ver),
                ]),
            ]),
            VarFileInfo([VarStruct("Translation", [1033, 1200])]),
        ],
    )


def read_pe_properties(exe_path: Path) -> dict[str, str]:
    """读取 Windows PE 文件的全部版本资源字段。"""
    props: dict[str, str] = {}
    if os.name != "nt" or not exe_path.is_file():
        return props

    try:
        import ctypes
        from ctypes import wintypes

        version_dll = getattr(ctypes.windll, "version", None)
        if version_dll is None:
            return props
        size = version_dll.GetFileVersionInfoSizeW(str(exe_path), None)
        if not size:
            return props
        buf = ctypes.create_string_buffer(size)
        if not version_dll.GetFileVersionInfoW(str(exe_path), 0, size, buf):
            return props

        val_ptr = ctypes.c_void_p()
        val_len = wintypes.UINT()

        # 尝试常用代码页前缀
        code_pages = ["040904b0", "080404b0", "000004b0", "040904e4", "080404e4"]
        keys = [
            "CompanyName",
            "FileDescription",
            "FileVersion",
            "InternalName",
            "LegalCopyright",
            "OriginalFilename",
            "ProductName",
            "ProductVersion",
        ]

        for k in keys:
            for cp in code_pages:
                sub = f"\\StringFileInfo\\{cp}\\{k}"
                if version_dll.VerQueryValueW(buf, sub, ctypes.byref(val_ptr), ctypes.byref(val_len)):
                    if val_len.value > 0 and val_ptr.value:
                        val = ctypes.wstring_at(val_ptr.value).strip()
                        if val:
                            props[k] = val
                            break
    except Exception:
        pass

    return props


def verify_exe_version_resources(install_dir: Path, version: str) -> dict[str, dict[str, str]]:
    """检查安装或发行包中三个 EXE 的版本属性。

    检查目标：
    1. 根目录 Launcher (Holdexar.exe)
    2. updater/UpdateHost.exe
    3. versions/<version>/Holdexar.exe
    """
    expected_file_ver = numeric_version_str(version)
    targets = {
        "launcher": install_dir / f"{APP_NAME}.exe",
        "updater": install_dir / "updater" / "UpdateHost.exe",
        "desktop": install_dir / "versions" / version / f"{APP_NAME}.exe",
    }

    results: dict[str, dict[str, str]] = {}
    for role, exe_path in targets.items():
        if not exe_path.is_file():
            raise FileNotFoundError(f"[{role}] 可执行文件缺失：{exe_path}")
        props = read_pe_properties(exe_path)
        if not props:
            raise ValueError(f"[{role}] 无法读取 Windows PE 版本资源（属性页空白）：{exe_path}")

        meta = ROLE_META[role]
        errors: list[str] = []

        if props.get("ProductName") != APP_NAME:
            errors.append(f"ProductName 不符：期望 '{APP_NAME}'，实际 '{props.get('ProductName')}'")
        if props.get("ProductVersion") != version:
            errors.append(f"ProductVersion 不符：期望 '{version}'，实际 '{props.get('ProductVersion')}'")
        if props.get("FileVersion") != expected_file_ver:
            errors.append(f"FileVersion 不符：期望 '{expected_file_ver}'，实际 '{props.get('FileVersion')}'")
        if props.get("FileDescription") != meta["description"]:
            errors.append(f"FileDescription 不符：期望 '{meta['description']}'，实际 '{props.get('FileDescription')}'")
        if props.get("CompanyName") != COMPANY_NAME:
            errors.append(f"CompanyName 不符：期望 '{COMPANY_NAME}'，实际 '{props.get('CompanyName')}'")

        if errors:
            raise ValueError(f"[{role}] 版本资源校验未通过：\n  " + "\n  ".join(errors))

        results[role] = props

    return results
