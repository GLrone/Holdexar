from pathlib import Path

import pytest
import yaml

from app.domains.proxies import clash_manager, runtime_config


def _render(text: str) -> dict:
    render = getattr(clash_manager, "render_subscription_config", None)
    assert render is not None, "subscription runtime generation is missing"
    return yaml.safe_load(render(text))


def _source(**extra) -> str:
    doc = {
        "mixed-port": 17890,
        "proxies": [{"name": "node-a", "type": "trojan", "server": "node.example",
                     "port": 443, "password": "test-password", "sni": "tls.example"}],
        "rules": ["GEOIP,CN,DIRECT", "GEOSITE,cn,DIRECT", "MATCH,DIRECT"],
        "dns": {"enable": True, "fallback-filter": {"geoip": True, "geosite": ["cn"]}},
        "tun": {"enable": True},
        "geodata-mode": True,
        "geo-auto-update": True,
        "rule-providers": {"region": {"type": "http", "url": "https://example.invalid/rules"}},
        "external-controller": "0.0.0.0:9999",
        "secret": "untrusted-controller",
    }
    doc.update(extra)
    return yaml.safe_dump(doc, sort_keys=False)


def test_subscription_runtime_preserves_nodes_without_geo_consumers():
    source = _source()
    runtime = _render(source)
    assert runtime["mode"] == "global"
    assert runtime["proxies"] == yaml.safe_load(source)["proxies"]
    assert not {"rules", "rule-providers", "sub-rules", "tun", "geodata-mode",
                "geo-auto-update", "geo-update-interval", "geodata-loader",
                "geosite-matcher", "geox-url", "external-controller",
                "secret"}.intersection(runtime)
    assert "fallback-filter" not in runtime["dns"]
    assert "fallback" not in runtime["dns"]
    assert runtime["dns"]["enhanced-mode"] == "redir-host"


def test_provider_only_subscription_must_be_expanded_before_rendering():
    providers = {"remote": {"type": "http", "url": "https://example.invalid/nodes",
                             "path": "./provider.yaml", "interval": 3600,
                             "override": {"udp": True}}}
    with pytest.raises(ValueError, match="展开"):
        _render(_source(proxies=[], **{"proxy-providers": providers}))


def test_expanded_provider_subscription_keeps_nodes_without_remote_definitions():
    providers = {"remote": {"type": "http", "url": "https://example.invalid/nodes",
                             "override": {"udp": True}}}
    source = _source(proxies=[], **{"proxy-providers": providers})
    nodes = [{"name": "p-1", "type": "trojan", "server": "p1.example", "port": 443,
              "password": "x"},
             {"name": "p-2", "type": "ss", "server": "p2.example", "port": 8388,
              "cipher": "aes-256-gcm", "password": "x"}]
    expanded = runtime_config.expand_provider_nodes(source, {"remote": nodes})
    runtime = _render(expanded)
    assert [p["name"] for p in runtime["proxies"]] == ["p-1", "p-2"]
    assert all(p["udp"] is True for p in runtime["proxies"])
    assert "proxy-providers" not in runtime
    runtime_config.validate_execution_config(runtime)


def test_geo_words_in_nodes_and_resolver_urls_are_preserved():
    proxies = [{"name": "GeoIP relay", "type": "trojan", "server": "geosite.example",
                "port": 443, "password": "secret", "sni": "geoip:service.example"}]
    resolver = "https://dns.example/geoip:edge"
    source = _source(
        proxies=proxies,
        dns={"enable": True, "nameserver": [resolver]},
    )

    runtime = _render(source)

    assert runtime["proxies"] == proxies
    assert runtime["dns"]["nameserver"] == [resolver]


def test_subscription_runtime_keeps_required_dialer_group_only():
    nodes = yaml.safe_load(_source())["proxies"]
    nodes[0]["dialer-proxy"] = "relay-group"
    nodes.append({"name": "relay", "type": "socks5", "server": "relay.example", "port": 1080})
    source = _source(proxies=nodes, **{"proxy-groups": [
        {"name": "relay-group", "type": "select", "proxies": ["relay"]},
        {"name": "unused", "type": "select", "proxies": ["DIRECT"]},
    ]})
    runtime = _render(source)
    assert any(g["name"] == "relay-group" for g in runtime["proxy-groups"])
    assert not any(g["name"] == "unused" for g in runtime["proxy-groups"])
    assert runtime["proxies"][0]["dialer-proxy"] == "relay-group"


