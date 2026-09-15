"""换装机制验收（desktop/main.py）：让位/落位/回滚三态 + 暂存包能力探测与拒换。

背景（实测事故）：老换装逻辑在**运行中的**程序目录里 `shutil.move` 装载中的
`_internal`——`os.rename` 被 Windows 拒绝（WinError 5）后 `shutil.move` 静默降级
为 copytree+rmtree，只复制得动未被占用的文件、再把原目录删剩被占用的那些，
安装当场半截化且回滚没救回来。现在的规则：换装由**独立进程**在旧进程退出后
执行，同卷让位只用 `os.replace`，跨卷落位显式复制且失败即回滚。

本文件在 tmp_path 里合成「旧安装 + 暂存新载荷」目录树，覆盖：
1. 正常换装：程序条目整体换代、`data/` 用户数据不动、上一版进 `__old__`；
2. 落位失败（复制抛错）→ 回滚，原安装完好可启动；
3. 落位数量对不上（复制静默失败）→ 校验拦下并回滚；
4. 暂存包能力探测：`--help` 里有换装入口才算支持；
5. 老暂存包（无换装入口）→ 拒换，一个文件都不动，只落标记避免重复弹窗。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _load_desktop_main():
    """按路径加载桌面启动器模块（desktop/ 不在 pytest 的 import 路径上）。"""
    spec = importlib.util.spec_from_file_location(
        "holdexar_desktop_main", ROOT / "desktop" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


desktop = _load_desktop_main()
APP_NAME = desktop.APP_NAME
EXE_NAME = f"{APP_NAME}.exe"


def _make_install(root: Path, *, exe: str = "old-exe", files: dict | None = None) -> Path:
    """合成旧安装：主程序 + `_internal/` + 用户数据目录 data/。"""
    root.mkdir(parents=True, exist_ok=True)
    (root / exe and root / EXE_NAME).write_bytes(exe.encode())
    internal = root / "_internal"
    internal.mkdir()
    for name, content in (files or {"a.txt": "old-a", "b.txt": "old-b"}).items():
        (internal / name).write_text(content, encoding="utf-8")
    user_data = root / "data"
    user_data.mkdir()
    (user_data / "holdexar.db").write_bytes(b"user-data")
    return root


def _make_payload(staging: Path, *, exe: str = "new-exe", files: dict | None = None) -> Path:
    """合成暂存新载荷：staging/<APP_NAME>/（结构与安装目录同形）。"""
    payload = staging / APP_NAME
    internal = payload / "_internal"
    internal.mkdir(parents=True)
    (payload / EXE_NAME).write_bytes(exe.encode())
    for name, content in (files or {"a.txt": "new-a", "c.txt": "new-c"}).items():
        (internal / name).write_text(content, encoding="utf-8")
    return payload


def _stage_manifest(staging: Path, tag: str = "v0.1.0-beta.1", ready: bool = True) -> None:
    import json

    staging.mkdir(parents=True, exist_ok=True)
    (staging / "manifest.json").write_text(
        json.dumps({"tag": tag, "sha256": "x" * 64, "ready": ready}), encoding="utf-8"
    )


# ── 换装主体（_swap_payload）───────────────────────────────────────────


def test_swap_payload_replaces_install_and_keeps_user_data(tmp_path: Path) -> None:
    """正常换装：程序条目整体换代；data/ 用户数据不动；上一版进 __old__。"""
    install = _make_install(tmp_path / "install")
    staging = tmp_path / "staging"
    _make_payload(staging)
    _stage_manifest(staging)

    assert desktop._swap_payload(staging, install) is True

    # 新载荷落位：主程序换代、新文件在、旧版独有文件随旧版一起让位
    assert (install / EXE_NAME).read_bytes() == b"new-exe"
    assert (install / "_internal" / "a.txt").read_text(encoding="utf-8") == "new-a"
    assert (install / "_internal" / "c.txt").is_file()
    assert not (install / "_internal" / "b.txt").exists()
    # 用户数据一个字节没动（白名单）
    assert (install / "data" / "holdexar.db").read_bytes() == b"user-data"
    # 上一版完整留在 __old__（手工恢复的唯一副本，绝不删）
    assert (install / "__old__" / EXE_NAME).read_bytes() == b"old-exe"
    assert (install / "__old__" / "_internal" / "b.txt").read_text(encoding="utf-8") == "old-b"
    # 暂存目录已消费（manifest 先删，避免重启再触发换装）
    assert not (staging / "manifest.json").exists()


def test_swap_payload_rolls_back_when_landing_fails(tmp_path: Path, monkeypatch) -> None:
    """落位抛错 → 整体回滚，原安装（含用户数据）完好。"""
    install = _make_install(tmp_path / "install")
    staging = tmp_path / "staging"
    _make_payload(staging)
    _stage_manifest(staging)

    def _boom(*_args, **_kwargs):
        raise OSError("模拟复制失败（磁盘满/权限）")

    monkeypatch.setattr(desktop.shutil, "copytree", _boom)
    monkeypatch.setattr(desktop.shutil, "copy2", _boom)

    assert desktop._swap_payload(staging, install) is False

    assert (install / EXE_NAME).read_bytes() == b"old-exe"
    assert (install / "_internal" / "a.txt").read_text(encoding="utf-8") == "old-a"
    assert (install / "_internal" / "b.txt").is_file()
    assert (install / "data" / "holdexar.db").read_bytes() == b"user-data"
    # 暂存包原样保留（下次启动可重试）
    assert (staging / "manifest.json").exists()


def test_swap_payload_rolls_back_on_file_count_mismatch(tmp_path: Path, monkeypatch) -> None:
    """落位"成功"但文件数对不上（半截复制）→ 校验拦下并回滚。"""
    install = _make_install(tmp_path / "install")
    staging = tmp_path / "staging"
    _make_payload(staging)
    _stage_manifest(staging)

    def _empty_dir(_src, dst, *args, **kwargs):
        Path(dst).mkdir(parents=True, exist_ok=True)  # 复制了个空目录：静默半截

    monkeypatch.setattr(desktop.shutil, "copytree", _empty_dir)
    monkeypatch.setattr(desktop.shutil, "copy2", lambda *_a, **_k: None)

    assert desktop._swap_payload(staging, install) is False

    assert (install / EXE_NAME).read_bytes() == b"old-exe"
    assert (install / "_internal" / "a.txt").is_file()
    assert (install / "data" / "holdexar.db").read_bytes() == b"user-data"


def test_swap_payload_refuses_when_old_residue_locked(tmp_path: Path, monkeypatch) -> None:
    """上一版残留 __old__ 清不掉 → 放弃换装，绝不动当前安装。"""
    install = _make_install(tmp_path / "install")
    (install / "__old__").mkdir()
    (install / "__old__" / "stale.txt").write_text("stale", encoding="utf-8")
    staging = tmp_path / "staging"
    _make_payload(staging)
    _stage_manifest(staging)

    # rmtree 被顶替为「什么都没删」：模拟 __old__ 被占用删不掉
    monkeypatch.setattr(desktop.shutil, "rmtree", lambda *_a, **_k: None)

    assert desktop._swap_payload(staging, install) is False
    assert (install / EXE_NAME).read_bytes() == b"old-exe"
    assert (install / "_internal" / "a.txt").exists()


# ── 暂存包能力探测与交接（_staged_supports_helper / _handoff_pending_update）──


def test_staged_supports_helper_detects_flag(tmp_path: Path, monkeypatch) -> None:
    """`--help` 里报出换装入口才算支持（老包没有该长选项）。"""
    exe = tmp_path / EXE_NAME
    exe.write_bytes(b"MZ")

    class _Result:
        def __init__(self, out: str) -> None:
            self.stdout = out
            self.stderr = ""

    monkeypatch.setattr(
        desktop.subprocess, "run", lambda *_a, **_k: _Result(
            "options:\n  --server\n  --apply-update\n  --staging\n"
        )
    )
    assert desktop._staged_supports_helper(exe) is True

    monkeypatch.setattr(
        desktop.subprocess, "run", lambda *_a, **_k: _Result("options:\n  --server\n")
    )
    assert desktop._staged_supports_helper(exe) is False


def test_staged_supports_helper_false_when_probe_fails(tmp_path: Path, monkeypatch) -> None:
    """起不来/超时一律按不支持处理（宁可不更新，不能毁现装）。"""
    exe = tmp_path / EXE_NAME
    exe.write_bytes(b"MZ")

    def _boom(*_args, **_kwargs):
        raise OSError("无法执行")

    monkeypatch.setattr(desktop.subprocess, "run", _boom)
    assert desktop._staged_supports_helper(exe) is False


def test_handoff_refuses_legacy_payload_without_touching_files(
    tmp_path: Path, monkeypatch
) -> None:
    """老暂存包（无换装入口）→ 拒换：不动任何文件，只落记号避免重复弹窗。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging, tag="v0.1.0")
    install = _make_install(tmp_path / "install")
    alerts: list[str] = []

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(desktop, "_staged_supports_helper", lambda _exe: False)
    monkeypatch.setattr(desktop, "_alert", lambda msg: alerts.append(msg))

    assert desktop._handoff_pending_update() is False

    # 安装目录与暂存包原封不动
    assert (install / EXE_NAME).read_bytes() == b"old-exe"
    assert (install / "_internal" / "a.txt").exists()
    assert (staging / APP_NAME / EXE_NAME).exists()
    assert (staging / "manifest.json").exists()
    # 记号已落：第二次启动不再重复探测/弹窗
    assert (staging / desktop._UNSUPPORTED_MARK).exists()
    assert len(alerts) == 1  # 首次即弹窗告知（用户要知道为什么没自动更新）

    assert desktop._handoff_pending_update() is False
    assert len(alerts) == 1  # 已记过，不再重复弹


