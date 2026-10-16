"""换装机制验收（desktop/main.py）：让位/落位/回滚三态 + 暂存包能力探测与拒换。

被测规则：换装由**独立进程**在旧进程退出后执行，同卷让位只用 `os.replace`，
跨卷落位显式复制且失败即回滚。不可在运行中的程序目录里 `shutil.move` 装载中的
`_internal`——`os.rename` 被 Windows 拒绝（WinError 5）后 `shutil.move` 会静默
降级为 copytree+rmtree，只复制得动未被占用的文件、再把原目录删剩被占用的那些，
安装当场半截化。

本文件在 tmp_path 里合成「旧安装 + 暂存新载荷」目录树，覆盖：
1. 正常换装：程序条目整体换代、`data/` 用户数据不动、上一版进 `__old__`；
2. 落位失败（复制抛错）→ 回滚，原安装完好可启动；
3. 落位数量对不上（复制静默失败）→ 校验拦下并回滚；
4. 暂存包能力探测：`--help` 里有换装入口才算支持；
5. 老暂存包（无换装入口）→ 拒换，一个文件都不动，只落标记避免重复弹窗；
6. 换装失败 → 落失败标记 + 作废该暂存载荷 + 回滚后拉起现装；
7. 失败标记在场 → 交接拒换（不重演同一次失败），且 `__old__` 恢复副本不被清理。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

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
_ORIGINAL_SWAP_PAYLOAD = desktop._swap_payload
_ORIGINAL_VERIFY_HEALTH = desktop._verify_new_version_health


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


def _stage_manifest(staging: Path, tag: str = "v9.9.9", ready: bool = True) -> None:
    """默认 tag 必须比现装（app_info.APP_VERSION，0.3.1-beta）新：版本门只放行更新。"""
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
    _stage_manifest(staging, tag="v9.9.9")
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


# ── 失败路径（标记 / 拉起次序 / 作废清理）──────────────────────────────


class _SyncThread:
    """后台线程改同步执行：用于断言「先拉起现装、再弹窗」的次序。

    弹窗是模态阻塞调用，排在拉起之前就会把现装挡在对话框之后。
    """

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self) -> None:
        if self._target:
            self._target(*self._args, **self._kwargs)


def _prepare_failed_helper_case(
    tmp_path: Path, monkeypatch, *, install: Path | None = None
) -> tuple[Path, Path, Path, list]:
    """合成「换装必然失败」的现场。

    返回 (数据目录, 暂存目录, 安装目录, 事件序列)；事件序列按发生次序记录
    ("spawn", exe) 与 ("alert", 文案)。
    """
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging)
    target = install if install is not None else _make_install(tmp_path / "install")
    events: list[tuple[str, object]] = []

    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_swap_payload", lambda _s, _t, **kwargs: False)
    # 互斥体是真实内核对象：helper 单测不碰它（独占语义由专属用例验证）
    monkeypatch.setattr(desktop, "_acquire_swap_mutex", lambda: True)
    monkeypatch.setattr(desktop, "_alert", lambda msg: events.append(("alert", msg)))
    monkeypatch.setattr(
        desktop.subprocess,
        "Popen",
        lambda cmd, **kwargs: events.append(("spawn", cmd[0])),
    )
    monkeypatch.setattr(desktop.threading, "Thread", _SyncThread)
    monkeypatch.setattr(desktop, "_verify_new_version_health", lambda *a, **k: (True, "ready", {}))
    def restored(target, **kwargs):
        exe = desktop._staged_main_exe(target)
        if exe is None:
            return False
        desktop.subprocess.Popen([str(exe)])
        return True
    monkeypatch.setattr(desktop, "_relaunch_and_verify_old_version", restored)
    return data_dir, staging, target, events


def test_run_update_helper_failure_marks_then_relaunches(
    tmp_path: Path, monkeypatch
) -> None:
    """换装失败：落安装状态标记 → 先拉起现装 → 再弹窗（次序不能反）。"""
    data_dir, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)

    desktop._run_update_helper(staging, target, None)

    # 标记落在数据目录（不随暂存目录清理）
    flag = data_dir / desktop._FAILED_MARK
    assert flag.is_file()
    assert str(target) in flag.read_text(encoding="utf-8")
    # 拉起先于弹窗：反过来会把现装挡在模态对话框之后
    assert [kind for kind, _ in events] == ["spawn", "alert"]
    assert events[0][1] == str(target / EXE_NAME)
    # 载荷留给下一次启动的现装进程清理（本进程跑在它里面，删不掉自己）
    assert (staging / APP_NAME / EXE_NAME).exists()


def test_run_update_helper_failure_without_target_exe(tmp_path: Path, monkeypatch) -> None:
    """程序目录内没有主程序：只落标记与弹窗，不凭空拉起。"""
    empty_install = tmp_path / "install"
    empty_install.mkdir()
    data_dir, staging, target, events = _prepare_failed_helper_case(
        tmp_path, monkeypatch, install=empty_install
    )

    desktop._run_update_helper(staging, target, None)

    assert (data_dir / desktop._FAILED_MARK).is_file()
    assert [kind for kind, _ in events] == ["alert"]


def test_handoff_refuses_after_failed_swap(tmp_path: Path, monkeypatch) -> None:
    """失败标记在场：不再交接，否则每次启动都在旧进程退出后重演同一次失败。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging)
    (data_dir / desktop._FAILED_MARK).write_text("failed\n", encoding="utf-8")
    install = _make_install(tmp_path / "install")
    spawns: list = []

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(desktop, "_staged_supports_helper", lambda _exe: True)
    monkeypatch.setattr(
        desktop.subprocess, "Popen", lambda cmd, **kwargs: spawns.append((cmd, kwargs))
    )

    assert desktop._handoff_pending_update() is False
    assert spawns == []
    # 现装与暂存包都没被动过（作废清理归下一次启动）
    assert (install / EXE_NAME).read_bytes() == b"old-exe"
    assert (staging / APP_NAME / EXE_NAME).exists()


