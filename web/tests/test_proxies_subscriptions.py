

@pytest.mark.asyncio
async def test_start_switches_subscription_when_running(monkeypatch, tmp_path):
    """内核已在跑时点「启动」换了订阅 → 停旧实例按新订阅重启，不是幂等早退
    （否则界面怎么换选订阅，检测永远测的是自启拉起的那条）。"""
    from app.domains.proxies import clash_manager as cm

    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "mixed-port: 17890\nproxies:\n  - name: A\n    type: ss\n"
        "    server: s.example.net\n    port: 8388\n    cipher: aes-128-gcm\n    password: p\n",
        encoding="utf-8",
    )

    class _FakeProc:
        pid = 111

        def poll(self):
            return None

        def terminate(self):
            pass

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr(cm.subprocess, "Popen", lambda *a, **kw: _FakeProc())
    monkeypatch.setattr(cm.ClashRuntime, "_kill_orphans", lambda self, exe, cfgp: 0)
    monkeypatch.setattr(cm, "ensure_kernel_files", lambda d: None)

    r = cm.ClashRuntime()
    r.start("exe", str(cfg), subscription_url="https://a.example/sub")
    assert r.subscription_url == "https://a.example/sub"
    first = r.process

    r.start("exe", str(cfg), subscription_url="https://b.example/sub")
    assert r.subscription_url == "https://b.example/sub", "不同订阅必须切换"
    assert r.process is not first, "切换 = 停旧起新"

    same = r.process
    r.start("exe", str(cfg), subscription_url="https://b.example/sub")
    assert r.process is same, "同订阅幂等：沿用运行中的实例"
    r.stop()
    assert r.subscription_url is None
