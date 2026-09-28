"""Holdexar 桌面启动器。

流程：单实例锁（被占先探活辨真伪）→ 后台线程 uvicorn → pywebview 窗口首载内联等待页（零网络依赖）→ health 就绪后整窗跳转真实应用（失败降级系统浏览器）。

用法：
    python desktop/main.py                # 桌面窗口模式
    python desktop/main.py --server       # 无窗口（仅本地服务）
    HOLDEXAR_DEV_URL=http://localhost:8080 python desktop/main.py    # 加载 Vite 开发服务器
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import httpx


def _ensure_stdio() -> None:
    """补齐缺失的标准句柄（windowed 打包 / pythonw 下 sys.stdout/stderr 为 None）。

    uvicorn 日志格式器初始化即调 sys.stdout.isatty()，None 下抛错、服务
    起不来；换成 devnull 流后 print/flush/isatty/StreamHandler 全部安全。
    控制台模式两流本就存在，不受影响。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8", errors="replace"))


_ensure_stdio()

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SERVER_ROOT = PROJECT_ROOT / "server"

# 品牌常量从服务端 app_info 收口：
# 开发态 → sys.path 注入 server/；打包态 → app 包在 PYZ（collect_submodules 收集），
# 另把 _MEIPASS/server/app 源码落盘兜底插进 sys.path（uvicorn 字符串导入走磁盘）
if not getattr(sys, "frozen", False):
    if str(SERVER_ROOT) not in sys.path:
        sys.path.insert(0, str(SERVER_ROOT))
else:
    _bundled_server = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "server"
    if (_bundled_server / "app" / "main.py").is_file() and str(_bundled_server) not in sys.path:
        sys.path.insert(0, str(_bundled_server))
from app.core.app_info import (  # noqa: E402
    APP_NAME,
    APP_SLUG,
    ENV_PREFIX,
    HANDOFF_UNSUPPORTED_MARK,
    STAGING_DIR_NAME,
    STAGING_MANIFEST_NAME,
    SWAP_FAILED_FLAG,
)
from app.core.paths import data_dir_filename, is_frozen, resolve_data_dir  # noqa: E402

# WebView2 Runtime（Evergreen 固定产品 GUID）注册表探测 + 官方离线安装链
_WEBVIEW2_REG_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)
_WEBVIEW2_DL_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# 启动等待页：pywebview 内联 HTML（零网络依赖）。窗口初始化与后端 lifespan
# 并行重叠，health 就绪后由 _wait_and_navigate 整窗跳转真实应用。
# uvicorn 在 lifespan 完成前不监听端口，等待期只有内联 html= 可用。
_SPLASH_HTML = (
    "<!doctype html><html><head><meta charset='utf-8'>"
    f"<title>{APP_NAME}</title>"
    "<style>html,body{height:100%;margin:0}"
    "body{display:flex;align-items:center;justify-content:center;"
    "background:#1b2838;color:#c7d5e0;"
    "font:15px/1.8 'Segoe UI',system-ui,sans-serif}"
    ".box{text-align:center}"
    ".spin{width:34px;height:34px;margin:0 auto 16px;border-radius:50%;"
    "border:3px solid rgba(199,213,224,.2);border-top-color:#66c0f4;"
    "animation:r .9s linear infinite}"
    "@keyframes r{to{transform:rotate(360deg)}}"
    "</style></head><body><div class='box'><div class='spin'></div>"
    f"{APP_NAME} 启动中，请稍候…</div></body></html>"
)

SERVER_HOST = "127.0.0.1"
# 端口协商结果：默认取环境变量（HOLDEXAR_PORT/HOLDEXAR_LOCK_PORT），
# main() 启动时被占则按 DEV/FROZEN 态分流协商（见 _negotiate_ports）
SERVER_PORT = int(os.environ.get(f"{ENV_PREFIX}PORT", "28765"))
# 锁端口可换：被外来程序占坑时，设 HOLDEXAR_LOCK_PORT 换一个即可自救
LOCK_PORT = int(os.environ.get(f"{ENV_PREFIX}LOCK_PORT", "28965"))
# frozen 态服务端口被占时的自动扫描窗口（从默认端口起试 N 个）
_PORT_SCAN_SPAN = 20
# 健康检查等待上限：须覆盖首装时大数据量导入的耗时，兼顾低端机/机械盘。
HEALTH_TIMEOUT = 90.0
WINDOW_TITLE = APP_NAME
DEFAULT_SIZE = (1280, 860)
MIN_SIZE = (960, 600)


def _acquire_lock() -> socket.socket | None:
    """占用锁端口实现单实例；占用失败返回 None，真伪由调用方探活判定。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((SERVER_HOST, LOCK_PORT))
        sock.listen(1)
        return sock
    except OSError:
        return None


def _health_is_ours(response) -> bool:
    """健康响应身份校验：200 且载荷 app 字段等于本应用名才算本应用。

    health 载荷自带 {"app": APP_NAME, ...}（system/router.py）；非 JSON /
    缺字段 / 异名一律视为外来占坑，避免唤醒到同端口的他服务。
    """
    if getattr(response, "status_code", None) != 200:
        return False
    try:
        return response.json().get("app") == APP_NAME
    except Exception:  # noqa: BLE001 —— 非 JSON 响应一律非本应用
        return False


def _probe_running(timeout_s: float = 8.0, quick: bool = False) -> bool:
    """轮询主端口健康端点，判定是否真有本应用实例在跑（含身份校验）。

    留重试窗口覆盖「双击两次、前者还在启动中」的竞态。quick=True 单发
    一次（150ms 超时），用于锁已被本进程持有、仅需排除竞态窗口的场景。
    本地回环一律 trust_env=False 直连（见 _wait_health）。
    """
    url = f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/health"
    if quick:
        try:
            return _health_is_ours(httpx.get(url, timeout=0.15, trust_env=False))
        except Exception:  # noqa: BLE001
            return False
    deadline = time.time() + timeout_s
    while True:
        try:
            if _health_is_ours(httpx.get(url, timeout=1.5, trust_env=False)):
                return True
        except Exception:  # noqa: BLE001 无实例/未就绪均属预期
            pass
        if time.time() >= deadline:
            return False
        time.sleep(0.5)


def _port_free(port: int) -> bool:
    """试绑探测端口可否占用（立即释放；与 uvicorn 真正绑定间有微小竞态窗口）。"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((SERVER_HOST, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _negotiate_ports() -> None:
    """启动期端口协商：结果写回模块级 SERVER_PORT / LOCK_PORT。

    - 服务端口被占：探活命中 = 本应用另一实例（main() 走唤醒分支）；
      未命中 = 外来占坑 → frozen 态自默认端口起 +1 扫描可用位，
      开发态报错退出（端口漂移会掩盖配置问题）。
    - 锁端口被占：依次尝试备选锁位（_LOCK_FALLBACKS），全占则告警让位，
      单实例改由服务端口探活兜底。
    """
    global SERVER_PORT, LOCK_PORT
    frozen = getattr(sys, "frozen", False)

    if not _port_free(SERVER_PORT):
        if _probe_running():
            return  # 已有实例：原值保留，由 main() 唤醒分支处理
        if not frozen:
            _alert(
                f"启动失败：服务端口 {SERVER_PORT} 已被其他程序占用。\n\n"
                f"可结束占用该端口的进程，或用 python run.py --port <其他端口> 换端口启动。"
            )
            sys.exit(1)
        for candidate in range(SERVER_PORT + 1, SERVER_PORT + _PORT_SCAN_SPAN):
            if _port_free(candidate):
                print(f"[端口] 服务端口 {SERVER_PORT} 被占用，自动切换到 {candidate}")
                SERVER_PORT = candidate
                break
        else:
            _alert(
                f"启动失败：端口 {SERVER_PORT}~{SERVER_PORT + _PORT_SCAN_SPAN - 1} "
                "均被占用，无法启动本地服务。"
            )
            sys.exit(1)

    if not _port_free(LOCK_PORT):
        for candidate in _LOCK_FALLBACKS:
            if candidate != SERVER_PORT and _port_free(candidate):
                print(f"[端口] 锁端口 {LOCK_PORT} 被占用，改用 {candidate}")
                LOCK_PORT = candidate
                break
        else:
            # 全部备选锁位被外来程序占满（极端环境）：让位，
            # 单实例判定回落到"服务端口健康探活"（_probe_running）
            print(f"[警告] 锁端口 {LOCK_PORT} 与备选全被占用，单实例锁降级为探活判定")


# 备选锁位：避开常用本地服务端口段（28765/28965 附近），依次尝试
_LOCK_FALLBACKS = (28966, 28967, 28968, 29965, 29966)


def _start_server(ready: threading.Event, error_box: list[str]) -> None:
    # sys.path 已在模块头部按开发/打包态配好；此处无需再注入
    try:
        import uvicorn

        uvicorn.run("app.main:app", host=SERVER_HOST, port=SERVER_PORT, log_level="info")
    except Exception:  # noqa: BLE001 —— 启动失败要完整带回主线程
        error_box.append(traceback.format_exc())
    finally:
        ready.set()


