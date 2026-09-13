"""日志：控制台 + 滚动文件（data/logs/<app_slug>.log，5MB x 5）+ 内存环形缓冲（日志页 SSE）。

**日志是凭据外泄的主要通道之一，故本模块承担脱敏职责。** 本项目的一次真实泄漏事故里，
工作区被整包提交，日志文件随同入库；即便不发生提交事故，日志也会被用户导出、贴进 issue、
发给他人排障。而第三方库与应用自身都会在无意间把凭据写进日志行：

- `httpx` 在 INFO 级打印**完整请求 URL**——Steam Web API 把 key 放在查询串里
  （`?key=<32 位十六进制>`），于是每 60 秒的钱包轮询都会把密钥明文写进日志文件；
- `app.crawler.main` 会把 `proxy_url` 整条打出来，其中含 `user:pass@`；
- 汇率源 URL、代理订阅链接（vless 等协议的节点链接里嵌着 UUID）同理。

两道防线各修一半，缺一不可：

1. **源头静音**：`httpx`/`httpcore` 降到 WARNING。它们逐请求打一行 INFO 本身即纯噪音
   （应用自己有语义化日志），而这一行恰恰是密钥泄漏的实际来源。
2. **兜底脱敏** `redact()`：挂在每个 handler 上，按「保留字段名、抹掉值」改写日志行。
   静音只覆盖已知的两个库；应用自身或将来新引入的库仍可能打出凭据，过滤器是那层的兜网。

脱敏**只抹值不删行**：排障时需要知道「打过一次带 key 的请求」，不需要知道 key 是什么。
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
from collections import deque
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.app_info import APP_SLUG

_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"

# 日志噪声与泄漏源：httpx 逐请求打印完整 URL（含查询串里的密钥）。
# 降到 WARNING 后请求失败仍可见（httpx 的错误走 WARNING/ERROR），成功不再刷屏。
_NOISY_LOGGERS = ("httpx", "httpcore")

# ── 脱敏规则 ────────────────────────────────────────────────
# 全部按「保留字段名、抹掉值」替换：日志的可读性与排障价值留在字段名上。

# 代理链接：值本身就是凭据（vless/vmess 的 UUID 嵌在 URL 里，ss 整条是 base64）。
_PROXY_LINK = re.compile(
    r"(?i)\b(vless|vmess|trojan|ssr?|hysteria2?|tuic|wireguard)://([^\s\"'<>]+)"
)

# URL 用户信息：scheme://user:pass@host → 抹掉 user:pass，保留 host（host 用于判断打到哪）。
_URL_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.\-]*)://([^/\s:@]+):([^/\s@]+)@")

# 查询串/表单里的凭据参数。key 一项即 Steam Web API；sessionid/steamLogin* 是 Steam 会话 Cookie。
_QUERY_SECRET = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|auth[_-]?token|id[_-]?token"
    r"|token|secret|password|passwd|pwd|signature|sign|key|sessionid"
    r"|steamloginsecure|steamlogin|authorization)=([^&\s;\"'<>]+)"
)

# JSON 形态：`{"smtp.password": "…"}`。凭据配置在多处以此形态落库/落日志，
# 它没有 `=`，上面的查询串规则一条都匹配不到。键名要求**包含**凭据词根
# （`smtp.password`、`account.steam_cookies`），故裸 `key` 不在其中——
# 那会把 `{"key": "games"}` 这类正常调试输出一起抹掉，得不偿失。
_JSON_SECRET = re.compile(
    r'(?i)"([A-Za-z0-9_.\-]*(?:api[_-]?key|access[_-]?token|refresh[_-]?token'
    r'|auth[_-]?token|token|secret|password|passwd|pwd|signature|cookie|credential)'
    r'[A-Za-z0-9_.\-]*)"(\s*:\s*)"([^"]*)"'
)

# Authorization 头。
_AUTH_HEADER = re.compile(r"(?i)\b(bearer|basic)\s+([A-Za-z0-9._~+/=\-]{8,})")

# 环境变量式赋值：HOLDEXAR_SMTP_PASSWORD=xxx / STEAM_API_KEY=xxx。
# 要求变量名含凭据词根且全大写，避免误伤 `max_length=100` 这类普通赋值。
_ENV_ASSIGN = re.compile(
    r"\b([A-Z][A-Z0-9_]*"
    r"(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|PWD|COOKIE|CREDENTIAL|SIGNATURE)"
    r"[A-Z0-9_]*)(\s*=\s*)([^\s,;\"'<>]+)"
)

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (_PROXY_LINK, r"\1://***"),
    (_URL_USERINFO, r"\1://***:***@"),
    (_QUERY_SECRET, r"\1=***"),
    (_JSON_SECRET, r'"\1"\2"***"'),
    (_AUTH_HEADER, r"\1 ***"),
    (_ENV_ASSIGN, r"\1\2***"),
)

REDACTED = "***"


def redact(text: str) -> str:
    """抹掉一行文本里的凭据值，保留字段名。

    纯函数、无副作用，便于单测；幂等——已抹过的行再抹一次结果不变
    （替换后的 `***` 不再匹配任何规则）。
    """
    if not text:
        return text
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


class RedactFilter(logging.Filter):
    """把脱敏施加到每一条日志记录上。

    挂在 **handler** 而非 logger 上：`Logger.callHandlers()` 只遍历各级 logger 的
    handler，**不**调用祖先 logger 的 `filter()`——过滤器挂到 root logger 上对
    `httpx` 这类子 logger 发出的记录完全不起作用，这是本项目踩过的坑。
    改写 `record.msg` 并清空 `record.args` 后，各 handler 后续 `format()` 拿到的
    已是脱敏文本。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 —— 格式化失败（占位符与参数不匹配）不该让日志消失
            return True
        scrubbed = redact(message)
        if scrubbed != message:
            record.msg = scrubbed
            record.args = ()
        return True


