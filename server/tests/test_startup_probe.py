import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("startup_probe", ROOT / "scripts" / "startup_probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


@pytest.mark.parametrize(("color", "label"), [((255, 255, 255), "white_blank"),
                                               ((27, 40, 56), "navy_blank"),
                                               ((0, 0, 0), "ambiguous_frame")])
def test_background_is_never_content(color, label):
    assert probe.classify_frame(bytes(color) * 100)["label"] == label


def test_splash_pixels_are_only_candidates():
    pixels = bytes((27, 40, 56)) * 90 + bytes((102, 192, 244)) * 10
    assert probe.classify_frame(pixels)["label"] == "painted_candidate"
    with pytest.raises(ValueError):
        probe.classify_frame(b"\x00")


def test_white_background_with_small_pointer_halo_is_not_content():
    pixels = bytes((255, 255, 255)) * 9935 + bytes((102, 192, 244)) * 65
    result = probe.classify_frame(pixels)
    assert result["white_fraction"] == .9935
    assert result["label"] == "ambiguous_frame"


def test_interaction_requires_click_target_and_changed_page_content():
    before = {"kind": "vue", "path": "/dashboard", "content": "Dashboard"}
    after = {"kind": "vue", "path": "/library", "content": "Games", "target_component": {
        "selector": '.navbar .search-box[data-tour="nav-search"]', "present": True,
        "visible": True, "entering": False, "leaving": False, "characters": 5}}
    assert probe.interaction_succeeded(before, after, "/library")
    assert not probe.interaction_succeeded(before, {**after, "content": ""}, "/library")
    assert not probe.interaction_succeeded(before, {**after, "content": "Dashboard"}, "/library")
    assert not probe.interaction_succeeded(before, {**after, "path": "/about"}, "/library")
    assert not probe.interaction_succeeded({**before, "kind": "splash"}, after, "/library")
    assert not probe.interaction_succeeded(after, after, "/library")


def test_route_change_and_old_dashboard_updates_are_not_target_page_success():
    before = {"kind": "vue", "path": "/dashboard", "content": "Dashboard 1"}
    after = {"kind": "vue", "path": "/library", "content": "Dashboard 2",
             "target_component": {"present": False, "visible": False}}
    assert not probe.interaction_succeeded(before, after, "/library")


@pytest.mark.parametrize("state", ["hidden", "leaving", "entering"])
def test_target_component_must_be_visible_and_outside_route_transition(state):
    before = {"kind": "vue", "path": "/dashboard", "content": "Dashboard"}
    target = {"selector": '.navbar .search-box[data-tour="nav-search"]', "present": True,
              "visible": state != "hidden", "leaving": state == "leaving",
              "entering": state == "entering", "characters": 5}
    after = {"kind": "vue", "path": "/library", "content": "Games", "target_component": target}
    assert not probe.interaction_succeeded(before, after, "/library")


def test_document_script_checks_actual_target_root_and_transition_ancestors():
    if not shutil.which("node"):
        pytest.skip("Node is required to execute the browser observation expression")
    script = """
    const output = [];
    for (const state of ['old-page','target','hidden','leaving','entering']) {
      const rect = () => ({x:100,y:100,left:100,top:100,right:800,bottom:600,width:700,height:500});
      const main = {innerText:'Dashboard statistics changed',classList:[],parentElement:null};
      const root = {innerText:'Games',className:state,classList:
        state === 'leaving' ? ['route-fade-leave-active'] : state === 'entering' ? ['route-fade-enter-active'] : [],
        parentElement:main,getBoundingClientRect:rect};
      const marker = {parentElement:root,classList:[],closest:() => root,getBoundingClientRect:rect};
      main.querySelector = selector => state !== 'old-page' &&
        selector === '.navbar .search-box[data-tour="nav-search"]' ? marker : null;
      const navigation = {innerText:'Store Following',getBoundingClientRect:rect};
      const button = {getBoundingClientRect:rect,contains:() => true};
      globalThis.document = {readyState:'complete',elementFromPoint:() => button,
        querySelector:selector => selector === '.view-container' ? main :
          selector === '.hl-sidebar nav' ? navigation : selector === '.hl-sidebar' ? {} : button};
      globalThis.location = {pathname:'/library'};
      globalThis.innerWidth = 1280; globalThis.innerHeight = 860;
      globalThis.getComputedStyle = element => ({display:'block',visibility:'visible',
        opacity:state === 'hidden' && element === root ? '0' : '1'});
      output.push(eval(EXPRESSION));
    }
    process.stdout.write(JSON.stringify(output));
    """.replace("EXPRESSION", json.dumps(probe.document_expression("/library")))
    output = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True)
    documents = json.loads(output.stdout)
    assert [probe.target_component_ready(document, "/library") for document in documents] == [False, True, False, False, False]


