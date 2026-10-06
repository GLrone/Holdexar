"""Clash 内核管理：随包内核就位 / 检测 / 网络下载兜底 / 订阅启动 / 停止。

内核与 GeoIP 数据**随发行包分发**（版本固定在 kernel_release.MIHOMO_VERSION，
源目录 assets/clash → 打包后随资源目录），用户机器上不再检索本机装没装 Clash：
内核由 ensure_kernel() 复制进 data/clash/，detect_kernel() 只认这一处。
网络下载（download_kernel*）退居兜底，只服务两类场景：源码 clone 尚未补资产、
用户手工删除了 data/clash/ 里的可执行文件。

订阅下载的链式引导：直连失败且内核已在跑时经本地混合端口重试。
红线：本模块不内置任何订阅源，订阅 URL 只能来自用户设置。
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import shutil
import socket
import subprocess
import urllib.parse
import zipfile
from collections.abc import Sequence
from pathlib import Path

import httpx
import yaml

from app.core.app_info import APP_SLUG
from app.core.config import get_settings
from app.core.logging import log_event
from app.domains.proxies.kernel_release import (
    GEO_ASSETS,
    MIHOMO_VERSION,
    MIRRORS,
    is_windows,
    kernel_filename,
    mihomo_url,
)

logger = logging.getLogger(__name__)

# 兼容旧引用名：镜像链（网络下载兜底用）与内核数据目录需就位的 GeoIP 数据
_MIRRORS = MIRRORS
_GEO_FILES = tuple(GEO_ASSETS)


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

    GeoIP 数据不在此列：滚动数据无版本可比，维持只补缺失不覆盖。
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


def _kernel_process_alive() -> bool:
    """内核进程存活探测：真孤儿（非本 runtime 实例）的兜底。

    tasklist 快速过滤 mihomo 进程；查询失败（Windows 专属命令、非 Windows
    环境等）返回 False——探测只是保守替换的额外闸门，失败不阻断补资产。
    """
    if not is_windows():
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {kernel_filename()}", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=10,
            encoding="utf-8", errors="replace",
        ).stdout
    except Exception:  # noqa: BLE001
        return False
    return kernel_filename().lower() in (out or "").lower()


def _copy_bundled(kernel_path: Path, names: Sequence[str]) -> dict:
    """把随包目录里的 `names` 补齐进内核目录（只补缺失，同名一律不覆盖）。

    GeoIP 数据的「不覆盖」是刻意的：用户可能自己更新过数据，静默覆盖
    等于替用户做决定。可执行文件的升级换代另走 _maybe_upgrade_kernel
    （版本感知，见其 docstring）。返回 {"copied": [...], "missing": [...]}。
    """
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
    """内核目录补齐：可执行文件 + GeoIP 数据（随包资产为唯一来源）。

    入参是**内核目录本身**（config.yaml 所在处，也是 mihomo `-d` 的工作目录）；
    以 data_dir 为入参的版本见 ensure_kernel()。
    """
    result = _copy_bundled(kernel_path, (kernel_filename(), *_GEO_FILES))
    upgrade = _maybe_upgrade_kernel(kernel_path)
    if upgrade:
        result["upgraded"] = upgrade
    return result


def ensure_kernel(data_dir: Path) -> dict:
    """随包内核就位（data_dir 版本）：把内核与 GeoIP 数据补进 data/clash/。

    随包资产落到用户数据目录的唯一入口。返回
    {"kernelReady": bool, "copied": [...], "missing": [...], "bundleDir": str,
     "upgraded": str | None}；发行包缺内核（源码 clone 未补资产）时
    kernelReady=False，由调用方决定是回退网络下载还是提示用户。
    """
    result = _copy_bundled(kernel_dir(data_dir), (kernel_filename(), *_GEO_FILES))
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
    elif result["missing"]:
        log_event(
            logger,
            "内核目录缺少部分文件，内核启动时会尝试自行下载（直连网络下可能卡住）",
            detail={"缺失文件": "、".join(result["missing"])},
        )
    return {
        "kernelReady": ready,
        "copied": result["copied"],
        "missing": result["missing"],
        "upgraded": upgrade,
        "bundleDir": str(bundled_clash_dir()),
    }


def ensure_geo_files(data_dir: Path) -> dict:
    """内核数据目录缺 GeoIP 库时，从随包目录补齐。

    只补缺失文件、同名不覆盖；随包也没有的（如 ASN 数据）交给内核自行下载。
    返回 {"copied": [...], "missing": [...]}。
    """
    return _copy_bundled(kernel_dir(data_dir), _GEO_FILES)


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
        if decoded:
            return decoded
    return None


# 机场面板普遍校验 UA：httpx 默认 UA 会被 403，伪装 Clash 系客户端（内核拉取
# provider 时的同款标识）。**必须含 "clash" 关键字**——面板按 UA 分流订阅格式，
# 含 clash/meta 才回 Clash YAML；只写 mihomo 会被当成通用客户端回落 base64
# 节点表（本项目校验只认 YAML，「更新失败」即此因）。版本随 kernel_release。
_SUB_HEADERS = {"User-Agent": f"clash.meta/{MIHOMO_VERSION.lstrip('v')}"}


def _port_reachable(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """本地端口探活（订阅/内核下载借道本地混合端口的判据）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# 本机常见混合端口：7890 是旧版默认，7897 是 Verge 默认
LOCAL_MIXED_PORTS: tuple[int, ...] = (7890, 7897)


