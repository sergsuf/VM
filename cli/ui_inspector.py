"""Windows UI Automation (UIA) inspector for discovering popups and clickable controls."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil


def _ensure_default_desktop() -> None:
    """Attach the current thread to WinSta0\\Default if running on a non-Default desktop."""
    if sys.platform != "win32":
        return
    try:
        u32 = ctypes.windll.user32
        k32 = ctypes.windll.kernel32
        hdesk = u32.GetThreadDesktop(k32.GetCurrentThreadId())
        buf = ctypes.create_unicode_buffer(256)
        if u32.GetUserObjectInformationW(hdesk, 2, buf, 512, None) and buf.value.lower() == "default":
            return
        hdef = u32.OpenDesktopW("Default", 0, False, 0x10000000)
        if hdef:
            u32.SetThreadDesktop(hdef)
    except Exception:
        pass


if sys.platform == "win32":
    _ensure_default_desktop()
    import uiautomation as auto  # type: ignore
    from PIL import ImageGrab  # type: ignore
else:
    auto = None
    ImageGrab = None


INTERACTIVE_CONTROL_TYPES = {
    "ButtonControl",
    "CheckBoxControl",
    "RadioButtonControl",
    "HyperlinkControl",
    "MenuItemControl",
    "TabItemControl",
    "ComboBoxControl",
    "EditControl",
}

TEXT_CONTROL_TYPES = {
    "TextControl",
}

# Heavy 3ds Max main-window panels/toolbars that never host startup popups
SKIP_SUBTREE_NAMES = {
    "Command Panel",
    "Scene Explorer - Default",
    "Main Toolbar",
    "Ribbon",
    "Viewport Layout Tabs",
    "Time Slider",
    "TimeSlider",
    "StatusBar",
    "ViewControlPanel",
    "AnimatePanel",
    "TimePanel",
    "StatusPanel",
    "Projects",
    "Autobackup",
    "QmaxMenuBar",
    "QDeferredResizeWidget",
}

SKIP_SUBTREE_IDS = {
    "CommandPanel",
    "Scene Explorer - Default",
    "Main Toolbar",
    "Ribbon",
    "Viewport Layout Tabs",
    "TimeSlider",
    "ProjectsToolbar",
    "AutobackupToolbar",
}


@dataclass
class UIElement:
    control_type: str
    name: str
    automation_id: str
    class_name: str
    rect: tuple[int, int, int, int]  # left, top, right, bottom
    center: tuple[int, int]
    is_enabled: bool
    is_offscreen: bool


@dataclass
class WindowSnapshot:
    hwnd: int
    title: str
    class_name: str
    process_name: str
    pid: int
    rect: tuple[int, int, int, int]
    elements: list[UIElement] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    def fingerprint(self) -> str:
        """Stable hash of window title + process + button names for deduplicating logs."""
        btn_names = "|".join(e.name for e in self.elements if e.name)
        raw = f"{self.process_name}::{self.title}::{btn_names}"
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_windows() -> None:
    if auto is None:
        raise RuntimeError("ui_inspector requires Windows (uiautomation / Win32).")


def _get_process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return "unknown"


def _walk_controls(
    root: Any,
    max_depth: int = 12,
    max_elements: int = 500,
) -> tuple[list[UIElement], list[str]]:
    """Breadth-first walk over UIA subtree to collect interactive controls and texts."""
    elements: list[UIElement] = []
    texts: list[str] = []
    queue: list[tuple[Any, int]] = [(root, 0)]

    while queue and len(elements) < max_elements:
        node, depth = queue.pop(0)
        if depth > max_depth:
            continue

        try:
            ctype = node.ControlTypeName
            name = (node.Name or "").strip()
            auto_id = (node.AutomationId or "").strip()
            cls = (node.ClassName or "").strip()
            rect_obj = node.BoundingRectangle
            rect = (rect_obj.left, rect_obj.top, rect_obj.right, rect_obj.bottom)
            w = rect[2] - rect[0]
            h = rect[3] - rect[1]
            cx = rect[0] + w // 2
            cy = rect[1] + h // 2
            is_enabled = bool(node.IsEnabled)
            is_offscreen = bool(node.IsOffscreen)
        except Exception:
            continue

        if depth > 0:
            if ctype in INTERACTIVE_CONTROL_TYPES and w > 0 and h > 0 and not is_offscreen:
                elements.append(
                    UIElement(
                        control_type=ctype,
                        name=name,
                        automation_id=auto_id,
                        class_name=cls,
                        rect=rect,
                        center=(cx, cy),
                        is_enabled=is_enabled,
                        is_offscreen=is_offscreen,
                    )
                )
            elif ctype in TEXT_CONTROL_TYPES and name and not is_offscreen:
                if name not in texts:
                    texts.append(name)

        if depth == 1 and (name in SKIP_SUBTREE_NAMES or auto_id in SKIP_SUBTREE_IDS):
            continue

        try:
            children = node.GetChildren()
            for child in children:
                queue.append((child, depth + 1))
        except Exception:
            pass

    return elements, texts


def _enum_win32_windows(
    process_filters: list[str] | None = None,
    include_all: bool = False,
    title_regexes: list[str] | None = None,
) -> list[tuple[int, str, str, str, int, tuple[int, int, int, int]]]:
    """Fast Win32 EnumWindows pre-filter before touching UIA COM objects."""
    u32 = ctypes.windll.user32
    filters_lower = [p.lower() for p in (process_filters or [])]
    compiled_rx = [re.compile(rx) for rx in (title_regexes or []) if rx]
    results: list[tuple[int, str, str, str, int, tuple[int, int, int, int]]] = []

    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    pid_cache: dict[int, str] = {}

    def _callback(hwnd: int, _lparam: int) -> bool:
        if not u32.IsWindowVisible(hwnd):
            return True
        rect_st = wintypes.RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(rect_st)):
            return True
        if rect_st.right <= rect_st.left or rect_st.bottom <= rect_st.top:
            return True

        length = u32.GetWindowTextLengthW(hwnd)
        title_buf = ctypes.create_unicode_buffer(length + 1)
        u32.GetWindowTextW(hwnd, title_buf, length + 1)
        title = title_buf.value.strip()

        if compiled_rx and not any(rx.search(title) for rx in compiled_rx):
            return True

        pid_val = wintypes.DWORD()
        u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_val))
        pid = int(pid_val.value)
        proc_name = pid_cache.get(pid)
        if proc_name is None:
            proc_name = _get_process_name(pid)
            pid_cache[pid] = proc_name
        if not include_all and filters_lower:
            if not any(f in proc_name.lower() for f in filters_lower):
                return True

        cls_buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls_buf, 256)
        cls = cls_buf.value.strip()

        rect = (rect_st.left, rect_st.top, rect_st.right, rect_st.bottom)
        results.append((int(hwnd), title, cls, proc_name, pid, rect))
        return True

    u32.EnumWindows(enum_proc_type(_callback), 0)
    return results


def scan_windows(
    process_filters: list[str] | None = None,
    include_all: bool = False,
    max_depth: int = 12,
    title_regexes: list[str] | None = None,
) -> list[WindowSnapshot]:
    """Scan currently visible top-level windows using fast Win32 pre-filter + UIA."""
    _ensure_windows()
    _ensure_default_desktop()
    snapshots: list[WindowSnapshot] = []

    candidates = _enum_win32_windows(
        process_filters=process_filters,
        include_all=include_all,
        title_regexes=title_regexes,
    )
    for hwnd, title, cls, proc_name, pid, rect in candidates:
        try:
            win = auto.ControlFromHandle(hwnd)
            if not win:
                continue
            elements, texts = _walk_controls(win, max_depth=max_depth)
            snapshots.append(
                WindowSnapshot(
                    hwnd=hwnd,
                    title=title,
                    class_name=cls,
                    process_name=proc_name,
                    pid=pid,
                    rect=rect,
                    elements=elements,
                    texts=texts,
                )
            )
        except Exception:
            continue

    return snapshots


def save_unknown_window_dump(
    snapshot: WindowSnapshot,
    output_dir: Path,
) -> tuple[Path, Path | None]:
    """Save UI tree JSON and window screenshot for an unrecognized popup."""
    output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    fp = snapshot.fingerprint()
    json_path = output_dir / f"popup_{ts}_{fp}.json"
    json_path.write_text(
        json.dumps(snapshot.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    screenshot_path: Path | None = None
    if ImageGrab is not None:
        try:
            left, top, right, bottom = snapshot.rect
            if right > left and bottom > top:
                img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
                screenshot_path = output_dir / f"popup_{ts}_{fp}.png"
                img.save(screenshot_path)
        except Exception:
            screenshot_path = None

    return json_path, screenshot_path


def uia_trigger_element(
    hwnd: int,
    element: UIElement,
    desired_state: int = 1,
) -> bool:
    """Invoke or toggle a UIA control directly by HWND + metadata without mouse coordinates."""
    _ensure_windows()
    _ensure_default_desktop()
    try:
        win = auto.ControlFromHandle(hwnd)
        if not win:
            return False

        target = None
        if element.automation_id:
            if element.control_type == "CheckBoxControl":
                cand = win.CheckBoxControl(AutomationId=element.automation_id, searchDepth=10)
            elif element.control_type == "ButtonControl":
                cand = win.ButtonControl(AutomationId=element.automation_id, searchDepth=10)
            else:
                cand = win.Control(AutomationId=element.automation_id, searchDepth=10)
            if cand.Exists(2.0):
                target = cand
        if target is None and element.name:
            if element.control_type == "CheckBoxControl":
                cand = win.CheckBoxControl(Name=element.name, searchDepth=10)
            elif element.control_type == "ButtonControl":
                cand = win.ButtonControl(Name=element.name, searchDepth=10)
            else:
                cand = win.Control(Name=element.name, searchDepth=10)
            if cand.Exists(2.0):
                target = cand

        if target is None:
            return False

        if element.control_type == "CheckBoxControl":
            tp = target.GetTogglePattern()
            if tp is not None:
                if tp.ToggleState != desired_state:
                    tp.Toggle()
                return True

        ip = target.GetInvokePattern()
        if ip is not None:
            ip.Invoke()
            return True
    except Exception:
        return False
    return False
