"""测试共享夹具与 sys.path 注入。"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 本机开着系统代理时，httpx 的 trust_env 会把**环回地址**的请求也交给
# 代理（httpx 不读注册表代理的「绕过本地」名单）→ 内核 controller 探测
# 拿到代理的 502，整批内核用例假失败。测试进程一律直连。
os.environ.setdefault("NO_PROXY", "*")
os.environ.setdefault("no_proxy", "*")
for _proxy_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                   "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_proxy_key, None)


@pytest.fixture(autouse=True)
def _sanitize_db_lru_caches():
    """检测并清除 get_engine / get_session_factory 的 lru_cache 毒化。

    防跨文件假失败：若干 tmp 库夹具只 monkeypatch 模块属性，而域模块
    持有原函数对象的直接引用（顶部 `from app.core.database import
    get_session_factory` 的那一批）——patch 期间任何此类模块被触及（
    init_db 的建表/域种子链就会触及），原函数经模块属性查到 tmp
    get_engine，把 **tmp 工厂写进原函数的 lru_cache**；属性还原后全进程
    仍读 tmp 空库，后续真实库用例成片假失败（games 列表全空）。

    setup 阶段先抓原函数与期望库地址（此刻必未被 patch）；teardown 只在
    「缓存工厂指向的库 ≠ 当前配置库」时清——正常用例零副作用（避免无谓
    的 engine 重建与 GC 噪声），不依赖与各测试 monkeypatch 还原的先后。
    """
    from app.core import database as database_module

    orig_engine = database_module.get_engine
    orig_factory = database_module.get_session_factory
    # 无条件清：条件式判断（比 bind 与当前配置）在夹具还原次序下会漏判——
    # 「patch 期间被惰性导入的模块把 tmp 工厂捕获为自己的直接引用」正是它漏掉
    # 的形态，漏判后果是整批用例静默读写上一个用例的库。清缓存的代价（每用例
    # 重建一个引擎对象）远低于假失败排查成本。
    orig_engine.cache_clear()
    orig_factory.cache_clear()
    database_module.get_settings.cache_clear()
    yield
    orig_engine.cache_clear()
    orig_factory.cache_clear()
    # settings 缓存同样会跨文件外溢（夹具在环境还原前清过、随后又被读回）
    database_module.get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _block_outbound_smtp(monkeypatch):
    """测试进程绝不发真实邮件：SMTP 传输层统一换成「只记录、不开连接」的假件。

    邮件段挂在多个调度 job 的链尾（价格网格 / Epic 喜加一 / HB 当月包 /
    备份失败 / 爬取失败告警），这些 job 会被测试直接调用；只要有一处忘了给
    alerts 域打桩，本地开发库里**真实可用**的 SMTP 配置就会把邮件发到用户
    邮箱——测试进程发信是必须从结构上杜绝的事故。

    桩打在传输层而不是各个调用点：新增邮件类型不必再逐个测试补桩，漏桩的
    最坏后果也只是「测试里发不出去」。需要断言邮件内容的用例
    （test_alert_mails / test_notify_test_mail）自行 patch notify.smtplib
    的两个类，函数级 patch 晚于本桩生效、覆盖本桩。
    """
    from app.domains.alerts import notify

    class _NoNetworkSMTP:
        """与 smtplib.SMTP* 同形的最小假件：不解析网络、不发任何字节。"""

        def __init__(self, host, port, timeout=0):
            self.host, self.port = host, port

        def login(self, user, password):
            return None

        def starttls(self):
            return None

        def sendmail(self, from_addr, to_addrs, msg_string):
            return None

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", _NoNetworkSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP", _NoNetworkSMTP)


# 本机私密配置（secrets/fx_maintenance.env，gitignored）：只设未存在的变量。
# 真实样本文件名/外部档案路径经环境变量注入（git 里只有合成默认值）。
_env_file = Path(__file__).resolve().parents[2] / "secrets" / "fx_maintenance.env"
if _env_file.is_file():
    for _line in _env_file.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _key, _, _value = _line.partition("=")
        os.environ.setdefault(_key.strip(), _value.strip())


@pytest.fixture(autouse=True)
def _db_caches_fresh_before_each(_sanitize_db_lru_caches):
    """开测前再清一次，与 `_sanitize_db_lru_caches` 同源：两级净化都无条件执行，
    覆盖「惰性导入模块捕获旧工厂」这类判不出、只能清干净的形态。"""
    from app.core import database as database_module

    database_module.get_engine.cache_clear()
    database_module.get_session_factory.cache_clear()
    database_module.get_settings.cache_clear()


def _scan_module_level_factory_importers() -> dict[str, tuple[str, ...]]:
    """扫描模块级 `from app.core.database import ...` 绑定了 get_session_factory /
    get_engine 的模块（顶格导入在 import 时绑定原函数对象；函数内导入调用时解析、
    不受影响）。tests 目录无包结构，模块名取文件名。"""
    import re as _re

    server_root = Path(__file__).resolve().parents[1]
    pattern = _re.compile(r"^from app\.core\.database import ([^\n(]+)$", _re.M)
    targets = ("get_session_factory", "get_engine")
    hits: dict[str, tuple[str, ...]] = {}
    for scan_root, dotted in (
        (server_root / "app", True),
        (server_root / "tests", False),
    ):
        for path in sorted(scan_root.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            found = {
                name
                for match in pattern.finditer(text)
                for name in targets
                if name in match.group(1)
            }
            if found:
                if dotted:
                    rel = path.relative_to(server_root).with_suffix("")
                    key = str(rel).replace(os.sep, ".")
                else:
                    key = path.stem
                hits[key] = tuple(sorted(found))
    return hits


_FACTORY_IMPORTERS = _scan_module_level_factory_importers()

# 模块导入时刻的原始工厂函数：夹具 setup 时 database_module 可能已被本用例
# 更早的夹具 patch，不能用「与当前 database_module 属性同对象」做判定
from app.core import database as _database_import_time  # noqa: E402

_ORIG_SESSION_FACTORY = _database_import_time.get_session_factory
_ORIG_ENGINE = _database_import_time.get_engine


class _ForwardedFactory:
    """database 模块工厂的动态转发：调用时解析 database 模块当前属性，测试
    patch 一处全域生效。cache_clear 转发到当前目标（目标非 lru 函数时安全
    no-op）——兼容测试夹具对 from-import 对象直接调 cache_clear 的既有写法。"""

    def __init__(self, database_module, name):
        self._dm = database_module
        self._name = name

    def __call__(self, *args, **kwargs):
        return getattr(self._dm, self._name)(*args, **kwargs)

    @property
    def cache_clear(self):
        return getattr(getattr(self._dm, self._name), "cache_clear", lambda: None)


@pytest.fixture(autouse=True)
def _forward_session_factory_imports(monkeypatch):
    """模块级 from-import 绑定的工厂函数统一转发到 database 模块当前值。

    30+ 域模块（与部分测试文件）顶部 `from app.core.database import
    get_session_factory` 在 import 时绑定原函数，测试对 database_module 的
    monkeypatch 拦不住这些本地引用——跨域调用点（settings.get_value 等）把
    读写漏进隔离空库，「no such table」成片假失败；测试文件自身的 from-import
    则依赖前序用例毒化 lru_cache 才偶然拿到测试库（跨文件污染判例同源）。

    本夹具把全部此类引用改写为转发：测试 patch database_module 一处即全域
    生效；测试自行 patch 某域模块属性时 setattr 晚于本夹具、正常覆盖；
    monkeypatch 后进先出的还原顺序保证撤销后回到转发形态。
    """
    import importlib

    from app.core import database as database_module

    orig = {
        "get_session_factory": _ORIG_SESSION_FACTORY,
        "get_engine": _ORIG_ENGINE,
    }
    for name, names in _FACTORY_IMPORTERS.items():
        module = importlib.import_module(name)
        for attr in names:
            current = getattr(module, attr, None)
            if isinstance(current, _ForwardedFactory):
                continue
            if current is not orig[attr]:
                continue  # 已被更早的夹具/测试换成桩，尊重它
            monkeypatch.setattr(module, attr, _ForwardedFactory(database_module, attr))


@pytest.fixture(autouse=True)
def _reset_crawl_control_globals():
    """爬取控制面的进程级全局（占用持有者 / 活动任务 / 链级停止位 / 主轮
    busy 标志）逐用例复位：它们不随事件循环销毁，跨文件泄漏会让后续文件
    的启动类用例撞上幽灵占用。"""
    yield
    from app.crawler import occupancy as occupancy_mod
    from app.domains.crawl import service as crawl_service
    from app.core import scheduler as sched_mod

    occupancy_mod.end_crawl()
    occupancy_mod.end_maintenance()
    crawl_service._active = None
    if crawl_service._chain_stop is not None:
        crawl_service.unregister_chain_stop(crawl_service._chain_stop)
    sched_mod._price_cycle_busy = False


# ── 会话级模板库：一个完整数据库，逐用例克隆 ────────────────────────
# 模板 = 真实 init_db() 产物（create_all + 迁移链 + 区服/汇率/标签种子），
# 与生产新装库同构，每会话只构建一次；每个用例复制文件得到完整空库，
# 建库成本从逐用例 ~330ms 降到文件复制 ~ms。迁移/建表/加列的耦合语义
# 仍由 test_schema_migrations 走真实 init_db 链路专项覆盖。
_TEMPLATE: dict = {"db_file": None, "dir": None}
_ISOLATED_DIRS: list[str] = []


def _get_template_db_file():
    """返回模板库文件路径；首次调用时以真实 init_db 构建。"""
    if _TEMPLATE["db_file"] is not None:
        return _TEMPLATE["db_file"]
    import asyncio
    import sqlite3
    import tempfile

    from app.core.database import init_db
    import app.core.database as database_module

    tpl_dir = Path(tempfile.mkdtemp(prefix="holdexar-test-template-"))
    old = os.environ.get("HOLDEXAR_DATA_DIR")
    os.environ["HOLDEXAR_DATA_DIR"] = str(tpl_dir)
    try:
        database_module.get_engine.cache_clear()
        database_module.get_session_factory.cache_clear()
        database_module.get_settings.cache_clear()
        asyncio.run(init_db())
    finally:
        if old is None:
            os.environ.pop("HOLDEXAR_DATA_DIR", None)
        else:
            os.environ["HOLDEXAR_DATA_DIR"] = old
        # 环境已还原，缓存必须重新解析，防模板目录外溢给后续用例
        database_module.get_engine.cache_clear()
        database_module.get_session_factory.cache_clear()
        database_module.get_settings.cache_clear()
    db_files = list(tpl_dir.glob("*.db"))
    if len(db_files) != 1:
        raise RuntimeError(f"模板库构建异常：期望 1 个 .db 文件，实际 {db_files}")
    db_file = db_files[0]
    conn = sqlite3.connect(db_file)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    _TEMPLATE["db_file"] = db_file
    _TEMPLATE["dir"] = tpl_dir
    return db_file


def pytest_sessionfinish(session, exitstatus):
    """会话末补删临时目录：Windows 下用例内打开的连接句柄可能延迟释放，
    逐用例 rmtree 失败的残留（holdexar-test-data-*）在会话末句柄已释放时
    统一补删，从源头消灭 Temp 积压。"""
    import shutil

    if _TEMPLATE["dir"] is not None:
        shutil.rmtree(_TEMPLATE["dir"], ignore_errors=True)
        _TEMPLATE["db_file"] = None
        _TEMPLATE["dir"] = None
    for d in _ISOLATED_DIRS:
        shutil.rmtree(d, ignore_errors=True)
    _ISOLATED_DIRS.clear()


@pytest.fixture(autouse=True)
def _isolate_data_dir(request, _db_caches_fresh_before_each):
    """默认把数据目录隔离进一次性临时目录，库文件 = 会话模板库的克隆。

    每个用例拿到与生产新装同构的完整空库（schema+种子，一次构建逐用例
    复制），漏桩的域模块落库落进本用例临时库，「测试写生产库」从结构上
    不可能。真实库用例仍按文件级 HOLDEXAR_TEST_REAL_DB 豁免（经
    request.module 读取，不走进程全局环境变量）。临时目录登记进
    _ISOLATED_DIRS，会话末统一补删。
    """
    # 测试内 monkeypatch 与本夹具对同一环境变量的还原顺序随文件内夹具结构
    # 变化，上个用例可能留下指向已删临时目录的孤儿值——每个用例 setup 先
    # 自愈清掉本家族残留，豁免用例才不会继承死路径。
    stale = os.environ.get("HOLDEXAR_DATA_DIR", "")
    if stale.startswith(os.path.join(tempfile.gettempdir(), "holdexar-test-data-")):
        os.environ.pop("HOLDEXAR_DATA_DIR", None)

    if getattr(request.module, "HOLDEXAR_TEST_REAL_DB", False) is True:
        yield
        return
    import shutil

    from app.core import database as database_module

    # 先抓原函数再清：teardown 时刻模块属性可能仍被本用例夹具的假件占据
    # （手动 setattr 的夹具晚于本夹具还原），只有原对象保证带 cache_clear
    orig_engine = database_module.get_engine
    orig_factory = database_module.get_session_factory
    orig_settings = database_module.get_settings
    old = os.environ.get("HOLDEXAR_DATA_DIR")
    tmp_dir = tempfile.mkdtemp(prefix="holdexar-test-data-")
    _ISOLATED_DIRS.append(tmp_dir)
    os.environ["HOLDEXAR_DATA_DIR"] = tmp_dir
    orig_engine.cache_clear()
    orig_factory.cache_clear()
    orig_settings.cache_clear()
    # 隔离库 = 完整模板库的克隆（schema+种子与生产新装同源），不再逐用例
    # 重跑 init_db 建库链；建库/迁移语义的覆盖归 test_schema_migrations。
    shutil.copyfile(_get_template_db_file(), Path(tmp_dir) / _get_template_db_file().name)
    yield
    os.environ.pop("HOLDEXAR_DATA_DIR", None)
    if old is not None:
        os.environ["HOLDEXAR_DATA_DIR"] = old
    orig_engine.cache_clear()
    orig_factory.cache_clear()
    orig_settings.cache_clear()
    shutil.rmtree(tmp_dir, ignore_errors=True)
