from __future__ import annotations

import hashlib
from datetime import datetime

import pytest
import yaml

from app.core.config import get_settings
from app.core.database import get_engine, get_session_factory, init_db
from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription
from app.domains.proxies.runtime_config import render_subscription_config
from app.domains.proxypool.models import ProxyNode, SubscriptionSnapshot, node_fingerprint
from app.domains.proxypool.pool import PoolBuildError, build_pool
from app.domains.proxypool.runtime import prepare_runtime_config, reconcile


@pytest.fixture
def source_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    yield tmp_path
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


def _source(suffix=""):
    return {
        "proxies": [
            {"name": "a", "type": "trojan", "server": f"a{suffix}.example",
             "port": 9443, "password": "secret", "sni": "tls.example",
             "dialer-proxy": "relay-group", "alpn": ["h2", "http/1.1"]},
            {"name": "relay", "type": "socks5", "server": f"relay{suffix}.example",
             "port": 1080, "username": "user", "password": "relay-secret"},
        ],
        "proxy-groups": [{"name": "relay-group", "type": "select", "proxies": ["relay"]}],
    }


def test_dns_policy_and_unknown_dns_fields_cannot_load_geo():
    doc = _source()
    doc["dns"] = {
        "enable": True,
        "nameserver": ["https://1.1.1.1/dns-query"],
        "proxy-server-nameserver": ["223.5.5.5"],
        "proxy-server-nameserver-policy": {"geosite:cn": ["223.5.5.5"]},
        "future-policy": {"geosite:cn": ["223.5.5.5"]},
    }
    dns = yaml.safe_load(render_subscription_config(yaml.safe_dump(doc)))["dns"]
    assert set(dns) == {"enable", "nameserver", "proxy-server-nameserver", "enhanced-mode"}
    assert "geosite" not in yaml.safe_dump(dns).lower()
    assert dns["proxy-server-nameserver"] == ["223.5.5.5"]


@pytest.mark.parametrize("kind", ["missing", "group-cycle", "node-cycle"])
def test_dependency_closure_rejects_broken_reachable_paths(kind):
    from app.domains.proxies import runtime_config

    doc = _source()
    if kind == "missing":
        doc["proxy-groups"][0]["proxies"] = ["absent"]
    elif kind == "group-cycle":
        doc["proxy-groups"][0]["proxies"] = ["relay-group"]
    else:
        doc["proxies"][1]["dialer-proxy"] = "a"
    render = getattr(runtime_config, "render_node_bundle", None)
    assert render is not None, "shared dependency closure is missing"
    with pytest.raises(ValueError, match="依赖"):
        render(doc["proxies"], {"a": "1|a", "relay": "1|relay"},
               groups=doc["proxy-groups"], namespace="1", roots=["a"])


def test_dependency_closure_ignores_unrelated_broken_node():
    from app.domains.proxies import runtime_config

    doc = _source()
    doc["proxies"].append({"name": "unrelated", "dialer-proxy": "absent"})
    render = getattr(runtime_config, "render_node_bundle", None)
    assert render is not None, "shared dependency closure is missing"
    result = render(doc["proxies"], {"a": "1|a", "relay": "1|relay", "unrelated": "1|bad"},
                    groups=doc["proxy-groups"], namespace="1", roots=["a"])
    assert {p["name"] for p in result["proxies"]} == {"1|a", "1|relay"}


@pytest.mark.parametrize("kind", ["missing", "cycle"])
def test_subscription_generation_rejects_broken_group_dependencies(kind):
    doc = _source()
    doc["proxy-groups"][0]["proxies"] = ["absent" if kind == "missing" else "a"]
    with pytest.raises(ValueError, match="依赖"):
        render_subscription_config(yaml.safe_dump(doc))


async def _register(session, data_dir, sid, doc):
    raw = yaml.safe_dump(doc).encode()
    path = data_dir / f"source-{sid}.yaml"
    path.write_bytes(raw)
    session.add(ProxySubscription(
        id=sid, kind="clash", url=f"https://example.test/{sid}",
        admission_status=ADMISSION_ACTIVE,
    ))
    session.add(SubscriptionSnapshot(
        subscription_id=sid, sha256=hashlib.sha256(raw).hexdigest(), format="yaml",
        raw_path=str(path), node_count=len(doc["proxies"]), status="OK", url=f"https://example.test/{sid}",
    ))
    for payload in doc["proxies"]:
        fp = node_fingerprint(payload, str(sid))
        session.add(ProxyNode(node_id=fp, subscription_id=sid, original_name=payload['name'], runtime_name=f"{sid}|{payload['name']}", normalized_config=payload, proxy_type=payload['type'], server=payload['server'], state='ACTIVE', first_seen=datetime.now(), last_seen=datetime.now()))
    await session.flush()


