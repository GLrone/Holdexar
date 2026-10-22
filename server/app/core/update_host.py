"""协调程序换装、受保护数据验收与失败恢复。"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import shutil
import socket
import subprocess
import threading
import sys
import tempfile
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
from app.core.paths import data_dir_filename, install_root as resolve_default_install_root

DEFAULT_SERVER_HOST = "127.0.0.1"
DEFAULT_SERVER_PORT = 28765
UPDATE_DATA_PROTOCOL = 1

# 健康验收失败归因（写入事务与诊断报告的 error 字段前缀）
HEALTH_BINDING_ERROR = "健康验收未绑定本事务"
HEALTH_EXIT_ERROR = "新版本进程已退出"
HEALTH_TIMEOUT_ERROR = "健康验收超时"
STARTUP_ERROR = "拉起新版本失败"
PAYLOAD_ERROR = "更新载荷不完整"

# 旧版扁平布局转换时，根目录先备好启动器的位置（不得与既有文件同名）
_RELATIVE_LAUNCHER_DIR = ".launcher"

logger = logging.getLogger(__name__)


def _validate_update_root(root: Path) -> None:
    root = root.resolve()
    owner = root.parent.parent
    if (root.parent.name == layout.VERSIONS_DIR_NAME
            and (owner / layout.STATE_DIR_NAME / layout.CURRENT_STATE_FILE).is_file()):
        raise RuntimeError("更新目标必须是安装目录，不能是其中的版本目录")


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


def _alert(message: str, title: str = APP_NAME) -> None:
    """换装宿主随应用启动、无控制台：关键失败弹系统对话框兜底。"""
    try:
        print(message)
    except Exception:  # noqa: BLE001
        pass
    if os.name != "nt":
        return
    try:
        ctypes.windll.user32.MessageBoxW(0, message, title, 0x10)
    except Exception:  # noqa: BLE001
        pass


def _notify(msg: str) -> None:
    """错误弹窗放后台：MessageBoxW 阻塞到用户点确定，放主流程会把换装卡住。"""
    threading.Thread(target=_alert, args=(f"[更新] {msg}",), daemon=True).start()


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
    """仅在能确认指定进程退出时继续更新。"""
    if os.name != "nt":
        deadline = time.monotonic() + timeout
        while True:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return True
            except OSError:
                return False
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x00100000, False, pid)
        if not handle:
            return kernel32.GetLastError() == 87
        try:
            return kernel32.WaitForSingleObject(handle, int(max(0, timeout) * 1000)) == 0
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return False


def process_image_path(pid: int) -> str | None:
    """进程可执行镜像的完整路径；取不到返回 None。"""
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32),
        ]
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = ctypes.c_uint32(32768)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return buf.value
            return None
        finally:
            kernel32.CloseHandle(handle)
    except Exception:  # noqa: BLE001
        return None


def _safe_replace_executable(src: Path, dest: Path) -> None:
    """安全覆盖可执行文件（针对 Windows 运行中文件锁做重命名保护）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        shutil.copy2(src, dest)
        return
    try:
        shutil.copy2(src, dest)
        return
    except (PermissionError, OSError):
        pass
    suffix = uuid.uuid4().hex[:8]
    old_dest = dest.with_name(f"{dest.stem}.old_{suffix}{dest.suffix}")
    try:
        os.replace(dest, old_dest)
    except OSError:
        pass
    shutil.copy2(src, dest)


def cleanup_old_executables(directory: Path) -> None:
    """清理历史由于运行期重命名产生的 *.old_* 临时文件。"""
    if not directory.is_dir():
        return
    for item in directory.glob("*.old_*"):
        try:
            if item.is_file():
                item.unlink(missing_ok=True)
        except OSError:
            pass


    # 2. 载荷多版本布局时同步刷新根目录启动器与 updater/UpdateHost.exe。
    #    利用 _safe_replace_executable 处理 Windows 运行期 EXE 文件锁。


def install_version_payload(
    root: Path,
    staging_dir: Path,
    target_version: str,
) -> Path:
    """将暂存载荷安装到 versions/<target_version>/。"""
    _validate_update_root(root)
    if not layout.is_managed_version_name(target_version):
        raise RuntimeError(f"目标版本号不可用作目录名：{target_version!r}")
    active = layout.get_current_version(root)
    if active and layout.same_version_directory(root, active, target_version):
        raise RuntimeError("更新载荷不得覆盖正在使用的版本目录")

    payload = staging_dir / APP_NAME
    if not payload.is_dir():
        payload = staging_dir
    if not payload.is_dir():
        raise RuntimeError("更新载荷目录不存在")

    dest = layout.get_version_dir(root, target_version)
    if dest.exists():
        shutil.rmtree(dest, ignore_errors=True)
    dest.parent.mkdir(parents=True, exist_ok=True)

    versions_sub = payload / "versions"

    # 0. 检查是否为文件级差量包（patch-manifest.json）
    patch_file = next(
        (p for p in (payload / "patch-manifest.json", staging_dir / "patch-manifest.json") if p.is_file()),
        None,
    )
    if patch_file is not None:
        from app.core.differential_patch import apply_differential_patch

        patch_manifest = json.loads(patch_file.read_text(encoding="utf-8"))
        from_ver = patch_manifest.get("fromVersion")
        if not from_ver:
            raise RuntimeError("差量更新清单缺少来源版本号（fromVersion）")
        base_ver_dir = layout.get_version_dir(root, from_ver)
        if not base_ver_dir.is_dir() or not layout.is_version_valid(root, from_ver):
            raise RuntimeError(f"差量更新所需的基准版本不存在或损坏：{from_ver}")

        _log_update(root, f"正在应用文件级差量包（从 v{from_ver} 升级至 v{target_version}）...")
        apply_differential_patch(
            patch_source=patch_file.parent,
            base_dir=base_ver_dir,
            target_dest_dir=dest,
        )

    # 1. 载荷自带多版本布局（含 versions/ 子目录）
    elif versions_sub.is_dir():
        src_ver = versions_sub / target_version
        if not src_ver.is_dir():
            # 载荷版本目录名与清单版本不一致即为错包：绝不拿别的目录冒充目标版本
            raise RuntimeError(f"更新载荷的 versions/ 下未找到版本目录：{target_version}")
        shutil.copytree(src_ver, dest, dirs_exist_ok=True)

    else:
        # 传统扁平单版本载荷：直接作为版本目录
        shutil.copytree(payload, dest, dirs_exist_ok=True)

    # 完整性校验：主程序不存在即整次换装失败（不得以残缺版本目录提交）
    if not layout.is_version_valid(root, target_version):
        raise RuntimeError(f"安装到 {dest} 失败：缺少主可执行程序")

    if (versions_sub.is_dir() or patch_file is not None):
        src_launcher = payload / f"{APP_NAME}.exe"
        if src_launcher.is_file():
            _safe_replace_executable(src_launcher, root / f"{APP_NAME}.exe")
        src_updater = payload / "updater" / "UpdateHost.exe"
        if src_updater.is_file():
            (root / "updater").mkdir(parents=True, exist_ok=True)
            _safe_replace_executable(src_updater, root / "updater" / "UpdateHost.exe")


    _log_update(root, f"新版本文件已就绪：{dest}")
    return dest


