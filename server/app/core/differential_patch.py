"""文件级差量更新包生成与应用。

基于 SHA-256 对比两个版本目录（或 onedir 产物），仅打包新增与变动文件，
结合 patch-manifest.json 记录文件变更与全量目标哈希，实现小体积差量升级与完整性验证。
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
import zipfile
from pathlib import Path
from typing import Any

PATCH_FORMAT = "holdexar-diff-v1"
PATCH_MANIFEST_NAME = "patch-manifest.json"
PATCH_FILES_PREFIX = "patch_files"


def hash_file(path: Path) -> str:
    """计算文件的 SHA-256 哈希值。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_directory(dir_path: Path) -> dict[str, tuple[int, str]]:
    """扫描目录下的所有普通文件，返回 { posix相对路径: (文件体积, sha256) }。"""
    result: dict[str, tuple[int, str]] = {}
    if not dir_path.is_dir():
        return result

    for p in sorted(dir_path.rglob("*")):
        if p.is_file():
            rel = p.relative_to(dir_path).as_posix()
            result[rel] = (p.stat().st_size, hash_file(p))
    return result


def compute_diff(
    base_files: dict[str, tuple[int, str]],
    target_files: dict[str, tuple[int, str]],
) -> dict[str, Any]:
    """对比两个文件清单，归纳新增、修改、删除与未变列表。"""
    added: list[str] = []
    modified: list[str] = []
    unchanged: list[str] = []
    removed: list[str] = []

    for rel, (size, sha) in target_files.items():
        if rel not in base_files:
            added.append(rel)
        elif base_files[rel][1] != sha:
            modified.append(rel)
        else:
            unchanged.append(rel)

    for rel in base_files:
        if rel not in target_files:
            removed.append(rel)

    return {
        "added": sorted(added),
        "modified": sorted(modified),
        "unchanged": sorted(unchanged),
        "removed": sorted(removed),
    }


def create_differential_patch(
    base_dir: Path,
    target_dir: Path,
    from_version: str,
    to_version: str,
    output_zip: Path,
) -> tuple[Path, dict[str, Any]]:
    """对比 base_dir 与 target_dir，生成文件级差量包。"""
    base_files = scan_directory(base_dir)
    target_files = scan_directory(target_dir)

    diff = compute_diff(base_files, target_files)
    changed_rel_paths = set(diff["added"] + diff["modified"])

    output_zip.parent.mkdir(parents=True, exist_ok=True)
    if output_zip.exists():
        output_zip.unlink()

    patch_file_entries: list[dict[str, Any]] = []
    for rel in sorted(changed_rel_paths):
        size, sha = target_files[rel]
        action = "add" if rel in diff["added"] else "modify"
        patch_file_entries.append({
            "path": rel,
            "action": action,
            "size": size,
            "sha256": sha,
        })

    target_verification: dict[str, str] = {
        rel: sha for rel, (_size, sha) in target_files.items()
    }

    manifest_data = {
        "format": PATCH_FORMAT,
        "fromVersion": from_version,
        "toVersion": to_version,
        "createdAt": int(time.time()),
        "summary": {
            "totalTargetFiles": len(target_files),
            "totalTargetBytes": sum(sz for sz, _ in target_files.values()),
            "addedCount": len(diff["added"]),
            "modifiedCount": len(diff["modified"]),
            "removedCount": len(diff["removed"]),
            "unchangedCount": len(diff["unchanged"]),
        },
        "files": patch_file_entries,
        "removedFiles": diff["removed"],
        "targetVerification": target_verification,
    }

    with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        # 写入差量清单
        zf.writestr(
            PATCH_MANIFEST_NAME,
            json.dumps(manifest_data, ensure_ascii=False, indent=2),
        )
        # 打包变更和新增文件
        for rel in sorted(changed_rel_paths):
            src_file = target_dir / rel
            arc_name = f"{PATCH_FILES_PREFIX}/{rel}"
            zf.write(src_file, arc_name)

    patch_size = output_zip.stat().st_size
    manifest_data["summary"]["patchFileBytes"] = patch_size
    manifest_data["patchSha256"] = hash_file(output_zip)

    return output_zip, manifest_data


