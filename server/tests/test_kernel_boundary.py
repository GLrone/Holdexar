"""内核受控启动边界与最终运行配置准入的回归。"""
import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from app.domains.proxies import clash_manager, runtime_config
from app.domains.proxies.kernel_process import (
    PRECHECK_ADMISSION,
    PRECHECK_CHANGED,
    PRECHECK_REJECTED,
    PRECHECK_TIMEOUT,
    PRECHECK_UNLAUNCHABLE,
    KernelPrecheckError,
    confirm_config_text,
    kernel_env,
    spawn_kernel,
)

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="stub kernel is a .cmd wrapper")

NODE = {"name": "n1", "type": "trojan", "server": "node.example", "port": 443,
        "password": "x"}
OVERRIDES = {
    "CLASH_CONFIG_STRING": "Ym9ndXM=",
    "clash_post_up": "calc.exe",
    "MIHOMO_HOME": "C:/elsewhere",
    "SAFE_PATHS": "C:/",
    "DISABLE_LOOPBACK_DETECTOR": "1",
    "SKIP_SYSTEM_IPV6_CHECK": "1",
}

STUB = textwrap.dedent('''
    import json, os, pathlib, sys, time
    here = pathlib.Path(__file__).parent
    args = sys.argv[1:]
    tag = "v" if "-v" in args else "t" if "-t" in args else "run"
    (here / f"env_{tag}.json").write_text(json.dumps(dict(os.environ)))
    (here / f"args_{tag}.json").write_text(json.dumps(args))
    if tag == "v":
        print("Mihomo Meta v1.19.32 stub")
        sys.exit(0)
    cfg = args[args.index("-f") + 1]
    text = pathlib.Path(cfg).read_text(encoding="utf-8")
    if tag == "t":
        if "#stub:sleep" in text:
            time.sleep(4)
        if "#stub:reject" in text:
            print('time="t" level=error msg="proxy 0: unsupport proxy type: nosuchproto"')
            sys.exit(1)
        if "#stub:mutate" in text:
            pathlib.Path(cfg).write_text(text + "\\n#changed\\n", encoding="utf-8")
        sys.exit(0)
    time.sleep(2)
''')


@pytest.fixture
def stub(tmp_path, monkeypatch):
    folder = tmp_path / "stubkernel"
    folder.mkdir()
    (folder / "stub_kernel.py").write_text(STUB, encoding="utf-8")
    exe = folder / "mihomo-stub.cmd"
    exe.write_text(f'@"{sys.executable}" "{folder / "stub_kernel.py"}" %*\r\n', encoding="utf-8")
    monkeypatch.setenv("HOLDEXAR_BENIGN_MARKER", "kept")
    for key, value in OVERRIDES.items():
        monkeypatch.setenv(key, value)

    class Stub:
        path = exe
        dir = folder

        @staticmethod
        def env(tag: str) -> dict:
            return json.loads((folder / f"env_{tag}.json").read_text(encoding="utf-8"))

        @staticmethod
        def ran(tag: str) -> bool:
            return (folder / f"env_{tag}.json").exists()

    return Stub


def _config(tmp_path: Path, extra: str = "", **doc) -> Path:
    body = {"mode": "global", "proxies": [NODE], **doc}
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(body) + extra, encoding="utf-8")
    return path


def _assert_clean(env: dict) -> None:
    upper = {key.upper() for key in env}
    for key in OVERRIDES:
        assert key.upper() not in upper, f"{key} 泄漏进内核环境"
    assert env["HOLDEXAR_BENIGN_MARKER"] == "kept"
    assert any(key.upper() == "PATH" for key in env)


def test_kernel_env_strips_overrides_and_keeps_ordinary_environment(monkeypatch):
    for key, value in OVERRIDES.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("HOLDEXAR_BENIGN_MARKER", "kept")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    env = kernel_env()
    _assert_clean(env)
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:1"
    assert kernel_env({"A": "1", "clash_x": "2", "Mihomo_Y": "3"}) == {"A": "1"}


