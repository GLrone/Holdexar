"""Clash 内核管理：随包内核就位 / 检测 / 网络下载兜底 / 订阅启动 / 停止。

内核与许可原文**随发行包分发**（版本固定在 kernel_release.MIHOMO_VERSION，
源目录 assets/clash → 打包后随资源目录），用户机器上不再检索本机装没装 Clash：
内核由 ensure_kernel() 复制进 data/clash/，detect_kernel() 只认这一处。
网络下载（download_kernel*）退居兜底，只服务两类场景：源码 clone 尚未补资产、
用户手工删除了 data/clash/ 里的可执行文件。

订阅下载的链式引导：直连失败且内核已在跑时经本地混合端口重试。
红线：本模块不内置任何订阅源，订阅 URL 只能来自用户设置。
"""
from __future__ import annotations

import base64
import ctypes
import io
import logging
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.parse
import zipfile
from collections.abc import Sequence
from ctypes import wintypes
from pathlib import Path

import httpx
import yaml

from app.core.config import get_settings
from app.core.logging import log_event
from app.core.net import free_local_port as _free_local_port
from app.core.net import port_listening as _port_listening
from app.domains.proxies.kernel_release import (
    MIHOMO_VERSION,
    MIRRORS,
    is_windows,
    kernel_filename,
    mihomo_url,
)
# 运行配置生成与节点依赖解析的唯一实现在 runtime_config；本模块只做转发，
# 使 clash_manager 保持「内核管理」这一处引用面（订阅侧与池侧共用同一实现）。
from app.domains.proxies.kernel_process import (
    PRECHECK_ADMISSION,
    PRECHECK_CHANGED,
    PRECHECK_REJECTED,
    PRECHECK_TIMEOUT,
    PRECHECK_UNLAUNCHABLE,
    KernelPrecheckError,
    confirm_config_text,
    run_kernel_cli,
    spawn_kernel,
)
from app.domains.proxies.runtime_config import (  # noqa: F401 —— 对外契约
    render_subscription_config,
    PROBE_LANE_GROUP_PREFIX,
    resolve_node_dependencies,
    validate_execution_config,
    validate_geo_free_config,
)

logger = logging.getLogger(__name__)

# 兼容旧引用名：镜像链（网络下载兜底用）
_MIRRORS = MIRRORS

# 生成文件后缀：`clash-sub-{id}.runtime.yaml`。与原始缓存分文件——原文是证据、
# 生成结果可随时重建，就地改写原文会让「订阅原样」在磁盘上不成立。
_RUNTIME_SUFFIX = ".runtime.yaml"


def bundled_clash_dir() -> Path:
    """随包内核目录（发行包 `_MEIPASS/clash`；源码态 `assets/clash`）。"""
    return get_settings().clash_dir


_VERSION_RE = re.compile(r"v(\d+(?:\.\d+)+)")


def parse_kernel_version(raw: str | None) -> tuple[int, ...] | None:
    """mihomo -v 输出首行 → 点分元组：`Mihomo Meta v1.19.12 …` → (1, 19, 12)。

    None = 无法识别（版本未知，调用方保守处理）。
    """
    m = _VERSION_RE.search(raw or "")
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def _kernel_exe_version(exe_path: Path) -> tuple[int, ...] | None:
    """就位内核的版本：跑 `-v` 解析（失败/超时/输出非 mihomo 格式 = None）。"""
    return parse_kernel_version(kernel_version(exe_path))


def _maybe_upgrade_kernel(kernel_path: Path) -> str | None:
    """版本感知的内核换装：随包版本**高于**就位版本才替换，返回替换动作描述。

    语义（升级不夺回）：
    - 就位版本 < 随包版本 → 替换（发行包升内核后，老安装随更新获得新内核）；
    - 就位版本 ≥ 随包版本 → 保留（用户手换更高版/自装同版，不替用户做决定）；
    - 版本不可辨（-v 失败、非 mihomo 文件）→ 保留（看不清的文件不动它）；
    - 内核进程在跑（本 runtime 实例或孤儿 mihomo）→ 保留（Windows 下运行中
      的 exe 无法覆盖；启动链里 start() 会先 stop/清孤儿再走到这里，
      真正撞上的只有 lifespan 启动期孤儿还活着的窗口，留给下次启动）。

    """
    exe = kernel_path / kernel_filename()
    bundled_exe = bundled_clash_dir() / kernel_filename()
    if not exe.is_file() or not bundled_exe.is_file():
        return None
    if runtime.status()["running"] or _kernel_process_alive():
        return None
    current = _kernel_exe_version(exe)
    target = parse_kernel_version(MIHOMO_VERSION)
    if current is None or target is None or current >= target:
        return None
    try:
        shutil.copy2(bundled_exe, exe)
    except OSError as e:  # noqa: BLE001 —— 文件锁/权限失败：保留旧内核继续可用
        log_event(
            logger,
            "内核升级替换失败，保留现有内核继续使用",
            tag="降级",
            level=logging.WARNING,
            detail={"现有版本": ".".join(map(str, current)), "原因": str(e)},
        )
        return None
    log_event(
        logger,
        f"内核已升级到 {MIHOMO_VERSION}",
        tag="成功",
        detail={"旧版本": ".".join(map(str, current)), "新版本": MIHOMO_VERSION},
    )
    return f"{'.'.join(map(str, current))} → {MIHOMO_VERSION}"


class PROCESSENTRY32W(ctypes.Structure):
    """`tlhelp32.h` 中 tagPROCESSENTRY32W 的逐字段映射。

    布局由 C ABI 推导而非试错对齐：th32DefaultHeapID 是 ULONG_PTR，x64 下宽 8
    且要求 8 字节对齐，而它前面三个 DWORD 只排到 offset 12，因此 offset 12 是
    C 编译器强制插入的 4 字节衬垫。x64 上由此得 sizeof == 568、
    offsetof(szExeFile) == 44——二者有断言锁定，改动字段顺序会立刻暴露错位。
    """

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p),  # ULONG_PTR：x64 下 8 字节
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * wintypes.MAX_PATH),
    ]


_TH32CS_SNAPPROCESS = 0x00000002


def _image_process_running(image_name: str) -> bool:
    """Toolhelp32 快照枚举：是否存在映像名为 `image_name` 的进程（忽略大小写）。

    等价替代 `tasklist /FI "IMAGENAME eq <name>"`，但全程是本进程内的 Win32 调用：
    只读内核给出的进程列表快照，不 OpenProcess、不触碰任何进程地址空间，也不拉起
    任何子进程。快照失败返回 False（沿用旧实现「查询失败不阻断」的保守语义）。
    """
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    if snapshot is None or snapshot == ctypes.c_void_p(-1).value:
        return False

    target = image_name.lower()
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.szExeFile.lower() == target:
                return True
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return False


def _kernel_process_alive() -> bool:
    """内核进程存活探测：真孤儿（非本 runtime 实例）的兜底。

    Windows 走 Toolhelp32 快照比对内核映像名（进程内 Win32 调用，不产生子进程）；
    非 Windows 与旧行为一致返回 False——探测只是保守替换的额外闸门，失败不阻断
    补资产。
    """
    if not is_windows():
        return False
    try:
        return _image_process_running(kernel_filename())
    except Exception:  # noqa: BLE001
        return False


def _copy_bundled(kernel_path: Path, names: Sequence[str]) -> dict:
    """补齐指定随包资产，同名不覆盖；可执行文件另按版本升级。"""
    bundle = bundled_clash_dir()
    kernel_path.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    missing: list[str] = []
    for name in names:
        target = kernel_path / name
        if target.is_file():
            continue
        src = bundle / name
        if src.is_file():
            shutil.copy2(src, target)
            copied.append(name)
        else:
            missing.append(name)
    return {"copied": copied, "missing": missing}


def ensure_kernel_files(kernel_path: Path) -> dict:
    """内核目录补齐：可执行文件与许可原文（随包资产为唯一来源）。

    入参是**内核目录本身**（config.yaml 所在处，也是 mihomo `-d` 的工作目录）；
    以 data_dir 为入参的版本见 ensure_kernel()。
    """
    result = _copy_bundled(kernel_path, (kernel_filename(),))
    upgrade = _maybe_upgrade_kernel(kernel_path)
    if upgrade:
        result["upgraded"] = upgrade
    return result