def _watch_launcher() -> None:
    """包装进程（run.py）死亡 → 本进程随之退出：孤儿实例防线。

    Windows 硬杀父进程不连带收割子进程，无主实例会占住服务端口与单实例锁；
    watcher 线程等包装进程句柄，触发即 os._exit。直接拉起（无 LAUNCHER_PID
    环境，如打包态 exe、--server）不设防——本无可监视的父进程。
    """
    if os.name != "nt":
        return
    raw = os.environ.get(f"{ENV_PREFIX}LAUNCHER_PID", "").strip()
    if not raw.isdigit() or int(raw) == os.getpid():
        return
    ppid = int(raw)
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        SYNCHRONIZE = 0x00100000
        INFINITE = 0xFFFFFFFF
        # HANDLE 按 64 位宽度声明（ctypes 默认 c_int 会在 64 位下截断句柄）
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        handle = kernel32.OpenProcess(SYNCHRONIZE, False, ppid)
        if not handle:
            return  # 包装进程已不在/句柄打不开：无孤儿风险，不设防

        def _await_death() -> None:
            kernel32.WaitForSingleObject(handle, INFINITE)
            print(f"[退出] 启动器进程 {ppid} 已终止，本实例随之退出。")
            sys.stdout.flush()
            os._exit(0)

        threading.Thread(target=_await_death, daemon=True).start()
    except Exception:  # noqa: BLE001 —— 监视失败不阻断正常启动
        pass


def _alert(message: str) -> None:
    """windowed 打包态 print 不可见：关键失败路径弹系统对话框兜底。"""
    print(message)  # 控制台模式（python 直跑 / --server 调试）照常可见
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(0, message, APP_NAME, 0x10)  # MB_ICONERROR
    except Exception:  # noqa: BLE001 —— 非 Windows/无桌面环境静默
        pass


def _webview2_available() -> bool:
    """WebView2 Runtime 注册表探测（EdgeChromium 渲染层依赖，Win10 部分环境缺失）。"""
    if os.name != "nt":
        return True
    try:
        import winreg
    except ImportError:
        return True
    for key_path in _WEBVIEW2_REG_KEYS:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path):
                return True
        except OSError:
            continue
    return False


def _app_icon() -> str | None:
    """窗口图标：开发态 desktop/app.ico；打包态在资源目录（_MEIPASS）根。"""
    if getattr(sys, "frozen", False):
        candidates = [
            Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "app.ico",
            Path(sys.executable).resolve().parent / "app.ico",
        ]
    else:
        candidates = [PROJECT_ROOT / "desktop" / "app.ico"]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _wait_health(timeout: float) -> bool:
    """轮询本机健康端点直到就绪/超时。

    本机回环请求必须 trust_env=False 直连：httpx 读注册表系统代理，且其
    逗号分隔的 no-proxy 判定对 Windows 分号分隔的 ProxyOverride 无效，
    本机探测会被转发到代理端口（HTTP(S)_PROXY 环境变量同理）。
    """
    deadline = time.time() + timeout
    url = f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/health"
    while time.time() < deadline:
        try:
            # 身份校验同 _probe_running：万一端口号被外来服务抢先绑定，
            # 对方对 health 回 200 也不算本应用就绪（否则窗口会加载到
            # 对方的页面还显示"服务已就绪"）
            if _health_is_ours(httpx.get(url, timeout=2.0, trust_env=False)):
                return True
        except Exception:  # noqa: BLE001 —— 服务未就绪属预期
            time.sleep(0.3)
    return False


def _server_port_listening() -> bool:
    """TCP 层探测服务端口是否真在监听（socket 直连，不经 httpx 代理层）。

    watchdog 用它区分「探测链路被代理/TUN 劫持」与「服务进程没起来」。
    """
    try:
        with socket.create_connection((SERVER_HOST, SERVER_PORT), timeout=2.0):
            return True
    except OSError:
        return False


def _wait_and_navigate(window, app_url: str) -> None:
    """health 就绪后把窗口从内联等待页切到真实应用（webview.start 后的
    后台线程）。90s 未就绪不在此处理——watchdog 线程负责弹窗报错，本线程
    停止轮询即可，窗口停在转圈页。"""
    if _wait_health(HEALTH_TIMEOUT):
        try:
            window.load_url(app_url)
        except Exception:  # noqa: BLE001 —— 窗口已销毁（托盘退出）：无需跳转
            pass