def _trace(message: str) -> None:
    """验收过程排障：写进宿主日志，失败时能看清卡在哪一步。"""
    try:
        root = os.environ.get(f"{ENV_PREFIX}INSTALL_ROOT", "").strip()
        if root:
            _log_update(Path(root), message)
    except Exception:  # noqa: BLE001
        pass


class _MIB_TCPROW_OWNER_PID(ctypes.Structure):
    _fields_ = [
        ("dwState", ctypes.c_ulong),
        ("dwLocalAddr", ctypes.c_ulong),
        ("dwLocalPort", ctypes.c_ulong),
        ("dwRemoteAddr", ctypes.c_ulong),
        ("dwRemotePort", ctypes.c_ulong),
        ("dwOwningPid", ctypes.c_ulong),
    ]


# 本机监听端口表的唯一取数口：GetExtendedTcpTable（TCP_TABLE_OWNER_PID_LISTENER）。
# 不走 netstat 子进程——宿主被 DETACHED 拉起时没有可继承的控制台句柄，外部命令
# 的管道输出依赖环境，验收不能建在它上面。
_TCP_TABLE_OWNER_PID_LISTENER = 3
_ERROR_INSUFFICIENT_BUFFER = 122


def _listening_table() -> list[tuple[int, int]]:
    """返回 [(监听端口, 属主 PID)]；取不到时返回空表。"""
    if os.name != "nt":
        return []
    try:
        ipproto_tcp = 6
        table = ctypes.windll.iphlpapi.GetExtendedTcpTable
        table.restype = ctypes.c_ulong
        size = ctypes.c_ulong(0)
        rc = table(None, ctypes.byref(size), False, 2, _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if rc not in (0, _ERROR_INSUFFICIENT_BUFFER):
            _trace(f"读取监听端口表失败：{rc}")
            return []
        buf = ctypes.create_string_buffer(size.value)
        rc = table(buf, ctypes.byref(size), False, 2, _TCP_TABLE_OWNER_PID_LISTENER, 0)
        if rc != 0:
            _trace(f"读取监听端口表失败：{rc}（{ipproto_tcp}）")
            return []
        count = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ulong))[0]
        rows = ctypes.cast(
            ctypes.byref(buf, ctypes.sizeof(ctypes.c_ulong)),
            ctypes.POINTER(_MIB_TCPROW_OWNER_PID),
        )
        result: list[tuple[int, int]] = []
        for index in range(count):
            row = rows[index]
            # dwLocalPort 按网络字节序存放（高字节在前）
            raw = int(row.dwLocalPort)
            port = ((raw & 0xFF) << 8) | ((raw >> 8) & 0xFF)
            result.append((port, int(row.dwOwningPid)))
        return result
    except Exception as exc:  # noqa: BLE001
        _trace(f"读取监听端口表异常：{exc}")
        return []


def _listen_ports(pid: int) -> tuple[list[int], int | None]:
    """按进程号查实际监听端口：返回 (端口列表, 端口属主 PID)。

    端口属主取系统报告值而非请求方推断——启动器拉起的是子进程时，监听套接字
    归子进程，argv[0] 的进程不监听任何端口。
    """
    ports = [port for port, owner in _listening_table() if owner == pid]
    return list(dict.fromkeys(ports)), (pid if ports else None)


def _answers_http(port: int, timeout: float = 1.0) -> bool:
    """端口是否真的用 HTTP 应答。

    单实例锁端口同样是本进程的监听端口，但它只接受连接、不回应用层数据——
    对着它发健康请求会一直等到超时，把整段验收时间预算耗光。先按「能否在
    短时间内拿到一个完整响应报文」筛一遍，只对真正的服务端口发起验收请求。
    """
    sock = socket.socket()
    try:
        sock.settimeout(timeout)
        sock.connect((DEFAULT_SERVER_HOST, port))
        sock.sendall(
            f"GET /api/v1/health HTTP/1.1\r\nHost: {DEFAULT_SERVER_HOST}\r\n"
            "Connection: close\r\n\r\n".encode("ascii")
        )
        return b"HTTP/" in sock.recv(64)
    except OSError:
        return False
    finally:
        sock.close()


def _same_path(a: str | Path, b: str | Path) -> bool:
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return str(a).casefold() == str(b).casefold()


