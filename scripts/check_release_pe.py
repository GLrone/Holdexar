"""发行包 PE 体检：被 Windows 加载的二进制的签名 / 版本资源 / 加壳迹象。

报毒与 SmartScreen 的判断依据很大一部分来自文件本身的特征，本脚本把它们
摊开看：有没有 Authenticode 签名、有没有版本资源、节区熵是否像被压缩过、
EXE 是否带动态加载类 API。

用法：
    python scripts/check_release_pe.py                    # 默认 release/build/Holdexar
    python scripts/check_release_pe.py --dir <目录>
    python scripts/check_release_pe.py --all-sections     # 连 DLL/PYD 也逐条列出
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pefile


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIR = ROOT / "release" / "build" / "Holdexar"

PE_EXTS = (".exe", ".dll", ".pyd", ".sys", ".cpl", ".ocx", ".drv", ".scr")
# 加壳器 / 压缩壳的节名：出现即说明二进制被打包过，是误报的主要来源之一
PACKER_SECTIONS = ("upx", "aspack", "nsp", "petite", "themida", "vmp", "y0da", "npack")
# 这些 API 单独出现都合法，成组出现才是「运行时解压/写入再执行」的提示
RUNTIME_APIS = (
    "virtualalloc", "virtualprotect", "writeprocessmemory", "createremotethread",
    "loadlibrarya", "loadlibraryw", "urldownloadtofilea", "urldownloadtofilew",
    "winexec", "shellexecutea", "shellexecutew",
)
HIGH_ENTROPY = 7.5
SECURITY_DIR = 4  # IMAGE_DIRECTORY_ENTRY_SECURITY


def _has_version_resource(pe: pefile.PE) -> bool:
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
        )
    except Exception:
        return False
    res = getattr(pe, "DIRECTORY_ENTRY_RESOURCE", None)
    for entry in getattr(res, "entries", []):
        if entry.id == pefile.RESOURCE_TYPE["RT_VERSION"]:
            return True
    return False


def _imported_apis(pe: pefile.PE) -> set[str]:
    try:
        pe.parse_data_directories(
            directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"]]
        )
    except Exception:
        return set()
    names: set[str] = set()
    for entry in getattr(pe, "DIRECTORY_ENTRY_IMPORT", []):
        for imp in entry.imports:
            if imp.name:
                names.add(imp.name.decode(errors="replace").lower())
    return names


def inspect(path: Path, root: Path) -> dict:
    stat = {"rel": path.relative_to(root).as_posix(), "size": path.stat().st_size, "ok": False}
    try:
        pe = pefile.PE(str(path), fast_load=True)
    except Exception as exc:  # noqa: BLE001 — 坏文件要计入报告而不是中断体检
        stat["error"] = str(exc)
        return stat
    with pe:
        dd = pe.OPTIONAL_HEADER.DATA_DIRECTORY[SECURITY_DIR]
        stat.update(
            ok=True,
            bits=64 if pe.OPTIONAL_HEADER.Magic == 0x20B else 32,
            subsystem=pe.OPTIONAL_HEADER.Subsystem,
            signed=dd.VirtualAddress > 0 and dd.Size > 0,
            version=_has_version_resource(pe),
            entropy=max((s.get_entropy() for s in pe.sections), default=0.0),
            sections=[s.Name.rstrip(b"\0").decode(errors="replace") for s in pe.sections],
        )
        if path.suffix.lower() == ".exe":
            stat["apis"] = sorted(_imported_apis(pe) & set(RUNTIME_APIS))
    return stat


def packer_hit(stat: dict) -> str:
    return ",".join(
        s for s in stat.get("sections", []) if any(p in s.lower() for p in PACKER_SECTIONS)
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Holdexar 发行包 PE 体检")
    parser.add_argument("--dir", default=str(DEFAULT_DIR), help="待检目录（onedir 产物根）")
    parser.add_argument("--all-sections", action="store_true", help="逐条列出 DLL/PYD")
    args = parser.parse_args()

    root = Path(args.dir).resolve()
    if not root.is_dir():
        sys.exit(f"[错误] 目录不存在：{root}")

    files = [p for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in PE_EXTS]
    stats = [inspect(p, root) for p in files]
    exes = [s for s in stats if s["rel"].lower().endswith(".exe")]
    libs = [s for s in stats if not s["rel"].lower().endswith(".exe")]
    broken = [s for s in stats if not s["ok"]]

    def mb(size: int) -> str:
        return f"{size / 1048576:.1f}MB"

    print(f"[PE 体检] {root}")
    print(f"  扫描 {len(stats)} 个 PE（EXE {len(exes)} / DLL·PYD {len(libs)}）\n")
    print("EXE（会被 Windows 直接执行的那些）")
    for s in exes:
        if not s["ok"]:
            print(f"  {s['rel']}  解析失败：{s.get('error')}")
            continue
        print(
            f"  {s['rel']}\n"
            f"      大小 {mb(s['size'])}  {s['bits']}位  子系统 {s['subsystem']}  "
            f"签名 {'有' if s['signed'] else '无'}  版本资源 {'有' if s['version'] else '无'}  "
            f"最大节熵 {s['entropy']:.2f}\n"
            f"      壳节名 {packer_hit(s) or '无'}  动态加载API {', '.join(s.get('apis', [])) or '无'}"
        )

    signed_libs = sum(1 for s in libs if s.get("signed"))
    ver_libs = sum(1 for s in libs if s.get("version"))
    hot_libs = [s for s in libs if s.get("entropy", 0) > HIGH_ENTROPY]
    print(f"\nDLL / PYD（{len(libs)} 个）")
    print(f"  有签名 {signed_libs}  有版本资源 {ver_libs}  高熵(>{HIGH_ENTROPY}) {len(hot_libs)}")
    if args.all_sections:
        for s in libs:
            flag = "" if s.get("ok") else "（解析失败）"
            print(
                f"  {s['rel']}  {mb(s['size'])}  签名 {'有' if s.get('signed') else '无'}  "
                f"版本资源 {'有' if s.get('version') else '无'}  熵 {s.get('entropy', 0):.2f}{flag}"
            )
    for s in sorted(hot_libs, key=lambda x: -x.get("entropy", 0))[:5]:
        print(f"  高熵 {s['rel']}  {mb(s['size'])}  熵 {s.get('entropy', 0):.2f}")
    for s in libs:
        if packer_hit(s):
            print(f"  壳节名 {s['rel']} → {packer_hit(s)}")

    print("\n结论")
    unsigned_exe = [s["rel"] for s in exes if s.get("ok") and not s["signed"]]
    nover_exe = [s["rel"] for s in exes if s.get("ok") and not s["version"]]
    print(f"  · EXE 未签名：{', '.join(unsigned_exe) or '无'}"
          f"（未签名程序会触发 SmartScreen，且被 Smart App Control 硬阻断）")
    print(f"  · EXE 缺版本资源：{', '.join(nover_exe) or '无'}"
          f"（属性页「详细信息」空白，会加重杀软疑点）")
    print(f"  · 解析失败的 PE：{', '.join(s['rel'] for s in broken) or '无'}")
    if broken:
        sys.exit(1)


if __name__ == "__main__":
    main()