def test_vue_navigation_does_not_wait_for_remote_dashboard_content():
    document = {"kind": "vue", "navigationVisible": True, "navigation": "Store Following",
                "path": "/dashboard", "content": ""}
    assert probe.vue_navigation_ready(document)
    assert not probe.vue_navigation_ready({**document, "navigation": ""})
    assert not probe.vue_navigation_ready({**document, "navigationVisible": False})
    assert not probe.vue_navigation_ready({**document, "kind": "splash"})
    assert probe.interaction_succeeded(document, {**document, "path": "/library", "content": "Games",
        "target_component": {"selector": '.navbar .search-box[data-tour="nav-search"]', "present": True,
                             "visible": True, "entering": False, "leaving": False, "characters": 5}}, "/library")


def dpi_api(monkeypatch, *, process_ok=True, error=0, current_v2=True, thread_ok=True):
    state = {"current_v2": current_v2, "thread_calls": 0}
    monkeypatch.setattr(probe.ctypes, "set_last_error", lambda value: None, raising=False)
    monkeypatch.setattr(probe.ctypes, "get_last_error", lambda: error, raising=False)

    def set_thread(context):
        state["thread_calls"] += 1
        if thread_ok:
            state["current_v2"] = True
        return 123 if thread_ok else None

    api = SimpleNamespace(SetProcessDpiAwarenessContext=lambda context: process_ok,
                          GetThreadDpiAwarenessContext=lambda: 123 if state["current_v2"] else 456,
                          AreDpiAwarenessContextsEqual=lambda a, b: state["current_v2"],
                          GetAwarenessFromDpiAwarenessContext=lambda context: 2 if state["current_v2"] else 1,
                          SetThreadDpiAwarenessContext=set_thread)
    return api, state


def test_dpi_awareness_is_verified_before_screen_coordinates(monkeypatch):
    api, state = dpi_api(monkeypatch)
    result = probe.enable_physical_pixels(api)
    assert result["verified"] is True
    assert result["thread_awareness"] == 2
    assert result["process_set"] is True
    assert state["thread_calls"] == 0


@pytest.mark.parametrize("current_v2", [False, True])
def test_existing_dpi_default_requires_verified_v2_or_thread_override(monkeypatch, current_v2):
    api, state = dpi_api(monkeypatch, process_ok=False, error=5, current_v2=current_v2)
    result = probe.enable_physical_pixels(api)
    assert result["verified"] is True
    assert result["process_error"] == 5
    assert result["thread_overridden"] is not current_v2
    assert state["thread_calls"] == int(not current_v2)


@pytest.mark.parametrize(("process_ok", "error", "current_v2", "thread_ok"), [
    (False, 87, False, True), (False, 5, False, False),
])
def test_dpi_failure_stops_sampling(monkeypatch, process_ok, error, current_v2, thread_ok):
    api, _state = dpi_api(monkeypatch, process_ok=process_ok, error=error,
                          current_v2=current_v2, thread_ok=thread_ok)
    with pytest.raises(RuntimeError, match="DPI"):
        probe.enable_physical_pixels(api)


@pytest.mark.parametrize("foreground_after", [77, 88])
def test_capture_uses_physical_client_bbox_and_rejects_foreground_changes(monkeypatch, foreground_after):
    from ctypes import wintypes as wt
    from PIL import ImageGrab

    screen = probe.WindowsScreen.__new__(probe.WindowsScreen)
    screen.wt = wt
    foreground = iter((77, foreground_after))

    def client_rect(hwnd, pointer):
        pointer._obj.right, pointer._obj.bottom = 640, 480
        return 1

    def client_origin(hwnd, pointer):
        pointer._obj.x, pointer._obj.y = 100, 200
        return 1

    screen.api = SimpleNamespace(AreDpiAwarenessContextsEqual=lambda a, b: True,
                                 GetThreadDpiAwarenessContext=lambda: 34,
                                 GetForegroundWindow=lambda: next(foreground), GetDpiForWindow=lambda hwnd: 144,
                                 GetClientRect=client_rect, ClientToScreen=client_origin,
                                 IsWindowVisible=lambda hwnd: True, IsIconic=lambda hwnd: False,
                                 GetWindowLongPtrW=lambda hwnd, key: 0)
    image = SimpleNamespace(size=(640, 480))
    image.convert = lambda mode: image
    boxes = []
    monkeypatch.setattr(ImageGrab, "grab", lambda bbox, **kwargs: (boxes.append(bbox), image)[1])
    assert screen.capture(77) is (image if foreground_after == 77 else None)
    assert boxes == [(100, 200, 740, 680)]
    assert screen.capture_metadata["bbox_physical"] == (100, 200, 740, 680)
    assert screen.capture_metadata["foreground_after"] == foreground_after
    assert screen.capture_metadata["dpi_verified"] is True


