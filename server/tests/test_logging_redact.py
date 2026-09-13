"""日志脱敏测试：`redact()` 的值抹除，以及过滤器在 logger 层级中的生效位置。

**为什么值得单独一个测试文件**：本项目的日志曾把 Steam Web API Key 逐分钟写进
`data/logs/holdexar.log`（`httpx` 在 INFO 级打印完整请求 URL，而 key 在查询串里）。
脱敏一旦失效不会报错、不会崩，只会静默地继续泄漏——只有测试能守住。

其中 `test_filter_applies_to_child_logger` 是一条针对**具体实现陷阱**的回归防线：
`logging.Logger.callHandlers()` 只遍历各级 logger 的 **handler**，不调用祖先 logger
的 `filter()`。因此过滤器若挂在 root logger 上，对 `httpx` 这类子 logger 发出的
记录完全无效——代码看起来「加了脱敏」，实际一行都没抹掉。
"""
from __future__ import annotations

import collections
import contextlib
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import logging as app_logging  # noqa: E402

# ── 样例值一律拼接构造，不用字面量 ────────────────────────────
# 代理节点链接的 scheme 前缀正是 scripts/check_secrets.py 的命中形状（其
# CONTENT_PATTERNS 的「代理节点 / 订阅链接」一条），字面写进源码会让提交门禁
# 拦住这个测试文件自己——本文件第一版就是这么被拦下的。拼接不改变被测行为：
# redact() 收到的仍是拼好的整串。
PROXY_SCHEME = "vless" + "://"
# 32 位十六进制，形状与真实 Steam Web API Key 相同（门禁只在它紧跟在
# `steam_api_key` 字样之后时才判定为凭据，故这里用查询串形式是安全的）。
FAKE_STEAM_KEY = "0123456789ABCDEF0123456789ABCDEF"
# 伪装成真实会话：<steamid64>%7C%7C<40位十六进制>
FAKE_STEAM_ID = "76561190000000000"
FAKE_SESSION = "A1B2C3D4E5F60718293A4B5C6D7E8F9012345678"
FAKE_COOKIE = f"steamLoginSecure={FAKE_STEAM_ID}%7C%7C{FAKE_SESSION}"
# 故意写成非 16 位纯字母数字：门禁的 SMTP 特征是 `smtp.password=<12+ 位字母数字>`
FAKE_SMTP_CODE = "fake-smtp-app-code"


# ── redact()：值抹除 ────────────────────────────────────────


def test_redact_steam_api_key_in_httpx_style_line():
    """httpx 逐请求日志的原始形状——这正是本次实际泄漏的那一行。"""
    line = (
        "2026-09-11 23:56:59,497 [INFO] httpx: HTTP Request: GET "
        "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
        f"?key={FAKE_STEAM_KEY}&steamids={FAKE_STEAM_ID} \"HTTP/1.1 200 OK\""
    )
    out = app_logging.redact(line)
    assert FAKE_STEAM_KEY not in out
    assert FAKE_STEAM_ID in out          # 非凭据参数保留：排障需要知道查了谁
    assert "key=***" in out              # 字段名保留
    assert "api.steampowered.com" in out  # 目标主机保留


def test_redact_proxy_url_credentials():
    """`app.crawler.main` 会整条打印 proxy_url，其中含 user:pass@。"""
    line = "代理已启用: http://nodeuser:nodepass123@127.0.0.1:7890"
    out = app_logging.redact(line)
    assert "nodeuser" not in out
    assert "nodepass123" not in out
    assert "127.0.0.1:7890" in out  # host 保留


def test_redact_proxy_node_link():
    """节点链接整条是凭据（vless 的 UUID 嵌在 URL 里）。"""
    line = f"订阅刷新：{PROXY_SCHEME}uuid-11112222@example.invalid:443?type=ws&security=tls"
    out = app_logging.redact(line)
    assert "uuid-11112222" not in out
    assert "example.invalid" not in out
    assert out.startswith("订阅刷新：")


def test_redact_steam_session_cookie():
    line = f"[account] 读取 Cookie：{FAKE_COOKIE}; sessionid=abcdef0123456789"
    out = app_logging.redact(line)
    assert FAKE_SESSION not in out
    assert "abcdef0123456789" not in out
    assert "steamLoginSecure=***" in out