def test_version_probe_precheck_and_launch_share_the_clean_environment(stub, tmp_path):
    assert clash_manager.kernel_version(stub.path) == "Mihomo Meta v1.19.32 stub"
    config = _config(tmp_path)
    clash_manager.validate_config(str(stub.path), str(config), tmp_path)
    process = spawn_kernel(stub.path, ["-f", str(config), "-d", str(tmp_path)])
    process.wait(timeout=20)
    for tag in ("v", "t", "run"):
        _assert_clean(stub.env(tag))


@pytest.mark.parametrize("marker, kind, text", [
    ("#stub:reject", PRECHECK_REJECTED, "nosuchproto"),
    ("#stub:mutate", PRECHECK_CHANGED, "改动"),
])
def test_validate_config_refuses_kernel_rejection_and_swapped_file(
    stub, tmp_path, marker, kind, text,
):
    config = _config(tmp_path, f"\n{marker}\n")
    with pytest.raises(KernelPrecheckError, match=text) as caught:
        clash_manager.validate_config(str(stub.path), str(config), tmp_path)
    assert caught.value.kind == kind


def test_validate_config_timeout_is_a_hard_refusal(stub, tmp_path, monkeypatch):
    monkeypatch.setattr(clash_manager, "CONFIG_TEST_TIMEOUT", 1.0)
    config = _config(tmp_path, "\n#stub:sleep\n")
    with pytest.raises(KernelPrecheckError) as caught:
        clash_manager.validate_config(str(stub.path), str(config), tmp_path)
    assert caught.value.kind == PRECHECK_TIMEOUT and "超时" in str(caught.value)


def test_validate_config_admission_failure_never_reaches_the_kernel(stub, tmp_path):
    config = _config(tmp_path, rules=["GEOSITE,cn,DIRECT"])
    with pytest.raises(KernelPrecheckError, match="地理数据") as caught:
        clash_manager.validate_config(str(stub.path), str(config), tmp_path)
    assert caught.value.kind == PRECHECK_ADMISSION
    assert not stub.ran("t")


def test_validate_config_reports_unlaunchable_kernel(tmp_path):
    config = _config(tmp_path)
    broken = tmp_path / "mihomo.exe"
    broken.write_bytes(b"stub")
    for exe in (broken, tmp_path / "missing.exe"):
        with pytest.raises(KernelPrecheckError) as caught:
            clash_manager.validate_config(str(exe), str(config), tmp_path)
        assert caught.value.kind == PRECHECK_UNLAUNCHABLE


def test_confirm_config_text_detects_replacement(tmp_path):
    path = tmp_path / "run.yaml"
    path.write_text("a: 1\n", encoding="utf-8")
    confirm_config_text(path, "a: 1\n")
    path.write_text("a: 2\n", encoding="utf-8")
    with pytest.raises(KernelPrecheckError) as caught:
        confirm_config_text(path, "a: 1\n")
    assert caught.value.kind == PRECHECK_CHANGED


def _runtime_for_start(monkeypatch) -> clash_manager.ClashRuntime:
    runtime = clash_manager.ClashRuntime()
    monkeypatch.setattr(runtime, "_kill_orphans", lambda *_a: 0)
    monkeypatch.setattr(clash_manager, "ensure_kernel_files", lambda *_a: None)
    return runtime


def test_start_refuses_unadmitted_config_before_touching_files_or_processes(
    stub, tmp_path, monkeypatch,
):
    config = _config(tmp_path, **{"x-stub": "#stub:reject"})
    original = config.read_text(encoding="utf-8")
    monkeypatch.setattr(
        clash_manager, "spawn_kernel",
        lambda *a, **k: pytest.fail("预检失败的配置不得拉起内核"),
    )
    with pytest.raises(KernelPrecheckError, match="nosuchproto"):
        _runtime_for_start(monkeypatch).start(
            str(stub.path), str(config), inject_lanes=False, mixed_port=17893,
        )
    assert config.read_text(encoding="utf-8") == original


