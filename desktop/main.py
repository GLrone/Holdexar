"""Holdexar 桌面启动器。

流程：单实例锁（被占先探活辨真伪）→ 后台线程 uvicorn → 轮询 /api/v1/health → pywebview 窗口（失败降级系统浏览器）。

用法：
    python desktop/main.py                # 桌面窗口模式
    python desktop/main.py --server       # 无窗口（仅本地服务）
    HOLDEXAR_DEV_URL=http://localhost:5173 python desktop/main.py    # 加载 Vite 开发服务器
"""
from __future__ import annotations

import argparse
import json
import os
import socket
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
from app.core.paths import is_frozen, resolve_data_dir  # noqa: E402

# 启动等待页：后端同源路由 /__splash（app/main.py _mount_spa 提供）。
# 窗口首载它，JS 轮询 health 就绪即整窗跳转真实应用——窗口初始化
# （CLR/WebView2 ~1s）与后端 lifespan（~2s）并行重叠。曾试过 data-URL
# 内联等待页，但 data: 源向 127.0.0.1 发 fetch 被 WebView2 跨源策略拦截
# （跨源策略会拦截等待页对本地服务的 fetch），故改同源路由。

# WebView2 Runtime（Evergreen 固定产品 GUID）注册表探测 + 官方离线安装链
_WEBVIEW2_REG_KEYS = (
    r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
    r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
)
_WEBVIEW2_DL_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

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
    仅需排除竞态窗口的场景，避免正常冷启动路径白等数秒。
    """
    url = f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/health"
    if quick:
        try:
            return _health_is_ours(httpx.get(url, timeout=0.15))
        except Exception:  # noqa: BLE001
            return False
    deadline = time.time() + timeout_s
    while True:
        try:
            if _health_is_ours(httpx.get(url, timeout=1.5)):
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
    deadline = time.time() + timeout
    url = f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/health"
    while time.time() < deadline:
        try:
            # 身份校验同 _probe_running：万一端口号被外来服务抢先绑定，
            # 对方对 health 回 200 也不算本应用就绪（否则窗口会加载到
            # 对方的页面还显示"服务已就绪"）
            if _health_is_ours(httpx.get(url, timeout=2.0)):
                return True
        except Exception:  # noqa: BLE001 —— 服务未就绪属预期
            time.sleep(0.3)
    return False


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

        js_api 调用在独立线程，os._exit 安全；重启后 main() 最早的
        _apply_pending_update 分支完成换装。
        """
        exe = Path(sys.executable) if getattr(sys, "frozen", False) else None
        if exe is None or not exe.is_file():
            return {"ok": False, "error": "仅打包态支持一键重启，请手动重启应用。"}
        import subprocess

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
            response = httpx.get(
                f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/proxies/resolve",
                timeout=3.0,
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
            resp = httpx.get(
                f"http://{SERVER_HOST}:{SERVER_PORT}/api/v1/account/list", timeout=3.0
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


def _apply_pending_update() -> bool:
    """启动期换装：staging 就绪则完成 程序目录替换，返回是否换装成功。

    时机：main() 最早分支、uvicorn 线程与窗口都还没拉起——此刻本进程
    持有的程序文件句柄最少（仅自身 exe），目录可移动；换装动作全程
    在子进程视角外完成，失败直接走原程序启动，用户无感回退。

    步骤：
    1. data/update-staging/Holdexar/ 存在且 manifest 就绪 → 进入换装
    2. 当前程序目录 → __old__（白名单目录原地保留）
    3. staging/Holdexar/* → 程序目录落位
    4. __old__ 延迟清理（sharing violation 重试，失败留待下次启动）

    **只在打包态生效**（这是硬门禁，不是优化）。开发态下 `_app_root()` 是**仓库根**，
    而白名单 `_UPDATE_KEEP` 只保 data/logs/__old__/update-staging——一旦触发，
    `.git`、`server/`、`web/`、`run.py`、`desktop/` 会被整批 move 进 `__old__`，
    再把 staging 的内容覆盖到仓库根上：一次误放的 `data/update-staging/manifest.json`
    就能毁掉工作树（且 .git 被移走时连 git 都没法回滚）。换装的目标是「已发布的
    程序目录」，开发态根本没有这个东西，所以直接不进入。
    """
    if not is_frozen():
        return False

    root = _app_root()
    staging = _data_dir() / "update-staging"
    new_dir = staging / APP_NAME
    manifest = staging / "manifest.json"

    if not manifest.is_file() or not new_dir.is_dir():
        return False
    try:
        info = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 坏 manifest 忽略，正常启动
        return False
    if not info.get("ready"):
        return False

    import shutil

    print(f"[更新] 检测到就绪暂存（{info.get('tag')}），开始换装…")
    old_dir = root / "__old__"

    # ① 旧目录残留（上次换装没清干净）：先删（这次反正要再放一版进去）
    if old_dir.exists():
        shutil.rmtree(old_dir, ignore_errors=True)
        if old_dir.exists():
            _alert("[更新] 清理旧版残留 __old__ 失败，跳过本次换装（可手动删除后重试）。")
            return False

    # ② 旧程序整体让位：程序目录里非白名单条目逐个移进 __old__
    #   （不整目录移动——data/ 在程序目录内，不能进备份）
    old_dir.mkdir()
    moved: list[Path] = []
    try:
        for entry in root.iterdir():
            if entry.name in _UPDATE_KEEP or entry == old_dir:
                continue
            target = old_dir / entry.name
            shutil.move(str(entry), str(target))
            moved.append(target)
    except Exception as e:  # noqa: BLE001 —— 移到一半失败：回滚已移条目
        print(f"[更新] 旧文件移动失败：{e}")
        for target in reversed(moved):
            try:
                shutil.move(str(target), str(root / target.name))
            except Exception:  # noqa: BLE001 —— 回滚失败只能留残迹
                pass
        _alert(f"[更新] 换装失败（旧文件移动阶段），已回滚。详情见 data/logs。")
        return False

    # ③ 新文件落位
    try:
        for entry in new_dir.iterdir():
            shutil.move(str(entry), str(root / entry.name))
        shutil.rmtree(staging, ignore_errors=True)
    except Exception as e:  # noqa: BLE001 —— 落位失败：__old__ 完整在位，
        #   下条弹窗指导手工移回 = 有退路（此时新文件已部分混入，
        #   程序目录可能不完整——手工把 __old__ 内容移回根目录即恢复）
        print(f"[更新] 新文件落位失败：{e}")
        _alert(
            "[更新] 换装失败（新文件落位阶段）。\n\n"
            "恢复方法：打开程序所在目录，把 __old__ 文件夹内全部内容移回上一级目录即可。"
        )
        return False

    # ④ 延迟清理上一版程序文件：运行中的 exe/WebView 句柄会拖住删除，
    #   重试窗口覆盖本进程退出 + 新版启动的间隙；失败留待下次启动再清
    _cleanup_old_dir_async(old_dir)
    print("[更新] 换装完成。")
    return True


def _cleanup_old_dir_async(old_dir: Path) -> None:
    """后台线程清 __old__：sharing violation 重试（Windows 文件锁语义）。"""
    import shutil

    def _retry_delete() -> None:
        for _ in range(30):  # ~30s 窗口
            if shutil.rmtree(old_dir, ignore_errors=True) or not old_dir.exists():
                return
            time.sleep(1.0)

    threading.Thread(target=_retry_delete, daemon=True).start()


def _relaunch_after_update() -> None:
    """换装成功后重启：新 exe 起进程，本进程退出。"""
    exe = Path(sys.executable) if getattr(sys, "frozen", False) else None
    if exe is None or not exe.is_file():
        print("[更新] 非打包态换装完成，不自动重启（开发态需手动重启服务）。")
        return
    import subprocess

    subprocess.Popen(
        [str(exe)],
        cwd=str(exe.parent),
        close_fds=True,
        creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
    )
    os._exit(0)


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


def _make_closing_guard(window):
    """关窗守卫：用户点 X = 隐藏到托盘（后端常驻），托盘「退出」放行。

    pywebview closing 事件契约：handler 返回 **False** = 取消关闭
    （Event.set 统计 False 值 → winforms on_closing 置 args.Cancel）；
    True/None = 放行。"""
    def _guard() -> bool:
        if _tray_state["quit"]:
            return True  # 托盘「退出」：放行真关闭
        try:
            window.hide()
        except Exception:  # noqa: BLE001
            pass
        return False  # 取消关闭：窗口隐藏，进程常驻

    return _guard


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


def _open_window(url: str, fallback_url: str | None = None) -> None:
    """开主窗。url 为首载页（窗口模式下是 data-URL 等待页）；fallback_url
    为窗口不可用时浏览器兜底要打开的真实地址（data-URL 在系统浏览器里
    兼容差，兜底必须走真实 URL）。"""
    real_url = fallback_url or url
    try:
        import webview
    except ImportError:
        webview = None  # type: ignore[assignment]
    if webview is None:
        _browser_fallback(real_url, "pywebview 未安装")
        return
    if not _webview2_available():
        _browser_fallback(
            real_url,
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
        window = webview.create_window(WINDOW_TITLE, url, **kwargs)
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
        # 托盘线程在窗口建好后由 start(func) 拉起（时序原因见 _run_tray：
        # NotifyIcon 早于 start() 创建会炸穿 WinForms 初始化 → 整窗降级）
        webview.start(_run_tray, (window,), **start_kwargs)  # 阻塞至窗口真关闭（仅托盘退出可达）
    except Exception as e:  # noqa: BLE001 —— 窗口创建/渲染层初始化失败一并降级浏览器
        _browser_fallback(real_url, f"窗口初始化失败：{e}")
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
    args = parser.parse_args()

    # 孤儿实例防线最早挂上：换装/端口协商/服务线程任何阶段包装进程死亡，
    # 本进程都不应存活成无主实例（防线语义见 _watch_launcher 顶注）
    _watch_launcher()

    # WebView2 静态参数须在任何窗口创建前就位（webview.start 建环境时读取）
    _apply_webview2_static_args()

    # 启动期换装：staging 就绪则替换程序文件后重启（早期分支：
    # uvicorn 线程/窗口/端口锁均未拉起，程序文件句柄最少）
    if _apply_pending_update():
        _relaunch_after_update()
        return

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
    # 同源等待页 /__splash，JS 轮询 health 就绪即整窗跳转真实应用，省掉
    # 旧的"先等 health 再开窗"串行等待（一轮 ~3s）。注意等待页也在后端
    # 上（服务就绪前窗口会短暂显示 WebView2 默认空白，属预期）
    splash = f"{url}/__splash"

    def _watchdog() -> None:
        """后台看门狗：HEALTH_TIMEOUT 内后端仍不可用则弹窗报错（窗口模式下
        等待页会一直转圈，用户需要明确的失败反馈）。"""
        if not _wait_health(HEALTH_TIMEOUT):
            detail = "".join(error_box)
            hint = ""
            if "10048" in detail or "already in use" in detail.lower():
                hint = f"\n\n[提示] 端口 {SERVER_PORT} 绑定失败，疑似被其他程序占用。"
            _alert(
                f"服务启动失败（{HEALTH_TIMEOUT:.0f}s 内健康检查未通过）。{hint}\n\n"
                + (detail[-1500:] if detail else "详细错误见 data/logs 日志。")
            )
            print("服务启动失败：")
            if detail:
                print(detail)

    threading.Thread(target=_watchdog, daemon=True).start()
    _open_window(splash, fallback_url=url)


if __name__ == "__main__":
    main()