def test_should_clean_old_dir_keeps_backup_after_failed_swap(
    tmp_path: Path, monkeypatch
) -> None:
    """失败标记在场时不清 `__old__`：回滚没搬回原件时它是手工恢复的唯一退路。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    old_dir = tmp_path / "install" / "__old__"
    old_dir.mkdir(parents=True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)

    assert desktop._should_clean_old_dir(old_dir) is True
    (data_dir / desktop._FAILED_MARK).write_text("failed\n", encoding="utf-8")
    assert desktop._should_clean_old_dir(old_dir) is False
    assert desktop._should_clean_old_dir(tmp_path / "absent") is False


def test_cleanup_staging_leftover_removes_voided_package(
    tmp_path: Path, monkeypatch
) -> None:
    """失败标记在场：作废的暂存包被清掉（腾空间 + 不再报「已下载待重启」）。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)

    # 未失败且带 manifest：待换装的正经包，不许动
    desktop._cleanup_staging_leftover()
    assert (staging / "manifest.json").is_file()

    (data_dir / desktop._FAILED_MARK).write_text("failed\n", encoding="utf-8")
    desktop._cleanup_staging_leftover()
    assert not staging.exists()
    # 标记本身在数据目录，清理带不走它
    assert (data_dir / desktop._FAILED_MARK).is_file()


def test_cleanup_staging_leftover_removes_consumed_staging(
    tmp_path: Path, monkeypatch
) -> None:
    """无 manifest 的暂存目录是已消费残留：启动即清。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)

    desktop._cleanup_staging_leftover()
    assert not staging.exists()


# ── 并发与循环防线（版本门 / 交接账本 / 换装互斥）──────────────────────


def test_handoff_refuses_stale_version_and_cleans_staging(
    tmp_path: Path, monkeypatch
) -> None:
    """版本门：同版本/更旧的暂存包不换装，清掉暂存防反复触发交接。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging, tag="v0.1.0-beta.2")  # 比现装 0.3.1-beta 旧
    install = _make_install(tmp_path / "install")
    spawns: list = []

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(
        desktop.subprocess, "Popen", lambda cmd, **kwargs: spawns.append(cmd)
    )

    assert desktop._handoff_pending_update() is False
    assert spawns == []
    assert not staging.exists()  # 残包被清理
    assert (install / EXE_NAME).read_bytes() == b"old-exe"


