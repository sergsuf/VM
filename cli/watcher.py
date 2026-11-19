"""Popup watcher that matches UIA windows against rules.yaml and triggers AHK."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil
import yaml

from cli.ahk_bridge import AHKBridge
from cli.ui_inspector import (
    UIElement,
    WindowSnapshot,
    save_unknown_window_dump,
    scan_windows,
    uia_trigger_element,
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


def _execute_action(
    snap: WindowSnapshot,
    action: dict[str, Any],
    rule_name: str,
    ahk: AHKBridge,
) -> bool:
    """Execute a single rule action or sequence of steps against a window snapshot."""
    action_type = action.get("type", "click_control")

    if action_type == "sequence":
        steps = action.get("steps", [])
        any_executed = False
        for step in steps:
            if _execute_action(snap, step, rule_name, ahk):
                any_executed = True
                time.sleep(float(step.get("delay_after_sec", 0.25)))
        return any_executed

    if action_type == "click_control":
        el = _find_target_element(snap, action)
        if not el:
            return False
        cx, cy = el.center
        desired_state = 0 if action.get("state") in (0, False, "0", "off", "false") else 1
        print(
            f"[RULE '{rule_name}'] Clicking '{el.name}' "
            f"at ({cx}, {cy}) in '{snap.title}' ({snap.process_name})"
        )
        if not ahk.dry_run and uia_trigger_element(snap.hwnd, el, desired_state=desired_state):
            return True
        ahk.click(cx, cy, hwnd=snap.hwnd)
        return True

    if action_type == "click_relative":
        rel_x = float(action.get("rel_x", 0.5))
        rel_y = float(action.get("rel_y", 0.5))
        left, top, right, bottom = snap.rect
        cx = int(left + (right - left) * rel_x)
        cy = int(top + (bottom - top) * rel_y)
        print(
            f"[RULE '{rule_name}'] Clicking relative ({rel_x:.2f}, {rel_y:.2f}) "
            f"-> ({cx}, {cy}) in '{snap.title}' ({snap.process_name})"
        )
        ahk.click(cx, cy, hwnd=snap.hwnd)
        return True

    if action_type == "send_keys":
        keys = action.get("keys", "{Enter}")
        print(
            f"[RULE '{rule_name}'] Sending keys '{keys}' "
            f"to '{snap.title}' ({snap.process_name})"
        )
        ahk.send_keys(keys, hwnd=snap.hwnd)
        return True

    if action_type == "close":
        print(f"[RULE '{rule_name}'] Closing '{snap.title}'")
        ahk.close_window(snap.hwnd)
        return True

    return False


def resolve_rules_by_name(
    rules: list[dict[str, Any]],
    names: list[str],
) -> list[dict[str, Any]]:
    """Select rules matching any requested name (case-insensitive exact or substring)."""
    selected: list[dict[str, Any]] = []
    for query in names:
        q = query.strip().lower()
        exact = [r for r in rules if r.get("name", "").lower() == q]
        matches = exact or [r for r in rules if q in r.get("name", "").lower()]
        if not matches:
            available = ", ".join(repr(r.get("name", "")) for r in rules)
            raise ValueError(f"Rule not found: {query!r}. Available: {available}")
        for r in matches:
            if r not in selected:
                selected.append(r)
    return selected


def wait_for_window(
    config: WatcherConfig,
    ahk: AHKBridge,
    rule_names: list[str] | None = None,
    process_filters: list[str] | None = None,
    title_regex: str | None = None,
    text_contains: list[str] | None = None,
    act: bool = False,
    click_control: str | None = None,
    poll_interval: float = 0.5,
    timeout_sec: float = 60.0,
) -> bool:
    """Wait until a specific window/rule appears (and optionally act on it), then return."""
    if rule_names:
        target_rules = resolve_rules_by_name(config.rules, rule_names)
        proc_filters = process_filters or sorted(
            {
                r["process_contains"]
                for r in target_rules
                if r.get("process_contains")
            }
        ) or config.target_processes
    else:
        target_rules = [
            {
                "name": "ad-hoc",
                "title_regex": title_regex,
                "text_contains": text_contains or None,
            }
        ]
        proc_filters = process_filters or config.target_processes

    title_rx_filter: list[str] | None = None
    if all(r.get("title_regex") for r in target_rules):
        title_rx_filter = [r["title_regex"] for r in target_rules]

    start_time = time.monotonic()
    print(
        f"[WAIT] Waiting for {[r.get('name') for r in target_rules]} "
        f"(processes={proc_filters}, timeout={timeout_sec}s, act={act or bool(click_control)})"
    )

    while True:
        snapshots = scan_windows(process_filters=proc_filters, title_regexes=title_rx_filter)
        for snap in snapshots:
            for rule in target_rules:
                if not _matches_rule(snap, rule):
                    continue
                rule_name = rule.get("name", "unnamed")
                print(
                    f"[WAIT] Matched '{rule_name}' on '{snap.title}' "
                    f"({snap.process_name}, HWND {snap.hwnd})"
                )
                if click_control:
                    action = {"type": "click_control", "control_names": [click_control]}
                    if _execute_action(snap, action, rule_name, ahk):
                        return True
                elif act and rule_names:
                    action = rule.get("action", {})
                    if _execute_action(snap, action, rule_name, ahk):
                        return True
                else:
                    return True

        elapsed = time.monotonic() - start_time
        if timeout_sec > 0 and elapsed >= timeout_sec:
            print(f"[WAIT] Timeout reached ({timeout_sec}s). Target window not found.")
            return False

        time.sleep(poll_interval)


def _is_process_running(process_substrings: tuple[str, ...] | list[str]) -> bool:
    """Return True if any running process name contains one of the given substrings."""
    needles = [s.lower() for s in process_substrings if s]
    if not needles:
        return False
    for proc in psutil.process_iter(["name"]):
        try:
            name = (proc.info.get("name") or "").lower()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name and any(n in name for n in needles):
            return True
    return False


def watch_loop(
    config: WatcherConfig,
    ahk: AHKBridge,
    poll_interval: float = 1.0,
    timeout_sec: float = 20.0,
    learn: bool = True,
    dump_dir: Path = Path("logs/unknown_popups"),
    stop_on_main_ready: bool = False,
    ready_grace_sec: float = 10.0,
    only_rules: list[str] | None = None,
    stop_after_all: bool = False,
    wait_while_running: tuple[str, ...] = ("3dsmax",),
) -> None:
    """Continuously scan windows, execute matching rules, and dump unknown popups."""
    active_rules = (
        resolve_rules_by_name(config.rules, only_rules)
        if only_rules
        else config.rules
    )
    completed_rules: set[str] = set()
    start_time = time.monotonic()
    last_rule_hit: dict[str, float] = {}
    seen_unknown_fps: set[str] = set()
    main_ready_since: float | None = None
    extended_for_process = False

    print(
        f"[WATCH] Started (processes={config.target_processes}, "
        f"rules={len(active_rules)}, timeout={timeout_sec}s, "
        f"learn={learn}, dry_run={ahk.dry_run})"
    )

    while True:
        elapsed = time.monotonic() - start_time
        if timeout_sec > 0 and elapsed >= timeout_sec:
            if stop_on_main_ready and _is_process_running(wait_while_running):
                if not extended_for_process:
                    extended_for_process = True
                    print(
                        f"[WATCH] Timeout ({timeout_sec}s) reached, but process "
                        f"{wait_while_running} is still running — waiting until ready or exit..."
                    )
            else:
                if extended_for_process:
                    print(f"[WATCH] Process {wait_while_running} exited after timeout. Exiting.")
                else:
                    print(f"[WATCH] Timeout reached ({timeout_sec}s). Exiting.")
                break

        snapshots = scan_windows(process_filters=config.target_processes)
        for snap in snapshots:
            is_ignored = any(_matches_rule(snap, ig) for ig in config.ignore_windows)
            matched = False
            for rule in active_rules:
                if is_ignored and not rule.get("run_before_ignore", False):
                    continue
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
                if _execute_action(snap, action, rule_name, ahk):
                    last_rule_hit[key] = now
                    completed_rules.add(rule_name)
                    matched = True
                    if stop_after_all and len(completed_rules) >= len(active_rules):
                        print(
                            f"[WATCH] All target rules executed "
                            f"({len(completed_rules)}/{len(active_rules)}). Stopping watcher."
                        )
                        return
                    break

            if is_ignored:
                if not matched and stop_on_main_ready:
                    now_ready = time.monotonic()
                    if main_ready_since is None:
                        main_ready_since = now_ready
                        print(
                            f"[WATCH] Main window detected: '{snap.title}'. "
                            f"Waiting {ready_grace_sec:.0f}s for delayed popups..."
                        )
                    elif now_ready - main_ready_since >= ready_grace_sec:
                        print(f"[WATCH] Main window ready: '{snap.title}'. Stopping watcher.")
                        return
                else:
                    main_ready_since = None
                continue

            if not matched and learn:
                fp = snap.fingerprint()
                if fp not in seen_unknown_fps and (snap.elements or snap.texts):
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