def test_dialer_target_must_exist():
    nodes = yaml.safe_load(_source())["proxies"]
    nodes[0]["dialer-proxy"] = "missing"
    with pytest.raises(ValueError, match="依赖"):
        _render(_source(proxies=nodes))


def test_dependency_mapping_preserves_full_config_and_source():
    resolve = getattr(clash_manager, "resolve_node_dependencies", None)
    assert resolve is not None, "shared node dependency resolver is missing"
    nodes = [{"name": "a", "type": "trojan", "password": "test",
              "dialer-proxy": "b", "port": 9443}, {"name": "b", "type": "socks5"}]
    result = resolve(nodes, {"a": "1|a", "b": "1|b"})
    assert result[0]["name"] == "1|a"
    assert result[0]["dialer-proxy"] == "1|b"
    assert result[0]["port"] == 9443
    assert nodes[0]["name"] == "a" and nodes[0]["dialer-proxy"] == "b"


def test_kernel_install_does_not_restore_geo(tmp_path: Path, monkeypatch):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / clash_manager.kernel_filename()).write_bytes(b"kernel")
    for name in ("Country.mmdb", "geoip.dat", "geosite.dat", "ASN.mmdb"):
        (bundle / name).write_bytes(b"unused")
    monkeypatch.setattr(clash_manager, "bundled_clash_dir", lambda: bundle)
    monkeypatch.setattr(clash_manager, "_maybe_upgrade_kernel", lambda _: None)
    result = clash_manager.ensure_kernel(tmp_path / "data")
    assert result["copied"] == [clash_manager.kernel_filename()]
    assert result["missing"] == []
    assert not list((tmp_path / "data" / "clash").glob("*.mmdb"))
    assert not list((tmp_path / "data" / "clash").glob("*.dat"))


def _valid_runtime(**overrides):
    doc = {
        "mode": "global",
        "mixed-port": 17890,
        "proxies": [{"name": "node", "type": "trojan", "server": "node.example",
                     "port": 443, "password": "secret", "sni": "tls.example"}],
        "dns": {"enable": True, "nameserver": ["https://dns.example/query"]},
    }
    doc.update(overrides)
    return doc


@pytest.mark.parametrize("patch", [
    {"rules": ["GEOIP,CN,DIRECT"]},
    {"rules": ["GEOSITE,cn,DIRECT"]},
    {"rules": ["AND,((GEOIP,CN),(DST-PORT,443)),DIRECT"]},
    {"rules": ["SRC-GEOIP,CN,DIRECT"]},
    {"rules": ["IP-ASN,13335,DIRECT"]},
    {"rules": ["SRC-IP-ASN,13335,DIRECT"]},
    {"geo-auto-update": False},
    {"geo-update-interval": 24},
    {"geodata-mode": False},
    {"geodata-loader": "memconservative"},
    {"geosite-matcher": "hybrid"},
    {"geox-url": {"geoip": "https://example.invalid/geoip.dat"}},
    {"rule-providers": {"regions": {"type": "http", "url": "https://example.invalid/rules"}}},
    {"sub-rules": {"regions": ["GEOSITE,cn,DIRECT"]}},
    {"dns": {"enable": True, "nameserver": ["1.1.1.1"],
              "nameserver-policy": {"geosite:cn": ["223.5.5.5"]}}},
    {"dns": {"enable": True, "nameserver": ["1.1.1.1"],
              "nameserver-policy": {"+.example.com": ["geosite:cn"]}}},
    {"dns": {"enable": True, "nameserver": ["1.1.1.1"],
              "proxy-server-nameserver": ["223.5.5.5"],
              "proxy-server-nameserver-policy": {"rule-set:regions": ["223.5.5.5"]}}},
    {"dns": {"enable": True, "nameserver": ["1.1.1.1"],
              "fallback": ["8.8.8.8"]}},
    {"dns": {"enable": True, "nameserver": ["1.1.1.1"],
              "fallback": ["8.8.8.8"],
              "fallback-filter": {"geoip": True, "geoip-code": "CN"}}},
    {"dns": {"enable": True, "nameserver": ["1.1.1.1"],
              "enhanced-mode": "fake-ip", "fake-ip-filter": ["geosite:cn"]}},
    {"proxy-providers": {"cache": {"type": "file", "path": "./Country.mmdb"}}},
    {"proxy-providers": {"cache": {"type": "http", "url": "https://cdn.example/geoip.dat"}}},
])
def test_geo_free_validator_rejects_configured_geo_consumers(patch):
    validator = getattr(runtime_config, "validate_geo_free_config", None)
    assert callable(validator), "shared Geo-free config admission is missing"

    with pytest.raises(ValueError, match="地理数据"):
        validator(_valid_runtime(**patch))