def test_handoff_spawns_helper_with_wait_pid(tmp_path: Path, monkeypatch) -> None:
    """支持的暂存包 → 以 DETACHED 方式拉起其 exe，带上暂存/目标/旧进程 PID。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging)
    install = _make_install(tmp_path / "install")
    calls: list = []

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(desktop, "_staged_supports_helper", lambda _exe: True)
    monkeypatch.setattr(desktop.os, "getpid", lambda: 4242)
    monkeypatch.setattr(
        desktop.subprocess, "Popen", lambda cmd, **kwargs: calls.append((cmd, kwargs))
    )

    assert desktop._handoff_pending_update() is True

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[0] == str(staging / APP_NAME / EXE_NAME)
    assert desktop._HELPER_FLAG in cmd
    assert "--staging" in cmd and str(staging) in cmd
    assert "--target" in cmd and str(install) in cmd
    assert "--wait-pid" in cmd and "4242" in cmd
    assert kwargs["creationflags"] == getattr(desktop.subprocess, "DETACHED_PROCESS", 0)


def test_handoff_ignores_staging_without_manifest(tmp_path: Path, monkeypatch) -> None:
    """无 manifest / 非就绪 → 不交接（正常启动）。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    assert desktop._handoff_pending_update() is False

    _stage_manifest(staging, ready=False)
    assert desktop._handoff_pending_update() is False


def test_staged_main_exe_tolerates_renamed_installed_exe(tmp_path: Path, monkeypatch) -> None:
    """用户给主程序改过名时，仍能认出暂存包里的主程序（不误报「缺少主程序」）。"""
    payload = tmp_path / APP_NAME
    payload.mkdir(parents=True)
    (payload / EXE_NAME).write_bytes(b"MZ")
    monkeypatch.setattr(desktop.sys, "executable", str(tmp_path / "Holdexar (1).exe"))

    assert desktop._staged_main_exe(payload) == payload / EXE_NAME


def test_staged_main_exe_none_when_payload_has_no_exe(tmp_path: Path) -> None:
    """载荷根目录一个 exe 都没有 → 无主程序（拒换）。"""
    payload = tmp_path / APP_NAME
    payload.mkdir(parents=True)
    (payload / "_internal").mkdir()
    assert desktop._staged_main_exe(payload) is None


def test_handoff_skipped_in_dev_mode(tmp_path: Path, monkeypatch) -> None:
    """开发态不换装：`_app_root()` 是仓库根，动它就是毁工作树。"""
    monkeypatch.setattr(desktop, "is_frozen", lambda: False)
    assert desktop._handoff_pending_update() is False