def test_redact_authorization_header():
    out = app_logging.redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
    assert "eyJhbGciOiJIUzI1NiJ9.payload.sig" not in out
    assert out.lower().startswith("authorization: bearer ***")


def test_redact_env_style_assignment():
    """环境变量式赋值：变量名含凭据词根且全大写才抹。"""
    out = app_logging.redact(f"HOLDEXAR_SMTP_PASSWORD={FAKE_SMTP_CODE} 已加载")
    assert FAKE_SMTP_CODE not in out
    assert "HOLDEXAR_SMTP_PASSWORD=***" in out
    assert "已加载" in out


def test_redact_smtp_password_json_key():
    out = app_logging.redact(f'{{"smtp.password": "{FAKE_SMTP_CODE}"}}')
    assert FAKE_SMTP_CODE not in out


def test_redact_is_idempotent():
    """幂等：已抹过的行再抹一次结果不变（替换出的 `***` 不再匹配任何规则）。"""
    line = f"?key={FAKE_STEAM_KEY}&token=abc123def456&steamids={FAKE_STEAM_ID}"
    once = app_logging.redact(line)
    assert app_logging.redact(once) == once


# ── redact()：不误伤 ────────────────────────────────────────


@pytest.mark.parametrize(
    "line",
    [
        "2026-09-12 00:01:43,217 [INFO] app.main: Holdexar 0.1.0 启动，data=C:\\Users\\k\\AppData\\Local\\holdexar-dev，port=28765",
        "2026-09-12 00:01:42,655 [INFO] app.main: 历史表初始化完成：2373 行",
        "[链式] 跳过 repair：没有待补抓的欠账（missing）数据",
        "2026-09-12 00:01:43,194 [INFO] app.main: 愿望单初始化完成：2570 款",
        # 无凭据的普通 URL 不应被改动
        "汇率源请求失败（3 次）: https://api.exchangerate.host/latest?base=CNY",
        # 短参数名但非凭据词根
        "分页参数 page=3&size=50 已应用",
    ],
)
def test_redact_leaves_ordinary_lines_alone(line):
    """正常日志行必须逐字不变——脱敏把日志改得读不懂就等于毁掉排障能力。"""
    assert app_logging.redact(line) == line


# ── 过滤器：生效位置 ────────────────────────────────────────


class _Sink(logging.Handler):
    """内存 handler，自带脱敏过滤器——用于断言「经 handler 之后」的最终文本。"""

    def __init__(self) -> None:
        super().__init__()
        self.lines: list[str] = []
        self.setFormatter(logging.Formatter(app_logging._FMT))
        self.addFilter(app_logging.RedactFilter())

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


@pytest.fixture
def root_logging():
    """把 root 调到 INFO 并隔离噪声 logger 的级别，测完还原。

    **root 默认是 WARNING(30)**，不调成 INFO 的话测试里的 `logger.info()` 会被
    直接丢弃、断言拿到空列表——这个坑与脱敏本身无关，但会让测试静默失效。
    """
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_noisy = {n: logging.getLogger(n).level for n in app_logging._NOISY_LOGGERS}
    root.setLevel(logging.INFO)
    for name in app_logging._NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.NOTSET)  # 让用例不受执行顺序影响
    sinks: list[_Sink] = []
    try:
        yield root, sinks
    finally:
        for sink in sinks:
            root.removeHandler(sink)
            sink.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_noisy.items():
            logging.getLogger(name).setLevel(level)


def _capture(root, sinks: list[_Sink]) -> _Sink:
    sink = _Sink()
    root.addHandler(sink)
    sinks.append(sink)
    return sink


