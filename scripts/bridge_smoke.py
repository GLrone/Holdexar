"""serverd 引导票据桥接冒烟。

验证 --bootstrap-file 协议全链与壳侧外部服务子进程模式：
1. 协议层（无头）：起 serverd → 轮询票据文件 → 引导端点换 Cookie →
   受保护端点 200 → 同一 nonce 重放 401 → 无 Cookie 401 → 消费后票据
   文件清理 → 第二实例票据过期（60 秒）后文件清理且 nonce 失效。
2. 壳集成：desktop/main.py --server 拉起外部服务子进程 → health 就绪 →
   票据握手 → 冒烟请求 → 壳退出后子进程无孤儿。

用法：python scripts/bridge_smoke.py（需 Go 工具链；全程本机回环，无外网）
"""
from __future__ import annotations

import http.client
import json
import os
import random
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parents[1]
SERVER_GO = ROOT / "server-go"
SHELL_ENTRY = ROOT / "desktop" / "main.py"
TICKET_NAME = "server-bootstrap.ticket"  # 与 desktop/main.py 同名
# 本机策略拦截 TEMP 目录里的可执行文件：构建产物放工作树内暂存目录，收尾删除
SCRATCH = SERVER_GO / ".bridge-tmp"

_PROCS: list[subprocess.Popen] = []


def build_serverd() -> Path:
    SCRATCH.mkdir(parents=True, exist_ok=True)
    exe = SCRATCH / "serverd.exe"
    subprocess.run(
        ["go", "build", "-o", str(exe), "./cmd/serverd"],
        cwd=str(SERVER_GO),
        check=True,
    )
    return exe


def make_data_dir(work: Path) -> Path:
    """服务端不代建库：以最小空库（结构版本 34）通过启动校验；
    协议链路涉及的端点（health/info/session）不读业务表。"""
    data = work / "data"
    data.mkdir(parents=True)
    con = sqlite3.connect(data / "holdexar.db")
    try:
        con.execute("PRAGMA user_version = 34")
    finally:
        con.close()
    return data


def pick_port() -> int:
    for _ in range(50):
        port = random.randint(20000, 40000)  # 避开动态端口低端段
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("无可用端口")


