"""Holdexar 桌面启动器。

流程：单实例锁（被占先探活辨真伪）→ 后台线程 uvicorn → pywebview 窗口首载内联等待页（零网络依赖）→ health 就绪后整窗跳转真实应用（失败降级系统浏览器）。

用法：
    python desktop/main.py                # 桌面窗口模式
    python desktop/main.py --server       # 无窗口（仅本地服务）
    HOLDEXAR_DEV_URL=http://localhost:5173 python desktop/main.py    # 加载 Vite 开发服务器
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
    """windowed 打包（console=False）/ pythonw 下无标准句柄：sys.stdout/stderr 为 None。

    uvicorn 默认日志格式器初始化即调 sys.stdout.isatty()，None 下当场
    AttributeError → dictConfig 包成 ValueError: Unable to configure
    formatter 'default'，服务永远起不来。开发机从 shell 拉起会继承句柄，
    掩盖此坑——冒烟必须 cmd start / Start-Process 脱离句柄才忠实。
    换成 devnull 流后 print/flush/isatty/StreamHandler 后续全部安全；
    控制台模式两流本就存在，原样不受影响。
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
from app.core.app_info import APP_NAME, APP_SLUG, ENV_PREFIX  # noqa: E402
from app.core.paths import data_dir_filename, is_frozen, resolve_data_dir  # noqa: E402

# WebView2 Runtime（Evergreen 固定产品 GUID）注册表探测 + 官方离线安装链
_WEBVIEW2_REG_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)
_WEBVIEW2_DL_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

