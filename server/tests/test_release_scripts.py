"""发布链脚本校验：版本号解析、空提交拦截、发布说明四段式、产物一致性及 dry-run 安全。"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import build_manifest  # noqa: E402
import publish_release  # noqa: E402
import release  # noqa: E402


# ── 1. 版本号解析与 bump 校验 ──────────────────────────────────────


@pytest.mark.parametrize(
    ("ver", "expected_match"),
    [
        ("0.3.1", True),
        ("0.3.1-beta", True),
        ("0.3.1-beta.2", True),
        ("1.0.0-rc.1", True),
        ("0.3.1.beta", False),
        ("v0.3.1", False),
        ("0.3", False),
        ("invalid", False),
    ],
)
def test_semver_re_matches_pre_release(ver: str, expected_match: bool) -> None:
    match = release.SEMVER_RE.match(ver)
    assert (match is not None) is expected_match


def test_bump_rejects_pre_release(monkeypatch) -> None:
    """对预发布版本（如 0.3.1-beta）调用 bump 时明确报错，不静默产生错误版本。"""
    monkeypatch.setattr(release, "APP_VERSION", "0.3.1-beta")
    with pytest.raises(SystemExit) as exc_info:
        release.bump("patch")
    assert "预发布版本" in str(exc_info.value)
    assert "--version" in str(exc_info.value)


def test_bump_regular_versions(monkeypatch) -> None:
    monkeypatch.setattr(release, "APP_VERSION", "0.3.1")
    assert release.bump("patch") == "0.3.2"
    assert release.bump("minor") == "0.4.0"
    assert release.bump("major") == "1.0.0"


# ── 2. 无变更提交保护 ──────────────────────────────────────────


def test_commit_and_push_skips_when_app_info_clean(monkeypatch) -> None:
    """版本文件未发生变化时不执行 git commit，避免制造空提交报错。"""
    commands_run = []

    def fake_capture(cmd: list[str]):
        if cmd[:3] == ["git", "status", "--porcelain"]:
            # app_info.py 无变更
            return 0, ""
        if cmd[:3] == ["git", "rev-parse", "HEAD"]:
            return 0, "hash123\n"
        if cmd[:3] == ["git", "rev-parse", "origin/main"]:
            return 0, "hash123\n"
        return 0, ""

    def fake_run(cmd: list[str], dry: bool = False, label: str = ""):
        commands_run.append(cmd)

    monkeypatch.setattr(release, "capture", fake_capture)
    monkeypatch.setattr(release, "run", fake_run)

    release.commit_and_push("0.3.1-beta", dry=False, yes=True)

    commit_commands = [c for c in commands_run if "commit" in c]
    assert len(commit_commands) == 0


# ── 3. 发布说明四段式校验 ──────────────────────────────────────


def test_validate_release_notes_success() -> None:
    valid_notes = (
        "新增：\n"
        "- 新增特性 A\n"
        "\n"
        "更新：\n"
        "- 更新逻辑 B\n"
        "\n"
        "优化：\n"
        "- 优化性能 C\n"
        "\n"
        "修复：\n"
        "- 修复缺陷 D\n"
    )
    assert release.validate_release_notes(valid_notes) is None


def test_validate_release_notes_standard_031_format_success() -> None:
    """0.3.1 标准版式（含 ## 本次更新 与 ### 分级标题）顺利通过校验。"""
    standard_notes = (
        "# Holdexar v0.3.1 Beta\n\n"
        "> 建议直接使用应用内「检查更新」升级\n\n"
        "Windows x64 便携版，解压双击即用。\n\n"
        "## 本次更新\n\n"
        "### 新增功能 (New Features)\n"
        "- **奖杯大厅**：成就页集中上线猎手等级与段位\n\n"
        "### 问题修复 (Bug Fixes)\n"
        "- **网络与代理**：修复本机网络被其他软件接管时的误判\n\n"
        "### 体验优化 (Improvements)\n"
        "- **领航员问答**：纯数据咨询直接给出答案\n\n"
        "## 安装与更新\n\n"
        "**方式一 · 应用内更新（推荐）**\n"
        "- 打开应用点击更新\n"
    )
    assert release.validate_release_notes(standard_notes) is None


def test_validate_release_notes_allows_empty_section_if_has_content() -> None:
    """允许部分段落为空，但正文整体至少有一条有效更新。"""
    valid_notes = (
        "新增：\n"
        "- 新增特性 A\n"
        "\n"
        "更新：\n"
        "\n"
        "优化：\n"
        "\n"
        "修复：\n"
    )
    assert release.validate_release_notes(valid_notes) is None


def test_validate_release_notes_rejects_missing_headers() -> None:
    """缺少任意一段标题即被拒绝。"""
    missing_fix = (
        "新增：\n- 条目\n"
        "更新：\n- 条目\n"
        "优化：\n- 条目\n"
    )
    err = release.validate_release_notes(missing_fix)
    assert err is not None
    assert "缺少四段式标题" in err
    assert "修复：" in err


def test_validate_release_notes_rejects_wrong_order() -> None:
    """标题顺序错误即被拒绝。"""
    inverted = (
        "新增：\n- 条目\n"
        "优化：\n- 条目\n"
        "更新：\n- 条目\n"
        "修复：\n- 条目\n"
    )
    err = release.validate_release_notes(inverted)
    assert err is not None
    assert "顺序错误" in err