def test_geo_free_validator_allows_plain_dns_policy_and_non_geo_fallback_filters():
    validator = getattr(runtime_config, "validate_geo_free_config", None)
    assert callable(validator), "shared Geo-free config admission is missing"
    config = _valid_runtime(dns={
        "enable": True,
        "nameserver": ["https://dns.example/geoip:region"],
        "nameserver-policy": {"+.example.com": ["223.5.5.5"]},
        "fallback": ["8.8.8.8"],
        "fallback-filter": {"geoip": False, "ipcidr": ["127.0.0.0/8"],
                             "domain": ["+.blocked.example"]},
    }, **{"proxy-providers": {
        "nodes": {"type": "http", "url": "https://sub.example/api/geoip-feed"},
    }})

    validator(config)


def test_start_rejects_geo_before_stopping_current_instance(tmp_path: Path, monkeypatch):
    old_path = tmp_path / "old.yaml"
    old_text = yaml.safe_dump(_valid_runtime())
    old_path.write_text(old_text, encoding="utf-8")
    next_path = tmp_path / "next.yaml"
    next_text = yaml.safe_dump(_valid_runtime(rules=["GEOIP,CN,DIRECT"]))
    next_path.write_text(next_text, encoding="utf-8")
    runtime = clash_manager.ClashRuntime()

    class _AliveProcess:
        pid = 101

        def poll(self):
            return None

    process = _AliveProcess()
    runtime.process = process
    runtime.config_path = str(old_path)
    runtime._startup_text = old_text
    runtime.subscription_url = "https://old.example/sub"
    stopped = []
    runtime.stop = lambda: stopped.append(True)
    monkeypatch.setattr(runtime, "_kill_orphans", lambda *_args: 0)
    monkeypatch.setattr(clash_manager, "ensure_kernel_files", lambda *_args: None)
    monkeypatch.setattr(
        clash_manager, "validate_config",
        lambda _exe, path, _work: runtime_config.validate_geo_free_config(
            Path(path).read_text(encoding="utf-8")
        ),
    )

    with pytest.raises(ValueError, match="地理数据"):
        runtime.start("unused.exe", str(next_path), subscription_url="https://new.example/sub")

    assert runtime.process is process
    assert runtime.config_path == str(old_path)
    assert old_path.read_text(encoding="utf-8") == old_text
    assert next_path.read_text(encoding="utf-8") == next_text
    assert stopped == []


@pytest.mark.asyncio
async def test_subscription_hot_reload_rejects_geo_before_put_and_keeps_old_instance(
    tmp_path: Path, monkeypatch,
):
    old_path = tmp_path / "old.yaml"
    old_text = yaml.safe_dump(_valid_runtime())
    old_path.write_text(old_text, encoding="utf-8")
    next_path = tmp_path / "next.yaml"
    next_text = yaml.safe_dump(_valid_runtime(
        rules=["GEOSITE,cn,DIRECT"],
        mode="rule",
    ))
    next_path.write_text(next_text, encoding="utf-8")
    runtime = clash_manager.ClashRuntime()

    class _AliveProcess:
        pid = 101

        def poll(self):
            return None

    process = _AliveProcess()
    runtime.process = process
    runtime.port = 17890
    runtime.config_path = str(old_path)
    runtime._startup_text = old_text
    runtime.subscription_url = "https://old.example/sub"
    runtime.controller_url = "http://127.0.0.1:19090"
    runtime.secret = "secret"
    stopped = []
    runtime.stop = lambda: stopped.append(True)
    runtime.start = lambda *_args, **_kwargs: pytest.fail("rejected reload must not restart")

    class _UnexpectedController:
        def __init__(self, **_kwargs):
            pytest.fail("Geo-dependent config reached the controller")

    monkeypatch.setattr(clash_manager.httpx, "AsyncClient", _UnexpectedController)
    monkeypatch.setattr(clash_manager, "_port_listening", lambda _port: True)
    monkeypatch.setattr(clash_manager, "_port_owned_by_process", lambda _port, _pid: True)

    with pytest.raises(ValueError, match="地理数据"):
        await runtime.ensure_running(
            "unused.exe", str(next_path), subscription_url="https://new.example/sub"
        )

    assert runtime.process is process
    assert runtime.config_path == str(old_path)
    assert old_path.read_text(encoding="utf-8") == old_text
    assert next_path.read_text(encoding="utf-8") == next_text
    assert stopped == []