# 启动等待页：pywebview 内联 HTML（create_window(html=...)，零网络依赖）。
# 窗口出现即显示转圈页，后端 health 就绪后由 _wait_and_navigate 整窗跳转
# 真实应用——窗口初始化（CLR/WebView2 ~1s）与后端 lifespan（数秒，首装
# 导入种子更久）并行重叠。等待页不能依赖后端：uvicorn 要等 lifespan 跑完
# 才监听端口，此窗口期加载同源等待页一律连接被拒（WebView2 直接显示
# 「网页加载失败」错误页，等页里的 JS 轮询根本没机会跑）；data-URL 方案
# 又被 WebView2 跨源策略拦截对 127.0.0.1 的 fetch（双击冒烟实证卡死）。
# 内联 html= 两者皆避：不经网络加载，也就没有加载失败可言。
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
# 健康检查等待上限。**不得低于首装导入耗时**：全新安装时随包资产种子
# （23 万行汇率档案）单事务并入本地库实测约 25s，恰好压在旧值 25s 死线上
# ——新用户第一次启动会看到「服务启动失败」误报弹窗（服务其实是好的）。
# 90s 覆盖低端机/机械盘的更慢情形；服务真起不来时也只是晚点报错。
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

    只看状态码会把「恰好占着同一端口的别的本地服务」误判成本应用
    实例——唤醒分支随后会去置前/浏览器打开**对方的页面**。health
    载荷自带 {"app": APP_NAME, ...}（system/router.py），比对字段
    即锚定身份；非 JSON / 缺字段 / 异名一律视为外来占坑。
    """
    if getattr(response, "status_code", None) != 200:
        return False
    try:
        return response.json().get("app") == APP_NAME
    except Exception:  # noqa: BLE001 —— 非 JSON 响应一律非本应用
        return False


def _probe_running(timeout_s: float = 8.0, quick: bool = False) -> bool:
    """轮询主端口健康端点，判定是否真有本应用实例在跑。

    锁端口被外来程序占用 ≠ 已有实例，以健康端点为准（含**身份校验**，
    见 _health_is_ours——端口撞车的别家应用不得被当成自己唤醒）；
    留重试窗口覆盖"双击两次图标、前者还在启动中"的竞态。
    quick=True 时单发一次（150ms 超时）——用于锁已被本进程持有、
    本地回环请求一律 trust_env=False 直连（同 _wait_health 顶部说明）。
    仅需排除竞态窗口的场景，避免正常冷启动路径白等数秒。
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

    - 服务端口被占：
      * 探活命中 = 本应用另一实例已在跑 → 保持原值，main() 会走"已在运行"
        分支（唤醒旧实例，不重复开）
      * 探活未命中 = 外来程序占坑 → frozen 态自默认端口起 +1 扫描可用位
        （绿色包普通用户无控制台可设环境变量，弹窗报错 = 软件坏了；
        开发态保持报错退出语义，端口漂移会掩盖配置问题）
    - 锁端口被占：先探活判定（真实例不会走到这里），未命中则依次尝试
      备选锁位（模块级 _LOCK_FALLBACKS），全占则告警后让位——单实例
      改由"服务端口探活"兜底判定。
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

    run.py 被硬杀（任务管理器 / taskkill / 脚本超时收割）时，Windows 不会
    连带收割子进程——desktop/main.py 存活成无主实例，继续占着服务端口与
    单实例锁，下一次启动便撞上「已在运行」或「端口被占用」，用户看到的
    就是「应用无法启动」。控制台正常关闭本就走整树收割，这里兜的是硬杀
    路径：watcher 线程等包装进程句柄，触发即整体退出。
    直接拉起（无 LAUNCHER_PID 环境）不设防——打包态 exe、--server 独立
    运行均属此类，本来就没有可监视的父进程。
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

    **本机回环请求必须 trust_env=False 直连**：httpx 在 Windows 上读注册表
    系统代理（Clash/加速器类常开），且其 no-proxy 判定按逗号分隔——注册表
    ProxyOverride 是分号分隔（localhost;127.*;…），整串被当成一个主机名，
    127.0.0.1 的例外**形同虚设**。开着系统代理时，发往本机的 health 探测
    会被转发到代理端口，服务明明已监听却探测不通——等待页永远转圈、
    watchdog 误报"服务启动失败"（实测复现）。HTTP(S)_PROXY 环境变量同理。
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

    watchdog 用它定位「health 超时」的责任方：TCP 通 + health 超时 =
    服务活着、探测链路被拦（代理/TUN 类软件劫持本机回环的残留形态）；
    TCP 不通 = 服务进程真没起来，走原始报错。
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

    _LOGIN_URL = "https://store.steampowered.com/login/"
    _LOGIN_TIMEOUT_S = 300
    _COOKIE_KEYS = ("steamLoginSecure", "sessionid", "steamCountry")

    # 登录窗 UX 助手注入脚本（evaluate_js，每次完整导航后在 events.loaded 重注入；
    # Steam 登录页 CSP default-src 带 'unsafe-inline'，DOM+CSSOM 注入不受限）。
    # 占位符（_login_helper_js 替换）：
    #   __SHOW_TOAST__ → true/false（代理气泡只展示一次）
    #   __KNOWN__      → JSON：本机已绑定账号 [{steam_id, name}]（步骤高亮旁的身份侧栏）
    _LOGIN_HELPER_JS = r"""
(function () {
  var SHOW_TOAST = __SHOW_TOAST__;
  var KNOWN = __KNOWN__;
  function mk(tag, styles, text) {
    var el = document.createElement(tag);
    for (var k in styles) el.style[k] = styles[k];
    if (text) el.textContent = text;
    return el;
  }

  /* 代理提示气泡：一次性，4.5s 自动淡出；pointer-events:none 不挡登录表单 */
  if (SHOW_TOAST && !window.__hlxToastShown) {
    window.__hlxToastShown = true;
    var toast = mk('div', {
      position: 'fixed', top: '14px', left: '50%', transform: 'translateX(-50%)',
      zIndex: '2147483647', pointerEvents: 'none',
      background: 'rgba(23, 26, 33, 0.92)', color: '#dcdedf',
      border: '1px solid rgba(255, 255, 255, 0.14)', borderRadius: '10px',
      padding: '9px 18px', fontSize: '13px',
      fontFamily: "'Motiva Sans', 'Segoe UI', Arial, sans-serif",
      boxShadow: '0 6px 24px rgba(0, 0, 0, 0.35)',
      opacity: '0', transition: 'opacity 0.4s ease',
    });
    toast.appendChild(mk('span', { color: '#5ba32b', marginRight: '7px', fontSize: '10px' }, '●'));
    toast.appendChild(document.createTextNode('已使用代理加速网页加载'));
    document.body.appendChild(toast);
    requestAnimationFrame(function () { toast.style.opacity = '1'; });
    setTimeout(function () {
      toast.style.opacity = '0';
      setTimeout(function () { toast.remove(); }, 500);
    }, 4500);
  }

  /* ── 步骤进度感知 ──────────────────────────────────────────
     锚点取自登录页 DOM 结构（React 登录页复用经典 ID，常驻 DOM、
     不可见，步骤激活时变可见）：
       1 账密    input[type=password] 可见
       2 人机    div#captchaImg / iframe[title*="验证"] 可见
       3 Guard   input#authcode / input#twofactorcode_entry 可见
       4 等确认  成功按钮可见（#success_continue_btn）或已无输入框 */
  var STEPS = [
    { sel: 'input[type=password]', hint: '账密' },
    { sel: '#captchaImg, iframe[title*="验证"], iframe[title*="recaptcha"], iframe[title*="challenge"]', hint: '人机' },
    { sel: '#authcode, #twofactorcode_entry', hint: 'Guard' },
    { sel: '#success_continue_btn', hint: '完成' },
  ];
  function visible(el) {
    return !!el && !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  }
  function activeStep() {
    for (var i = 0; i < STEPS.length; i++) {
      try {
        var els = document.querySelectorAll(STEPS[i].sel);
        for (var j = 0; j < els.length; j++) {
          if (visible(els[j])) return i;
        }
      } catch (e) { /* 选择器不适用（跨域 iframe 无法内探）——外层可见性已够 */ }
    }
    // 任何锚点都不可见：页面加载中/失败或二维码展开态，无法判定 → -1 保持现状态。
    // React 表单未挂载时全锚点缺席，穿透返"完成"会误报
    return -1;
  }

  /* 右下角验证教程卡：✕ 关闭置 __hlxHelpClosed，Python 侧回读后本次登录窗不再重注入 */
  if (window.__hlxHelpClosed || document.getElementById('hlx-login-help')) return;
  var card = mk('div', {
    position: 'fixed', right: '16px', bottom: '16px', width: '292px',
    zIndex: '2147483647',
    background: 'rgba(27, 40, 56, 0.96)', color: '#cfd8e3',
    border: '1px solid rgba(255, 255, 255, 0.15)', borderRadius: '12px',
    padding: '13px 15px',
    boxShadow: '0 10px 34px rgba(0, 0, 0, 0.45)',
    fontFamily: "'Motiva Sans', 'Segoe UI', Arial, sans-serif",
  });
  card.id = 'hlx-login-help';

  var head = mk('div', { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '8px' });
  head.appendChild(mk('span', { fontSize: '13.5px', fontWeight: '700', color: '#ffffff' }, '登录验证指南'));
  var close = mk('button', {
    background: 'transparent', border: 'none', color: '#8f98a0',
    fontSize: '13px', cursor: 'pointer', padding: '2px 5px', lineHeight: '1',
  }, '✕');
  close.addEventListener('click', function () {
    window.__hlxHelpClosed = true;
    card.remove();
  });
  close.addEventListener('mouseenter', function () { close.style.color = '#ffffff'; });
  close.addEventListener('mouseleave', function () { close.style.color = '#8f98a0'; });
  head.appendChild(close);
  card.appendChild(head);

  /* 当前步骤徽标（"当前：账密"），随 DOM 轮询更新 */
  var stepBadge = mk('div', {
    marginBottom: '8px', fontSize: '11.5px', fontWeight: '700', letterSpacing: '0.4px',
    color: '#66c0f4',
  }, '当前：账密');
  card.appendChild(stepBadge);

  /* 步骤行：完成=绿勾、当前=高亮圈、待做=灰圈；随进度轮询移动样式 */
  var STEP_TEXTS = [
    '输入 Steam 账号密码登录',
    '遇到人机验证（拼图 / 勾选）按提示完成，失败可多点几次重试',
    '打开手机 Steam App 确认登录（Steam Guard）',
    '确认时按顺序核对：我的网页浏览器（Chrome、Edge 等）、网站登录 IP、其他',
    '验证通过后本窗口自动关闭并回传登录',
  ];
  var rows = [];
  STEP_TEXTS.forEach(function (text, i) {
    var row = mk('div', { display: 'flex', gap: '8px', margin: '6px 0', fontSize: '12.5px', lineHeight: '1.55', transition: 'opacity 0.3s ease' });
    var n = mk('span', {
      flexShrink: '0', width: '17px', height: '17px', marginTop: '2px',
      borderRadius: '50%', background: 'rgba(102, 192, 244, 0.16)', color: '#66c0f4',
      fontSize: '10.5px', fontWeight: '700', display: 'flex',
      alignItems: 'center', justifyContent: 'center',
    }, String(i + 1));
    row.appendChild(n);
    row.appendChild(mk('span', null, text));
    card.appendChild(row);
    rows.push({ row: row, num: n });
  });

  /* 文案 5 条 vs 感知步骤 4 段：activeStep 0=账密 1=人机 2=Guard 3=完成。
     文案行 2（打开手机 App 确认）与行 3（核对确认信息）同属 Guard 段，
     段内一起高亮；完成段高亮尾行（成功瞬间的最后指引）。 */
  function paint(cur) {
    var hi, doneUpTo;
    if (cur >= 3) { hi = 4; doneUpTo = 4; }
    else if (cur === 2) { hi = 2; doneUpTo = 2; }
    else if (cur === 1) { hi = 1; doneUpTo = 1; }
    else { hi = 0; doneUpTo = -1; }
    rows.forEach(function (r, i) {
      if (i < doneUpTo) {
        /* 已过步骤：绿勾淡显 */
        r.num.style.background = 'rgba(91, 163, 43, 0.18)';
        r.num.style.color = '#a3cf06';
        r.num.textContent = '✓';
        r.row.style.opacity = '0.55';
      } else if (i === hi || (cur === 2 && i === 3)) {
        /* 当前步骤：高亮圈 + 不透明（Guard 段行 2/3 同亮） */
        r.num.style.background = 'rgba(102, 192, 244, 0.28)';
        r.num.style.color = '#66c0f4';
        r.num.textContent = String(i + 1);
        r.row.style.opacity = '1';
      } else {
        /* 待做：灰圈半透明 */
        r.num.style.background = 'rgba(255, 255, 255, 0.08)';
        r.num.style.color = '#8f98a0';
        r.num.textContent = String(i + 1);
        r.row.style.opacity = '0.55';
      }
    });
    stepBadge.textContent = '当前：' + ['账密', '人机验证', 'Steam Guard', '即将完成'][cur];
  }
  var curShown = -1;
  function repaint() {
    var cur = activeStep();
    if (cur === -1 || cur === curShown) return;  // 未知态保持现状态；同态不重绘
    curShown = cur;
    paint(cur);
  }
  repaint();
  if (!window.__hlxStepTimer) {
    window.__hlxStepTimer = setInterval(function () {
      var c = document.getElementById('hlx-login-help');
      if (!c || window.__hlxHelpClosed) {
        clearInterval(window.__hlxStepTimer);
        window.__hlxStepTimer = null;
        return;
      }
      repaint();
    }, 1200);
  }

  /* 本机已绑定账号侧栏：判定"登的是哪个号"的参照（来自后端账号表，只读展示） */
  if (KNOWN && KNOWN.length) {
    var box = mk('div', {
      marginTop: '9px', paddingTop: '8px',
      borderTop: '1px solid rgba(255, 255, 255, 0.12)',
      fontSize: '11.5px', color: '#8f98a0', lineHeight: '1.6',
    });
    box.appendChild(mk('div', { marginBottom: '3px', color: '#cfd8e3', fontWeight: '700' }, '本机已绑定账号'));
    KNOWN.forEach(function (a) {
      var line = mk('div', null, a.name || a.steam_id.slice(-10));
      line.title = a.steam_id;
      box.appendChild(line);
    });
    box.appendChild(mk('div', { marginTop: '3px' }, '登录其他账号 = 新增绑定，不影响以上账号'));
    card.appendChild(box);
  }

  card.appendChild(mk('div', {
    marginTop: '9px', paddingTop: '8px',
    borderTop: '1px solid rgba(255, 255, 255, 0.12)',
    fontSize: '11.5px', color: '#8f98a0', lineHeight: '1.5',
  }, '一直加载失败？稍后重试，或到「代理管理」换个节点再登录。'));

  document.body.appendChild(card);

  /* 登录成功横幅：window.__hlxLoginDone(payload) 由 Python 侧在 Cookie 命中后
     调用（evaluate_js），置绿"登录成功，正在回传"——用户能看见窗口为何关闭 */
  window.__hlxLoginDone = function (payload) {
    var c = document.getElementById('hlx-login-help');
    if (!c) return;
    var who = '';
    try {
      var p = typeof payload === 'string' ? JSON.parse(payload) : payload;
      if (p && p.name) who = p.name;
      else if (p && p.steam_id) who = p.steam_id.slice(-10);
    } catch (e) { /* 展示降级，不影响回传 */ }
    var bar = mk('div', {
      margin: '0 -15px -13px', padding: '12px 15px',
      background: 'rgba(91, 163, 43, 0.22)',
      borderTop: '1px solid rgba(163, 207, 6, 0.35)',
      borderRadius: '0 0 12px 12px',
      fontSize: '12.5px', fontWeight: '700', color: '#a3cf06', textAlign: 'center',
    }, who ? ('登录成功：' + who + '，正在回传…') : '登录成功，正在回传…');
    c.appendChild(bar);
    paint(3);
  };
})();
"""

    @staticmethod
    def _login_helper_js(show_toast: bool, known_accounts: list[dict] | None = None) -> str:
        """助手注入脚本：占位替换。

        __SHOW_TOAST__：气泡只在代理注入成功且首次加载时展示；
        __KNOWN__：本机已绑定账号（教程卡侧栏参照，展示用最小字段）。
        """
        known = [
            {"steam_id": str(a.get("steam_id") or ""), "name": str(a.get("persona_name") or "")}
            for a in (known_accounts or [])
        ]
        return (
            DesktopApi._LOGIN_HELPER_JS.replace(
                "__SHOW_TOAST__", "true" if show_toast else "false"
            )
            .replace("__KNOWN__", json.dumps(known, ensure_ascii=False))
        )

    # 登录窗必需 Steam 域（探活集）：只探页面域不够——页面能从国内 CDN 打开
    # ≠ 能登录。api.steampowered.com（IAuthenticationService，二维码轮询/密码
    # 认证）与 community.steam-api.com（二维码确认 websocket）任一不通，页面
    # 照常渲染但登录动作会永远挂起。
    _LOGIN_PROBE_HOSTS = (
        "https://login.steampowered.com/jwt/refresh",
        "https://api.steampowered.com/ISteamWebAPIUtil/GetServerInfo/v1/",
        "https://community.steam-api.com/",
    )
    _LOGIN_PROBE_HOST_MINIMAL = ("https://login.steampowered.com/jwt/refresh",)

    @staticmethod
    def _sid_from_login_secure(value: str) -> str:
        """steamLoginSecure 值 → SteamID64（`<sid>||<token>` 前段，兼容 URL 编码
        %7C%7C；纯数字值直接判）。与服务端 steam_id_from_cookies 同口径，桌面
        侧独立实现——登录窗要拿 ID 判"是哪个账号/新号还是换绑"，不能为这一步
        import 服务端模块（打包态路径耦合）。
        """
        v = (value or "").strip()
        for sep in ("%7C%7C", "||"):
            if sep in v:
                head = v.split(sep, 1)[0].strip()
                return head if head.isdigit() else ""
        return v if v.isdigit() else ""

    @staticmethod
    def _probe_channel(proxy_url: str | None, hosts: tuple[str, ...], timeout: float = 4.0) -> bool:
        """通道探活：对 hosts 并发各发一笔 GET，**全部**拿到响应才算通。

        任意 HTTP 状态码（302/404/4xx/5xx）都证明 TLS+HTTP 可达；异常/超时
        即不通。trust_env=False 保证探的就是这条通道本身（不吃环境代理）。
        """
        def _hit(url: str) -> bool:
            try:
                kwargs: dict = {"timeout": timeout, "trust_env": False, "follow_redirects": False}
                if proxy_url:
                    kwargs["proxy"] = proxy_url
                httpx.get(url, **kwargs)
                return True
            except Exception:  # noqa: BLE001 —— 连接失败/超时/代理断均算不通
                return False

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=len(hosts)) as pool:
            return all(pool.map(_hit, hosts))

    def restart_app(self) -> dict:
        """前端「重启以完成更新」入口：新进程拉起自己，本进程即刻退出。

        js_api 调用在独立线程，os._exit 安全；重启后 main() 的
        _handoff_pending_update 分支把换装交给暂存包的新 exe 执行
        （见该函数 docstring：换装必须等旧进程退出，不能在跑着的进程里动目录）。
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

    @staticmethod
    def _resolve_login_channel() -> tuple[str, str | None, bool]:
        """选择登录窗网络通道，返回 (mode, proxy_url, full_ok)。

        mode：
          - 'proxy'  : 引擎代理通过探活 → env 注入 --proxy-server
          - 'direct' : 代理不可用但真直连通过 → env 注入 --no-proxy-server
            （WebView2 默认继承系统代理；用户开着 Verge 系统代理且节点死时，
            "直连回退"会悄悄变成死路由——强制绕过才是真直连）
          - 'system' : 两通道连登录域都不通 → 不动任何参数，页面能否打开听
            天由命（教程卡底部"换个节点"提示兜底）
        full_ok：认证域全套通过。False = 只有登录域通（api/社区域仍被墙，
        页面能开但登录会卡）——此时**不得**展示"已使用代理加速"气泡，半残
        通道不邀功。

        顺序遵循 proxy_first：proxy 全套 → direct 全套 → proxy 登录域 →
        direct 登录域 → system。探活目标用认证端点而非页面 URL（页面域国内
        CDN 可达 ≠ 登录可用，第一版只探页面域正是这次误判根因）。
        """
        import logging as _logging

        log = _logging.getLogger("holdexar.desktop.login")

        proxy_url: str | None = None
        try:
            # 本机回环请求直连（trust_env=False），不吃系统代理——同 _wait_health
            response = httpx.get(
                f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/proxies/resolve",
                timeout=3.0,
                trust_env=False,
            )
            data = response.json() if response.status_code == 200 else {}
            proxy_url = data.get("proxyUrl") or None
        except Exception:  # noqa: BLE001 —— 解析失败按无代理继续走直连分支
            proxy_url = None
        # 带认证的池代理 WebView2 启动参数不支持（--proxy-server 无凭据位），视同无代理
        if proxy_url and "@" in proxy_url:
            proxy_url = None

        if proxy_url and DesktopApi._probe_channel(proxy_url, DesktopApi._LOGIN_PROBE_HOSTS):
            return "proxy", proxy_url, True
        if DesktopApi._probe_channel(None, DesktopApi._LOGIN_PROBE_HOSTS):
            return "direct", None, True
        if proxy_url and DesktopApi._probe_channel(proxy_url, DesktopApi._LOGIN_PROBE_HOST_MINIMAL):
            log.info("登录窗代理仅登录域可达（认证域不全），降级使用")
            return "proxy", proxy_url, False
        if DesktopApi._probe_channel(None, DesktopApi._LOGIN_PROBE_HOST_MINIMAL):
            log.info("登录窗直连仅登录域可达（认证域不全），降级使用")
            return "direct", None, False
        log.info("登录窗代理与直连探活均失败，按系统默认通道加载")
        return "system", None, False

    def start_steam_login(self) -> dict:
        """打开 Steam 登录子窗口，轮询 Cookie；登录成功自动回传三件套。

        pywebview 5.4+ EdgeChromium（WebView2）后端可经 CookieManager 读到
        httpOnly 的 steamLoginSecure——即 webapi_token 的 Cookie 载体。
        轮询到即关窗返回；超时/用户关窗返回错误。js_api 调用在独立线程，
        阻塞安全。

        ⚠️ get_cookies 返回 SimpleCookie 列表（每个元素含一个 Morsel），
        Morsel 的名字在 .key / ['name']，不在 .name——直接取 .name 会
        全部落空、永远识别不到 steamLoginSecure（修过，勿回退）。

        通道选择（见 _resolve_login_channel）：开窗前对**认证域
        全套**（login / api / community-api）并发探活选通道——proxy 全套 →
        真直连全套 → proxy 登录域 → 直连登录域 → 系统默认。只探页面域不够：
        store 页面国内 CDN 能开 ≠ 能登录（api.steampowered.com 直连被墙时
        页面照常渲染但二维码轮询/密码认证永远挂起，首轮实测踩坑）。选 direct
        时注入 --no-proxy-server 强制绕过系统代理。

        窗内 UX 助手：代理**全套通过**时展示「已使用代理加速
        网页加载」气泡（仅首次加载，降级通道不展示）；右下角常驻「登录验证
        指南」教程卡（可 ✕ 关闭，关闭后本次登录窗不再重现）。经
        events.loaded + evaluate_js 注入，见 _LOGIN_HELPER_JS。

        教程卡进度感知 + 账号判定：卡内 1.2s 轮询登录页 DOM
        锚点（password / 人机验证 / #authcode·#twofactorcode_entry / 成功钮），
        实时高亮当前步骤；开窗前拉 /account/list 把已绑定账号列进卡侧栏，
        登录的是哪个号一眼可比对；Cookie 命中即提取 SteamID64（steamLoginSecure
        首段，与服务端 steam_id_from_cookies 同口径）展示「登录成功：<账号>，
        正在回传」横幅后再关窗，返回值带 steam_id 供前端绑定提示复用。

        技术约束：同一 user-data 目录下所有 WebView2 环境的浏览器参数必须
        一致，主窗口已按直连参数创建 → 登录窗必须换独立 user-data 目录
        才能带 --proxy-server 参数拉起自己的浏览器进程；pywebview 5.4 无
        按窗口 storage API，临时换 winforms 模块级 cache_dir（5.4 实测源码
        全局单值，升级 pywebview 需复核）。
        """
        import logging as _logging
        import tempfile as _tempfile
        import time as _time

        import webview

        log = _logging.getLogger("holdexar.desktop.login")

        # ── 通道选择：代理全套 → 真直连全套 → 代理登录域 → 直连登录域 → 系统默认 ──
        mode, proxy_url, channel_full = self._resolve_login_channel()
        env_key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
        saved_env = os.environ.get(env_key)
        saved_cache: str | None = None
        login_cache_dir: str | None = None
        if mode in ("proxy", "direct"):
            # proxy/direct 都要动浏览器参数；同一 user-data 目录下 WebView2 参数
            # 必须一致 → 登录窗换独立会话目录拉起自己的浏览器进程
            os.environ[env_key] = (
                f"--proxy-server={proxy_url}" if mode == "proxy" else "--no-proxy-server"
            )
            try:
                import webview.platforms.winforms as _wf

                saved_cache = _wf.cache_dir
                login_cache_dir = _tempfile.mkdtemp(prefix="holdexar-login-")
                _wf.cache_dir = login_cache_dir
                if mode == "proxy":
                    log.info("登录窗经代理加载：%s（独立会话目录）", proxy_url)
                else:
                    log.info("登录窗真直连加载（--no-proxy-server，独立会话目录）")
            except Exception:  # noqa: BLE001 —— 换不了目录就撤参数（同目录参数不一致会让 WebView2 初始化失败）
                log.exception("登录窗独立会话目录创建失败，撤回参数按系统默认加载")
                if saved_env is None:
                    os.environ.pop(env_key, None)
                else:
                    os.environ[env_key] = saved_env
                mode = "system"
        else:
            log.info("登录窗按系统默认通道加载（代理与直连探活均失败）")

        try:
            login_window = webview.create_window(
                "Steam 登录 · 登录成功后自动返回",
                self._LOGIN_URL,
                width=1000,
                height=760,
                on_top=True,
            )
        except Exception as e:  # noqa: BLE001
            self._restore_login_env(env_key, saved_env, saved_cache, login_cache_dir)
            return {"ok": False, "error": f"无法打开登录窗口: {e}"}

        # ── 窗内 UX 助手（代理气泡 + 验证教程卡）──
        # events.loaded 每次完整导航后触发 → 重注入（Steam 登录流程是页内 SPA，
        # 步骤切换不清空注入；全量跳转后靠本回调恢复）。教程卡 ✕ 关闭后由
        # 轮询回读 __hlxHelpClosed 停止重注入。
        helper_state = {"toast_shown": False, "dismissed": False}

        # 已绑定账号侧栏数据：本进程后端 /account/list（桌面进程与服务同源，
        # 打包态亦在本机）；失败静默降级为无侧栏，不阻断登录窗
        known_accounts: list[dict] = []
        try:
            # 本机回环请求直连（trust_env=False），不吃系统代理——同 _wait_health
            resp = httpx.get(
                f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/account/list",
                timeout=3.0,
                trust_env=False,
            )
            if resp.status_code == 200:
                known_accounts = [
                    {"steam_id": a.get("steam_id", ""), "persona_name": a.get("persona_name", "")}
                    for a in resp.json()[:6]
                ]
        except Exception:  # noqa: BLE001 —— 拉不到只影响卡侧栏展示
            known_accounts = []
        known_name_by_sid = {
            a["steam_id"]: a["persona_name"] for a in known_accounts if a.get("steam_id")
        }

        def _inject_login_helper() -> None:
            if helper_state["dismissed"]:
                return
            # 气泡只属于"代理且认证域全套通过"——半残通道（登录域-only）或直连
            # 不展示，避免页面能开但登录卡死时还邀功
            show_toast = mode == "proxy" and channel_full and not helper_state["toast_shown"]
            try:
                login_window.evaluate_js(self._login_helper_js(show_toast, known_accounts))
                if show_toast:
                    helper_state["toast_shown"] = True
            except Exception:  # noqa: BLE001 —— 导航窗口期注入失败属预期，下次 loaded 重试
                log.info("登录窗助手注入暂未成功（页面导航中，待下次加载重试）")

        login_window.events.loaded += _inject_login_helper
        if login_window.events.loaded.is_set():
            _inject_login_helper()  # 竞态兜底：handler 挂载前页面已加载完

        def _extract(jars: list) -> dict:
            """SimpleCookie 列表 → {name: value}；兼容 Morsel 与 dict 两种形态。"""
            named: dict[str, str] = {}
            for jar in jars or []:
                items = []
                if hasattr(jar, "values"):  # SimpleCookie
                    items = list(jar.values())
                elif isinstance(jar, dict):
                    items = [jar]
                for m in items:
                    name = (
                        getattr(m, "key", None)
                        or (m.get("name") if hasattr(m, "get") else None)
                    )
                    value = (
                        getattr(m, "value", None)
                        or (m.get("value") if hasattr(m, "get") else None)
                    )
                    if name in self._COOKIE_KEYS and value:
                        named[str(name)] = str(value)
            return named

        deadline = _time.time() + self._LOGIN_TIMEOUT_S
        cookie_str = ""
        login_sid = ""
        poll_errors = 0
        poll_count = 0
        while _time.time() < deadline:
            if login_window.events.closed.is_set():
                break
            poll_count += 1
            # 教程卡关闭态回读（✕ 后本次登录窗不再随页面重载重现；导航中读取失败静默）
            if not helper_state["dismissed"] and poll_count % 8 == 0:
                try:
                    if login_window.evaluate_js("window.__hlxHelpClosed === true"):
                        helper_state["dismissed"] = True
                except Exception:  # noqa: BLE001
                    pass
            try:
                named = _extract(login_window.get_cookies() or [])
                if named.get("steamLoginSecure"):
                    cookie_str = "; ".join(
                        f"{k}={named[k]}" for k in self._COOKIE_KEYS if named.get(k)
                    )
                    # ── 账号判定：steamLoginSecure 值首段即 SteamID64（`<sid>%7C%7C<token>` /
                    #    `||` 双形态，与服务端 steam_id_from_cookies 同口径）。有 ID 先在
                    #    窗内亮「登录成功：<账号>」横幅（映射本机已绑定的昵称，陌生号显示
                    #    ID 尾 10 位），短暂停留后关窗回传。 ──
                    login_sid = self._sid_from_login_secure(named["steamLoginSecure"])
                    try:
                        who = known_name_by_sid.get(login_sid) or (
                            login_sid[-10:] if login_sid else ""
                        )
                        login_window.evaluate_js(
                            "window.__hlxLoginDone && window.__hlxLoginDone(%s)"
                            % json.dumps(
                                {"steam_id": login_sid, "name": who}, ensure_ascii=False
                            )
                        )
                    except Exception:  # noqa: BLE001 —— 横幅失败不影响回传
                        pass
                    _time.sleep(1.8)
                    break
            except Exception as e:  # noqa: BLE001 —— 页面跳转中偶发不可读
                poll_errors += 1
                if poll_errors <= 3 or poll_errors % 25 == 0:
                    log.info("登录 Cookie 轮询异常（第 %d 次）：%s", poll_errors, e)
            _time.sleep(1.2)

        try:
            login_window.destroy()
        except Exception:  # noqa: BLE001
            pass
        self._restore_login_env(env_key, saved_env, saved_cache, login_cache_dir)
        if cookie_str:
            result = {"ok": True, "cookies": cookie_str}
            if login_sid:
                result["steam_id"] = login_sid
                result["is_new"] = login_sid not in known_name_by_sid
            return result
        return {"ok": False, "error": "未检测到登录成功（超时 5 分钟或窗口被关闭）"}

    @staticmethod
    def _restore_login_env(
        env_key: str,
        saved_env: str | None,
        saved_cache: str | None,
        login_cache_dir: str | None,
    ) -> None:
        """登录窗收尾：还原 WebView2 启动参数与 winforms cache_dir，清临时会话目录。

        临时目录此时可能仍被浏览器进程占用（销毁异步），删除失败静默——
        留给系统临时目录清理，不影响后续登录窗。
        """
        if saved_env is None:
            os.environ.pop(env_key, None)
        else:
            os.environ[env_key] = saved_env
        if saved_cache is not None:
            try:
                import webview.platforms.winforms as _wf

                _wf.cache_dir = saved_cache
            except Exception:  # noqa: BLE001
                pass
        if login_cache_dir:
            import shutil as _shutil

            try:
                _shutil.rmtree(login_cache_dir, ignore_errors=True)
            except Exception:  # noqa: BLE001
                pass


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
    "update-staging",  # 暂存目录自身
}

# 换装助手入口标记：暂存包的新 exe 报出这个长选项才允许自动换装（见探测函数）
_HELPER_FLAG = "--apply-update"
# 不支持安全换装的暂存包落盘标记：避免每次启动重复探测与弹窗
_UNSUPPORTED_MARK = ".handoff-unsupported"


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

    用**载荷自己的 exe**而不是「当前进程的 exe 名」定位：用户可能给主程序改过名
    （`Holdexar (1).exe`、带版本号的副本……），按当前名字硬找会把好好的更新包
    判成「缺少主程序」而拒换。换装后的启动目标也随之取载荷那个文件名。
    """
    candidates = sorted(p for p in payload.glob("*.exe") if p.is_file())
    if not candidates:
        return None
    same_name = payload / Path(sys.executable).name
    return same_name if same_name.is_file() else candidates[0]