def test_validate_release_notes_rejects_placeholder() -> None:
    """包含模板占位符即被拒绝。"""
    with_placeholder = (
        "新增：\n- 在此写本次更新内容\n"
        "更新：\n"
        "优化：\n"
        "修复：\n"
    )
    err = release.validate_release_notes(with_placeholder)
    assert err is not None
    assert "模板占位文案" in err


def test_validate_release_notes_rejects_empty_body() -> None:
    """四段均无有效内容即被拒绝。"""
    empty_sections = (
        "新增：\n\n"
        "更新：\n\n"
        "优化：\n\n"
        "修复：\n\n"
    )
    err = release.validate_release_notes(empty_sections)
    assert err is not None
    assert "各段均无有效更新条目" in err


# ── 4. 产物与清单一致性 ────────────────────────────────────────


def test_pick_zip_rejects_mismatched_version(tmp_path: Path, monkeypatch) -> None:
    """当目标版本 ZIP 不存在时，拒绝静默使用旧版本 ZIP。"""
    monkeypatch.setattr(build_manifest, "RELEASE", tmp_path)
    old_zip = tmp_path / f"{build_manifest.APP_NAME}-win64-v0.3.0-beta.zip"
    old_zip.write_bytes(b"old-zip")

    with pytest.raises(SystemExit) as exc_info:
        build_manifest.pick_zip(None, "0.3.1-beta")
    assert "未找到当前版本 v0.3.1-beta 的应用包" in str(exc_info.value)
    assert "不允许静默选择其他版本的旧 ZIP" in str(exc_info.value)


def test_pick_zip_accepts_matching_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(build_manifest, "RELEASE", tmp_path)
    matching_zip = tmp_path / f"{build_manifest.APP_NAME}-win64-v0.3.1-beta.zip"
    matching_zip.write_bytes(b"matching-zip")

    picked = build_manifest.pick_zip(None, "0.3.1-beta")
    assert picked == matching_zip


def test_build_manifest_assets_coverage(tmp_path: Path, monkeypatch) -> None:
    """清单同时声明 app、seed 以及实际分发的 seed-archive，且哈希与体积一致。"""
    app_zip = tmp_path / f"{build_manifest.APP_NAME}-win64-v0.3.1-beta.zip"
    app_zip.write_bytes(b"app-data" * 10)

    seed_db = tmp_path / "holdexar_seed.db"
    seed_db.write_bytes(b"seed-data" * 20)

    seed_gz = tmp_path / "holdexar_seed.db.gz"
    seed_gz.write_bytes(b"seed-gz-data" * 5)

    notes_file = tmp_path / "RELEASE_NOTES.md"
    notes_file.write_text("新增：\n- 功能\n更新：\n优化：\n修复：\n", encoding="utf-8")

    monkeypatch.setattr(build_manifest, "SEED_DB", seed_db)
    monkeypatch.setattr(build_manifest, "SEED_GZ", seed_gz)
    monkeypatch.setattr(build_manifest, "NOTES_FILE", notes_file)

    manifest = build_manifest.build_manifest("0.3.1-beta", app_zip)
    roles = {asset["role"]: asset for asset in manifest["assets"]}

    assert "app" in roles
    assert "seed" in roles
    assert "seed-archive" in roles

    archive = roles["seed-archive"]
    assert archive["name"] == "holdexar_seed.db.gz"
    assert archive["size"] == len(b"seed-gz-data" * 5)
    assert archive["sha256"] == build_manifest.sha256_of(seed_gz)
    assert manifest["notes"] == notes_file.read_text(encoding="utf-8").strip()


# ── 5. 发布脚本干跑与覆盖更新安全性 ────────────────────────────


def test_publish_release_dry_run_no_side_effects(tmp_path: Path, monkeypatch) -> None:
    """--dry-run 收集产物时不触发 gzip 压缩等写盘副作用。"""
    seed_db = tmp_path / "holdexar_seed.db"
    seed_db.write_bytes(b"uncompressed-seed-data")
    seed_gz = tmp_path / "holdexar_seed.db.gz"

    monkeypatch.setattr(publish_release, "SEED_RAW_FALLBACK", seed_db)
    monkeypatch.setattr(publish_release, "SEED_GZ", seed_gz)

    # 在 dry=True 模式下 _ensure_seed_gz 不应在磁盘上生成 gz 文件
    gz_target = publish_release._ensure_seed_gz(dry=True)
    assert gz_target is None
    assert not seed_gz.exists()


def test_publish_release_updates_existing_release_notes(monkeypatch) -> None:
    """版本 Release 已存在时，重新发布必须调用 gh release edit 更新发布说明。"""
    gh_calls = []

    def fake_run(cmd: list[str], dry: bool = False):
        gh_calls.append(cmd)

    monkeypatch.setattr(publish_release, "release_exists", lambda tag: True)
    monkeypatch.setattr(publish_release, "release_is_draft", lambda tag: False)
    monkeypatch.setattr(publish_release, "run", fake_run)

    notes_p = ROOT / "release" / "RELEASE_NOTES.md"
    publish_release.publish_version_release("0.3.1-beta", [], notes_p, prerelease=True, dry=False)

    edit_calls = [c for c in gh_calls if "edit" in c]
    assert len(edit_calls) == 1
    edit_cmd = edit_calls[0]
    assert edit_cmd[0] == "gh"
    assert edit_cmd[1] == "release"
    assert edit_cmd[2] == "edit"
    assert edit_cmd[3] == "v0.3.1-beta"
    assert "--notes-file" in edit_cmd