def supports_data_protocol(exe: Path) -> bool:
    try:
        with tempfile.TemporaryDirectory(prefix="holdexar-update-protocol-") as tmp:
            result_path = Path(tmp) / "capability.json"
            env = dict(os.environ, HOLDEXAR_UPDATE_PROTOCOL_RESULT=str(result_path))
            result = subprocess.run(
                [str(exe), "--update-protocol", "--help"], env=env,
                capture_output=True, timeout=25,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return result.returncode == 0 and json.loads(result_path.read_text(encoding="utf-8")).get("updateDataProtocol") == UPDATE_DATA_PROTOCOL
    except (OSError, ValueError, AttributeError, subprocess.TimeoutExpired):
        return False


def report_data_protocol(argv: list[str]) -> bool:
    if "--update-protocol" not in argv:
        return False
    payload = {"updateDataProtocol": UPDATE_DATA_PROTOCOL}
    result = os.environ.get(f"{ENV_PREFIX}UPDATE_PROTOCOL_RESULT")
    if result:
        Path(result).write_text(json.dumps(payload), encoding="utf-8")
    else:
        print(json.dumps(payload))
    return True


def verify_health_response(
    data: dict[str, Any],
    *,
    target_version: str,
    attempt_id: str,
    exe: Path,
    data_dir: Path,
    process_image: str | None = None,
    require_guard: bool = False,
) -> str | None:
    """健康响应的身份校验：不匹配返回原因，全部匹配返回 None。

    四项都是「这次换装到底换成了什么」的必要证据，缺一不可：
    ①版本 = 目标版本（同端口的其它实例或旧版本都会露馅）；
    ②事务标识 = 本次 attempt（进程确实由本次换装拉起，不是残留实例）；
    ③数据目录 = 安装根 data/（进程确实读的是用户库，而不是别处的库）；
    ④进程镜像 = 目标版本目录内的主程序（不是别处同名程序）。

    打包态主程序就是验收进程本身；开发态直接跑脚本时 `sys.executable` 是解释器，
    该字段由 `process_image` 传入实际拉起的进程映像，二者比对同一事实。
    """
    if require_guard:
        if any(key not in data for key in ("pid", "dataDir", "executable")):
            return "健康响应缺少程序或数据身份"
        if data.get("updateDataProtocol") != UPDATE_DATA_PROTOCOL or data.get("writeProtected") is not True:
            return "更新期间的数据写入保护未生效"
    version = str(data.get("version") or "").removeprefix("v")
    if version != str(target_version).removeprefix("v"):
        return f"版本不符：响应 {version or '(空)'} / 目标 {target_version}"
    if str(data.get("updateAttemptId") or "") != str(attempt_id):
        return "事务标识不符：响应不是本次换装拉起的进程"
    reported_dir = data.get("dataDir")
    if reported_dir is not None and not _same_path(reported_dir, data_dir):
        return f"数据目录不符：响应 {reported_dir} / 目标 {data_dir}"
    reported_exe = data.get("executable")
    if reported_exe is not None and not _same_path(reported_exe, exe):
        # 直接跑脚本时 executable 是解释器：只要求与本进程映像一致
        if not (process_image and _same_path(reported_exe, process_image)):
            return f"进程镜像不符：响应 {reported_exe} / 目标 {exe}"
    return None


def probe_version_health(
    root: Path,
    target_version: str,
    attempt_id: str,
    timeout: float = HEALTH_TIMEOUT,
    *,
    data_dir: Path | None = None,
    target_exe: Path | None = None,
    require_window: bool = False,
    keep_running: bool = False,
    require_guard: bool | None = None,
) -> tuple[bool, str]:
    """启动新版本进行健康验收：只认本次拉起、目标版本、目标数据目录的进程。"""
    exe = target_exe or layout.get_version_exe(root, target_version)
    if not exe.is_file():
        return False, f"{HEALTH_BINDING_ERROR}：主程序不存在"

    expected_data_dir = (data_dir or (root / "data")).resolve()
    transaction = layout.read_transaction(root) or {}
    protected = bool(transaction.get("dataPrepared") and not transaction.get("dataAccepted")
                     and transaction.get("dataRecoveryPhase") != "restored") if require_guard is None else require_guard
    from app.core.update_process import ProbeTree

    child_env = dict(os.environ)
    child_env[f"{ENV_PREFIX}INSTALL_ROOT"] = str(root)
    child_env[f"{ENV_PREFIX}DATA_DIR"] = str(expected_data_dir)
    child_env[f"{ENV_PREFIX}UPDATE_ATTEMPT_ID"] = attempt_id
    if protected:
        child_env[f"{ENV_PREFIX}UPDATE_DATA_TOKEN"] = str(transaction["dataToken"])
    else:
        child_env.pop(f"{ENV_PREFIX}UPDATE_DATA_TOKEN", None)
        if require_guard is False:
            child_env.pop(f"{ENV_PREFIX}UPDATE_ATTEMPT_ID", None)
    child_env["WEBVIEW2_USER_DATA_FOLDER"] = str(expected_data_dir / "cache" / "webview")
    child_env["NO_PROXY"] = "127.0.0.1,localhost"
    child_env["no_proxy"] = "127.0.0.1,localhost"

    # 验收进程只起服务：端口交给启动逻辑按占用情况现选。预先钉死端口会让
    # 子进程在端口被占时自动漂移，而宿主还在盯原端口，结果把正常启动判成超时。
    child_env.pop(f"{ENV_PREFIX}PORT", None)
    # 验收只看「装上以后能不能起来」，不能继承宿主身上的开发态调试变量：
    # 一旦带上开发服务器地址，子进程会去等一个不存在的 Vite，永远起不来。
    for debug_key in (f"{ENV_PREFIX}DEV_URL", f"{ENV_PREFIX}LAUNCHER_PID",
                      f"{ENV_PREFIX}UPDATE_RESTART", f"{ENV_PREFIX}PREV_PID"):
        child_env.pop(debug_key, None)

    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    proc = None
    tree = ProbeTree()
    preserved = False
    if protected:
        layout.update_transaction(root, layout.STAGE_VERIFYING,
                                  dataProbeRegistrationRequired=True, launchPending=True)
    try:
        # 带 --server：仅起本地服务做健康检查，不打开 WebView2 窗口
        proc = subprocess.Popen(
            [str(exe), *([] if require_window else ["--server"])],
            cwd=str(exe.parent),
            env=child_env,
            creationflags=flags,
        )
        tree.assign(proc)
        if protected:
            children = list((layout.read_transaction(root) or {}).get("children", []))
            children.append({"pid": proc.pid, "image": process_image_path(proc.pid), "stopped": False})
            layout.update_transaction(root, layout.STAGE_VERIFYING, children=children, launchPending=False)
    except Exception as exc:  # noqa: BLE001
        if proc is not None:
            proc.terminate()
            proc.wait(timeout=5)
        tree.close()
        if protected:
            layout.update_transaction(root, layout.STAGE_VERIFYING, launchPending=False)
        return False, f"{STARTUP_ERROR}：{exc}"

    deadline = time.monotonic() + timeout
    last_reason = f"{HEALTH_BINDING_ERROR}：无可用健康响应"
    # 验收进程的映像在拉起后固化：后续只与自身比对，防止中途被替换
    expected_image = process_image_path(proc.pid)

    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                return False, f"{HEALTH_EXIT_ERROR}，退出码 {proc.returncode}"
            ports, owner_pid = _listen_ports(proc.pid)
            if expected_image is not None:
                current_image = process_image_path(proc.pid)
                if current_image and not _same_path(current_image, expected_image):
                    return False, f"{HEALTH_BINDING_ERROR}：进程镜像被替换为 {current_image}"
            _trace(f"验收轮询：pid={proc.pid} 监听端口={ports} 属主={owner_pid}")
            for candidate in ports:
                if not _answers_http(candidate):
                    continue
                url = f"http://{DEFAULT_SERVER_HOST}:{candidate}/api/v1/health"
                try:
                    resp = httpx.get(url, timeout=3.0, trust_env=False)
                except Exception:  # noqa: BLE001
                    continue
                if resp.status_code != 200:
                    continue
                try:
                    data = resp.json()
                except Exception:  # noqa: BLE001
                    continue
                if not isinstance(data, dict) or data.get("app") != APP_NAME:
                    continue
                pid = data.get("pid")
                if pid is not None and int(pid) != int(proc.pid):
                    last_reason = f"{HEALTH_BINDING_ERROR}：端口属主 {owner_pid}≠本次进程 {proc.pid}"
                    continue
                if owner_pid is not None and int(owner_pid) != int(proc.pid):
                    last_reason = f"{HEALTH_BINDING_ERROR}：监听者 {owner_pid}≠本次进程 {proc.pid}"
                    continue
                if require_guard is False and "dataDir" not in data:
                    try:
                        info_response = httpx.get(
                            f"http://{DEFAULT_SERVER_HOST}:{candidate}/api/v1/info",
                            timeout=3.0, trust_env=False,
                        )
                        info = info_response.json() if info_response.status_code == 200 else {}
                        data["dataDir"] = info.get("data_dir")
                    except (OSError, ValueError, httpx.HTTPError):
                        continue
                reason = verify_health_response(
                    data,
                    target_version=target_version,
                    attempt_id=attempt_id,
                    exe=exe,
                    data_dir=expected_data_dir,
                    process_image=expected_image,
                    require_guard=protected,
                )
                if require_guard is False:
                    reason = None if (
                        str(data.get("version", "")).removeprefix("v") == target_version.removeprefix("v")
                        and data.get("dataDir") and _same_path(data["dataDir"], expected_data_dir)
                        and expected_image and _same_path(expected_image, exe)
                    ) else "原版本或数据目录不匹配"
                if reason:
                    last_reason = f"{HEALTH_BINDING_ERROR}：{reason}"
                    continue
                if require_window and data.get("pageReady") is not True:
                    last_reason = "程序服务已启动，等待应用页面完成加载"
                    continue
                if keep_running:
                    if protected:
                        from app.core import update_data

                        update_data.accept(root)
                    tree.detach()
                    preserved = True
                return True, "健康检查通过"
            time.sleep(0.5)
        return False, f"{HEALTH_TIMEOUT_ERROR}（{timeout}s）：{last_reason}"
    finally:
        if proc and not preserved:
            try:
                tree.stop(proc)
                if protected:
                    current = layout.read_transaction(root) or {}
                    children = current.get("children", [])
                    for child in children:
                        if child.get("pid") == proc.pid:
                            child["stopped"] = True
                    layout.update_transaction(root, current.get("stage", layout.STAGE_VERIFYING), children=children)
            finally:
                tree.close()


def _read_manifest(staging_dir: Path) -> dict[str, Any]:
    manifest_file = staging_dir / STAGING_MANIFEST_NAME
    if not manifest_file.is_file():
        return {}
    try:
        data = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    return data if isinstance(data, dict) else {}


def _resolve_data_dir(root: Path, requested: Path | None) -> Path:
    """本次换装的数据目录：显式传入优先，否则安装根 data/。"""
    candidate = Path(requested).resolve() if requested else (root / "data").resolve()
    return candidate


def _spawn_detached(root: Path, exe: Path, argv: list[str] | None = None) -> None:
    """脱离宿主进程拉起目标：宿主退出后窗口不能跟着消失。"""
    command = [str(exe), *(argv or [])]
    env = dict(os.environ)
    for key in ("UPDATE_DATA_TOKEN", "UPDATE_ATTEMPT_ID", "LAUNCHER_PID", "DEV_URL", "UPDATE_RESTART", "PREV_PID"):
        env.pop(f"{ENV_PREFIX}{key}", None)
    env[f"{ENV_PREFIX}INSTALL_ROOT"] = str(root)
    tx = layout.read_transaction(root) or {}
    env[f"{ENV_PREFIX}DATA_DIR"] = str(tx.get("dataDir") or (root / "data"))
    subprocess.Popen(
        command,
        cwd=str(exe.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        env=env,
    )
    _log_update(root, f"已拉起：{exe} {' '.join(argv or [])}".strip())


def _relaunch_previous(root: Path, version: str | None) -> bool:
    """验收或安装失败后，把上一有效版本拉起来，用户不至于面对没有窗口。"""
    target_ver = version or layout.get_current_version(root)
    if not target_ver or not layout.is_version_valid(root, target_ver):
        return False
    exe = layout.get_version_exe(root, target_ver)
    try:
        tx = layout.read_transaction(root) or {}
        healthy, reason = probe_version_health(
            root, target_ver, str(tx.get("attemptId") or "recovery"),
            data_dir=Path(tx.get("dataDir") or root / "data"), require_guard=False,
        )
        layout.update_transaction(root, tx.get("stage", layout.STAGE_ROLLED_BACK), sourceServiceHealthy=healthy)
        if not healthy:
            _log_update(root, f"原程序尚未恢复运行，数据已保留：{reason}")
            return False
        _spawn_detached(root, exe)
        return True
    except Exception as exc:  # noqa: BLE001
        _log_update(root, f"拉起回退版本 {target_ver} 失败：{exc}")
        return False


def _launch_entry(root: Path) -> bool:
    """拉起根目录启动器；启动器缺失时退回版本目录主程序。"""
    launcher_exe = root / f"{APP_NAME}.exe"
    if launcher_exe.is_file():
        _spawn_detached(root, launcher_exe)
        return True
    current = layout.get_current_version(root)
    for candidate in (current, layout.previous_version_entry(root)):
        if not candidate:
            continue
        exe = layout.get_version_exe(root, candidate)
        if exe.is_file():
            _spawn_detached(root, exe)
            return True
    return False


def _launch_gui(root: Path) -> bool:
    """验收通过后打开正式界面：根目录启动器在就交给它，否则直接开目标版本。

    验收进程是带 `--server` 的无窗口实例，不能沿用它——必须新起一个带窗口的。
    """
    launcher_exe = root / f"{APP_NAME}.exe"
    if launcher_exe.is_file():
        _spawn_detached(root, launcher_exe)
        return True
    current = layout.get_current_version(root)
    if current and layout.is_version_valid(root, current):
        _spawn_detached(root, layout.get_version_exe(root, current))
        return True
    return _launch_entry(root)


def _fail(
    root: Path,
    diagnosis: dict[str, Any],
    error: str,
    *,
    launched_version: str | None = None,
    launch_previous: bool = True,
) -> bool:
    """统一失败出口：撤下未验收的目标版本、指针交还、恢复旧版并留痕。"""
    _log_update(root, f"换装故障：{error}，执行回滚...")
    tx = layout.read_transaction(root) or {}
    if tx.get("dataAccepted"):
        diagnosis.update(success=False, error=error, dataPreserved=True, stage="accepted_failure")
        _write_diagnosis(root, diagnosis)
        return False
    if tx.get("dataPrepared"):
        from app.core import update_data

        try:
            update_data.rollback(root, children_stopped=True)
        except Exception as exc:
            error = f"{error}；数据恢复未完成，已保留现场：{exc}"
            layout.update_transaction(root, "recovery_required", error=error)
            diagnosis.update(success=False, error=error, dataPreserved=True, stage="recovery_required")
            _write_diagnosis(root, diagnosis)
            return False
    elif tx.get("dataDir"):
        from app.core import update_data

        try:
            update_data.abort_preparation(root, data_dir=Path(tx["dataDir"]))
        except Exception as exc:
            layout.update_transaction(root, "recovery_required", error=f"恢复点准备未结束：{exc}")
            return False
    target_ver = str(diagnosis.get("targetVersion") or "")
    pointer = launched_version or layout.get_current_version(root)
    if target_ver and pointer == target_ver:
        restored = layout.abort_version(root, target_ver)
        if restored:
            _log_update(root, f"已撤下未通过验收的版本 {target_ver}，指针交还上一有效版本")
        else:
            _log_update(root, f"未能交还版本指针（目标 {target_ver} 无可用回退版本）")
    layout.update_transaction(root, layout.STAGE_ROLLED_BACK, error=error, relaunchPrevious=launch_previous)
    if tx.get("dataPrepared"):
        from app.core import update_data

        try:
            update_data.finish_rollback(root)
        except Exception as exc:
            error = f"{error}；程序与数据恢复尚未完成：{exc}"
            layout.update_transaction(root, "recovery_required", error=error, relaunchPrevious=False)
            diagnosis.update(success=False, error=error, stage="recovery_required", dataPreserved=True)
            _write_diagnosis(root, diagnosis)
            return False
    diagnosis.update({
        "stage": layout.STAGE_ROLLED_BACK,
        "success": False,
        "error": error,
        "endTime": time.time(),
    })
    _write_diagnosis(root, diagnosis)
    return False


def _legacy_version_name(root: Path, source_version: str | None, staging_dir: Path) -> str:
    """旧版真实版本号：显式传入 > 暂存清单记载 > 现存指针 > PE版本/app_info > legacy_version。"""
    for candidate in (source_version, _read_manifest(staging_dir).get("source_version")):
        text = version_from_tag(str(candidate or ""))
        if text and layout.is_managed_version_name(text):
            return text
    current = layout.get_current_version(root)
    if current and layout.is_managed_version_name(current):
        return current
    detected = layout.detect_installed_version(root)
    if detected:
        text = version_from_tag(detected)
        if text and layout.is_managed_version_name(text):
            return text
    return "legacy_version"


def _read_staged_launcher(payload: Path) -> Path | None:
    candidate = payload / f"{APP_NAME}.exe"
    return candidate if candidate.is_file() else None


def prepare_launcher_only(
    root: Path,
    staging_dir: Path,
    source_version: str | None = None,
    attempt_id: str | None = None,
) -> bool:
    """旧式扁平布局第一步：把新启动器放到根目录，确保持续有可双击的程序入口。

    启动器只读 `state/current.json` 指针决定拉起哪个版本；此时指针尚未写入，
    它会按 `previousVersion` 回退到旧版主程序——即「根目录新启动器 + 旧版照常运行」。
    """
    payload = staging_dir / APP_NAME
    if not payload.is_dir():
        payload = staging_dir
    src_launcher = _read_staged_launcher(payload)
    if src_launcher is None:
        _log_update(root, "暂存包未携带根目录启动器，无法提供新版入口")
        return False

    ver = _legacy_version_name(root, source_version, staging_dir)
    staged_launcher = root / _RELATIVE_LAUNCHER_DIR / f"{APP_NAME}.exe"
    staged_launcher.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src_launcher, staged_launcher)

    # 先备好恢复入口侧：旧版主程序此刻仍在根目录，指向它对下次启动完全无副作用
    layout.write_current_state(
        root, ver,
        last_known_good=ver,
        **{
            "previousVersion": ver,
            "pendingVersion": layout.get_current_version(root) or ver,
            "attemptId": attempt_id,
        },
    )

    root_launcher = root / f"{APP_NAME}.exe"
    if root_launcher.is_file():
        backup = root / "__legacy__" / f"{APP_NAME}.exe"
        backup.parent.mkdir(parents=True, exist_ok=True)
        if not backup.exists():
            shutil.copy2(root_launcher, backup)

    os.replace(staged_launcher, root_launcher)
    _log_update(root, f"根目录启动器已就位（回退目标 {ver}）")
    return True


def _move_legacy_entry(root: Path, source: Path, dest: Path) -> None:
    """把旧版条目移到版本目录：同卷改名，跨卷复制后删除源。"""
    if dest.exists():
        if dest.is_dir():
            shutil.rmtree(dest, ignore_errors=True)
        else:
            dest.unlink(missing_ok=True)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, dest)
        return
    except OSError:
        pass
    if source.is_dir():
        shutil.copytree(source, dest, dirs_exist_ok=True)
    else:
        shutil.copy2(source, dest)
    try:
        if source.is_dir():
            shutil.rmtree(source, ignore_errors=True)
        else:
            source.unlink(missing_ok=True)
    except OSError:
        pass