@pytest.mark.parametrize(("alpha", "visible", "minimized", "eligible"), [
    (0, True, False, False), (128, True, False, False), (255, True, False, True),
    (255, False, False, False), (255, True, True, False),
])
def test_transparent_hidden_or_minimized_host_is_not_an_eligible_window(alpha, visible, minimized, eligible):
    from ctypes import wintypes as wt

    screen = probe.WindowsScreen.__new__(probe.WindowsScreen)
    screen.wt = wt

    def attributes(hwnd, color, opacity, flags):
        opacity._obj.value, flags._obj.value = alpha, 2
        return 1

    screen.api = SimpleNamespace(IsWindowVisible=lambda hwnd: visible, IsIconic=lambda hwnd: minimized,
                                 GetWindowLongPtrW=lambda hwnd, key: 0x80000,
                                 GetLayeredWindowAttributes=attributes)
    assert screen.visibility(77)["eligible"] is eligible


def test_cdp_websocket_timeout_is_recoverable_builtin_timeout():
    import websocket

    connection = probe.Cdp.__new__(probe.Cdp)
    connection.sequence = 0
    connection.deadline = probe.time.perf_counter() + 5

    def receive():
        raise websocket.WebSocketTimeoutException("Connection timed out")

    connection.socket = SimpleNamespace(send=lambda payload: None, recv=receive, settimeout=lambda value: None)
    with pytest.raises(TimeoutError, match="CDP"):
        connection.call("Runtime.evaluate", {"expression": "1"})


def test_expired_cdp_deadline_sends_no_request():
    connection = probe.Cdp.__new__(probe.Cdp)
    connection.sequence, connection.deadline = 0, probe.time.perf_counter() - 1
    sent = []
    connection.socket = SimpleNamespace(send=sent.append, settimeout=lambda value: None)
    with pytest.raises(TimeoutError, match="deadline"):
        connection.call("Runtime.evaluate", {"expression": "1"})
    assert sent == []


def test_layered_window_with_unreadable_opacity_is_unverified():
    from ctypes import wintypes as wt

    screen = probe.WindowsScreen.__new__(probe.WindowsScreen)
    screen.wt = wt
    screen.api = SimpleNamespace(IsWindowVisible=lambda hwnd: True, IsIconic=lambda hwnd: False,
                                 GetWindowLongPtrW=lambda hwnd, key: 0x80000,
                                 GetLayeredWindowAttributes=lambda *args: 0)
    result = screen.visibility(77)
    assert result["alpha"] is None
    assert result["eligible"] is False


def test_dom_event_frames_are_saved_even_when_pixel_classification_is_unchanged(tmp_path, monkeypatch):
    import psutil
    from PIL import Image

    root = tmp_path / "installation"
    root.mkdir()
    launcher = root / "Holdexar.exe"
    launcher.write_bytes(b"fixture")
    actions = []
    image = Image.new("RGB", (320, 240), (27, 40, 56))
    image.paste((255, 255, 255), (0, 0, 60, 240))
    save = image.save

    def save_frame(path):
        actions.append("frame")
        save(path)

    image.save = save_frame

    class Screen:
        dpi = {"verified": True}
        window_state = {"hwnd": 77, "pid": 123, "eligible": True}
        capture_metadata = {"dpi_verified": True}

        def find(self, *args):
            return 77, 123

        def foreground(self, hwnd):
            return True

        def capture(self, hwnd):
            return image

    class Browser:
        clicked = False

        def __init__(self, *args):
            pass

        def document(self, route):
            return {"kind": "vue", "navigationVisible": True, "navigation": "Store Following",
                    "path": "/library" if self.clicked else "/dashboard",
                    "content": "Games" if self.clicked else "", "button": {"x": 10, "y": 10},
                    "target_component": {"selector": '.navbar .search-box[data-tour="nav-search"]',
                                         "present": self.clicked, "visible": self.clicked, "entering": False,
                                         "leaving": False, "characters": 5 if self.clicked else 0}}

        def click(self, point):
            actions.append("click")
            self.clicked = True

        def metrics(self):
            return {}

        def close(self):
            pass

    def missing_process(pid):
        raise psutil.NoSuchProcess(pid)

    monkeypatch.setattr(probe, "WindowsScreen", Screen)
    monkeypatch.setattr(probe, "Cdp", Browser)
    monkeypatch.setattr(probe, "processes", lambda *args: [])
    monkeypatch.setattr(probe, "read_json", lambda *args: {})
    monkeypatch.setattr(probe.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=123))
    monkeypatch.setattr(psutil, "Process", missing_process)
    args = SimpleNamespace(expected_root=root, launcher=launcher, confirm_isolated=True, target_route="/library",
                           out=tmp_path / "output", mode="cold", port=28931, lock_port=29131, cdp_port=29331,
                           server=False, extra=[], title="Holdexar", label="fixture", timeout=2,
                           observe_seconds=.01, interval=.001, max_frames=16)
    report = probe.probe(args)
    reasons = {reason for event in report["events"] if event["event"] == "screen_frame"
               for reason in event.get("requested_events", [])}
    assert reasons == {"vue_document", "before_navigation_click", "after_navigation_click", "interaction_confirmed"}
    assert report["interactive_confirmed"] is True
    assert actions.index("click") >= 2


