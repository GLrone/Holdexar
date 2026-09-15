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
