"""Windows UI Automation (UIA) inspector for discovering popups and clickable controls."""

from __future__ import annotations

from collections import deque
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

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None  # type: ignore[assignment]


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
        if hdef and not u32.SetThreadDesktop(hdef):
            u32.CloseDesktop(hdef)
    except Exception:
        pass


if sys.platform == "win32":
    _ensure_default_desktop()
    import uiautomation as auto  # type: ignore
    from PIL import Image, ImageGrab  # type: ignore
else:
    auto = None
    Image = None
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
    child_titles: list[str] = field(default_factory=list)
    webview_hwnd: int | None = None

    @property
    def display_title(self) -> str:
        if self.title:
            return self.title
        if self.child_titles:
            return self.child_titles[0]
        return ""

    def fingerprint(self) -> str:
        """Stable hash of window title + process + button names for deduplicating logs."""
        btn_names = "|".join(e.name for e in self.elements if e.name)
        raw = f"{self.process_name}::{self.display_title}::{btn_names}"
        return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:12]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _ensure_windows() -> None:
    if auto is None:
        raise RuntimeError("ui_inspector requires Windows (uiautomation / Win32).")


def _get_process_name(pid: int) -> str:
    if psutil is None:
        comm = Path(f"/proc/{pid}/comm")
        if comm.is_file():
            try:
                return comm.read_text(encoding="utf-8", errors="ignore").strip() or "unknown"
            except OSError:
                pass
        return "unknown"
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
    queue: deque[tuple[Any, int]] = deque([(root, 0)])

    while queue and len(elements) < max_elements:
        node, depth = queue.popleft()
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


def _inspect_win32_children(hwnd: int) -> tuple[list[str], int | None]:
    """Fast Win32 EnumChildWindows lookup for nested dialog titles and WebView2 render HWND."""
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    enum_proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumChildWindows.argtypes = [wintypes.HWND, enum_proc_type, wintypes.LPARAM]
    u32.EnumChildWindows.restype = wintypes.BOOL
    u32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetClassNameW.restype = ctypes.c_int
    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextLengthW.restype = ctypes.c_int
    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowTextW.restype = ctypes.c_int

    child_titles: list[str] = []
    webview_hwnd: int | None = None

    def _child_cb(child_hwnd: int, _lparam: int) -> bool:
        nonlocal webview_hwnd
        cls_buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(child_hwnd, cls_buf, 256)
        cls = cls_buf.value.strip()

        if webview_hwnd is None and cls == "Chrome_RenderWidgetHostHWND":
            webview_hwnd = int(child_hwnd)

        length = u32.GetWindowTextLengthW(child_hwnd)
        if length > 0:
            title_buf = ctypes.create_unicode_buffer(length + 1)
            u32.GetWindowTextW(child_hwnd, title_buf, length + 1)
            text = title_buf.value.strip()
            if text and text != "Chrome Legacy Window" and text not in child_titles:
                child_titles.append(text)
        return True

    u32.EnumChildWindows(hwnd, enum_proc_type(_child_cb), 0)
    return child_titles, webview_hwnd


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


def list_visible_windows(
    process_filters: list[str] | None = None,
    include_all: bool = False,
    title_regexes: list[str] | None = None,
) -> list[WindowSnapshot]:
    """Return lightweight Win32 snapshots (including child titles and WebView2 HWND) without UIA walks."""
    _ensure_windows()
    _ensure_default_desktop()
    snapshots: list[WindowSnapshot] = []

    candidates = _enum_win32_windows(
        process_filters=process_filters,
        include_all=include_all,
        title_regexes=title_regexes,
    )
    for hwnd, title, cls, proc_name, pid, rect in candidates:
        child_titles, webview_hwnd = _inspect_win32_children(hwnd)
        snapshots.append(
            WindowSnapshot(
                hwnd=hwnd,
                title=title,
                class_name=cls,
                process_name=proc_name,
                pid=pid,
                rect=rect,
                child_titles=child_titles,
                webview_hwnd=webview_hwnd,
            )
        )
    return snapshots


def populate_snapshot_controls(
    snapshot: WindowSnapshot,
    max_depth: int = 12,
) -> WindowSnapshot:
    """Populate UIA elements and texts for a WindowSnapshot, attaching directly to WebView2 when present."""
    _ensure_windows()
    _ensure_default_desktop()
    target_hwnd = snapshot.webview_hwnd or snapshot.hwnd
    effective_depth = min(max_depth, 8) if snapshot.webview_hwnd else max_depth
    try:
        win = auto.ControlFromHandle(target_hwnd)
        if win:
            elements, texts = _walk_controls(win, max_depth=effective_depth)
            snapshot.elements = elements
            snapshot.texts = texts
    except Exception:
        pass
    return snapshot


def scan_windows(
    process_filters: list[str] | None = None,
    include_all: bool = False,
    max_depth: int = 12,
    title_regexes: list[str] | None = None,
) -> list[WindowSnapshot]:
    """Scan currently visible top-level windows using fast Win32 pre-filter + UIA."""
    snapshots = list_visible_windows(
        process_filters=process_filters,
        include_all=include_all,
        title_regexes=title_regexes,
    )
    for snap in snapshots:
        populate_snapshot_controls(snap, max_depth=max_depth)
    return snapshots


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