def convert_legacy_layout(
    root: Path,
    source_version: str | None = None,
    attempt_id: str | None = None,
) -> tuple[bool, str]:
    """旧式扁平布局 → 独立版本目录。

    返回 (是否成功, 说明)。任何一步失败都会把已移动的旧版条目搬回原位，
    保证仍有可双击的旧版主程序。
    """
    legacy_ver = _legacy_version_name(root, source_version, root)
    legacy_dir = layout.get_version_dir(root, legacy_ver)
    interrupted = legacy_dir.exists() and any(legacy_dir.iterdir())

    moved: list[tuple[Path, Path]] = []
    try:
        # 1. 搬运 _internal
        root_internal = root / "_internal"
        dest_internal = legacy_dir / "_internal"
        if root_internal.exists():
            if not (interrupted and dest_internal.is_dir() and any(dest_internal.iterdir())):
                _move_legacy_entry(root, root_internal, dest_internal)
                moved.append((dest_internal, root_internal))

        # 2. 搬运旧版主程序：
        #    如果 prepare_launcher_only 已经执行，旧版真实可执行文件在 __legacy__/Holdexar.exe，
        #    根目录的 Holdexar.exe 已经是新 Launcher，绝不能将 Launcher 移入旧版目录！
        legacy_backup = root / "__legacy__" / f"{APP_NAME}.exe"
        root_exe = root / f"{APP_NAME}.exe"
        dest_exe = legacy_dir / f"{APP_NAME}.exe"

        if legacy_backup.is_file():
            _move_legacy_entry(root, legacy_backup, dest_exe)
            moved.append((dest_exe, legacy_backup))
            shutil.rmtree(root / "__legacy__", ignore_errors=True)
        elif root_exe.is_file():
            _move_legacy_entry(root, root_exe, dest_exe)
            moved.append((dest_exe, root_exe))

        if not layout.is_version_valid(root, legacy_ver):
            raise RuntimeError("旧版主程序未完整进入版本目录")
    except Exception as exc:  # noqa: BLE001
        for dest_path, orig_path in reversed(moved):
            try:
                _move_legacy_entry(root, dest_path, orig_path)
            except Exception as back_exc:  # noqa: BLE001
                _log_update(root, f"搬回旧版条目 {dest_path.name} 失败：{back_exc}")
        restorable = (root / f"{APP_NAME}.exe").is_file() or (root / "__legacy__" / f"{APP_NAME}.exe").is_file()
        _log_update(root, f"旧版布局转换失败：{exc}（可启动旧版在位：{restorable}）")
        return False, str(exc)

    layout.write_current_state(
        root, legacy_ver,
        last_known_good=legacy_ver,
        **{
            "previousVersion": legacy_ver,
            "pendingVersion": legacy_ver,
            "attemptId": attempt_id,
        },
    )
    _log_update(root, f"旧版已转换至版本目录：{legacy_dir}")
    return True, ""


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
    from app.core.update_process import update_mutex

    try:
        with update_mutex():
            result, reopen_entry = _bridge_legacy_update_locked(
                staging, target, wait_pid, attempt_id, data_dir, current_version,
                skip_health_check, launch_on_finish,
            )
    except Exception as exc:
        _log_update(target, f"更新未完成，保留数据等待恢复：{exc}")
        return False
    if reopen_entry and launch_on_finish:
        _launch_entry(target)
    return _finish_update_result(target, result)