class RingBufferLogHandler(logging.Handler):
    """内存环形缓冲：保留最近 capacity 行，供日志页拉取与 SSE 订阅。

    emit 可能在任意线程调用（爬虫线程/线程池），订阅者是 asyncio.Queue，
    故经 call_soon_threadsafe 投递到事件循环（SSE 端点首次订阅时注册 loop）。
    队列满（1000 行积压）直接丢弃——日志页只保证"尽量实时"，不背压。
    """

    def __init__(self, capacity: int = 2000) -> None:
        super().__init__()
        self._buf: deque[str] = deque(maxlen=capacity)
        self._subscribers: list[asyncio.Queue] = []
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self.setFormatter(logging.Formatter(_FMT))

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """SSE 端点在事件循环线程内调用一次；重复绑定无妨（uvicorn 单循环）。"""
        self._loop = loop

    def emit(self, record: logging.LogRecord) -> None:
        line = self.format(record)
        with self._lock:
            self._buf.append(line)
            subscribers = list(self._subscribers)
        loop = self._loop
        if loop is None or not subscribers:
            return
        for queue in subscribers:
            try:
                loop.call_soon_threadsafe(self._offer, queue, line)
            except RuntimeError:  # 循环已关闭（关闭阶段尾部日志）
                pass

    @staticmethod
    def _offer(queue: asyncio.Queue, line: str) -> None:
        if queue.full():
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        queue.put_nowait(line)

    def snapshot(self, limit: int = 200) -> list[str]:
        with self._lock:
            tail = list(self._buf)
        return tail[-limit:]

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=1000)
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(queue)
            except ValueError:
                pass


ring_log_handler = RingBufferLogHandler()


def setup_logging(data_dir: Path, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if root.handlers:
        return
    root.setLevel(level)
    fmt = logging.Formatter(_FMT)
    scrub = RedactFilter()

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(scrub)
    root.addHandler(console)

    logs_dir = Path(data_dir) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(
        logs_dir / f"{APP_SLUG}.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(scrub)
    root.addHandler(file_handler)

    ring_log_handler.addFilter(scrub)
    root.addHandler(ring_log_handler)

    # 源头静音放在最后：它只影响此后新取的 logger，与上面的 handler 装配互不依赖。
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