def test_start_launches_exactly_the_validated_file_with_clean_environment(
    stub, tmp_path, monkeypatch,
):
    config = _config(tmp_path)
    runtime = _runtime_for_start(monkeypatch)
    validated = []
    validate = clash_manager._validate_config_text

    def observe(exe, text, directory):
        validated.append(text)
        return validate(exe, text, directory)

    monkeypatch.setattr(clash_manager, "_validate_config_text", observe)
    with pytest.raises(RuntimeError):  # stub 不监听端口：入口就绪检查按设计拒绝
        runtime.start(str(stub.path), str(config), inject_lanes=False, mixed_port=17894)
    args = json.loads((stub.dir / "args_run.json").read_text(encoding="utf-8"))
    assert args[args.index("-f") + 1] == str(config)
    _assert_clean(stub.env("run"))
    actual = config.read_text(encoding="utf-8")
    assert actual == validated[-1]
    loaded = yaml.safe_load(actual)
    assert loaded["mixed-port"] == 17894
    assert loaded["secret"] == args[args.index("-secret") + 1]
    assert len(loaded["secret"]) >= 32


def test_pool_reload_applies_the_same_final_admission(tmp_path, monkeypatch):
    from app.domains.proxypool.runtime import RuntimeRebuildError, reload_runtime_config

    class Unexpected:
        def __init__(self, **_kwargs):
            pytest.fail("未准入的配置到达了控制器")

    monkeypatch.setattr("app.domains.proxypool.runtime.httpx.AsyncClient", Unexpected)
    path = tmp_path / "runtime.yaml"
    path.write_text(yaml.safe_dump({
        "proxies": [NODE],
        "proxy-groups": [{"name": "auto", "type": "url-test", "proxies": ["n1"],
                          "url": "http://x/generate_204", "interval": 300}],
    }), encoding="utf-8")
    with pytest.raises(RuntimeRebuildError, match="自行决定出口"):
        asyncio.run(reload_runtime_config("http://127.0.0.1:1", "s", str(path)))


# ── 最终运行配置准入 ───────────────────────────────────────────────

def _runtime(**extra) -> dict:
    return {"mode": "global", "proxies": [NODE], **extra}


@pytest.mark.parametrize("patch, message", [
    ({"sniffer": {"force-domain": ["geosite:cn"]}}, "sniffer.force-domain"),
    ({"sniffer": {"skip-domain": ["+.example", "geosite:cn"]}}, "sniffer.skip-domain"),
    ({"sniffer": {"skip-src-address": ["geoip:cn"]}}, "sniffer.skip-src-address"),
    ({"sniffer": {"skip-dst-address": ["geoip:private"]}}, "sniffer.skip-dst-address"),
    ({"proxy-providers": {"p": {"type": "file", "path": "./geoip.db"}}}, "地理数据"),
    ({"proxy-providers": {"p": {"type": "file", "path": "./geoip.metadb"}}}, "地理数据"),
    ({"proxy-providers": {"p": {"type": "http", "url": "https://x.example/n"}}}, "未展开"),
    ({"proxy-providers": {"p": {"type": "inline", "payload": []}}}, "未展开"),
    ({"external-ui-url": "https://x.example/ui.zip"}, "界面资源"),
    ({"external-ui": "ui"}, "界面资源"),
    ({"proxy-groups": [{"name": "g", "type": "url-test", "proxies": ["n1"]}]}, "自行决定出口"),
    ({"proxy-groups": [{"name": "g", "type": "fallback", "proxies": ["n1"]}]}, "自行决定出口"),
    ({"proxy-groups": [{"name": "g", "type": "load-balance", "proxies": ["n1"]}]}, "自行决定出口"),
    ({"proxy-groups": [{"name": "g", "type": "select", "proxies": ["n1"], "url": "http://x",
                        "interval": 300}]}, "自主选路或探活"),
    ({"proxy-groups": [{"name": "g", "type": "select", "use": ["p"]}]}, "自主选路或探活"),
    ({"proxy-groups": [{"name": "g", "type": "select", "include-all": True}]}, "自主选路或探活"),
    ({"proxy-groups": [{"name": "g", "type": "select", "proxies": ["n1"],
                        "filter": "HK"}]}, "自主选路或探活"),
    ({"proxy-groups": [{"name": "HlProbeLane-1", "type": "url-test", "include-all": True}]},
     "自行决定出口"),
    ({"proxy-groups": [{"name": "HlProbeLane-1", "type": "select", "include-all": True,
                        "url": "http://x"}]}, "自主选路或探活"),
])
def test_execution_admission_rejects_uncontrolled_resources_and_autonomy(patch, message):
    with pytest.raises(ValueError, match=message):
        runtime_config.validate_execution_config(_runtime(**patch))


