import pytest

from app.core import update_host


def test_recovery_cli_uses_existing_recovery_without_running_an_update(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(update_host, "recover_interrupted_update", lambda root: calls.append(root))
    monkeypatch.setattr(update_host, "execute_update", lambda **kwargs: pytest.fail("unexpected update"))
    with pytest.raises(SystemExit) as result:
        update_host.main(["--recover-only", "--target", str(tmp_path)])
    assert result.value.code == 0
    assert calls == [tmp_path.resolve()]


def test_recovery_failure_returns_without_a_second_dialog(tmp_path, monkeypatch, capsys):
    def fail(root):
        raise RuntimeError("snapshot unavailable")
    monkeypatch.setattr(update_host, "recover_interrupted_update", fail)
    monkeypatch.setattr(update_host, "_alert", lambda *args: pytest.fail("launcher owns the error dialog"))
    with pytest.raises(SystemExit) as result:
        update_host.main(["--recover-only", "--target", str(tmp_path)])
    assert result.value.code == 1
    assert "snapshot unavailable" in capsys.readouterr().err


def test_recovery_and_update_actions_are_exclusive(tmp_path):
    with pytest.raises(SystemExit) as result:
        update_host.main(["--recover-only", "--staging", str(tmp_path)])
    assert result.value.code == 2


def test_recovery_rejects_a_version_directory(tmp_path, monkeypatch):
    (tmp_path / "state").mkdir()
    (tmp_path / "state/current.json").write_text('{"version":"1.0.0"}', encoding="utf-8")
    version = tmp_path / "versions/1.0.0"
    version.mkdir(parents=True)
    monkeypatch.setattr(update_host, "recover_interrupted_update", lambda root: pytest.fail("invalid install root"))
    with pytest.raises(SystemExit) as result:
        update_host.main(["--recover-only", "--target", str(version)])
    assert result.value.code == 1


def test_update_host_is_installed_before_the_launcher_that_requires_it(tmp_path, monkeypatch):
    root, staging = tmp_path / "install", tmp_path / "staging"
    payload = staging / "Holdexar"
    (payload / "versions/0.3.6").mkdir(parents=True)
    (payload / "versions/0.3.6/Holdexar.exe").write_bytes(b"version")
    (payload / "updater").mkdir()
    (payload / "updater/UpdateHost.exe").write_bytes(b"new host")
    (payload / "Holdexar.exe").write_bytes(b"new launcher")
    root.mkdir()
    (root / "Holdexar.exe").write_bytes(b"old launcher")
    replaced = []
    def fail_host(source, target):
        replaced.append(target.name)
        if target.name == "UpdateHost.exe":
            raise PermissionError("host cannot be installed")
        target.write_bytes(source.read_bytes())
    monkeypatch.setattr(update_host, "_safe_replace_executable", fail_host)
    with pytest.raises(PermissionError):
        update_host.install_version_payload(root, staging, "0.3.6")
    assert replaced == ["UpdateHost.exe"]
    assert (root / "Holdexar.exe").read_bytes() == b"old launcher"


def test_incomplete_executable_copy_preserves_the_installed_entry(tmp_path, monkeypatch):
    source, target = tmp_path / "new.exe", tmp_path / "installed.exe"
    source.write_bytes(b"complete new executable")
    target.write_bytes(b"complete old executable")
    def partial_copy(src, dest):
        dest.write_bytes(b"partial")
        raise OSError("disk write interrupted")
    monkeypatch.setattr(update_host.shutil, "copy2", partial_copy)
    with pytest.raises(OSError, match="disk write interrupted"):
        update_host._safe_replace_executable(source, target)
    assert target.read_bytes() == b"complete old executable"


@pytest.mark.parametrize("restore_blocked", [False, True])
def test_failed_second_replace_retains_the_old_executable(tmp_path, monkeypatch, restore_blocked):
    source, target = tmp_path / "new.exe", tmp_path / "installed.exe"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    replace = update_host.os.replace
    def fail_new(src, dest):
        if dest == target and (src.suffix == ".tmp" or restore_blocked):
            raise PermissionError("replace blocked")
        return replace(src, dest)
    monkeypatch.setattr(update_host.os, "replace", fail_new)
    with pytest.raises(PermissionError):
        update_host._safe_replace_executable(source, target)
    old = list(tmp_path.glob("installed.old_*.exe")) if restore_blocked else [target]
    assert len(old) == 1 and old[0].read_bytes() == b"old"
    assert not list(tmp_path.glob("*.tmp"))


@pytest.mark.parametrize("window", [False, True])
def test_final_gui_retains_the_launchers_service_port(tmp_path, monkeypatch, window):
    root = tmp_path / "install"
    exe = root / "versions/0.3.6/Holdexar.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"fixture")
    monkeypatch.setenv("HOLDEXAR_PORT", "28981")
    environments = []
    def capture(*args, **kwargs):
        environments.append(kwargs["env"])
        raise OSError("captured before starting a process")
    monkeypatch.setattr(update_host.subprocess, "Popen", capture)
    ok, _ = update_host.probe_version_health(root, "0.3.6", "fixture", require_window=window, keep_running=window, require_guard=False)
    assert not ok and len(environments) == 1
    assert environments[0].get("HOLDEXAR_PORT") == ("28981" if window else None)