def _bridge_legacy_update_locked(
    staging: Path, target: Path, wait_pid: int | None, attempt_id: str | None,
    data_dir: Path | None, current_version: str | None,
    skip_health_check: bool, launch_on_finish: bool,
) -> tuple[bool, bool]:
    """保持现用数据目录，将旧安装转换为可验收的多版本布局。"""
    staging, target = staging.resolve(), target.resolve()
    _validate_update_root(target)
    _log_update(target, f"兼容桥接收交接请求：staging={staging} target={target} wait_pid={wait_pid}")

    if wait_pid:
        _log_update(target, f"等待旧版进程 PID {wait_pid} 退出...")
        if not wait_process_exit(wait_pid, timeout=15.0):
            _log_update(target, "原程序尚未退出，停止更新以保护数据")
            return False, False

    if (layout.read_transaction(target) or {}).get("dataDir"):
        recover_interrupted_update(target)

    manifest = _read_manifest(staging)
    active_version = layout.get_current_version(target)
    requested_source = current_version or manifest.get("source_version") or manifest.get("previousVersion")
    requested_target = version_from_tag(str(manifest.get("tag") or ""))
    if active_version and (
        (requested_source and requested_source != active_version)
        or (requested_target and layout.same_version_directory(target, requested_target, active_version))
    ):
        _log_update(target, "更新交接与当前程序不一致，保留现有程序与数据")
        return False, False
    aid = attempt_id or manifest.get("attempt_id") or str(uuid.uuid4())
    db_name = data_dir_filename()
    actual_data_dir = _resolve_data_dir(target, data_dir)
    if not (actual_data_dir / db_name).is_file() and (staging.parent / db_name).is_file():
        actual_data_dir = staging.parent.resolve()

    flat_layout = (target / "_internal").is_dir()
    if not flat_layout and active_version:
        result = _execute_update_locked(
            target, staging, None, requested_source, None, aid,
            skip_health_check, launch_on_finish, actual_data_dir,
        )
        return result, False

    # 1. 先修入口：根目录启动器就该落在调用方给的 target（用户真正的安装根）
    if not prepare_launcher_only(
        target, staging, source_version=current_version, attempt_id=aid,
    ):
        _log_update(target, "新版入口未能就位，终止换装（旧版仍在根目录）")
        _restore_entry_only(target, launch=False)
        return False, True
    if not flat_layout:
        _log_update(target, "根目录已是新启动器（旧版扁平布局已转换），从入口继续换装")
    # 3. 扁平布局转换为版本目录（失败已自行搬回，根目录仍是旧版）
    if flat_layout:
        ok, reason = convert_legacy_layout(
            target,
            source_version=current_version,
            attempt_id=aid,
        )
        if not ok:
            _log_update(target, f"旧版布局转换失败，终止换装：{reason}")
            _restore_entry_only(target, launch=False)
            return False, True

    # 4. 调用更新宿主标准换装
    result = _execute_update_locked(
        target, staging, None, None, None, aid,
        skip_health_check, launch_on_finish, actual_data_dir,
    )
    return result, False


