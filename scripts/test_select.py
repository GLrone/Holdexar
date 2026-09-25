#!/usr/bin/env python3
"""影响面选测：按代码导入关系决定该跑哪些测试，不按业务印象。

用法：
  python scripts/test_select.py                 # 按 git 工作树当前改动自动选测
  python scripts/test_select.py <路径>...       # 显式指定改动文件
  python scripts/test_select.py --smoke [--run] # 核心链路冒烟精选集（可加 --why 看依据）
  python scripts/test_select.py --run           # 选完直接执行 pytest

选测范围 = 改动模块 + 在代码上导入它的模块一路向上（服务依赖链）。
数据产物关联不算依赖：A 的输出落库后被 B 读取，不构成 B 的测试义务。
影响面无界的文件（conftest / 全局配置 / 依赖清单）不做局部选测，直接判全量。
"""
from __future__ import annotations

import argparse
import ast
import functools
import subprocess
import sys
from pathlib import Path

# 子进程直接写继承的 stdout，父进程的选测报告须即时冲刷，否则管道下顺序颠倒
print = functools.partial(print, flush=True)

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "server" / "app"
TESTS_DIR = ROOT / "server" / "tests"

# 这些文件被全部模块依赖：改动即影响面无界，局部选测是假安全
FULL_BLAST = {
    "server/tests/conftest.py",
    "server/app/core/config.py",
    "server/app/core/database.py",
    "server/app/main.py",
    "server/requirements.txt",
    "server/requirements-dev.txt",
    "pyproject.toml",
}

# 无导入关系的工具脚本 → 手工映射到其行为测试
SCRIPT_TESTS = {
    "scripts/check_secrets.py": ["server/tests/test_check_secrets.py"],
    "scripts/check_dev_traces.py": ["server/tests/test_dev_traces.py"],
}

# 核心生命线精选集：建库 → 启动 → 出口 → 任务 → 落价 → 展示
SMOKE = [
    ("server/tests/test_schema_migrations.py", "建库与迁移链（库能建、老库能升级）"),
    ("server/tests/test_data_dir.py", "数据目录判定（升级不丢数据）"),
    ("server/tests/test_startup_chain.py", "启动链顺序（先开门再收拾）"),
    ("server/tests/test_proxypool_bootstrap_wiring.py", "代理池启动接线（出口建得起来）"),
    ("server/tests/test_crawler_scheduler.py", "抓取调度（任务能被消费收尾）"),
    ("server/tests/test_crawl_backfill.py", "任务生成与回补（该抓的都有任务）"),
    ("server/tests/test_monitoring_lifecycle.py", "监控生命周期（添加/关注语义）"),
    ("server/tests/test_game_price_data.py", "价格数据状态（价格落库且可判新旧）"),
    ("server/tests/test_games_query.py", "列表查询（页面有数据可显示）"),
]


def _module_of(path: Path) -> str | None:
    """文件路径 → 模块点路径；__init__.py 归并为包本身。"""
    rel = path.resolve().relative_to(ROOT).as_posix()
    parts = rel[:-3].split("/") if rel.endswith(".py") else rel.split("/")
    if parts[0] == "server" and parts[1] == "app":
        parts = parts[1:]
    elif parts[:2] == ["server", "tests"]:
        parts = ["tests"] + parts[2:]
    else:
        return None
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _structural_imports(tree: ast.AST):
    """只收集模块顶层的 import（结构性依赖）。

    函数/类体内的延迟导入（建表注册、迁移种子、避循环的局部 import）
    不入图——它们是运行期接线，不是模块间的结构依赖；否则 core 层对
    domains 的种子导入会把全图挂到任何改动的下游。
    """
    def visit(node):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            yield node
            return
        for child in ast.iter_child_nodes(node):
            yield from visit(child)

    yield from visit(tree)