@pytest.mark.asyncio
async def test_snapshot_groups_reach_pool_runtime_without_affecting_node_counts(source_dir):
    await init_db()
    async with get_session_factory()() as session:
        await _register(session, source_dir, 1, _source("1"))
        await _register(session, source_dir, 2, _source("2"))
        build = await build_pool(session, data_dir=source_dir)
        assert build.expected_count == build.written_count == 4
        doc = yaml.safe_load(build.path.read_text(encoding="utf-8"))
        groups = {g["name"]: g for g in doc["proxy-groups"]}
        assert len(groups) == 2
        for sid in (1, 2):
            a = next(p for p in doc["proxies"] if p["name"] == f"{sid}|a")
            assert groups[a["dialer-proxy"]]["proxies"] == [f"{sid}|relay"]
            assert a["alpn"] == ["h2", "http/1.1"]
            assert a["port"] == 9443 and a["password"] == "secret"
        runtime = yaml.safe_load(prepare_runtime_config(source_dir).read_text(encoding="utf-8"))
        assert set(groups).issubset({g["name"] for g in runtime["proxy-groups"]})
        observed = set(build.runtime_names) | set(groups)
        result = reconcile(registry_names=set(build.runtime_names), pool_names=set(build.runtime_names),
                           observed_names=observed, auxiliary_names=set(groups))
        assert result.ok and len(result.runtime_names) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["missing", "cycle", "corrupt", "missing-file"])
async def test_broken_group_source_does_not_replace_current_pool(source_dir, broken):
    from sqlalchemy import select

    await init_db()
    async with get_session_factory()() as session:
        await _register(session, source_dir, 1, _source())
        build = await build_pool(session, data_dir=source_dir)
        before = build.path.read_bytes()
        row = await session.scalar(select(SubscriptionSnapshot))
        path = source_dir / "source-1.yaml"
        if broken == "corrupt":
            path.write_text("invalid", encoding="utf-8")
        elif broken == "missing-file":
            path.unlink()
        else:
            doc = _source()
            doc["proxy-groups"][0]["proxies"] = ["absent" if broken == "missing" else "a"]
            raw = yaml.safe_dump(doc).encode()
            path.write_bytes(raw)
            row.sha256 = hashlib.sha256(raw).hexdigest()
        with pytest.raises(PoolBuildError, match="依赖"):
            await build_pool(session, data_dir=source_dir)
        assert build.path.read_bytes() == before


@pytest.mark.asyncio
async def test_existing_subscription_cache_supplies_groups_when_no_snapshot(source_dir):
    from sqlalchemy import delete
    from app.domains.proxies.clash_manager import subscription_config_path

    await init_db()
    async with get_session_factory()() as session:
        doc = _source()
        await _register(session, source_dir, 1, doc)
        await session.execute(delete(SubscriptionSnapshot))
        path = subscription_config_path(source_dir, 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(doc), encoding="utf-8")
        build = await build_pool(session, data_dir=source_dir)
        assert build.auxiliary_names == ("HlRelay|1|relay-group",)


@pytest.mark.asyncio
async def test_provider_snapshot_keeps_group_needed_by_expanded_node(source_dir):
    import json

    await init_db()
    doc = _source()
    doc["proxy-providers"] = {
        "remote": {
            "type": "http", "url": "https://provider.invalid/nodes",
            "override": {"dialer-proxy": "relay-group"},
        }
    }
    raw = yaml.safe_dump(doc).encode()
    path = source_dir / "source-1.yaml"
    path.write_bytes(raw)
    provider_node = {
        "name": "remote-node", "type": "http", "server": "remote.example",
        "port": 8080, "dialer-proxy": "relay-group",
    }
    async with get_session_factory()() as session:
        session.add(ProxySubscription(
            id=1, kind="clash", url="https://example.test/1",
            admission_status=ADMISSION_ACTIVE,
        ))
        session.add(SubscriptionSnapshot(
            subscription_id=1, sha256=hashlib.sha256(raw).hexdigest(),
            format="provider_yaml", raw_path=str(path), node_count=3,
            status="OK", url="https://example.test/1",
            provider_nodes_json=json.dumps({"remote": [provider_node]}),
        ))
        for payload in [*doc["proxies"], provider_node]:
            fp = node_fingerprint(payload, "1")
            session.add(ProxyNode(node_id=fp, subscription_id=1, original_name=payload['name'], runtime_name=f"1|{payload['name']}", normalized_config=payload, proxy_type=payload['type'], server=payload['server'], state='ACTIVE', first_seen=datetime.now(), last_seen=datetime.now()))
        await session.commit()

        build = await build_pool(session, data_dir=source_dir)
        assert build.expected_count == 3
        assert "HlRelay|1|relay-group" in build.auxiliary_names


