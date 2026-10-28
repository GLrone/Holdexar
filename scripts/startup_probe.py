"""Measure an isolated Windows installation from launch request through a CDP click."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import queue
import statistics
import subprocess
import threading
import time
import urllib.request

TARGET_COMPONENTS = {
    "/library": '.navbar .search-box[data-tour="nav-search"]',
    "/dashboard": ".dashboard-page",
    "/about": ".about-page",
    "/pool": ".pool-page",
}


def sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def classify_frame(rgb: bytes) -> dict:
    from PIL import Image

    if not rgb or len(rgb) % 3:
        raise ValueError("Expected packed RGB pixels")
    size = len(rgb) // 3
    palette = Image.frombytes("RGB", (size, 1), rgb).getcolors(size)
    white = sum(count for count, pixel in palette if min(pixel) >= 246) / size
    navy = sum(count for count, pixel in palette if max(abs(a - b) for a, b in
               zip(pixel, (27, 40, 56))) <= 7) / size
    colors = len({tuple(channel // 24 for channel in pixel) for _, pixel in palette})
    label = ("white_blank" if white >= .995 else "navy_blank" if navy >= .995
             else "ambiguous_frame" if max(white, navy) >= .98
             else "painted_candidate" if colors >= 2 else "ambiguous_frame")
    return {"label": label, "white_fraction": round(white, 4),
            "navy_fraction": round(navy, 4), "coarse_colors": colors}


def read_json(url: str, timeout: float = .15):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.load(response)


def inside(path: str | Path, root: Path) -> bool:
    try:
        return Path(path).resolve().is_relative_to(root.resolve())
    except (OSError, ValueError, TypeError):
        return False


def valid_health(value: dict, root: Path, pids: set[int]) -> bool:
    return (value.get("app") == "Holdexar" and value.get("pid") in pids
            and inside(value.get("executable", ""), root)
            and Path(value.get("dataDir", "")).resolve() == (root / "data").resolve())


def document_expression(route: str) -> str:
    selector = '[data-tour="sb-' + route.strip("/") + '"]'
    return """(() => {
      const main = document.querySelector('.view-container');
      const navigation = document.querySelector('.hl-sidebar nav');
      const nr = navigation?.getBoundingClientRect();
      const button = document.querySelector(NAV_SELECTOR);
      const r = button?.getBoundingClientRect();
      const x = r ? r.x + r.width / 2 : -1, y = r ? r.y + r.height / 2 : -1;
      const marker = main?.querySelector(PAGE_SELECTOR);
      const root = marker?.closest('.view-container > *');
      const mr = marker?.getBoundingClientRect(), rr = root?.getBoundingClientRect();
      let visible = !!rr && rr.width > 0 && rr.height > 0 && !!mr && mr.width > 0 && mr.height > 0
        && mr.right > 0 && mr.bottom > 0 && mr.left < innerWidth && mr.top < innerHeight;
      let entering = false, leaving = false;
      for (let element = marker; element; element = element.parentElement) {
        const style = getComputedStyle(element);
        visible = visible && style.display !== 'none' && style.visibility === 'visible' && Number(style.opacity) > 0;
        entering ||= [...element.classList].some(name => name.startsWith('route-fade-enter-'));
        leaving ||= [...element.classList].some(name => name.startsWith('route-fade-leave-'));
        if (element === main) break;
      }
      return {path:location.pathname, ready:document.readyState,
        kind:document.querySelector('.hl-sidebar') ? 'vue' : document.querySelector('.spin') ? 'splash' : 'other',
        navigation:navigation?.innerText || '',
        navigationVisible:!!nr && nr.width > 0 && nr.height > 0 &&
          getComputedStyle(navigation).visibility !== 'hidden',
        content:main?.innerText || '',
        target_component:{selector:PAGE_SELECTOR, present:!!root, visible, entering, leaving,
          characters:(root?.innerText || '').trim().length, rootClasses:root?.className || '',
          rect:rr ? {x:rr.x,y:rr.y,width:rr.width,height:rr.height} : null},
        button:r && r.width > 0 && r.height > 0 && x >= 0 && y >= 0 &&
          x < innerWidth && y < innerHeight && button.contains(document.elementFromPoint(x,y))
          ? {x,y} : null};
    })()""".replace("NAV_SELECTOR", json.dumps(selector)).replace("PAGE_SELECTOR", json.dumps(TARGET_COMPONENTS[route]))


def vue_navigation_ready(document: dict) -> bool:
    return (document.get("kind") == "vue" and document.get("navigationVisible")
            and bool(document.get("navigation", "").strip()))


def target_component_ready(document: dict, route: str) -> bool:
    target = document.get("target_component", {})
    return (route in TARGET_COMPONENTS and target.get("selector") == TARGET_COMPONENTS[route]
            and target.get("present") is True and target.get("visible") is True
            and target.get("entering") is False and target.get("leaving") is False
            and (target.get("characters") or 0) > 0)


def interaction_succeeded(before: dict, after: dict, target_route: str) -> bool:
    return (before.get("kind") == "vue" and before.get("path") != target_route
            and after.get("kind") == "vue" and after.get("path") == target_route
            and target_component_ready(after, target_route)
            and bool(after.get("content", "").strip())
            and after.get("content") != before.get("content"))


class Cdp:
    def __init__(self, port: int, origin: str, deadline: float = math.inf):
        import websocket

        self.deadline = deadline
        remaining = min(1, deadline - time.perf_counter())
        if remaining <= 0:
            raise TimeoutError("CDP overall sampling deadline expired")
        targets = read_json(f"http://127.0.0.1:{port}/json/list", timeout=min(.15, remaining))
        matches = [target for target in targets if target.get("type") == "page"
                   and target.get("url", "").startswith(origin + "/")]
        if len(matches) != 1:
            raise ValueError("No unique application CDP page")
        remaining = min(1, deadline - time.perf_counter())
        if remaining <= 0:
            raise TimeoutError("CDP overall sampling deadline expired")
        try:
            self.socket = websocket.create_connection(matches[0]["webSocketDebuggerUrl"],
                                                      timeout=remaining, suppress_origin=True,
                                                      http_no_proxy=["127.0.0.1", "localhost"])
        except websocket.WebSocketTimeoutException as exc:
            raise TimeoutError("CDP connection timed out") from exc
        self.sequence = 0
        try:
            self.call("Performance.enable")
        except Exception:
            self.close()
            raise

    def call(self, method: str, params: dict | None = None):
        import websocket

        self.sequence += 1
        deadline = min(self.deadline, time.perf_counter() + 1)
        try:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise TimeoutError(f"CDP {method} exceeded its deadline")
            self.socket.settimeout(remaining)
            self.socket.send(json.dumps({"id": self.sequence, "method": method, "params": params or {}}))
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    raise TimeoutError(f"CDP {method} exceeded its deadline")
                self.socket.settimeout(remaining)
                message = json.loads(self.socket.recv())
                if message.get("id") == self.sequence:
                    if "error" in message:
                        raise RuntimeError(message["error"])
                    return message.get("result", {})
        except websocket.WebSocketTimeoutException as exc:
            raise TimeoutError(f"CDP {method} timed out") from exc
        except websocket.WebSocketConnectionClosedException as exc:
            raise ConnectionError(f"CDP {method} disconnected") from exc

    def document(self, route: str):
        result = self.call("Runtime.evaluate", {"expression": document_expression(route),
                                                "returnByValue": True})
        return result["result"]["value"]

    def painted_document(self, route: str):
        # DOM visibility can precede the WebView2 compositor replacing the HTML loader.
        self.call("Runtime.evaluate", {"expression": "new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve(true))))",
                                       "awaitPromise": True, "returnByValue": True})
        return self.document(route)

    def click(self, point: dict):
        parameters = {"x": point["x"], "y": point["y"], "button": "left", "clickCount": 1}
        try:
            self.call("Input.dispatchMouseEvent", {"type": "mousePressed", **parameters})
        finally:
            self.call("Input.dispatchMouseEvent", {"type": "mouseReleased", **parameters})

    def metrics(self):
        values = self.call("Performance.getMetrics").get("metrics", [])
        entries = self.call("Runtime.evaluate", {"returnByValue": True, "expression": """(() => {
          if (!window.__hlxStartupProbeTasks && typeof PerformanceObserver !== 'undefined') {
            window.__hlxStartupProbeTasks = [];
            try {
              const observer = new PerformanceObserver(list => {
                for (const entry of list.getEntries()) window.__hlxStartupProbeTasks.push({
                  startTime:entry.startTime, duration:entry.duration});
              });
              observer.observe({type:'longtask', buffered:true});
              window.__hlxStartupProbeTaskObserver = observer;
            } catch (_) {}
          }
          const resource = performance.getEntriesByType('resource')
            .filter(entry => ['script','link','css'].includes(entry.initiatorType))
            .map(entry => ({path:new URL(entry.name, location.href).pathname,
              initiatorType:entry.initiatorType, startTime:entry.startTime, duration:entry.duration,
              responseEnd:entry.responseEnd, transferSize:entry.transferSize,
              decodedBodySize:entry.decodedBodySize}));
          const navigation = performance.getEntriesByType('navigation').map(entry => ({
            startTime:entry.startTime, responseEnd:entry.responseEnd,
            domInteractive:entry.domInteractive, domContentLoadedEventEnd:entry.domContentLoadedEventEnd,
            loadEventEnd:entry.loadEventEnd}));
          return {timeOrigin:performance.timeOrigin, now:performance.now(),
            resource, navigation, longTasks:window.__hlxStartupProbeTasks || [],
            longTaskSupported:typeof PerformanceObserver !== 'undefined' &&
              PerformanceObserver.supportedEntryTypes.includes('longtask')};
        })()"""}).get("result", {}).get("value", {})
        return {"performance_metrics": values, "page_entries": entries}

    def close(self):
        self.socket.close(timeout=.1)


def enable_physical_pixels(api) -> dict:
    signatures = {
        "SetProcessDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_int),
        "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
        "GetThreadDpiAwarenessContext": ([], ctypes.c_void_p),
        "AreDpiAwarenessContextsEqual": ([ctypes.c_void_p, ctypes.c_void_p], ctypes.c_int),
        "GetAwarenessFromDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_int),
    }
    try:
        for name, (arguments, result) in signatures.items():
            function = getattr(api, name)
            function.argtypes, function.restype = arguments, result
    except AttributeError as exc:
        raise RuntimeError("PER_MONITOR_AWARE_V2 DPI APIs are unavailable") from exc
    desired = ctypes.c_void_p(-4)
    ctypes.set_last_error(0)
    process_set = bool(api.SetProcessDpiAwarenessContext(desired))
    process_error = 0 if process_set else ctypes.get_last_error()
    if not process_set and process_error != 5:
        raise RuntimeError(f"DPI process configuration failed (Windows error {process_error})")
    context = api.GetThreadDpiAwarenessContext()
    overridden = not bool(api.AreDpiAwarenessContextsEqual(context, desired))
    if overridden:
        if not api.SetThreadDpiAwarenessContext(desired):
            raise RuntimeError(f"DPI thread configuration failed (Windows error {ctypes.get_last_error()})")
        context = api.GetThreadDpiAwarenessContext()
    awareness = api.GetAwarenessFromDpiAwarenessContext(context)
    if not api.AreDpiAwarenessContextsEqual(context, desired) or awareness != 2:
        raise RuntimeError("DPI context is not verified PER_MONITOR_AWARE_V2")
    return {"requested": "PER_MONITOR_AWARE_V2", "verified": True,
            "process_set": process_set, "process_error": process_error,
            "thread_overridden": overridden, "thread_context": context, "thread_awareness": awareness}


class WindowsScreen:
    def __init__(self):
        from ctypes import wintypes as wt

        self.wt = wt
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.dpi = enable_physical_pixels(self.api)
        self.capture_metadata = {}
        self.window_state = {}
        self.api.EnumWindows.argtypes = [ctypes.c_void_p, wt.LPARAM]
        self.api.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
        self.api.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        self.api.IsWindowVisible.argtypes = [wt.HWND]
        self.api.IsIconic.argtypes = [wt.HWND]
        self.api.GetWindowLongPtrW.argtypes = [wt.HWND, ctypes.c_int]
        self.api.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self.api.GetLayeredWindowAttributes.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD),
                                                       ctypes.POINTER(wt.BYTE), ctypes.POINTER(wt.DWORD)]
        self.api.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        self.api.ClientToScreen.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
        self.api.GetForegroundWindow.restype = wt.HWND
        self.api.GetDpiForWindow.argtypes = [wt.HWND]
        self.api.GetDpiForWindow.restype = wt.UINT

    def visibility(self, hwnd) -> dict:
        visible, minimized = bool(self.api.IsWindowVisible(hwnd)), bool(self.api.IsIconic(hwnd))
        layered = bool(self.api.GetWindowLongPtrW(hwnd, -20) & 0x80000)
        alpha, flags, readable = self.wt.BYTE(255), self.wt.DWORD(0), True
        if layered:
            color = self.wt.DWORD()
            readable = bool(self.api.GetLayeredWindowAttributes(
                hwnd, ctypes.byref(color), ctypes.byref(alpha), ctypes.byref(flags)))
        opaque = not layered or readable and flags.value == 2 and alpha.value == 255
        return {"visible": visible, "minimized": minimized, "layered": layered,
                "alpha": alpha.value if readable else None, "layered_flags": flags.value,
                "opacity_verified": bool(opaque), "eligible": bool(visible and not minimized and opaque)}

    def find(self, pids: set[int], title: str, root: Path):
        import psutil

        found, self.window_state = [], {}
        callback_type = ctypes.WINFUNCTYPE(self.wt.BOOL, self.wt.HWND, self.wt.LPARAM)

        def inspect(hwnd, _):
            name = ctypes.create_unicode_buffer(512)
            self.api.GetWindowTextW(hwnd, name, len(name))
            if name.value == title:
                pid = self.wt.DWORD()
                self.api.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                try:
                    if pid.value in pids or inside(psutil.Process(pid.value).exe(), root):
                        pids.add(pid.value)
                        self.window_state = {"hwnd": hwnd, "pid": pid.value, **self.visibility(hwnd)}
                        if self.window_state["eligible"]:
                            found.append((hwnd, pid.value))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return True

        callback = callback_type(inspect)
        self.api.EnumWindows(ctypes.cast(callback, ctypes.c_void_p), 0)
        return found[0] if len(found) == 1 else None

    def foreground(self, hwnd) -> bool:
        """Bring an eligible window to the foreground so pixel capture is possible.

        A launch chain from a background console is denied foreground activation by
        Windows; measurement must not proceed on background windows, so this only
        assists the OS, and capture still verifies foreground itself.
        """
        if self.api.GetForegroundWindow() == hwnd:
            return True
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        this_thread = kernel32.GetCurrentThreadId()
        target_thread = self.api.GetWindowThreadProcessId(hwnd, None)
        for _ in range(3):
            foreground_window = self.api.GetForegroundWindow()
            foreground_thread = (self.api.GetWindowThreadProcessId(foreground_window, None)
                                 if foreground_window else 0)
            attached = [thread for thread in {foreground_thread, target_thread}
                        if thread and thread != this_thread]
            for thread in attached:
                self.api.AttachThreadInput(this_thread, thread, True)
            try:
                self.api.BringWindowToTop(hwnd)
                self.api.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x43)
                self.api.SetForegroundWindow(hwnd)
                if self.api.GetForegroundWindow() == hwnd:
                    return True
                try:
                    self.api.SwitchToThisWindow(hwnd, True)
                except AttributeError:
                    pass
                if self.api.GetForegroundWindow() == hwnd:
                    return True
                self.api.keybd_event(0x12, 0, 0, 0)
                self.api.SetForegroundWindow(hwnd)
                self.api.keybd_event(0x12, 0, 2, 0)
                if self.api.GetForegroundWindow() == hwnd:
                    return True
            finally:
                for thread in attached:
                    self.api.AttachThreadInput(this_thread, thread, False)
            time.sleep(.25)
        return self.api.GetForegroundWindow() == hwnd

    def capture(self, hwnd):
        from PIL import ImageGrab

        verified = bool(self.api.AreDpiAwarenessContextsEqual(
            self.api.GetThreadDpiAwarenessContext(), ctypes.c_void_p(-4)))
        self.capture_metadata = {"dpi_verified": verified, "foreground_before": self.api.GetForegroundWindow(),
                                 "window_dpi": self.api.GetDpiForWindow(hwnd),
                                 "visibility_before": self.visibility(hwnd)}
        if not verified:
            raise RuntimeError("DPI context changed before screen capture")
        if self.capture_metadata["foreground_before"] != hwnd:
            self.capture_metadata["reason"] = "Target window is not foreground"
            return None
        if not self.capture_metadata["visibility_before"]["eligible"]:
            self.capture_metadata["reason"] = "Target window is hidden, minimized, transparent, or opacity is unverified"
            return None
        rect = self.wt.RECT()
        point = self.wt.POINT(0, 0)
        if not self.api.GetClientRect(hwnd, ctypes.byref(rect)) or rect.right < 300 or rect.bottom < 200:
            self.capture_metadata["reason"] = "Client rectangle is unavailable or too small"
            return None
        if not self.api.ClientToScreen(hwnd, ctypes.byref(point)):
            self.capture_metadata["reason"] = "Client physical screen origin is unavailable"
            return None
        bbox = (point.x, point.y, point.x + rect.right, point.y + rect.bottom)
        self.capture_metadata["bbox_physical"] = bbox
        image = ImageGrab.grab(bbox, include_layered_windows=True).convert("RGB")
        self.capture_metadata["foreground_after"] = self.api.GetForegroundWindow()
        self.capture_metadata["visibility_after"] = self.visibility(hwnd)
        if self.capture_metadata["foreground_after"] != hwnd:
            self.capture_metadata["reason"] = "Foreground changed during capture"
            return None
        if (self.capture_metadata["visibility_before"] != self.capture_metadata["visibility_after"]
                or not self.capture_metadata["visibility_after"]["eligible"]):
            self.capture_metadata["reason"] = "Window visibility or opacity changed during capture"
            return None
        if image.size != (rect.right, rect.bottom):
            raise RuntimeError("DPI capture size does not match the physical client rectangle")
        return image


def processes(parent_pids: set[int] | None = None):
    import psutil
    from ctypes import wintypes as wt

    class Entry(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("pid", wt.DWORD),
                    ("heap", ctypes.c_size_t), ("module", wt.DWORD), ("threads", wt.DWORD),
                    ("ppid", wt.DWORD), ("priority", wt.LONG), ("flags", wt.DWORD),
                    ("name", wt.WCHAR * 260)]

    result = []
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wt.HANDLE
    kernel.Process32FirstW.argtypes = kernel.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(Entry)]
    kernel.CloseHandle.argtypes = [wt.HANDLE]
    handle = kernel.CreateToolhelp32Snapshot(2, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    snapshot, entry = [], Entry()
    entry.dwSize = ctypes.sizeof(entry)
    try:
        more = kernel.Process32FirstW(handle, ctypes.byref(entry))
        while more:
            snapshot.append({"pid": entry.pid, "ppid": entry.ppid, "name": entry.name})
            more = kernel.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel.CloseHandle(handle)
    selected = set(parent_pids or ())
    selected.update(process["pid"] for process in snapshot if process["name"].lower()
                    in {"holdexar.exe", "updatehost.exe"})
    while True:
        children = {process["pid"] for process in snapshot if process["ppid"] in selected}
        if children.issubset(selected):
            break
        selected.update(children)
    for process in snapshot:
        if process["pid"] not in selected:
            continue
        try:
            actual = psutil.Process(process["pid"])
            result.append({**process, "exe": actual.exe(), "create_time": actual.create_time()})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return result


def probe(args) -> dict:
    import psutil

    if os.name != "nt":
        raise RuntimeError("A real Windows desktop is required")
    root, launcher = args.expected_root.resolve(), args.launcher.resolve()
    if not args.confirm_isolated or not launcher.is_file() or not inside(launcher, root):
        raise ValueError("A launcher inside an explicitly confirmed isolated installation is required")
    if args.target_route not in TARGET_COMPONENTS:
        raise ValueError("Only read-only navigation targets are supported")
    if args.out.exists():
        raise ValueError("Output directory must be new")
    existing = processes()
    resident = [process for process in existing if inside(process["exe"], root)]
    other = [process for process in existing if (process["name"] or "").lower() in {"holdexar.exe", "updatehost.exe"}]
    if args.mode != "activation" and other:
        raise ValueError("Process-cold and cache-warm samples require all Holdexar processes to exit")
    if args.mode == "activation" and not resident:
        raise ValueError("Activation requires a resident instance inside the isolated root")
    environment = os.environ.copy()
    profile = root / "_userprofile"
    environment.update({"LOCALAPPDATA": str(root / "_localappdata"),
                        "APPDATA": str(root / "_appdata"), "USERPROFILE": str(profile),
                        "HOMEDRIVE": profile.drive, "HOMEPATH": str(profile)[len(profile.drive):],
                        "HOLDEXAR_DATA_DIR": str(root / "data"),
                        "HOLDEXAR_PORT": str(args.port), "HOLDEXAR_LOCK_PORT": str(args.lock_port),
                        "WEBVIEW2_USER_DATA_FOLDER": str(root / "_webview2")})
    if args.cdp_port:
        environment["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            "--disable-component-update --disable-background-networking "
            f"--remote-debugging-port={args.cdp_port} --remote-debugging-address=127.0.0.1")
    args.out.mkdir(parents=True)
    screen, events, lock = WindowsScreen(), [], threading.Lock()
    frame_requests = queue.Queue()
    stop, painted, interactive = threading.Event(), threading.Event(), threading.Event()
    pids = {process["pid"] for process in resident}
    seen, resources, responses, errors, screen_samples, scan_durations, cdp_metrics = {}, {}, [], [], [], [], []
    t0_unix, t0 = time.time(), time.perf_counter()

    def emit(event: str, **data):
        record = {"event": event, "ms": round((time.perf_counter() - t0) * 1000, 3), **data}
        with lock:
            events.append(record)
        print(json.dumps(record, ensure_ascii=False), flush=True)
        return record["ms"]

    def request_frame(reason: str, *, wait: bool = False) -> bool:
        request = {"reason": reason, "done": threading.Event(), "captured": False}
        frame_requests.put(request)
        if wait:
            request["done"].wait(max(0, min(1, t0 + args.timeout - time.perf_counter())))
        return request["captured"]

    command = [str(launcher), *( ["--server"] if args.server else []), *args.extra]
    child = subprocess.Popen(command, cwd=str(root), env=environment)
    pids.add(child.pid)
    emit("launcher_created", pid=child.pid)
    origin = f"http://127.0.0.1:{args.port}"

    def health_worker():
        while not stop.is_set():
            try:
                value = read_json(origin + "/api/v1/health")
                if value.get("pid") not in pids:
                    process = psutil.Process(value.get("pid", -1))
                    if inside(process.exe(), root):
                        pids.add(process.pid)
                if valid_health(value, root, pids):
                    emit("health_identity", **{key: value.get(key) for key in
                         ("pid", "executable", "dataDir", "version", "schedulerReady", "writeProtected", "pageReady")})
                    return
            except (OSError, ValueError, TypeError, psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            stop.wait(.1)

    def process_worker():
        prefix = str(root).lower() + os.sep
        while not stop.is_set():
            scan_start = time.perf_counter()
            for process in processes(pids):
                if process["exe"].lower().startswith(prefix) or process["ppid"] in pids:
                    pids.add(process["pid"])
                    if process["pid"] not in seen:
                        seen[process["pid"]] = process
                        emit("process_observed", **process,
                             created_ms=round((process["create_time"] - t0_unix) * 1000, 3))
            for pid in pids.copy():
                try:
                    process = psutil.Process(pid)
                    cpu, memory, io = process.cpu_times(), process.memory_info(), process.io_counters()
                    last = resources.setdefault(pid, {"cpu_seconds": 0, "peak_private_bytes": 0,
                                                      "peak_working_set_bytes": 0})
                    last.update(cpu_seconds=max(last["cpu_seconds"], cpu.user + cpu.system),
                                peak_private_bytes=max(last["peak_private_bytes"], getattr(memory, "private", 0)),
                                peak_working_set_bytes=max(last["peak_working_set_bytes"], memory.rss),
                                read_bytes=io.read_bytes, write_bytes=io.write_bytes,
                                page_faults=getattr(memory, "num_page_faults", None))
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            scan_durations.append(round((time.perf_counter() - scan_start) * 1000, 3))
            stop.wait(.2)

    def cdp_worker():
        connection, before, vue_seen, main_seen, clicked, next_response = None, None, False, False, False, 0
        try:
            while not stop.is_set() and time.perf_counter() < t0 + args.timeout:
                try:
                    if connection is None:
                        connection = Cdp(args.cdp_port, origin, t0 + args.timeout)
                        emit("cdp_connected")
                    start = time.perf_counter()
                    document = connection.document(args.target_route)
                    now = time.perf_counter()
                    if vue_navigation_ready(document) and painted.is_set() and not vue_seen:
                        vue_seen = True
                        emit("vue_document", path=document["path"],
                             characters=len(document["navigation"]), content="navigation_shell")
                        request_frame("vue_document")
                    if document["kind"] == "vue" and document["content"].strip() and not main_seen:
                        main_seen = True
                        emit("main_content_ready", path=document["path"], characters=len(document["content"]))
                    if not clicked and vue_seen and painted.is_set() and document.get("button"):
                        document = connection.painted_document(args.target_route)
                        if not vue_navigation_ready(document) or not document.get("button"):
                            continue
                        if not request_frame("before_navigation_click", wait=True):
                            continue
                        before = connection.document(args.target_route)
                        if not vue_navigation_ready(before) or not before.get("button"):
                            continue
                        if before["path"] == args.target_route:
                            raise ValueError("Navigation target is already active; choose another target")
                        clicked = True
                        try:
                            connection.click(before["button"])
                            emit("navigation_click_dispatched", target=args.target_route,
                                 point=before["button"], before_path=before["path"])
                        finally:
                            request_frame("after_navigation_click")
                    elif clicked and not interactive.is_set() and interaction_succeeded(before, document, args.target_route):
                        emit("interaction_dom_confirmed", target=args.target_route, characters=len(document["content"]),
                             target_component=document["target_component"])
                        interactive.set()
                        request_frame("interaction_confirmed")
                    if now >= next_response:
                        responses.append({"ms": round((now - t0) * 1000, 3),
                                          "roundtrip_ms": round((now - start) * 1000, 3)})
                        cdp_metrics.append({"ms": round((now - t0) * 1000, 3), **connection.metrics()})
                        next_response = now + 1
                except (OSError, ValueError, KeyError, RuntimeError) as exc:
                    if connection is not None:
                        errors.append(str(exc))
                        emit("cdp_retry", error=str(exc))
                        connection.close()
                        connection = None
                stop.wait(.05)
        except Exception as exc:
            errors.append(f"CDP unavailable: {type(exc).__name__}: {exc}")
        finally:
            if connection is not None:
                connection.close()

    workers = [threading.Thread(target=health_worker, daemon=True),
               threading.Thread(target=process_worker, daemon=True)]
    if args.cdp_port and not args.server:
        workers.append(threading.Thread(target=cdp_worker, daemon=True))
    for worker in workers:
        worker.start()
    previous, window, frames, interaction_frame_saved, last_window_state = None, None, 0, False, None
    try:
        while time.perf_counter() - t0 < args.timeout:
            now = time.perf_counter()
            requests, frame_saved = [], False
            while not frame_requests.empty():
                requests.append(frame_requests.get_nowait())
            if not args.server:
                candidate = screen.find(pids, args.title, root)
                if candidate and candidate != window:
                    window = candidate
                    focused = screen.foreground(window[0])
                    emit("window_visible", foreground_requested=not focused, **screen.window_state)
                elif not candidate and screen.window_state and screen.window_state != last_window_state:
                    emit("window_unverified", **screen.window_state)
                last_window_state = screen.window_state.copy()
                if window:
                    image = screen.capture(window[0])
                    if (image is None
                            and screen.capture_metadata.get("reason") == "Target window is not foreground"):
                        screen.foreground(window[0])
                        image = screen.capture(window[0])
                    if image is not None:
                        state = classify_frame(image.resize((240, 160)).tobytes())
                        label = state["label"]
                        screen_samples.append({"ms": round((time.perf_counter() - t0) * 1000, 3),
                                               "label": label, **screen.capture_metadata})
                        if label == "painted_candidate":
                            painted.set()
                        else:
                            painted.clear()
                        if label != previous or requests:
                            if frames < args.max_frames:
                                filename = f"frame_{frames:02d}_{label}.png"
                                emit("screen_frame", **state, **screen.capture_metadata,
                                     requested_events=[request["reason"] for request in requests],
                                     after_interaction=interactive.is_set(), file=filename)
                                image.save(args.out / filename)
                                frame_saved = True
                                frames += 1
                                if (interactive.is_set() and label == "painted_candidate" and not interaction_frame_saved
                                        and any(request["reason"] == "interaction_confirmed" for request in requests)):
                                    emit("interaction_confirmed", target=args.target_route, file=filename)
                                    interaction_frame_saved = True
                            previous = label
                    elif previous != "not_foreground":
                        painted.clear()
                        emit("screen_unverified", **screen.capture_metadata)
                        previous = "not_foreground"
            for request in requests:
                request["captured"] = frame_saved
                request["done"].set()
                if not frame_saved:
                    emit("screen_event_unverified", requested_event=request["reason"])
            if (now - t0 >= args.observe_seconds and (interaction_frame_saved or args.server)
                    and frame_requests.empty()):
                break
            stop.wait(args.interval)
    except Exception as exc:
        errors.append(f"Screen sampling stopped: {type(exc).__name__}: {exc}")
        emit("measurement_failed", error=errors[-1])
    finally:
        stop.set()
        for worker in workers:
            worker.join(timeout=1)
    artifacts = {str(path.relative_to(root)): sha256(path) for path in
                 [launcher, *sorted((root / "versions").glob("*/Holdexar.exe")),
                  *sorted((root / "versions").glob("*/_internal/web/index.html"))] if path.is_file()}
    frontend = {}
    for directory in sorted((root / "versions").glob("*/_internal/web")):
        manifest = [(str(path.relative_to(directory)), sha256(path)) for path in sorted(directory.rglob("*"))
                    if path.is_file() and path.suffix in {".html", ".js", ".css"}]
        frontend[str(directory.relative_to(root))] = {
            "file_count": len(manifest),
            "manifest_sha256": hashlib.sha256(json.dumps(manifest).encode()).hexdigest()}
    report = {"schema": 1, "label": args.label or root.name, "mode": args.mode, "server": args.server,
              "time_origin": "perf_counter immediately before subprocess.Popen",
              "t0_perf_counter": t0, "t0_unix": t0_unix,
              "os_cache": "uncontrolled; no cache flush or prewarming",
              "root": str(root), "launcher": str(launcher), "artifacts_sha256": artifacts,
              "frontend": frontend,
              "interactive_confirmed": interactive.is_set() and interaction_frame_saved,
              "dom_interaction_confirmed": interactive.is_set(),
              "interaction_method": "CDP click, visible settled target component, then saved opaque client frame",
              "vue_document_definition": "Navigation shell text plus opaque client painted candidate; statistics not awaited; frame review required",
              "screen_evidence": "Foreground client pixels; categories are candidates requiring frame review",
              "screen_contract": 3,
              "dpi": screen.dpi,
              "interval_seconds": args.interval, "events": sorted(events, key=lambda item: item["ms"]),
              "resources_by_pid": resources, "cdp_roundtrips": responses,
              "cdp_metrics": cdp_metrics,
              "process_scan_durations_ms": scan_durations,
              "screen_samples": screen_samples,
              "max_screen_sample_gap_ms": max((b["ms"] - a["ms"] for a, b in
                                                zip(screen_samples, screen_samples[1:])), default=None),
              "errors": list(dict.fromkeys(errors)), "limitations": [
                  "No process is terminated; exit the fixture before the next non-activation sample",
                  "Sampling can miss shorter flashes; DOM and screenshots have separate clocks",
                  "CDP adds diagnostic overhead and measures browser input, not physical mouse latency",
                  "Page faults include soft faults; hard faults and disk cache misses are unverified",
                  "CPU and IO counters are lifetime counters; activation includes resident history",
                  "Process discovery may miss short-lived children"]}
    (args.out / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def summarize(files: list[Path]) -> dict:
    groups = {}
    for file in files:
        report = json.loads(file.read_text(encoding="utf-8"))
        key = f"{report.get('label', 'unlabeled')}:{report['mode']}:{'server' if report.get('server') else 'gui'}"
        group = groups.setdefault(key, {"samples": 0, "interactive_failures": 0,
                                        "invalid_pixel_samples": 0, "metrics": {}})
        group["samples"] += 1
        valid_pixels = report.get("dpi", {}).get("verified") is True and report.get("screen_contract", 0) >= 2
        valid_interaction = valid_pixels and report.get("screen_contract", 0) >= 3 and report.get("interactive_confirmed")
        group["invalid_pixel_samples"] += not valid_pixels and not report.get("server")
        group["interactive_failures"] += not valid_interaction and not report.get("server")
        first = {}
        for event in report["events"]:
            name = event["event"]
            if (not valid_pixels and name in {"screen_frame", "interaction_confirmed"}
                    or name == "interaction_confirmed" and not valid_interaction):
                continue
            if name == "screen_frame":
                if event["label"] != "painted_candidate":
                    continue
                name = "first_painted_candidate"
            if name in {"first_painted_candidate", "window_visible", "health_identity",
                        "vue_document", "main_content_ready", "interaction_confirmed"}:
                first.setdefault(name, event["ms"])
        for name, value in first.items():
            group["metrics"].setdefault(name, []).append(value)
    for group in groups.values():
        group["metrics"] = {name: {"count": len(values), "p50_ms": statistics.median(values),
                                  "p90_ms": sorted(values)[math.ceil(.9 * len(values)) - 1],
                                  "max_ms": max(values)} for name, values in group["metrics"].items()}
    return groups


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launcher", type=Path)
    parser.add_argument("--expected-root", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--label")
    parser.add_argument("--confirm-isolated", action="store_true")
    parser.add_argument("--mode", choices=("cold", "cache-warm", "activation"), default="cold")
    parser.add_argument("--port", type=int, default=28931)
    parser.add_argument("--lock-port", type=int, default=29131)
    parser.add_argument("--cdp-port", type=int)
    parser.add_argument("--title", default="Holdexar")
    parser.add_argument("--target-route", default="/library")
    parser.add_argument("--server", action="store_true")
    parser.add_argument("--timeout", type=float, default=45)
    parser.add_argument("--observe-seconds", type=float, default=16)
    parser.add_argument("--interval", type=float, default=.04)
    parser.add_argument("--max-frames", type=int, default=16)
    parser.add_argument("--summarize", nargs="+", type=Path)
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.summarize:
        print(json.dumps(summarize(args.summarize), ensure_ascii=False, indent=2))
    elif not all((args.launcher, args.expected_root, args.out)):
        parser.error("--launcher, --expected-root and --out are required")
    else:
        if args.extra[:1] == ["--"]:
            args.extra.pop(0)
        probe(args)


if __name__ == "__main__":
    main()