def ensure_kernel(data_dir: Path) -> dict:
    """随包内核就位（data_dir 版本）：把内核与许可原文补进 data/clash/。

    随包资产落到用户数据目录的唯一入口。返回
    {"kernelReady": bool, "copied": [...], "missing": [...], "bundleDir": str,
     "upgraded": str | None}；发行包缺内核（源码 clone 未补资产）时
    kernelReady=False，由调用方决定是回退网络下载还是提示用户。
    """
    result = _copy_bundled(kernel_dir(data_dir), (kernel_filename(),))
    upgrade = _maybe_upgrade_kernel(kernel_dir(data_dir))
    if upgrade:
        result["upgraded"] = upgrade
    ready = kernel_exe(data_dir).is_file()
    if result["copied"]:
        log_event(
            logger,
            "随包内核资产已就位",
            tag="成功",
            detail={"文件": "、".join(result["copied"])},
        )
    if not ready:
        log_event(
            logger,
            "随包内核不可用，将回退网络下载",
            tag="降级",
            level=logging.WARNING,
            detail={"目录": str(bundled_clash_dir()), "缺失文件": kernel_filename()},
        )
    return {
        "kernelReady": ready,
        "copied": result["copied"],
        "missing": result["missing"],
        "upgraded": upgrade,
        "bundleDir": str(bundled_clash_dir()),
    }


def decode_profile_title(raw: str | None) -> str | None:
    """订阅面板名：profile-title 响应头，base64:xxx 或纯文本。"""
    if not raw:
        return None
    raw = raw.strip().strip('"')
    if raw.lower().startswith("base64:"):
        data = raw[7:].strip()
        try:
            return base64.b64decode(data + "=" * (-len(data) % 4)).decode("utf-8")
        except Exception:  # noqa: BLE001
            return None
    return raw or None


def _decode_content_disposition_filename(raw: str | None) -> str | None:
    """Content-Disposition 里的文件名（机场名常用通道）。

    先 RFC 5987 扩展格式 filename*（percent-decode 后按 '' 切实际值），
    回落普通 filename=；
    取不到返回 None。头常见形态：
    attachment; filename*=UTF-8''%E6%9C%BA%E5%9C%BA%E5%90%8D.yaml
    attachment; filename="airport.yaml"
    """
    if not raw:
        return None
    m = re.search(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)", raw)
    if m:
        try:
            name = urllib.parse.unquote(m.group(1).strip().strip('"'))
        except Exception:  # noqa: BLE001
            name = None
        if name:
            return name
    m = re.search(r'filename\s*=\s*"([^";]+)"', raw) or re.search(
        r"filename\s*=\s*([^;]+)", raw
    )
    if m:
        name = m.group(1).strip().strip('"')
        if name:
            return name
    return None


def subscription_auto_name(headers: httpx.Headers, url: str) -> str | None:
    """订阅自动取名链（仅保存时调用；刷新不覆写）。

    优先级：profile-title 头（base64/纯文本，面板专用通道）→
    Content-Disposition 文件名 → URL 末段（去 query，percent 解码）。
    三级全空返回 None（调用方落 null，前端回落显示 URL）。
    兜底若用固定字符串（如 "Remote File"）会造出一个假机场名——
    这里返回 None 保持「无机场名」的诚实语义。

    URL 末段本身是 token 时（全 hex、含哈希特征）也返回 None：那串东西
    对用户没有任何辨识度，比「无名称」（前端回落显示完整 URL，可核对）
    更差——只有主域名路径段那种可读末段才当名字用。
    """
    title = decode_profile_title(headers.get("profile-title"))
    if title:
        return title
    name = _decode_content_disposition_filename(
        headers.get("content-disposition")
    )
    if name:
        return name
    # URL 末段：去 query、去 scheme/netloc（根路径末段是主机名，不是名字），
    # 取路径最后一段 percent 解码
    path = url.split("?", 1)[0]
    if path.count("/") > 2:
        # https://host/xxx → /xxx；https://host → /
        path = "/" + path.split("//", 1)[1].split("/", 1)[1]
    else:
        path = "/"
    last = path.rstrip("/").rsplit("/", 1)[-1]
    if last:
        try:
            decoded = urllib.parse.unquote(last)
        except Exception:  # noqa: BLE001
            decoded = last
        if decoded and not _looks_like_opaque_token(decoded):
            return decoded
    return None


def _looks_like_opaque_token(text: str) -> bool:
    """末段是否是「看不出含义的凭据」（长 hex / 长随机串）。

    判据保守：长度 ≥ 16 且不含分隔词、无空格、非中文——命中即不当名字。
    """
    if len(text) < 16:
        return False
    if any(ch.isspace() for ch in text):
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in text):
        return False
    return bool(re.fullmatch(r"[A-Za-z0-9_\-=]+", text))


# 机场面板普遍校验 UA：httpx 默认 UA 会被 403，伪装 Clash 系客户端（内核拉取
# provider 时的同款标识）。**必须含 "clash" 关键字**——面板按 UA 分流订阅格式，
# 含 clash/meta 才回 Clash YAML；只写 mihomo 会被当成通用客户端回落 base64
# 节点表（本项目校验只认 YAML，「更新失败」即此因）。版本随 kernel_release。
_SUB_HEADERS = {"User-Agent": f"clash.meta/{MIHOMO_VERSION.lstrip('v')}"}


# External local proxy defaults. Holdexar's own listener comes from runtime status.
LOCAL_MIXED_PORTS: tuple[int, ...] = (7897,)
# 借道口探活后的最小可用性验证：口在听不代表出口能出网
# （内核起了但没选中节点、节点全挂时端口照样 listen）。
_LOCAL_CHANNEL_PROBE_URL = "https://www.gstatic.com/generate_204"


def local_mixed_channels(exclude_port: int | None = None) -> list[str]:
    """Discover external local proxy defaults; requests validate actual egress."""
    return [
        f"http://127.0.0.1:{port}"
        for port in LOCAL_MIXED_PORTS
        if port != exclude_port and _port_listening(port)
    ]


def local_channel_egress_ok(proxy_url: str | None, timeout: float = 8.0) -> bool:
    """借道口是否真能出网（端口在听 ≠ 出口可用）。

    内核在跑但没选中可用节点时端口照样 listen，此时经它发请求必然失败；
    把它排进通道链只会让每次刷新白等一轮超时。直连（proxy_url=None）恒真
    ——直连不经过借道口，无需探活。
    """
    if not proxy_url:
        return True
    try:
        resp = httpx.get(
            _LOCAL_CHANNEL_PROBE_URL, proxy=proxy_url, timeout=timeout,
            trust_env=False, follow_redirects=True,
        )
        return resp.status_code < 400
    except Exception:  # noqa: BLE001 —— 探活失败即视为该口不可用
        return False


def _download_attempts(
    runtime_port: int | None = None, proxy_url: str | list[str] | None = None
) -> list[tuple[str | None, str]]:
    """Build generic asset download paths from managed, saved, local, and direct exits."""
    attempts: list[tuple[str | None, str]] = []
    if runtime_port:
        attempts.append((f"http://127.0.0.1:{runtime_port}", "经内核代理"))
    saved = [proxy_url] if isinstance(proxy_url, str) else list(proxy_url or [])
    for url in saved:
        if url and url not in [a[0] for a in attempts]:
            attempts.append((url, "经已保存代理"))
    for port in LOCAL_MIXED_PORTS:
        if port == runtime_port:
            continue
        if _port_listening(port):
            attempts.append((f"http://127.0.0.1:{port}", f"本地混合端口 {port}"))
    # 直连放最后：订阅面板域名多数被墙，直连通常要等满超时才轮到下一条通道，
    # 排在最前会让每次刷新都白等（与 Clash Verge Rev「经当前代理更新订阅」同向）。
    # 内核没跑（首次添加订阅）时前几条通道自然缺席，直连即唯一通道。
    attempts.append((None, "直连"))
    return attempts