def test_execution_admission_keeps_accepting_controlled_configs():
    runtime_config.validate_execution_config(_runtime(
        sniffer={"enable": True, "force-domain": ["+.example.com"], "skip-domain": []},
        **{"proxy-groups": [
            {"name": "pick", "type": "select", "proxies": ["n1"], "interval": 0, "lazy": False},
            {"name": "chain", "type": "relay", "proxies": ["n1"]},
            {"name": "HlProbeLane-1", "type": "select", "include-all": True},
        ]},
    ))
    runtime_config.validate_execution_config(_runtime(proxies=[{
        **NODE, "name": "geosite geoip:cn", "server": "geosite.example", "sni": "geoip:x.example",
    }]))
    runtime_config.validate_execution_config(
        "mode: global\nproxies:\n  - {name: a, type: ss, server: s, port: 1, cipher: aes-256-gcm,"
        " password: p}\n"
    )


def test_every_geo_negative_in_the_acceptance_set_still_fails_final_admission():
    for patch in (
        {"rules": ["GEOIP,CN,DIRECT"]}, {"rules": ["GEOSITE,cn,DIRECT"]},
        {"rule-providers": {"r": {"type": "http", "url": "https://x/r"}}},
        {"geo-auto-update": True}, {"geox-url": {"geoip": "https://x/geoip.dat"}},
        {"dns": {"enable": True, "nameserver-policy": {"geosite:cn": ["223.5.5.5"]}}},
    ):
        with pytest.raises(ValueError):
            runtime_config.validate_execution_config(_runtime(**patch))


# ── 来源配置 → 执行配置 ────────────────────────────────────────────

def _render(doc: dict) -> dict:
    return yaml.safe_load(runtime_config.render_subscription_config(yaml.safe_dump(doc)))


RELAY = {"name": "hop", "type": "socks5", "server": "hop.example", "port": 1080}


def test_auto_group_in_a_dialer_chain_becomes_a_deterministic_select():
    leaf = {**NODE, "dialer-proxy": "auto"}
    for kind in ("url-test", "fallback"):
        runtime = _render({
            "proxies": [leaf, RELAY, {**RELAY, "name": "hop2"}],
            "proxy-groups": [{"name": "auto", "type": kind, "proxies": ["hop", "hop2"],
                              "url": "http://www.gstatic.com/generate_204",
                              "interval": 300, "tolerance": 50, "lazy": True}],
        })
        group = runtime["proxy-groups"][0]
        assert group["type"] == "select" and group["proxies"] == ["hop", "hop2"]
        assert not {"url", "interval", "tolerance", "lazy"}.intersection(group)
        runtime_config.validate_execution_config(runtime)


@pytest.mark.parametrize("group, message", [
    ({"name": "auto", "type": "load-balance", "proxies": ["hop"], "strategy": "round-robin"},
     "无法确定性执行"),
    ({"name": "auto", "type": "smart", "proxies": ["hop"]}, "无法确定性执行"),
    ({"name": "auto", "type": "select", "include-all": True, "filter": "HK"}, "节点过滤"),
])
def test_auto_group_without_deterministic_meaning_is_refused_with_reason(group, message):
    with pytest.raises(ValueError, match=message):
        _render({"proxies": [{**NODE, "dialer-proxy": "auto"}, RELAY], "proxy-groups": [group]})


