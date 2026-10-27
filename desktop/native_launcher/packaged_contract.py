"""Exercise packaged launchers against isolated Windows files and a version fixture."""
import argparse
from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import struct
import subprocess
import time
import uuid


VECTORS = (
    "current", "fallback", "forbidden_fallback", "corrupt_pointer",
    "relative_version", "absolute_version", "wal_schema", "internal_junction",
    "external_junction", "database_conflict", "active_wal_migration",
    "pointer_replace_locked", "arguments_background_server",
)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_database(path, marker):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version=32")
        connection.execute("CREATE TABLE facts (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO facts VALUES (1, ?)", (marker,))
        connection.commit()
    finally:
        connection.close()


def hold_wal(path):
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return connection


def junction(link, target):
    subprocess.run(
        ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
        check=True, capture_output=True,
    )


@contextmanager
def deny_replace(path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateFileW(str(path), 0x80000000, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        original = path.read_bytes()
        probe = path.with_name("replace-probe.json")
        probe.write_bytes(original)
        try:
            probe.replace(path)
        except OSError as error:
            denial = error.winerror
            assert denial in (5, 32), error
        else:
            raise AssertionError("replacement lock was not enforced by Windows")
        finally:
            probe.unlink(missing_ok=True)
        yield denial
    finally:
        kernel.CloseHandle(handle)


def invoke(entry, arguments, environment):
    process = subprocess.Popen(
        [str(entry), *arguments], env=environment, cwd=entry.parent,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        stdout, stderr = process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        cleanup = subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True, timeout=15,
        )
        if cleanup.returncode:
            raise RuntimeError(f"cleanup failed for owned process {process.pid}; stop without retry")
        process.communicate(timeout=15)
        raise TimeoutError("packaged launcher exceeded 90 seconds")
    def decoded(data):
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return data.decode("mbcs", "replace")

    return process.returncode, decoded(stdout), decoded(stderr)


def run_vector(mode, name, options, destination, row):
    case = destination / mode / name
    root = case / "中文 安装目录"
    root.mkdir(parents=True)
    (root / "versions").mkdir()
    for directory in ("local", "roaming", "home", "temp"):
        (case / directory).mkdir(exist_ok=True)
    entry = root / "Holdexar.exe"
    shutil.copy2(getattr(options, f"{mode}_launcher"), entry)
    if mode != "python":
        (root / ".launcher").mkdir()
        shutil.copy2(options.python_launcher, root / ".launcher" / "SafetyHost.exe")
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("HOLDEXAR_")}
    environment.update(
        HOLDEXAR_INSTALL_ROOT=str(root), HOLDEXAR_CONTRACT_OBSERVED=str(case / "observed.json"),
        HOLDEXAR_CONTRACT_SENTINEL="中文 inherited", HOLDEXAR_NO_DIALOG="1",
        LOCALAPPDATA=str(case / "local"), APPDATA=str(case / "roaming"),
        USERPROFILE=str(case / "home"), HOME=str(case / "home"),
        TEMP=str(case / "temp"), TMP=str(case / "temp"),
    )
    environment.pop("WEBVIEW2_USER_DATA_FOLDER", None)
    state = {"version": "0.3.8", "lastKnownGood": None}
    state["dataCompatibility"] = {
        "dataDir": str(root / "data"), "schemaVersion": 32,
        "versions": ["0.3.8", "0.3.7"], "contractExtension": "keep",
    }
    pointer = root / "state" / "current.json"
    pointer.parent.mkdir()
    database = root / "data" / "holdexar.db"
    make_database(database, "current")
    expected_version, expected_code = "0.3.8", 37
    arguments = ["--server"]
    preserved = [database]

    def version(directory):
        directory.mkdir(parents=True)
        shutil.copy2(options.version_fixture, directory / "Holdexar.exe")

    with ExitStack() as stack:
        if name in {"fallback", "forbidden_fallback", "pointer_replace_locked"}:
            version(root / "versions" / "0.3.7")
            state["lastKnownGood"] = "0.3.7"
            expected_version = "0.3.7"
            if name == "fallback":
                state.update(pendingVersion="0.3.8", attemptId="obsolete")
            if name == "forbidden_fallback":
                state["dataCompatibility"]["versions"] = ["0.3.8"]
                expected_code = 1
        elif name in {"relative_version", "absolute_version", "external_junction"}:
            outside = case / "outside"
            version(outside)
            if name == "external_junction":
                junction(root / "versions" / "0.3.8", outside)
            else:
                state.pop("dataCompatibility")
                state["version"] = "../../outside" if name == "relative_version" else str(outside)
            expected_code = 1
        elif name == "internal_junction":
            version(root / "versions" / "0.3.7")
            junction(root / "versions" / "0.3.8", root / "versions" / "0.3.7")
            state["dataCompatibility"]["versions"] = ["0.3.8"]
            expected_code = 1
        else:
            version(root / "versions" / "0.3.8")

        if name == "wal_schema":
            connection = hold_wal(database)
            stack.callback(connection.close)
            connection.execute("PRAGMA user_version=33")
            connection.commit()
            assert struct.unpack(">I", database.read_bytes()[60:64])[0] == 32
            assert connection.execute("PRAGMA user_version").fetchone()[0] == 33
            preserved.append(Path(str(database) + "-wal"))
            row["walSchema"] = {"mainHeader": 32, "committedSql": 33}
            expected_code = 1
        if name in {"database_conflict", "active_wal_migration"}:
            state.pop("dataCompatibility")
            source = case / "local" / "holdexar"
            source_database = source / "holdexar.db"
            make_database(source_database, "legacy")
            for key in ("secret.key", "secret.master"):
                (source / key).write_bytes(f"sandbox-{key}".encode())
                preserved.append(source / key)
            preserved.append(source_database)
            if name == "database_conflict":
                expected_code = 1
            else:
                database.unlink()
                preserved.remove(database)
                connection = hold_wal(source_database)
                stack.callback(connection.close)
                connection.execute("INSERT INTO facts VALUES (2, 'committed-wal')")
                connection.commit()
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("INSERT INTO facts VALUES (3, 'uncommitted')")
                preserved.append(Path(str(source_database) + "-wal"))
        if name == "arguments_background_server":
            arguments += ["--background", "--data-dir", "", "--data-dir", '中文 two words "quoted"\\']
            for key in ("UPDATE_ATTEMPT_ID", "UPDATE_DATA_TOKEN", "LAUNCHER_PID", "DEV_URL", "UPDATE_RESTART", "PREV_PID"):
                environment[f"HOLDEXAR_{key}"] = "contract-stale"
        pointer.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        if name == "corrupt_pointer":
            pointer.write_bytes(b'{"version": "0.3.8", invalid')
            expected_code = 1
        original_pointer = pointer.read_bytes()
        original_hashes = {str(path): sha256(path) for path in preserved}
        if name == "pointer_replace_locked":
            denial = stack.enter_context(deny_replace(pointer))
            row["faultInjection"] = {"method": "readable current.json handle denies FILE_SHARE_DELETE", "replaceProbeWinError": denial}
            expected_code = 1
        code, stdout, stderr = invoke(entry, arguments, environment)
        row.update(returncode=code, expectedExitCode=expected_code, stdout=stdout[-4000:], stderr=stderr[-4000:])
        observed_path = case / "observed.json"
        assert code == expected_code, f"exit code {code}, expected {expected_code}"
        if expected_code == 1:
            assert not observed_path.exists(), "rejected case executed a version process"
            assert pointer.read_bytes() == original_pointer, "rejected case changed current.json"
        else:
            observed = json.loads(observed_path.read_text(encoding="utf-8"))
            row["observed"] = observed
            assert os.path.samefile(observed["exe"], root / "versions" / expected_version / "Holdexar.exe")
            assert observed["args"] == arguments
            assert os.path.samefile(observed["env"]["HOLDEXAR_INSTALL_ROOT"], root)
            assert os.path.samefile(observed["env"]["HOLDEXAR_DATA_DIR"], root / "data")
            assert observed["env"]["HOLDEXAR_CONTRACT_SENTINEL"] == "中文 inherited"
            assert not any(value == "contract-stale" for value in observed["env"].values())
            if name == "fallback":
                after = json.loads(pointer.read_text(encoding="utf-8"))
                assert after["version"] == expected_version
                assert after["dataCompatibility"] == state["dataCompatibility"]
                assert "pendingVersion" not in after and "attemptId" not in after
            if name == "active_wal_migration":
                migrated = sqlite3.connect(database)
                try:
                    assert migrated.execute("SELECT * FROM facts ORDER BY id").fetchall() == [(1, "legacy"), (2, "committed-wal")]
                    assert migrated.execute("PRAGMA quick_check").fetchone()[0] == "ok"
                finally:
                    migrated.close()
                for key in ("secret.key", "secret.master"):
                    assert (root / "data" / key).read_bytes() == (source / key).read_bytes()
        assert {str(path): sha256(path) for path in preserved} == original_hashes, "source/database/key content changed"
        row["preservedSha256"] = original_hashes
        if name in {"wal_schema", "active_wal_migration"}:
            row["preservationScope"] = "DB/WAL sampled while fixture writer remains open; test cleanup may checkpoint SQLite"


def main():
    parser = argparse.ArgumentParser()
    for name in ("python-launcher", "go-launcher", "rust-launcher", "version-fixture", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    options = parser.parse_args()
    if os.name != "nt":
        parser.error("Windows packaged binaries and file sharing are required")
    for name, value in vars(options).items():
        setattr(options, name, value.resolve())
    destination = options.output / f"contract-{uuid.uuid4().hex}"
    destination.mkdir(parents=True)
    report = {
        "scope": "packaged launcher protection contracts; excludes GUI latency and UpdateHost upgrade/rollback",
        "inputs": {name: {"path": str(value), "sha256": sha256(value), "bytes": value.stat().st_size}
                   for name, value in vars(options).items() if name != "output"},
        "results": [],
    }
    for mode in ("python", "go", "rust"):
        for name in VECTORS:
            row = {"mode": mode, "vector": name, "status": "failed"}
            started = time.perf_counter()
            try:
                run_vector(mode, name, options, destination, row)
                row["status"] = "passed"
            except Exception as error:
                row["error"] = f"{type(error).__name__}: {error}"
                if str(error).startswith("cleanup failed"):
                    report["results"].append(row)
                    (destination / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                    raise
            row["elapsedSeconds"] = time.perf_counter() - started
            report["results"].append(row)
            (destination / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(mode, name, row["status"], row.get("error", ""), flush=True)
    print(destination / "results.json", flush=True)
    return int(any(row["status"] != "passed" for row in report["results"]))


if __name__ == "__main__":
    raise SystemExit(main())