@pytest.mark.asyncio
async def test_provider_snapshot_expands_group_members_before_pool_render(source_dir):
    import json

    await init_db()
    doc = {
        "proxies": [
            {"name": "front", "type": "trojan", "server": "front.example",
             "port": 443, "password": "front-secret", "dialer-proxy": "provider-group"},
        ],
        "proxy-providers": {
            "remote": {
                "type": "http", "url": "https://provider.invalid/nodes",
                "override": {"additional-prefix": "remote-"},
            },
        },
        "proxy-groups": [{
            "name": "provider-group", "type": "select", "use": ["remote"],
        }],
    }
    raw = yaml.safe_dump(doc).encode()
    path = source_dir / "source-1.yaml"
    path.write_bytes(raw)
    provider_node = {
        "name": "remote-node", "type": "http", "server": "remote.example",
        "port": 8080,
    }
    expanded_provider_node = {**provider_node, "name": "remote-remote-node"}
    async with get_session_factory()() as session:
        session.add(ProxySubscription(
            id=1, kind="clash", url="https://example.test/1",
            admission_status=ADMISSION_ACTIVE,
        ))
        session.add(SubscriptionSnapshot(
            subscription_id=1, sha256=hashlib.sha256(raw).hexdigest(),
            format="provider_yaml", raw_path=str(path), node_count=2,
            status="OK", url="https://example.test/1",
            provider_nodes_json=json.dumps({"remote": [provider_node]}),
        ))
        for payload in [doc["proxies"][0], expanded_provider_node]:
            fp = node_fingerprint(payload, "1")
            session.add(ProxyNode(node_id=fp, subscription_id=1, original_name=payload['name'], runtime_name=f"1|{payload['name']}", normalized_config=payload, proxy_type=payload['type'], server=payload['server'], state='ACTIVE', first_seen=datetime.now(), last_seen=datetime.now()))
        await session.commit()

        build = await build_pool(session, data_dir=source_dir)
        rendered = yaml.safe_load(build.path.read_text(encoding="utf-8"))
        group = next(g for g in rendered["proxy-groups"] if g["name"] in build.auxiliary_names)
        front = next(p for p in rendered["proxies"] if p["name"] == "1|front")
        assert group["proxies"] == ["1|remote-remote-node"]
        assert "use" not in group
        assert front["dialer-proxy"] == group["name"]


@pytest.mark.asyncio
async def test_runtime_ready_uses_only_declared_auxiliary_groups(source_dir, monkeypatch):
    from app.domains.proxypool import bootstrap

    await init_db()
    async with get_session_factory()() as session:
        await _register(session, source_dir, 1, _source())
        build = await build_pool(session, data_dir=source_dir)
        observed = set(build.runtime_names) | set(build.auxiliary_names)
        monkeypatch.setattr(bootstrap, "current_runtime_proxy_url", lambda _: "http://local")
        monkeypatch.setattr(bootstrap, "controller_endpoint_of", lambda _: ("http://local", ""))

        async def names(*args, **kwargs):
            return frozenset(observed)

        monkeypatch.setattr(bootstrap, "wait_proxy_names", names)
        assert await bootstrap.runtime_ready(session, data_dir=source_dir)
        observed.add("undeclared-group")
        assert not await bootstrap.runtime_ready(session, data_dir=source_dir)


@pytest.mark.asyncio
async def test_source_loading_reports_one_corrupt_source_and_retains_other(source_dir):
    from app.domains.proxypool.subscription import load_source_documents

    await init_db()
    async with get_session_factory()() as session:
        await _register(session, source_dir, 1, _source("1"))
        await _register(session, source_dir, 2, _source("2"))
        (source_dir / "source-1.yaml").write_text("corrupt", encoding="utf-8")
        errors = {}
        docs = await load_source_documents(session, [1, 2], data_dir=source_dir, errors=errors)
        assert set(errors) == {1}
        assert set(docs) == {2}
        assert docs[2]["proxy-groups"][0]["proxies"] == ["relay"]


@pytest.mark.parametrize("field", ["proxy", "dialer-proxy"])
def test_provider_connection_dependency_keeps_required_group(field):
    from app.domains.proxies.runtime_config import expand_provider_nodes, render_node_bundle

    doc = _source()
    del doc["proxies"][0]["dialer-proxy"]
    provider = {"type": "http", "url": "https://example.invalid/nodes", "path": "./nodes.yaml"}
    if field == "proxy":
        provider[field] = "relay-group"
    else:
        provider["override"] = {field: "relay-group"}
    doc["proxy-providers"] = {"remote": provider}
    bundle = render_node_bundle(
        doc["proxies"], {}, groups=doc["proxy-groups"], provider_configs=doc["proxy-providers"],
    )
    assert "relay-group" in {g["name"] for g in bundle["proxy-groups"]}

    if field == "dialer-proxy":
        expanded = expand_provider_nodes(
            yaml.safe_dump(doc),
            {"remote": [{"name": "remote-node", "type": "socks5", "server": "r.example", "port": 1080}]},
        )
        runtime = yaml.safe_load(render_subscription_config(expanded))
        assert "relay-group" in {g["name"] for g in runtime["proxy-groups"]}


