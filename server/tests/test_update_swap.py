"""桌面更新交接、暂存保护与入口参数测试。"""
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
    updater = payload / "updater" / "UpdateHost.exe"
    updater.parent.mkdir()
    updater.write_bytes(b"update-host")
    return payload


def _stage_manifest(staging: Path, tag: str = "v9.9.9", ready: bool = True) -> None:
    """默认 tag 必须比现装（app_info.APP_VERSION，0.3.1-beta）新：版本门只放行更新。"""
    import json

    staging.mkdir(parents=True, exist_ok=True)
    (staging / "manifest.json").write_text(
        json.dumps({"tag": tag, "sha256": "x" * 64, "ready": ready}), encoding="utf-8"
    )


# ── 换装主体（_swap_payload）───────────────────────────────────────────


# 更新交接与暂存保护


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
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda _exe: False)
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
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda _exe: True)
    monkeypatch.setattr(desktop.os, "getpid", lambda: 4242)
    monkeypatch.setattr(
        desktop.subprocess, "Popen", lambda cmd, **kwargs: calls.append((cmd, kwargs))
    )

    assert desktop._handoff_pending_update() is True

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert cmd[0] == str(staging / APP_NAME / "updater" / "UpdateHost.exe")
    assert "--apply-update" not in cmd
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
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda _exe: True)
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
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda _exe: True)
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
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda _exe: True)

    def _boom_popen(*args, **kwargs):
        raise OSError("模拟系统拒绝创建进程")

    monkeypatch.setattr(desktop.subprocess, "Popen", _boom_popen)

    assert desktop._handoff_pending_update() is False
    assert not (data_dir / desktop.HANDOFF_LEDGER_NAME).exists()
    # 暂存包依然完好
    assert (staging / "manifest.json").exists()


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


def test_handoff_does_not_remove_active_helper_staging(tmp_path, monkeypatch):
    data = tmp_path / "data"
    staging = data / "update-staging"
    _make_payload(staging)
    _stage_manifest(staging)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data)
    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda *a: True)
    desktop._health_checking_flag().write_text(
        __import__("json").dumps({"pid": os.getpid(), "attemptId": "active"}), encoding="utf-8"
    )
    assert desktop._handoff_pending_update() is False
    assert (staging / "manifest.json").exists()


def test_desktop_entry_rejects_retired_update_switch(monkeypatch):
    entry = "main.py"
    spec = importlib.util.spec_from_file_location("retired_update_entry", ROOT / "desktop" / entry)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(sys, "argv", ["Holdexar.exe", "--apply-update"])
    with pytest.raises(SystemExit) as result:
        module.main()
    assert result.value.code == 2


def test_handoff_preserves_install_when_update_host_is_missing(tmp_path, monkeypatch):
    data = tmp_path / "data"
    staging = data / "update-staging"
    _make_payload(staging)
    (staging / APP_NAME / "updater" / "UpdateHost.exe").unlink()
    _stage_manifest(staging)
    install = _make_install(tmp_path / "install")
    calls = []
    monkeypatch.setattr(desktop, "is_frozen", lambda: True)
    monkeypatch.setattr(desktop, "_data_dir", lambda: data)
    monkeypatch.setattr(desktop, "_app_root", lambda: install)
    monkeypatch.setattr(desktop, "_alert", lambda _message: None)
    monkeypatch.setattr("app.core.update_host.supports_data_protocol", lambda _exe: True)
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *args, **kwargs: calls.append(args))

    assert desktop._handoff_pending_update() is False
    assert calls == []
    assert (install / EXE_NAME).read_bytes() == b"old-exe"
    assert (staging / "manifest.json").is_file()


