"""写事务路径机械审计：裸写事务（未过写调度器）进不了仓库。

SQLite 单写者约束下，server/app 内所有写事务必须位于 write_gate() 调度上下文
（见 app/core/database.py 的「全局写入调度器」）。本脚本用 AST 静态扫描：

- 检测点：`.commit()` / `.flush()` 调用；`.execute()` / `.executescript()` /
  `.executemany()` 的 SQL 字面量含写语句（INSERT/UPDATE/DELETE/REPLACE/
  BEGIN IMMEDIATE/DDL/VACUUM/ATTACH/ANALYZE/REINDEX）；`engine.begin()` /
  `session.begin()` 事务块（排除 begin_nested）内的 execute/executemany
  整体按写对待（提交在块退出自动发生）。
- 已知 AST 数据流边界（fail-closed 之外）：别名绑定调用（`c = s.commit` 后
  `await c()`）与非常量 SQL 变量不可见，词法表达不了的形态走 ALLOW 登记。
- 判定：沿 AST 祖先链找 `async with write_gate(...)` / `write_session(...)`
  （含复合 with 的任一 context item）；命中 = PASS，未命中 = FAIL。
- 复合形态：write_gate 与 get_session_factory 同 with 复合 → FAIL（闸内新建
  会话必须走 write_session：事务起点显式取写锁；write_session 自身定义豁免）。
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
    ("crawler/db_writer.py", "_session_scope"): (
        frozenset({"execute"}),
        "自有会话事务起点 BEGIN IMMEDIATE（调用点在 write_gate/write_session 上下文内，防跨进程先读后写快照失效）"),
    ("core/seed_assets.py", "apply_curated_rows"): (
        frozenset({"execute"}),
        "人工列批量补挂语句体（调用点在 write_session/段内保存点事务内，事务边界在调用方）"),
    ("core/seed_assets.py", "_merge_history_chunk"): (
        frozenset({"commit", "execute"}),
        "线程池原生连接历史合并（调用点已 write_gate 包裹（分块续跑），缺键 INSERT + 派生列差异覆写 UPDATE，价格键不回拨）"),
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
    ("core/backup.py", "_vacuum_file"): (
        frozenset({"execute"}),
        "原地 VACUUM（停写入面的独占窗口执行，靠调用方调度保证独占）"),
    ("core/backup.py", "snapshot_to"): (
        frozenset({"execute"}),
        "VACUUM INTO 在线快照（对源库只读，产出自洽新文件）"),
    ("core/migration.py", "migrate_appdata_to_portable"): (
        frozenset({"execute"}),
        "VACUUM INTO 迁移对源库只读，产出自洽新文件"),
    ("core/migration.py", "adopt_source_database"): (
        frozenset({"execute"}),
        "同一 VACUUM INTO 快照（对源库只读），函数拆分后写点随之迁移"),
    ("domains/family/service.py", "_absorb_local_members"): (
        frozenset({"commit"}),
        "调用点已 write_gate 包裹（吸收即调用方事务的一步）"),
    ("domains/proxypool/registry.py", "settle_nodes_after_source_change"): (
        frozenset({"flush"}), "flush-only 共用收尾（快照对齐/退出/清理），事务边界在调用方闸内"),
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
    r"BEGIN\s+IMMEDIATE|CREATE|DROP|ALTER|VACUUM|ATTACH|ANALYZE|REINDEX)\b",
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
            "init_db" in funcs or "_run_schema_migrations" in funcs
            or any(f.startswith("_migrate_") for f in funcs)
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

    def _in_begin_tx(self, node: ast.AST) -> bool:
        cur = self._parents.get(node)
        while cur is not None:
            if isinstance(cur, (ast.AsyncWith, ast.With)):
                for item in cur.items:
                    ctx = item.context_expr
                    if isinstance(ctx, ast.Call) and isinstance(ctx.func, ast.Attribute) \
                            and ctx.func.attr == "begin":
                        return True
            cur = self._parents.get(cur)
        return False

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in (
            "commit", "flush", "execute", "executescript", "executemany",
        ):
            flagged = f.attr in ("commit", "flush")
            if not flagged and node.args:
                sql = self._sql_text(node.args[0])
                flagged = bool(sql and SQL_WRITE_RE.match(sql))
            if not flagged and f.attr in ("execute", "executemany") and self._in_begin_tx(node):
                flagged = True
            if flagged and not self._in_gate(node):
                self.kind = f.attr
                reason = self._allow(node)
                kind = "EXCEPTION" if reason else "FAIL"
                self.hits.append((node.lineno, kind))
        self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:  # noqa: N802
        self._check_gate_session_composite(node)
        self.generic_visit(node)

    visit_With = visit_AsyncWith

    @staticmethod
    def _ctx_name(ctx: ast.expr) -> str | None:
        """with 上下文名（解包 get_session_factory()() 这类嵌套调用）。"""
        f = ctx.func if isinstance(ctx, ast.Call) else ctx
        while isinstance(f, ast.Call):
            f = f.func
        if isinstance(f, ast.Name):
            return f.id
        if isinstance(f, ast.Attribute):
            return f.attr
        return None

    def _check_gate_session_composite(self, node: ast.AST) -> None:
        """write_gate 与 get_session_factory 同 with 复合 → FAIL。

        闸内新建会话必须走 write_session（事务起点显式取写锁，防跨进程
        先读后写快照失效）；write_session 自身定义是唯一合法形态。
        """
        has_gate = has_session = False
        for item in getattr(node, "items", []):
            name = self._ctx_name(item.context_expr)
            if name == "write_gate":
                has_gate = True
            elif name == "get_session_factory":
                has_session = True
        if not (has_gate and has_session):
            return
        if self._file_suffix == "core/database.py" and "write_session" in self._enclosing_funcs(node):
            return
        self.hits.append((node.items[0].context_expr.lineno, "FAIL_COMPOSITE"))

    def _sql_text(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = "".join(
                v.value for v in node.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            )
            if parts:
                return parts
            return "UPDATE"  # 无常量片段无法判读，fail-closed 按写处理
        if isinstance(node, ast.Call) and node.args:
            return self._sql_text(node.args[0])
        return None


def audit_file(path: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    v = _Visitor()
    v.build_parents(tree)
    v._file_suffix = path.relative_to(APP_DIR).as_posix()
    v.source_lines = path.read_text(encoding="utf-8").splitlines()
    v.visit(tree)
    fails = [(ln, k) for ln, k in v.hits if k == "FAIL"]
    exceptions = [(ln, k) for ln, k in v.hits if k == "EXCEPTION"]
    composites = [(ln, k) for ln, k in v.hits if k == "FAIL_COMPOSITE"]
    return fails, exceptions, composites


def main() -> int:
    verbose = "--verbose" in sys.argv
    total_fail = 0
    total_exc = 0
    for path in sorted(APP_DIR.rglob("*.py")):
        try:
            fails, exceptions, composites = audit_file(path)
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
        for lineno, _ in composites:
            src = " ".join(path.read_text(encoding="utf-8").splitlines()[lineno - 1].split())[:100]
            print(f"FAIL {rel}:{lineno}")
            print(f"    {src}")
            print("    闸内新建会话必须走 write_session（事务起点显式取写锁）")
        for lineno, _ in exceptions:
            if verbose:
                print(f"EXCEPTION {rel}:{lineno}（ALLOW 已登记）")
        total_fail += len(fails) + len(composites)
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
