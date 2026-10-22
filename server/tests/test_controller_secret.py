"""Mihomo 控制密钥：每次冷启动随机生成，配置里的值不被采信。

控制面只绑回环，但固定密钥会让「本机可达」等价于「本机可控制」——
同机任意进程可拿它 PUT /configs 把配置灌进本内核。
"""
from __future__ import annotations

import re

import yaml

from app.domains.proxies import clash_manager


def test_runtime_secret_does_not_change_subscription_identity():
    config = "proxies: []\nsecret: subscription-value\n"
    running = "proxies: []\nsecret: current-process-value\n"
    assert clash_manager.config_unchanged(config, running)


def test_reload_file_keeps_current_controller_secret(tmp_path):
    runtime = clash_manager.ClashRuntime()
    runtime.secret = "current-process-value"
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump({"secret": "subscription-value", "proxies": [{
        "name": "n1", "type": "trojan", "server": "node.example", "port": 443, "password": "x",
    }]}), encoding="utf-8")
    prepared, _ = runtime._running_kernel_text(str(path))
    assert yaml.safe_load(prepared)["secret"] == runtime.secret


def test_controller_secret_is_random_each_call():
    config = "external-controller: 127.0.0.1:19090\nsecret: attacker-chosen\n"
    _, first = clash_manager.resolve_controller(config)
    _, second = clash_manager.resolve_controller(config)
    assert first != second
    assert len(first) >= 32


def test_controller_secret_ignores_config_value():
    """配置文件里的 secret 由机场写入，不是本应用发放——绝不沿用。"""
    config = "external-controller: 127.0.0.1:19090\nsecret: from-subscription\n"
    _, secret = clash_manager.resolve_controller(config)
    assert secret != "from-subscription"


def test_controller_secret_is_not_the_old_constant():
    """回归：过去用应用名当固定密钥，等于公开密钥。"""
    from app.core.app_info import APP_SLUG

    config = "external-controller: 127.0.0.1:19090\n"
    _, secret = clash_manager.resolve_controller(config)
    assert secret != APP_SLUG
    assert secret != "holdexar-proxypool"


def test_controller_url_is_loopback():
    config = "external-controller: 127.0.0.1:19090\n"
    url, _ = clash_manager.resolve_controller(config)
    assert url.startswith("http://127.0.0.1:")


def test_pool_runtime_secret_generated_on_cold_start(tmp_path):
    """池运行配置冷启动时不得写入可预测的固定密钥。"""
    from app.domains.proxypool import runtime

    pool = runtime.pool_path(tmp_path)
    pool.parent.mkdir(parents=True, exist_ok=True)
    pool.write_text(
        "proxies:\n  - name: n1\n    type: trojan\n    server: a.example\n"
        "    port: 443\n    password: x\nproxy-groups: []\n",
        encoding="utf-8",
    )
    out = runtime.prepare_runtime_config(tmp_path, lanes=1)
    text = out.read_text(encoding="utf-8")
    match = re.search(r"^secret:\s*(\S+)", text, re.MULTILINE)
    assert match is not None
    secret = match.group(1)
    assert secret != "holdexar-proxypool"
    assert len(secret) >= 32