def _restore_entry_only(root: Path, *, launch: bool = True) -> None:
    """必要时恢复旧入口，并在许可允许时重新打开。"""
    installed = layout.list_installed_versions(root)
    legacy_backup = root / "__legacy__" / f"{APP_NAME}.exe"
    if not installed and legacy_backup.is_file():
        try:
            root_exe = root / f"{APP_NAME}.exe"
            shutil.copy2(legacy_backup, root_exe)
            _log_update(root, "已还原旧版主程序至根目录")
        except Exception as exc:  # noqa: BLE001
            _log_update(root, f"还原旧版主程序失败：{exc}")

    if not launch or not _entry_available(root):
        return
    try:
        _launch_entry(root)
    except Exception as exc:  # noqa: BLE001
        _log_update(root, f"拉起当前版本失败：{exc}")


def _entry_available(root: Path) -> bool:
    if (root / f"{APP_NAME}.exe").is_file():
        return True
    return any(
        layout.get_version_exe(root, ver).is_file()
        for ver in layout.list_installed_versions(root)
    )


def execute_update(
    root: Path,
    staging_dir: Path,
    target_version: str | None = None,
    source_version: str | None = None,
    wait_pid: int | None = None,
    attempt_id: str | None = None,
    skip_health_check: bool = False,
    launch_on_finish: bool = True,
    data_dir: Path | None = None,
) -> bool:
    from app.core.update_process import update_mutex

    try:
        with update_mutex():
            _validate_update_root(root)
            result = _execute_update_locked(
                root, staging_dir, target_version, source_version, wait_pid,
                attempt_id, skip_health_check, launch_on_finish, data_dir,
            )
    except Exception as exc:
        _log_update(root, f"更新未完成，保留数据等待恢复：{exc}")
        return False
    return _finish_update_result(root, result)


