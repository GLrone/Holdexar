"""日志：控制台 + 滚动文件（data/logs/<app_slug>.log，5MB x 5）+ 内存环形缓冲（日志页 SSE）。

行结构 `时间 [结果词] 模块中文名 · 人话主句 │ 键=值 键=值`：主句给人读，`│` 后的
结构化细节给排障读。调用方写 `log_event(...)` 给主句与细节，不再自己拼字符串——
拼接把「结论」和「参数」混成一句，日志页就成了开发者内部简写的堆砌。

`[结果词]` 默认由级别推出（INFO→信息 / WARNING→注意 / ERROR→失败），事件语义更精确
时用 `tag=` 显式覆盖（跳过 / 成功 / 已修复 …）。

**日志是凭据外泄的主要通道之一，故本模块承担脱敏职责。** 日志文件会被整包提交进
版本库、被用户导出、贴进 issue、发给他人排障，而第三方库与应用自身都会在无意间
把凭据写进日志行：

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
from collections.abc import Mapping
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.app_info import APP_SLUG

# 日志噪声与泄漏源：httpx 逐请求打印完整 URL（含查询串里的密钥）。
# 降到 WARNING 后请求失败仍可见（httpx 的错误走 WARNING/ERROR），成功不再刷屏。
#
# APScheduler 同理且量更大：每跑一个 job 打两行 INFO（Running job "_job_x
# (trigger: interval[0:05:00], next run at: ...)" / executed successfully），
# 占全量日志约一半，零业务信息，把真正的日志冲得看不见。它的失败仍走
# WARNING/ERROR（Job 抛异常、maximum number of running instances reached），不受影响。
_NOISY_LOGGERS = (
    "httpx",
    "httpcore",
    "apscheduler.executors.default",
    "apscheduler.scheduler",
)

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


# ── 人话渲染 ────────────────────────────────────────────────
# 结果词由级别兜底，事件语义更精确时调用方用 tag= 覆盖。
_RESULT_BY_LEVEL = {
    logging.DEBUG: "调试",
    logging.INFO: "信息",
    logging.WARNING: "注意",
    logging.ERROR: "失败",
    logging.CRITICAL: "严重",
}

# 结果词 → 严重度。**这是闭集**：`tag=` 只能取这里已有的键，新增词必须同步
# 前端日志页 `web/src/views/logs/Index.vue` 的分色与计数（依据键值，不靠猜词根）。
# 行首只印一个词，级别信息由这张表承载——所以「降级」「未完成」尽管是 tag，
# 也仍然按注意级呈现，不会因为盖掉级别词而丢掉严重度。
TAG_SEVERITY = {
    "调试": "info",
    "信息": "info",
    "成功": "info",
    "跳过": "info",
    "已修复": "info",
    "忽略": "info",
    "复用": "info",
    "注意": "warning",
    "降级": "warning",
    "未完成": "warning",
    "失败": "error",
    "严重": "error",
}

# 点分模块路径 → 用户可读名。日志页印 `app.domains.proxypool.health` 对读者是零信息，
# 印「代理池·健康检查」才说明白这条日志属于谁。映射是 1:1 的，故排障时按名反查模块无歧义。
# 与源码同步的守卫见 tests/test_logging_redact.py（新增日志模块漏登记会测试失败）。
_MODULE_LABELS = {
    # 内核
    "app.main": "应用启动",
    "app.core.scheduler": "定时任务",
    "app.core.database": "数据库",
    "app.core.backup": "数据备份",
    "app.core.updater": "应用更新",
    "app.core.rekey": "密钥换装",
    "app.core.external_time": "时间校准",
    "app.core.seed_assets": "种子数据",
    "app.core.seed_fetch": "种子拉取",
    "app.core.data_export": "数据导出",
    "app.core.orchestration": "编排事件",
    "app.core.logging": "日志",
    # 爬取
    "app.crawler.scheduler": "爬取调度",
    "app.crawler.runner": "爬取执行",
    "app.crawler.main": "爬取·命令行",
    "app.crawler.router": "爬取·任务路由",
    "app.crawler.browse_store": "爬取·商店页",
    "app.crawler.epic_free": "爬取·Epic 免费",
    "app.crawler.db_writer": "爬取·入库",
    "app.crawler.network_check": "爬取·网络自检",
    "app.crawler.rate_limit": "爬取·出网限流",
    "app.crawler.http_client": "爬取·HTTP 客户端",
    "app.crawler.cdk_fetcher": "爬取·CDK 查价",
    "app.crawler.tag_names": "爬取·标签名",
    # 价格刷新
    "app.domains.crawl.service": "价格刷新·周期链",
    "app.domains.crawl.cycle_run": "价格刷新·周期执行",
    "app.domains.crawl.cycle": "价格刷新·周期",
    "app.domains.crawl.router": "价格刷新·接口",
    # 代理池
    "app.domains.proxypool.health": "代理池·健康检查",
    "app.domains.proxypool.runtime": "代理池·运行时",
    "app.domains.proxypool.bootstrap": "代理池·启动装配",
    "app.domains.proxypool.scheduling": "代理池·调度",
    "app.domains.proxypool.admission": "代理池·准入",
    "app.domains.proxypool.retention": "代理池·清理",
    "app.domains.proxypool.exitstats": "代理池·出口统计",
    "app.domains.proxypool.jobruns": "代理池·作业记录",
    # 代理订阅与内核
    "app.domains.proxies.clash_manager": "代理内核",
    "app.domains.proxies.service": "代理订阅",
    "app.domains.proxies.router": "代理接口",
    "app.domains.proxies.subscription_secret": "代理订阅·密钥",
    # 账户与资产
    "app.domains.account.service": "Steam 账号",
    "app.domains.account.login": "Steam 登录",
    "app.domains.account.steam_wallet": "Steam 钱包",
    "app.domains.family.service": "Steam 家庭",
    "app.domains.wishlist.service": "愿望单同步",
    "app.domains.achievements.service": "成就同步",
    "app.domains.steam_events.service": "Steam 活动",
    # 价格与账单
    "app.domains.bills.service": "账单同步",
    "app.domains.bills.steam_fetch": "账单抓取",
    "app.domains.games.boards": "榜单同步",
    "app.domains.games.service": "游戏库",
    "app.domains.games.series": "游戏系列",
    "app.domains.metadata.service": "游戏资料",
    "app.domains.bundles.refresh": "捆绑包刷新",
    "app.domains.bundles.service": "捆绑包列表",
    "app.domains.rates.service": "汇率刷新",
    "app.domains.rates.history": "汇率历史",
    "app.domains.rates.snapshot": "汇率快照",
    "app.domains.rates.providers.bing_currency": "汇率源·Bing",
    "app.domains.alerts.service": "价格提醒",
    "app.domains.alerts.notify": "提醒推送",
    "app.domains.notifications.service": "通知中心",
    "app.domains.notifications.facts": "通知·事实",
    "app.domains.redeem.service": "兑换",
    "app.domains.regions.service": "区域",
    # 运行账本与领航员
    "app.domains.agent.service": "运行账本",
    "app.domains.agent.runtime.task_runner": "运行账本·任务执行",
    "app.domains.agent.runtime.scheduler_bridge": "运行账本·调度桥",
    "app.domains.agent.runtime.executor": "运行账本·工具执行",
    "app.domains.agent.runtime.fake": "运行账本·模拟运行",
    "app.domains.agent.providers.registry": "运行账本·模型来源",
    "app.domains.pilot.service": "领航员·编排",
    "app.domains.pilot.session": "领航员·会话",
    "app.domains.pilot.router": "领航员·接口",
    "app.domains.pilot.llm": "领航员·模型",
}

# 三级域兜底：未登记的中文名按 `app.<层>.<名>` 推 `层·名`，至少不让点分路径漏给用户。
_LAYER_LABELS = {"core": "内核", "crawler": "爬取", "domains": "业务", "providers": "数据源"}
_DROPPED_SEGMENTS = {"service", "router", "models", "schemas", "main", "__init__"}


def module_label(name: str) -> str:
    """模块路径 → 用户可读名；未登记的按结构兜底，绝不返回点分路径。"""
    label = _MODULE_LABELS.get(name)
    if label is not None:
        return label
    parts = [p for p in name.split(".") if p != "app"]
    parts = [p for p in parts if p not in _DROPPED_SEGMENTS] or parts
    if parts:
        parts[0] = _LAYER_LABELS.get(parts[0], parts[0])
    return "·".join(parts) or name


def result_tag(record: logging.LogRecord) -> str:
    """行首结果词：调用方 tag= 优先，否则按级别兜底。"""
    return str(getattr(record, "hl_tag", None) or _RESULT_BY_LEVEL.get(record.levelno, "信息"))


class HumanFormatter(logging.Formatter):
    """`时间 [结果词] 模块中文名 · 主句 │ 键=值 键=值`。

    文本 handler 用 `datefmt="%H:%M:%S"`（日志页要的是当下读到什么，日期由文件日志承担）；
    文件 handler 留默认日期。脱敏在**渲染之后**再走一次 `redact()`：`hl_detail` 的值
    不经过 `RedactFilter`（它只看 `record.getMessage()`），订阅链接这类凭据正是从这里漏的。
    """

    def format(self, record: logging.LogRecord) -> str:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 —— 占位符与参数不匹配时保住这条日志
            message = str(record.msg)
        line = f"{self.formatTime(record, self.datefmt)} [{result_tag(record)}] {module_label(record.name)} · {message}"
        detail = getattr(record, "hl_detail", None)
        if detail:
            line += " │ " + " ".join(f"{k}={v}" for k, v in detail.items())
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            line += "\n" + record.exc_text
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return redact(line)


def log_event(
    logger: logging.Logger,
    message: str,
    *,
    tag: str | None = None,
    detail: Mapping[str, object] | None = None,
    level: int = logging.INFO,
    exc_info: bool = False,
) -> None:
    """一条人话日志：主句写结论，`detail` 放排障要看的键值。

    主句里不要再拼状态枚举与模块黑话（`[L0恢复]`、`frozen`、`missing`）——那些
    要么翻成人话写进主句，要么作为键值进 `detail`。
    """
    logger.log(level, message, extra={"hl_tag": tag, "hl_detail": detail or None}, exc_info=exc_info)


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
        self.setFormatter(HumanFormatter(datefmt="%H:%M:%S"))

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
    # 控制台与日志页要「当下读到什么」，只留时分秒；文件是归档与贴 issue 的凭据，留完整日期。
    live_fmt = HumanFormatter(datefmt="%H:%M:%S")
    file_fmt = HumanFormatter()
    scrub = RedactFilter()

    console = logging.StreamHandler()
    console.setFormatter(live_fmt)
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
    file_handler.setFormatter(file_fmt)
    file_handler.addFilter(scrub)
    root.addHandler(file_handler)

    ring_log_handler.addFilter(scrub)
    root.addHandler(ring_log_handler)

    # 源头静音放在最后：它只影响此后新取的 logger，与上面的 handler 装配互不依赖。
    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