def _staged_supports_helper(exe: Path) -> bool:
    """暂存包的新 exe 是否支持安全换装（`--apply-update`）。

    为什么必须探测：换装要在**旧进程退出后**由暂存包的新 exe 执行；已发布的老
    版本没有这个入口，硬换装只能走老逻辑——它在运行中的程序目录上对装载中的
    `_internal` 做 `shutil.move`：`os.rename` 被 Windows 拒绝（WinError 5，实测），
    `shutil.move` 遂静默降级为 copytree+rmtree，只复制得动未被占用的文件、再把
    原目录删剩被占用的那些（实测 `_internal` 1319 个文件剩 32 个，安装半截化）。
    探测失败即拒换：宁可不更新，也不能毁掉现装。
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

    为什么不在本进程里换装（v0.1.0 的老做法，实测毁安装）：本进程正从
    `_internal` 装载 DLL，而该目录在运行中无法整体 `os.rename`（WinError 5）。
    换装进程等本进程的句柄消失后再动文件，那时目录没有任何占用，改名与复制
    都是普通操作。

    返回 True = 已交接（调用方必须立即退出，别再起 uvicorn 与窗口）。
    """
    if not is_frozen():
        return False

    staging = _data_dir() / "update-staging"
    payload = staging / APP_NAME
    manifest = staging / "manifest.json"
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

    只用两种搬运方式，别的一律不碰：
    - 旧条目**只做 os.replace 改名**（同卷原子）。绝不用 `shutil.move`——它在
      rename 失败时会静默降级成 copytree+rmtree，那正是毁安装的根源；
    - 新载荷**复制**落位（暂存目录在系统盘、程序目录可能在别的盘，跨卷没法改名）。
    任一步失败即整体回滚到原版本，程序目录始终保持可启动。
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

    # ④ 落位校验：少一个文件就是半截安装，宁可回滚
    #   （计数含顶层文件本身——主程序漏算过一次，把正常换装误判成了半截）
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
    (staging / "manifest.json").unlink(missing_ok=True)
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
        _alert(
            "[更新] 换装失败，已回滚到原版本（程序目录未被破坏，可直接使用）。\n\n"
            f"详情见 {_update_log()}；也可到发布页下载新版压缩包手动解压覆盖。"
        )
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


