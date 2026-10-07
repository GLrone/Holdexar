"""写事务路径机械审计：裸写事务（未过写调度器）进不了仓库。

SQLite 单写者约束下，server/app 内所有写事务必须位于 write_gate() 调度上下文
（见 app/core/database.py 的「全局写入调度器」）。本脚本用 AST 静态扫描：

- 检测点：`.commit()` / `.flush()` 调用；`.execute()` / `.executescript()` 的
  SQL 字面量含写语句（INSERT/UPDATE/DELETE/REPLACE/BEGIN IMMEDIATE/DDL）。
- 判定：沿 AST 祖先链找 `async with write_gate(...)`（含复合 with 的任一
  context item）；命中 = PASS，未命中 = FAIL。
- 豁免：调用点在闸内、本体是线程池同步函数或迁移机制等无法词法表达的形态，
  逐条登记在 ALLOW（文件 + 函数 + 理由）；登记是豁免生效的前置条件，
  与 docs/agent-context/CONSTRAINTS.md §八例外登记表同则。

用法：`python scripts/check_write_paths.py`（全量扫描，pre-commit 调用）；
`--verbose` 额外打印 PASS 明细。FAIL 存在时 exit 1。
"""
from __future__ import annotations

import ast
import re
import sys
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent.parent / "server" / "app"

# 写事务豁免登记（文件后缀, 函数名）→ (豁免语句种类, 理由)。kind ∈
# {"commit", "flush", "execute"}：按语句种类豁免（如只豁免 flush，该函数
# 日后新增的裸 commit 仍会被拦）。豁免 = 调用点已在 write_gate 内、或写锁
# 天然独占的启动机制；新增豁免须先在此登记并同步 CONSTRAINTS §八。
ALLOW: dict[tuple[str, str], tuple[frozenset[str], str]] = {
    ("core/seed_assets.py", "_merge_history_chunk"): (
        frozenset({"commit", "execute"}),
        "线程池原生连接历史合并（分块续跑，每块调用点已 write_gate 包裹，六段逻辑键 INSERT only）"),
    ("core/seed_assets.py", "_merge_bundles_sync"): (
        frozenset({"commit", "execute"}),
        "线程池原生连接捆绑包合并（调用点已 write_gate 包裹，与历史合并同形态）"),
    ("core/seed_assets.py", "_merge_current_prices_sync"): (
        frozenset({"commit", "execute"}),
        "线程池原生连接现价快照合并（调用点已 write_gate 包裹，与历史合并同形态）"),
    ("core/seed_assets.py", "_merge_game_tags_sync"): (
        frozenset({"commit", "execute"}),
        "线程池原生连接玩家标签合并（调用点已 write_gate 包裹，与历史合并同形态）"),
    ("core/database.py", "_ensure_schema"): (
        frozenset({"execute"}),
        "启动链建表与存量回填（sync 引擎独占窗口执行，写调度器管不到同步连接）"),
    ("core/backup.py", "restore_backup"): (
        frozenset({"execute"}),
        "恢复通道（独占进程内执行，整库重建）"),
    ("domains/monitoring/service.py", "_ensure_target"): (
        frozenset({"flush"}), "flush-only，事务边界在调用方闸内（ensure_source 等）"),
    ("domains/proxypool/admission.py", "exit_from_production"): (
        frozenset({"flush"}), "flush-only，事务边界在调用方闸内"),
    ("domains/proxypool/admission.py", "promote_to_active"): (
        frozenset({"flush"}), "flush-only，事务边界在调用方闸内"),
    ("domains/proxypool/registry.py", "apply_snapshot"): (
        frozenset({"flush"}), "flush-only，事务边界在调用方闸内（账本对齐/转正）"),
    ("domains/proxypool/subscription.py", "persist_snapshot"): (
        frozenset({"flush"}), "flush-only，事务边界在调用方闸内（订阅同步）"),
    ("domains/proxypool/jobruns.py", "start_run"): (
        frozenset({"flush"}), "flush-only，事务边界在调用方闸内（record_start）"),
    ("domains/proxypool/exitstats.py", "record_run_exits"): (
        frozenset({"commit"}), "提交由调用方 write_run_exits 的 write_gate 覆盖"),
}

