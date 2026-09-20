"""数据目录解析：程序目录（可整体替换）与用户数据目录（绝不随程序移动）的分离判定。

独立成模块（纯标准库，不引 pydantic）的原因：
- `app.core.config` 与 `desktop/main.py` 必须对「data/ 在哪」给出**同一个答案**——
  启动期换装要按同一路径找 `update-staging`，桌面壳又要早于服务进程知道自己
  的数据目录；
- 启动器在导入任何第三方库之前就可能要它，故依赖只允许 os / sys / pathlib。

判定顺序（先到先得）：
1. 环境变量 `<ENV_PREFIX>DATA_DIR` —— 显式覆盖（测试 / 多实例 / 自定义部署）
2. 程序目录内 `portable.flag` —— 便携模式显式声明（U 盘 / 绿色目录）
3. 程序目录内 `data/<slug>.db` 已存在 —— 程序目录内的既有库原地继续用
4. 未打包（开发态）—— 机器级 `<slug>-dev` 目录（**刻意与打包态数据物理隔离**）
5. 机器级目录 `%LOCALAPPDATA%` 下的 `<slug>` —— 打包态新装默认落点

第 5 条让数据不再与 exe 同生共死：「解压到新目录覆盖升级」「直接从压缩包里
双击运行（Windows 临时解包）」「整目录删除重装」都不会产生空白数据环境。

第 3 条排在第 4 条之前：程序目录内已有库时原地使用，第 4 条只在「程序目录内
没有库、即没有东西可丢」时生效。第 4 条的意义是让运行时数据不落在工作树内——
爬虫的库、日志、备份若写进工作树，一次 `git add -A` 就会把它们收进提交。
开发态落到机器级目录后，工作树里不再有任何运行时会写入的位置。

机器级目录同样不可写时（极端只读环境）回退程序目录旁 `data/`——数据位置
不如可用性重要，且该情况会在启动告警里被点名。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app.core.app_info import APP_SLUG, ENV_PREFIX

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # server/app/core/paths.py -> Holdexar/

# 便携模式显式标记（程序目录内放一个同名空文件即启用）
PORTABLE_FLAG = "portable.flag"


def is_frozen() -> bool:
    """是否运行在打包产物中（PyInstaller onedir）。"""
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """程序目录：打包态 = exe 所在目录；开发态 = 仓库根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return PROJECT_ROOT


def _candidates_for(name: str) -> list[Path]:
    """把 `<name>` 解析成各机器级根目录下的落点（按优先级）。

    LOCALAPPDATA 优先于 APPDATA：漫游目录会被域环境整份同步，几十 MB 的
    SQLite 库放进漫游盘不合适；APPDATA / 用户主目录只作兜底。
    主目录下的落点加 `.` 前缀，避免在家目录可见区散落文件夹。
    """
    out: list[Path] = []
    for env_key in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env_key)
        if base:
            out.append(Path(base) / name)
    home = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if home:
        out.append(Path(home) / f".{name}")
    return out


def machine_candidates(slug: str = APP_SLUG) -> list[Path]:
    """打包态数据目录候选。"""
    return _candidates_for(slug)


def dev_machine_candidates(slug: str = APP_SLUG) -> list[Path]:
    """开发态数据目录候选：`<root>/<slug>-dev`。

    **刻意不复用 machine_candidates**：开发态的迁移演练、爬虫基准、browse 测试
    都会往数据目录里写。与用户的正式库同目录，迟早互相污染——一次开发态的
    `PRAGMA user_version` 前向迁移就能把正式库改到不兼容的新 schema。
    """
    return _candidates_for(f"{slug}-dev")


def data_dir_filename(slug: str = APP_SLUG) -> str:
    return f"{slug}.db"


def _ensure(path: Path) -> Path:
    """确保目录及其 logs/ 子目录存在且可写；不可写时抛 OSError 由调用方换候选。

    可写性判定**不落任何临时文件**：能建出 logs/ 子目录即证明父目录可写
    （mkdir 需要父目录写权限），再用 os.access 兜住「目录已存在但只读」的
    情形。不用「建探针文件再删」——删除动作在受限环境（只读盘 / 删除守卫 /
    杀软）会失败，而判定本身不该依赖删除。
    """
    logs = path / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if not os.access(logs, os.W_OK):
        raise OSError(f"数据目录不可写：{path}")
    return path


def _first_writable(candidates: list[Path], fallback: Path) -> Path:
    """按顺序取第一个可写的候选目录；全不可写时退回 fallback。

    **不抛异常**：只读盘 / 受限沙箱下「起不来」比「退回旧布局」糟得多。
    """
    for candidate in candidates:
        try:
            return _ensure(candidate)
        except OSError:
            continue
    return _ensure(fallback)