def kernel_dir(data_dir: Path) -> Path:
    d = data_dir / "clash"
    d.mkdir(parents=True, exist_ok=True)
    return d


def kernel_exe(data_dir: Path) -> Path:
    return kernel_dir(data_dir) / kernel_filename()


def detect_kernel(data_dir: Path) -> dict:
    """检测内核就位情况：只认本应用的内核目录 data/clash/。

    刻意**不检索本机其他 Clash 安装**（scoop / Program Files / PATH）：内核已随
    发行包分发且版本固定，借用用户自装的内核会让「内核从哪来、是哪个版本」不可控
    ——用户卸载 Verge 或改了 PATH，代理会在毫无提示的情况下失去内核。
    内核只可能来自随包资产，故 found=True 时 builtin 恒为 True（字段保留给前端）。
    """
    exe = kernel_exe(data_dir)
    if exe.is_file():
        return {"found": True, "path": str(exe), "builtin": True}
    return {"found": False, "path": None, "builtin": False}


def install_kernel(data_dir: Path) -> dict:
    """安装内核：随包资产优先（本地复制，瞬时完成），随包缺失才走网络下载。

    前端「自动下载内核」与保存订阅时的自动补装都走这里。发行包必然带内核，
    网络那一支只服务两类情形：源码 clone 尚未补资产、用户手工删了 exe。
    """
    ensure_kernel(data_dir)
    target = kernel_exe(data_dir)
    if target.is_file():
        return {"ok": True, "path": str(target), "source": "bundled", "via": "随包资产"}
    log_event(
        logger,
        "随包内核不可用，回退网络下载",
        tag="降级",
        level=logging.WARNING,
        detail={"目标版本": MIHOMO_VERSION},
    )
    return download_kernel(data_dir) if is_windows() else download_kernel_linux(data_dir)


# 内核下载进度（模块级单例：下载线程写，轮询端点读；dict 原子替换免锁）
_KERNEL_PROGRESS: dict = {}


def _kernel_progress_reset() -> None:
    _KERNEL_PROGRESS.clear()
    _KERNEL_PROGRESS.update({
        "running": False, "phase": None, "percent": None,
        "received": 0, "total": None, "source": None, "via": None,
        "error": None, "ok": False,
    })


def kernel_download_progress() -> dict:
    """当前/最近一次内核下载进度（前端轮询）。无记录时 running=False。"""
    if not _KERNEL_PROGRESS:
        _kernel_progress_reset()
    return dict(_KERNEL_PROGRESS)


def _download_zip_kernel(
    url: str, proxy: str | None, target: Path, source: str, via: str
) -> None:
    """流式下载 mihomo zip 并解出 exe；进度按 Content-Length 上报百分比。

    连接超时收紧到 10s（被墙通道快速轮转），读超时 120s 容慢速传输。
    """
    received = 0
    total = None
    with httpx.stream(
        "GET", url, timeout=httpx.Timeout(120, connect=10),
        follow_redirects=True, trust_env=False,
        proxy=proxy if proxy else None,
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("content-length") or 0) or None
        buf = io.BytesIO()
        for chunk in resp.iter_bytes(64 * 1024):
            buf.write(chunk)
            received += len(chunk)
            _KERNEL_PROGRESS.update({
                "phase": "下载内核", "received": received, "total": total,
                "percent": round(received * 100 / total, 1) if total else None,
                "source": source, "via": via,
            })
    with zipfile.ZipFile(buf) as z:
        for name in z.namelist():
            if name.endswith(".exe"):
                target.write_bytes(z.read(name))
                return
    raise ValueError("压缩包中未找到 exe")


def download_kernel(data_dir: Path) -> dict:
    """下载 mihomo 内核到 data/clash/（镜像优先，逐镜像过通道链）。

    **兜底路径**：正常安装走 install_kernel() 的随包复制。同步阻塞，调用方放
    线程；进度写 _KERNEL_PROGRESS 供轮询端点上报。
    """
    _kernel_progress_reset()
    _KERNEL_PROGRESS["running"] = True
    try:
        base = mihomo_url()
        target = kernel_exe(data_dir)
        # 内核没下载成功自己必然没跑：仅直连 + 本地混合端口（用户 Verge 等）
        channels = _download_attempts(None, None)
        for mirror in _MIRRORS:
            url = mirror + base if mirror else base
            for proxy, label in channels:
                try:
                    log_event(
                        logger,
                        f"下载 Clash 内核：尝试镜像 {mirror or 'github'}、通道 {label}",
                        detail={"镜像": mirror or "github", "通道": label, "下载地址": url},
                    )
                    # 尝试开始即上报：连接阶段（最长 10s）弹窗也可见当前镜像/通道
                    _KERNEL_PROGRESS.update({
                        "phase": "连接下载源", "source": mirror or "github", "via": label,
                    })
                    _download_zip_kernel(url, proxy, target, mirror or "github", label)
                    _KERNEL_PROGRESS.update({"ok": True, "running": False, "percent": 100})
                    return {
                        "ok": True, "path": str(target),
                        "source": mirror or "github", "via": label,
                    }
                except Exception as e:  # noqa: BLE001
                    log_event(
                        logger,
                        f"内核下载失败：镜像 {mirror or 'github'} 经 {label} 未成功，换下一通道",
                        level=logging.WARNING,
                        detail={"镜像": mirror or "github", "通道": label, "原因": str(e)},
                    )
        _KERNEL_PROGRESS.update({
            "running": False,
            "error": "所有下载源均失败，可手动放置内核到 data/clash/",
        })
        return {"ok": False, "error": "所有下载源均失败，可手动放置内核到 data/clash/"}
    finally:
        _KERNEL_PROGRESS["running"] = False


def download_kernel_linux(data_dir: Path) -> dict:
    """linux: gz 单文件（进度按流式字节数上报）。兜底路径，同上。"""
    import gzip

    _kernel_progress_reset()
    _KERNEL_PROGRESS["running"] = True
    try:
        base = mihomo_url()
        target = kernel_exe(data_dir)
        for mirror in _MIRRORS:
            url = mirror + base if mirror else base
            try:
                resp = httpx.get(url, timeout=120, follow_redirects=True, trust_env=False)
                resp.raise_for_status()
                target.write_bytes(gzip.decompress(resp.content))
                target.chmod(0o755)
                return {"ok": True, "path": str(target), "source": mirror or "github"}
            except Exception as e:  # noqa: BLE001
                log_event(
                    logger,
                    "镜像下载内核失败，换下一个镜像",
                    level=logging.WARNING,
                    detail={"原因": str(e)},
                )
        return {"ok": False, "error": "所有下载源均失败"}
    finally:
        _KERNEL_PROGRESS["running"] = False


def kernel_version(exe_path: Path) -> str | None:
    try:
        result = run_kernel_cli(exe_path, ["-v"], timeout=10)
        first = (result.stdout or result.stderr).splitlines()
        return first[0] if first else None
    except Exception:  # noqa: BLE001
        return None


def parse_mixed_port(config_text: str) -> int | None:
    """Read the configured mixed listener port when one is present."""
    match = re.search(r"^\s*mixed-port\s*:\s*(\d+)", config_text, re.MULTILINE)
    if match:
        return int(match.group(1))
    match = re.search(r"^\s*port\s*:\s*(\d+)", config_text, re.MULTILINE)
    if match:
        return int(match.group(1))
    return None


def pin_mixed_port(config_text: str, port: int) -> str:
    """Set the managed mixed listener and keep it on loopback."""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return config_text
    if not isinstance(doc, dict):
        return config_text
    if doc.get("mixed-port") == int(port):
        if doc.get("allow-lan") is False and doc.get("bind-address") == "127.0.0.1":
            return config_text
    doc["mixed-port"] = int(port)
    doc["allow-lan"] = False
    doc["bind-address"] = "127.0.0.1"
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def strip_tun_section(config_text: str) -> str:
    """剥掉订阅自带的虚拟网卡段（tun）与其下的域名解析接管配置（幂等）。

    程序的内核只做出口，不接管本机流量——订阅 yaml 常自带 tun 段与 dns-hijack
    列表，照单收下会让内核去抢系统 DNS、把整机的域名解析换成假地址。不可解析
    的配置原样返回（校验与入口就绪检查会拦住起不来的形态）。"""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return config_text
    if not isinstance(doc, dict) or "tun" not in doc:
        return config_text
    doc.pop("tun", None)
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


