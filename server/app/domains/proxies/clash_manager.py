"""Clash 内核管理：随包内核就位 / 检测 / 网络下载兜底 / 订阅启动 / 停止。

内核与 GeoIP 数据**随发行包分发**（版本固定在 kernel_release.MIHOMO_VERSION，
源目录 assets/clash → 打包后随资源目录），用户机器上不再检索本机装没装 Clash：
内核由 ensure_kernel() 复制进 data/clash/，detect_kernel() 只认这一处。
网络下载（download_kernel*）退居兜底，只服务两类场景：源码 clone 尚未补资产、
用户手工删掉了 data/clash/ 里的可执行文件。

订阅下载的链式引导：直连失败且内核已在跑时经本地混合端口重试。
红线：本模块不内置任何订阅源，订阅 URL 只能来自用户设置。
"""
from __future__ import annotations

import base64
import io
import logging
import re
import shutil
import subprocess
import urllib.parse
import zipfile
from collections.abc import Sequence
from pathlib import Path

import httpx

from app.core.app_info import APP_SLUG
from app.core.config import get_settings
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


def _copy_bundled(kernel_path: Path, names: Sequence[str]) -> dict:
    """把随包目录里的 `names` 补齐进内核目录（只补缺失，同名一律不覆盖）。

    「不覆盖」是刻意的：用户可能手动换了更高版本的内核、或自己更新过 GeoIP
    数据，静默覆盖等于替用户做决定。返回 {"copied": [...], "missing": [...]}。
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
    return _copy_bundled(kernel_path, (kernel_filename(), *_GEO_FILES))


def ensure_kernel(data_dir: Path) -> dict:
    """随包内核就位（data_dir 版本）：把内核与 GeoIP 数据补进 data/clash/。

    随包资产落到用户数据目录的唯一入口。返回
    {"kernelReady": bool, "copied": [...], "missing": [...], "bundleDir": str}；
    发行包缺内核（源码 clone 未补资产）时 kernelReady=False，由调用方决定
    是回退网络下载还是提示用户。
    """
    result = _copy_bundled(kernel_dir(data_dir), (kernel_filename(), *_GEO_FILES))
    ready = kernel_exe(data_dir).is_file()
    if result["copied"]:
        logger.info("随包内核资产已就位：%s", ", ".join(result["copied"]))
    if not ready:
        logger.warning(
            "随包内核不可用（%s 内无 %s），将回退网络下载",
            bundled_clash_dir(), kernel_filename(),
        )
    elif result["missing"]:
        logger.info(
            "内核目录缺少 %s（内核启动时会尝试自行下载，直连网络下可能卡住）",
            ", ".join(result["missing"]),
        )
    return {
        "kernelReady": ready,
        "copied": result["copied"],
        "missing": result["missing"],
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


# 机场面板普遍校验 UA：httpx 默认 UA 会被 403，伪装 Clash 客户端（内核同款标识）
_SUB_HEADERS = {"User-Agent": "mihomo/1.19.20"}


def _port_reachable(port: int, host: str = "127.0.0.1", timeout: float = 0.3) -> bool:
    """本地端口探活（订阅/内核下载借道本地混合端口的判据）。"""
    import socket

    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _download_attempts(
    runtime_port: int | None = None, proxy_url: str | None = None
) -> list[tuple[str | None, str]]:
    """下载通道链：直连 → 本内核代理 → 本地常见混合端口 → 指定代理。

    订阅/内核下载全是"鸡生蛋"场景：首次添加订阅时本内核必然没跑
    （无 config.yaml 起不来），面板域名又多被墙——直连是唯一通道时
    必挂。用户桌面常有 Clash Verge 等在跑（混合端口 7890/7897），
    试绑探测可用即借道，不依赖系统代理开关（httpx 不读 Windows
    注册表代理）。
    """
    attempts: list[tuple[str | None, str]] = [(None, "直连")]
    if runtime_port:
        attempts.append((f"http://127.0.0.1:{runtime_port}", "经内核代理"))
    for port in (7890, 7897):
        if port == runtime_port:
            continue
        if _port_reachable(port):
            attempts.append((f"http://127.0.0.1:{port}", f"本地混合端口 {port}"))
    if proxy_url and proxy_url not in [a[0] for a in attempts]:
        attempts.append((proxy_url, "指定代理"))
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
    logger.warning("随包内核不可用，回退网络下载（%s）", MIHOMO_VERSION)
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
        follow_redirects=True,
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
                    logger.info("下载 Clash 内核（%s / %s）：%s", mirror or "github", label, url)
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
                    logger.warning("镜像下载失败（%s / %s）：%s", mirror or "github", label, e)
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
                resp = httpx.get(url, timeout=120, follow_redirects=True)
                resp.raise_for_status()
                target.write_bytes(gzip.decompress(resp.content))
                target.chmod(0o755)
                return {"ok": True, "path": str(target), "source": mirror or "github"}
            except Exception as e:  # noqa: BLE001
                logger.warning("镜像下载失败：%s", e)
        return {"ok": False, "error": "所有下载源均失败"}
    finally:
        _KERNEL_PROGRESS["running"] = False


def kernel_version(exe_path: Path) -> str | None:
    try:
        result = subprocess.run(
            [str(exe_path), "-v"], capture_output=True, text=True, timeout=10
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


def ensure_controller(config_text: str) -> tuple[str, str, str]:
    """确保订阅 yaml 开启 external-controller（RESTful API），返回 (text, base_url, secret)。

    已有配置则沿用（解析 host:port 与 secret）；没有则注入本地默认值。
    """
    match = re.search(r"^external-controller\s*:\s*['\"]?([^'\"\n#]+)", config_text, re.MULTILINE)
    secret_match = re.search(r"^secret\s*:\s*['\"]?([^'\"\n#]+)", config_text, re.MULTILINE)
    if match:
        addr = match.group(1).strip()
        if not addr.startswith("127.0.0.1") and not addr.startswith("0.0.0.0"):
            # 控制器不在本机的情况少见；统一按 host:port 拼 URL
            pass
        secret = secret_match.group(1).strip() if secret_match else ""
        return config_text, f"http://{addr}", secret

    injection = (
        f"\nexternal-controller: 127.0.0.1:{CONTROLLER_PORT}\n"
        f"secret: {CONTROLLER_SECRET}\n"
    )
    return config_text.rstrip() + "\n" + injection, f"http://127.0.0.1:{CONTROLLER_PORT}", CONTROLLER_SECRET


def config_unchanged(startup_text: str | None, disk_text: str) -> bool:
    """内核启动文本与磁盘配置是否等价（重启开关的判据）。

    config.yaml 是「下载到的原始订阅文本 + 启动时注入的 external-controller」，
    而重拉下载写回磁盘的是**原始文本**（没有注入段）——直接字符串比较会把
    「机场内容其实没变」判成变了，每次重拉都白重启一次内核（在跑的连接全断）。
    这里先补注入再比，语义回到「内容真的变了才重启」。
    """
    if startup_text == disk_text:
        return True
    normalized, _url, _secret = ensure_controller(disk_text)
    return startup_text == normalized


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


class ClashRuntime:
    """本地内核进程的启动 / 停止 / 状态。进程内单例。"""

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.port: int | None = None
        self.config_path: str | None = None
        self.controller_url: str | None = None
        self.secret: str = ""
        self._startup_text: str | None = None  # 启动时的配置文本（重启判定用）

    def status(self) -> dict:
        running = self.process is not None and self.process.poll() is None
        return {
            "running": running,
            "port": self.port,
            "configPath": self.config_path,
            "controllerUrl": self.controller_url if running else None,
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
            logger.info("已清理 %d 个残留 Clash 内核进程", killed)
        return killed

    def start(self, exe_path: str, config_path: str, *, restart_if_changed: bool = False) -> dict:
        """启动内核。restart_if_changed：已在跑但配置文件内容较启动时
        有变化 → 重启生效（免重启刷新订阅节点用）；内容没变沿用实例。"""
        if self.status()["running"]:
            if not restart_if_changed:
                return self.status()
            disk_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
            if self.config_path == config_path and config_unchanged(
                self._startup_text, disk_text
            ):
                return self.status()
            self.stop()
        try:
            self._kill_orphans(exe_path, config_path)
        except Exception as e:  # noqa: BLE001 —— 清理失败不阻断启动（psutil 缺失等）
            logger.warning("残留内核清理跳过：%s", e)
        try:
            ensure_kernel_files(Path(config_path).parent)
        except Exception as e:  # noqa: BLE001
            logger.warning("随包内核资产补齐失败：%s", e)
        config_text = Path(config_path).read_text(encoding="utf-8", errors="ignore")
        self.port = parse_mixed_port(config_text)
        # 注入/解析 external-controller，供节点检测（出口 IP / 存活）使用
        text, controller_url, secret = ensure_controller(config_text)
        if text != config_text:
            Path(config_path).write_text(text, encoding="utf-8")
        self.controller_url = controller_url
        self.secret = secret
        self.config_path = config_path
        self._startup_text = text  # 注入 controller 后的最终文本（与磁盘一致）
        self.process = subprocess.Popen(
            [exe_path, "-f", config_path, "-d", str(Path(config_path).parent)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Clash 内核已启动 pid=%s port=%d controller=%s", self.process.pid, self.port, controller_url)
        return self.status()

    def running_subscription_is(self, sub_url: str) -> bool:
        """内核当前运行的配置是否来自该订阅 URL。

        订阅刷新的重启开关用：config.yaml 是共用缓存，重下后文件内容
        已变——只有「内核启动文本里含该 URL」才是真正在跑这条订阅，
        此时重启才不会把内核悄悄切到另一条订阅上。
        """
        return bool(
            self.status()["running"]
            and self._startup_text
            and (sub_url or "").strip()
            and (sub_url or "").strip() in self._startup_text
        )

    def stop(self) -> dict:
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            logger.info("Clash 内核已停止")
        self.process = None
        self.controller_url = None
        self._startup_text = None
        return self.status()

    def _download_attempts(
        self, proxy_url: str | None = None
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
                    headers=_SUB_HEADERS,
                ) as client:
                    resp = await client.get(sub_url)
                    resp.raise_for_status()
                    return {"userinfo": resp.headers.get("subscription-userinfo")}
            except Exception as e:  # noqa: BLE001 —— 逐通道降级
                logger.warning("订阅头拉取失败（%s）：%s", label, e)
        return None

    async def download_subscription(self, sub_url: str, data_dir: Path, proxy_url: str | None = None) -> dict:
        """下载用户订阅 yaml 到 data/clash/config.yaml。

        链式引导（对齐旧爬虫脚本）：直连失败且内核已在跑时，经本地混合
        端口重试一次；全部失败回退上次成功下载的本地缓存。订阅面板域名
        多数被墙，直连成功纯属侥幸。
        返回 {path, title, userinfo, nodes, cached}；title 取自 profile-title
        响应头（订阅名），userinfo 为 subscription-userinfo 流量头原文。
        """
        path = kernel_dir(data_dir) / "config.yaml"
        attempts = self._download_attempts(proxy_url)
        last_err: Exception | None = None
        for attempt_proxy, label in attempts:
            try:
                async with httpx.AsyncClient(
                    timeout=60, proxy=attempt_proxy, follow_redirects=True,
                    headers=_SUB_HEADERS,
                ) as client:
                    resp = await client.get(sub_url)
                    resp.raise_for_status()
                    text = resp.text
                if "proxies:" not in text and "proxy-providers:" not in text:
                    raise ValueError("订阅内容不是有效的 Clash 配置（缺少 proxies 段）")
                nodes = parse_node_names(text)
                if not nodes and "proxy-providers:" not in text:
                    raise ValueError("订阅内容解析到 0 个节点，疑似面板返回异常页")
                path.write_text(text, encoding="utf-8")
                # 自动取名链：profile-title → Content-Disposition → URL 末段
                title = subscription_auto_name(resp.headers, sub_url)
                userinfo = resp.headers.get("subscription-userinfo")
                logger.info(
                    "订阅下载成功（%s）：%d 节点，面板名=%s", label, len(nodes), title
                )
                return {
                    "path": str(path), "title": title, "userinfo": userinfo,
                    "nodes": len(nodes), "cached": False,
                }
            except Exception as e:  # noqa: BLE001 —— 逐通道降级
                last_err = e
                logger.warning("订阅下载失败（%s）：%s", label, e)
        if path.is_file():
            logger.warning("订阅全部通道失败，回退本地缓存 %s", path)
            cached_text = path.read_text(encoding="utf-8", errors="ignore")
            return {
                "path": str(path), "title": None, "userinfo": None,
                "nodes": len(parse_node_names(cached_text)), "cached": True,
            }
        raise last_err  # type: ignore[misc]


runtime = ClashRuntime()
