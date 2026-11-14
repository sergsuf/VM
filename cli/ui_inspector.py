"""Windows UI Automation (UIA) inspector for discovering popups and clickable controls."""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

if sys.platform == "win32":
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
    max_depth: int = 8,
    max_elements: int = 120,
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

        try:
            children = node.GetChildren()
            for child in children:
                queue.append((child, depth + 1))
        except Exception:
            pass

    return elements, texts


def scan_windows(
    process_filters: list[str] | None = None,
    include_all: bool = False,
    max_depth: int = 8,
) -> list[WindowSnapshot]:
    """Scan currently visible top-level windows and their interactive controls."""
    _ensure_windows()
    filters_lower = [p.lower() for p in (process_filters or [])]
    snapshots: list[WindowSnapshot] = []

    root = auto.GetRootControl()
    for win in root.GetChildren():
        try:
            if win.IsOffscreen:
                continue
            rect_obj = win.BoundingRectangle
            if rect_obj.width() <= 0 or rect_obj.height() <= 0:
                continue

            pid = win.ProcessId
            proc_name = _get_process_name(pid)
            if not include_all and filters_lower:
                if not any(f in proc_name.lower() for f in filters_lower):
                    continue

            title = (win.Name or "").strip()
            cls = (win.ClassName or "").strip()
            hwnd = int(win.NativeWindowHandle or 0)
            rect = (rect_obj.left, rect_obj.top, rect_obj.right, rect_obj.bottom)

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