def local_mixed_channels(exclude_port: int | None = None) -> list[str]:
    """本机**在听**的常见混合端口，供下载与订阅抓取借道。

    只探"端口在听"——出口是否可用无人担保，所以调用方一律把它排在自己
    管理、自己体检过的出口之后。
    """
    return [
        f"http://127.0.0.1:{port}"
        for port in LOCAL_MIXED_PORTS
        if port != exclude_port and _port_reachable(port)
    ]


def _download_attempts(
    runtime_port: int | None = None, proxy_url: str | list[str] | None = None
) -> list[tuple[str | None, str]]:
    """下载通道链：本内核代理 → 项目保存的可用代理 → 本地常见混合端口 → 直连。

    订阅/内核下载全是"鸡生蛋"场景：首次添加订阅时本内核必然没跑
    （无 config.yaml 起不来），面板域名又多被墙——此时直连是唯一通道，
    排序自然退化为「只有直连」。

    **代理通道排在前、直连垫底**：订阅面板域名绝大多数时候直连不通，
    直连打头意味着每次刷新都先等满一次超时；经自己管理的出口（内核 /
    体检过的已保存代理）取订阅与 Clash Verge Rev 的「经当前代理更新」
    同向。已保存代理排在本机混合端口之前——自己管理、自己体检过的
    出口优先于借道用户自启的 Verge（后者只探端口在听，出口是否可用
    无人担保）。

    `proxy_url` 可传单个 URL，也可传 URL 列表（逐个作为后续通道）。
    传列表是给「项目保存的可用代理」用的：池里挑一条也可能它自己正
    失效，一次只试一条等于把「借道」变成掷骰子。
    """
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
        if _port_reachable(port):
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
        result = subprocess.run(
            [str(exe_path), "-v"], capture_output=True, timeout=10,
            encoding="utf-8", errors="replace",
        )
        first = (result.stdout or result.stderr).splitlines()
        return first[0] if first else None
    except Exception:  # noqa: BLE001
        return None


def parse_mixed_port(config_text: str) -> int:
    """从订阅 yaml 文本解析混合端口（mixed-port / port），默认 7890。"""
    match = re.search(r"^\s*mixed-port\s*:\s*(\d+)", config_text, re.MULTILINE)
    if match:
        return int(match.group(1))
    match = re.search(r"^\s*port\s*:\s*(\d+)", config_text, re.MULTILINE)
    if match:
        return int(match.group(1))
    return 7890


CONTROLLER_PORT = 19090
CONTROLLER_SECRET = APP_SLUG