def test_handoff_ledger_terminates_second_handoff(tmp_path: Path, monkeypatch) -> None:
    """交接账本：同一暂存包第二次交接被终止——旧构建暂存包（不摘 manifest）
    的死循环由此斩断，不依赖暂存包自己守规矩。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging, tag="v9.9.9")
    install = _make_install(tmp_path / "install")
    spawns: list = []
    alerts: list[str] = []

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(desktop, "_staged_supports_helper", lambda _exe: True)
    monkeypatch.setattr(
        desktop.subprocess, "Popen", lambda cmd, **kwargs: spawns.append(cmd)
    )
    monkeypatch.setattr(desktop, "_alert", lambda msg: alerts.append(msg))

    # 第一次交接：放行，账本落 count=1
    assert desktop._handoff_pending_update() is True
    assert len(spawns) == 1
    import json

    ledger = json.loads(
        (data_dir / desktop.HANDOFF_LEDGER_NAME).read_text(encoding="utf-8")
    )
    assert ledger["tag"] == "v9.9.9"
    assert ledger["count"] == 1

    # 第二次交接（模拟：换装失败回滚后现装又被拉起）：终止 + 清场 + 告知
    assert desktop._handoff_pending_update() is False
    assert len(spawns) == 1  # 没有第二个换装进程
    assert not staging.exists()  # 已被证明换不动的包被清理
    assert not (data_dir / desktop.HANDOFF_LEDGER_NAME).exists()  # 账本复位
    assert len(alerts) == 1  # 用户得到「上次更新失败已回滚」的说明


def test_handoff_clears_stale_ledger_when_no_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    """无待换装包时顺带清旧账本：旧构建换装进程不清账，现装启动兜底复位。"""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / desktop.HANDOFF_LEDGER_NAME).write_text(
        '{"tag": "v9.9.9", "count": 1}', encoding="utf-8"
    )

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)

    assert desktop._handoff_pending_update() is False
    assert not (data_dir / desktop.HANDOFF_LEDGER_NAME).exists()


def test_run_update_helper_success_clears_ledger_and_flag(
    tmp_path: Path, monkeypatch
) -> None:
    """换装成功：交接账本与失败标记一并清零，安装状态回到干净态。"""
    data_dir, staging, target, events = _prepare_failed_helper_case(
        tmp_path, monkeypatch
    )
    # 反转前置桩：这次换装成功
    monkeypatch.setattr(desktop, "_swap_payload", lambda _s, _t, **kwargs: True)
    (data_dir / desktop._FAILED_MARK).write_text("stale\n", encoding="utf-8")
    (data_dir / desktop.HANDOFF_LEDGER_NAME).write_text(
        '{"tag": "v9.9.9", "count": 1}', encoding="utf-8"
    )

    desktop._run_update_helper(staging, target, None)

    assert not (data_dir / desktop.HANDOFF_LEDGER_NAME).exists()
    assert not (data_dir / desktop._FAILED_MARK).is_file()
    # 成功后拉起新版本
    assert [kind for kind, _ in events] == ["spawn"]
    assert events[0][1] == str(target / EXE_NAME)


@pytest.mark.skipif(os.name != "nt", reason="换装互斥体是 Windows 命名互斥")
def test_swap_mutex_serializes_holders() -> None:
    """互斥独占：持有期间第二个申请者拿不到；持有者退场后互斥体随之消失。"""
    import threading

    acquired = threading.Event()
    release = threading.Event()

    def _holder() -> None:
        assert desktop._acquire_swap_mutex() is True
        acquired.set()
        release.wait(5)  # 持有到主线程放行；句柄随线程退出被内核回收

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    assert acquired.wait(2)

    assert desktop._swap_mutex_held() is True
    release.set()
    thread.join(timeout=2)
    desktop._release_swap_mutex()
    assert desktop._swap_mutex_held() is False


@pytest.mark.skipif(os.name != "nt", reason="换装互斥体是 Windows 命名互斥")
def test_release_swap_mutex_clears_held() -> None:
    """换装完成或失败拉起前主动释放互斥体，放行新实例。"""
    import threading

    acquired = threading.Event()
    released = threading.Event()

    def _holder() -> None:
        assert desktop._acquire_swap_mutex() is True
        acquired.set()
        released.wait(5)
        desktop._release_swap_mutex()

    thread = threading.Thread(target=_holder, daemon=True)
    thread.start()
    assert acquired.wait(2)
    assert desktop._swap_mutex_held() is True

    released.set()
    thread.join(timeout=2)
    assert desktop._swap_mutex_held() is False


def test_handoff_ledger_not_recorded_when_popen_fails(tmp_path: Path, monkeypatch) -> None:
    """拉起换装进程抛异常时，不落交接账本：防止启动失败被当成换装失败清空暂存。"""
    data_dir = tmp_path / "data"
    staging = data_dir / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging, tag="v9.9.9")
    install = _make_install(tmp_path / "install")

    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data_dir)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(desktop, "_staged_supports_helper", lambda _exe: True)

    def _boom_popen(*args, **kwargs):
        raise OSError("模拟系统拒绝创建进程")

    monkeypatch.setattr(desktop.subprocess, "Popen", _boom_popen)

    assert desktop._handoff_pending_update() is False
    assert not (data_dir / desktop.HANDOFF_LEDGER_NAME).exists()
    # 暂存包依然完好
    assert (staging / "manifest.json").exists()


def test_run_update_helper_rolls_back_when_new_exe_crashes(tmp_path: Path, monkeypatch) -> None:
    """新版本启动即崩溃（退出码非 0）时，helper 自动回滚到 __old__ 并拉起旧版。"""
    data_dir, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)

    class MockCrashProcess:
        def wait(self, timeout=None):
            return 1

    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kwargs: MockCrashProcess())
    monkeypatch.setattr(desktop, "_verify_new_version_health", _ORIGINAL_VERIFY_HEALTH)

    desktop._run_update_helper(staging, target, None)

    assert (data_dir / desktop._FAILED_MARK).is_file()
    assert (target / EXE_NAME).read_bytes() == b"old-exe"
    diag = desktop._read_diagnosis()
    assert diag.get("errorCode") == "UPDATE-LAUNCH-FAIL"
    assert diag.get("rollback", {}).get("completed") is True


def test_restore_moved_reports_completeness(tmp_path: Path, monkeypatch) -> None:
    """回滚完整性度量：全部恢复返回 True，部分条目失败返回 False 并列出清单。"""
    target = tmp_path / "target"
    old_dir = tmp_path / "old"
    target.mkdir()
    old_dir.mkdir()
    (old_dir / "ok.txt").write_text("ok", encoding="utf-8")
    (old_dir / "fail.txt").write_text("fail", encoding="utf-8")

    def _replace_mock(src: Path, dst: Path, *args, **kwargs):
        if src.name == "fail.txt":
            raise PermissionError("模拟占用")
        src.replace(dst)

    monkeypatch.setattr(desktop, "_replace_with_retry", _replace_mock)

    completed, failed = desktop._restore_moved(["ok.txt", "fail.txt"], target, old_dir)
    assert completed is False
    assert failed == ["fail.txt"]
    assert (target / "ok.txt").read_text(encoding="utf-8") == "ok"
    assert (old_dir / "fail.txt").read_text(encoding="utf-8") == "fail"


def test_wait_target_idle_verifies_taskkill_returncode(tmp_path: Path, monkeypatch) -> None:
    """进程等待与强制清理验证退出码：杀死失败时返回未退出 PID。"""
    target = tmp_path / "target"
    target.mkdir()

    monkeypatch.setattr(desktop, "_pids_running_from", lambda _t: [12345])

    class MockCompletedProcess:
        returncode = 1
        stdout = ""
        stderr = "ERROR: Access denied"

    calls = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: calls.append(a) or MockCompletedProcess())
    monkeypatch.setattr(desktop.time, "sleep", lambda _s: None)

    remaining = desktop._wait_target_idle(target, timeout_s=0.01, grace_s=0)
    assert remaining == [12345]
    assert calls


def test_run_update_helper_aborts_when_target_locked(tmp_path: Path, monkeypatch) -> None:
    """程序目录存在无法退出的残留进程时，换装提前中止，不动任何安装文件。"""
    data_dir, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    (target / "app.txt").write_text("running", encoding="utf-8")

    monkeypatch.setattr(desktop, "_wait_target_idle", lambda _t: [8888])

    desktop._run_update_helper(staging, target, None)

    diag = desktop._read_diagnosis()
    assert diag.get("errorCode") == "UPDATE-FILE-LOCK"
    assert "8888" in (diag.get("errorMessage") or "")
    assert (target / "app.txt").read_text(encoding="utf-8") == "running"
    assert not (target / "__old__").exists()


def test_swap_payload_preflight_disk_space_abort(tmp_path: Path, monkeypatch) -> None:
    """预检磁盘剩余空间：空间不足直接终止换装，不修改目标文件。"""
    install = _make_install(tmp_path / "install")
    staging = tmp_path / "staging"
    _make_payload(staging)
    _stage_manifest(staging)

    import collections
    Usage = collections.namedtuple("Usage", ["total", "used", "free"])
    monkeypatch.setattr(desktop.shutil, "disk_usage", lambda _p: Usage(1000, 900, 100))

    diag_ctx = desktop._init_diagnosis(staging, install)
    assert desktop._swap_payload(staging, install, diag_ctx=diag_ctx) is False
    assert diag_ctx.get("errorCode") == "UPDATE-DISK-SPACE"
    assert (install / EXE_NAME).read_bytes() == b"old-exe"


def test_run_update_helper_post_swap_health_timeout_rolls_back(tmp_path: Path, monkeypatch) -> None:
    """新版本启动但健康检查超时：自动回滚到原版本并记录故障归因。"""
    data_dir, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)

    class MockRunningProcess:
        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kwargs: MockRunningProcess())

    def _mock_verify_health(target_version, proc, timeout=25.0):
        return False, "等待健康检查超时（模拟）", None

    monkeypatch.setattr(desktop, "_verify_new_version_health", _mock_verify_health)

    desktop._run_update_helper(staging, target, None)

    assert (data_dir / desktop._FAILED_MARK).is_file()
    assert (target / EXE_NAME).read_bytes() == b"old-exe"
    diag = desktop._read_diagnosis()
    assert diag.get("errorCode") == "UPDATE-HEALTH-TIMEOUT"
    assert diag.get("rollback", {}).get("completed") is True


def test_run_update_helper_records_successful_diagnosis(tmp_path: Path, monkeypatch) -> None:
    """换装成功并经验收通过：诊断记录标记完成并持久化。"""
    data_dir, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)

    class MockRunningProcess:
        def poll(self):
            return None

    monkeypatch.setattr(desktop.subprocess, "Popen", lambda cmd, **kwargs: MockRunningProcess())

    def _mock_verify_health(target_version, proc, timeout=25.0):
        return True, "健康检查通过", {"app": desktop.APP_NAME, "version": "9.9.9"}

    monkeypatch.setattr(desktop, "_verify_new_version_health", _mock_verify_health)

    desktop._run_update_helper(staging, target, None)

    diag = desktop._read_diagnosis()
    assert diag.get("success") is True
    assert diag.get("stage") == "completed"
    assert diag.get("healthCheck", {}).get("status") == "passed"


def test_run_update_helper_incomplete_rollback_alert(tmp_path: Path, monkeypatch) -> None:
    """回滚不完整时弹窗明确提示未恢复文件与手工恢复路径。"""
    data_dir, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)

    def _fake_swap(s, t, diag_ctx=None):
        if diag_ctx is not None:
            diag_ctx["errorCode"] = "UPDATE-FILE-LOCK"
            diag_ctx["errorMessage"] = "模拟搬运失败"
            diag_ctx["rollback"] = {
                "attempted": True,
                "completed": False,
                "failedFiles": ["locked_file.dll"],
            }
        return False

    monkeypatch.setattr(desktop, "_swap_payload", _fake_swap)

    desktop._run_update_helper(staging, target, None)

    diag = desktop._read_diagnosis()
    assert diag.get("errorCode") == "UPDATE-FILE-LOCK"
    assert diag.get("rollback", {}).get("completed") is False
    assert diag.get("rollback", {}).get("failedFiles") == ["locked_file.dll"]
    alerts = [text for kind, text in events if kind == "alert"]
    assert len(alerts) == 1
    assert "部分原文件未恢复" in alerts[0]
    assert "locked_file.dll" in alerts[0]




def test_helper_guards_backup_before_starting_new_version(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)
    observed = []
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *a, **k: observed.append(desktop._health_checking_flag().exists()))
    monkeypatch.setattr(desktop, "_verify_new_version_health", lambda *a, **k: (True, "ready", {}))
    desktop._run_update_helper(staging, target, None)
    assert observed == [True]


def test_helper_restores_old_files_when_new_process_cannot_start(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)
    def denied(*args, **kwargs):
        raise PermissionError("launch denied")
    monkeypatch.setattr(desktop.subprocess, "Popen", denied)
    monkeypatch.setattr(desktop, "_relaunch_and_verify_old_version", lambda *a, **k: True)
    desktop._run_update_helper(staging, target, None)
    assert (target / EXE_NAME).read_bytes() == b"old-exe"
    assert desktop._read_diagnosis()["rollback"]["completed"] is True
    assert desktop._swap_failed_flag().exists()


def test_helper_confirms_new_tree_exit_before_restoring_files(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)
    checks = []
    monkeypatch.setattr(desktop, "_wait_target_idle", lambda *a, **k: checks.append(k) or [])
    monkeypatch.setattr(desktop, "_verify_new_version_health", lambda *a, **k: (False, "not ready", None))
    monkeypatch.setattr(desktop, "_relaunch_and_verify_old_version", lambda *a, **k: True)
    desktop._run_update_helper(staging, target, None)
    assert len(checks) >= 2
    assert (target / EXE_NAME).read_bytes() == b"old-exe"


def test_helper_keeps_backup_and_new_files_while_new_tree_is_stuck(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)
    checks = []
    def idle(*args, **kwargs):
        checks.append(kwargs)
        return [] if len(checks) == 1 else [12345]
    monkeypatch.setattr(desktop, "_wait_target_idle", idle)
    monkeypatch.setattr(desktop, "_verify_new_version_health", lambda *a, **k: (False, "not ready", None))
    monkeypatch.setattr(desktop, "_relaunch_and_verify_old_version", lambda *a, **k: True)
    desktop._run_update_helper(staging, target, None)
    assert (target / "__old__" / EXE_NAME).read_bytes() == b"old-exe"
    assert (target / EXE_NAME).read_bytes() == b"new-exe"
    assert desktop._read_diagnosis()["rollback"]["completed"] is False


def test_failed_recovery_does_not_claim_old_version_is_open(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_relaunch_and_verify_old_version", lambda *a, **k: False)
    desktop._run_update_helper(staging, target, None)
    alerts = [text for kind, text in events if kind == "alert"]
    assert alerts and "重新打开应用" not in alerts[0]
    assert desktop._read_diagnosis().get("restoration", {}).get("healthy") is False


def test_startup_preserves_staging_during_health_verification(tmp_path, monkeypatch):
    data = tmp_path / "data"
    staging = data / desktop.STAGING_DIR_NAME
    staging.mkdir(parents=True)
    marker = staging / "runtime.txt"
    marker.write_text("in use", encoding="utf-8")
    monkeypatch.setattr(desktop, "_data_dir", lambda: data)
    desktop._health_checking_flag().write_text("verifying", encoding="utf-8")
    desktop._cleanup_staging_leftover()
    assert marker.exists()


def test_health_rejects_response_from_another_update_attempt(monkeypatch):
    class Proc:
        _update_attempt_id = "this-attempt"
        def poll(self):
            return None
    class Response:
        status_code = 200
        def json(self):
            return {"app": desktop.APP_NAME, "version": "9.9.9", "updateAttemptId": "other-attempt"}
    monkeypatch.setattr(desktop.httpx, "get", lambda *a, **k: Response())
    ok, _, _ = desktop._verify_new_version_health("9.9.9", Proc(), timeout=0.01)
    assert not ok


def test_health_does_not_accept_version_prefix(monkeypatch):
    class Proc:
        _update_attempt_id = "this-attempt"
        def poll(self):
            return None
    class Response:
        status_code = 200
        def json(self):
            return {"app": desktop.APP_NAME, "version": "9.9.90", "updateAttemptId": "this-attempt"}
    monkeypatch.setattr(desktop.httpx, "get", lambda *a, **k: Response())
    ok, _, _ = desktop._verify_new_version_health("9.9.9", Proc(), timeout=0.01)
    assert not ok


def test_health_without_process_is_not_success():
    ok, _, _ = desktop._verify_new_version_health("9.9.9", None, timeout=0.01)
    assert not ok


def test_finished_recovery_is_not_run_again_after_marker_cleanup_failure(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)
    monkeypatch.setattr(desktop, "_verify_new_version_health", lambda *a, **k: (False, "not ready", None))
    calls = []
    original = Path.unlink
    def unlink(path, *a, **k):
        if path.name == desktop._HEALTH_CHECK_MARK:
            calls.append(path)
            if len(calls) == 1:
                raise PermissionError("guard still open")
        return original(path, *a, **k)
    monkeypatch.setattr(Path, "unlink", unlink)
    checks = []
    monkeypatch.setattr(desktop, "_wait_target_idle", lambda *a, **k: checks.append(k) or [])
    desktop._run_update_helper(staging, target, None)
    assert (target / EXE_NAME).read_bytes() == b"old-exe"
    assert len(checks) == 2


def test_recovery_reuses_mutex_already_held_before_launch(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    (staging / APP_NAME / EXE_NAME).unlink()
    monkeypatch.setattr(desktop, "_swap_payload", _ORIGINAL_SWAP_PAYLOAD)
    monkeypatch.setattr(desktop, "_SWAP_MUTEX_HANDLE", 91)
    acquires = []
    monkeypatch.setattr(desktop, "_acquire_swap_mutex", lambda: acquires.append(True) or True)
    monkeypatch.setattr(desktop, "_release_swap_mutex", lambda: None)
    desktop._run_update_helper(staging, target, None)
    assert len(acquires) == 1
    assert (target / EXE_NAME).read_bytes() == b"old-exe"


def test_swap_preserves_existing_nonempty_recovery_backup(tmp_path, monkeypatch):
    install = _make_install(tmp_path / "install")
    staging = tmp_path / "staging"
    _make_payload(staging)
    old = install / "__old__"
    old.mkdir()
    marker = old / "only-copy.txt"
    marker.write_text("keep", encoding="utf-8")
    assert desktop._swap_payload(staging, install) is False
    assert marker.read_text(encoding="utf-8") == "keep"
    assert (install / EXE_NAME).read_bytes() == b"old-exe"


def test_helper_reaps_children_of_previous_instance(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setenv(f"{desktop.ENV_PREFIX}PREV_PID", "12345")
    roots = []
    monkeypatch.setattr(desktop, "_wait_target_idle", lambda *a, **k: roots.append(k.get("root_pid")) or [])
    desktop._run_update_helper(staging, target, None)
    assert roots[0] == 12345


def test_helper_resolves_data_dir_from_target(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    wrong = tmp_path / "wrong-home"
    monkeypatch.setenv(f"{desktop.ENV_PREFIX}DATA_DIR", str(wrong))
    monkeypatch.setattr(desktop, "_data_dir", lambda: Path(os.environ[f"{desktop.ENV_PREFIX}DATA_DIR"]))
    desktop._run_update_helper(staging, target, None)
    # 诊断写入安装目标的 data 目录而非暂存目录的父级
    target_data = target / "data"
    assert (target_data / "logs" / "update-diagnosis.json").is_file()
    assert not (wrong / "logs" / "update-diagnosis.json").exists()
    # 环境变量在调用后必须恢复
    assert os.environ[f"{desktop.ENV_PREFIX}DATA_DIR"] == str(wrong)


def test_helper_uses_explicit_data_dir_argument(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    explicit = tmp_path / "custom-data"
    # 让 _data_dir 读环境变量（匹配真实行为）
    monkeypatch.setattr(
        desktop, "_data_dir",
        lambda: Path(os.environ.get(f"{desktop.ENV_PREFIX}DATA_DIR", str(data))),
    )
    desktop._run_update_helper(staging, target, None, data_dir=explicit)
    assert (explicit / "logs" / "update-diagnosis.json").is_file()
    assert not (target / "data" / "logs" / "update-diagnosis.json").exists()


@pytest.mark.parametrize("reserved_name", ["data", "Data", "LOGS", "__OLD__"])
def test_payload_cannot_replace_user_data(tmp_path, reserved_name):
    target = _make_install(tmp_path / "install")
    staging = tmp_path / "staging"
    payload = _make_payload(staging)
    (payload / reserved_name).mkdir()
    (payload / reserved_name / "untrusted.txt").write_text("bad", encoding="utf-8")
    assert desktop._swap_payload(staging, target) is False
    assert (target / "data" / "holdexar.db").read_bytes() == b"user-data"
    assert (target / EXE_NAME).read_bytes() == b"old-exe"


def test_portable_flag_survives_swap(tmp_path):
    target = _make_install(tmp_path / "install")
    (target / "portable.flag").write_text("", encoding="utf-8")
    staging = tmp_path / "staging"
    _make_payload(staging)
    assert desktop._swap_payload(staging, target) is True
    assert (target / "portable.flag").is_file()


def test_consumed_manifest_lock_does_not_undo_completed_swap(tmp_path, monkeypatch):
    target = _make_install(tmp_path / "install")
    staging = tmp_path / "staging"
    _make_payload(staging)
    _stage_manifest(staging)
    original = Path.unlink
    def unlink(path, *args, **kwargs):
        if path == staging / "manifest.json":
            raise PermissionError("manifest is locked")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(desktop, "_data_dir", lambda: tmp_path / "data")
    assert desktop._swap_payload(staging, target) is True
    assert (target / "__old__" / EXE_NAME).read_bytes() == b"old-exe"


def test_handoff_does_not_remove_active_helper_staging(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_staged_supports_helper", lambda *a: True)
    desktop._health_checking_flag().write_text(
        __import__("json").dumps({"pid": os.getpid(), "attemptId": "active"}), encoding="utf-8"
    )
    assert desktop._handoff_pending_update() is False
    assert (staging / "manifest.json").exists()


def test_helper_keeps_working_directory_outside_consumed_staging(tmp_path, monkeypatch):
    data, staging, target, events = _prepare_failed_helper_case(tmp_path, monkeypatch)
    observed = []
    original_cwd = Path.cwd()
    monkeypatch.setattr(desktop, "_apply_staged_update", lambda *a: observed.append(Path.cwd()))
    desktop._run_update_helper(staging, target, None)
    assert observed == [target.resolve()]
    assert Path.cwd() == original_cwd


def test_loopback_health_does_not_need_helper_certificate_files(monkeypatch):
    class Proc:
        _update_attempt_id = "owned"
        def poll(self):
            return None
    class Response:
        status_code = 200
        def json(self):
            return {"app": desktop.APP_NAME, "version": "9.9.9", "updateAttemptId": "owned"}
    def get(*args, **kwargs):
        if kwargs.get("verify") is not False:
            raise FileNotFoundError("helper certificates already cleaned")
        return Response()
    monkeypatch.setattr(desktop.httpx, "get", get)
    healthy, _, _ = desktop._verify_new_version_health("9.9.9", Proc(), timeout=.01)
    assert healthy


def test_swap_keeps_case_variant_of_user_data(tmp_path):
    target = _make_install(tmp_path / "install")
    (target / "data").rename(target / "Data")
    staging = tmp_path / "staging"
    _make_payload(staging)
    assert desktop._swap_payload(staging, target) is True
    assert (target / "Data" / "holdexar.db").read_bytes() == b"user-data"
