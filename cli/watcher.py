"""Popup watcher that matches UIA windows against rules.yaml and triggers AHK."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from cli.ahk_bridge import AHKBridge
from cli.ui_inspector import (
    UIElement,
    WindowSnapshot,
    save_unknown_window_dump,
    scan_windows,
)


@dataclass
class WatcherConfig:
    target_processes: list[str]
    ignore_windows: list[dict[str, Any]]
    rules: list[dict[str, Any]]

    @classmethod
    def from_yaml(cls, path: Path) -> WatcherConfig:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            target_processes=data.get("target_processes", ["3dsmax", "Autodesk"]),
            ignore_windows=data.get("ignore_windows", []),
            rules=data.get("rules", []),
        )


def _matches_rule(snapshot: WindowSnapshot, rule: dict[str, Any]) -> bool:
    proc_sub = rule.get("process_contains")
    if proc_sub and proc_sub.lower() not in snapshot.process_name.lower():
        return False

    title_regex = rule.get("title_regex")
    if title_regex:
        if not re.search(title_regex, snapshot.title):
            return False

    text_contains = rule.get("text_contains")
    if text_contains:
        haystack = " ".join(
            [snapshot.title, *snapshot.texts, *(e.name for e in snapshot.elements)]
        ).lower()
        if not any(t.lower() in haystack for t in text_contains):
            return False

    return True


def _find_target_element(
    snapshot: WindowSnapshot,
    action: dict[str, Any],
) -> UIElement | None:
    auto_id = action.get("automation_id")
    if auto_id:
        for el in snapshot.elements:
            if el.is_enabled and el.automation_id == auto_id:
                return el

    candidates = [c.lower() for c in action.get("control_names", [])]
    # First pass: exact match (case-insensitive)
    for cand in candidates:
        for el in snapshot.elements:
            if el.is_enabled and el.name.lower() == cand:
                return el
    # Second pass: substring match
    for cand in candidates:
        for el in snapshot.elements:
            if el.is_enabled and cand in el.name.lower():
                return el
    return None


def watch_loop(
    config: WatcherConfig,
    ahk: AHKBridge,
    poll_interval: float = 1.0,
    timeout_sec: float = 300.0,
    learn: bool = True,
    dump_dir: Path = Path("logs/unknown_popups"),
    stop_on_main_ready: bool = False,
) -> None:
    """Continuously scan windows, execute matching rules, and dump unknown popups."""
    start_time = time.monotonic()
    last_rule_hit: dict[str, float] = {}
    seen_unknown_fps: set[str] = set()

    print(
        f"[WATCH] Started (processes={config.target_processes}, "
        f"timeout={timeout_sec}s, learn={learn}, dry_run={ahk.dry_run})"
    )

    while True:
        elapsed = time.monotonic() - start_time
        if timeout_sec > 0 and elapsed >= timeout_sec:
            print(f"[WATCH] Timeout reached ({timeout_sec}s). Exiting.")
            break

        snapshots = scan_windows(process_filters=config.target_processes)
        for snap in snapshots:
            # Skip ignored windows (like the main 3ds Max viewport)
            if any(_matches_rule(snap, ig) for ig in config.ignore_windows):
                if stop_on_main_ready:
                    print(f"[WATCH] Main window detected: '{snap.title}'. Stopping watcher.")
                    return
                continue

            matched = False
            for rule in config.rules:
                if not _matches_rule(snap, rule):
                    continue

                rule_name = rule.get("name", "unnamed")
                cooldown = float(rule.get("cooldown_sec", 2.0))
                key = f"{rule_name}:{snap.hwnd}"
                now = time.monotonic()
                if now - last_rule_hit.get(key, 0.0) < cooldown:
                    matched = True
                    break

                action = rule.get("action", {})
                action_type = action.get("type", "click_control")

                if action_type == "click_control":
                    el = _find_target_element(snap, action)
                    if el:
                        cx, cy = el.center
                        print(
                            f"[RULE '{rule_name}'] Clicking '{el.name}' "
                            f"at ({cx}, {cy}) in '{snap.title}' ({snap.process_name})"
                        )
                        ahk.click(cx, cy, hwnd=snap.hwnd)
                        last_rule_hit[key] = now
                        matched = True
                        break
                elif action_type == "send_keys":
                    keys = action.get("keys", "{Enter}")
                    print(
                        f"[RULE '{rule_name}'] Sending keys '{keys}' "
                        f"to '{snap.title}' ({snap.process_name})"
                    )
                    ahk.send_keys(keys, hwnd=snap.hwnd)
                    last_rule_hit[key] = now
                    matched = True
                    break
                elif action_type == "close":
                    print(f"[RULE '{rule_name}'] Closing '{snap.title}'")
                    ahk.close_window(snap.hwnd)
                    last_rule_hit[key] = now
                    matched = True
                    break

            if not matched and learn:
                fp = snap.fingerprint()
                if fp not in seen_unknown_fps and (snap.elements or snap.title):
                    seen_unknown_fps.add(fp)
                    json_p, png_p = save_unknown_window_dump(snap, dump_dir)
                    btns = [f"'{e.name}' ({e.control_type})" for e in snap.elements if e.name]
                    print(
                        f"[UNKNOWN POPUP] '{snap.title}' ({snap.process_name}) | "
                        f"Controls: {', '.join(btns[:8]) or 'none'}\n"
                        f"  -> Saved UI dump: {json_p}"
                        + (f"\n  -> Saved screenshot: {png_p}" if png_p else "")
                    )

        time.sleep(poll_interval)
