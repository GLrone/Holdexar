"""提交前敏感面门禁：路径 / 体积 / 凭据特征三重检查。

**为什么放在 scripts/ 而不是内联进 .githooks/pre-commit**：
同一份判定要同时被本地钩子与 CI 使用（本地钩子可以被 `--no-verify` 绕过，
CI 不能）。两份实现必然漂移，而漂移的方向恰好是「一边拦得住、另一边拦不住」。
正则写在 shell 里还要过三层转义，改一次错一次。这里做成单一来源，可 pytest 覆盖。

**凭据特征为什么不写关键词而是写「值的形状」**：
`steamLoginSecure`、`steam_api_key`、`smtp.password` 在 18 / 9 / 2 个**合法源码**
文件里作为变量名或配置键出现（已实测）。裸关键词扫描会拦住正常提交，
而一个会拦住合法提交的门禁最终一定会被 `--no-verify` 绕过——等于没有门禁。
所以每条特征都要求「标识符 + 赋值 + 真实凭据的长度/字符集形状」。

**绝不回显命中的值**：只打印标签、位置与掩码预览。门禁把密钥打印进 CI 日志
本身就是一条新的泄漏路径。

用法：
    python scripts/check_secrets.py --staged      # pre-commit：只查本次暂存的改动
    python scripts/check_secrets.py --all         # CI：扫全树（兜住 --no-verify）
    python scripts/check_secrets.py <path>...     # 指定文件（测试用）

退出码：0 通过 / 1 命中（拒绝）/ 2 用法错误。
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# 单文件体积上限：挡住误入库的 SQLite 库 / 构建产物 / 二进制大文件。
# 这次事故里那份 23,351,296 字节的活库就是被这一条挡下的。
MAX_FILE_BYTES = 5 * 1024 * 1024

# ── 路径层：这些位置的任何文件都不该进版本库 ──
# 与 .gitignore 是两道独立防线：.gitignore 靠「默认不跟踪」，这里是
# 「即使被 -f 强加或 .gitignore 被改坏，也在提交那一刻拦住」。
#
# 第 1 组**必须锚定仓库根**（`^`，不写 `(?:^|/)`）：这些是本项目根目录下的
# 目录名。写成「任意深度」会误伤 `web/src/views/logs/Index.vue` 这类正常源码
# ——「logs」「release」作为业务视图目录名完全合法，已实测踩到过。
# 嵌套位置的数据文件由第 2 组的扩展名规则兜住。
PATH_DENY: list[tuple[str, str]] = [
    (
        r"^(?:data|data\.old|logs|release|reference|reference-repos|secrets"
        r"|gui-test-screenshots|price-history-prototype|assets/seed|assets/clash"
        r"|\.tmp-pytest|\.tmp-chrome-profile|\.tmp-research)(?:/|$)",
        "根目录下的运行时数据 / 构建产物 / 参考资料目录",
    ),
    (r"\.(?:db|sqlite3|db-wal|db-shm|log|pid|pem|key|p12|pfx|jks|keystore|mmdb|dat)$",
     "数据文件 / 私钥 / 证书 / 二进制资源"),
    (r"(?:^|/)\.env(?:\.|$)", "环境变量文件（可能含凭据）"),
    (r"(?:^|/)mail\.ini$", "邮件配置（含 SMTP 授权码）"),
    # secrets/ 作为目录名在业务代码里没有合法用途，故不锚定根。
    (r"(?:^|/)secrets(?:/|$)", "凭据目录"),
]

# ── 内容层：只匹配「真实凭据的形状」，匹配到的值一律掩码输出 ──
# 每条都必须满足：合法源码里不存在同形状的字面量。加新特征前先跑
# `python scripts/check_secrets.py --all` 确认全树零命中。
CONTENT_PATTERNS: list[tuple[str, str]] = [
    # Steam 登录会话：真值形如 <steamid64>%7C%7C<40位十六进制>。
    # 只看 `%7C%7C` 之后必须是十六进制串——测试夹具里的 `%7C%7Ctoken` 因此不命中。
    (r"%7C%7C[A-Fa-f0-9]{16,}", "Steam 登录会话 cookie"),
    (r"""steam_api_key["']?\s*[:=]\s*["']?[A-Fa-f0-9]{32}""", "Steam Web API Key（32 位十六进制）"),
    # SMTP 授权码：QQ/163 等为 16 位纯字母数字。测试里的 "saved-pass" 因含连字符且不足 12 位而漏过。
    (r"""smtp\.password["']?\s*[:=]\s*["'][A-Za-z0-9]{12,}["']""", "SMTP 授权码"),
    (r"\b(?:vless|vmess|trojan|ss|ssr|hysteria2?)://", "代理节点 / 订阅链接"),
    (r"-----BEGIN [A-Z ]*PRIVATE KEY-----", "私钥"),
    # 含密码的数据库连接串。要求密码段为 ≥8 位可打印 ASCII，
    # 故 fx_maintenance.py 里 `<本地旧库密码>` 这类中文占位符不命中。
    (r"postgres(?:ql)?://[A-Za-z0-9._-]+:[!-~]{8,}@", "含密码的数据库连接串"),
    (r"\bgh[pousr]_[A-Za-z0-9]{36}\b", "GitHub 令牌"),
    (r"\bsk-[A-Za-z0-9]{20,}\b", "API 密钥"),
]