def resolve_controller(config_text: str) -> tuple[str, str]:
    """解析本次启动的控制器端点，返回 (base_url, secret)。

    控制器只经内核命令行参数（-ext-ctl / -secret）下发——配置文件里的
    external-controller / secret 行一律不信任也不改写：订阅 yaml 自带的
    控制器行可能指向已被占用的端口，沿用会让所有控制通信打到占用者的
    内核上（PUT /configs 会把配置灌进运行中的其他内核进程，必须写自有内核的端点）。
    端口被占时从 CONTROLLER_PORT 起避让扫描（内核对「绑定失败但继续跑」
    的行为只打日志不退出，必须启动前保证端口空闲）。
    """
    match = re.search(r"^external-controller\s*:\s*['\"]?([^'\"\n#]+)", config_text, re.MULTILINE)
    secret_match = re.search(r"^secret\s*:\s*['\"]?([^'\"\n#]+)", config_text, re.MULTILINE)
    secret = secret_match.group(1).strip() if secret_match else CONTROLLER_SECRET
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
    """启动前用内核 `-t` 校验配置（不启动进程），不合法抛 ValueError。

    -d 必须给 geo 数据已就位的真实内核目录：`-t` 遇到 GEOIP 规则而目录里
    没有 MMDB 时会现场联网下载并长时间阻塞（20s 量级不返回），独立临时目录不可用。
    校验通过（退出码 0）正常放行；被拒（退出码非 0）抛出内核的 error 行；
    校验超时视为不可校验（geo 缺失触发下载的场景），放行启动由内核自行处理。
    """
    try:
        result = subprocess.run(
            [exe_path, "-t", "-f", config_path, "-d", str(work_dir)],
            capture_output=True, timeout=CONFIG_TEST_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired:
        log_event(
            logger,
            "内核配置校验超时（可能在下载 GeoIP 数据），放行启动",
            tag="跳过",
            level=logging.WARNING,
        )
        return
    if result.returncode == 0:
        return
    lines = [
        line.split("level=error", 1)[1].strip()
        for line in ((result.stdout or "") + (result.stderr or "")).splitlines()
        if "level=error" in line
    ]
    detail = "；".join(lines[:5]) or f"退出码 {result.returncode}"
    raise ValueError(f"内核拒绝配置（-t 校验未通过）：{detail}")


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
    """配置的订阅内容视图：剥掉探测 lane 后的 yaml 结构（等价比较用）。"""
    try:
        doc = yaml.safe_load(config_text)
    except yaml.YAMLError:
        return None
    if not isinstance(doc, dict):
        return None
    view = dict(doc)
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
PROBE_LANE_PREFIX = "HlProbeLane"
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


def _port_listening(port: int) -> bool:
    """端口有进程在听（connect 探测）——热重载后校验 lane listener 存活用。"""
    with socket.socket() as sock:
        sock.settimeout(0.3)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _alloc_probe_ports(count: int, *, avoid: set[int]) -> list[int]:
    """确定性扫空闲端口；凑不满 count 就给多少用多少（空列表 = 不注入）。"""
    ports: list[int] = []
    candidate = PROBE_LANE_PORT_BASE
    ceiling = PROBE_LANE_PORT_BASE + PROBE_LANE_PORT_SPAN
    while len(ports) < count and candidate < ceiling:
        if candidate not in avoid and _port_free(candidate):
            ports.append(candidate)
        candidate += 1
    return ports


def inject_probe_lanes(config_text: str) -> str:
    """把探测 lane 注入内核配置（幂等）：剔除本前缀旧条目后追加
    PROBE_LANE_COUNT 组 `select`(include-all) + 同数 mixed listener。
    端口避让本配置自己的混合口与控制器口。不可解析的配置原样返回；
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
    ports = _alloc_probe_ports(PROBE_LANE_COUNT, avoid=avoid)
    if not ports:
        return config_text
    groups = [
        g for g in (doc.get("proxy-groups") or [])
        if not (isinstance(g, dict) and str(g.get("name", "")).startswith(PROBE_LANE_PREFIX))
    ]
    listeners = [
        l for l in (doc.get("listeners") or [])
        if not (isinstance(l, dict) and str(l.get("name", "")).startswith(PROBE_LANE_PREFIX))
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
    """
    return kernel_dir(data_dir) / f"clash-sub-{sub_id}.yaml"


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

    def status(self) -> dict:
        running = self.process is not None and self.process.poll() is None
        return {
            "running": running,
            "port": self.port,
            "configPath": self.config_path,
            "controllerUrl": self.controller_url if running else None,
            "subscriptionUrl": self.subscription_url if running else None,
        }

    def _kill_orphans(self, exe_path: str, config_path: str) -> int:
        """清理同 exe+config 的残留内核进程（服务被强杀后 mihomo 不随父进程退出）。

        只杀命令行包含本内核 exe 且带 -f 本配置的实例，不动用户自启的
        Clash / Verge；返回清理数。wmic 已从新版 Windows 移除，用 CIM。
        """
        escaped_cfg = config_path.replace("\\", "\\")
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
        inject_lanes: bool = True,
        subscription_url: str | None = None,
    ) -> dict:
        """启动内核。restart_if_changed：已在跑但配置文件内容较启动时
        有变化 → 重启生效（免重启刷新订阅节点用）；内容没变沿用实例。
        inject_lanes：注入探测 lane（节点检测的 20 并发通道）；池内核有
        自己的 lane 体系，传 False 保持其运行配置与对账口径纯净。
        subscription_url：本次启动归属的订阅（「在跑哪条」的事实源，
        检测归属与订阅重拉的重启开关都认它）。"""
        if self.status()["running"]:
            if not restart_if_changed:
                # 已在跑：同订阅（或调用方未带归属）幂等返回；带了**不同**的
                # 订阅 URL = 用户要求切换订阅 → 停掉当前实例，走下方完整启动
                target = (subscription_url or "").strip()
                if not target or self.subscription_url == target:
                    return self.status()
                log_event(
                    logger,
                    "切换订阅：停掉当前内核并按新订阅重启",
                    detail={"当前订阅": self.subscription_url, "新订阅": target},
                )
                self.stop()
            else:
                disk_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
                if self.config_path == config_path and config_unchanged(
                    self._startup_text, disk_text
                ):
                    return self.status()
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
        config_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
        validate_config(exe_path, config_path, Path(config_path).parent)
        self.port = parse_mixed_port(config_text)
        # 控制器经命令行参数下发（-ext-ctl / -secret）：优先级高于配置文件，
        # 配置里订阅自带的控制器行不再构成劫持面，文件本身保持订阅原样
        self.controller_url, self.secret = resolve_controller(config_text)
        controller_addr = self.controller_url.split("://", 1)[-1]
        self.probe_lanes = []
        text = config_text
        if inject_lanes:
            text = inject_probe_lanes(config_text)
            self.probe_lanes = parse_probe_lanes(text)
            if text != config_text:
                Path(config_path).write_text(text, encoding="utf-8")
        self.config_path = config_path
        self.subscription_url = (subscription_url or "").strip() or None
        self._startup_text = text if inject_lanes else config_text
        # stdout/stderr 追加到内核目录的 kernel.log：内核对控制器绑定失败等
        # 异常只打日志不退出，没有这份日志这些失败完全不可见
        log_path = Path(config_path).parent / "kernel.log"
        log_file = open(log_path, "ab")  # noqa: SIM115 —— 句柄随子进程存活，父进程不持有
        self.process = subprocess.Popen(
            [exe_path, "-f", config_path, "-d", str(Path(config_path).parent),
             "-ext-ctl", controller_addr, "-secret", self.secret],
            stdout=log_file,
            stderr=subprocess.STDOUT,
        )
        log_file.close()
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

    def _running_kernel_text(self, config_path: str) -> tuple[str, str]:
        """给**运行中**内核热重载用的配置文本：做合法性预检（YAML 可解析
        + 有节点段），不合法抛 _ReloadRejected——坏配置不该走到内核面前。
        返回 (注入 lane 后文本, 写盘前的文件原内容)：拒载回滚恢复的是**这个
        文件自己**被覆盖前的内容——每订阅各存一份缓存后，目标文件与运行中
        订阅经常不是同一条，用运行文本回滚会把别的订阅内容写进这份缓存。
        控制器不需写入文本：端点由启动时的命令行参数固定，热重载不会把它
        搬回配置文件里的值（PUT /configs 之后控制器仍保持在命令行端点）。"""
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
        Path(config_path).write_text(text, encoding="utf-8")
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

    async def ensure_running(
        self, exe_path: str, config_path: str, *, subscription_url: str | None = None,
    ) -> dict:
        """确保内核跑着指定订阅的配置：没跑就启动；在跑时**热重载**生效
        （切换订阅 / 同订阅内容更新都走控制器 PUT /configs，内核进程不动——
        与 Clash Verge Rev 的换配置路径同款）；控制器不可达或重载校验不通过
        才回退进程重启。返回 status + {started, reloaded} 两个标记。"""
        sub_url = (subscription_url or "").strip() or None
        if not self.status()["running"]:
            status = self.start(exe_path, config_path, subscription_url=sub_url)
            return {**status, "started": True, "reloaded": False}

        same_sub = bool(sub_url and self.subscription_url == sub_url)
        if same_sub and self.config_path == config_path and config_unchanged(
            self._startup_text,
            Path(config_path).read_text(encoding="utf-8", errors="ignore"),
        ):
            # 同订阅且订阅内容没变：不重载（幂等）
            return {**self.status(), "started": False, "reloaded": False}

        prev_file_text = ""
        wrote = False
        try:
            injected, prev_file_text = self._running_kernel_text(config_path)
            wrote = True
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
            status = self.start(exe_path, config_path, subscription_url=sub_url)
            return {**status, "started": True, "reloaded": False}

        self.config_path = config_path
        self._startup_text = injected
        self.port = parse_mixed_port(injected)
        self.probe_lanes = parse_probe_lanes(injected)
        self.subscription_url = sub_url or self.subscription_url
        log_event(
            logger,
            "内核配置已热重载，进程未重启",
            tag="成功",
            detail={"订阅": self.subscription_url, "混合端口": self.port},
        )
        return {**self.status(), "started": False, "reloaded": True}

    def stop(self) -> dict:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            log_event(logger, "Clash 内核已停止")
        self.process = None
        self.controller_url = None
        self.subscription_url = None
        self.probe_lanes = []
        self._startup_text = None
        return self.status()

    def _download_attempts(
        self, proxy_url: str | list[str] | None = None
    ) -> list[tuple[str | None, str]]:
        """实例包装：补上本内核运行端口后走模块级通道链。"""
        running = self.status()["running"] and self.port
        return _download_attempts(self.port if running else None, proxy_url)

    async def fetch_subscription_headers(
        self, sub_url: str, proxy_url: str | None = None
    ) -> dict | None:
        """轻量拉取订阅响应头（subscription-userinfo），不动 config.yaml。

        流量统计实时回填用：面板流量随消耗增长，仅在启动时落库会滞后
        数十 GB。通道降级同 download_subscription；全部失败返回 None
        （缓存不适用于流量——头是实时值）。
        """
        attempts = self._download_attempts(proxy_url)
        for attempt_proxy, label in attempts:
            try:
                async with httpx.AsyncClient(
                    timeout=20, proxy=attempt_proxy, follow_redirects=True,
                    headers=_SUB_HEADERS, trust_env=False,
                ) as client:
                    resp = await client.get(sub_url)
                    resp.raise_for_status()
                    return {"userinfo": resp.headers.get("subscription-userinfo")}
            except Exception as e:  # noqa: BLE001 —— 逐通道降级
                log_event(
                    logger,
                    f"订阅响应头经 {label} 拉取失败，换下一通道",
                    level=logging.WARNING,
                    detail={"通道": label, "原因": str(e)},
                )
        return None

    async def download_subscription(
        self, sub_url: str, data_dir: Path, proxy_url: str | list[str] | None = None,
        *, target_path: Path | None = None,
    ) -> dict:
        """下载用户订阅 yaml 到缓存文件（缺省 data/clash/config.yaml；每条
        订阅各存一份时由调用方传 `target_path`，切换订阅因此只是本地文件
        热重载，不再现场下载）。

        通道（_download_attempts）：经内核代理 → 已保存代理 → 本地混合端口
        → 直连，**各通道并发竞速**——第一个拿到有效配置的通道获胜、其余
        取消。顺序逐个试会让被墙通道的超时串行累加（一次刷新卡几分钟）。
        全部通道失败回退上次成功下载的本地缓存。
        返回 {path, title, userinfo, nodes, cached}；title 取自 profile-title
        响应头（订阅名），userinfo 为 subscription-userinfo 流量头原文。
        """
        path = target_path or kernel_dir(data_dir) / "config.yaml"
        attempts = self._download_attempts(proxy_url)

        async def _fetch(proxy: str | None) -> tuple[str, httpx.Response]:
            async with httpx.AsyncClient(
                timeout=45, proxy=proxy, follow_redirects=True,
                headers=_SUB_HEADERS, trust_env=False,
            ) as client:
                resp = await client.get(sub_url)
                resp.raise_for_status()
                text = resp.text
                if "proxies:" not in text and "proxy-providers:" not in text:
                    raise ValueError("订阅内容不是有效的 Clash 配置（缺少 proxies 段）")
                if not parse_node_names(text) and "proxy-providers:" not in text:
                    raise ValueError("订阅内容解析到 0 个节点，疑似面板返回异常页")
                try:
                    yaml.safe_load(text)
                except yaml.YAMLError as e:
                    # 竞速下截断的响应体也能通过前两道字符串检查——YAML 完整
                    # 解析是最后一道闸，坏响应在通道内淘汰，不进缓存
                    raise ValueError(f"订阅内容 YAML 不完整（{e}）") from None
                return text, resp

        tasks = {
            asyncio.create_task(_fetch(proxy)): (proxy, label)
            for proxy, label in attempts
        }
        pending = set(tasks)
        last_err: Exception | None = None
        try:
            while pending:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    proxy, label = tasks[task]
                    try:
                        text, resp = task.result()
                    except Exception as e:  # noqa: BLE001 —— 单通道失败不影响竞速
                        last_err = e
                        log_event(
                            logger,
                            f"订阅下载经 {label} 失败，继续等待其它通道",
                            level=logging.WARNING,
                            detail={"通道": label, "原因": str(e)},
                        )
                        continue
                    for t in pending:
                        t.cancel()
                    nodes = parse_node_names(text)
                    path.write_text(text, encoding="utf-8")
                    # 自动取名链：profile-title → Content-Disposition → URL 末段
                    title = subscription_auto_name(resp.headers, sub_url)
                    userinfo = resp.headers.get("subscription-userinfo")
                    log_event(
                        logger,
                        f"订阅下载成功：{len(nodes)} 个节点",
                        tag="成功",
                        detail={"通道": label, "节点数": len(nodes), "面板名": title},
                    )
                    return {
                        "path": str(path), "title": title, "userinfo": userinfo,
                        "nodes": len(nodes), "cached": False,
                    }
        finally:
            for t in pending:
                t.cancel()
        if path.is_file():
            log_event(
                logger,
                "订阅全部通道下载失败，回退上次成功的本地缓存",
                tag="降级",
                level=logging.WARNING,
                detail={"缓存文件": str(path)},
            )
            cached_text = path.read_text(encoding="utf-8", errors="ignore")
            return {
                "path": str(path), "title": None, "userinfo": None,
                "nodes": len(parse_node_names(cached_text)), "cached": True,
            }
        raise last_err  # type: ignore[misc]


runtime = ClashRuntime()
# 池专用实例：与订阅功能那个 `runtime` 是**两套东西**——配置目录、`-d`、
# controller、mixed-port、生命周期互不相干。共用实例会让一次池重建停掉订阅内核
# （反之亦然），所以隔离必须是结构性的，不靠调用方自觉。
pool_runtime = ClashRuntime()