def request(port: int, path: str, headers: dict | None = None) -> tuple[int, dict, bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        resp = conn.getresponse()
        body = resp.read()
        return resp.status, {k.lower(): v for k, v in resp.getheaders()}, body
    finally:
        conn.close()


def wait_health(port: int, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, _, body = request(port, "/api/v1/health")
            if status == 200:
                return json.loads(body)
            last = f"HTTP {status}"
        except OSError as exc:
            last = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"health 未就绪（{last}）")


def wait_ticket(data_dir: Path, timeout: float = 10.0) -> str:
    path = data_dir / TICKET_NAME
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            nonce = path.read_text(encoding="utf-8").strip()
        except OSError:
            nonce = ""
        if nonce:
            return nonce
        time.sleep(0.05)
    raise AssertionError(f"票据文件未出现：{path}")


def spawn_serverd(exe: Path, port: int, data_dir: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [
            str(exe),
            "-addr", f"127.0.0.1:{port}",
            "-data-dir", str(data_dir),
            "--bootstrap-file", str(data_dir / TICKET_NAME),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    _PROCS.append(proc)
    return proc


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def dump_output(proc: subprocess.Popen) -> None:
    try:
        rest = proc.stdout.read() or b""
    except (OSError, ValueError):
        rest = b""
    text = rest.decode("utf-8", "replace").strip()
    if text:
        print("---- serverd 输出 ----")
        print(text[-2000:])


def cookie_from(headers: dict) -> str:
    raw = headers.get("set-cookie", "")
    assert raw.startswith("holdexar_session="), f"引导响应未发放会话 Cookie：{raw!r}"
    assert "httponly" in raw.lower(), f"会话 Cookie 必须是 HttpOnly：{raw!r}"
    return raw.split(";", 1)[0]


def bootstrap_url(port: int, nonce: str) -> str:
    return "/api/v1/session/bootstrap?" + urlencode({"nonce": nonce})


def run_protocol_checks(port: int, data_dir: Path) -> None:
    nonce = wait_ticket(data_dir)
    assert len(nonce) >= 32, "nonce 应为高强度随机串"

    status, _, _ = request(port, "/api/v1/info")
    assert status == 401, f"无 Cookie 访问受保护端点应 401，得到 {status}"

    status, headers, _ = request(port, bootstrap_url(port, nonce))
    assert status == 303, f"引导端点应 303，得到 {status}"
    cookie = cookie_from(headers)
    assert cookie.split("=", 1)[1] != nonce, "会话令牌不得与票据 nonce 同值"

    status, _, body = request(port, "/api/v1/info", {"Cookie": cookie})
    assert status == 200, f"带 Cookie 访问受保护端点应 200，得到 {status}"
    assert json.loads(body).get("app"), "info 载荷缺少应用标识"

    assert not (data_dir / TICKET_NAME).exists(), "票据被消费后文件应立即删除"

    status, _, _ = request(port, bootstrap_url(port, nonce))
    assert status == 401, f"同一 nonce 重放应 401，得到 {status}"


def wait_expiry(port: int, data_dir: Path, nonce: str) -> None:
    """票据过期语义：服务端清理文件（票据有效期 60s + 清理宽限），nonce 随之失效。"""
    ticket = data_dir / TICKET_NAME
    deadline = time.monotonic() + 75.0
    while time.monotonic() < deadline and ticket.exists():
        time.sleep(0.5)
        print(".", end="", flush=True)
    print()
    assert not ticket.exists(), "票据过期后文件未被服务端清理"
    status, _, _ = request(port, bootstrap_url(port, nonce))
    assert status == 401, f"过期 nonce 应 401，得到 {status}"


def process_alive(pid: int) -> bool:
    # CSV 输出 + 宽松解码：中文系统 tasklist 的提示行是 GBK，严格 UTF-8 会炸
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
        capture_output=True, text=True, errors="replace", check=True,
    ).stdout or ""
    return f'"{pid}"' in out


def drain(pipe, sink: list) -> None:
    try:
        for line in pipe:
            sink.append(line)
    except (OSError, ValueError):
        pass


def run_shell_checks(exe: Path, work: Path, attempts: int = 2) -> None:
    """壳集成：--server 形态拉起外部服务子进程，壳退出后无孤儿。

    换装互斥体是跨会话全局命名互斥体，并行会话的更新链测试会瞬时持有；
    壳此时正常退位等待，表现为 health 未就绪——对这种环境碰撞重试一次。
    """
    for attempt in range(1, attempts + 1):
        try:
            _run_shell_checks_once(exe, work)
            return
        except AssertionError as exc:
            if attempt == attempts or "health 未就绪" not in str(exc):
                raise
            print(f"[壳] 第 {attempt} 次未就绪（疑似并行会话占用换装互斥体），稍后重试")
            time.sleep(10.0)


def _run_shell_checks_once(exe: Path, work: Path) -> None:
    port = pick_port()
    env = {
        **os.environ,
        "HOLDEXAR_SERVER_EXE": str(exe),
        "HOLDEXAR_PORT": str(port),
        "HOLDEXAR_LOCK_PORT": str(pick_port()),
        "PYTHONUNBUFFERED": "1",
    }
    data = make_data_dir(work / "shell")
    shell = subprocess.Popen(
        [sys.executable, str(SHELL_ENTRY), "--server", "--data-dir", str(data)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )
    lines: list[str] = []
    threading.Thread(target=drain, args=(shell.stdout, lines), daemon=True).start()
    serverd_pid = 0
    try:
        health = wait_health(port, timeout=90.0)
        serverd_pid = int(health["pid"])
        assert process_alive(serverd_pid), "外部服务进程应存活"

        nonce = wait_ticket(data)
        status, headers, _ = request(port, bootstrap_url(port, nonce))
        assert status == 303, f"引导端点应 303，得到 {status}"
        cookie = cookie_from(headers)
        status, _, _ = request(port, "/api/v1/info", {"Cookie": cookie})
        assert status == 200, f"带 Cookie 访问受保护端点应 200，得到 {status}"
        assert not (data / TICKET_NAME).exists(), "票据被消费后文件应立即删除"

        # 壳正常退出路径（Ctrl+C → 解释器收尾 → 回收子进程）
        os.kill(shell.pid, signal.CTRL_BREAK_EVENT)
        shell.wait(timeout=30)
        deadline = time.monotonic() + 15.0
        while process_alive(serverd_pid) and time.monotonic() < deadline:
            time.sleep(0.3)
        assert not process_alive(serverd_pid), "壳退出后外部服务进程未被回收（孤儿）"
    except Exception:
        print("---- 壳输出 ----")
        print("".join(lines)[-2000:])
        raise
    finally:
        if shell.poll() is None:
            shell.terminate()
            try:
                shell.wait(timeout=5)
            except subprocess.TimeoutExpired:
                shell.kill()
        # 兜底回收：壳任何异常路径留下的服务子进程都不该成为孤儿
        if serverd_pid and process_alive(serverd_pid):
            subprocess.run(
                ["taskkill", "/PID", str(serverd_pid), "/T", "/F"],
                capture_output=True,
            )


def main() -> int:
    exe = build_serverd()
    work = Path(tempfile.mkdtemp(prefix="holdexar-bridge-smoke-"))
    print(f"[环境] 工作目录 {work}")
    try:
        # 过期等待最慢（约 65s）：B 实例先起，其等待与快断言重叠
        port_b = pick_port()
        data_b = make_data_dir(work / "instance-b")
        proc_b = spawn_serverd(exe, port_b, data_b)
        wait_health(port_b)
        nonce_b = wait_ticket(data_b)
        print("[B] 票据就绪，与协议断言并行等待过期")

        port_a = pick_port()
        data_a = make_data_dir(work / "instance-a")
        proc_a = spawn_serverd(exe, port_a, data_a)
        wait_health(port_a)
        print("[A] 协议断言：单次有效 / Cookie 交接 / 消费与过期清理 / 无 Cookie 401")
        run_protocol_checks(port_a, data_a)
        stop(proc_a)
        print("[A] 协议断言全部通过")

        run_shell_checks(exe, work)
        print("[壳] 外部服务子进程拉起、票据握手、退出回收全部通过")

        print("[B] 等待票据过期（约 60-65s）")
        wait_expiry(port_b, data_b, nonce_b)
        print("[B] 过期清理与失效断言通过")

        print("桥接冒烟全部通过")
        return 0
    except AssertionError as exc:
        print(f"[失败] {exc}")
        # 先收进程再倒输出：常驻服务进程不退出，stdout.read() 会永不返回
        for proc in _PROCS:
            stop(proc)
        for proc in _PROCS:
            dump_output(proc)
        return 1
    finally:
        for proc in _PROCS:
            stop(proc)
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(SCRATCH, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
