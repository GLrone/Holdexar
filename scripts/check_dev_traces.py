"""注释留痕门禁：暂存新增行的注释 / docstring 里禁入开发过程叙述。

判定标准：注释只描述**当前行为与约束**（时序 / 口径 / 字段含义 / 不变量）。
改动经过、方案取舍、验证数据、否决过的备选一律不写入代码文件——注释是给
下一位读者理解代码用的，不是开发日志。

只拦「本次暂存新增行」：已提交存量不追溯（用 --all 盘点清理）。

用法：
    python scripts/check_dev_traces.py --staged      # pre-commit：拦暂存新增行
    python scripts/check_dev_traces.py --all         # 全树排查（存量清理盘点）
    python scripts/check_dev_traces.py <path>...     # 指定文件（测试用）

退出码：0 通过 / 1 命中（拒绝）/ 2 用法错误。
"""
from __future__ import annotations

import argparse
import ast
import io
import re
import subprocess
import sys
import tokenize
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]

# ── 词形清单（唯一来源；加新词形前先跑 --staged 确认对当前暂存零误报）──
# 四类：过程叙述 / AI 痕迹 / 历史对比与改动交代 / 问答式论证。
# 词形宁缺毋滥：每条都必须「注释里出现即留痕」，有合法语义的词不进清单
# （「佐证」是系列判定规则用词、「迁移链」是 schema 机制术语，均不收）。
TRACE_PATTERNS: list[tuple[str, str]] = [
    (r"实测|拍板|复盘", "过程叙述（验证 / 决策经过）"),
    (r"AI\s*生成|生成于|Claude|ChatGPT|GPT-\d|Copilot|大语言模型", "AI 生成痕迹"),
    (r"原先|旧写法|旧逻辑|旧的实现|曾用|重构后|迁移自|已删除了|删掉了|去掉了",
     "历史对比 / 改动交代"),
    (r"^[ \t]*(?:[*/<>!\-#\"'\u201c\u201d·•][ \t]*){0,6}为什么", "问答式开发论证"),
    (r"背景[：:]|动机[：:]", "开发背景叙述"),
    (r"我们", "第一人称叙述"),
    (r"20\d{2}-\d{2}-\d{2}", "注释内开发日期"),
]

# 例外登记：文件 → [(正则, 理由)]。命中行的注释文本匹配该正则即放行。
# 仅收「词形在注释里有正当数据语义」的场景（数据集范围 / 字段取值示例 /
# 真实世界日期），逐条写明理由——登记是例外生效的前置条件。
ALLOW: dict[str, list[tuple[str, str]]] = {
    "scripts/fx_maintenance.py": [
        (r"20\d{2}-\d{2}-\d{2}", "汇率档案的数据集年份范围（产品语义）"),
    ],
    "server/app/domains/games/models.py": [
        (r"20\d{2}-\d{2}-\d{2}", "epic_date 字段的取值形态示例"),
    ],
    "server/app/domains/metadata/service.py": [
        (r"20\d{2}-\d{2}-\d{2}", "日期解析的兼容形态示例"),
    ],
    "server/tests/test_bills.py": [
        (r"20\d{2}-\d{2}-\d{2}", "交易日期夹具（断言口径即日期先后）"),
    ],
    "server/tests/test_price_anchor_schedule.py": [
        (r"20\d{2}-\d{2}-\d{2}", "夏令时切换的测试场景日期（真实世界事实）"),
    ],
}

# ── 文件分类：注释形态决定提取器 ──
PY_LIKE = {".py", ".spec"}
C_LIKE = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".vue", ".css", ".scss", ".html", ".htm"}
HASH_LIKE = {".sh", ".ps1"}
BAT_LIKE = {".bat", ".cmd"}
SCANNABLE = PY_LIKE | C_LIKE | HASH_LIKE | BAT_LIKE

_COMPILED_TRACES = [(re.compile(p), label) for p, label in TRACE_PATTERNS]
_COMPILED_ALLOW = {
    path: [(re.compile(p), reason) for p, reason in rules]
    for path, rules in ALLOW.items()
}


# ── 注释提取：返回 [(行号, 注释文本)]；字符串字面量一律不收 ──

def _py_comments(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(text).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.start[0], tok.string))
    except tokenize.TokenError:
        # 语法不完整时退化为逐行前缀判定， # 注释仍可收
        out.extend(
            (i, line) for i, line in enumerate(text.splitlines(), 1)
            if line.lstrip().startswith("#")
        )
    return out


