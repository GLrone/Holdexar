"""文件级差量包生成、校验与应用测试。"""
from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest
from app.core.differential_patch import (
    PATCH_MANIFEST_NAME,
    apply_differential_patch,
    compute_diff,
    create_differential_patch,
    hash_file,
    scan_directory,
    verify_patch_archive,
)


def test_scan_and_compute_diff(tmp_path: Path) -> None:
    base = tmp_path / "base"
    target = tmp_path / "target"
    base.mkdir()
    target.mkdir()

    # 未变文件
    (base / "common.txt").write_text("same content", encoding="utf-8")
    (target / "common.txt").write_text("same content", encoding="utf-8")

    # 修改文件
    (base / "mod.txt").write_text("old content", encoding="utf-8")
    (target / "mod.txt").write_text("new content", encoding="utf-8")

    # 删除文件
    (base / "del.txt").write_text("to be deleted", encoding="utf-8")

    # 新增文件
    (target / "new.txt").write_text("brand new", encoding="utf-8")

    base_files = scan_directory(base)
    target_files = scan_directory(target)

    diff = compute_diff(base_files, target_files)

    assert diff["unchanged"] == ["common.txt"]
    assert diff["modified"] == ["mod.txt"]
    assert diff["removed"] == ["del.txt"]
    assert diff["added"] == ["new.txt"]


def test_create_and_apply_differential_patch(tmp_path: Path) -> None:
    base = tmp_path / "v1"
    target = tmp_path / "v2"
    reconstructed = tmp_path / "reconstructed"
    patch_zip = tmp_path / "patch.zip"

    base.mkdir()
    target.mkdir()

    # 1. 构造多级目录文件
    (base / "sub").mkdir()
    (target / "sub").mkdir()
    (base / "Holdexar.exe").write_bytes(b"old-exe-bytes")
    (target / "Holdexar.exe").write_bytes(b"new-exe-bytes")
    (base / "sub" / "lib.dll").write_bytes(b"lib-v1-bytes")
    (target / "sub" / "lib.dll").write_bytes(b"lib-v1-bytes")  # 未变
    (base / "sub" / "deprecated.dll").write_bytes(b"deprecated")  # 待删除
    (target / "sub" / "feature.dll").write_bytes(b"new-feature")  # 新增

    # 2. 生成差量包
    zip_path, manifest = create_differential_patch(
        base_dir=base,
        target_dir=target,
        from_version="0.3.5-beta",
        to_version="0.3.6-beta",
        output_zip=patch_zip,
    )

    assert zip_path.is_file()
    assert manifest["summary"]["unchangedCount"] == 1
    assert manifest["summary"]["modifiedCount"] == 1
    assert manifest["summary"]["addedCount"] == 1
    assert manifest["summary"]["removedCount"] == 1

    # 3. 校验差量包自洽性
    verified = verify_patch_archive(patch_zip)
    assert verified["fromVersion"] == "0.3.5-beta"
    assert verified["toVersion"] == "0.3.6-beta"

    # 4. 在 base 上应用差量包生成 reconstructed
    result = apply_differential_patch(
        patch_source=patch_zip,
        base_dir=base,
        target_dest_dir=reconstructed,
    )
    assert result["ok"] is True
    assert result["toVersion"] == "0.3.6-beta"

    # 5. 核对目标目录与生成目录完全一致
    assert (reconstructed / "Holdexar.exe").read_bytes() == b"new-exe-bytes"
    assert (reconstructed / "sub" / "lib.dll").read_bytes() == b"lib-v1-bytes"
    assert (reconstructed / "sub" / "feature.dll").read_bytes() == b"new-feature"
    assert not (reconstructed / "sub" / "deprecated.dll").exists()

    # 6. 对比最终生成的全量文件与 target_dir 逐一哈希
    reconstructed_files = scan_directory(reconstructed)
    target_files = scan_directory(target)
    assert reconstructed_files == target_files


def test_apply_patch_detects_tampered_patch(tmp_path: Path) -> None:
    base = tmp_path / "v1"
    target = tmp_path / "v2"
    reconstructed = tmp_path / "reconstructed"
    patch_zip = tmp_path / "tampered_patch.zip"

    base.mkdir()
    target.mkdir()
    (base / "file.txt").write_text("old", encoding="utf-8")
    (target / "file.txt").write_text("new", encoding="utf-8")

    create_differential_patch(base, target, "1.0.0", "1.0.1", patch_zip)

    # 篡改 patch 内部文件
    tmp_extract = tmp_path / "extracted"
    with zipfile.ZipFile(patch_zip, "r") as zf:
        zf.extractall(tmp_extract)

    # 破坏变更文件内容
    (tmp_extract / "patch_files" / "file.txt").write_text("tampered", encoding="utf-8")

    # 重新压包
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        for f in tmp_extract.rglob("*"):
            if f.is_file():
                zf.write(f, f.relative_to(tmp_extract).as_posix())

    with pytest.raises(ValueError, match="差量包文件校验失败"):
        verify_patch_archive(bad_zip)
