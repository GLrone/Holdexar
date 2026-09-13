"""数据目录解析测试：环境变量 / 便携 / 存量 / 开发态 / 打包态 五条判定分支。

覆盖 app/core/paths.py 的判定链——它是「升级不丢数据」的唯一入口，
判定顺序错一步就是老用户库被当成新装（或反之）。

开发态分支是本次改造的重点：过去开发态直接写工作树内的 `data/`，爬虫的库、日志、
备份全落在 git 仓库里，一次 `git add -A` 就会把它们收进提交（本项目的一次真实
泄漏事故正是这么发生的）。因此这里有一条硬断言：**开发态数据目录不得位于仓库工作树内**。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import paths


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """隔离环境：程序目录 / 仓库根 / 机器级目录 / 环境变量全部指到 tmp。"""
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setattr(paths.sys, "executable", str(app_dir / "Holdexar.exe"), raising=False)
    # 未打包时 app_dir() 返回 PROJECT_ROOT，故仓库根也要指到 tmp，
    # 否则开发态用例会去解析（并创建）真实仓库里的 data/。
    monkeypatch.setattr(paths, "PROJECT_ROOT", app_dir)
    monkeypatch.delenv(f"{paths.ENV_PREFIX}DATA_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(tmp_path / "roaming"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))
    return {"app_dir": app_dir, "local": local, "tmp": tmp_path}


def _freeze(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)


def _unfreeze(monkeypatch) -> None:
    monkeypatch.setattr(sys, "frozen", False, raising=False)


def _seed_repo_db(app_dir: Path) -> Path:
    """在「程序目录」内造成存量布局：data/<slug>.db 存在。"""
    legacy = app_dir / "data"
    legacy.mkdir(parents=True, exist_ok=True)
    (legacy / paths.data_dir_filename()).write_bytes(b"")
    return legacy


# ── 判定链：前三条 ──────────────────────────────────────────


def test_env_override_wins(fake_env, monkeypatch, tmp_path):
    _freeze(monkeypatch)
    custom = tmp_path / "custom"
    monkeypatch.setenv(f"{paths.ENV_PREFIX}DATA_DIR", str(custom))
    assert paths.resolve_data_dir() == custom
    assert (custom / "logs").is_dir()


def test_portable_flag_keeps_data_next_to_app(fake_env, monkeypatch):
    _freeze(monkeypatch)
    (fake_env["app_dir"] / paths.PORTABLE_FLAG).write_text("", encoding="utf-8")
    assert paths.resolve_data_dir() == fake_env["app_dir"] / "data"


def test_legacy_layout_kept_in_place(fake_env, monkeypatch):
    """存量布局（程序目录已有 data/<slug>.db）：原地继续用，绝不擅迁。"""
    _freeze(monkeypatch)
    legacy = _seed_repo_db(fake_env["app_dir"])  # 先造库，再解析
    assert paths.resolve_data_dir() == legacy


def test_dev_legacy_repo_db_kept_in_place(fake_env, monkeypatch):
    """**开发态**存量布局同样原地不动——老检出升级后数据一个字节都不换位置。

    这是「判定 3 必须排在判定 4 之前」的回归防线：一旦顺序颠倒，老检出下次启动
    就会指向一个空的新目录，在用户看来就是「数据全没了」。
    """
    _unfreeze(monkeypatch)
    legacy = _seed_repo_db(fake_env["app_dir"])
    assert paths.resolve_data_dir() == legacy


# ── 判定链：第 4 条（开发态）────────────────────────────────


def test_dev_mode_uses_dev_machine_dir(fake_env, monkeypatch):
    """开发态落机器级 `<slug>-dev`，且**不在仓库工作树内**。"""
    _unfreeze(monkeypatch)
    resolved = paths.resolve_data_dir()
    assert resolved == fake_env["local"] / f"{paths.APP_SLUG}-dev"
    assert (resolved / "logs").is_dir()
    # 硬约束：开发态数据目录绝不位于仓库工作树内（本次改造的根因防线）
    assert not paths.is_inside(resolved, paths.PROJECT_ROOT)


def test_dev_dir_is_separate_from_packaged_dir(fake_env, monkeypatch):
    """开发态与打包态落点必须互不相同，且互不包含。

    同目录的后果不是「看着乱」：开发态的 `PRAGMA user_version` 前向迁移会把用户
    正式库改到新 schema，而降级回旧版本时旧代码读不懂这个 schema。
    """
    _unfreeze(monkeypatch)
    dev = paths.resolve_data_dir()
    packaged = paths.machine_candidates()[0]
    assert dev != packaged
    assert not paths.is_inside(dev, packaged)
    assert not paths.is_inside(packaged, dev)


def test_dev_mode_falls_back_when_machine_dir_unwritable(fake_env, monkeypatch):
    """机器级目录全不可写 → 退回程序目录旁 data/，而不是起不来。

    受限环境（只读盘 / 沙箱）下可用性优先于数据位置；该情形会在启动告警里被点名。
    """
    _unfreeze(monkeypatch)
    monkeypatch.setattr(paths, "_ensure", _only_appdir_writable(fake_env["app_dir"]))
    assert paths.resolve_data_dir() == fake_env["app_dir"] / "data"


def _only_appdir_writable(app_dir: Path):
    real = paths._ensure

    def fake(path: Path) -> Path:
        if not paths.is_inside(path, app_dir):
            raise OSError("只读")
        return real(path)

    return fake


# ── 判定链：第 5 条（打包态）────────────────────────────────


def test_frozen_fresh_install_uses_machine_dir(fake_env, monkeypatch):
    """打包态全新安装（程序目录无 data/）：落机器级目录。

    这条是「解压到新目录 / 从压缩包直接运行不再产生空白数据环境」的根因防线。
    """
    _freeze(monkeypatch)
    resolved = paths.resolve_data_dir()
    assert resolved == fake_env["local"] / paths.APP_SLUG
    assert (resolved / "logs").is_dir()


# ── 布局标签 ────────────────────────────────────────────────


def test_layout_labels(fake_env, monkeypatch):
    _freeze(monkeypatch)
    assert paths.describe_layout(paths.resolve_data_dir()) == "machine"
    assert paths.describe_layout(fake_env["tmp"] / "elsewhere") == "custom"
    # 程序目录 data/ 但无库：打包态是「系统目录不可写时的兜底」
    assert paths.describe_layout(fake_env["app_dir"] / "data") == "appdir"
    # 造出存量库后，同一个位置改判 legacy（放在最后：seed 会留下文件）
    assert paths.describe_layout(_seed_repo_db(fake_env["app_dir"])) == "legacy"


def test_layout_labels_unfrozen(fake_env, monkeypatch):
    """未打包时 portable / legacy / dev 三支都必须可达。

    旧实现把 portable/legacy 的判定放在 `is_frozen()` 之后，导致未打包时这两支
    永远走不到、统统被标成 dev——因为原测试只在 `_freeze` 下断言，一直没暴露。
    """
    _unfreeze(monkeypatch)
    assert paths.describe_layout(paths.resolve_data_dir()) == "dev-machine"
    # 便携标记优先于「无库」
    (fake_env["app_dir"] / paths.PORTABLE_FLAG).write_text("", encoding="utf-8")
    assert paths.describe_layout(fake_env["app_dir"] / "data") == "portable"
    (fake_env["app_dir"] / paths.PORTABLE_FLAG).unlink()
    # 存量库（未打包）→ legacy，不是 dev
    assert paths.describe_layout(_seed_repo_db(fake_env["app_dir"])) == "legacy"


def test_every_layout_has_a_label(fake_env, monkeypatch):
    """describe_layout 的每个返回值都必须有文案，否则日志里会打出裸标签。"""
    _freeze(monkeypatch)
    produced = {
        paths.describe_layout(paths.resolve_data_dir()),
        paths.describe_layout(_seed_repo_db(fake_env["app_dir"])),
        paths.describe_layout(fake_env["tmp"] / "elsewhere"),
        paths.describe_layout(fake_env["app_dir"] / "data"),
        paths.describe_layout(fake_env["local"] / f"{paths.APP_SLUG}-dev"),
    }
    assert produced <= set(paths.LAYOUT_LABEL)


# ── is_inside / layout_warning ──────────────────────────────


def test_is_inside(tmp_path):
    assert paths.is_inside(tmp_path / "a" / "b", tmp_path)
    assert paths.is_inside(tmp_path / "data.old", tmp_path)
    assert not paths.is_inside(tmp_path, tmp_path)  # 相等不算「之内」
    assert not paths.is_inside(tmp_path.parent, tmp_path)


def test_layout_warning_fires_for_repo_internal_data(fake_env, monkeypatch):
    """仓库工作树内的数据目录 → 每次启动都告警，并指出正确做法。"""
    _unfreeze(monkeypatch)
    msg = paths.layout_warning(fake_env["app_dir"] / "data")
    assert "仓库工作树内" in msg
    assert "dev.bat" in msg  # 必须给出可执行的正解，而不只是报错
    assert paths.ENV_PREFIX in msg  # 以及另一条出路：显式指定目录


def test_layout_warning_silent_outside_repo(fake_env, monkeypatch):
    _unfreeze(monkeypatch)
    assert paths.layout_warning(fake_env["local"] / paths.APP_SLUG) == ""
    assert paths.layout_warning(paths.resolve_data_dir()) == ""  # 正常开发态不告警


def test_layout_warning_silent_when_explicitly_configured(fake_env, monkeypatch):
    """显式设了 DATA_DIR → 不告警：用户已明确指定位置，尊重其选择。"""
    _unfreeze(monkeypatch)
    repo_data = fake_env["app_dir"] / "data"
    monkeypatch.setenv(f"{paths.ENV_PREFIX}DATA_DIR", str(repo_data))
    assert paths.layout_warning(repo_data) == ""


def test_layout_warning_silent_when_frozen(fake_env, monkeypatch):
    """打包态不告警：把发布包解压进某个 git 检出是常规做法，不是开发态污染。"""
    _freeze(monkeypatch)
    assert paths.layout_warning(fake_env["app_dir"] / "data") == ""
