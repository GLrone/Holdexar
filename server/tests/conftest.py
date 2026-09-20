"""测试共享夹具与 sys.path 注入。"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


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
    expected = str(database_module.get_settings().db_url)
    yield
    try:
        factory = orig_factory()
    except Exception:  # noqa: BLE001 —— 取不到工厂：保守清除
        orig_engine.cache_clear()
        orig_factory.cache_clear()
        return
    bind = getattr(getattr(factory, "kw", {}), "get", lambda *_: None)("bind")
    bind_url = str(getattr(bind, "url", "") or "")
    if not bind_url or bind_url != expected:
        orig_engine.cache_clear()
        orig_factory.cache_clear()


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