_COMPILED_PATHS = [(re.compile(p), label) for p, label in PATH_DENY]
_COMPILED_CONTENT = [(re.compile(p), label) for p, label in CONTENT_PATTERNS]


def _mask(value: str) -> str:
    """掩码预览：只留首 4 字符与长度，绝不出示完整值。

    首 4 字符足以让人认出「这是哪一条」（例如 `7656…` 是 SteamID 前缀），
    又不足以还原。长度信息用于判断是完整凭据还是截断的示例。
    """
    head = value[:4]
    return f"{head}…（共 {len(value)} 字符，已掩码）"


def _run_git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(ROOT), capture_output=True, text=True, errors="replace"
    )
    if proc.returncode != 0:
        raise SystemExit(f"[门禁] git {' '.join(args)} 失败：{proc.stderr.strip()}")
    return proc.stdout


class Finding:
    __slots__ = ("path", "label", "detail")

    def __init__(self, path: str, label: str, detail: str) -> None:
        self.path, self.label, self.detail = path, label, detail

    def __str__(self) -> str:
        return f"  {self.path}\n      {self.label}：{self.detail}"


def check_path(path: str) -> list[Finding]:
    findings = []
    for pattern, label in _COMPILED_PATHS:
        if pattern.search(path):
            findings.append(Finding(path, label, "该路径不应进入版本库"))
            break
    return findings


def check_size(path: str, size: int) -> list[Finding]:
    if size > MAX_FILE_BYTES:
        mb = size / 1048576
        return [Finding(path, "文件体积超限", f"{mb:.1f} MB > {MAX_FILE_BYTES // 1048576} MB")]
    return []


def scan_lines(path: str, lines: list[tuple[int, str]]) -> list[Finding]:
    """扫 (行号, 行内容) 列表；命中只报标签 + 位置 + 掩码。"""
    findings = []
    for lineno, text in lines:
        for pattern, label in _COMPILED_CONTENT:
            match = pattern.search(text)
            if match:
                findings.append(Finding(path, label, f"第 {lineno} 行，{_mask(match.group(0))}"))
                break  # 一行只报第一条，避免同一行刷屏
    return findings


def staged_files() -> list[str]:
    out = _run_git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z")
    return [p for p in out.split("\0") if p]


