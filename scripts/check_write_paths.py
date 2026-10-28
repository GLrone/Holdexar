"""写事务路径机械审计：裸写事务（未过写调度器）进不了仓库。

SQLite 单写者约束下，应用与维护脚本写事务必须位于共享写锁上下文
（见 app/core/database.py 的「全局写入调度器」）。本脚本用 AST 静态扫描：

- 检测点：`.commit()` / `.flush()`；execute/executescript/executemany/exec_driver_sql
  的 SQL 字面量含写语句或结构元数据 PRAGMA 赋值；`engine.begin()` /
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
import subprocess
import sys
from pathlib import Path


# 输出统一 UTF-8：管道场景 stdout 编码跟随 ANSI 代码页，西欧环境编不了中文
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "server" / "app"
BLOCKED_GATE_CALLS = {
    "gather", "create_task", "ensure_future", "TaskGroup", "sleep",
    "open", "read_text", "read_bytes", "write_text", "write_bytes", "unlink",
    "mkdir", "iterdir", "glob",
}
DATABASE_THREAD_CALLS = {
    "_merge_current_prices_sync", "_merge_game_tags_sync", "_merge_history_chunk",
    "_merge_bundles_sync",
}
BLOCKED_IO_MODULES = {
    "httpx", "requests", "aiohttp", "urllib", "socket", "smtplib",
    "subprocess", "ftplib", "websocket", "websockets",
}
NETWORK_METHODS = {"request", "get", "post", "put", "patch", "delete", "send", "fetch"}

# (文件, 函数) → (语句种类及调用点上限, 理由)。
ALLOW: dict[tuple[str, str], tuple[dict[str, int], str]] = {
    ("scripts/check_launcher_contract.py", "make_database"): (
        {"execute": 3, "commit": 1},
        "启动器合同检查的测试夹具：沙盒内造带 schema 的迷你库，非生产数据"),
    ("scripts/check_launcher_contract.py", "run_vector"): (
        {"execute": 7, "executemany": 1, "commit": 4},
        "启动器合同向量的夹具写：WAL 桩、迁移源库与事务样本均为沙盒临时数据"),
    ("core/update_data.py", "_read_only_source"): (
        {"execute": 1}, "同步维护锁内排空原库写者并保持快照窗口"),
    ("core/update_data.py", "prepare"): (
        {"gate:open": 1}, "维护窗口内同步不可变原始快照"),
    ("core/update_data.py", "rollback"): (
        {"gate:open": 1}, "维护窗口内同步待落位恢复副本"),
    ("core/update_data.py", "finish_rollback"): (
        {"gate:open": 1}, "解除保护前同步已恢复的程序指针"),
    ("core/update_data.py", "_release_completed_rollback"): (
        {"gate:open": 1}, "重入已结束恢复时确认终态记录持久化"),
    ("core/database.py", "_ensure_schema"): (
        {"execute": 1},
        "维护屏障内同步补列，事务由初始化持有"),
    ("core/database.py", "init_db"): (
        {"execute": 1}, "维护屏障内的建表与补列事务起点"),
    ("core/database.py", "_run_schema_migrations"): (
        {"execute": 2, "exec_driver_sql": 2, "commit": 2}, "维护屏障内的版本登记与迁移事务"),
    ("core/database.py", "_migrate_alert_targets_to_cny"): (
        {"execute": 1}, "迁移事务内批量归一提醒金额"),
    ("core/database.py", "_migrate_bundle_price_units"): (
        {"execute": 1}, "迁移事务内批量归一捆绑包金额"),
    ("core/database.py", "_migrate_cycle_coverage_snapshot"): (
        {"execute": 1}, "迁移事务内补齐周期覆盖字段"),
    ("core/database.py", "_migrate_drop_redundant_gph_indexes"): (
        {"execute": 1}, "迁移事务内删除冗余历史索引"),
    ("core/database.py", "_migrate_fts_trigger_when_gate"): (
        {"execute": 2}, "迁移事务内更新全文检索触发器"),
    ("core/database.py", "_migrate_fx_history_canonical"): (
        {"execute": 4}, "迁移事务内归一汇率历史"),
    ("core/database.py", "_migrate_games_name_fts"): (
        {"execute": 6}, "迁移事务内创建游戏全文检索"),
    ("core/database.py", "_migrate_gph_snapshot_unique"): (
        {"execute": 1}, "迁移事务内补齐历史观测唯一索引"),
    ("core/database.py", "_migrate_notification_identity"): (
        {"execute": 5}, "迁移事务内保留事件及来源身份高水位"),
    ("core/database.py", "_migrate_retire_orchestration"): (
        {"execute": 1}, "迁移事务内退役无消费运行记录"),
    ("core/database.py", "_migrate_retire_coverage_snapshot"): (
        {"execute": 1}, "迁移事务内退役周期覆盖明细副本"),
    ("core/database.py", "_migrate_proxy_ledger_columns"): (
        {"execute": 7}, "迁移事务内保全节点与冷却并退役重复列"),
    ("core/database.py", "_migrate_pilot_provider_config"): (
        {"execute": 2}, "既有迁移事务内批量迁入供方配置并清除旧键"),
    ("core/database.py", "_drop_projection_columns"): (
        {"execute": 1}, "既有迁移事务校验依赖后退役派生列；不独立提交"),
    ("core/database.py", "_prepare_legacy_v10_columns"): (
        {"execute": 1}, "初始化维护事务内补齐旧v10临时计数列"),
    ("core/database.py", "_migrate_monitoring_projection"): (
        {"execute": 1}, "既有迁移事务内校验并退役两项监控投影索引"),
    ("core/database.py", "_migrate_node_cooldown"): (
        {"execute": 2}, "既有迁移事务批量迁入节点冷却并退役旧表"),
    ("core/database.py", "_migrate_unused_session_state"): (
        {"execute": 1}, "既有迁移事务内退役无消费的账单探测键"),
    ("core/database.py", "_migrate_monitoring_bootstrap"): (
        {"execute": 5}, "迁移事务内建立监控来源"),
    ("core/database.py", "_migrate_player_tags"): (
        {"execute": 1}, "迁移事务内移除旧玩家标签"),
    ("core/database.py", "_migrate_pool_flags_retire"): (
        {"execute": 9}, "迁移事务内替换旧代理池标记"),
    ("core/database.py", "_migrate_proxynode_subscription_identity"): (
        {"execute": 9}, "迁移事务内归一节点订阅身份"),
    ("core/backup.py", "_vacuum_file"): (
        {"execute": 1},
        "原地 VACUUM（停写入面的独占窗口执行，靠调用方调度保证独占）"),
    ("core/backup.py", "_protect_restore_recovery"): (
        {"flush": 1}, "维护屏障内同步恢复保护文件缓冲"),
    ("core/db_safety.py", "_acquire_restore_lock"): (
        {"flush": 1}, "维护屏障内同步恢复锁文件落盘缓冲"),
    ("core/db_safety.py", "snapshot_to"): (
        {"execute": 1},
        "VACUUM INTO 在线快照（对源库只读，产出自洽新文件）"),
    ("core/migration.py", "migrate_appdata_to_portable"): (
        {"execute": 1},
        "VACUUM INTO 迁移对源库只读，产出自洽新文件"),
    ("core/migration.py", "adopt_source_database"): (
        {"execute": 1},
        "同一 VACUUM INTO 快照（对源库只读），函数拆分后写点随之迁移"),
    ("domains/proxypool/registry.py", "settle_nodes_after_source_change"): (
        {"flush": 1}, "flush-only 共用收尾（快照对齐/退出/清理），事务边界在调用方闸内"),
    ("domains/proxypool/admission.py", "exit_from_production"): (
        {"flush": 1}, "flush-only，事务边界在调用方闸内"),
    ("domains/proxypool/admission.py", "promote_to_active"): (
        {"flush": 1}, "flush-only，事务边界在调用方闸内"),
    ("domains/proxypool/registry.py", "_apply_nodes"): (
        {"flush": 1}, "flush-only，事务边界在调用方闸内（账本对齐/转正）"),
    ("domains/proxypool/subscription.py", "persist_snapshot"): (
        {"flush": 1}, "flush-only，事务边界在调用方闸内（订阅同步）"),
    ("domains/proxypool/jobruns.py", "start_run"): (
        {"flush": 1}, "flush-only，事务边界在调用方闸内（record_start）"),
    ("domains/proxypool/exitstats.py", "record_run_exits"): (
        {"commit": 1}, "提交由调用方 write_run_exits 的 write_gate 覆盖"),
}

SQL_WRITE_RE = re.compile(
    r"^\s*(?:(INSERT\s+OR\s+REPLACE|INSERT|UPDATE|DELETE|REPLACE\s+INTO|"
    r"BEGIN\s+IMMEDIATE|CREATE|DROP|ALTER|VACUUM|ATTACH|ANALYZE|REINDEX)\b|"
    r"PRAGMA\s+(?:\w+\.)?(?:user_version|application_id|schema_version|writable_schema)\s*(?:=|\())",
    re.IGNORECASE,
)


class _Visitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.hits: list[tuple[int, str]] = []
        self._parents: dict[ast.AST, ast.AST] = {}
        self._func_stack: list[str] = []
        self._module_aliases: dict[str, str] = {}
        self._direct_aliases: dict[str, str] = {}
        self._assigned_aliases: dict[str, list[str]] = {}
        self._allow_used: dict[tuple[str, str], int] = {}

    def build_parents(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                self._parents[child] = node
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self._module_aliases[alias.asname or alias.name.split(".")[0]] = alias.name
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if module.split(".")[0] in BLOCKED_IO_MODULES or module in {"asyncio", "app.core.database"}:
                    for alias in node.names:
                        self._direct_aliases[alias.asname or alias.name] = f"{module}.{alias.name}"

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
                        parts = self._resolved_parts(ctx.func)
                        name = parts[-1] if parts else None
                        if name in ("write_gate", "write_session", "sync_write_gate"):
                            return True
            cur = self._parents.get(cur)
        return False

    @staticmethod
    def _call_parts(node: ast.AST) -> list[str]:
        if isinstance(node, ast.Name):
            return [node.id]
        if isinstance(node, ast.Attribute):
            return _Visitor._call_parts(node.value) + [node.attr]
        return []

    def _gate_effect_reason(self, node: ast.Call) -> str | None:
        parts = self._resolved_parts(node.func)
        if not parts:
            return None
        direct = self._direct_aliases.get(parts[0])
        module_root = self._module_aliases.get(parts[0], parts[0]).split(".")[0]
        if direct:
            module_root = direct.split(".")[0]
        leaf = parts[-1]
        if direct and direct.split(".")[-1] in BLOCKED_GATE_CALLS:
            return "闸内禁止并发派生、等待与文件读写"
        if leaf in BLOCKED_GATE_CALLS:
            return "闸内禁止并发派生、等待与文件读写"
        if leaf == "to_thread" or (direct and direct.endswith(".to_thread")):
            return "闸内同步数据库操作必须使用取消安全的线程收尾"
        if leaf == "complete_db_thread":
            target_parts = self._resolved_parts(node.args[0]) if node.args else []
            if not target_parts or target_parts[-1] not in DATABASE_THREAD_CALLS:
                return "闸内线程池调用仅允许登记过的 SQLite 批量写入"
        if module_root in BLOCKED_IO_MODULES:
            return "闸内禁止网络、子进程及外部文件 IO"
        if leaf in NETWORK_METHODS and parts[0].lower() in {
            "client", "http", "api", "steam", "smtp", "transport", "websocket",
        }:
            return "闸内禁止外部网络 IO"
        return None

    def _resolved_parts(self, node: ast.AST) -> list[str]:
        parts = self._call_parts(node)
        if parts and parts[0] in self._assigned_aliases:
            parts = self._assigned_aliases[parts[0]] + parts[1:]
        if parts and parts[0] in self._direct_aliases:
            parts = self._direct_aliases[parts[0]].split(".") + parts[1:]
        return parts

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        self.generic_visit(node)
        parts = self._resolved_parts(node.value)
        for target in node.targets:
            for name in ast.walk(target):
                if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store):
                    self._assigned_aliases.pop(name.id, None)
                    if isinstance(target, ast.Name) and parts:
                        self._assigned_aliases[name.id] = parts

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        self.generic_visit(node)
        if node.value is not None and isinstance(node.target, ast.Name):
            parts = self._resolved_parts(node.value)
            self._assigned_aliases.pop(node.target.id, None)
            if parts:
                self._assigned_aliases[node.target.id] = parts

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        outer = self._assigned_aliases
        self._assigned_aliases = outer.copy()
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg:
                self._assigned_aliases.pop(arg.arg, None)
        self.generic_visit(node)
        self._assigned_aliases = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def _allow(self, node: ast.AST) -> str | None:
        funcs = self._enclosing_funcs(node)
        # 文件内豁免按「任意外层函数名」匹配（内层 helper 属于外层事务）
        for fname in reversed(funcs):
            entry = ALLOW.get((self._file_suffix, fname))
            if entry and self.kind in entry[0]:
                key = (fname, self.kind)
                used = self._allow_used.get(key, 0) + 1
                self._allow_used[key] = used
                return entry[1] if used <= entry[0][self.kind] else None
        return None

    file_suffix = ""
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
        if self._in_gate(node):
            reason = self._gate_effect_reason(node)
            if reason:
                self.kind = f"gate:{self._call_parts(node.func)[-1]}"
                if self._allow(node):
                    self.hits.append((node.lineno, "EXCEPTION"))
                else:
                    self.hits.append((node.lineno, f"FAIL_GATE_EFFECT:{reason}"))
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr in (
            "commit", "flush", "execute", "executescript", "executemany", "exec_driver_sql",
        ):
            flagged = f.attr in ("commit", "flush")
            if not flagged and node.args:
                sql = self._sql_text(node.args[0])
                flagged = bool(sql and SQL_WRITE_RE.match(sql))
            if not flagged and f.attr in ("execute", "executemany", "exec_driver_sql") and self._in_begin_tx(node):
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

    def _ctx_name(self, ctx: ast.expr) -> str | None:
        """with 上下文名（解包 get_session_factory()() 这类嵌套调用）。"""
        f = ctx.func if isinstance(ctx, ast.Call) else ctx
        while isinstance(f, ast.Call):
            f = f.func
        parts = self._resolved_parts(f)
        return parts[-1] if parts else None

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


def audit_source(source: str, file_suffix: str = "<test>") -> tuple[
    list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, str]]
]:
    tree = ast.parse(source)
    v = _Visitor()
    v.build_parents(tree)
    v._file_suffix = file_suffix
    v.visit(tree)
    fails = [(ln, k) for ln, k in v.hits if k == "FAIL"]
    exceptions = [(ln, k) for ln, k in v.hits if k == "EXCEPTION"]
    composites = [(ln, k) for ln, k in v.hits if k == "FAIL_COMPOSITE"]
    effects = [(ln, k.split(":", 1)[1]) for ln, k in v.hits if k.startswith("FAIL_GATE_EFFECT:")]
    return fails, exceptions, composites, effects


def audit_file(path: Path) -> tuple[list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, str]], list[tuple[int, str]]]:
    return audit_source(
        path.read_text(encoding="utf-8"),
        path.relative_to(APP_DIR).as_posix() if path.is_relative_to(APP_DIR) else path.relative_to(ROOT).as_posix(),
    )


def source_paths() -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--",
         "scripts", "server/scripts"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    )
    return sorted(set(APP_DIR.rglob("*.py")) | {
        ROOT / name for name in result.stdout.splitlines()
        if name.endswith(".py") and (ROOT / name).is_file()
    })


def main() -> int:
    verbose = "--verbose" in sys.argv
    total_fail = 0
    total_exc = 0
    for path in source_paths():
        try:
            fails, exceptions, composites, effects = audit_file(path)
        except SyntaxError as e:
            print(f"FAIL {path.relative_to(ROOT)}: 解析失败 {e}")
            total_fail += 1
            continue
        rel = path.relative_to(ROOT).as_posix()
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
        for lineno, reason in effects:
            print(f"FAIL {rel}:{lineno}")
            print(f"    {reason}")
        for lineno, _ in exceptions:
            if verbose:
                print(f"EXCEPTION {rel}:{lineno}（ALLOW 已登记）")
        total_fail += len(fails) + len(composites) + len(effects)
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