def test_provider_missing_dependency_is_rejected():
    from app.domains.proxies.runtime_config import expand_provider_nodes

    doc = _source()
    doc["proxy-providers"] = {
        "remote": {"type": "http", "override": {"dialer-proxy": "absent"}},
    }
    with pytest.raises(ValueError, match="未展开"):
        render_subscription_config(yaml.safe_dump(doc))

    expanded = expand_provider_nodes(
        yaml.safe_dump(doc),
        {"remote": [{"name": "remote-node", "type": "socks5", "server": "r.example", "port": 1080}]},
    )
    with pytest.raises(ValueError, match="依赖"):
        render_subscription_config(expanded)


@pytest.mark.asyncio
async def test_non_yaml_snapshot_does_not_block_node_only_sources(source_dir):
    from sqlalchemy import update
    from app.domains.proxypool.subscription import load_source_documents

    await init_db()
    async with get_session_factory()() as session:
        await _register(session, source_dir, 1, _source())
        await session.execute(update(SubscriptionSnapshot).values(format="uri"))
        errors = {}
        assert await load_source_documents(session, [1], data_dir=source_dir, errors=errors) == {1: {}}
        assert not errors


@pytest.mark.asyncio
async def test_group_only_change_updates_pool_signature_and_requests_rebuild(source_dir, monkeypatch):
    from sqlalchemy import select
    from app.domains.proxypool import bootstrap, scheduling

    await init_db()
    async with get_session_factory()() as session:
        await _register(session, source_dir, 1, _source())
        await session.commit()
        before = await bootstrap.pool_signature(session)

        async def sync(*args, **kwargs):
            row = await session.scalar(select(SubscriptionSnapshot))
            doc = _source()
            doc["proxy-groups"][0]["proxies"] = ["DIRECT"]
            raw = yaml.safe_dump(doc).encode()
            (source_dir / "source-1.yaml").write_bytes(raw)
            row.sha256 = hashlib.sha256(raw).hexdigest()
            return bootstrap.SyncResult(1, {1: row.sha256}, {1: 2}, ("1|a", "1|relay"), False, {})

        async def ready(*args, **kwargs):
            return True

        requested = []
        monkeypatch.setattr(bootstrap, "sync_subscriptions", sync)
        monkeypatch.setattr(bootstrap, "runtime_ready", ready)
        monkeypatch.setattr(scheduling, "request_rebuild", lambda: requested.append(True))
        result = await bootstrap.handle_subscription_refresh(
            session, data_dir=source_dir, runtime=None, exe_path="", now=datetime.now(),
        )
        assert await bootstrap.pool_signature(session) != before
        assert result.pool_changed and result.action == "rebuild_requested"
        assert requested == [True]


@pytest.mark.asyncio
async def test_source_group_can_match_a_generated_node_name(source_dir):
    doc = _source()
    doc["proxies"][0]["dialer-proxy"] = "1|relay"
    doc["proxy-groups"][0]["name"] = "1|relay"
    render_subscription_config(yaml.safe_dump(doc))
    await init_db()
    async with get_session_factory()() as session:
        await _register(session, source_dir, 1, doc)
        build = await build_pool(session, data_dir=source_dir)
        result = yaml.safe_load(build.path.read_text(encoding="utf-8"))
        assert result["proxies"][0]["dialer-proxy"] == "HlRelay|1|1|relay"
        assert result["proxy-groups"][0]["proxies"] == ["1|relay"]


def test_recovery_keeps_source_group_separate_from_runtime_name():
    from app.domains.proxypool.dead_probe import RecoveredNode, _render_config

    doc = _source()
    doc["proxies"][0]["dialer-proxy"] = "1|relay"
    doc["proxy-groups"][0]["name"] = "1|relay"
    nodes = [RecoveredNode(str(i), f"1|{node['name']}", node["name"], node, 1)
             for i, node in enumerate(doc["proxies"])]
    text, _ = _render_config(nodes, controller_port=23200, secret="s", listener_base=23220,
                             source_documents={1: doc})
    result = yaml.safe_load(text)
    assert result["proxies"][0]["dialer-proxy"] == "HlRelay|1|1|relay"
    assert result["proxy-groups"][0]["proxies"] == ["1|relay"]