def staged_added_lines() -> tuple[list[Finding], list[str]]:
    """返回 (发现, 已检查文件列表)。只扫新增行——删除行不构成泄漏。"""
    findings: list[Finding] = []
    files = staged_files()
    if not files:
        return findings, []

    # 逐文件取 diff 而非解析全局 diff 的 `+++` 头：路径含空格/引号时，
    # 全局 diff 的头部会被 git 转义（八进制），解析出来的路径对不上 ls-files。
    for path in files:
        findings.extend(check_path(path))
        proc = subprocess.run(
            ["git", "cat-file", "-s", f":{path}"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        if proc.returncode == 0:
            findings.extend(check_size(path, int(proc.stdout.strip())))

        diff = subprocess.run(
            ["git", "diff", "--cached", "-U0", "--no-color", "--", path],
            cwd=str(ROOT), capture_output=True, text=True, errors="replace",
        ).stdout
        lineno, lines = 0, []
        for raw in diff.splitlines():
            if raw.startswith("@@"):
                m = re.search(r"\+(\d+)", raw)
                lineno = int(m.group(1)) if m else 0
            elif raw.startswith("+") and not raw.startswith("+++"):
                lines.append((lineno, raw[1:]))
                lineno += 1
        findings.extend(scan_lines(path, lines))
    return findings, files


def tracked_files() -> list[str]:
    out = _run_git("ls-files", "-z")
    return [p for p in out.split("\0") if p]


def whole_tree() -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    files = tracked_files()
    for path in files:
        findings.extend(check_path(path))
        full = ROOT / path
        try:
            size = full.stat().st_size
        except OSError:
            continue
        findings.extend(check_size(path, size))
        # 二进制文件跳过内容扫描：正则扫二进制既慢又必然误报
        try:
            text = full.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings.extend(scan_lines(path, list(enumerate(text.splitlines(), 1))))
    return findings, files


def explicit(paths: list[str]) -> tuple[list[Finding], list[str]]:
    findings: list[Finding] = []
    for path in paths:
        rel = str(Path(path).resolve().relative_to(ROOT)) if Path(path).is_absolute() else path
        findings.extend(check_path(rel))
        full = Path(path) if Path(path).is_absolute() else ROOT / path
        if full.is_file():
            findings.extend(check_size(rel, full.stat().st_size))
            try:
                text = full.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            findings.extend(scan_lines(rel, list(enumerate(text.splitlines(), 1))))
    return findings, paths


def main() -> int:
    parser = argparse.ArgumentParser(description="Holdexar 敏感面门禁")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--staged", action="store_true", help="只检查已暂存的改动（pre-commit）")
    group.add_argument("--all", action="store_true", help="扫描全部已跟踪文件（CI）")
    group.add_argument("paths", nargs="*", default=None, help=argparse.SUPPRESS)
    args = parser.parse_args()

    if args.staged:
        findings, files = staged_added_lines()
        scope = f"暂存区（{len(files)} 个文件）"
    elif args.all:
        findings, files = whole_tree()
        scope = f"全树（{len(files)} 个已跟踪文件）"
    else:
        findings, files = explicit(args.paths)
        scope = f"指定文件（{len(files)} 个）"

    # 输出一律用 cp936 可编码的字符：本机 Windows 控制台是 cp936，
    # `✓`/`✗`（U+2713/U+2717）不在该字符集内，Python 会退化成字面 `✗`
    # 打印出来。用 ASCII 标记，中英文在 cp936 与 UTF-8 下都能正常显示。
    if not findings:
        print(f"[门禁] [OK] 敏感面检查通过 —— {scope}")
        return 0

    print(f"[门禁] [FAIL] 敏感面检查未通过 —— {scope}", file=sys.stderr)
    for finding in findings:
        print(str(finding), file=sys.stderr)
    print(
        "\n[门禁] 处置方式：\n"
        "       · 数据/日志/库文件 → 它们不该进版本库。开发态数据请走 dev.bat\n"
        "         （数据目录已在仓库工作区之外），仓库里不该再出现 data/。\n"
        "       · 凭据           → 不要提交，也不要「先提交再删」：历史里的值\n"
        "         即使后续提交删除也仍可被 checkout，必须视为已泄露并轮换。\n"
        "       · 确属误报       → 修 scripts/check_secrets.py 的特征，而不是\n"
        "         用 --no-verify 绕过（那会让本地与 CI 的判定永久分叉）。",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