def _py_docstrings(text: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return out
    lines = text.splitlines()
    for node in ast.walk(tree):
        if not isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            continue
        if not node.body or not isinstance(node.body[0], ast.Expr):
            continue
        value = node.body[0].value
        if not (isinstance(value, ast.Constant) and isinstance(value.value, str)):
            continue
        for ln in range(value.lineno, value.end_lineno + 1):
            if ln <= len(lines):
                out.append((ln, lines[ln - 1]))
    return out


def _c_like_comments(text: str) -> list[tuple[int, str]]:
    """状态机提取 // /* */ <!-- -->；字符串与模板串内容跳过。

    `//` 前一字符为 `:` 时不算注释头（URL 的 `https://`）；跨行块注释按行
    拆成多条，报错行号指向实际行而非注释起始行。
    """
    out: list[tuple[int, str]] = []
    collected: list[str] = []
    line = 1
    start_line = 1

    def flush() -> None:
        if not collected:
            return
        for idx, part in enumerate("".join(collected).split("\n")):
            out.append((start_line + idx, part))
        collected.clear()

    state = "code"
    prev = ""
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""

        if state == "code":
            if ch == "\n":
                line += 1
                prev = ""
                i += 1
                continue
            if ch == "/" and nxt == "/" and prev != ":":
                state, start_line, i = "line_comment", line, i + 2
                continue
            if ch == "/" and nxt == "*":
                state, start_line, i = "block_comment", line, i + 2
                continue
            if text.startswith("<!--", i):
                state, start_line, i = "html_comment", line, i + 4
                continue
            if ch in ("'", '"', "`"):
                state = {"'": "s_quote", '"': "d_quote", "`": "t_quote"}[ch]
            prev = ch
            i += 1
            continue

        if state == "line_comment":
            if ch == "\n":
                flush()
                state, line, prev, i = "code", line + 1, "", i + 1
                continue
            collected.append(ch)
            prev = ch
            i += 1
            continue

        if state in ("block_comment", "html_comment"):
            term = "*/" if state == "block_comment" else "-->"
            if text.startswith(term, i):
                flush()
                state, prev, i = "code", "", i + len(term)
                continue
            if ch == "\n":
                collected.append("\n")
                line += 1
                i += 1
                continue
            collected.append(ch)
            prev = ch
            i += 1
            continue

        # 字符串态：跳过内容（含模板串），不把其中的 // 或 /* 当注释
        if ch == "\n":
            line += 1
            prev = ""
            i += 1
            continue
        if ch == "\\":
            i += 2
            continue
        if (state == "s_quote" and ch == "'") or (
            state == "d_quote" and ch == '"'
        ) or (state == "t_quote" and ch == "`"):
            state = "code"
            prev = ch
        i += 1

    flush()  # EOF：未闭合注释（截断文件）也收
    return out


def _hash_comments(text: str) -> list[tuple[int, str]]:
    return [
        (i, line)
        for i, line in enumerate(text.splitlines(), 1)
        if line.lstrip().startswith("#")
    ]


def _bat_comments(text: str) -> list[tuple[int, str]]:
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        stripped = line.lstrip().upper()
        if stripped.startswith("REM ") or stripped.startswith("::"):
            out.append((i, line))
    return out


def extract_comments(path: str, text: str) -> list[tuple[int, str]]:
    suffix = PurePosixPath(path).suffix.lower()
    if suffix in PY_LIKE:
        return _py_comments(text) + _py_docstrings(text)
    if suffix in C_LIKE:
        return _c_like_comments(text)
    if suffix in HASH_LIKE:
        return _hash_comments(text)
    if suffix in BAT_LIKE:
        return _bat_comments(text)
    return []


# ── 扫描 ──

class Finding:
    __slots__ = ("path", "label", "detail")

    def __init__(self, path: str, label: str, detail: str) -> None:
        self.path, self.label, self.detail = path, label, detail

    def __str__(self) -> str:
        return f"  {self.path}\n      {self.label}：{self.detail}"


def scan_comments(path: str, comments: list[tuple[int, str]]) -> list[Finding]:
    findings: list[Finding] = []
    waivers = _COMPILED_ALLOW.get(path, [])
    for lineno, text in comments:
        for pattern, label in _COMPILED_TRACES:
            match = pattern.search(text)
            if not match:
                continue
            if any(w.search(text) for w, _reason in waivers):
                break
            snippet = text.strip()[:90]
            findings.append(
                Finding(path, label, f"第 {lineno} 行「{match.group(0)}」：{snippet}")
            )
            break  # 一行只报第一条
    return findings


# ── 范围解析 ──

def _run_git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, errors="replace"
    )
    if proc.returncode != 0:
        raise SystemExit(f"[留痕门禁] git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


def staged_new_lines(path: str) -> set[int]:
    """暂存 diff 的新增行号集合（-U0，含 hunk 头解析）。"""
    diff = _run_git("diff", "--cached", "-U0", "--no-color", "--", path)
    lines: set[int] = set()
    lineno = 0
    for raw in diff.splitlines():
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)", raw)
            lineno = int(m.group(1)) if m else 0
        elif raw.startswith("+") and not raw.startswith("+++"):
            lines.add(lineno)
            lineno += 1
    return lines