def test_existing_runtime_cache_is_regenerated_from_subscription_source(tmp_path: Path):
    raw_path = tmp_path / "clash-sub-4.yaml"
    raw_path.write_text(_source(), encoding="utf-8")
    runtime_path = clash_manager.materialize_subscription_config(raw_path)
    stale = _valid_runtime(
        rules=["GEOIP,CN,DIRECT"],
        **{"geo-auto-update": False, "geox-url": {"geoip": "https://old.example/geoip.dat"}},
    )
    runtime_path.write_text(yaml.safe_dump(stale), encoding="utf-8")

    refreshed_path = clash_manager.materialize_subscription_config(runtime_path)
    refreshed = yaml.safe_load(refreshed_path.read_text(encoding="utf-8"))

    assert refreshed_path == runtime_path
    assert refreshed["proxies"] == yaml.safe_load(_source())["proxies"]
    assert not {"rules", "geo-auto-update", "geox-url"}.intersection(refreshed)


def test_pool_runtime_generation_and_reload_share_geo_free_admission(tmp_path: Path, monkeypatch):
    from app.domains.proxypool.pool import pool_path
    from app.domains.proxypool.runtime import (
        RuntimeRebuildError,
        prepare_runtime_config,
        reload_runtime_config,
    )

    pool = pool_path(tmp_path)
    pool.parent.mkdir(parents=True)
    pool.write_text(yaml.safe_dump({"proxies": _valid_runtime()["proxies"]}), encoding="utf-8")
    generated_path = prepare_runtime_config(tmp_path, lanes=1)
    generated = yaml.safe_load(generated_path.read_text(encoding="utf-8"))
    validator = getattr(runtime_config, "validate_geo_free_config", None)
    assert callable(validator), "shared Geo-free config admission is missing"
    validator(generated)
    assert generated["listeners"]
    assert generated["external-controller"]

    bad_text = yaml.safe_dump(_valid_runtime(rules=["GEOIP,CN,DIRECT"]))
    generated_path.write_text(bad_text, encoding="utf-8")

    class _UnexpectedController:
        def __init__(self, **_kwargs):
            pytest.fail("Geo-dependent pool config reached the controller")

    monkeypatch.setattr("app.domains.proxypool.runtime.httpx.AsyncClient", _UnexpectedController)
    with pytest.raises(RuntimeRebuildError, match="地理数据"):
        import asyncio

        asyncio.run(reload_runtime_config("http://127.0.0.1:19090", "secret", str(generated_path)))


def test_dead_node_probe_config_omits_geo_loading_controls():
    from app.domains.proxypool.dead_probe import RecoveredNode, _render_config

    text, _ports = _render_config(
        [RecoveredNode(
            node_id="node-1", runtime_name="node-1", original_name="node-1",
            config={"type": "trojan", "server": "node.example", "port": 443,
                    "password": "secret"},
        )],
        controller_port=19090,
        secret="probe-secret",
        listener_base=21100,
    )
    config = yaml.safe_load(text)

    assert "geo-auto-update" not in config
    assert "geodata-mode" not in config
    validator = getattr(runtime_config, "validate_geo_free_config", None)
    assert callable(validator), "shared Geo-free config admission is missing"
    validator(config)