def _capture_window_printwindow(hwnd: int) -> Any | None:
    """Capture a window via Win32 PrintWindow (works even when the VM console is minimized)."""
    if sys.platform != "win32" or Image is None:
        return None
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    g32 = ctypes.WinDLL("gdi32", use_last_error=True)

    u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    u32.GetWindowRect.restype = wintypes.BOOL
    u32.GetWindowDC.argtypes = [wintypes.HWND]
    u32.GetWindowDC.restype = wintypes.HDC
    u32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    u32.ReleaseDC.restype = ctypes.c_int
    u32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    u32.PrintWindow.restype = wintypes.BOOL

    g32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    g32.CreateCompatibleDC.restype = wintypes.HDC
    g32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    g32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    g32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    g32.SelectObject.restype = wintypes.HGDIOBJ
    g32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    g32.DeleteObject.restype = wintypes.BOOL
    g32.DeleteDC.argtypes = [wintypes.HDC]
    g32.DeleteDC.restype = wintypes.BOOL
    g32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        ctypes.c_void_p,
        ctypes.POINTER(_BITMAPINFO),
        wintypes.UINT,
    ]
    g32.GetDIBits.restype = ctypes.c_int

    rect = wintypes.RECT()
    if not u32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    w = rect.right - rect.left
    h = rect.bottom - rect.top
    if w <= 0 or h <= 0:
        return None

    hwnd_dc = u32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = None
    bmp = None
    old_obj = None
    try:
        mem_dc = g32.CreateCompatibleDC(hwnd_dc)
        if not mem_dc:
            return None
        bmp = g32.CreateCompatibleBitmap(hwnd_dc, w, h)
        if not bmp:
            return None
        old_obj = g32.SelectObject(mem_dc, bmp)
        # PW_RENDERFULLCONTENT = 2 captures DirectComposition / WebView2 surfaces
        ok = u32.PrintWindow(hwnd, mem_dc, 2) or u32.PrintWindow(hwnd, mem_dc, 0)
        if not ok:
            return None

        bmi = _BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB

        buf = ctypes.create_string_buffer(w * h * 4)
        rows = g32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        if rows <= 0:
            return None
        return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1).copy()
    except Exception:
        return None
    finally:
        if old_obj and mem_dc:
            g32.SelectObject(mem_dc, old_obj)
        if bmp:
            g32.DeleteObject(bmp)
        if mem_dc:
            g32.DeleteDC(mem_dc)
        u32.ReleaseDC(hwnd, hwnd_dc)


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
    img = None
    if ImageGrab is not None:
        try:
            left, top, right, bottom = snapshot.rect
            if right > left and bottom > top:
                img = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
        except Exception:
            img = None
    if img is None:
        img = _capture_window_printwindow(snapshot.hwnd)
    if img is not None:
        try:
            screenshot_path = output_dir / f"popup_{ts}_{fp}.png"
            img.save(screenshot_path)
        except Exception:
            screenshot_path = None

    return json_path, screenshot_path


def _find_uia_control(
    win: Any,
    control_type: str | None,
    search_depth: int = 10,
    **criteria: Any,
) -> Any:
    """Resolve a typed UIA child control under win with the given lookup criteria."""
    if control_type == "CheckBoxControl":
        return win.CheckBoxControl(searchDepth=search_depth, **criteria)
    if control_type == "ButtonControl":
        return win.ButtonControl(searchDepth=search_depth, **criteria)
    return win.Control(searchDepth=search_depth, **criteria)


def _invoke_uia_control(
    target: Any,
    control_type: str | None = None,
    desired_state: int = 1,
) -> bool:
    """Trigger an already-located UIA control via Toggle, Invoke, or LegacyIAccessible."""
    try:
        if hasattr(target, "IsEnabled") and not target.IsEnabled:
            return False
    except Exception:
        pass

    effective_type = control_type or getattr(target, "ControlTypeName", None)
    if effective_type == "CheckBoxControl":
        tp = target.GetTogglePattern()
        if tp is not None:
            if tp.ToggleState != desired_state:
                tp.Toggle()
            return True

    ip = target.GetInvokePattern()
    if ip is not None:
        ip.Invoke()
        return True

    try:
        lp = target.GetLegacyIAccessiblePattern()
        if lp is not None:
            lp.DoDefaultAction()
            return True
    except Exception:
        pass

    return False


def uia_invoke_direct(
    hwnd: int,
    automation_id: str | None = None,
    control_names: list[str] | None = None,
    control_type: str | None = "ButtonControl",
    desired_state: int = 1,
    search_depth: int = 8,
    timeout_sec: float = 1.0,
) -> bool:
    """Directly locate and invoke a single UIA control by AutomationId/Name without scanning all buttons."""
    _ensure_windows()
    _ensure_default_desktop()
    try:
        win = auto.ControlFromHandle(hwnd)
        if not win:
            return False

        target = None
        if automation_id:
            cand = _find_uia_control(
                win,
                control_type,
                search_depth=search_depth,
                AutomationId=automation_id,
            )
            if cand.Exists(timeout_sec, 0.1):
                target = cand

        if target is None and control_names:
            for name in control_names:
                cand = _find_uia_control(
                    win,
                    control_type,
                    search_depth=search_depth,
                    Name=name,
                )
                if cand.Exists(timeout_sec, 0.1):
                    target = cand
                    break

        if target is None:
            return False

        return _invoke_uia_control(target, control_type=control_type, desired_state=desired_state)
    except Exception:
        return False


def uia_trigger_element(
    hwnd: int,
    element: UIElement,
    desired_state: int = 1,
    search_depth: int = 10,
) -> bool:
    """Invoke or toggle a UIA control directly by HWND + metadata without mouse coordinates."""
    return uia_invoke_direct(
        hwnd=hwnd,
        automation_id=element.automation_id or None,
        control_names=[element.name] if element.name else None,
        control_type=element.control_type,
        desired_state=desired_state,
        search_depth=search_depth,
        timeout_sec=2.0,
    )

