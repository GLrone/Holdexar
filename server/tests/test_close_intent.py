"""关窗意图询问（desktop/main.py `_ask_close_intent`）单测：不出网、不弹真窗。

背景：此前点 X **静默隐藏**到托盘——用户以为退出了程序，进程却常驻后台
继续占端口/跑调度。改为点 X 弹三选一（是=最小化到托盘、否=退出、取消=留
在窗口）后，本文件钉死三件事：
1. 对话框结果 → 意图的映射（Yes/No/Cancel）；
2. 异常降级 = minimize：pythonnet/桌面会话不可用时保持旧的「隐藏常驻」
   语义，绝不误杀进程，也绝不让弹窗失败把用户卡在关不掉的窗口上；
3. 关窗守卫对三种意图 + 托盘退出置位的行为（返回值即 closing 契约）。

真实弹窗链路（UI 线程同步 MessageBox）由真窗口冒烟覆盖，此处只测逻辑层。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]


def _load_desktop_main():
    """按路径加载桌面启动器模块（desktop/ 不在 pytest 的 import 路径上）。"""
    spec = importlib.util.spec_from_file_location(
        "holdexar_desktop_main_close", ROOT / "desktop" / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


desktop = _load_desktop_main()


class _FakeDialogResult:
    """占位 DialogResult：值属性由测试按需注入。"""


def _patch_winforms(monkeypatch, *, show_returns=None, dialog_result=None):
    """把 System.Windows.Forms 换成纯 Python 假模块（sys.modules 条目替换）。

    pythonnet 的 CLR 模块对象不可 setattr（.NET 类型对 Python 只读），
    但 `from System.Windows.Forms import ...` 走 sys.modules 查找——把
    条目换成 types.ModuleType 假模块即可全权控制被测代码拿到的件。

    show_returns：MessageBox.Show 的返回值（模拟用户点键）或 Exception
    实例（模拟弹窗链路炸穿，走降级）。
    dialog_result：替身 DialogResult（带 Yes/No/Cancel 值）。
    返回 calls 列表（每次 Show 调用的实参元组）。
    """
    import types

    calls: list[tuple] = []

    class _FakeMessageBox:
        @staticmethod
        def Show(owner, text, caption, buttons, icon):
            calls.append((owner, text, caption, buttons, icon))
            if isinstance(show_returns, Exception):
                raise show_returns
            return show_returns

    fake = types.ModuleType("System.Windows.Forms")
    fake.MessageBox = _FakeMessageBox
    fake.DialogResult = dialog_result or SimpleNamespace(
        Yes=object(), No=object(), Cancel=object()
    )
    fake.MessageBoxButtons = SimpleNamespace(YesNoCancel=object())
    fake.MessageBoxIcon = SimpleNamespace(Question=object())

    import clr  # noqa: F401 —— 真环境同序：装配件命名空间先注册（被测代码也这么走）

    # 先确认真装配件已注册（ clr.AddReference），再换 sys.modules 条目——
    # 还原时放回 None 让 pythonnet 按需重建，避免测试间串味
    monkeypatch.setitem(sys.modules, "System.Windows.Forms", fake)
    return calls


def test_yes_maps_to_minimize(monkeypatch):
    """对话框「是」→ 最小化到托盘（默认推荐行为，与旧语义衔接）。"""
    chosen = _FakeDialogResult()
    result = SimpleNamespace(Yes=chosen, No=object(), Cancel=object())
    _patch_winforms(monkeypatch, show_returns=result.Yes, dialog_result=result)
    assert desktop._ask_close_intent(None) == "minimize"


def test_no_maps_to_quit(monkeypatch):
    """对话框「否」→ 完全退出程序。"""
    chosen = _FakeDialogResult()
    result = SimpleNamespace(Yes=object(), No=chosen, Cancel=object())
    _patch_winforms(monkeypatch, show_returns=result.No, dialog_result=result)
    assert desktop._ask_close_intent(None) == "quit"


def test_cancel_maps_to_stay(monkeypatch):
    """对话框「取消」→ 留在窗口，什么都不做。"""
    chosen = _FakeDialogResult()
    result = SimpleNamespace(Yes=object(), No=object(), Cancel=chosen)
    _patch_winforms(monkeypatch, show_returns=result.Cancel, dialog_result=result)
    assert desktop._ask_close_intent(None) == "stay"


def test_dialog_failure_falls_back_to_minimize(monkeypatch):
    """弹窗链路炸穿（无桌面/装配件缺失）→ 降级最小化，不误杀进程。"""
    _patch_winforms(
        monkeypatch, show_returns=RuntimeError("no desktop session")
    )
    assert desktop._ask_close_intent(None) == "minimize"


def test_dialog_gets_owner_and_full_text(monkeypatch):
    """弹窗必须以主窗口为 owner（居中其上、不藏到别的窗口后面）。"""
    chosen = _FakeDialogResult()
    result = SimpleNamespace(Yes=object(), No=object(), Cancel=chosen)
    calls = _patch_winforms(
        monkeypatch, show_returns=result.Cancel, dialog_result=result
    )
    owner = object()
    desktop._ask_close_intent(owner)
    assert len(calls) == 1
    assert calls[0][0] is owner  # owner 原样传给 MessageBox.Show
    assert calls[0][1]  # 文案非空


def test_guard_quit_intent_releases_close(monkeypatch):
    """守卫：意图 quit → 返回 True 放行真关闭（closed 事件收尾进程）。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "quit")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is True
    assert not hidden  # 退出路径不再隐藏


def test_guard_minimize_intent_hides_and_cancels(monkeypatch):
    """守卫：意图 minimize → 隐藏窗口 + 返回 False（进程常驻）。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "minimize")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is False
    assert hidden == [1]


def test_guard_stay_intent_keeps_window(monkeypatch):
    """守卫：意图 stay → 只返回 False，不隐藏窗口。"""
    monkeypatch.setattr(desktop, "_ask_close_intent", lambda owner=None: "stay")
    hidden = []
    window = SimpleNamespace(hide=lambda: hidden.append(1), native=None)
    guard = desktop._make_closing_guard(window)
    assert guard() is False
    assert not hidden


def test_guard_tray_quit_bypasses_dialog(monkeypatch):
    """守卫：托盘「退出」置位 → 不弹询问直接放行（明确意图免二次确认）。"""
    asked: list = []
    monkeypatch.setattr(
        desktop, "_ask_close_intent", lambda owner=None: asked.append(1) or "stay"
    )
    window = SimpleNamespace(hide=lambda: None, native=None)
    guard = desktop._make_closing_guard(window)
    desktop._tray_state["quit"] = True
    try:
        assert guard() is True
    finally:
        desktop._tray_state["quit"] = False  # 模块级状态，测完必须复位
    assert not asked  # 托盘退出不该触发询问
