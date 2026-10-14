from pathlib import Path

import pytest
import yaml

from app.domains.proxies import clash_manager


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
                "external-controller", "secret"}.intersection(runtime)
    assert runtime["geo-auto-update"] is False
    assert "fallback-filter" not in runtime["dns"]
    assert "fallback" not in runtime["dns"]
    assert runtime["dns"]["enhanced-mode"] == "redir-host"


def test_provider_only_subscription_retains_node_source():
    providers = {"remote": {"type": "http", "url": "https://example.invalid/nodes",
                             "path": "./provider.yaml", "interval": 3600,
                             "override": {"udp": True}}}
    runtime = _render(_source(proxies=[], **{"proxy-providers": providers}))
    assert runtime["proxy-providers"] == providers
    assert any("remote" in group.get("use", []) for group in runtime["proxy-groups"])


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
