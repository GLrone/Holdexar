"""Holdexar 一键启动脚本。

流程：选解释器（缺 venv 自动创建）→ 补后端依赖 → 补随包资产
（汇率档案种子 / Clash 内核与 GeoIP 数据，缺失则从上游取）→ 补前端依赖
（node_modules 缺失自动 npm install）→ 校验前端产物（缺失则构建）
→ 拉起桌面壳。

用法：
    python run.py                 # 桌面窗口模式（默认）
    python run.py --server        # 只起本地服务，不开窗口
    python run.py --dev           # 对接 Vite 开发服务器（需另开终端 npm run dev）
    python run.py --build         # 强制重建前端后启动
    python run.py --port 18888    # 指定端口
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SERVER_ROOT = ROOT / "server"
VENV_PY = SERVER_ROOT / ".venv" / "Scripts" / "python.exe"
WEB_ROOT = ROOT / "web"
DIST_INDEX = WEB_ROOT / "dist" / "index.html"
NODE_MODULES = WEB_ROOT / "node_modules"

# 品牌常量与后端共用同一来源（app_info.py 无第三方依赖，可直接导入）
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
from app.core.app_info import APP_NAME, ENV_PREFIX  # noqa: E402

DEFAULT_PORT = "28765"
# 覆盖运行链关键面：Web 框架 / 数据层 / 调度 / 桌面壳
REQUIRED_MODULES = ("fastapi", "uvicorn", "sqlalchemy", "aiohttp", "apscheduler", "webview")
# 随包内核资产就位的判据（assets/clash/ 内的可执行文件，见 scripts/fetch_kernel.py）
KERNEL_EXE = ROOT / "assets" / "clash" / ("mihomo.exe" if os.name == "nt" else "mihomo")


def _run(cmd: list[str], **kw) -> int:
    """subprocess.run 的 shell=False 直通（Windows 下 npm 需要 shell=True）。"""
    return subprocess.run(cmd, **kw).returncode


def ensure_venv() -> Path:
    """server/.venv 存在则用之；缺失则自动创建（新机 BAT 双击无人工介入，
    避免依赖装进系统 Python）。"""
    if VENV_PY.is_file():
        return VENV_PY
    print("[环境] 未找到 server/.venv，自动创建虚拟环境 ...")
    rc = _run([sys.executable, "-m", "venv", str(SERVER_ROOT / ".venv")])
    if rc == 0 and VENV_PY.is_file():
        print("[环境] 虚拟环境已就绪")
        return VENV_PY
    # venv 建失败（如精简发行版缺 ensurepip）：退回当前解释器，依赖照装
    print("[环境] venv 创建失败，回退当前解释器（依赖将装入该环境）")
    return Path(sys.executable)


def ensure_deps(python: Path) -> None:
    """逐个 import 探测，缺任何关键包则整份安装 server/requirements.txt。"""
    missing = []
    for name in REQUIRED_MODULES:
        rc = _run([str(python), "-c", f"import {name}"], capture_output=True)
        if rc != 0:
            missing.append(name)
    if not missing:
        print("[依赖] 后端依赖完整")
        return
    print(f"[依赖] 缺少 {', '.join(missing)}，安装 server/requirements.txt ...")
    # PIP_UTF8=1：requirements 无 BOM 时 pip 按 OS locale（zh-CN 为 cp936）
    # 解码，非 ASCII 字节直接 UnicodeDecodeError——强制 UTF-8 与文件实际编码对齐
    install_env = os.environ.copy()
    install_env["PIP_UTF8"] = "1"
    rc = _run(
        [str(python), "-m", "pip", "install", "-r", str(SERVER_ROOT / "requirements.txt")],
        env=install_env,
    )
    if rc != 0:
        sys.exit("[错误] 后端依赖安装失败，请检查网络/镜像源后重试。")


def ensure_npm() -> None:
    """npm 不在 PATH：给出带下载指引的明确报错（BAT 场景无终端回滚提示）。"""
    if shutil.which("npm") is not None:
        return
    sys.exit(
        "[错误] 未找到 npm。\n"
        "       前端构建依赖 Node.js（含 npm），请到 https://nodejs.org 下载安装\n"
        "       （LTS 版即可），装完重开本脚本。"
    )


def ensure_seed(python: Path) -> None:
    """资产种子（16 年汇率档案）：缺失则从 Release 资产补一份（不进 git，
    见 scripts/fetch_seed.py）。失败只警告不阻断——没有种子只是汇率页
    没有历史档案。"""
    seed = ROOT / "assets" / "seed" / "holdexar_seed.db"
    if seed.is_file():
        return
    print("[数据] 未找到汇率档案种子，尝试从 Release 资产获取（约 12MB）…")
    rc = _run([str(python), str(ROOT / "scripts" / "fetch_seed.py")])
    if rc != 0:
        print("[警告] 汇率档案种子未获取到：汇率历史页将为空，其余功能不受影响。")


def ensure_kernel(python: Path) -> None:
    """随包内核资产（mihomo + GeoIP 数据）：缺失则从上游补一份（不进 git）。
    失败只警告不阻断——代理页可一键安装。"""
    if KERNEL_EXE.is_file():
        return
    print("[内核] 未找到随包 Clash 内核，尝试获取（mihomo + GeoIP 数据，约 80MB）…")
    rc = _run([str(python), str(ROOT / "scripts" / "fetch_kernel.py")])
    if rc != 0:
        print("[警告] 内核资产未获取到：代理页将显示「内核缺失」，可页内一键安装。")


def ensure_web_deps() -> None:
    """node_modules 缺失时自动 npm install（新机 clone 后直接双击的场景）。"""
    if NODE_MODULES.is_dir():
        return
    print("[前端] 未找到 node_modules，安装依赖（首次较慢，约 1-2 分钟）...")
    rc = _run(["npm", "install"], cwd=str(WEB_ROOT), shell=True)
    if rc != 0:
        sys.exit("[错误] npm install 失败，请检查网络/代理后重试。")


def build_web() -> None:
    """重建前端产物。先手动删 dist，避免 vite 清空目录被系统回收站钩子拦截。"""
    ensure_npm()
    if DIST_INDEX.parent.is_dir():
        shutil.rmtree(DIST_INDEX.parent)
    print("[前端] 构建 web/dist ...")
    rc = _run(["npm", "run", "build"], cwd=str(WEB_ROOT), shell=True)
    if rc != 0:
        sys.exit("[错误] 前端构建失败（lint 门禁或编译错误），请向上翻日志。")


def main() -> None:
    parser = argparse.ArgumentParser(description=f"{APP_NAME} 一键启动")
    parser.add_argument("--server", action="store_true", help="无窗口模式，仅启动本地服务")
    parser.add_argument("--dev", action="store_true", help="加载 Vite 开发服务器 http://localhost:8080")
    parser.add_argument("--build", action="store_true", help="强制重建前端产物")
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"服务端口（默认 {DEFAULT_PORT}）")
    args = parser.parse_args()

    python = ensure_venv()
    ensure_deps(python)
    ensure_seed(python)
    ensure_kernel(python)
    if args.build or not DIST_INDEX.is_file():
        ensure_web_deps()
        build_web()

    env = os.environ.copy()
    env[f"{ENV_PREFIX}PORT"] = str(args.port)
    if args.dev:
        env[f"{ENV_PREFIX}DEV_URL"] = "http://localhost:8080"
        print(f"[开发] 窗口将加载 {env[f'{ENV_PREFIX}DEV_URL']}（需确保 npm run dev 已运行）")

    print(f"[启动] {APP_NAME} · http://127.0.0.1:{args.port} · Ctrl+C 退出")
    # 开发态防陈旧字节码：__pycache__ 缓存未失效时会加载到旧字节码导致启动即崩，
    # 每次启动清掉这批 .pyc（模块很小，重编只是几毫秒）。打包态走 exe 自包含、
    # 不经由本路径，不受影响。
    import glob as _glob

    for _p in _glob.glob(str(ROOT / "desktop" / "__pycache__" / "main*.pyc")):
        try:
            os.remove(_p)
        except OSError:
            pass
    cmd = [str(python), str(ROOT / "desktop" / "main.py")]
    if args.server:
        cmd.append("--server")
    # LAUNCHER_PID：桌面壳据此监视本进程——包装进程被硬杀时桌面壳随之退出，
    # 不留孤儿实例占用服务端口与单实例锁。
    env[f"{ENV_PREFIX}LAUNCHER_PID"] = str(os.getpid())
    try:
        subprocess.run(cmd, cwd=str(ROOT), env=env, check=True)
    except KeyboardInterrupt:
        print(f"\n[停止] {APP_NAME} 已退出")
    except subprocess.CalledProcessError as e:
        # 桌面壳异常退出（窗口层初始化失败等）：给可行动的指引而非裸 traceback
        print(f"\n[错误] 启动器异常退出（exit {e.returncode}）。")
        print("       可改用 python run.py --server 以无窗口模式启动本地服务排查。")
        sys.exit(e.returncode or 1)


if __name__ == "__main__":
    main()