class DesktopApi:
    """暴露给前端 window.pywebview.api 的桌面能力。

    仅桌面窗口模式可用；浏览器模式前端须检测 window.pywebview 不存在时降级。
    """


    def restart_app(self) -> dict:
        """前端「重启以完成更新」入口：新进程拉起自己，本进程即刻退出。

        js_api 调用在独立线程，os._exit 安全；重启后由 main() 的
        _handoff_pending_update 分支接管换装。
        """
        exe = Path(sys.executable) if getattr(sys, "frozen", False) else None
        if exe is None or not exe.is_file():
            return {"ok": False, "error": "仅打包态支持一键重启，请手动重启应用。"}
    
        subprocess.Popen(
            [str(exe)],
            cwd=str(exe.parent),
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        threading.Timer(0.5, lambda: os._exit(0)).start()
        return {"ok": True}

def _app_root() -> Path:
    """程序根目录：打包态 = exe 所在目录；开发态 = 项目根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def _data_dir() -> Path:
    """用户数据目录（含 update-staging 暂存）。

    必须与后端同源：`app.core.paths.resolve_data_dir` 是唯一判定入口，
    两边各写一套判定必然漂移——数据目录一旦分叉，换装会找不到暂存包，
    更糟的是把用户库认成「另一个库」。
    """
    return resolve_data_dir(APP_SLUG, ENV_PREFIX)


# 换装白名单：程序目录下这些名字**不参与**换装移动（用户数据永不动）
_UPDATE_KEEP = {
    "data",          # 用户数据（库/备份/导出/日志/种子暂存）
    "logs",          # 兼容旧目录名
    "__old__",       # 上一版程序备份（换装时刚移入的）
    STAGING_DIR_NAME,  # 暂存目录自身
}

# 换装助手入口标记：暂存包的新 exe 报出这个长选项才允许自动换装（见探测函数）
_HELPER_FLAG = "--apply-update"
# 暂存目录内的文件名（与后端 app/core/updater.py 同源常量，改名单点生效）
_MANIFEST_NAME = STAGING_MANIFEST_NAME
# 不支持安全换装的暂存包落盘标记：避免每次启动重复探测与弹窗
_UNSUPPORTED_MARK = HANDOFF_UNSUPPORTED_MARK
# 换装失败并已回滚的安装状态标记（落在数据目录，不随暂存目录被清理）
_FAILED_MARK = SWAP_FAILED_FLAG


def _update_log() -> Path:
    """换装日志路径。换装进程脱离控制台（print 无处可看），失败必须留痕。"""
    logs = _data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    return logs / "update-apply.log"


def _log_update(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(f"[更新] {line}")
    try:
        with _update_log().open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 —— 日志写不进去不阻断换装
        pass


def _swap_failed_flag() -> Path:
    """上次换装失败的安装状态标记。

    落点是数据目录而非暂存目录：作废清理由下一次启动的现装进程执行（换装进程
    跑在暂存目录里删不掉自己），标记若跟着暂存目录一起被清掉，`__old__`
    恢复副本与「不再重试」两条判据会同时失效。
    """
    return _data_dir() / _FAILED_MARK


def _remove_path(path: Path) -> None:

    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001 —— 清不掉留给下次启动
        pass


def _staged_main_exe(payload: Path) -> Path | None:
    """暂存载荷的主程序：优先与本程序同名，否则取载荷根目录下的 exe。

    按载荷自己的 exe 定位（用户可能给主程序改过名），换装后的启动目标
    也随之取该文件名。
    """
    candidates = sorted(p for p in payload.glob("*.exe") if p.is_file())
    if not candidates:
        return None
    same_name = payload / Path(sys.executable).name
    return same_name if same_name.is_file() else candidates[0]


def _staged_supports_helper(exe: Path) -> bool:
    """暂存包的新 exe 是否支持安全换装（`--apply-update`）。

    探不到即拒换：老版本没有换装入口，硬换装会在运行中的程序目录上做
    `shutil.move`（rename 失败静默降级 copytree+rmtree），把安装毁成半截。
    宁可不更新，不能毁掉现装。
    """

    try:
        result = subprocess.run(
            [str(exe), "--help"], capture_output=True, timeout=25,
            encoding="utf-8", errors="replace",
        )
    except Exception:  # noqa: BLE001 —— 起不来/超时一律按不支持处理
        return False
    return _HELPER_FLAG in ((result.stdout or "") + (result.stderr or ""))


def _wait_pid_exit(pid: int, timeout: float = 180.0) -> None:
    """等旧进程退出（Windows 句柄等待：换装进程要先拿到「程序目录没人占用」）。

    非 Windows / 句柄打不开时退化为短等待——换装进程本就是脱前台的兜底进程，
    多等一会儿不伤人；早动手才会撞文件锁。
    """
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            SYNCHRONIZE = 0x00100000
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, int(pid))
            if handle:
                kernel32.WaitForSingleObject(handle, int(timeout * 1000))
                return
        except Exception:  # noqa: BLE001 —— 句柄打不开走下面的兜底等待
            pass
    time.sleep(min(timeout, 5.0))


def _handoff_pending_update() -> bool:
    """staging 就绪 → 把换装交给**暂存包的新 exe**，本进程随即退出。

    本进程正从 `_internal` 装载 DLL，运行中的目录无法整体改名；换装进程
    等本进程句柄消失后再动文件。返回 True = 已交接（调用方必须立即退出，
    别再起 uvicorn 与窗口）。
    """
    if not is_frozen():
        return False

    # 上次换装失败过：交接只会在旧进程退出后重演同一次失败，用户看到的是
    # 「点了更新、应用关掉、版本没变」。标记是安装状态，只在重新下载解出
    # 新暂存包时解除（见 app/core/updater.py）。
    if _swap_failed_flag().is_file():
        _log_update("上次换装失败已回滚，跳过自动换装，按现装启动")
        return False

    staging = _data_dir() / STAGING_DIR_NAME
    payload = staging / APP_NAME
    manifest = staging / _MANIFEST_NAME
    if not manifest.is_file() or not payload.is_dir():
        return False
    try:
        info = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 坏 manifest 忽略，正常启动
        return False
    if not info.get("ready"):
        return False

    staged_exe = _staged_main_exe(payload)
    if staged_exe is None:
        _alert("[更新] 暂存包缺少主程序，本次换装已跳过（现装保持完好）。")
        return False

    if not _staged_supports_helper(staged_exe):
        if not (staging / _UNSUPPORTED_MARK).exists():
            (staging / _UNSUPPORTED_MARK).write_text(
                f"{info.get('tag')} 由旧版本生成，不支持安全换装\n", encoding="utf-8"
            )
            _log_update(f"暂存包 {info.get('tag')} 不支持安全换装，已跳过")
            # 弹窗放后台线程：MessageBoxW 是模态阻塞调用，放主线程会把本次启动
            # 卡在对话框上（用户不点确定，应用就起不来）
            threading.Thread(
                target=_alert,
                args=(
                    "[更新] 该更新包由旧版本生成，不支持安全换装，已跳过——现装保持完好。\n\n"
                    "请到发布页下载新版压缩包，解压后覆盖本程序目录完成升级；\n"
                    "用户数据不在程序目录内，覆盖解压不会影响它。",
                ),
                daemon=True,
            ).start()
        return False


    try:
        subprocess.Popen(
            [
                str(staged_exe), _HELPER_FLAG,
                "--staging", str(staging),
                "--target", str(_app_root()),
                "--wait-pid", str(os.getpid()),
            ],
            cwd=str(staged_exe.parent),
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
    except Exception as e:  # noqa: BLE001 —— 拉不起来就让位正常运行，下次启动再试
        _log_update(f"拉起换装进程失败：{e}")
        return False
    _log_update(f"换装已交给暂存包（{info.get('tag')}），本进程退出")
    return True


def _count_tree(path: Path) -> int:
    """文件计数（文件自身算 1，目录递归计数）——落位校验用。"""
    if path.is_file():
        return 1
    return sum(1 for p in path.rglob("*") if p.is_file())


def _restore_moved(moved: list[str], target: Path, old_dir: Path) -> None:
    """回滚已让位的旧条目（改不回的留在 __old__ 供手工恢复，绝不删）。"""
    for name in reversed(moved):
        src, dst = old_dir / name, target / name
        try:
            if dst.exists():
                _remove_path(dst)
            os.replace(src, dst)
        except Exception as e:  # noqa: BLE001
            _log_update(f"回滚 {name} 失败：{e}（原件仍在 {old_dir}，可手工移回）")


def _swap_payload(staging: Path, target: Path) -> bool:
    """换装主体：旧条目让位 → 新载荷落位 → 校验 → 清暂存。返回是否成功。

    搬运规范：旧条目只做 os.replace 改名（同卷原子，禁止 shutil.move——
    rename 失败会静默降级成 copytree+rmtree 毁安装）；新载荷复制落位
    （暂存与目标可能跨卷）。任一步失败即整体回滚，程序目录始终保持可启动。
    """

    payload = staging / APP_NAME
    old_dir = target / "__old__"

    # ① 上次换装残留：先清（这次反正要再放一版进去）
    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)
        if old_dir.exists():
            _log_update("残留 __old__ 清理失败，放弃本次换装")
            return False
    old_dir.mkdir(parents=True)

    # ② 旧条目让位（纯改名，逐条登记以便回滚）
    moved: list[str] = []
    try:
        for entry in target.iterdir():
            if entry.name in _UPDATE_KEEP or entry == old_dir:
                continue
            os.replace(entry, old_dir / entry.name)
            moved.append(entry.name)
    except Exception as e:  # noqa: BLE001
        _log_update(f"旧条目让位失败：{e}（回滚已让位条目）")
        _restore_moved(moved, target, old_dir)
        return False

    # ③ 新载荷落位
    landed: list[str] = []
    try:
        for entry in payload.iterdir():
            dst = target / entry.name
            if entry.is_dir():
                shutil.copytree(entry, dst)
            else:
                shutil.copy2(entry, dst)
            landed.append(entry.name)
    except Exception as e:  # noqa: BLE001
        _log_update(f"新载荷落位失败：{e}（回滚到原版本）")
        for name in landed:
            _remove_path(target / name)
        _restore_moved(moved, target, old_dir)
        return False

    # ④ 落位校验：少一个文件就是半截安装，宁可回滚（计数含顶层文件本身）
    want = sum(1 for p in payload.rglob("*") if p.is_file())
    got = sum(_count_tree(target / name) for name in landed)
    if got != want:
        _log_update(f"落位校验失败：期望 {want} 个文件、实得 {got} 个（回滚）")
        for name in landed:
            _remove_path(target / name)
        _restore_moved(moved, target, old_dir)
        return False

    # ⑤ 清标记与暂存：manifest 先删（重启不再触发换装），暂存目录尽力清——
    #   本进程就跑在暂存目录里，自己的 exe/DLL 删不掉，残留由新程序启动时收尾
    (staging / _MANIFEST_NAME).unlink(missing_ok=True)
    shutil.rmtree(staging, ignore_errors=True)
    _log_update(f"换装完成：{got} 个文件落位，上一版在 {old_dir}")
    return True


def _run_update_helper(staging: Path, target: Path, wait_pid: int | None) -> None:
    """换装进程入口（由暂存包的新 exe 以 --apply-update 拉起）。

    先等旧进程退出再动文件：这是整条换装链的立身之本（旧进程在跑时，程序目录
    的 `_internal` 无法整体改名）。成功即拉起新程序；失败保持原版本完好并弹窗
    给出日志路径与手工升级指引。
    """

    _log_update(f"换装进程启动：staging={staging} target={target} wait_pid={wait_pid}")
    if wait_pid:
        _wait_pid_exit(int(wait_pid))
    time.sleep(1.5)  # 进程退出 ≠ 文件锁立刻消失，留一点收尾余量

    if not _swap_payload(staging, target):
        _mark_swap_failed(target)
        # 先把窗口还给用户，再弹窗：MessageBoxW 是模态阻塞调用，排在拉起之前
        # 会让现装一直回不来——用户不点确定，应用就停在「点更新后消失」。
        _relaunch_after_failed_swap(target)
        threading.Thread(
            target=_alert,
            args=(
                "[更新] 换装失败，已回滚到原版本并重新打开应用。\n\n"
                "本次暂存包不会自动重试（下次启动会清理掉）；"
                "可到发布页下载新版压缩包手动解压覆盖。\n"
                f"详情见 {_update_log()}",
            ),
            daemon=True,
        ).start()
        return

    try:
        subprocess.Popen(
            [str(target / Path(sys.executable).name)],
            cwd=str(target),
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        _log_update("新版本已拉起")
    except Exception as e:  # noqa: BLE001 —— 新程序已就位，手动双击即可
        _log_update(f"拉起新版本失败：{e}（新程序已就位，可手动启动）")


def _mark_swap_failed(target: Path) -> None:
    """登记换装失败的安装状态。

    本进程正跑在暂存载荷里，删不掉自己的目录；作废暂存由下一次启动的现装进程
    （`_cleanup_staging_leftover`）执行，这里只落标记。
    """
    try:
        _swap_failed_flag().write_text(
            f"换装失败已回滚 target={target}\n", encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001 —— 标记写不进去不阻断回滚后的正常启动
        _log_update(f"写换装失败标记失败：{e}")


def _relaunch_after_failed_swap(target: Path) -> None:
    """回滚完成后拉起现装。

    换装进程是脱前台的：本进程退出而旧进程已先退出，不拉起就没有任何窗口，
    用户面对的是「点了更新应用消失」。主程序定位沿用暂存载荷的同一条规则
    （优先同名 exe，其次根目录任一 exe），用户改过主程序名也能拉起。
    """
    exe = _staged_main_exe(target)
    if exe is None:
        _log_update("回滚后未在程序目录找到主程序，请按弹窗提示手动恢复")
        return
    try:
        subprocess.Popen(
            [str(exe)],
            cwd=str(target),
            close_fds=True,
            creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
        )
        _log_update(f"回滚完成，已拉起现装 {exe.name}")
    except Exception as e:  # noqa: BLE001 —— 程序目录已回滚，手动双击即可
        _log_update(f"拉起现装失败：{e}（程序目录已回滚，可手动启动）")


def _should_clean_old_dir(old_dir: Path) -> bool:
    """`__old__` 清理判据：存在，且没有换装失败标记。

    失败标记在场说明 `__old__` 里可能还压着回滚没搬回去的原件，那是手工恢复的
    唯一退路；清掉它会让「回滚没救回来」变成不可逆。
    """
    return old_dir.exists() and not _swap_failed_flag().is_file()


def _cleanup_old_dir_async(old_dir: Path) -> None:
    """后台线程清 __old__：sharing violation 重试（Windows 文件锁语义）。"""

    def _retry_delete() -> None:
        for _ in range(30):  # ~30s 窗口
            if shutil.rmtree(old_dir, ignore_errors=True) or not old_dir.exists():
                return
            time.sleep(1.0)

    threading.Thread(target=_retry_delete, daemon=True).start()


def _cleanup_staging_leftover() -> None:
    """清理已消费或已作废的暂存目录（换装进程自身跑在暂存目录里，退出前删不干净）。

    判据是「没有 manifest」或「换装失败标记在场」：带 manifest 且未失败的暂存
    是待换装的正经包，不许动。失败包由本进程（现装，不在暂存目录内）在这里删掉，
    否则它会一直占着几百兆，且 manifest 留着会让前端一直报「已下载待重启」。
    """

    staging = _data_dir() / STAGING_DIR_NAME
    if not staging.is_dir():
        return
    if (staging / _MANIFEST_NAME).is_file() and not _swap_failed_flag().is_file():
        return
    shutil.rmtree(staging, ignore_errors=True)


def _browser_fallback(url: str, reason: str) -> None:
    """无窗口可用时的兜底：系统浏览器打开本地服务，进程保活维持后端。"""
    print(f"{reason}，改用系统浏览器打开。")
    _alert(f"{reason}，将改用系统浏览器打开本地服务（关闭本进程即退出服务）。")
    webbrowser.open(url)
    while True:  # 保活进程以维持本地服务
        time.sleep(3600)


# 托盘「退出」置位后，关窗守卫放行真关窗（区别于用户点 X 的隐藏）
_tray_state: dict = {"quit": False, "tray": None}


def _create_tray(window) -> object | None:
    """托盘图标：pythonnet/WinForms（pywebview winforms 后端自带依赖链）。

    双击 / 菜单「显示主窗口」唤起；菜单「退出」才真退进程；点 X = 隐藏窗口，
    后端随进程常驻。创建失败返回 None：关窗守卫照常隐藏，恢复靠再次启动唤醒。
    """
    try:
        import clr

        # 本函数运行在托盘线程（窗口建好后由 start(func) 拉起），pywebview
        # 的 winforms 装配件加载已发生——显式 AddReference 托盘所需两件
        clr.AddReference("System.Drawing")
        clr.AddReference("System.Windows.Forms")
        from System.Drawing import Icon as _DrawingIcon
        from System.Windows.Forms import (
            ContextMenuStrip,
            NotifyIcon,
            ToolStripMenuItem,
        )
    except Exception:  # noqa: BLE001
        return None

    icon = None
    icon_path = _app_icon()
    if icon_path:
        try:
            icon = _DrawingIcon(icon_path)
        except Exception:  # noqa: BLE001
            icon = None
    if icon is None:
        try:
            icon = _DrawingIcon.ExtractAssociatedIcon(sys.executable)
        except Exception:  # noqa: BLE001
            return None

    def _restore(_sender=None, _args=None) -> None:
        try:
            window.show()
        except Exception:  # noqa: BLE001
            pass
        _focus_running_window()

    def _quit(_sender=None, _args=None) -> None:
        _tray_state["quit"] = True
        try:
            tray.Visible = False
            tray.Dispose()
        except Exception:  # noqa: BLE001
            pass
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass
        # closed 事件兜底之外直接终审：后台线程随进程内核终结
        sys.stdout.flush()
        os._exit(0)

    tray = NotifyIcon()
    tray.Icon = icon
    tray.Text = APP_NAME
    tray.Visible = True
    menu = ContextMenuStrip()
    show_item = ToolStripMenuItem("显示主窗口")
    show_item.Click += _restore
    quit_item = ToolStripMenuItem("退出")
    quit_item.Click += _quit
    menu.Items.Add(show_item)
    menu.Items.Add(quit_item)
    tray.ContextMenuStrip = menu
    tray.DoubleClick += _restore
    _tray_state["tray"] = tray
    return tray


# ── 关闭弹窗外观（色值对齐 tokens.css 两档主题）────────────────────────
# 弹窗是独立 WinForms 窗，主题跟随网页（_app_theme 读 app_settings 镜像）；
# WinForms 无 CSS 变量体系，模块级只放字体与几何常量，配色全在 _DIALOG_THEMES。
_DIALOG_FONT_FAMILY = "Microsoft YaHei UI"  # 同网页 --font-sans 的中文回退栈
# 窗体圆角半径（无边框窗的圆滑轮廓，Region 裁切 + Paint 描边共用）
_DIALOG_CORNER_RADIUS = 12

# ── 弹窗幕布与出入场动效（数值对齐网页弹层档位）──────────────────────
# 幕布是页面级遮罩（.hl-close-curtain，同 .hl-overlay），由 _toggle_close_curtain
# 调页面里的 window.__hlxCloseCurtain 拉起。出入场：透明度 + 位移，
# 时长与缓动对齐 .hl-dialog-pop-* 档位（--ease-inout）。
_ANIM_MS = 170            # 网页档位 --duration-2/3 之间
_ANIM_TICK_MS = 15        # 帧间隔：约 60fps
_ANIM_RISE = 16           # 入场位移（px）：自下方浮到位，出场反向沉出

# 页面幕布开关脚本：显式返回布尔（页面没有挂载点 → false）。
# hook(true) 返回 undefined，不能用 !!(hook && hook(true)) 缩写。
_CURTAIN_JS = (
    "(function () {{ if (!window.__hlxCloseCurtain) return false;"
    " window.__hlxCloseCurtain({0}); return true; }})()"
)

# 双主题配色（色值对齐 tokens.css 两档）：深色 = html.dark 档，浅色 = :root 档。
# 模块级 RGB 元组，绘制处经 _c()（Color.FromArgb(*palette[name])）取用。
_DIALOG_THEMES: dict[str, dict] = {
    "dark": {
        "bg": (27, 40, 56),              # #1b2838
        "border": (44, 62, 82),          # #2c3e52
        "head": (255, 255, 255),         # 标题白
        "text": (199, 213, 224),         # #c7d5e0
        "subtle": (143, 152, 160),       # #8f98a0
        "accent_fill": (102, 192, 244),  # #66c0f4
        "accent_hover": (142, 208, 248),  # #8ed0f8
        "on_fill": (16, 32, 46),         # #10202e
        "danger_fill": (231, 76, 60),    # #e74c3c
        "danger_hover": (192, 57, 43),   # #c0392b
        "on_danger": (255, 255, 255),
        "close_fg": (154, 168, 181),     # #9aa8b5
        "close_fg_hover": (255, 255, 255),
        # 关闭键圆形悬停底：半透明白（FromArgb 参数序是 (a, r, g, b)）
        "close_hover_bg": (32, 255, 255, 255),
        "logo": "logo_dark.ico",         # 深色面 → 浅色（白色线条）logo
    },
    "light": {
        "bg": (247, 250, 253),           # #f7fafd（surface-pop-deep）
        "border": (219, 228, 238),       # 浅色描边档
        "head": (23, 32, 42),            # #17202a text-primary
        "text": (51, 71, 90),            # #33475a text-secondary
        "subtle": (100, 119, 140),       # #64778c text-muted
        "accent_fill": (124, 185, 226),  # #7cb9e2 accent-fill
        "accent_hover": (95, 168, 216),  # #5fa8d8 accent-fill-hover
        "on_fill": (16, 32, 46),         # #10202e
        "danger_fill": (192, 57, 43),    # #c0392b danger
        "danger_hover": (150, 40, 27),   # #96281b danger-deep
        "on_danger": (255, 255, 255),
        "close_fg": (100, 119, 140),     # #64778c（浅色面叉线用深灰蓝）
        "close_fg_hover": (23, 32, 42),  # #17202a
        "close_hover_bg": (26, 23, 32, 42),  # 半透明深（a,r,g,b）
        "logo": "logo_light.ico",        # 浅色面 → 深色线条 logo
    },
}


def _app_theme() -> str:
    """应用主题偏好：'light' / 'dark'（读不到按深色兜底）。

    网页主题本体在 localStorage（桌面壳读不到），前端 apply() 把它镜像进
    app_settings（ui.theme，初始化与每次切换都写）——这里同步只读查询。
    """
    try:
        import sqlite3

        db_path = resolve_data_dir(APP_SLUG) / data_dir_filename(APP_SLUG)
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute(
                "SELECT value_json FROM app_settings WHERE key = 'ui.theme'"
            ).fetchone()
        finally:
            con.close()
        if row and row[0] is not None:
            # app_settings 存的是 JSON 编码值（前端写进去的是带引号的 "light"），
            # 先按 JSON 解码，解不动再按裸值判。
            value = str(row[0]).strip()
            try:
                value = str(json.loads(value))
            except Exception:  # noqa: BLE001 —— 不是合法 JSON 就当裸值
                value = value.strip().strip("\"'")
            if value in ("dark", "light"):
                return value
    except Exception:  # noqa: BLE001 —— 读不到按深色兜底
        pass
    return "dark"


def _brand_logo_path(theme: str = "dark") -> str | None:
    """弹窗标题栏 logo：深色面用浅色（白线条）版，浅色面用深色版。

    logo_dark / logo_light 指「给哪种主题用」，反了会糊进底色。打包态
    前端产物在 web/dist/assets/，找不到回退 _app_icon()。
    """
    name = "logo_light.ico" if theme == "light" else "logo_dark.ico"
    candidates: list[Path] = []
    if getattr(sys, "frozen", False):
        mei = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidates.append(mei / "web" / "dist" / "assets" / name)
    else:
        candidates.append(PROJECT_ROOT / "web" / "public" / "assets" / name)
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return _app_icon()


def _round_path(rect, radius: int):
    """圆角矩形路径（左上/右上/右下/左下四段圆弧闭合）。"""
    from System.Drawing.Drawing2D import GraphicsPath

    path = GraphicsPath()
    d = radius * 2
    path.AddArc(rect.X, rect.Y, d, d, 180, 90)
    path.AddArc(rect.Right - d, rect.Y, d, d, 270, 90)
    path.AddArc(rect.Right - d, rect.Bottom - d, d, d, 0, 90)
    path.AddArc(rect.X, rect.Bottom - d, d, d, 90, 90)
    path.CloseFigure()
    return path


def _style_surface(control) -> None:
    """把控件切成自绘模式（无系统边框/背景，交给 Paint 事件画主题外观）。

    **必须用事件绑定而非 override OnPaint**：pythonnet 对 Python 子类的
    虚方法分派不生效，CLR→Python 只有事件通路（托盘 NotifyIcon 同此）。
    """
    from System.Windows.Forms import ControlStyles

    control.SetStyle(
        ControlStyles.UserPaint
        | ControlStyles.AllPaintingInWmPaint
        | ControlStyles.OptimizedDoubleBuffer,
        True,
    )


def _attach_close_animation(dialog) -> None:
    """给弹窗挂出入场动效：淡入 + 自下浮入 / 淡出 + 反向沉出。

    入场在 Shown 后起步（CenterParent 的落点显示时才定，位移须基于该
    落点）；出场在 FormClosing 拦一次，动效走完再真关。

    关闭被拦下时 WinForms 会把 DialogResult 复位、真关只回 Cancel（按键
    等于按了没反应）——故先存 state，真关前放回（见 _close_now）。

    动效是装饰：任何一步失败都退化为弹窗照常开合（入场失败恢复不透明度
    1，出场失败立即放行），绝不把弹窗留在不可见或关不掉的状态。
    """
    from System.Drawing import Point
    from System.Windows.Forms import DialogResult, Timer

    # 枚举的 None 成员在 Python 里写不出来（"None" 是关键字），只能 getattr 取；
    # 取不到（单测替身）退化成 Python 的 None，「未选择」判定两边都落在同一个哨兵上。
    unset_result = getattr(DialogResult, "None", None)

    state: dict = {
        "timer": None, "phase": "in", "step": 0, "base": None, "closed": False,
        "result": None,  # 用户的选择（DialogResult）：关闭被拦时它会丢，这里存一份
    }

    def _stop() -> None:
        timer = state["timer"]
        state["timer"] = None
        if timer is not None:
            try:
                timer.Stop()
                timer.Dispose()
            except Exception:  # noqa: BLE001
                pass

    def _place(k: float, phase: str) -> None:
        """按进度 k（0..1）铺两条表面的终态：透明度 + 位移。"""
        base = state["base"]
        if phase == "in":
            dialog.Opacity = k
            offset = int(round(_ANIM_RISE * (1.0 - k)))
        else:
            dialog.Opacity = 1.0 - k
            offset = int(round(_ANIM_RISE * k))
        if base is not None:
            dialog.Location = Point(base.X, base.Y + offset)

    def _settle() -> None:
        """终态：弹窗全不透明落回基准位，幕布满档。

        透明度是必须到位的（否则弹窗隐形）；位置拿不到只当少一次位移，
        不影响弹窗可用。
        """
        dialog.Opacity = 1.0
        try:
            base = state["base"] if state["base"] is not None else dialog.Location
            state["base"] = base
            dialog.Location = Point(base.X, base.Y)
        except Exception:  # noqa: BLE001
            pass

    def _close_now() -> None:
        """真关弹窗：放回用户的选择（被拦下的那次关闭把 DialogResult 清掉了），
        再 Close()——ShowDialog 这才带得回 Yes / No，而不是清空后的 Cancel。"""
        state["closed"] = True
        if state["result"] is not None:
            try:
                dialog.DialogResult = state["result"]
            except Exception:  # noqa: BLE001 —— 放不回也别把弹窗留在屏上
                pass
        try:
            dialog.Close()
        except Exception:  # noqa: BLE001
            pass

    total_steps = max(1, _ANIM_MS // _ANIM_TICK_MS)

    def _tick(sender, e) -> None:
        try:
            state["step"] += 1
            t = min(1.0, state["step"] / total_steps)
            k = t * t * (3.0 - 2.0 * t)  # smoothstep ≈ 网页 --ease-inout
            _place(k, state["phase"])
            if t < 1.0:
                return
            _stop()
            if state["phase"] == "in":
                _settle()
            else:
                _close_now()
        except Exception:  # noqa: BLE001 —— 动效炸穿也要把弹窗开/关到位
            _stop()
            if state["phase"] == "in":
                try:
                    _settle()
                except Exception:  # noqa: BLE001
                    pass
            elif not state["closed"]:
                _close_now()

    def _start(phase: str) -> None:
        _stop()
        state["phase"] = phase
        state["step"] = 0
        timer = Timer()
        timer.Interval = _ANIM_TICK_MS
        timer.Tick += _tick
        state["timer"] = timer
        timer.Start()

    def _on_shown(sender, e) -> None:
        try:
            state["base"] = dialog.Location
        except Exception:  # noqa: BLE001
            state["base"] = None
        if not _animations_enabled():
            _settle()
            return
        try:
            _place(0.0, "in")  # 先归零：显示瞬间可能已用实底画过一帧
            _start("in")
        except Exception:  # noqa: BLE001
            _settle()

    def _on_closing(sender, e) -> None:
        # 动效走完的那次真关（_close_now 已置位）、系统关动画：直接放行
        if state["closed"] or not _animations_enabled():
            return
        # DialogResult 还是「未选择」= 不是用户按键（程序化/system 路径），交给原流程
        result = dialog.DialogResult
        if result is unset_result or result == unset_result:
            return
        state["result"] = result  # 接住选择：本次关闭被拦后它会被 WinForms 复位
        e.Cancel = True
        try:
            _place(0.0, "out")
            _start("out")
        except Exception:  # noqa: BLE001 —— 动效起不来就立刻放行，不留关不掉的窗
            _close_now()

    dialog.Shown += _on_shown
    dialog.FormClosing += _on_closing
    # 入场前先隐形（Shown 里再归零会闪一帧实底）；动效不可用时不留痕
    if _animations_enabled():
        dialog.Opacity = 0.0


def _animations_enabled() -> bool:
    """系统「在 Windows 中显示动画」开关（SPI_GETCLIENTAREAANIMATION）。

    与网页侧 --motion-scale 总闸对齐：系统关动画时直接出终态。
    """
    try:
        import ctypes

        value = ctypes.c_int(1)
        ok = ctypes.windll.user32.SystemParametersInfoW(
            0x1042, 0, ctypes.byref(value), 0  # SPI_GETCLIENTAREAANIMATION
        )
        return bool(value.value) if ok else True
    except Exception:  # noqa: BLE001 —— 查询失败按开启处理
        return True


def _make_close_button(
    dialog_result,
    location,
    size=36,
    bg=None,
    fg=None,
    fg_hover=None,
    hover_bg=None,
) -> object:
    """右上角关闭键（圆形悬停底 + 细叉线，自绘覆盖系统方形 X）。

    Label 载体（无系统边框/焦点框），Paint 事件画悬停底与叉线；点击手动置
    窗体 DialogResult（语义同 Button.DialogResult）。叉线/悬停色按主题传入。
    """
    from System.Drawing import Color, Point, Rectangle, Size, SolidBrush, Pen
    from System.Drawing.Drawing2D import SmoothingMode
    from System.Windows.Forms import Cursors, Label as WinLabel

    button = WinLabel()
    button.AutoSize = False
    button.Text = ""
    button.AccessibleName = "关闭"
    button.Size = Size(size, size)
    button.Location = Point(*location)
    button.Cursor = Cursors.Hand
    if bg is not None:
        button.BackColor = bg
    _style_surface(button)

    state = {"hover": False}

    def _on_paint(sender, e):
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        if bg is not None:
            g.FillRectangle(SolidBrush(bg), Rectangle(0, 0, button.Width, button.Height))
        if state["hover"]:
            # 圆形悬停底：hover 高亮是圆的，不是系统那种方块。
            # fg/fg_hover/hover_bg 已是 Color 对象（主题表经 _c() 转换后传入），
            # 直接用——对 Color 做 * 展开会抛「argument after * must be an
            # iterable, not Color」并弹 CLR 异常对话框。
            g.FillEllipse(
                SolidBrush(hover_bg),
                Rectangle(0, 0, button.Width - 1, button.Height - 1),
            )
        pen = Pen(
            fg_hover if state["hover"] else fg,
            1.6,
        )
        inset = size * 0.34
        far = size - inset
        g.DrawLine(pen, inset, inset, far, far)
        g.DrawLine(pen, far, inset, inset, far)

    def _on_click(sender, e):
        form = button.FindForm()
        if form is not None:
            form.DialogResult = dialog_result

    def _on_enter(sender, e):
        state["hover"] = True
        button.Invalidate()

    def _on_leave(sender, e):
        state["hover"] = False
        button.Invalidate()

    button.Paint += _on_paint
    button.Click += _on_click
    button.MouseEnter += _on_enter
    button.MouseLeave += _on_leave
    return button


def _build_close_dialog(owner=None, theme: str | None = None):
    """构建「最小化 / 退出程序」二选一对话框（WinForms 自绘，随应用主题）。

    返回 (form, mapping)：mapping 把 DialogResult 译成意图（'minimize' /
    'quit'；Cancel = 留在窗口）。系统 MessageBox 出不了「最小化」「退出
    程序」动作词，故整窗自绘。要点：theme 缺省读 app_settings 镜像
    （ui.theme）；无边框自绘标题栏（logo + 圆滑关闭键）；Region 裁圆角 +
    1px 描边；按键双色分工动作性质（最小化 = 品牌蓝 / 退出 = 危险红）；
    字号用像素单位（pt 随 DPI 放大，与像素布局混用会失调）；回车 = 最小化
    （AcceptButton，安全侧），Esc = 留在窗口（KeyPreview + KeyDown）。
    """
    import clr  # noqa: F401 —— pythonnet 装配件

    clr.AddReference("System.Drawing")
    clr.AddReference("System.Windows.Forms")
    from System.Drawing import (
        Color,
        Font,
        FontStyle,
        GraphicsUnit,
        Icon,
        Pen,
        Point,
        Rectangle,
        RectangleF,
        Size,
        SolidBrush,
        StringFormat,
        StringAlignment,
    )
    from System.Drawing.Drawing2D import GraphicsPath, SmoothingMode
    from System.Windows.Forms import (
        AutoScaleMode,
        Button as WinButton,
        ControlStyles,
        Cursors,
        DialogResult,
        Form as WinForm,
        FormBorderStyle,
        FormStartPosition,
        Label as WinLabel,
        MouseButtons,
        PictureBox,
        PictureBoxSizeMode,
    )
    from System.Drawing import Region

    theme = theme or _app_theme()
    palette = _DIALOG_THEMES.get(theme, _DIALOG_THEMES["dark"])

    def _c(name: str) -> Color:
        return Color.FromArgb(*palette[name])

    # 字号用**像素单位**（GraphicsUnit.Pixel）而非 pt：pt 是物理单位，会随
    # 系统 DPI 放大，而布局是像素——两者混用就会「字大了框没大」。统一像素
    # 后任何 DPI 下比例一致。
    font_title = Font(_DIALOG_FONT_FAMILY, 15.0, FontStyle.Bold, GraphicsUnit.Pixel)
    font_head = Font(_DIALOG_FONT_FAMILY, 17.0, FontStyle.Bold, GraphicsUnit.Pixel)
    font_body = Font(_DIALOG_FONT_FAMILY, 14.0, FontStyle.Regular, GraphicsUnit.Pixel)
    font_button = Font(_DIALOG_FONT_FAMILY, 15.0, FontStyle.Bold, GraphicsUnit.Pixel)

    # 布局（客户区 520×240）：自绘标题栏 52 → 正文 → 按键行 y=170
    WIDTH, HEIGHT = 520, 240
    BTN_W, BTN_H, BTN_GAP = 200, 44, 16
    BTN_Y = 170
    BTN_X0 = (WIDTH - (BTN_W * 2 + BTN_GAP)) // 2  # 组居中：起点 52

    def _round_button(
        text: str, dialog_result, x: int, y: int, font, fill, hover, text_color
    ) -> WinButton:
        """圆角纯色按键（自绘）。

        不用 FlatStyle.Flat（方角 + 系统描边 + 固定面色，且无圆角可调），
        也不用 override OnPaint——pythonnet 对 Python 子类的虚方法分派不生效，
        事件绑定才是 CLR→Python 的通路（托盘 NotifyIcon 同此）。DrawString
        文字框必须 RectangleF（pythonnet 不做 Rectangle→RectangleF 隐式转换，
        传 Rectangle 会抛 CLR 异常）。载体保留 Button：DialogResult /
        AcceptButton（回车默认）语义齐全。
        """
        btn = WinButton()
        btn.Text = text
        btn.AccessibleName = text  # 自绘按键给语义名（读屏/测试都按它找控件）
        btn.DialogResult = dialog_result
        btn.Size = Size(BTN_W, BTN_H)
        btn.Location = Point(x, y)
        btn.Font = font
        btn.Cursor = Cursors.Hand
        btn.SetStyle(
            ControlStyles.UserPaint
            | ControlStyles.AllPaintingInWmPaint
            | ControlStyles.OptimizedDoubleBuffer,
            True,
        )
        state = {"fill": fill}

        def _on_paint(sender, e):
            g = e.Graphics
            g.SmoothingMode = SmoothingMode.AntiAlias
            rect = Rectangle(0, 0, btn.Width - 1, btn.Height - 1)
            radius = 9
            path = GraphicsPath()
            path.AddArc(rect.X, rect.Y, radius, radius, 180, 90)
            path.AddArc(rect.Right - radius, rect.Y, radius, radius, 270, 90)
            path.AddArc(rect.Right - radius, rect.Bottom - radius, radius, radius, 0, 90)
            path.AddArc(rect.X, rect.Bottom - radius, radius, radius, 90, 90)
            path.CloseFigure()
            g.FillPath(SolidBrush(state["fill"]), path)
            fmt = StringFormat()
            fmt.Alignment = StringAlignment.Center
            fmt.LineAlignment = StringAlignment.Center
            g.DrawString(
                text,
                btn.Font,
                SolidBrush(text_color),
                RectangleF(0.0, 0.0, float(btn.Width), float(btn.Height)),
                fmt,
            )

        def _on_enter(sender, e):
            state["fill"] = hover
            btn.Invalidate()

        def _on_leave(sender, e):
            state["fill"] = fill
            btn.Invalidate()

        btn.Paint += _on_paint
        btn.MouseEnter += _on_enter
        btn.MouseLeave += _on_leave
        return btn

    dialog = WinForm()
    dialog.Text = f"关闭 {APP_NAME}"
    # 无边框自绘：标题栏（logo/标题/圆滑关闭键）全部自绘，拖动转交系统
    # （WM_NCLBUTTONDOWN）。AutoScaleMode.None + 全程像素坐标（含字号），
    # 不做 DPI 换算。
    dialog.AutoScaleMode = getattr(AutoScaleMode, "None")
    dialog.FormBorderStyle = getattr(FormBorderStyle, "None")
    dialog.BackColor = _c("bg")
    dialog.ForeColor = _c("text")
    dialog.ShowInTaskbar = False
    dialog.StartPosition = (
        FormStartPosition.CenterParent
        if owner is not None
        else FormStartPosition.CenterScreen
    )
    dialog.ClientSize = Size(WIDTH, HEIGHT)

    # 窗口图标 = 品牌 logo（任务栏/Alt-Tab；按主题取线条版）
    logo_bitmap = None
    icon_path = _brand_logo_path(theme)
    if icon_path:
        try:
            logo_bitmap = Icon(icon_path).ToBitmap()
            dialog.Icon = Icon(icon_path)
        except Exception:  # noqa: BLE001 —— 图标加载失败不影响弹窗
            logo_bitmap = None

    def _on_form_paint(sender, e):
        """窗底：圆角填充 + 1px 描边（无边框窗没有系统边框可依）。"""
        g = e.Graphics
        g.SmoothingMode = SmoothingMode.AntiAlias
        path = _round_path(
            Rectangle(0, 0, dialog.Width - 1, dialog.Height - 1),
            _DIALOG_CORNER_RADIUS,
        )
        g.FillPath(SolidBrush(_c("bg")), path)
        g.DrawPath(Pen(_c("border"), 1.0), path)

    def _apply_round_region(*_args):
        """窗形裁成圆滑轮廓（Region 不随 DPI 自动换算，按实际尺寸重算）。"""
        try:
            dialog.Region = Region(
                _round_path(
                    Rectangle(0, 0, dialog.Width, dialog.Height),
                    _DIALOG_CORNER_RADIUS,
                )
            )
        except Exception:  # noqa: BLE001 —— 圆角失败退化为方窗，功能不受影响
            pass

    def _start_drag(sender, e):
        """自绘标题栏的拖动：转交系统标题栏拖动（WM_NCLBUTTONDOWN）。"""
        if e.Button != MouseButtons.Left:
            return
        try:
            import ctypes

            ctypes.windll.user32.ReleaseCapture()
            ctypes.windll.user32.SendMessageW(dialog.Handle.ToInt64(), 0xA1, 0x2, 0)
        except Exception:  # noqa: BLE001 —— 拖动失败不影响其它交互
            pass

    dialog.Paint += _on_form_paint
    _apply_round_region()
    dialog.SizeChanged += _apply_round_region
    dialog.MouseDown += _start_drag

    # ── 标题栏：品牌 logo（按主题取线条版）+ 标题 + 圆滑关闭键 ──
    if logo_bitmap is not None:
        logo = PictureBox()
        logo.Image = logo_bitmap
        logo.SizeMode = PictureBoxSizeMode.Zoom
        logo.Size = Size(20, 20)
        logo.Location = Point(22, 16)
        logo.BackColor = _c("bg")
        logo.AutoSize = False
        logo.MouseDown += _start_drag
        dialog.Controls.Add(logo)

    title = WinLabel()
    title.Text = f"关闭 {APP_NAME}"
    title.AutoSize = False
    title.Size = Size(320, 24)
    title.Location = Point(52, 15)
    title.Font = font_title
    title.ForeColor = _c("head")
    title.BackColor = _c("bg")
    title.MouseDown += _start_drag
    dialog.Controls.Add(title)

    btn_close = _make_close_button(
        DialogResult.Cancel,
        (WIDTH - 54, 12),
        36,
        bg=_c("bg"),
        fg=_c("close_fg"),
        fg_hover=_c("close_fg_hover"),
        hover_bg=_c("close_hover_bg"),
    )
    dialog.Controls.Add(btn_close)

    # ── 正文 ──
    head = WinLabel()
    head.Text = f"要如何关闭 {APP_NAME}？"
    head.AutoSize = False
    head.Size = Size(WIDTH - 56, 28)
    head.Location = Point(28, 74)
    head.Font = font_head
    head.ForeColor = _c("head")
    head.BackColor = _c("bg")
    dialog.Controls.Add(head)

    detail = WinLabel()
    detail.Text = "最小化到托盘后，应用仍在后台继续更新数据。"
    detail.AutoSize = False
    detail.Size = Size(WIDTH - 56, 22)
    detail.Location = Point(28, 110)
    detail.Font = font_body
    detail.ForeColor = _c("subtle")
    detail.BackColor = _c("bg")
    dialog.Controls.Add(detail)

    # ── 按键：圆角纯色双色（两色分工动作性质）──
    btn_min = _round_button(
        "最小化",
        DialogResult.Yes,
        BTN_X0,
        BTN_Y,
        font_button,
        _c("accent_fill"),
        _c("accent_hover"),
        _c("on_fill"),
    )
    btn_quit = _round_button(
        "退出程序",
        DialogResult.No,
        BTN_X0 + BTN_W + BTN_GAP,
        BTN_Y,
        font_button,
        _c("danger_fill"),
        _c("danger_hover"),
        _c("on_danger"),
    )
    dialog.Controls.Add(btn_min)
    dialog.Controls.Add(btn_quit)

    dialog.AcceptButton = btn_min  # 回车 = 最小化（安全侧）
    # Esc = 留在窗口（回车交给 AcceptButton）：圆滑关闭键是 Label 载体，
    # 接不了 CancelButton，键盘语义由 KeyPreview + KeyDown 兜住。
    dialog.KeyPreview = True

    def _on_key_down(sender, e):
        from System.Windows.Forms import Keys

        if e.KeyCode == Keys.Escape:
            dialog.DialogResult = DialogResult.Cancel

    dialog.KeyDown += _on_key_down

    return dialog, {
        DialogResult.Yes: "minimize",
        DialogResult.No: "quit",
        DialogResult.Cancel: "stay",
    }


def _toggle_close_curtain(window, on: bool) -> None:
    """切页面级关窗幕布：给网页发 __hlxCloseCurtain(true/false)。

    必须后台线程派发：evaluate_js 同步阻塞（回调排回 UI 线程），从关窗
    守卫（UI 线程）直接调会自锁。
    """
    if window is None:
        return
    script = _CURTAIN_JS.format("true" if on else "false")

    def _dispatch() -> None:
        try:
            window.evaluate_js(script)
        except Exception:  # noqa: BLE001 —— 页面没有挂载点/窗口已销毁都不影响关窗流程
            pass

    try:
        threading.Thread(target=_dispatch, daemon=True).start()
    except Exception:  # noqa: BLE001
        pass


def _ask_close_intent(owner=None, window=None) -> str:
    """关窗意图询问：二选一（最小化到托盘 / 退出程序），同步模态对话框。

    返回 'minimize' / 'quit'；任何异常（pythonnet 缺失 / 无桌面会话）一律
    降级 'minimize'（问不出意图时保持「点 X = 后台常驻」语义，不误杀进程）。
    同步弹窗：closing handler 本就同步跑在 UI 线程，阻塞拿用户决定。幕布与
    出入场动效在此装配，任一步失败只是少幕布/动效，弹窗照常弹出。
    """
    dialog = None
    try:
        theme = _app_theme()
        dialog, mapping = _build_close_dialog(owner, theme)
        _toggle_close_curtain(window, True)  # 幕布先亮（页面侧渲染，与弹窗同拍）
        try:
            _attach_close_animation(dialog)
        except Exception:  # noqa: BLE001
            dialog.Opacity = 1.0  # 动效装配失败：弹窗必须看得见
        result = dialog.ShowDialog(owner) if owner is not None else dialog.ShowDialog()
        return mapping.get(result, "minimize")
    except Exception:  # noqa: BLE001 —— 问不出就按旧语义隐藏，不误杀进程
        return "minimize"
    finally:
        if dialog is not None:
            try:
                dialog.Dispose()
            except Exception:  # noqa: BLE001 —— 资源回收失败不影响流程
                pass
        _toggle_close_curtain(window, False)


def _make_closing_guard(window):
    """关窗守卫：点 X 先问意图——最小化到托盘 / 退出程序（弹窗 X = 留在窗口）。

    pywebview closing 事件契约：handler 返回 **False** = 取消关闭
    （closing 事件 should_lock=True，handler 同步跑在 UI 线程，返回值
    统计 False → winforms on_closing 置 args.Cancel）；True/None = 放行。
    托盘「退出」置 _tray_state['quit'] 后走托盘自己的销毁收尾，不进本守卫
    （托盘菜单是明确意图，免询问）。"""
    def _guard() -> bool:
        if _tray_state["quit"]:
            return True  # 托盘「退出」：放行真关闭
        owner = getattr(window, "native", None)
        intent = _ask_close_intent(owner, window)
        if intent == "quit":
            # 先撤窗再放行：点「退出程序」窗口必须当场消失，进程收尾期间窗口
            # 不滞留屏幕。撤窗只收走可见面；真关闭照常走（closed 事件里
            # os._exit(0) 终结进程）。
            try:
                window.hide()
            except Exception:  # noqa: BLE001
                pass
            return True  # 放行真关闭：closed 事件里 os._exit(0) 终结进程
        if intent == "stay":
            return False  # 弹窗右上角 X：取消关闭，窗口原地不动
        # 托盘没建起来时**不能 hide()**：窗口从屏幕与任务栏一起消失且无入口
        # 可召回。退化成最小化，任务栏仍可点回来。
        if _tray_state.get("tray") is None:
            try:
                window.minimize()
            except Exception:  # noqa: BLE001
                pass
            return False
        try:
            window.hide()
        except Exception:  # noqa: BLE001
            pass
        return False  # 最小化：取消关闭，窗口隐藏、进程常驻

    return _guard


def _on_window_ready(window, app_url: str) -> None:
    """webview.start(func) 入口：窗口真正建好后拉起两件后台事——
    health 等待跳转线程（等待页 → 真实应用）与托盘线程。"""
    threading.Thread(target=_wait_and_navigate, args=(window, app_url), daemon=True).start()
    _run_tray(window)


def _run_tray(window) -> None:
    """托盘线程入口（经 webview.start(func) 在窗口建好后拉起）。

    NotifyIcon 必须晚于 pywebview 的 SetCompatibleTextRenderingDefault
    创建，早建会炸穿 webview.start()。本线程自起 WinForms 消息泵派发
    托盘事件；主泵归窗口，两泵互不干扰。
    """
    tray = _create_tray(window)
    if tray is None:
        return
    try:
        import System.Windows.Forms as WinForms  # noqa: F401 —— pythonnet 装配件

        WinForms.Application.Run()  # 托盘线程消息泵，直至 Application.ExitThread
    except Exception:  # noqa: BLE001 —— 托盘失效不拖垮主窗口
        pass


def _open_window(app_url: str) -> None:
    """开主窗。首载内联等待页（零网络依赖，见 _SPLASH_HTML），health 就绪
    后由 _wait_and_navigate 切到 app_url；窗口不可用时浏览器兜底直接开
    app_url（等待页是进程内 HTML，系统浏览器里无意义，兜底必须走真实地址）。"""
    try:
        import webview
    except ImportError:
        webview = None  # type: ignore[assignment]
    if webview is None:
        _browser_fallback(app_url, "pywebview 未安装")
        return
    if not _webview2_available():
        _browser_fallback(
            app_url,
            "未检测到 WebView2 Runtime（Windows 渲染组件缺失），"
            f"请从微软官网安装后重新启动：{_WEBVIEW2_DL_URL}",
        )
        return
    kwargs = {
        "width": DEFAULT_SIZE[0],
        "height": DEFAULT_SIZE[1],
        "min_size": MIN_SIZE,
        "js_api": DesktopApi(),
        # 放开原生文本选择：默认 text_select=False 会显式关掉 WebView2 的
        # IsTextSelectionEnabled；置 True 后与浏览器一致（拖选 + Ctrl+C）。
        "text_select": True,
    }
    try:
        window = webview.create_window(WINDOW_TITLE, html=_SPLASH_HTML, **kwargs)
        # 关窗语义：点 X = 隐藏到托盘，后端（uvicorn 线程 + 调度器）随进程
        # 常驻继续更新数据；托盘「退出」置 quit 后放行真关闭。closed 事件
        # 保留为终审兜底（两条退出路径都汇聚到 os._exit，不走解释器收尾
        # 的延迟路径——后台线程随进程内核直接终结）。
        window.events.closing += _make_closing_guard(window)
        window.events.closed += lambda: (sys.stdout.flush(), os._exit(0))
        start_kwargs = {}
        icon = _app_icon()
        if icon:
            # pywebview 5.x：icon 是 start() 的参数（create_window 无此参，
            # 传了会 TypeError 顶层炸穿兜底）
            start_kwargs["icon"] = icon
        # 托盘与 health 跳转都在窗口建好后由 start(func) 拉起（时序见 _run_tray）
        webview.start(_on_window_ready, (window, app_url), **start_kwargs)  # 阻塞至窗口真关闭（仅托盘退出可达）
    except Exception as e:  # noqa: BLE001 —— 窗口创建/渲染层初始化失败一并降级浏览器
        _browser_fallback(app_url, f"窗口初始化失败：{e}")
        return
    sys.stdout.flush()
    os._exit(0)


def _focus_running_window() -> bool:
    """置前已在运行的本应用窗口（FindWindow 精确标题匹配）。

    单实例唤醒语义：找到窗口 → 还原最小化 + 前台；找不到（服务在跑但
    无窗口，如 --server 模式）→ 返回 False 由调用方降级 webbrowser。
    """
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return False
        # 托盘隐藏态 / 最小化态都先 SW_RESTORE（IsIconic 判定；user32 无
        # IsIconicWindow 导出，误用会恒抛 AttributeError 退化成只前台化不还原）
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001 —— 非 Windows/无桌面环境降级
        return False


def _apply_webview2_static_args() -> None:
    """主窗口 WebView2 静态浏览器参数：禁组件更新与后台网络服务。

    WebView2 组件更新器会创建 BITS 任务；宿主 exe 无数字签名时杀软会弹
    拦截窗并建议阻止。两参数从源头关闭组件更新与后台流量，BITS 不再创建，
    本地 UI 不受影响（本项目数据全走后端 httpx）。进程级固定：同一
    user-data 目录下所有 WebView2 环境参数必须一致。
    """
    key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    if not os.environ.get(key):
        os.environ[key] = "--disable-component-update --disable-background-networking"


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 桌面启动器")
    parser.add_argument("--server", action="store_true", help="无窗口模式，仅启动本地服务")
    # 换装进程入口（见 _run_update_helper）。⚠️ 必须出现在 --help 里：
    # 老版本靠 --help 探测暂存包是否支持安全换装（见 _staged_supports_helper）。
    parser.add_argument(
        _HELPER_FLAG, action="store_true",
        help="执行待安装的更新后退出（由应用内更新流程自动调用）",
    )
    parser.add_argument("--staging", help=argparse.SUPPRESS)
    parser.add_argument("--target", help=argparse.SUPPRESS)
    parser.add_argument("--wait-pid", type=int, default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    # 换装进程优先分流：不挂孤儿防线、不协商端口、不起服务与窗口——只做换装
    if getattr(args, "apply_update"):
        if not args.staging or not args.target:
            print("[更新] 换装进程缺少 --staging/--target，退出。")
            return
        _run_update_helper(Path(args.staging), Path(args.target), args.wait_pid)
        return

    # 孤儿实例防线最早挂上：换装/端口协商/服务线程任何阶段包装进程死亡，
    # 本进程都不应存活成无主实例（防线语义见 _watch_launcher 顶注）
    _watch_launcher()

    # WebView2 静态参数须在任何窗口创建前就位（webview.start 建环境时读取）
    _apply_webview2_static_args()

    # 换装：暂存就绪则把换装交给暂存包的新 exe（本进程立即退出，不再起服务）；
    # 交接失败（旧版暂存包/拉不起来）就照常启动现装，下次启动再试
    if _handoff_pending_update():
        sys.stdout.flush()
        os._exit(0)

    # 换装残留清理：已消费的暂存目录（无 manifest）+ 上一版程序备份 __old__
    _cleanup_staging_leftover()
    _stale_old_dir = _app_root() / "__old__"
    if _should_clean_old_dir(_stale_old_dir):
        _cleanup_old_dir_async(_stale_old_dir)

    # 启动期端口协商（已运行实例检测在前）：被占自动避让/唤醒分流
    _negotiate_ports()

    lock = _acquire_lock()
    if lock is None:
        # 锁没拿到：真实例（探活命中→置前唤醒）或外来占坑（降级告警）
        if _probe_running():
            print(f"{APP_NAME} 已在运行。")
            if not _focus_running_window():
                # 无窗口实例（--server 模式）：才退到浏览器唤醒
                webbrowser.open(f"http://{SERVER_HOST}:{SERVER_PORT}")
            return
        # 锁位被外来程序占坑且无真实例：协商已选好服务端口，降级运行
        print(f"[警告] 锁端口 {LOCK_PORT} 被其他程序占用，单实例保护降级为探活判定。")

    ready = threading.Event()
    error_box: list[str] = []
    threading.Thread(target=_start_server, args=(ready, error_box), daemon=True).start()

    url = os.environ.get(f"{ENV_PREFIX}DEV_URL") or f"http://{SERVER_HOST}:{SERVER_PORT}"
    if args.server:
        if not _wait_health(HEALTH_TIMEOUT):
            detail = "".join(error_box)
            print("服务启动失败：")
            if detail:
                print(detail)
            else:
                print(f"{HEALTH_TIMEOUT:.0f}s 内健康检查未通过")
            sys.exit(1)
        print(f"服务已就绪：{url}（Ctrl+C 退出）")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    # 窗口模式：窗口初始化（CLR/WebView2）与后端 lifespan 并行——窗口先载
    # 内联等待页（不经网络，后端未监听也不受影响），health 就绪后整窗跳转
    # 真实应用（_wait_and_navigate）
    def _watchdog() -> None:
        """后台看门狗：HEALTH_TIMEOUT 内后端仍不可用则弹窗报错（窗口模式下
        等待页会一直转圈，用户需要明确的失败反馈）。"""
        if not _wait_health(HEALTH_TIMEOUT):
            detail = "".join(error_box)
            hint = ""
            if "10048" in detail or "already in use" in detail.lower():
                hint = f"\n\n[提示] 端口 {SERVER_PORT} 绑定失败，疑似被其他程序占用。"
            if not detail and _server_port_listening():
                hint = (
                    "\n\n[提示] 服务进程在监听，但健康探测被拦截——疑似"
                    "代理/加速器类软件（系统代理或 TUN 模式）劫持了本机回环流量。"
                    "关闭其系统代理/TUN 后重试，或重启应用。"
                )
            _alert(
                f"服务启动失败（{HEALTH_TIMEOUT:.0f}s 内健康检查未通过）。{hint}\n\n"
                + (detail[-1500:] if detail else "详细错误见 data/logs 日志。")
            )
            print("服务启动失败：")
            if detail:
                print(detail)

    threading.Thread(target=_watchdog, daemon=True).start()
    _open_window(url)


if __name__ == "__main__":
    main()