CONTROLLER_PORT = 19090


def _new_controller_secret() -> str:
    return secrets.token_urlsafe(32)


def _with_controller_secret(config_text: str, secret: str) -> str:
    line = f"secret: {secret}"
    if re.search(r"^secret\s*:", config_text, re.MULTILINE):
        return re.sub(r"^secret\s*:.*$", line, config_text, count=1, flags=re.MULTILINE)
    return f"{config_text.rstrip()}\n{line}\n"


def resolve_controller(config_text: str) -> tuple[str, str]:
    """分配自有回环控制端点和随机密钥；端口占用时从基址避让。"""
    match = re.search(r"^external-controller\s*:\s*['\"]?([^'\"\n#]+)", config_text, re.MULTILINE)
    secret = _new_controller_secret()
    port: int | None = None
    if match:
        addr = match.group(1).strip()
        m = re.search(r":(\d+)\s*$", addr)
        if m:
            candidate = int(m.group(1))
            if candidate > 0 and _port_free(candidate):
                port = candidate
    if port is None:
        port = CONTROLLER_PORT
        while not _port_free(port):
            port += 1
    return f"http://127.0.0.1:{port}", secret


CONFIG_TEST_TIMEOUT = 15.0


def validate_config(exe_path: str, config_path: str, work_dir: Path) -> None:
    """启动前校验运行配置：先静态准入再跑内核 `-t`；任何失败都抛 `KernelPrecheckError`。"""
    config_file = Path(config_path)
    raw = config_file.read_bytes()
    try:
        validate_execution_config(raw.decode("utf-8", errors="ignore"))
    except KernelPrecheckError:
        raise
    except ValueError as exc:
        raise KernelPrecheckError(PRECHECK_ADMISSION, str(exc)) from None
    try:
        result = run_kernel_cli(
            exe_path, ["-t", "-f", config_path, "-d", str(work_dir)],
            timeout=CONFIG_TEST_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise KernelPrecheckError(
            PRECHECK_TIMEOUT,
            f"内核配置校验超时（{CONFIG_TEST_TIMEOUT}s），禁止启动",
        ) from None
    except OSError as exc:
        raise KernelPrecheckError(
            PRECHECK_UNLAUNCHABLE, f"内核无法执行：{type(exc).__name__}",
        ) from None
    if config_file.read_bytes() != raw:
        raise KernelPrecheckError(
            PRECHECK_CHANGED, "运行配置在预检期间被改动，拒绝启动",
        )
    if result.returncode == 0:
        return
    lines = [
        line.split("level=error", 1)[1].strip()
        for line in ((result.stdout or "") + (result.stderr or "")).splitlines()
        if "level=error" in line
    ]
    detail = "；".join(lines[:5]) or f"退出码 {result.returncode}"
    raise KernelPrecheckError(
        PRECHECK_REJECTED, f"内核拒绝配置（-t 校验未通过）：{detail}",
    )


def _validate_config_text(exe_path: str, text: str, work_dir: Path) -> None:
    """Validate the final in-memory configuration without replacing a live file."""
    validate_execution_config(text)
    with tempfile.TemporaryDirectory(prefix=".mihomo-config-check-", dir=work_dir) as folder:
        candidate = Path(folder) / "config.yaml"
        candidate.write_text(text, encoding="utf-8")
        validate_config(exe_path, str(candidate), work_dir)


def config_unchanged(startup_text: str | None, disk_text: str) -> bool:
    """内核启动文本与磁盘配置是否等价（重启开关的判据）。

    比较对象是**订阅内容视图**（`_subscription_view`）：双方都剥掉探测 lane
    （端口分配随本机占用态变化，不属于订阅内容）再比 yaml 结构。语义是
    「订阅内容真的变了才重启」，格式变化与端口漂移都不触发重启。
    """
    if startup_text == disk_text:
        return True
    a = _subscription_view(startup_text or "")
    b = _subscription_view(disk_text)
    if a is None or b is None:
        return False
    return a == b


def _subscription_view(config_text: str) -> dict | None:
    """配置的订阅内容视图：剥掉探测 lane、混合口与控制密钥后的 yaml 结构。

    mixed-port 一并剥掉：监听口归程序管（pin_mixed_port 钉成工作端口），
    不属于订阅内容——端口漂移不触发重启的语义由这里兑现。"""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    view = dict(doc)
    view.pop("mixed-port", None)
    view.pop("secret", None)
    for key in ("proxy-groups", "listeners"):
        entries = view.get(key)
        if isinstance(entries, list):
            kept = [
                e for e in entries
                if not (isinstance(e, dict) and str(e.get("name", "")).startswith(PROBE_LANE_PREFIX))
            ]
            if kept:
                view[key] = kept
            else:
                view.pop(key, None)  # 剥空的键与「本就没有」等价
    return view


# ─── 探测 lane（节点检测的并行通道）────────────────────────────
# 与池内核的 lane 同一形态：一个 select 组 + 一个 mixed listener，listener 的
# `proxy` 指向本组。组选择是组内状态、互不干扰，探测经 listener 直达所选节点
# （绕过规则引擎与 GLOBAL）——节点检测因此无需切 selector、无需切 mode，可
# PROBE_LANE_COUNT 路并发。组用 include-all：成员随内核当前节点集自动变化，
# 注入内容与订阅内容无关，配置比较时剥掉即可。
PROBE_LANE_PREFIX = PROBE_LANE_GROUP_PREFIX
PROBE_LANE_COUNT = 20
# 端口从固定基址起确定性分配：同占用态 → 同端口。避开默认混合口/控制器段与
# Windows 临时端口段（49152+，池内核 listener 落在那里）。
PROBE_LANE_PORT_BASE = 20000
PROBE_LANE_PORT_SPAN = 1000


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _port_owned_by_process(port: int, process_id: int) -> bool:
    if os.name == "nt":
        try:
            output = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"], capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=5,
            ).stdout or ""
        except (OSError, subprocess.SubprocessError):
            return False
        for line in output.splitlines():
            parts = line.split()
            if len(parts) < 5 or parts[0].upper() != "TCP":
                continue
            local = parts[1].rsplit(":", 1)
            if len(local) != 2 or not local[1].isdigit():
                continue
            if (
                int(local[1]) == port
                and local[0].strip("[]") in {"127.0.0.1", "::1"}
                and parts[3].upper() == "LISTENING"
                and parts[4].isdigit()
                and int(parts[4]) == int(process_id)
            ):
                return True
        return False
    try:
        import psutil

        return any(
            conn.pid == process_id and conn.status == psutil.CONN_LISTEN
            and conn.laddr.port == port and conn.laddr.ip in {"127.0.0.1", "::1"}
            for conn in psutil.net_connections(kind="tcp")
        )
    except (ImportError, OSError, PermissionError):
        return False


def _alloc_probe_ports(
    count: int, *, avoid: set[int],
    port_base: int = PROBE_LANE_PORT_BASE, port_span: int = PROBE_LANE_PORT_SPAN,
) -> list[int]:
    """确定性扫空闲端口；凑不满 count 就给多少用多少（空列表 = 不注入）。"""
    ports: list[int] = []
    candidate = port_base
    ceiling = port_base + port_span
    while len(ports) < count and candidate < ceiling:
        if candidate not in avoid and _port_free(candidate):
            ports.append(candidate)
        candidate += 1
    return ports