SQL_WRITE_RE = re.compile(
    r"^\s*(INSERT\s+OR\s+REPLACE|INSERT|UPDATE|DELETE|REPLACE\s+INTO|"
    r"BEGIN\s+IMMEDIATE|CREATE|DROP|ALTER)\b",
    re.IGNORECASE,
)


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []
        self._parents: dict[ast.AST, ast.AST] = {}
        self._func_stack: list[str] = []

    def build_parents(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node

    def _enclosing_funcs(self, node: ast.AST) -> list[str]:
        names: list[str] = []
        cur = self._parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(cur.name)
            cur = self._parents.get(cur)
        return names

    def _in_gate(self, node: ast.AST) -> bool:
        cur = self._parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.AsyncWith, ast.With)):
                for item in cur.items:
                    ctx = item.context_expr
                    if isinstance(ctx, ast.Call):
                        f = ctx.func
                        name = f.id if isinstance(f, ast.Name) else (
                            f.attr if isinstance(f, ast.Attribute) else None)
                        if name in ("write_gate", "write_session"):
                            return True
            cur = self._parents.get(cur)
        return False

    def _allow(self, node: ast.AST) -> str | None:
        funcs = self._enclosing_funcs(node)
        # schema 迁移链：启动初始化机制，写锁天然独占窗口，整族豁免
        if self._file_suffix == "core/database.py" and (
            "init_db" in funcs or any(f.startswith("_migrate_") for f in funcs)
        ):
            return "schema 迁移链（启动初始化，写锁天然独占窗口）"
        # 文件内豁免按「任意外层函数名」匹配（内层 helper 属于外层事务）
        for fname in reversed(funcs):
            entry = ALLOW.get((self._file_suffix, fname))
            if entry and self.kind in entry[0]:
                return entry[1]
        return None

    file_suffix = ""
    source_lines: list[str] = []
    kind = "commit"

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in ("commit", "flush", "execute", "executescript"):
            flagged = f.attr in ("commit", "flush")
            if not flagged and node.args:
                sql = self._sql_text(node.args[0])
                flagged = bool(sql and SQL_WRITE_RE.match(sql))
            if flagged and not self._in_gate(node):
                self.kind = f.attr
                reason = self._allow(node)
                kind = "EXCEPTION" if reason else "FAIL"
                self.hits.append((node.lineno, kind))
        self.generic_visit(node)

    def _sql_text(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "text":
            if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                return node.args[0].value
        if isinstance(node, ast.JoinedStr):
            return ""
        return None


def audit_file(path: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    v = _Visitor()
    v.build_parents(tree)
    v._file_suffix = path.relative_to(APP_DIR).as_posix()
    v.source_lines = path.read_text(encoding="utf-8").splitlines()
    v.visit(tree)
    fails = [(ln, k) for ln, k in v.hits if k == "FAIL"]
    exceptions = [(ln, k) for ln, k in v.hits if k == "EXCEPTION"]
    return fails, exceptions


def main() -> int:
    verbose = "--verbose" in sys.argv
    total_fail = 0
    total_exc = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        try:
            fails, exceptions = audit_file(path)
        except SyntaxError as e:
            print(f"FAIL {path.relative_to(APP_DIR.parent)}: 解析失败 {e}")
            total_fail += 1
            continue
        rel = path.relative_to(APP_DIR.parent).as_posix()
        for lineno, _ in fails:
            src = " ".join(path.read_text(encoding="utf-8").splitlines()[lineno - 1].split())[:100]
            print(f"FAIL {rel}:{lineno}")
            print(f"    {src}")
            print("    未检测到 write_gate 上下文（写事务必须过全局写调度器）")
        for lineno, _ in exceptions:
            if verbose:
                print(f"EXCEPTION {rel}:{lineno}（ALLOW 已登记）")
        total_fail += len(fails)
        total_exc += len(exceptions)
    if verbose:
        print(f"EXCEPTION 合计 {total_exc}（ALLOW 登记，见脚本头）")
    if total_fail:
        print(f"✗ 裸写事务 {total_fail} 处：过 write_gate 或在 ALLOW 表登记（文件+函数+理由，"
              f"并同步 CONSTRAINTS §八）。")
        return 1
    print(f"✓ 写路径审计通过（豁免 {total_exc} 处均已登记）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