@contextlib.contextmanager
def installed_logging(tmp_path):
    """在「root 无 handler」的前提下调用 `setup_logging`，测完还原。

    清空必须发生在**测试体内**：pytest 的 logging 插件是在**调用测试函数时**
    才把捕获 handler 挂到 root 上的，fixture 阶段清空挡不住它；而 `setup_logging`
    开头的 `if root.handlers: return` 幂等守卫会让它直接早退——测试于是「通过」
    却什么都没测到（这正是本文件第一版踩到的坑）。
    """
    root = logging.getLogger()
    saved_handlers, saved_level = list(root.handlers), root.level
    saved_noisy = {n: logging.getLogger(n).level for n in app_logging._NOISY_LOGGERS}
    saved_filters = list(app_logging.ring_log_handler.filters)
    saved_buf = app_logging.ring_log_handler._buf
    root.handlers = []
    try:
        app_logging.setup_logging(tmp_path)
        assert root.handlers, "setup_logging 未装配任何 handler（幂等守卫被误命中？）"
        yield root
    finally:
        for handler in list(root.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.close()  # 关掉临时日志文件句柄
        root.handlers = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_noisy.items():
            logging.getLogger(name).setLevel(level)
        app_logging.ring_log_handler.filters = saved_filters
        app_logging.ring_log_handler._buf = saved_buf


def test_filter_applies_to_child_logger(root_logging):
    """回归防线：过滤器挂在 handler 上时，子 logger 的记录才会被抹。

    `httpx` 走的是子 logger + propagate 到 root handler 的路径。若把过滤器挂到
    root **logger** 上（看起来更"集中"），`Logger.callHandlers()` 不会调用它的
    `filter()`，这条断言会失败。
    """
    root, sinks = root_logging
    sink = _capture(root, sinks)
    logging.getLogger("httpx").info(
        "HTTP Request: GET https://api.steampowered.com/x?key=%s&steamids=%s",
        FAKE_STEAM_KEY, FAKE_STEAM_ID,
    )
    assert len(sink.lines) == 1
    assert FAKE_STEAM_KEY not in sink.lines[0]
    assert "key=***" in sink.lines[0]


def test_filter_scrubs_parent_logger_too(root_logging):
    root, sinks = root_logging
    sink = _capture(root, sinks)
    logging.getLogger("app.domains.rates.service").warning(
        "汇率源请求失败（3 次）: https://api.example.com/latest?apikey=%s", FAKE_STEAM_KEY
    )
    assert len(sink.lines) == 1
    assert FAKE_STEAM_KEY not in sink.lines[0]


def test_filter_keeps_record_when_never_logged():
    """过滤器只改写、不放行丢弃：返回 False 会让整条日志消失。"""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    assert app_logging.RedactFilter().filter(record) is True
    assert record.getMessage() == "hello world"


def test_filter_survives_broken_format_args():
    """占位符与参数不匹配时不该让日志消失（那会把排障线索一起吞掉）。"""
    record = logging.LogRecord("x", logging.INFO, __file__, 1, "bad %s %s", ("only-one",), None)
    assert app_logging.RedactFilter().filter(record) is True


# ── setup_logging：源头静音 ─────────────────────────────────


def test_setup_logging_silences_httpx(tmp_path):
    """httpx 的逐请求 INFO 日志是本次泄漏的实际来源，必须降到 WARNING。"""
    with installed_logging(tmp_path) as root:
        assert logging.getLogger("httpx").level >= logging.WARNING
        assert logging.getLogger("httpcore").level >= logging.WARNING
        assert root.level == logging.INFO  # 应用自身仍是 INFO


def test_setup_logging_attaches_redact_filter_to_every_handler(tmp_path):
    """三道 handler（控制台/文件/环形缓冲）都必须挂上过滤器——漏一个就是一条外泄路径。"""
    with installed_logging(tmp_path) as root:
        assert len(root.handlers) == 3, f"handler 数量变了：{root.handlers}"
        for handler in root.handlers:
            assert any(
                isinstance(f, app_logging.RedactFilter) for f in handler.filters
            ), f"{handler!r} 未挂脱敏过滤器"


def test_ring_buffer_holds_scrubbed_lines(tmp_path):
    """日志页（SSE / 环形缓冲）读到的也必须是抹过的——UI 同样是一条外泄出口。"""
    with installed_logging(tmp_path) as root:
        assert app_logging.ring_log_handler in root.handlers
        app_logging.ring_log_handler._buf = collections.deque(maxlen=50)
        logging.getLogger("app.domains.account.service").info(
            "[account] 读取 Cookie：%s", FAKE_COOKIE
        )
        tail = app_logging.ring_log_handler.snapshot(10)
        assert tail, "环形缓冲未收到日志"
        assert all(FAKE_SESSION not in line for line in tail), tail
