"""数据目录解析：程序目录（可整体替换）与用户数据目录（绝不随程序移动）分离。
纯标准库（os/sys/pathlib）：config 与桌面壳必须对「data/ 在哪」同答案，且启动器
在导入任何第三方库之前就可能要它。判定顺序（先到先得）：①环境变量 DATA_DIR
显式覆盖 ②程序目录 portable.flag 便携声明 ③程序目录内已有 data/<slug>.db
原地续用 ④未打包开发态读工作树指针文件里写明的数据目录 ⑤开发态机器级
<slug>-dev（与打包态物理隔离，运行时写入不落工作树）⑥打包态机器级已有库
原地续用 ⑦打包态新装落程序目录旁 data/（与解压位置同盘），该目录不可写才回落
机器级 <slug>。机器级目录不可写的极端环境同样回退程序目录旁 data/，启动告警
点名。指针目标不可写时同样落回 ⑤，不因一处配置让程序起不来。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from app.core.app_info import APP_SLUG, ENV_PREFIX

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # server/app/core/paths.py -> Holdexar/

# 便携模式显式标记（程序目录内放一个同名空文件即启用）
PORTABLE_FLAG = "portable.flag"

# 开发态数据目录指针（工作树内的文本文件，内容一行绝对路径即生效）
DEV_POINTER = f"{APP_SLUG}-dev-home.txt"


def read_dev_pointer(base: Path | None = None) -> Path | None:
    """取指针文件里写明的开发态数据目录；缺失、空行、注释行、非绝对路径都算未配置。"""
    path = (base or app_dir()) / DEV_POINTER
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        entry = line.strip()
        if not entry or entry.startswith("#"):
            continue
        candidate = Path(entry)
        return candidate if candidate.is_absolute() else None
    return None


def is_frozen() -> bool:
    """是否运行在打包产物中（PyInstaller onedir）。"""
    return bool(getattr(sys, "frozen", False))


def install_root() -> Path:
    """安装根目录：所有子模块的标准基准。"""
    override = os.environ.get(f"{ENV_PREFIX}INSTALL_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if is_frozen():
        exe = Path(sys.executable).resolve()
        # 1. 运行在 versions/<version>/Holdexar.exe
        if exe.parent.parent.name == "versions":
            return exe.parent.parent.parent
        # 2. 运行在 updater/UpdateHost.exe
        if exe.parent.name == "updater":
            return exe.parent.parent
        # 3. 运行在根目录 Holdexar.exe (Launcher 或旧式扁平)
        return exe.parent
    return PROJECT_ROOT


def staging_dir() -> Path:
    return install_root() / "update-staging"


def logs_dir() -> Path:
    return install_root() / "logs"


def app_dir() -> Path:
    """程序安装根目录（兼容旧调用）。"""
    return install_root()


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
        return _ensure(Path(explicit).expanduser().resolve())

    root = install_root()

    # 开发态：指针文件或机器级 dev 目录，避免运行时写污染工作树
    if not is_frozen():
        if (root / PORTABLE_FLAG).is_file():
            return _ensure(root / "data")
        if (root / "data" / data_dir_filename(slug)).is_file():
            return _ensure(root / "data")
        pointed = read_dev_pointer(root)
        if pointed is not None:
            try:
                return _ensure(pointed)
            except OSError:
                pass
        return _first_writable(dev_machine_candidates(slug), root / "data")

    # 打包态（正式运行）：永远落安装根目录 data/
    target = root / "data"
    try:
        _ensure(target)
    except OSError as e:
        raise PermissionError(f"安装目录数据文件夹不可写，请更换安装位置或检查权限：{target}") from e

    # 存量/新装判定与平滑迁移：
    # 检查是否有旧版本 AppData 数据与冲突，自动完成仲裁、备份与收拢
    db_file = target / data_dir_filename(slug)
    from app.core.migration import (
        detect_legacy_data_locations,
        migrate_appdata_to_portable,
        resolve_data_dir_conflict,
    )

    try:
        locs = detect_legacy_data_locations(root, slug)
        old_dir = locs.get("appdataDataDir")
        if locs.get("hasConflict") and old_dir:
            res = resolve_data_dir_conflict(target, old_dir, slug)
            if not res.get("completed"):
                raise PermissionError(
                    f"检测到数据库损坏且自动恢复失败，已停止启动以保护数据：{target} 与 {old_dir}"
                )
        elif locs.get("hasAppdataDb") and old_dir and not db_file.is_file():
            result = migrate_appdata_to_portable(old_dir, target, slug)
            if not result.get("completed"):
                raise PermissionError(
                    f"旧数据迁移未完成，已停止启动以免生成空库。旧库位置：{old_dir}"
                )
    except PermissionError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise PermissionError(
            f"旧数据迁移失败，已停止启动以免生成空库：{exc}"
        ) from exc

    return target


def describe_layout(data_dir: Path, slug: str = APP_SLUG) -> str:
    """给已解析的数据目录打布局标签（启动日志用，便于排查「数据跑哪去了」）。

    只做归类不做决策：决策唯一入口是 resolve_data_dir，避免两套判定漂移。
    """
    path = Path(data_dir).resolve()
    root = install_root().resolve()
    if any(path == cand.resolve() for cand in machine_candidates(slug)):
        return "machine"
    if any(path == cand.resolve() for cand in dev_machine_candidates(slug)):
        return "dev-machine"
    if path == (root / "data").resolve():
        if (root / PORTABLE_FLAG).is_file():
            return "portable"
        if (root / "data" / data_dir_filename(slug)).is_file():
            return "legacy"
        return "appdir" if is_frozen() else "dev"
    if not is_frozen():
        pointed = read_dev_pointer(root)
        if pointed is not None and path == pointed.resolve():
            return "dev-pointer"
    return "custom"


LAYOUT_LABEL = {
    "machine": "系统数据目录（与程序目录解耦，升级/重装/换目录均不受影响）",
    "dev-machine": "开发态机器级目录（与打包态数据物理隔离，推荐）",
    "dev-pointer": "开发态自定义目录（工作树内指针文件指定，运行时写入不落仓库）",
    "portable": "便携模式（程序目录 data/，随包整体迁移）",
    "legacy": "程序目录 data/ 内有库（原地使用；升级需整目录保留）",
    "appdir": "程序目录 data/（与程序同盘，随解压位置走；系统数据目录不可写时也是兜底）",
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

    告警每次启动都发：错误配置是持续状态而非一次性事件；「只报一次」需要往
    那个（本就不该存在的）目录里写标记文件——往问题位置写文件正是问题本身。

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
    "DEV_POINTER",
    "ENV_PREFIX",
    "LAYOUT_LABEL",
    "PORTABLE_FLAG",
    "PROJECT_ROOT",
    "app_dir",
    "data_dir_filename",
    "describe_layout",
    "dev_machine_candidates",
    "install_root",
    "is_frozen",
    "is_inside",
    "layout_warning",
    "logs_dir",
    "machine_candidates",
    "read_dev_pointer",
    "resolve_data_dir",
    "staging_dir",
    "state_dir",
    "updater_dir",
    "versions_dir",
]