def test_health_must_match_process_root_and_data_directory(tmp_path):
    value = {"app": "Holdexar", "pid": 123, "executable": str(tmp_path / "versions/v/Holdexar.exe"),
             "dataDir": str(tmp_path / "data")}
    assert probe.valid_health(value, tmp_path, {123})
    assert not probe.valid_health(value, tmp_path, {124})
    assert not probe.valid_health({**value, "executable": str(tmp_path.parent / "other.exe")}, tmp_path, {123})
    assert not probe.valid_health({**value, "dataDir": str(tmp_path / "other-data")}, tmp_path, {123})


def test_cdp_metrics_keep_raw_values_separate_from_interaction():
    calls = []
    connection = probe.Cdp.__new__(probe.Cdp)

    def call(method, params=None):
        calls.append((method, params))
        if method == "Performance.getMetrics":
            return {"metrics": [{"name": "ScriptDuration", "value": .4}]}
        return {"result": {"value": {"longTasks": [{"startTime": 10, "duration": 80}]}}}

    connection.call = call
    assert connection.metrics() == {
        "performance_metrics": [{"name": "ScriptDuration", "value": .4}],
        "page_entries": {"longTasks": [{"startTime": 10, "duration": 80}]}}
    assert calls[0][0] == "Performance.getMetrics"
    assert ".pathname" in calls[1][1]["expression"]


def test_summary_separates_modes_and_never_substitutes_health_for_interaction(tmp_path):
    files = []
    for index, (mode, ms) in enumerate([("cold", 100), ("cold", 200), ("cache-warm", 50)]):
        path = tmp_path / f"{index}.json"
        path.write_text(json.dumps({"mode": mode, "dpi": {"verified": True}, "screen_contract": 3,
            "interactive_confirmed": index == 1, "events": [
            {"event": "health_identity", "ms": 10},
            {"event": "screen_frame", "label": "white_blank", "ms": 15},
            {"event": "screen_frame", "label": "painted_candidate", "ms": ms},
            {"event": "screen_frame", "label": "painted_candidate", "ms": ms + 20},
            *([{"event": "interaction_confirmed", "ms": 250}] if index == 1 else [])]}), encoding="utf-8")
        files.append(path)
    result = probe.summarize(files)
    assert result["unlabeled:cold:gui"]["samples"] == 2
    assert result["unlabeled:cold:gui"]["interactive_failures"] == 1
    assert result["unlabeled:cold:gui"]["metrics"]["first_painted_candidate"] == {
        "count": 2, "p50_ms": 150, "p90_ms": 200, "max_ms": 200}
    assert result["unlabeled:cold:gui"]["metrics"]["interaction_confirmed"]["count"] == 1
    assert "interaction_confirmed" not in result["unlabeled:cache-warm:gui"]["metrics"]


def test_summary_rejects_unverified_dpi_pixels_but_keeps_api_evidence(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text(json.dumps({"mode": "cold", "interactive_confirmed": True, "events": [
        {"event": "health_identity", "ms": 10},
        {"event": "screen_frame", "label": "painted_candidate", "ms": 20},
        {"event": "interaction_confirmed", "ms": 30}]}), encoding="utf-8")
    result = probe.summarize([path])["unlabeled:cold:gui"]
    assert result["invalid_pixel_samples"] == 1
    assert result["interactive_failures"] == 1
    assert set(result["metrics"]) == {"health_identity"}


def test_summary_keeps_calibrated_old_pixels_but_rejects_old_interaction_contract(tmp_path):
    path = tmp_path / "old-target-contract.json"
    path.write_text(json.dumps({"mode": "cold", "dpi": {"verified": True}, "screen_contract": 2,
        "interactive_confirmed": True, "events": [
        {"event": "screen_frame", "label": "painted_candidate", "ms": 20},
        {"event": "interaction_confirmed", "ms": 30}]}), encoding="utf-8")
    result = probe.summarize([path])["unlabeled:cold:gui"]
    assert result["invalid_pixel_samples"] == 0
    assert result["interactive_failures"] == 1
    assert set(result["metrics"]) == {"first_painted_candidate"}