def verify_patch_archive(patch_zip: Path) -> dict[str, Any]:
    """校验差量包压缩包的内部完整性（清单与变更文件哈希）。"""
    if not patch_zip.is_file():
        raise FileNotFoundError(f"差量包不存在：{patch_zip}")

    with zipfile.ZipFile(patch_zip, "r") as zf:
        namelist = set(zf.namelist())
        if PATCH_MANIFEST_NAME not in namelist:
            raise ValueError(f"差量包损坏：缺少 {PATCH_MANIFEST_NAME}")

        manifest_raw = zf.read(PATCH_MANIFEST_NAME).decode("utf-8")
        manifest: dict[str, Any] = json.loads(manifest_raw)

        if manifest.get("format") != PATCH_FORMAT:
            raise ValueError(f"差量包格式不匹配：{manifest.get('format')}")

        for item in manifest.get("files", []):
            rel = item["path"]
            expected_sha = item["sha256"]
            arc_name = f"{PATCH_FILES_PREFIX}/{rel}"
            if arc_name not in namelist:
                raise ValueError(f"差量包文件缺失：{arc_name}")

            content = zf.read(arc_name)
            actual_sha = hashlib.sha256(content).hexdigest()
            if actual_sha != expected_sha:
                raise ValueError(
                    f"差量包文件校验失败：{rel}，期望 {expected_sha}，实际 {actual_sha}"
                )

    return manifest


def apply_differential_patch(
    patch_source: Path,
    base_dir: Path,
    target_dest_dir: Path,
) -> dict[str, Any]:
    """在 base_dir 的基础上应用差量包，组装为目标完整目录 target_dest_dir。

    支持 patch_source 为差量 .zip 文件或已解压的目录。
    成功后逐文件执行 targetVerification SHA-256 全量核验。
    """
    if not base_dir.is_dir():
        raise FileNotFoundError(f"差量基准目录不存在：{base_dir}")

    # 1. 读取清单并定位变更文件
    if patch_source.is_file() and patch_source.suffix.lower() == ".zip":
        manifest = verify_patch_archive(patch_source)
        is_zip = True
    elif patch_source.is_dir():
        mf = patch_source / PATCH_MANIFEST_NAME
        if not mf.is_file():
            raise FileNotFoundError(f"差量暂存目录缺少清单：{mf}")
        manifest = json.loads(mf.read_text(encoding="utf-8"))
        is_zip = False
    else:
        raise ValueError(f"无效差量来源：{patch_source}")

    if target_dest_dir.exists():
        shutil.rmtree(target_dest_dir, ignore_errors=True)
    target_dest_dir.parent.mkdir(parents=True, exist_ok=True)

    # 2. 将 base_dir 完整复制为新目标基底
    shutil.copytree(base_dir, target_dest_dir, dirs_exist_ok=True)

    # 3. 删除 removedFiles 中的文件
    for removed_rel in manifest.get("removedFiles", []):
        target_path = target_dest_dir / removed_rel
        if target_path.is_file():
            target_path.unlink()
        elif target_path.is_dir():
            shutil.rmtree(target_path, ignore_errors=True)

    # 4. 覆盖与新增变更文件
    if is_zip:
        with zipfile.ZipFile(patch_source, "r") as zf:
            prefix = f"{PATCH_FILES_PREFIX}/"
            for info in zf.infolist():
                if info.filename.startswith(prefix) and not info.is_dir():
                    rel = info.filename[len(prefix):]
                    dest = target_dest_dir / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(zf.read(info))
    else:
        patch_files_root = patch_source / PATCH_FILES_PREFIX
        if not patch_files_root.is_dir():
            # 兼容扁平放置
            patch_files_root = patch_source
        for item in manifest.get("files", []):
            rel = item["path"]
            src = patch_files_root / rel
            if not src.is_file():
                src = patch_source / PATCH_FILES_PREFIX / rel
            if not src.is_file():
                raise FileNotFoundError(f"差量变更文件缺失：{src}")
            dest = target_dest_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)

    # 5. 全量目标文件 SHA-256 最终核验（防静默破坏或残缺）
    target_verif = manifest.get("targetVerification", {})
    failed_verifications: list[str] = []
    for rel, expected_sha in target_verif.items():
        actual_file = target_dest_dir / rel
        if not actual_file.is_file():
            failed_verifications.append(f"文件缺失：{rel}")
            continue
        actual_sha = hash_file(actual_file)
        if actual_sha != expected_sha:
            failed_verifications.append(
                f"哈希不符：{rel}（期望 {expected_sha[:12]}…，实际 {actual_sha[:12]}…）"
            )

    if failed_verifications:
        shutil.rmtree(target_dest_dir, ignore_errors=True)
        raise ValueError(
            "差量应用后全量完整性核验失败：\n  " + "\n  ".join(failed_verifications[:10])
        )

    return {
        "ok": True,
        "fromVersion": manifest.get("fromVersion"),
        "toVersion": manifest.get("toVersion"),
        "filesVerified": len(target_verif),
    }