def staged_check() -> tuple[list[Finding], list[str]]:
    """对每个暂存代码文件：暂存区版本的注释提取 ∩ 新增行号。"""
    out = _run_git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    files = [p for p in out.split("\0") if p]
    findings: list[Finding] = []
    checked = []
    for path in files:
        if PurePosixPath(path).suffix.lower() not in SCANNABLE:
            continue
        blob = _run_git("show", f":{path}")
        comments = extract_comments(path, blob)
        new_lines = staged_new_lines(path)
        hits = [c for c in comments if c[0] in new_lines]
        if hits:
            checked.append(path)
            findings.extend(scan_comments(path, hits))
    return findings, checked


def explicit(paths: list[str]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    for path in paths:
        rel = str(Path(path).resolve().relative_to(ROOT)) if Path(path).is_absolute() else path
        full = Path(path) if Path(path).is_absolute() else ROOT / path
        if PurePosixPath(rel).suffix.lower() not in SCANNABLE or not full.is_file():
            continue
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_comments(rel, extract_comments(rel, text)))
    return findings, paths


def whole_tree() -> tuple[list[Finding], list[str]]:
    """全树排查：扫已跟踪代码文件的**全部**注释（含存量，供清理盘点）。

    与 --staged 的分工：这里是盘点工具而非提交闸门——存量尚未清完时
    --all 非零退出属预期，不阻断提交。
    """
    out = _run_git("ls-files", "-z")
    files = [p for p in out.split("\0") if p]
    findings: list[Finding] = []
    scanned = 0
    for path in files:
        if PurePosixPath(path).suffix.lower() not in SCANNABLE:
            continue
        full = ROOT / path
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scanned += 1
        findings.extend(scan_comments(path, extract_comments(path, text)))
    return findings, [f"{scanned} 个代码文件"]


def _ensure_output_encoding() -> None:
    probe = "门禁"
    for stream in (sys.stdout, sys.stderr):
        encoding = getattr(stream, "encoding", None)
        if not encoding:
            continue
        try:
            probe.encode(encoding)
        except (UnicodeEncodeError, LookupError):
            pass
        else:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure:
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (OSError, ValueError):
                pass


def main(argv: list[str] | None = None) -> int:
    _ensure_output_encoding()
    parser = argparse.ArgumentParser(
        prog="check_dev_traces.py",
        description="Holdexar 注释留痕门禁",
        usage="%(prog)s [-h] (--staged | --all | <path>...)",
    )
    parser.add_argument("--staged", action="store_true", help="只检查暂存新增行（pre-commit）")
    parser.add_argument("--all", action="store_true", help="全树排查（存量清理盘点，非提交闸门）")
    parser.add_argument("paths", nargs="*", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if sum((args.staged, args.all, bool(args.paths))) != 1:
        parser.error("必须且只能指定一种范围：--staged / --all / <path>...")

    if args.staged:
        findings, files = staged_check()
        scope = f"暂存新增行（{len(files)} 个代码文件）"
    elif args.all:
        findings, files = whole_tree()
        scope = f"全树（{files[0]}）"
    else:
        findings, files = explicit(args.paths)
        scope = f"指定文件（{len(files)} 个）"

    if not findings:
        print(f"[留痕门禁] [OK] 注释留痕检查通过 —— {scope}")
        return 0

    print(f"[留痕门禁] [FAIL] 注释留痕检查未通过 —— {scope}", file=sys.stderr)
    for finding in findings:
        print(str(finding), file=sys.stderr)
    print(
        "\n[留痕门禁] 处置方式：\n"
        "       · 注释只写当前行为与约束（时序 / 口径 / 字段含义）；改动经过、\n"
        "         方案取舍、验证数据一律不写入代码文件。\n"
        "       · 命中处优先改写成行为陈述（纯叙述才删），保留原约束语义。\n"
        "       · 确属数据语义等正当场景 → 修 scripts/check_dev_traces.py 的\n"
        "         ALLOW（文件 + 正则 + 理由），而不是 --no-verify 绕行。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
