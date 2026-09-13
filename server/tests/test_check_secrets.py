"""敏感面门禁（scripts/check_secrets.py）的行为测试。

**本文件里所有假凭据都必须由字符串拼接构造，不能写成字面量。**
原因：门禁的最后一条测试 `test_whole_tree_is_clean` 会扫描整个已跟踪树，
包括本文件。写成连续字面量的话，本文件会命中自己的特征，于是门禁永远失败。
这反过来正好证明了门禁是「按值的形状」而不是「按关键词」在判定——
关键词式门禁写不出这样的自洽测试。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))  # 与 conftest 一致，供 app.* 导入

_spec = importlib.util.spec_from_file_location(
    "check_secrets", ROOT / "scripts" / "check_secrets.py"
)
assert _spec and _spec.loader
check_secrets = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_secrets)


def labels(findings) -> list[str]:
    return [f.label for f in findings]


def scan_one(line: str):
    return check_secrets.scan_lines("probe.py", [(1, line)])


# ── 内容层：必须命中 ──


def test_detects_steam_session_cookie():
    # 真实形状：<steamid64>%7C%7C<40 位十六进制>
    line = f"cookies = \"{76561198000000000}{'%7C'}{'%7C'}{'a' * 40}\""
    assert labels(scan_one(line)) == ["Steam 登录会话 cookie"]


def test_detects_steam_api_key():
    line = f'{{"account.steam_api_key": "{"0" * 32}"}}'
    assert labels(scan_one(line)) == ["Steam Web API Key（32 位十六进制）"]


def test_detects_smtp_password():
    line = '{' + '"smtp.password"' + ': "' + "abcdefghijklmnop" + '"}'
    assert labels(scan_one(line)) == ["SMTP 授权码"]


def test_detects_proxy_subscription_url():
    line = f"url = {'vless'}://{'x' * 24}@host:443"
    assert labels(scan_one(line)) == ["代理节点 / 订阅链接"]


def test_detects_private_key():
    line = "-----BEGIN " + "RSA PRIVATE KEY" + "-----"
    assert labels(scan_one(line)) == ["私钥"]


def test_detects_db_dsn_with_password():
    line = "postgresql://user:" + "hunter2xyz" + "@localhost:5432/db"
    assert labels(scan_one(line)) == ["含密码的数据库连接串"]


def test_detects_github_token():
    line = f"token = {'ghp_ ' .strip()}{'A' * 36}"
    assert labels(scan_one(line)) == ["GitHub 令牌"]


# ── 内容层：必须放过（这些是实测踩到过的误报，每条都对应一个真实文件）──


@pytest.mark.parametrize(
    "line",
    [
        # server/tests/test_account_wallet.py:137 —— 合成夹具，%7C%7C 后不是十六进制
        '("steamLoginSecure=76561198123456789%7C%7Ctoken", "76561198123456789")',
        # server/tests/test_account_wallet.py:216 —— 同上，单字符后缀
        '"steamLoginSecure=76561198123456789%7C%7Ct"',
    ],
)
def test_allows_synthetic_steam_cookie_fixtures(line):
    assert scan_one(line) == []


@pytest.mark.parametrize(
    "line",
    [
        # 变量名/配置键的裸提及，共 18 / 9 / 2 个合法源码文件
        "STEAM_LOGIN_SECURE = 'steamLoginSecure'",
        "key = 'account.steam_api_key'",
        "await set_value('smtp.password', value)",
        # server/tests/test_notify_test_mail.py:52 —— 值太短且含连字符
        'return {"smtp.password": "saved-pass"}.get(key, default)',
        # scripts/fx_maintenance.py:70 —— 中文占位符，非真实密码
        '"postgresql://postgres:<本地旧库密码>@localhost:5432/holdexar_archive",',
        # desktop/main.py:8 —— 文档里的用法示例，不是凭据
        "HOLDEXAR_DEV_URL=http://localhost:5173 python desktop/main.py",
    ],
)
def test_allows_bare_identifier_and_placeholder_mentions(line):
    assert scan_one(line) == []


# ── 路径层 ──


@pytest.mark.parametrize(
    "path",
    [
        "data/holdexar.db",
        "data.old/notes.json",
        "release/Holdexar-win64.zip",
        "reference/参考脚本s/x.txt",
        "secrets/fx_maintenance.env",
        "assets/seed/holdexar_seed.db",
        ".tmp-pytest/x/test.db",
        "gui-test-screenshots/a.png",
        "server/data/holdexar.db-wal",  # 嵌套位置：由扩展名规则兜住
        "backup/creds.pem",
        ".env.local",
        "mail.ini",
    ],
)
def test_path_deny_matches(path):
    assert check_secrets.check_path(path), f"应被拒绝：{path}"


@pytest.mark.parametrize(
    "path",
    [
        # 回归：曾把「任意深度的 logs/release 目录名」写成规则，误伤了这三类
        "web/src/views/logs/Index.vue",
        "server/app/domains/release/router.py",
        "web/src/views/data/Index.vue",
        # 其余正常源码
        "scripts/check_secrets.py",
        "server/app/core/paths.py",
        "web/scripts/check-routes.mjs",
        "docs/DESIGN_SYSTEM_PLAN.md",
        "dev.bat",
        "start.bat",
    ],
)
def test_path_deny_does_not_match_business_paths(path):
    assert not check_secrets.check_path(path), f"不应被拒绝：{path}"


def test_detects_oversize_file():
    assert check_secrets.check_size("blob.bin", check_secrets.MAX_FILE_BYTES + 1)


def test_allows_normal_file_size():
    assert not check_secrets.check_size("small.py", 4096)


# ── 掩码：门禁自身不得成为泄漏路径 ──


def test_mask_never_reveals_the_value():
    secret = "76561198000000000" + "%7C" + "%7C" + "b" * 40
    masked = check_secrets._mask(secret)
    assert secret not in masked
    assert secret[4:] not in masked  # 除首 4 字符外一律不出现
    assert str(len(secret)) in masked  # 长度信息保留，便于判断是否完整凭据


# ── 全树自检：任何新增特征若误伤既有代码，在这里立刻暴露 ──


def test_whole_tree_is_clean():
    """当前仓库已跟踪内容必须零命中。

    这条测试的意义不在「现在通过」，而在「以后不通过」：任何一次往
    CONTENT_PATTERNS / PATH_DENY 里加特征的动作，只要误伤了已有代码，
    CI 会立刻失败——把误报挡在门禁上线之前，而不是等它拦住一次真实提交。
    """
    findings, files = check_secrets.whole_tree()
    assert len(files) > 100, "未取到已跟踪文件，测试环境异常"
    assert not findings, "命中：\n" + "\n".join(str(f) for f in findings)