def inject_probe_lanes(
    config_text: str, *,
    port_base: int = PROBE_LANE_PORT_BASE, port_span: int = PROBE_LANE_PORT_SPAN,
    pin_ports: Sequence[int] | None = None,
) -> str:
    """把探测 lane 注入内核配置（幂等）：剔除本前缀旧条目后追加
    PROBE_LANE_COUNT 组 `select`(include-all) + 同数 mixed listener。
    端口避让本配置自己的混合口与控制器口；`pin_ports` 钉住沿用（热重载通道，
    端口归现役内核所有，绕过空闲检查）。不可解析的配置原样返回；
    一个端口都分不到时不动配置（检测退回串行路径）。"""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return config_text
    if not isinstance(doc, dict):
        return config_text
    avoid: set[int] = set()
    mixed = doc.get("mixed-port")
    if isinstance(mixed, int):
        avoid.add(mixed)
    controller = str(doc.get("external-controller") or "")
    m = re.search(r":(\d+)\s*$", controller)
    if m:
        avoid.add(int(m.group(1)))
    pinned = [
        p for p in (pin_ports or ())
        if isinstance(p, int) and p > 0 and p not in avoid
    ][:PROBE_LANE_COUNT]
    ports = pinned + _alloc_probe_ports(
        PROBE_LANE_COUNT - len(pinned), avoid=avoid | set(pinned),
        port_base=port_base, port_span=port_span,
    )
    if not ports:
        return config_text
    groups = [
        g for g in (doc.get("proxy-groups") or [])
        if not (isinstance(g, dict) and str(g.get("name", "")).startswith(PROBE_LANE_PREFIX))
    ]
    listeners = [
        entry for entry in (doc.get("listeners") or [])
        if not (isinstance(entry, dict) and str(entry.get("name", "")).startswith(PROBE_LANE_PREFIX))
    ]
    for i, port in enumerate(ports, start=1):
        group_name = f"{PROBE_LANE_PREFIX}-{i}"
        groups.append({"name": group_name, "type": "select", "include-all": True})
        listeners.append({
            "name": f"{group_name}-in",
            "type": "mixed",
            "port": port,
            "listen": "127.0.0.1",
            "proxy": group_name,
        })
    doc["proxy-groups"] = groups
    doc["listeners"] = listeners
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False)


def subscription_config_path(data_dir: Path, sub_id: int) -> Path:
    """每条订阅各存一份配置缓存（data/clash/clash-sub-{id}.yaml）。

    切换订阅因此只是「本地缓存文件热重载」，不再现场下载——下载只属于
    重拉/首次使用。共享单缓存文件会被任何一次下载覆写，是「切换卡住」
    与「配置互相覆盖」的根源。

    这份文件是**原始订阅正文**：来源证据与离线缓存，不再直接交给内核。
    """
    return kernel_dir(data_dir) / f"clash-sub-{sub_id}.yaml"


def materialize_subscription_config(raw_path: Path) -> Path:
    """原始订阅缓存 → 内核实际加载的运行配置，返回生成文件路径。

    内核加载的必须是项目生成的纯节点配置：地区规则、rule-providers、地区 DNS
    过滤、系统接管设置都不进运行配置（也因此不消费地理数据）。原始缓存保持
    原文不动——它是来源证据，且同一份原文总是渲染出同一份运行配置（幂等：
    重复刷新相同内容不触发重启）。

    入参已是生成文件时优先从配对的原始缓存重新生成，避免旧运行缓存携带过期
    配置；原始缓存不存在时才从该运行配置重建。
    """
    raw_path = Path(raw_path)
    out = raw_path
    if raw_path.name.endswith(_RUNTIME_SUFFIX):
        source_name = f"{raw_path.name[:-len(_RUNTIME_SUFFIX)]}.yaml"
        paired_source = raw_path.with_name(source_name)
        source_path = paired_source if paired_source.is_file() else raw_path
    else:
        source_path = raw_path
        out = raw_path.with_name(f"{raw_path.stem}{_RUNTIME_SUFFIX}")
    raw_text = source_path.read_text(encoding="utf-8", errors="ignore")
    rendered = render_subscription_config(raw_text)
    tmp = out.with_name(f"{out.name}.tmp")
    tmp.write_text(rendered, encoding="utf-8")
    tmp.replace(out)
    from app.domains.proxies import node_secret_files

    node_secret_files.tighten(out)
    node_secret_files.tighten(source_path)
    return out


def _subscription_id_of(config_path: Path | None) -> int | None:
    """从按订阅分文件的缓存路径反解订阅 id（`clash-sub-{id}.yaml`）。

    共享单文件路径（`config.yaml`）与任何非法命名返回 None——调用方据此
    决定是否写元数据 sidecar（没有订阅身份就没有归属对象）。
    """
    if config_path is None:
        return None
    stem = config_path.stem
    if not stem.startswith("clash-sub-"):
        return None
    try:
        return int(stem[len("clash-sub-"):])
    except ValueError:
        return None


def subscription_meta_path(data_dir: Path, sub_id: int) -> Path:
    """订阅缓存正文的响应头元数据 sidecar（`clash-sub-{id}.meta.json`）。

    面板名与流量头只存在于网络响应里，正文落盘时它们就丢了；降级回读缓存
    时便无从取值。这份 sidecar 把「上次成功拉取到的响应头」与正文一同持久化，
    成为标题/流量在离线场景下的**唯一事实源**。
    """
    return kernel_dir(data_dir) / f"clash-sub-{sub_id}.meta.json"