def _cleanup_old_dir_async(old_dir: Path) -> None:
    """后台线程清 __old__：sharing violation 重试（Windows 文件锁语义）。"""

    def _retry_delete() -> None:
        for _ in range(30):  # ~30s 窗口
            if shutil.rmtree(old_dir, ignore_errors=True) or not old_dir.exists():
                return
            time.sleep(1.0)

    threading.Thread(target=_retry_delete, daemon=True).start()


def _cleanup_staging_leftover() -> None:
    """清理已消费的暂存目录残留。

    换装进程自己就跑在暂存目录里，它的 exe 与 DLL 在退出前删不掉——换装成功后
    总会留下这一撮。判据是「没有 manifest」：带 manifest 的暂存是待换装的正经
    包，一个字节都不许动。
    """

    staging = _data_dir() / "update-staging"
    if not staging.is_dir() or (staging / "manifest.json").is_file():
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

    双击 / 菜单「显示主窗口」唤起；菜单「退出」才真退进程。常态下用户
    点 X = 隐藏窗口，后端（uvicorn + 调度器）随进程常驻继续更新数据。
    创建失败（理论上仅异常环境）返回 None：关窗守卫照常隐藏，恢复靠
    再次启动唤醒（_focus_running_window 已覆盖隐藏窗口）。
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
        if row and row[0] in ("dark", "light"):
            return str(row[0])
    except Exception:  # noqa: BLE001 —— 读不到按深色兜底
        pass
    return "dark"