def _finish_update_result(root: Path, result: bool) -> bool:
    tx = layout.read_transaction(root) or {}
    if not result and tx.get("relaunchPrevious"):
        layout.update_transaction(root, tx["stage"], relaunchPrevious=False)
        _relaunch_previous(root, tx.get("sourceVersion"))
    return result


def recover_interrupted_update(root: Path) -> bool:
    from app.core import update_data
    from app.core.update_process import update_mutex

    with update_mutex():
        tx = layout.read_transaction(root) or {}
        if tx.get("dataRollbackReleased"):
            update_data.finish_rollback(root)
            if layout.get_current_version(root) != tx.get("sourceVersion"):
                raise RuntimeError("已恢复的数据与当前程序指向不一致，已停止启动并保留数据")
            return True
        if tx.get("dataAccepted"):
            update_data.accept(root)
            from app.core.launcher import find_target_version

            compatible = find_target_version(root)
            if not compatible:
                raise RuntimeError("缺少与当前数据相容的程序，数据已保留；请重新解压已验收版本")
            layout.confirm_version(root, compatible)
            layout.update_transaction(root, layout.STAGE_COMMITTED, currentVersion=compatible)
            return True
        if tx.get("dataPrepared"):
            diagnosis = {
                "attemptId": tx["attemptId"], "targetVersion": tx["targetVersion"],
                "sourceVersion": tx["sourceVersion"], "dataDir": tx["dataDir"],
            }
            _fail(root, diagnosis, "上次更新中断，恢复原程序与数据", launch_previous=False)
            current = layout.read_transaction(root) or {}
            if (current.get("dataRollbackReleased") is not True
                    or current.get("stage") != layout.STAGE_ROLLED_BACK
                    or layout.get_current_version(root) != current.get("sourceVersion")
                    or update_data._marker_path(Path(current["dataDir"])).exists()):
                raise RuntimeError("更新恢复尚未完成，原始快照与故障数据均已保留，请查看更新诊断")
        elif tx.get("dataDir"):
            update_data.abort_preparation(root, data_dir=Path(tx["dataDir"]))
        return True