def test_nested_groups_and_relay_dependencies_survive_rendering():
    runtime = _render({
        "proxies": [{**NODE, "dialer-proxy": "outer"}, RELAY],
        "proxy-groups": [
            {"name": "outer", "type": "select", "proxies": ["inner", "DIRECT"]},
            {"name": "inner", "type": "url-test", "proxies": ["hop"], "url": "http://x",
             "interval": 60},
        ],
    })
    by_name = {g["name"]: g for g in runtime["proxy-groups"]}
    assert set(by_name) == {"outer", "inner"}
    assert by_name["outer"]["proxies"] == ["inner", "DIRECT"]
    assert by_name["inner"]["type"] == "select"
    assert runtime["proxies"][0]["dialer-proxy"] == "outer"
    assert {p["name"] for p in runtime["proxies"]} == {"n1", "hop"}


def test_tls_reality_ws_udp_and_dialer_fields_are_preserved():
    node = {"name": "r", "type": "vless", "server": "r.example", "port": 443, "uuid": "u",
            "udp": True, "tls": True, "servername": "s.example", "network": "ws",
            "ws-opts": {"path": "/x", "headers": {"Host": "h.example"}},
            "reality-opts": {"public-key": "k", "short-id": "ab"},
            "client-fingerprint": "chrome", "dialer-proxy": "hop"}
    runtime = _render({"proxies": [node, RELAY]})
    assert runtime["proxies"][0] == node


def test_invalidate_lane_plan_blocks_crawl_admission_until_realigned(tmp_path, monkeypatch):
    from app.domains.proxypool import runtime as R

    monkeypatch.setattr(R, "lane_proxy_urls", lambda _d: ["http://127.0.0.1:28901"])
    plan_file = R.lane_plan_path(tmp_path)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        yaml.safe_dump({"lanes": [{"lane": 0, "url": "http://127.0.0.1:28901",
                                   "exitIp": "203.0.113.10", "node": "1|A"}]}),
        encoding="utf-8",
    )
    slots = [R.ExitSlot(exit_ip="203.0.113.10", runtime_name="1|A", node_id="fp-a")]
    ok_before, _ = R.lane_plan_consistency(R.lane_bindings(tmp_path), slots)
    assert ok_before is True

    R.invalidate_lane_plan(tmp_path)
    assert not plan_file.exists()
    # lane_bindings keeps the listener URL with node=None/exitIp=None after invalidation,
    # which lane_plan_consistency rejects because node None != slot.runtime_name
    ok_after, reason = R.lane_plan_consistency(R.lane_bindings(tmp_path), slots)
    assert ok_after is False
    assert "lane 0" in reason


@pytest.mark.asyncio
async def test_hot_apply_invalidates_lane_plan_before_reload_and_keeps_unaligned_on_failure(
    tmp_path, monkeypatch
):
    from app.domains.proxypool import runtime as R
    from app.domains.proxypool.pool import PoolBuild

    plan_file = R.lane_plan_path(tmp_path)
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        yaml.safe_dump({"lanes": [{"lane": 0, "url": "http://127.0.0.1:28901",
                                   "exitIp": "203.0.113.10", "node": "1|A"}]}),
        encoding="utf-8",
    )
    pool_file = tmp_path / "proxypool" / "crawl-pool.yaml"
    pool_file.write_text(yaml.safe_dump({"proxies": [NODE]}), encoding="utf-8")
    R.prepare_runtime_config(tmp_path, lanes=1)

    seen_during_put: list[bool] = []

    async def _fail_put(*a, **kw):
        seen_during_put.append(plan_file.exists())
        raise R.RuntimeRebuildError("reload failed")

    monkeypatch.setattr(R, "reload_runtime_config", _fail_put)
    build = PoolBuild(
        path=pool_file,
        runtime_names=("n1",),
        written_count=1,
        expected_count=1,
    )
    slots = [R.ExitSlot(exit_ip="203.0.113.10", runtime_name="n1", node_id="fp-n1")]
    with pytest.raises(R.RuntimeRebuildError):
        await R._hot_apply_rebuild(
            data_dir=tmp_path,
            controller_url="http://127.0.0.1:19090",
            secret="s",
            runtime=None,
            build=build,
            slots=slots,
            previous="n1",
            previous_lanes=["n1"],
            wait_timeout=1.0,
        )
    assert seen_during_put == [False]
    assert not plan_file.exists()