def read_subscription_meta(data_dir: Path, sub_id: int) -> dict:
    """读缓存 sidecar（缺失/损坏一律返回空 dict，不抛）。"""
    import json

    try:
        raw = subscription_meta_path(data_dir, sub_id).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def write_subscription_meta(data_dir: Path, sub_id: int, meta: dict) -> None:
    """写缓存 sidecar（best-effort：写失败不影响正文已落盘的事实）。"""
    import json

    path = subscription_meta_path(data_dir, sub_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        log_event(
            logger,
            "订阅缓存元数据写入失败（正文已落盘，不影响本次使用）",
            level=logging.WARNING,
            detail={"文件": str(path)},
        )


def invalidate_subscription_cache_identity(data_dir: Path, sub_id: int) -> None:
    """Mark an existing raw cache as unrelated to the subscription's new URL."""
    meta = read_subscription_meta(data_dir, sub_id)
    meta["urlIdentity"] = None
    write_subscription_meta(data_dir, sub_id, meta)


def remove_subscription_artifacts(data_dir: Path, sub_id: int) -> list[str]:
    """删除一条订阅的全部本地缓存产物（原文 + 生成配置 + 元数据），返回删掉的文件名。

    删除订阅时**必须**与数据库行、快照目录在同一收尾里调用：只删行不删文件，
    磁盘上会留下永远无人引用的孤儿（旧订阅的节点内容继续躺在 clash 目录里），
    既占体积也让「订阅已删除」在文件系统层面不成立。
    """
    removed: list[str] = []
    raw = subscription_config_path(data_dir, sub_id)
    for path in (
        raw,
        raw.with_name(f"{raw.stem}{_RUNTIME_SUFFIX}"),
        subscription_meta_path(data_dir, sub_id),
    ):
        try:
            path.unlink()
            removed.append(path.name)
        except FileNotFoundError:
            continue
        except OSError as e:  # noqa: BLE001 —— 回收尽力而为，失败不阻断删除
            log_event(
                logger,
                "订阅缓存文件删除失败（订阅行已删，文件成为孤儿）",
                level=logging.WARNING,
                detail={"文件": str(path), "原因": str(e)},
            )
    return removed


def parse_probe_lanes(config_text: str) -> list[tuple[str, int]]:
    """从内核配置读出探测 lane（组名, listener 端口），按注入序返回。"""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return []
    if not isinstance(doc, dict):
        return []
    listeners = doc.get("listeners")
    if not isinstance(listeners, list):
        return []
    lanes: list[tuple[str, int]] = []
    for entry in listeners:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name", ""))
        proxy = str(entry.get("proxy", ""))
        port = entry.get("port")
        if (
            name.startswith(PROBE_LANE_PREFIX)
            and proxy.startswith(PROBE_LANE_PREFIX)
            and isinstance(port, int)
        ):
            lanes.append((proxy, port))
    lanes.sort(key=lambda lane: lane[0])
    return lanes


# 非真实节点（控制器逻辑节点/内置）
_NON_NODE_TYPES = {
    "selector", "urltest", "fallback", "loadbalance", "relay",
    "direct", "reject", "rejectdrop", "compatible", "pass", "dns",
}


def parse_node_names(config_text: str) -> list[str]:
    """从订阅 yaml 的 proxies: 段解析节点名列表（轻量正则，避免 yaml 依赖）。

    兼容两种 yaml 风格：块状 `- name: xx` 与行内 `- { name: 'xx', type: ... }`。
    """
    names: list[str] = []
    in_proxies = False
    for line in config_text.splitlines():
        stripped = line.strip()
        if re.match(r"^proxies\s*:", line):
            in_proxies = True
            continue
        if in_proxies:
            if re.match(r"^[A-Za-z-]+\s*:", line):  # 下一个顶级键
                break
            m = re.match(
                r"-\s*(?:\{\s*)?name\s*:\s*(?:'([^']*)'|\"([^\"]*)\"|([^,}]+))",
                stripped,
            )
            if m:
                name = (m.group(1) or m.group(2) or m.group(3) or "").strip()
                if name:
                    names.append(name)
    return names


class _ReloadRejected(ValueError):
    """内核明确拒载新配置（HTTP 4xx）或新配置本身不合法。

    与「热重载通路故障」不同档：配置被拒时内核还好好跑着旧配置，
    正确动作是回滚磁盘、保留运行实例、把原因报给用户——回退重启只会
    让内核载着同一份坏配置起不来。"""


class ClashRuntime:
    """本地内核进程的启动 / 停止 / 状态。进程内单例。"""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.config_path: str | None = None
        self.controller_url: str | None = None
        self.secret: str = ""
        # 本次启动归属的订阅 URL——「内核在跑哪条订阅」的事实源。配置文本里
        # 不含订阅 URL（机场 yaml 是纯配置），靠文本匹配永远认不出来。
        self.subscription_url: str | None = None
        # 本次启动实际注入并生效的探测 lane（组名, 端口）——检测通道的事实源。
        # 不从磁盘配置读：订阅下载会在启动后把 config.yaml 覆写回原始文本
        # （内核在内存里照常跑着注入后的配置），磁盘内容不可信。
        self.probe_lanes: list[tuple[str, int]] = []
        self._startup_text: str | None = None  # 启动时的配置文本（重启判定用）
        # 混合入口就绪事实：启动/重载时验证过「端口真的在听」才为 True。
        # 内核对端口绑定失败只打日志不退出（订阅没写端口时解析出的默认值
        # 无人监听、端口被占同理），「进程在跑」与「入口可用」是两回事。
        self.entry_ready: bool = False

    def status(self) -> dict:
        running = self.process is not None and self.process.poll() is None
        return {
            "running": running,
            "port": self.port if running else None,
            "entryReady": bool(running and self.entry_ready and self.entry_listening()),
            "configPath": self.config_path,
            "controllerUrl": self.controller_url if running else None,
            "subscriptionUrl": self.subscription_url if running else None,
        }

    def entry_listening(self) -> bool:
        """混合入口必须由当前存活的内核进程监听。"""
        process = self.process
        if process is None or process.poll() is not None or not self.port:
            return False
        process_id = getattr(process, "pid", None)
        return bool(
            process_id and _port_listening(self.port)
            and _port_owned_by_process(self.port, process_id)
        )

    def _wait_mixed_port(self, timeout: float = 1.5) -> bool:
        """启动后等混合端口真的在听（控制器就绪 ≠ 入口就绪，池内核同款检查）。"""
        if not self.port:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                break  # 进程已退出：不必再等
            if _port_listening(self.port):
                return True
            time.sleep(0.1)
        log_event(
            logger,
            "Clash 内核混合端口未监听，入口暂不可用",
            level=logging.WARNING,
            detail={"端口": self.port, "配置": self.config_path},
        )
        return False

    def _kill_orphans(self, exe_path: str, config_path: str) -> int:
        """清理同 exe+config 的残留内核进程（服务被强杀后 mihomo 不随父进程退出）。

        只杀命令行包含本内核 exe 且带 -f 本配置的实例，不动用户自启的
        Clash / Verge；返回清理数。wmic 已从新版 Windows 移除，用 CIM。
        """
        escaped_cfg = config_path.replace("[", "`[").replace("]", "`]").replace("'", "''")
        ps_script = (
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
            "Get-CimInstance Win32_Process -Filter \"Name='mihomo.exe'\" | "
            "Where-Object { $_.CommandLine -like '*mihomo.exe*' -and "
            f"$_.CommandLine -like '*{escaped_cfg}*' }} | "
            "Select-Object -ExpandProperty ProcessId"
        )
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
        ).stdout or ""
        current = self.process.pid if self.process else -1
        killed = 0
        for line in out.splitlines():
            pid = line.strip()
            if pid.isdigit() and int(pid) != current:
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, timeout=10,
                )
                killed += 1
        if killed:
            log_event(
                logger,
                f"已清理 {killed} 个残留 Clash 内核进程",
                tag="已修复",
                detail={"清理数": killed},
            )
        return killed

    def start(
        self, exe_path: str, config_path: str, *,
        restart_if_changed: bool = False,
        force_restart: bool = False,
        inject_lanes: bool = True,
        subscription_url: str | None = None,
        mixed_port: int | None = None,
        allocate_mixed_port: bool = False,
        _port_retry: int = 0,
    ) -> dict:
        """启动内核。restart_if_changed：已在跑但配置文件内容较启动时
        有变化 → 重启生效（免重启刷新订阅节点用）；内容没变沿用实例。
        inject_lanes：注入探测 lane（节点检测的 20 并发通道）；池内核有
        自己的 lane 体系，传 False 保持其运行配置与对账口径纯净。
        subscription_url：本次启动归属的订阅（「在跑哪条」的事实源，
        检测归属与订阅重拉的重启开关都认它）。
        mixed_port：钉死本内核的混合监听口（程序工作端口）——给了就覆写
        配置里的端口，订阅写什么不再决定监听口；不给保持订阅原样（池内核）。
        force_restart：先校验完整的新配置，再替换现役进程。"""
        running = self.status()["running"]
        if running and not force_restart:
            disk_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
            validate_execution_config(disk_text)
            if not restart_if_changed:
                # 已在跑：同订阅（或调用方未带归属）幂等返回；带了**不同**的
                # 订阅 URL = 用户要求切换订阅，走下方完整启动
                target = (subscription_url or "").strip()
                if not target or self.subscription_url == target:
                    return self.status()
                log_event(
                    logger,
                    "切换订阅：停掉当前内核并按新订阅重启",
                    detail={"当前订阅": self.subscription_url, "新订阅": target},
                )
            else:
                if self.config_path == config_path and config_unchanged(
                    self._startup_text, disk_text
                ) and (mixed_port is None or self.port == mixed_port) and not allocate_mixed_port:
                    return self.status()
        config_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
        selected_port = _free_local_port() if allocate_mixed_port else mixed_port
        if selected_port:
            pinned = pin_mixed_port(config_text, selected_port)
            config_text = pinned
        stripped = strip_tun_section(config_text)
        config_text = stripped
        text = inject_probe_lanes(config_text) if inject_lanes else config_text
        controller_url, controller_secret = resolve_controller(text)
        text = _with_controller_secret(text, controller_secret)
        _validate_config_text(exe_path, text, Path(config_path).parent)

        try:
            ensure_kernel_files(Path(config_path).parent)
        except Exception as e:  # noqa: BLE001 —— 清理失败不阻断启动（psutil 缺失等）
            log_event(
                logger,
                "随包内核资产补齐失败，不影响启动",
                tag="忽略",
                level=logging.WARNING,
                detail={"原因": str(e)},
            )
        if running or self.process is not None:
            self.stop()
        try:
            self._kill_orphans(exe_path, config_path)
        except Exception as e:  # noqa: BLE001 —— 清理失败不阻断启动（psutil 缺失等）
            log_event(
                logger,
                "残留内核进程清理跳过",
                tag="跳过",
                level=logging.WARNING,
                detail={"原因": str(e)},
            )
        tmp_path = Path(f"{config_path}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(config_path)
        from app.domains.proxies import node_secret_files

        node_secret_files.tighten(Path(config_path))
        confirm_config_text(config_path, text)

        self.port = parse_mixed_port(text)
        # 控制器经命令行参数下发（-ext-ctl / -secret）：优先级高于配置文件，
        # 文件与命令行使用同一次生成的控制密钥。
        self.controller_url, self.secret = controller_url, controller_secret
        controller_addr = self.controller_url.split("://", 1)[-1]
        self.probe_lanes = parse_probe_lanes(text) if inject_lanes else []
        self.config_path = config_path
        self.subscription_url = (subscription_url or "").strip() or None
        self._startup_text = text
        # stdout/stderr 追加到内核目录的 kernel.log：内核对控制器绑定失败等
        # 异常只打日志不退出，没有这份日志这些失败完全不可见
        log_path = Path(config_path).parent / "kernel.log"
        log_file = open(log_path, "ab")  # noqa: SIM115 —— 句柄随子进程存活，父进程不持有
        self.process = spawn_kernel(
            exe_path,
            ["-f", config_path, "-d", str(Path(config_path).parent),
             "-ext-ctl", controller_addr, "-secret", self.secret],
            log_file=log_file,
        )
        log_file.close()
        self.entry_ready = bool(
            self._wait_mixed_port()
            and self.process is not None
            and _port_owned_by_process(self.port, self.process.pid)
        )
        if not self.entry_ready:
            failed_port = self.port
            self.stop()
            if allocate_mixed_port and _port_retry < 3:
                log_event(
                    logger, "动态代理端口启动校验失败，正在更换端口重试",
                    level=logging.WARNING, detail={"端口": failed_port},
                )
                return self.start(
                    exe_path, config_path, restart_if_changed=restart_if_changed,
                    force_restart=force_restart,
                    inject_lanes=inject_lanes, subscription_url=subscription_url,
                    allocate_mixed_port=True, _port_retry=_port_retry + 1,
                )
            raise RuntimeError(f"代理入口未由内核进程监听：{failed_port}")
        log_event(
            logger,
            "Clash 内核已启动",
            tag="成功",
            detail={
                "进程编号": self.process.pid,
                "混合端口": self.port,
                "控制器地址": self.controller_url,
            },
        )
        return self.status()

    def running_subscription_is(self, sub_url: str) -> bool:
        """内核当前跑的是否该订阅（按启动时记录的订阅 URL 判定）。

        订阅刷新的重启开关用：config.yaml 是共用缓存、会被后续任何一次下载
        覆盖，文件本身不是依据——「在跑哪条」的事实源是启动时登记的
        subscription_url，此时重启才不会把内核悄悄切到另一条订阅上。
        """
        target = (sub_url or "").strip()
        return bool(
            self.status()["running"]
            and self.subscription_url
            and target
            and self.subscription_url == target
        )

    def note_pool_config_reloaded(self, config_path: str) -> None:
        """池配置热重载后的**账目同步**：进程没动，但本实例记账的启动配置
        / 文本 / mixed-port 已随新配置变化。由池重建编排层在热通道成功后
        调用；controller/secret 不用更新（热重载沿用现役端点，两者不变）。"""
        text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
        self.config_path = config_path
        self._startup_text = text
        self.port = parse_mixed_port(text)

    def _running_kernel_text(
        self, config_path: str, *, mixed_port: int | None = None,
        exe_path: str | None = None,
    ) -> tuple[str, str]:
        """给**运行中**内核热重载用的配置文本：做合法性预检（YAML 可解析
        + 有节点段），不合法抛 _ReloadRejected——坏配置不该走到内核面前。
        返回 (注入 lane 后文本, 写盘前的文件原内容)：拒载回滚恢复的是**这个
        文件自己**被覆盖前的内容——每订阅各存一份缓存后，目标文件与运行中
        订阅经常不是同一条，用运行文本回滚会把别的订阅内容写进这份缓存。
        控制器端点由命令行固定；文件密钥同步当前进程的实际密钥。
        mixed_port 给了就一并钉死混合监听口（程序工作端口不随订阅走）。"""
        prev_file_text = ""
        if Path(config_path).is_file():
            prev_file_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
        text = prev_file_text
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise _ReloadRejected(f"新配置不是合法 YAML：{e}") from None
        if not isinstance(doc, dict) or not (doc.get("proxies") or doc.get("proxy-providers")):
            raise _ReloadRejected("新配置没有 proxies / proxy-providers 段")
        text = inject_probe_lanes(text)
        if mixed_port:
            text = pin_mixed_port(text, mixed_port)
        text = strip_tun_section(text)
        if self.secret:
            text = _with_controller_secret(text, self.secret)
        try:
            if exe_path:
                _validate_config_text(exe_path, text, Path(config_path).parent)
            else:
                validate_execution_config(text)
        except ValueError as exc:
            raise _ReloadRejected(str(exc)) from None
        tmp_path = Path(f"{config_path}.tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(config_path)
        from app.domains.proxies import node_secret_files

        node_secret_files.tighten(Path(config_path))
        return text, prev_file_text

    async def _verify_reloaded(self, injected_text: str) -> None:
        """热重载生效性校验：控制器活着 + 新配置的节点真的出现在内核里 +
        探测 lane 端口在听。任一不满足抛异常，调用方回退进程重启。"""
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
        async with httpx.AsyncClient(timeout=8, headers=headers, trust_env=False) as client:
            version = await client.get(f"{self.controller_url}/version")
            if version.status_code != 200:
                raise RuntimeError(f"重载后控制器不可用（HTTP {version.status_code}）")
            resp = await client.get(f"{self.controller_url}/proxies")
            resp.raise_for_status()
            observed = set((resp.json() or {}).get("proxies", {}).keys())
        expected = parse_node_names(injected_text)
        missing = [n for n in expected if n not in observed]
        if expected and missing:
            raise RuntimeError(f"重载后内核缺 {len(missing)} 个新配置节点（如 {missing[0]!r}）")
        lanes = parse_probe_lanes(injected_text)
        if lanes and not any(_port_listening(port) for _g, port in lanes):
            raise RuntimeError("重载后探测 lane 端口无一在听")
        # 混合入口与 lane 同一道就绪检查：重载会重绑 listener，新配置的
        # 混合端口（可能随订阅变了）不在听 = 入口不可用，走回退重启。
        new_port = parse_mixed_port(injected_text)
        process_id = getattr(self.process, "pid", None)
        if new_port and not (
            process_id and _port_listening(new_port)
            and _port_owned_by_process(new_port, process_id)
        ):
            raise RuntimeError(f"重载后混合端口 {new_port} 未由当前内核监听")

    async def ensure_running(
        self, exe_path: str, config_path: str, *, subscription_url: str | None = None,
        mixed_port: int | None = None, allocate_mixed_port: bool = False,
        force_restart: bool = False,
    ) -> dict:
        """确保内核跑着指定订阅的配置：没跑就启动；在跑时**热重载**生效
        （切换订阅 / 同订阅内容更新都走控制器 PUT /configs，内核进程不动——
        与 Clash Verge Rev 的换配置路径同款）；控制器不可达或重载校验不通过
        才回退进程重启。返回 status + {started, reloaded} 两个标记。
        mixed_port：钉死混合监听口（程序工作端口），当前监听口与它不符时
        不走幂等空转，重载/重启收敛到新端口。"""
        sub_url = (subscription_url or "").strip() or None
        running = self.process is not None and self.process.poll() is None
        if running and allocate_mixed_port:
            if self.entry_listening():
                mixed_port = self.port
                allocate_mixed_port = False
            else:
                status = self.start(
                    exe_path, config_path, subscription_url=sub_url,
                    allocate_mixed_port=True, force_restart=True,
                )
                return {**status, "started": True, "reloaded": False}
        if not running:
            status = self.start(
                exe_path, config_path, subscription_url=sub_url, mixed_port=mixed_port,
                allocate_mixed_port=allocate_mixed_port,
            )
            return {**status, "started": True, "reloaded": False}
        if force_restart:
            status = self.start(
                exe_path, config_path, subscription_url=sub_url, mixed_port=mixed_port,
                allocate_mixed_port=allocate_mixed_port, force_restart=True,
            )
            return {**status, "started": True, "reloaded": False}

        candidate_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
        validate_execution_config(candidate_text)
        same_sub = bool(sub_url and self.subscription_url == sub_url)
        if (
            same_sub
            and self.config_path == config_path
            and (mixed_port is None or self.port == mixed_port)
            and self.entry_listening()
            and config_unchanged(
                self._startup_text, candidate_text,
            )
        ):
            # 同订阅且订阅内容没变：不重载（幂等）
            return {**self.status(), "started": False, "reloaded": False}

        prev_file_text = ""
        wrote = False
        try:
            injected, prev_file_text = self._running_kernel_text(
                config_path, mixed_port=mixed_port, exe_path=exe_path,
            )
            wrote = True
            try:
                confirm_config_text(config_path, injected)
            except KernelPrecheckError as exc:
                raise _ReloadRejected(str(exc)) from None
            headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
            async with httpx.AsyncClient(timeout=15, headers=headers, trust_env=False) as client:
                put = await client.put(
                    f"{self.controller_url}/configs?force=true",
                    json={"path": config_path},
                )
                if put.status_code >= 300:
                    raise _ReloadRejected(f"内核拒绝新配置（HTTP {put.status_code}）")
            await self._verify_reloaded(injected)
        except _ReloadRejected as e:
            # 内核明确拒载 / 配置本身不合法：把目标文件回滚到本进程写入前的
            # 内容，内核继续跑旧配置——回退重启只会载着同一份坏配置起不来。
            # 预检阶段（写盘前）被拒时文件本来就没动，无需回滚。
            if wrote:
                if prev_file_text:
                    Path(config_path).write_text(prev_file_text, encoding="utf-8")
                else:
                    Path(config_path).unlink(missing_ok=True)  # 本次新建的缓存，删掉
            log_event(
                logger,
                "内核拒绝热重载新配置，已保留当前运行配置",
                level=logging.WARNING,
                detail={"原因": str(e)},
            )
            raise ValueError(str(e)) from None
        except Exception as e:  # noqa: BLE001 —— 通路故障才回退进程重启
            log_event(
                logger,
                "配置热重载失败，回退内核重启",
                tag="降级",
                level=logging.WARNING,
                detail={"异常类型": type(e).__name__, "原因": str(e)},
            )
            self.stop()
            status = self.start(
                exe_path, config_path, subscription_url=sub_url, mixed_port=mixed_port,
                allocate_mixed_port=allocate_mixed_port,
            )
            return {**status, "started": True, "reloaded": False}

        self.config_path = config_path
        self._startup_text = injected
        self.port = parse_mixed_port(injected)
        self.probe_lanes = parse_probe_lanes(injected)
        self.subscription_url = sub_url or self.subscription_url
        # 混合端口在 _verify_reloaded 已验证在听，入口事实随之落章
        self.entry_ready = True
        log_event(
            logger,
            "内核配置已热重载，进程未重启",
            tag="成功",
            detail={"订阅": self.subscription_url, "混合端口": self.port},
        )
        return {**self.status(), "started": False, "reloaded": True}

    def stop(self) -> dict:
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
            except ProcessLookupError:
                pass
            log_event(logger, "Clash 内核已停止")
        self.process = None
        self.controller_url = None
        self.subscription_url = None
        self.probe_lanes = []
        self._startup_text = None
        self.port = None
        self.entry_ready = False
        return self.status()

    def _download_attempts(
        self, proxy_url: str | list[str] | None = None
    ) -> list[tuple[str | None, str]]:
        """实例包装：补上本内核运行端口后走模块级通道链。端口没在听
        （进程活着但入口是死的）就不交这条通道——否则每次刷新先白等一轮。"""
        running = (
            self.status()["running"] and self.port and self.entry_listening()
        )
        return _download_attempts(self.port if running else None, proxy_url)

    async def download_subscription(
        self, sub_url: str, data_dir: Path, proxy_url: str | list[str] | None = None,
        *, target_path: Path | None = None, snapshot_only: bool = False,
    ) -> dict:
        """Return the current subscription snapshot in the legacy config-file location."""
        from app.domains.proxypool.subscription import (
            detect_format,
            latest_snapshot,
            parse_nodes,
            subscription_url_identity,
        )

        path = target_path or kernel_dir(data_dir) / "config.yaml"
        meta_id = _subscription_id_of(target_path)
        saved = [proxy_url] if isinstance(proxy_url, str) else list(proxy_url or [])
        if meta_id is not None:
            from datetime import datetime

            from app.core.database import get_session_factory
            from app.domains.proxies.models import ProxySubscription
            from app.domains.proxies.subscription_secret import open_url
            from app.domains.proxypool.bootstrap import sync_subscriptions

            async with get_session_factory()() as session:
                subscription = await session.get(ProxySubscription, meta_id)
                if subscription is not None:
                    stored_url = open_url(subscription.url)
                    if stored_url != sub_url:
                        raise ValueError("订阅地址与缓存归属不一致")
                    sync = None
                    if snapshot_only:
                        snapshot = await latest_snapshot(session, meta_id, url=sub_url)
                        cached = True
                    else:
                        sync = await sync_subscriptions(
                            session, data_dir=data_dir, now=datetime.now(),
                            only_sub_id=meta_id, saved_proxies=saved,
                        )
                        snapshot = sync.snapshots.get(meta_id)
                        cached = snapshot is None
                    if snapshot is None and not snapshot_only:
                        snapshot = await latest_snapshot(
                            session, meta_id, url=sub_url,
                        )
                    if snapshot is None:
                        if snapshot_only:
                            cached_meta = read_subscription_meta(data_dir, meta_id)
                            requested_identity = subscription_url_identity(sub_url)
                            stored_identity = subscription.cache_url_identity
                            if stored_identity is not None and stored_identity != requested_identity:
                                raise RuntimeError(
                                    "没有可恢复的成功快照或有效订阅缓存"
                                )
                            try:
                                cached_raw = path.read_bytes()
                                cached_nodes = parse_nodes(
                                    cached_raw, detect_format(cached_raw),
                                )
                            except Exception as exc:  # noqa: BLE001
                                raise RuntimeError(
                                    "没有可恢复的成功快照或有效订阅缓存"
                                ) from exc
                            return {
                                "path": str(path),
                                "title": cached_meta.get("title") or
                                    subscription_auto_name({}, sub_url),
                                "userinfo": cached_meta.get("userinfo"),
                                "nodes": len(cached_nodes),
                                "cached": True,
                                "_sync_result": None,
                            }
                        detail = (sync.failures.get(meta_id) if sync is not None else None)
                        detail = detail or "订阅没有可恢复的成功快照"
                        raise RuntimeError(detail)

                    path.parent.mkdir(parents=True, exist_ok=True)
                    if snapshot.provider_nodes:
                        from app.domains.proxies.runtime_config import expand_provider_nodes

                        runtime_source = expand_provider_nodes(
                            snapshot.raw_content.decode("utf-8"), snapshot.provider_nodes
                        ).encode("utf-8")
                    else:
                        runtime_source = snapshot.raw_content
                    path.write_bytes(runtime_source)
                    headers = httpx.Headers({
                        key: value for key, value in {
                            "profile-title": snapshot.profile_title,
                            "content-disposition": snapshot.content_disposition,
                        }.items() if value
                    })
                    title = subscription_auto_name(headers, sub_url)
                    userinfo = snapshot.userinfo
                    if not cached:
                        write_subscription_meta(
                            data_dir, meta_id,
                            {"title": title, "userinfo": userinfo,
                             "fetchedAt": int(time.time() * 1000)},
                        )
                    else:
                        cached_meta = read_subscription_meta(data_dir, meta_id)
                        title = cached_meta.get("title") or title
                        userinfo = cached_meta.get("userinfo") or userinfo
                    return {
                        "path": str(path), "title": title, "userinfo": userinfo,
                        "nodes": snapshot.node_count, "cached": cached,
                        "_sync_result": sync,
                    }

        raise RuntimeError("订阅缓存归属未解析：目标路径不是 clash-sub-{id} 形态")

runtime = ClashRuntime()
# 池专用实例：与订阅功能那个 `runtime` 是**两套东西**——配置目录、`-d`、
# controller、mixed-port、生命周期互不相干。共用实例会让一次池重建停掉订阅内核
# （反之亦然），所以隔离必须是结构性的，不靠调用方自觉。
pool_runtime = ClashRuntime()