def _brand_logo_path(theme: str = "dark") -> str | None:
    """弹窗标题栏 logo：**深色面用浅色（白色线条）版**，浅色面用深色版。

    命名语义：logo_dark / logo_light 指「给哪种主题用」——logo_dark 是
    深色主题下使用的白色线条版，logo_light 是浅色主题下的深色线条版，
    反了会糊进底色里看不见。打包态前端产物收在 web/dist/assets/（vite
    把 public/assets 原样复制），找不到回退窗口图标 _app_icon()。
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
    虚方法分派不生效（实测 override 不被回调），CLR→Python 只有事件通路
    （同托盘 NotifyIcon 的沉淀）。
    """
    from System.Windows.Forms import ControlStyles

    control.SetStyle(
        ControlStyles.UserPaint
        | ControlStyles.AllPaintingInWmPaint
        | ControlStyles.OptimizedDoubleBuffer,
        True,
    )


def _make_close_button(
    dialog_result,
    location,
    size=36,
    bg=None,
    fg=None,
    fg_hover=None,
    hover_bg=None,
) -> object:
    """右上角关闭键（圆滑设计覆盖层：圆形悬停底 + 细叉线）。

    系统标题栏的方形 X 没有任何圆滑可调（DWM 只能染底色），故以自绘按键
    覆盖：Label 载体（无 Button 的系统边框/焦点框残留），Paint 事件画
    圆形悬停底与叉线；点击时手动置窗体 DialogResult，语义与
    Button.DialogResult 一致。叉线/悬停色按主题传入（浅色面深叉线、深色面
    浅叉线）。
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
            # iterable, not Color」并弹 CLR 异常对话框（实测）。
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

    返回 (form, mapping)：mapping 把 ShowDialog 的 DialogResult 译成意图
    （'minimize' / 'quit'；Cancel = 右上角圆滑关闭键，留在窗口不动）。系统
    MessageBox 的按键文案只有是/否/确定/取消固定几组，出不了「最小化」
    「退出程序」动作词，故整窗自绘。

    外观（对齐主界面 + 逐条要求）：
    - **随应用主题**：theme 缺省时读 app_settings 镜像（ui.theme，前端
      apply() 初始化与每次切换都写），深浅两套配色对齐 tokens.css 两档；
      标题栏 logo 也按面取版（深色面浅色线条版 / 浅色面深色线条版）；
    - **无边框自绘标题栏**：logo + 标题 + 右上角圆滑关闭键（圆形悬停底 +
      细叉线——系统方形 X 无圆滑可言，DWM 也只能染底色，故自绘覆盖）；
    - **窗体圆角 + 描边**：无边框窗用 Region 裁圆滑轮廓，Paint 画 1px 描边；
    - **按键圆角纯色双色**：安全动作（最小化）品牌蓝实底，终止动作（退出
      程序）危险红实底——两色即两种动作性质；
    - **无问号图标**；字号用像素单位（pt 是物理单位会随 DPI 放大，与像素
      布局混用会「字大了框没大」）。
    回车 = 最小化（AcceptButton，安全侧）；Esc = 留在窗口（KeyPreview +
    KeyDown）。
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

        为什么不用 FlatStyle.Flat：它的方角 + 系统描边 + 深灰面色正是「按键
        突兀」的来源，且没有可调的圆角。这里 UserPaint + Paint 事件自绘抗锯齿
        圆角矩形——**不用 override OnPaint**：pythonnet 对 Python 子类的虚方法
        分派不生效（实测 override 不被回调，事件绑定才是 CLR→Python 的既有
        通路，同托盘 NotifyIcon）。DrawString 文字框必须 RectangleF（pythonnet
        不做 Rectangle→RectangleF 隐式转换，传 Rectangle 会抛 CLR 异常）。
        载体保留 Button：DialogResult / AcceptButton（回车默认）语义齐全。
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
    # 不掺 DPI 换算——任何 DPI 下比例一致（换算方案在部分机器上把窗口撑歪
    # 过，稳定优先一律不做）。
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


def _ask_close_intent(owner=None) -> str:
    """关窗意图询问：二选一（最小化到托盘 / 退出程序），同步模态对话框。

    返回 'minimize' / 'quit'。弹窗拿主窗口作 owner（居中其上、模态随主窗，
    不会藏到别的窗口后面）；任何异常（pythonnet 缺失 / 无桌面会话 / owner
    拿不到）一律降级 'minimize'——问不出意图时保持「点 X = 后台常驻」的
    既定语义，绝不误杀进程。

    同步弹窗是刻意的：closing 事件 handler 本就同步跑在 UI 线程
    （should_lock=True），pywebview 上游的 confirm_close 也在同一位置
    同步弹原生对话框——阻塞在对话框上正是拿用户决定的手段。
    """
    dialog = None
    try:
        dialog, mapping = _build_close_dialog(owner)
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
        intent = _ask_close_intent(owner)
        if intent == "quit":
            return True  # 放行真关闭：closed 事件里 os._exit(0) 终结进程
        if intent == "stay":
            return False  # 弹窗右上角 X：取消关闭，窗口原地不动
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

    NotifyIcon 是 WinForms 控件——必须晚于 pywebview 的
    SetCompatibleTextRenderingDefault 创建（实测：早于它建控件会把
    webview.start() 炸穿、整窗降级浏览器）。本线程自起 WinForms 消息泵
    派发托盘菜单/双击事件；主泵归窗口，两泵互不干扰。
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
        # 托盘与 health 跳转都在窗口建好后由 start(func) 拉起（时序原因见
        # _run_tray：NotifyIcon 早于 start() 创建会炸穿 WinForms 初始化 →
        # 整窗降级）
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
        # 托盘隐藏态 / 最小化态都先 SW_RESTORE（顺带修正：原 IsIconicWindow
        # 不是 user32 的真实导出，恒抛 AttributeError 走降级，最小化窗口
        # 只被前台化不被还原）
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        return True
    except Exception:  # noqa: BLE001 —— 非 Windows/无桌面环境降级
        return False


def _apply_webview2_static_args() -> None:
    """主窗口 WebView2 静态浏览器参数：禁组件更新与后台网络服务。

    WebView2（EdgeChromium）运行时进程的 Chromium 组件更新器会创建 BITS
    任务（msedgewebview2.exe 的 bits_service）。宿主 exe 无数字签名时，
    杀软主动防御把这笔 BITS 创建归因到宿主头上——实测拦截弹窗「程序正在
    创建 BITS 任务，可能造成系统关键文件被篡改」，建议用户阻止（阻止后
    WebView2 组件更新失效，极端情况窗口渲染异常）。--disable-component-
    update + --disable-background-networking 从源头关掉组件更新与后台
    更新流量：BITS 任务不再创建，本地 UI 功能不受影响（本项目全部数据
    走后端 httpx，WebView2 只渲染 127.0.0.1 页面）。

    进程级固定：同一 user-data 目录下所有 WebView2 环境的浏览器参数必须
    一致；登录窗（独立 user-data 目录）的暂存/还原逻辑与本值天然兼容
    （saved_env 即本值，还原后主窗口参数不变）。
    """
    key = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
    if not os.environ.get(key):
        os.environ[key] = "--disable-component-update --disable-background-networking"


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 桌面启动器")
    parser.add_argument("--server", action="store_true", help="无窗口模式，仅启动本地服务")
    # 换装进程入口（内部用，见 _run_update_helper）：由**暂存包的新 exe** 带着
    # 暂存目录/程序目录/旧进程 PID 拉起，等旧进程退出后完成换装。
    # ⚠️ 这个选项**必须出现在 --help 里**：老版本启动时靠 `--help` 探测暂存包
    # 是否支持安全换装（见 _staged_supports_helper）——用 SUPPRESS 藏起来会让
    # 探测永远失败、所有更新包都被判成「不支持安全换装」。
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
    if _stale_old_dir.exists():
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