def resolve_data_dir(slug: str = APP_SLUG, env_prefix: str = ENV_PREFIX) -> Path:
    """按判定顺序解析数据目录，返回已就绪（存在且可写）的绝对路径。"""
    explicit = os.environ.get(f"{env_prefix}DATA_DIR", "").strip()
    if explicit:
        return _ensure(Path(explicit).expanduser())

    base = app_dir()

    # 便携显式声明：程序目录内 data/ 即数据目录（随包搬走）
    if (base / PORTABLE_FLAG).is_file():
        return _ensure(base / "data")

    # 存量布局：已有库就原地继续用，绝不擅自搬家（升级不丢数据的根因防线）
    if (base / "data" / data_dir_filename(slug)).is_file():
        return _ensure(base / "data")

    # 开发态：机器级 dev 目录，与打包态数据物理隔离，工作树内不写任何运行时数据
    if not is_frozen():
        return _first_writable(dev_machine_candidates(slug), base / "data")

    # 打包态新装：机器级数据目录
    return _first_writable(machine_candidates(slug), base / "data")


def describe_layout(data_dir: Path, slug: str = APP_SLUG) -> str:
    """给已解析的数据目录打布局标签（启动日志用，便于排查「数据跑哪去了」）。

    只做归类不做决策：决策唯一入口是 resolve_data_dir，避免两套判定漂移。
    """
    path = Path(data_dir)
    if path in machine_candidates(slug):
        return "machine"
    if path in dev_machine_candidates(slug):
        return "dev-machine"
    if path == app_dir() / "data":
        # 便携 / 存量两支必须**先于** is_frozen() 判定：它们与「是否打包」无关，
        # 放在其后会让未打包时这两支永远不可达、统统被标成 dev。
        if (app_dir() / PORTABLE_FLAG).is_file():
            return "portable"
        if (app_dir() / "data" / data_dir_filename(slug)).is_file():
            return "legacy"
        return "appdir" if is_frozen() else "dev"
    return "custom"


LAYOUT_LABEL = {
    "machine": "系统数据目录（与程序目录解耦，升级/重装/换目录均不受影响）",
    "dev-machine": "开发态机器级目录（与打包态数据物理隔离，推荐）",
    "portable": "便携模式（程序目录 data/，随包整体迁移）",
    "legacy": "程序目录 data/ 内有库（原地使用；升级需整目录保留）",
    "appdir": "程序目录 data/（系统数据目录不可写时的兜底）",
    "dev": "程序目录 data/（开发态兜底：机器级目录全不可写时才会走到，不推荐）",
    "custom": "自定义数据目录（环境变量指定）",
}


def is_inside(path: Path | str, parent: Path | str) -> bool:
    """`path` 是否位于 `parent` 之内（相等不算）。resolve 后再比，规避 `..` 与符号链接。"""
    try:
        inner = Path(path).resolve()
        outer = Path(parent).resolve()
    except OSError:
        return False
    return inner != outer and outer in inner.parents


def layout_warning(data_dir: Path | str, slug: str = APP_SLUG) -> str:
    """数据目录落在仓库工作树内时的告警文案；空串 = 无需告警。

    为什么每次启动都告警而不是只报一次：这是一项**尚未修复的错误配置**，
    不是一次性事件。而「只报一次」需要往那个（本就不该存在的）目录里写标记
    文件——往问题位置写文件，正是问题本身。

    两种情形刻意不告警：
    - 显式设了 `<ENV_PREFIX>DATA_DIR`：用户已经明确指定位置，尊重其选择；
    - 打包态：把发布包解压进某个 git 检出是常规做法，那不是开发态污染。
    """
    if is_frozen():
        return ""
    if os.environ.get(f"{ENV_PREFIX}DATA_DIR", "").strip():
        return ""
    if not is_inside(data_dir, PROJECT_ROOT):
        return ""
    return (
        f"[数据目录] 当前数据目录在仓库工作树内：{data_dir}\n"
        f"           爬虫会持续往这里写库、日志与备份，一次 `git add -A` 就会把它们收进提交。\n"
        f"           请用 dev.bat 启动开发态（数据落在机器级 {slug}-dev 目录），\n"
        f"           或显式设置 {ENV_PREFIX}DATA_DIR 指向仓库之外的位置。"
    )


__all__ = [
    "APP_SLUG",
    "ENV_PREFIX",
    "LAYOUT_LABEL",
    "PORTABLE_FLAG",
    "PROJECT_ROOT",
    "app_dir",
    "data_dir_filename",
    "describe_layout",
    "dev_machine_candidates",
    "is_frozen",
    "is_inside",
    "layout_warning",
    "machine_candidates",
    "resolve_data_dir",
]