def _scan_imports(path: Path) -> set[str]:
    """一个 .py 文件顶层导入的全部本仓模块（绝对 + 相对，解析到现有模块为止）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return set()
    own = _module_of(path)
    if own is None:
        return set()
    pkg_parts = own.split(".")
    found: set[str] = set()

    def _add(target: str) -> None:
        found.add(target)

    for node in _structural_imports(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base: list[str] = node.module.split(".") if node.module else []
            else:
                up = node.level - 1
                anchor = pkg_parts[:-1] if len(pkg_parts) > 1 else pkg_parts
                base = anchor[: len(anchor) - up] if up else list(anchor)
                if node.module:
                    base = base + node.module.split(".")
            if not base:
                continue
            # 只记精确目标：包本身（__init__ 会执行）+ 点名的子模块；
            # 不记更高层祖先——否则一改 core 下任何文件就全图爆炸
            _add(".".join(base))
            for alias in node.names:
                if alias.name != "*":
                    _add(".".join(base + [alias.name]))
    return found


def _collect_modules() -> tuple[dict[str, set[str]], dict[str, set[str]], dict[str, set[str]]]:
    """返回（app 模块导入表, 反向导入表, 测试文件导入表）。"""
    app_imports: dict[str, set[str]] = {}
    test_imports: dict[str, set[str]] = {}
    for base in (APP_DIR, TESTS_DIR):
        for py in base.rglob("*.py"):
            if "__pycache__" in py.parts or py.name == "conftest.py":
                continue
            module = _module_of(py)
            if module is None:
                continue
            imports = _scan_imports(py)
            if py.is_relative_to(TESTS_DIR):
                test_imports[py.resolve().relative_to(ROOT).as_posix()] = imports
            else:
                app_imports[module] = imports
    universe = set(app_imports)
    for module, imports in app_imports.items():
        app_imports[module] = {m for m in imports if m in universe}
    reverse: dict[str, set[str]] = {}
    for importer, targets in app_imports.items():
        for target in targets:
            reverse.setdefault(target, set()).add(importer)
    return app_imports, reverse, test_imports


def _changed_paths(args: list[str]) -> list[Path]:
    if args:
        return [Path(p) for p in args]
    out = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
    )
    paths: list[Path] = []
    for line in out.stdout.splitlines():
        if len(line) < 4:
            continue
        name = line[3:].strip()
        if " -> " in name:
            name = name.split(" -> ", 1)[1]
        paths.append(Path(name))
    return paths


def _rel(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _affected_modules(changed: list[Path], reverse: dict[str, set[str]]) -> set[str]:
    seeds: list[str] = []
    for path in changed:
        module = _module_of(path)
        if module:
            # 只放被改模块本身；__init__.py 已由 _module_of 归并为包名，
            # 改包入口时种子即包，不影响普通模块改动
            seeds.append(module)
        else:
            seeds.append(_rel(path))
    affected: set[str] = set()
    queue = list(dict.fromkeys(seeds))
    while queue:
        current = queue.pop()
        if current in affected:
            continue
        affected.add(current)
        queue.extend(reverse.get(current, ()))
    return affected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("paths", nargs="*", help="改动文件（缺省取 git 工作树改动）")
    parser.add_argument("--smoke", action="store_true", help="核心链路冒烟精选集")
    parser.add_argument("--run", action="store_true", help="选完直接执行 pytest")
    parser.add_argument("--why", action="store_true", help="输出每个测试文件的命中依据")
    opts = parser.parse_args()

    runner = sys.executable
    venv = ROOT / "server" / ".venv" / "Scripts" / "python.exe"
    if venv.exists():
        runner = str(venv)

    if opts.smoke:
        print("[冒烟] 核心生命线精选集（建库 → 启动 → 出口 → 任务 → 落价 → 展示）：")
        for path, purpose in SMOKE:
            print(f"  {path}  # {purpose}")
        if not opts.run:
            print("执行：python scripts/test_select.py --smoke --run")
            return 0
        files = [path for path, _ in SMOKE]
    else:
        changed = _changed_paths(opts.paths)
        if not changed:
            print("[选测] 工作树无改动。")
            return 0
        rel_changed = [_rel(p) for p in changed]
        web_only = [p for p in rel_changed if p.startswith(("web/", "docs/"))]
        server_changed = [p for p in rel_changed if p.startswith("server/") or p in SCRIPT_TESTS]
        hit_full = [p for p in rel_changed if p in FULL_BLAST]
        if hit_full:
            print(f"[全量] 影响面无界（命中地基文件）：{', '.join(hit_full)}")
            print("执行：python -m pytest -q")
            return 0
        if not server_changed:
            print(f"[选测] 改动均不涉后端（{len(web_only)} 个前端/文档文件 → 走构建门禁），无后端测试可跑。")
            return 0
        _, reverse, test_imports = _collect_modules()
        affected = _affected_modules([Path(p) for p in server_changed], reverse)

        selected: dict[str, list[str]] = {}
        for test_path, imports in sorted(test_imports.items()):
            if test_path in server_changed:
                selected[test_path] = ["改动文件本身"]
                continue
            hits = sorted(imports & affected)
            if hits:
                selected[test_path] = hits
        for script, tests in SCRIPT_TESTS.items():
            if script in server_changed:
                for test_path in tests:
                    selected.setdefault(test_path, [f"{script} 的行为测试"])

        print(f"[选测] 后端改动 {len(server_changed)} 个文件：")
        for p in server_changed:
            print(f"  {p}")
        if web_only:
            print(f"[选测] 另有 {len(web_only)} 个前端/文档改动 → 走构建门禁，不进 pytest。")
        if not selected:
            print("[选测] 无测试在代码导入关系上受影响（该模块尚无测试覆盖）。")
            return 0
        print(f"[选测] 命中 {len(selected)} 个测试文件（改动模块 + 导入它的模块 = {len(affected)} 个模块）：")
        for test_path, why in selected.items():
            if opts.why:
                print(f"  {test_path}  ← {', '.join(why[:4])}")
            else:
                print(f"  {test_path}")
        files = list(selected)

    cmd = [runner, "-m", "pytest", *files, "-q", "--no-header"]
    print("执行：" + " ".join(cmd[1:]))
    if opts.run:
        return subprocess.run(cmd, cwd=ROOT).returncode
    return 0


if __name__ == "__main__":
    sys.exit(main())
