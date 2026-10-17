"""Holdexar 独立更新宿主（UpdateHost）核心逻辑。

职责：
1. 等待原有应用进程与相关句柄彻底退出；
2. 将更新载荷就地安装至 versions/<target_version>/，验证完整性；
3. 同步刷新 updater/ 与根目录 Launcher（若更新包携带）；
4. 原子切换 state/current.json 版本指针；
5. 静默健康验收：
   - 验收通过：提交事务，清理暂存，按保留策略（当前版 + 上一版）清理更老历史版本，拉起新版；
   - 验收失败：自动回退 state/current.json 到上一有效版本，恢复旧版运行并写入诊断。
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from app.core import layout
from app.core.app_info import (
    APP_NAME,
    ENV_PREFIX,
    HEALTH_TIMEOUT,
    STAGING_MANIFEST_NAME,
    version_from_tag,
)
from app.core.paths import install_root as resolve_default_install_root

DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 28765

logger = logging.getLogger(__name__)


def _log_update(root: Path, message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(f"[更新宿主] {line}")
    log_file = root / "logs" / "update.log"
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        with log_file.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


def _write_diagnosis(root: Path, record: dict[str, Any]) -> None:
    try:
        diag_file = root / "logs" / "update-diagnosis.json"
        diag_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = diag_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(diag_file)
    except Exception as exc:  # noqa: BLE001
        _log_update(root, f"写入诊断报告失败：{exc}")


def wait_process_exit(pid: int, timeout: float = 10.0) -> bool:
    """等待指定 PID 进程退出。"""
    if os.name != "nt":
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return True
        try:
            res = kernel32.WaitForSingleObject(handle, int(timeout * 1000))
            return res == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return True


def install_version_payload(
    root: Path,
    staging_dir: Path,
    target_version: str,
) -> Path:
    """将暂存载荷安装到 versions/<target_version>/。"""
    payload = staging_dir / APP_NAME
    if not payload.is_dir():
        payload = staging_dir

    dest = layout.get_version_dir(root, target_version)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    # 1. 载荷自带多版本布局（含 versions/ 子目录）
    versions_sub = payload / "versions"
    if versions_sub.is_dir():
        src_ver = versions_sub / target_version
        if not src_ver.is_dir():
            candidates = [p for p in versions_sub.iterdir() if p.is_dir()]
            if candidates:
                src_ver = candidates[0]
        if src_ver.is_dir():
            shutil.copytree(src_ver, dest, dirs_exist_ok=True)
        else:
            raise RuntimeError(f"更新载荷的 versions/ 下未找到版本目录：{target_version}")

        # 同步刷新根目录 Launcher
        src_launcher = payload / f"{APP_NAME}.exe"
        if src_launcher.is_file():
            shutil.copy2(src_launcher, root / f"{APP_NAME}.exe")

        # 同步刷新 updater/UpdateHost.exe
        src_updater = payload / "updater" / "UpdateHost.exe"
        if src_updater.is_file():
            (root / "updater").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_updater, root / "updater" / "UpdateHost.exe")
    else:
        # 传统扁平单版本载荷：直接作为版本目录
        shutil.copytree(payload, dest, dirs_exist_ok=True)

    # 完整性校验
    if not layout.is_version_valid(root, target_version):
        raise RuntimeError(f"安装到 {dest} 失败：缺少主可执行程序")

    _log_update(root, f"新版本文件已就绪：{dest}")
    return dest


def probe_version_health(
    root: Path,
    target_version: str,
    attempt_id: str,
    timeout: float = HEALTH_TIMEOUT,
) -> tuple[bool, str]:
    """启动新版本进行健康验收。"""
    exe = layout.get_version_exe(root, target_version)
    if not exe.is_file():
        return False, "主程序不存在"

    port = int(os.environ.get(f"{ENV_PREFIX}PORT", DEFAULT_SERVER_PORT))

    child_env = dict(os.environ)
    child_env[f"{ENV_PREFIX}INSTALL_ROOT"] = str(root)
    child_env[f"{ENV_PREFIX}DATA_DIR"] = str(root / "data")
    child_env[f"{ENV_PREFIX}UPDATE_ATTEMPT_ID"] = attempt_id
    child_env[f"{ENV_PREFIX}PORT"] = str(port)
    child_env["WEBVIEW2_USER_DATA_FOLDER"] = str(root / "data" / "cache" / "webview")
    child_env["NO_PROXY"] = "127.0.0.1,localhost"
    child_env["no_proxy"] = "127.0.0.1,localhost"

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = None
    try:
        proc = subprocess.Popen(
            [str(exe), "--server"],
            cwd=str(exe.parent),
            env=child_env,
            creationflags=flags,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"拉起新版本进程失败：{exc}"

    deadline = time.monotonic() + timeout
    health_url = f"http://{DEFAULT_SERVER_HOST}:{port}/api/v1/health"

    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False, f"新版本启动后异常退出，退出码：{proc.returncode}"
            try:
                resp = httpx.get(health_url, timeout=1.0, trust_env=False)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("app") == APP_NAME:
                        return True, "健康检查通过"
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.5)
        return False, f"健康检查超时（{timeout}s）"
    finally:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3.0)
            except Exception:  # noqa: BLE001
                try:
                    proc.kill()
                except Exception:  # noqa: BLE001
                    pass


def execute_update(
    root: Path,
    staging_dir: Path,
    target_version: str | None = None,
    source_version: str | None = None,
    wait_pid: int | None = None,
    attempt_id: str | None = None,
    skip_health_check: bool = False,
    launch_on_finish: bool = True,
) -> bool:
    """执行换装事务。"""
    _log_update(root, f"开始换装：staging={staging_dir} root={root}")

    if wait_pid:
        _log_update(root, f"等待旧实例（PID {wait_pid}）退出...")
        wait_process_exit(wait_pid, timeout=15.0)

    # 读取暂存清单
    manifest_file = staging_dir / STAGING_MANIFEST_NAME
    manifest_data: dict[str, Any] = {}
    if manifest_file.is_file():
        try:
            manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    aid = attempt_id or manifest_data.get("attempt_id") or str(uuid.uuid4())
    tag = manifest_data.get("tag") or ""
    t_ver = target_version or version_from_tag(tag) or tag
    if not t_ver:
        _log_update(root, "错误：无法识别目标版本")
        return False

    curr_state = layout.read_current_state(root)
    s_ver = source_version or (curr_state.get("version") if curr_state else None) or "legacy"

    # 开启事务
    layout.start_transaction(root, t_ver, s_ver, attempt_id=aid)
    diagnosis: dict[str, Any] = {
        "attemptId": aid,
        "targetVersion": t_ver,
        "sourceVersion": s_ver,
        "startTime": time.time(),
        "stage": layout.STAGE_SWITCHING,
        "success": False,
        "error": None,
    }
    _write_diagnosis(root, diagnosis)

    try:
        # 1. 解包与就地安装
        layout.update_transaction(root, layout.STAGE_SWITCHING)
        install_version_payload(root, staging_dir, t_ver)

        # 2. 原子切换版本指针
        layout.write_current_state(root, t_ver, last_known_good=s_ver)
        layout.update_transaction(root, layout.STAGE_VERIFYING)

        # 3. 健康检查
        healthy = True
        reason = "跳过健康检查"
        if not skip_health_check:
            healthy, reason = probe_version_health(root, t_ver, attempt_id=aid)

        if not healthy:
            raise RuntimeError(f"健康检查失败：{reason}")

        # 4. 成功：提交事务，清理暂存与历史版本
        layout.update_transaction(root, layout.STAGE_COMMITTED)
        shutil.rmtree(staging_dir, ignore_errors=True)
        pruned = layout.prune_versions(root, min_keep=2)
        _log_update(root, f"换装成功！清理历史版本：{pruned}")

        diagnosis.update({
            "stage": layout.STAGE_COMMITTED,
            "success": True,
            "endTime": time.time(),
        })
        _write_diagnosis(root, diagnosis)

        # 拉起新版本
        if launch_on_finish:
            launcher_exe = root / f"{APP_NAME}.exe"
            if launcher_exe.is_file():
                subprocess.Popen([str(launcher_exe)], cwd=str(root))
            else:
                layout_exe = layout.get_version_exe(root, t_ver)
                subprocess.Popen([str(layout_exe)], cwd=str(layout_exe.parent))

        return True

    except Exception as exc:  # noqa: BLE001
        _log_update(root, f"换装故障：{exc}，执行自动回滚...")
        # 回滚版本指针
        if s_ver and s_ver != "legacy" and layout.is_version_valid(root, s_ver):
            layout.write_current_state(root, s_ver, last_known_good=None)
            _log_update(root, f"已安全回滚指针到上一版本：{s_ver}")
            if launch_on_finish:
                old_exe = layout.get_version_exe(root, s_ver)
                if old_exe.is_file():
                    subprocess.Popen([str(old_exe)], cwd=str(old_exe.parent))

        layout.update_transaction(root, layout.STAGE_ROLLED_BACK, error=str(exc))
        diagnosis.update({
            "stage": layout.STAGE_ROLLED_BACK,
            "success": False,
            "error": str(exc),
            "endTime": time.time(),
        })
        _write_diagnosis(root, diagnosis)
        return False


def bridge_legacy_update(
    staging: Path,
    target: Path,
    wait_pid: int | None = None,
    attempt_id: str | None = None,
    data_dir: Path | None = None,
    current_version: str | None = None,
    skip_health_check: bool = False,
    launch_on_finish: bool = True,
) -> bool:
    """旧版交接协议兼容桥：将老式扁平布局无缝转换为新架构（versions/ + state/ + Launcher）。"""
    staging, target = staging.resolve(), target.resolve()
    _log_update(target, f"兼容桥接收交接请求：staging={staging} target={target} wait_pid={wait_pid}")

    if wait_pid:
        _log_update(target, f"等待旧版进程 PID {wait_pid} 退出...")
        wait_process_exit(wait_pid, timeout=15.0)

    # 1. 解析真实数据目录
    from app.core.paths import data_dir_filename

    db_name = data_dir_filename()

    actual_data_dir = data_dir
    if actual_data_dir is None:
        if (staging.parent / db_name).is_file():
            actual_data_dir = staging.parent
        elif (target / "data" / db_name).is_file():
            actual_data_dir = target / "data"
        else:
            actual_data_dir = target / "data"

    actual_data_dir = actual_data_dir.resolve()
    target_data_dir = (target / "data").resolve()

    # 2. 若数据在外部（如 AppData），执行安全迁移到 target/data/
    if actual_data_dir != target_data_dir and (actual_data_dir / db_name).is_file():
        if not (target_data_dir / db_name).is_file():
            _log_update(target, f"检测到旧版外部数据：{actual_data_dir}，安全迁移到 {target_data_dir}")
            try:
                from app.core.migration import migrate_appdata_to_portable

                migrate_appdata_to_portable(actual_data_dir, target_data_dir)
            except Exception as exc:  # noqa: BLE001
                _log_update(target, f"数据迁移出现异常：{exc}")

    # 3. 读取暂存清单确认目标版本
    manifest_file = staging / STAGING_MANIFEST_NAME
    manifest_data: dict[str, Any] = {}
    if manifest_file.is_file():
        try:
            manifest_data = json.loads(manifest_file.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass

    tag = manifest_data.get("tag") or ""
    target_ver = version_from_tag(tag) or tag or "new_version"
    cur_ver = current_version or manifest_data.get("source_version") or "legacy_version"

    # 4. 检查是否为旧式扁平布局（target 根下直接有 _internal）
    old_internal = target / "_internal"
    if old_internal.is_dir():
        _log_update(target, "检测到旧版扁平安装布局，开始转换为独立版本目录...")
        legacy_ver_dir = layout.get_version_dir(target, cur_ver)
        legacy_ver_dir.mkdir(parents=True, exist_ok=True)

        # 仅将旧版主程序与 _internal 依赖目录迁移至 versions/<cur_ver>/
        # 绝不移动 root 下的 portable.flag、标记文件与数据
        for item_name in ["_internal", f"{APP_NAME}.exe"]:
            item = target / item_name
            if item.exists():
                dst = legacy_ver_dir / item_name
                try:
                    if dst.exists():
                        if dst.is_dir():
                            shutil.rmtree(dst, ignore_errors=True)
                        else:
                            dst.unlink(missing_ok=True)
                    shutil.move(str(item), str(dst))
                except Exception as exc:  # noqa: BLE001
                    _log_update(target, f"迁移旧版文件 {item.name} 失败：{exc}")

        # 写入初始版本指针为旧版本
        layout.write_current_state(target, cur_ver)

    # 5. 调用 UpdateHost 标准换装
    return execute_update(
        root=target,
        staging_dir=staging,
        target_version=target_ver,
        source_version=cur_ver,
        wait_pid=None,
        attempt_id=attempt_id,
        skip_health_check=skip_health_check,
        launch_on_finish=launch_on_finish,
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 独立更新宿主")
    parser.add_argument("--staging", required=True, help="更新载荷暂存目录")
    parser.add_argument("--target", default=None, help="安装根目录")
    parser.add_argument("--target-version", default=None, help="目标版本号")
    parser.add_argument("--source-version", default=None, help="源版本号")
    parser.add_argument("--wait-pid", type=int, default=None, help="等待退出的进程 PID")
    parser.add_argument("--attempt-id", default=None, help="更新尝试唯一 ID")
    parser.add_argument("--skip-health-check", action="store_true", help="跳过健康检查")
    parser.add_argument("--no-launch", action="store_true", help="完成后不自动拉起")

    args = parser.parse_args(argv)
    root = Path(args.target).resolve() if args.target else resolve_default_install_root()
    staging = Path(args.staging).resolve()

    ok = execute_update(
        root=root,
        staging_dir=staging,
        target_version=args.target_version,
        source_version=args.source_version,
        wait_pid=args.wait_pid,
        attempt_id=args.attempt_id,
        skip_health_check=args.skip_health_check,
        launch_on_finish=not args.no_launch,
    )
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