@pytest.mark.asyncio
async def test_l2_tunnel_failure_vs_target_vs_uncertain_state_and_exitstats(
    tmp_path, monkeypatch
):
    from datetime import datetime
    from sqlalchemy import select
    from app.core.config import get_settings
    from app.core.database import get_engine, get_session_factory, init_db
    from app.domains.proxies.models import ADMISSION_ACTIVE, ProxySubscription
    from app.domains.proxypool import exitstats as E
    from app.domains.proxypool import health as H
    from app.domains.proxypool.models import HealthObservation, ProxyNode, node_fingerprint
    from app.domains.proxypool.state import NODE_ACTIVE, NODE_DEAD

    monkeypatch.setenv("HOLDEXAR_DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    try:
        await init_db()
        now = datetime(2026, 10, 21, 8, 0, 0)
        async with get_session_factory()() as s:
            s.add(ProxySubscription(
                id=1, kind="clash", url="https://sub.invalid/1",
                admission_status=ADMISSION_ACTIVE,
            ))
            for idx, name in enumerate(("1|tunnel-dead", "1|target-502", "1|uncertain-502"), 1):
                cfg = {"name": name, "type": "ss", "server": f"10.0.0.{idx}",
                       "port": 443, "cipher": "aes-128-gcm", "password": "p"}
                fp = node_fingerprint(cfg)
                s.add(ProxyNode(node_id=fp, runtime_name=name, proxy_type='ss', server=cfg['server'], normalized_config=cfg, state=NODE_ACTIVE, consecutive_failures=2, subscription_id=1, original_name=name, first_seen=now, last_seen=now, last_source_seen=now))
            await s.commit()

        async with get_session_factory()() as s:
            rows = {
                n.runtime_name: n
                for n in (await s.execute(select(ProxyNode))).scalars().all()
            }
            await H._commit_business_wave(
                s,
                [
                    (rows["1|tunnel-dead"], H.BusinessResult(
                        "1|tunnel-dead", False, 502, None,
                        f"{H.NODE_TUNNEL_FAILED}: HTTP 502（内核探测失败（HTTP 503））",
                        10, attribution=H.ATTR_NODE,
                    )),
                    (rows["1|target-502"], H.BusinessResult(
                        "1|target-502", False, 502, None,
                        f"{H.TARGET_SERVICE_FAILED}: HTTP 502",
                        12, attribution=H.ATTR_TARGET,
                    )),
                    (rows["1|uncertain-502"], H.BusinessResult(
                        "1|uncertain-502", False, 502, None,
                        f"{H.UNCERTAIN_FAILURE}: HTTP 502（控制器请求异常：ConnectError）",
                        None, attribution=H.ATTR_UNCERTAIN,
                    )),
                ],
                now=now,
                run_id=None,
                channel="test",
                target_url="https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
            )

        async with get_session_factory()() as s:
            rows = {
                n.runtime_name: n
                for n in (await s.execute(select(ProxyNode))).scalars().all()
            }
            assert rows["1|tunnel-dead"].consecutive_failures == 3
            assert rows["1|tunnel-dead"].state == NODE_DEAD
            assert rows["1|target-502"].consecutive_failures == 2
            assert rows["1|target-502"].state == NODE_ACTIVE
            assert rows["1|uncertain-502"].consecutive_failures == 2
            assert rows["1|uncertain-502"].state == NODE_ACTIVE

            obs = {
                o.node_id: o.detail
                for o in (await s.execute(select(HealthObservation))).scalars().all()
            }
            assert obs[rows["1|uncertain-502"].node_id].startswith(H.UNCERTAIN_FAILURE)

            # 环境级失败：本作业所有出口零响应（断网/上游全断）不构成节点证据
            await E._writeback_production_evidence(
                s,
                [
                    {"exit_ip": "198.51.100.1", "success": 0, "e5xx": 0, "tunnel_error": 1},
                    {"exit_ip": "198.51.100.2", "success": 0, "e5xx": 1, "uncertain": True},
                ],
                {
                    "198.51.100.1": rows["1|target-502"].runtime_name,
                    "198.51.100.2": rows["1|uncertain-502"].runtime_name,
                },
                now,
            )
            await s.commit()
            assert rows["1|target-502"].consecutive_failures == 2
            assert rows["1|target-502"].state == NODE_ACTIVE

            # 出口级失败：环境确认存活（有出口拿到 Steam 响应）时，零响应出口
            # 判 DEAD（可复活）但不累加探测死亡计数——退休只归健康探测。
            await E._writeback_production_evidence(
                s,
                [
                    {"exit_ip": "198.51.100.3", "success": 2},
                    {"exit_ip": "198.51.100.1", "success": 0, "tunnel_error": 1},
                ],
                {
                    "198.51.100.3": rows["1|uncertain-502"].runtime_name,
                    "198.51.100.1": rows["1|target-502"].runtime_name,
                },
                now,
            )
            await s.commit()
            assert rows["1|target-502"].consecutive_failures == 2
            assert rows["1|target-502"].state == NODE_DEAD
            assert rows["1|uncertain-502"].consecutive_failures == 0
            assert rows["1|uncertain-502"].state == NODE_ACTIVE
            assert rows["1|uncertain-502"].last_l2_at == now
            assert rows["1|target-502"].last_l2_at == now
    finally:
        get_settings.cache_clear()
        get_engine.cache_clear()
        get_session_factory.cache_clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_write", [False, True])
async def test_runtime_exit_health_and_observation_commit_together(fail_write):
    from datetime import datetime, timedelta
    from sqlalchemy import event, select
    from app.core.database import get_engine, get_session_factory, write_session
    from app.crawler.exit_stats import ExitStatsCollector
    from app.domains.proxypool.exitstats import write_run_exits
    from app.domains.proxypool.models import ProxyJobRun, ProxyNode, ProxyRunExit, node_fingerprint

    old = datetime(2026, 10, 21)
    now = old + timedelta(hours=1)
    config = {"type": "ss", "server": "192.0.2.1", "port": 443, "cipher": "aes-128-gcm", "password": "fixture"}
    identities = [node_fingerprint(config, tag=str(i)) for i in (1, 2)]
    async with write_session() as session:
        session.add(ProxyJobRun(id=1, status="success", started_at=old))
        for i, identity in enumerate(identities, 1):
            session.add(ProxyNode(node_id=identity, runtime_name=f"{i}|shared-name",
                                  subscription_id=i, proxy_type="ss", normalized_config=config,
                                  state="DEAD", consecutive_failures=2,
                                  last_l2_at=old, cooldown_until=now + timedelta(hours=1)))
        await session.commit()
    collector = ExitStatsCollector()
    collector.record(exit_key="203.0.113.1", url="https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                     outcome="ok", duration_ms=10)
    def inject(_c, _cur, statement, _parameters, _ctx, _many):
        if fail_write and statement.lstrip().upper().startswith("INSERT INTO PROXY_RUN_EXITS"):
            raise RuntimeError("controlled exit ledger write failure")
    engine = get_engine().sync_engine
    event.listen(engine, "before_cursor_execute", inject)
    try:
        assert await write_run_exits(run_id=1, collector=collector,
                                    node_by_exit={"203.0.113.1": "1|shared-name"}, now=now) == (0 if fail_write else 1)
    finally:
        event.remove(engine, "before_cursor_execute", inject)
    async with get_session_factory()() as session:
        nodes = (await session.execute(select(ProxyNode).order_by(ProxyNode.subscription_id))).scalars().all()
        affected, other = nodes
        assert affected.node_id == identities[0] != affected.runtime_name
        assert affected.state == ("DEAD" if fail_write else "ACTIVE")
        assert affected.consecutive_failures == (2 if fail_write else 0)
        assert affected.last_l2_at == (old if fail_write else now)
        assert affected.cooldown_until == (now + timedelta(hours=1) if fail_write else None)
        assert (other.state, other.consecutive_failures, other.last_l2_at) == ("DEAD", 2, old)
        exits = (await session.execute(select(ProxyRunExit))).scalars().all()
        assert len(exits) == (0 if fail_write else 1)
        if exits:
            assert exits[0].node == "1|shared-name" and exits[0].success == 1