def _execute_update_locked(
    root: Path, staging_dir: Path, target_version: str | None,
    source_version: str | None, wait_pid: int | None, attempt_id: str | None,
    skip_health_check: bool, launch_on_finish: bool, data_dir: Path | None,
) -> bool:
    """执行换装事务。"""
    _log_update(root, f"开始换装：staging={staging_dir} root={root}")

    if wait_pid:
        _log_update(root, f"等待旧实例（PID {wait_pid}）退出...")
        if not wait_process_exit(wait_pid, timeout=15.0):
            _log_update(root, "原程序尚未退出，停止更新以保护数据")
            return False

    manifest_data = _read_manifest(staging_dir)
    aid = attempt_id or manifest_data.get("attempt_id") or str(uuid.uuid4())
    tag = manifest_data.get("tag") or ""
    t_ver = target_version or version_from_tag(tag) or tag
    if not t_ver:
        _log_update(root, "错误：无法识别目标版本")
        return False
    if not layout.is_managed_version_name(t_ver):
        _log_update(root, f"错误：目标版本号不可用作目录名：{t_ver!r}")
        return False

    previous_tx = layout.read_transaction(root) or {}
    if previous_tx.get("dataDir"):
        recover_interrupted_update(root)
        if (not previous_tx.get("dataAccepted") and not previous_tx.get("dataRollbackReleased")
                and previous_tx.get("stage") != layout.STAGE_ROLLED_BACK):
            current_tx = layout.read_transaction(root) or {}
            layout.update_transaction(root, current_tx.get("stage", layout.STAGE_ROLLED_BACK), relaunchPrevious=True)
            return False
    curr_state = layout.read_current_state(root)
    staging_meta: dict[str, Any] = {}
    if (staging_dir / "manifest.json").is_file():
        try:
            staging_meta = json.loads((staging_dir / "manifest.json").read_text(encoding="utf-8"))
        except Exception:
            pass

    notes = str(staging_meta.get("notes") or "")
    staging_source = staging_meta.get("source_version") or staging_meta.get("previousVersion")
    s_ver = (
        source_version
        or staging_source
        or (curr_state.get("version") if curr_state else None)
        or (curr_state.get("lastKnownGood") if curr_state else None)
        or "legacy_version"
    )
    expected_data_dir = _resolve_data_dir(root, data_dir)
    pending_self = curr_state.get("pendingVersion") if curr_state else None
    recovering = pending_self is not None and str(pending_self) == t_ver

    active_version = curr_state.get("version") if curr_state else None
    if active_version and (layout.same_version_directory(root, t_ver, active_version)
                           or s_ver != active_version):
        _log_update(root, "更新请求与当前程序版本不一致或目标已安装，保留现有程序与数据")
        return False

    layout.start_transaction(root, t_ver, s_ver, attempt_id=aid)
    diagnosis: dict[str, Any] = {
        "attemptId": aid,
        "targetVersion": t_ver,
        "sourceVersion": s_ver,
        "previousVersion": s_ver,
        "currentVersion": s_ver,
        "notes": notes,
        "dataDir": str(expected_data_dir),
        "startTime": time.time(),
        "stage": layout.STAGE_SWITCHING,
        "success": False,
        "error": None,
    }
    _write_diagnosis(root, diagnosis)

    layout.update_transaction(root, layout.STAGE_SWITCHING, dataDir=str(expected_data_dir))
    if recovering:
        # 上次换装装完文件却没来得及提交：指针已在本版本上，回退目标已落盘
        _log_update(root, f"检测到未完成的换装（版本 {t_ver}），直接沿用已落盘文件重新验收")
    else:
        try:
            install_version_payload(root, staging_dir, t_ver)
        except Exception as exc:  # noqa: BLE001
            return _fail(root, diagnosis, f"{PAYLOAD_ERROR}：{exc}")

    from app.core import update_data

    db_exists = update_data._db_path(expected_data_dir).is_file()
    if skip_health_check and db_exists:
        return _fail(root, diagnosis, "已有用户数据时不能跳过安全验收")
    if not skip_health_check:
        if not supports_data_protocol(layout.get_version_exe(root, t_ver)):
            return _fail(root, diagnosis, "目标程序缺少数据保护协议，原数据未改动")
        try:
            from app.core import update_data

            update_data.prepare(root, expected_data_dir, aid)
        except Exception as exc:
            return _fail(root, diagnosis, f"无法准备升级恢复点：{exc}")

    layout.write_current_state(
        root, t_ver,
        last_known_good=s_ver,
        **{
            "previousVersion": s_ver,
            "pendingVersion": t_ver,
            "attemptId": aid,
        },
    )
    layout.update_transaction(root, layout.STAGE_VERIFYING)

    if skip_health_check:
        layout.confirm_version(root, t_ver)
        return _commit(root, staging_dir, t_ver, diagnosis, launch_on_finish)

    healthy, reason = probe_version_health(
        root, t_ver, attempt_id=aid, data_dir=expected_data_dir,
    )
    if not healthy:
        return _fail(root, diagnosis, f"健康检查失败：{reason}", launched_version=t_ver)

    return _commit(root, staging_dir, t_ver, diagnosis, launch_on_finish)


def _commit(
    root: Path,
    staging_dir: Path,
    target_version: str,
    diagnosis: dict[str, Any],
    launch_on_finish: bool,
) -> bool:
    """正式页面验收及接受完成后提交程序指针，再清理暂存。"""
    transaction = layout.read_transaction(root) or {}
    protected = transaction.get("dataPrepared", False)
    launched = not protected
    if protected:
        try:
            launched, reason = probe_version_health(
                root, target_version, diagnosis["attemptId"],
                data_dir=Path(diagnosis["dataDir"]), require_window=True, keep_running=launch_on_finish,
            )
            if not launched:
                return _fail(root, diagnosis, f"页面验收失败：{reason}")
            if not launch_on_finish:
                from app.core import update_data

                update_data.accept(root)
        except Exception as exc:
            return _fail(root, diagnosis, f"页面验收未完成：{exc}")
    elif launch_on_finish:
        _log_update(root, "健康验收通过，拉起正式界面")
        try:
            launched = _launch_gui(root)
        except Exception as exc:  # noqa: BLE001
            _log_update(root, f"拉起正式界面失败：{exc}")
            launched = False
    if not launched:
        return _fail(root, diagnosis, f"{STARTUP_ERROR}：新版未能拉起")

    layout.confirm_version(root, target_version)
    try:
        running_from_staging = Path(sys.executable).resolve().is_relative_to(staging_dir.resolve())
    except OSError:
        running_from_staging = True
    if running_from_staging:
        (staging_dir / STAGING_MANIFEST_NAME).unlink(missing_ok=True)
    else:
        shutil.rmtree(staging_dir, ignore_errors=True)
    cleanup_old_executables(root / "updater")
    cleanup_old_executables(root)
    layout.update_transaction(root, layout.STAGE_COMMITTED, currentVersion=target_version)
    pruned = layout.prune_versions(root, min_keep=2)
    _log_update(root, f"换装成功！清理历史版本：{pruned}")

    diagnosis.update({
        "stage": layout.STAGE_COMMITTED,
        "currentVersion": target_version,
        "success": True,
        "endTime": time.time(),
        "acknowledged": False,
    })
    _write_diagnosis(root, diagnosis)
    return True


def main(argv: list[str] | None = None) -> None:
    if report_data_protocol(argv if argv is not None else sys.argv[1:]):
        return
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 独立更新宿主")
    parser.add_argument("--staging", required=True, help="更新载荷暂存目录")
    parser.add_argument("--target", default=None, help="安装根目录")
    parser.add_argument("--target-version", default=None, help="目标版本号")
    parser.add_argument("--source-version", default=None, help="源版本号")
    parser.add_argument("--wait-pid", type=int, default=None, help="等待退出的进程 PID")
    parser.add_argument("--attempt-id", default=None, help="更新尝试唯一 ID")
    parser.add_argument("--data-dir", default=None, help="用户数据目录")
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
        data_dir=Path(args.data_dir).resolve() if args.data_dir else None,
    )
    if not ok:
        _alert(
            f"[更新] 本次更新未完成。数据恢复状态与处理方法见诊断说明。\n\n"
            f"详情见 {root / 'logs' / 'update-diagnosis.json'}"
        )
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
