"""Popup watcher that matches UIA windows against rules.yaml and triggers AHK."""

from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:
    import psutil  # type: ignore[import-not-found]
except ImportError:
    psutil = None  # type: ignore[assignment]

from cli.ahk_bridge import AHKBridge
from cli.ui_inspector import (
    UIElement,
    WindowSnapshot,
    list_visible_windows,
    populate_snapshot_controls,
    save_unknown_window_dump,
    uia_invoke_direct,
    uia_trigger_element,
)


@dataclass
class WatchResult:
    status: str  # "ready" | "licensing_required" | "timeout" | "failed" | "aborted"
    detail: str
    matched_rules: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)
    main_workspace_title: str | None = None
    elapsed_sec: float = 0.0
    exit_code: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


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


def _matches_win32_predicates(snapshot: WindowSnapshot, rule: dict[str, Any]) -> bool:
    """Fast match using only Win32 window/child metadata (no UIA tree walk)."""
    proc_sub = rule.get("process_contains")
    if proc_sub and proc_sub.lower() not in snapshot.process_name.lower():
        return False

    class_name = rule.get("class_name")
    if class_name and snapshot.class_name.lower() != class_name.lower():
        return False

    class_name_regex = rule.get("class_name_regex")
    if class_name_regex and not re.search(class_name_regex, snapshot.class_name):
        return False

    title_regex = rule.get("title_regex")
    if title_regex:
        if not re.search(title_regex, snapshot.title):
            return False

    any_title_contains = rule.get("any_title_contains")
    if any_title_contains:
        titles_haystack = " ".join([snapshot.title, *snapshot.child_titles]).lower()
        if not any(t.lower() in titles_haystack for t in any_title_contains):
            return False

    if "has_webview" in rule:
        has_wv = snapshot.webview_hwnd is not None
        if bool(rule["has_webview"]) != has_wv:
            return False

    return True


def _matches_rule(snapshot: WindowSnapshot, rule: dict[str, Any]) -> bool:
    if not _matches_win32_predicates(snapshot, rule):
        return False

    text_contains = rule.get("text_contains")
    if text_contains:
        haystack = " ".join(
            [
                snapshot.title,
                *snapshot.child_titles,
                *snapshot.texts,
                *(e.name for e in snapshot.elements),
            ]
        ).lower()
        if not any(t.lower() in haystack for t in text_contains):
            return False

    return True


def _action_needs_ui_scan(action: dict[str, Any]) -> bool:
    action_type = action.get("type", "click_control")
    if action_type == "sequence":
        return any(_action_needs_ui_scan(step) for step in action.get("steps", []))
    return action_type not in {"uia_invoke", "close", "send_keys", "click_relative"}


def _rule_needs_ui_scan(rule: dict[str, Any]) -> bool:
    if rule.get("text_contains"):
        return True
    if "action" in rule:
        return _action_needs_ui_scan(rule["action"])
    return False


def _find_target_element(
    snapshot: WindowSnapshot,
    action: dict[str, Any],
) -> UIElement | None:
    auto_ids: list[str] = list(action.get("automation_ids") or [])
    if action.get("automation_id"):
        auto_ids.insert(0, action["automation_id"])
    for auto_id in auto_ids:
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

    if action_type == "uia_invoke":
        auto_id = action.get("automation_id")
        control_names = action.get("control_names") or []
        control_type = action.get("control_type", "ButtonControl")
        use_webview = bool(action.get("use_webview", True))
        target_hwnd = snap.webview_hwnd if (use_webview and snap.webview_hwnd) else snap.hwnd
        default_depth = 8 if (use_webview and snap.webview_hwnd) else 10
        search_depth = int(action.get("search_depth", default_depth))
        timeout_sec = float(action.get("timeout_sec", 1.0))
        desired_state = 0 if action.get("state") in (0, False, "0", "off", "false") else 1
        target_desc = f"id={auto_id!r}" if auto_id else f"names={control_names!r}"

        if ahk.dry_run:
            print(
                f"[RULE '{rule_name}'] [DRY-RUN] Would UIA-invoke {target_desc} "
                f"in '{snap.display_title}' ({snap.process_name}, hwnd={target_hwnd})"
            )
            return True

        if uia_invoke_direct(
            hwnd=target_hwnd,
            automation_id=auto_id,
            control_names=control_names,
            control_type=control_type,
            desired_state=desired_state,
            search_depth=search_depth,
            timeout_sec=timeout_sec,
        ):
            print(
                f"[RULE '{rule_name}'] UIA-invoked {target_desc} "
                f"in '{snap.display_title}' ({snap.process_name}, hwnd={target_hwnd})"
            )
            return True
        return False

    if action_type == "click_control":
        el = _find_target_element(snap, action)
        if not el:
            return False
        cx, cy = el.center
        desired_state = 0 if action.get("state") in (0, False, "0", "off", "false") else 1
        target_hwnd = snap.webview_hwnd or snap.hwnd
        search_depth = 8 if snap.webview_hwnd else 10
        print(
            f"[RULE '{rule_name}'] Clicking '{el.name}' "
            f"at ({cx}, {cy}) in '{snap.display_title}' ({snap.process_name})"
        )
        if not ahk.dry_run and uia_trigger_element(
            target_hwnd,
            el,
            desired_state=desired_state,
            search_depth=search_depth,
        ):
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
            f"-> ({cx}, {cy}) in '{snap.display_title}' ({snap.process_name})"
        )
        ahk.click(cx, cy, hwnd=snap.hwnd)
        return True

    if action_type == "send_keys":
        keys = action.get("keys", "{Enter}")
        print(
            f"[RULE '{rule_name}'] Sending keys '{keys}' "
            f"to '{snap.display_title}' ({snap.process_name})"
        )
        ahk.send_keys(keys, hwnd=snap.hwnd)
        return True

    if action_type == "close":
        print(f"[RULE '{rule_name}'] Closing '{snap.display_title}'")
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
        snapshots = list_visible_windows(process_filters=proc_filters, title_regexes=title_rx_filter)
        for snap in snapshots:
            controls_loaded = False
            for rule in target_rules:
                if not _matches_win32_predicates(snap, rule):
                    continue
                needs_scan = bool(click_control) or _rule_needs_ui_scan(rule)
                if needs_scan and not controls_loaded:
                    populate_snapshot_controls(snap)
                    controls_loaded = True
                if not _matches_rule(snap, rule):
                    continue
                rule_name = rule.get("name", "unnamed")
                print(
                    f"[WAIT] Matched '{rule_name}' on '{snap.display_title}' "
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


DEFAULT_DUMP_DIR = Path(__file__).resolve().parent.parent / "logs" / "unknown_popups"


def is_process_running(process_substrings: tuple[str, ...] | list[str]) -> bool:
    """Return True if any running process name contains one of the given substrings."""
    needles = [s.lower() for s in process_substrings if s]
    if not needles:
        return False
    if psutil is not None:
        for proc in psutil.process_iter(["name"]):
            try:
                name = (proc.info.get("name") or "").lower()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            if name and any(n in name for n in needles):
                return True
        return False
    if sys.platform == "win32":
        try:
            res = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            for line in res.stdout.splitlines():
                proc_name = line.split(",", 1)[0].strip().strip('"').lower()
                if proc_name and any(n in proc_name for n in needles):
                    return True
        except Exception:
            pass
        return False
    proc_root = Path("/proc")
    if proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                name = (entry / "comm").read_text(encoding="utf-8", errors="ignore").strip().lower()
                if name and any(n in name for n in needles):
                    return True
            except OSError:
                continue
    return False


def _is_main_workspace_window(
    snapshot: WindowSnapshot,
    ignore_windows: list[dict[str, Any]],
) -> bool:
    """Return True if snapshot matches the primary 3ds Max workspace window."""
    explicit_main = [ig for ig in ignore_windows if ig.get("is_main_workspace")]
    candidates = explicit_main if explicit_main else ignore_windows
    return any(_matches_rule(snapshot, ig) for ig in candidates)


def _is_client_licensing_rule(rule: dict[str, Any], snap: WindowSnapshot) -> bool:
    """Return True if matched rule/window represents an interactive licensing or sign-in gate."""
    if str(rule.get("client_action", "")).lower() == "licensing_required":
        return True
    if rule.get("is_licensing") is True:
        return True
    proc_lower = snap.process_name.lower()
    if "adsklicensingagent" in proc_lower or "adskidentitymanager" in proc_lower:
        return True
    if "license startup error" in snap.display_title.lower():
        return True
    return False


def watch_loop(
    config: WatcherConfig,
    ahk: AHKBridge,
    poll_interval: float = 1.0,
    timeout_sec: float = 20.0,
    learn: bool = True,
    dump_dir: Path = DEFAULT_DUMP_DIR,
    stop_on_main_ready: bool = False,
    ready_grace_sec: float = 10.0,
    only_rules: list[str] | None = None,
    stop_after_all: bool = False,
    wait_while_running: tuple[str, ...] = ("3dsmax",),
    profile: str = "vm-test",
    max_timeout_sec: float | None = None,
    cancel_event: threading.Event | None = None,
    fail_if_process_exits: bool = False,
) -> WatchResult:
    """Continuously scan windows, execute matching rules, and dump unknown popups."""
    normalized_profile = (profile or "vm-test").strip().lower()
    if normalized_profile not in ("vm-test", "client"):
        raise ValueError(f"Unsupported watcher profile: {profile!r}. Expected 'vm-test' or 'client'.")

    active_rules = (
        resolve_rules_by_name(config.rules, only_rules)
        if only_rules
        else config.rules
    )
    completed_rules: set[str] = set()
    matched_rules_ordered: list[str] = []
    screenshots: list[str] = []
    start_time = time.monotonic()
    last_rule_hit: dict[str, float] = {}
    seen_unknown_fps: set[str] = set()
    main_ready_since: float | None = None
    extended_for_process = False
    process_seen = is_process_running(wait_while_running) if fail_if_process_exits else False

    print(
        f"[WATCH] Started (profile={normalized_profile}, processes={config.target_processes}, "
        f"rules={len(active_rules)}, timeout={timeout_sec}s, max_timeout={max_timeout_sec}, "
        f"learn={learn}, dry_run={ahk.dry_run})"
    )

    while True:
        if cancel_event is not None and cancel_event.is_set():
            elapsed = round(time.monotonic() - start_time, 3)
            print("[WATCH] Cancelled via cancel_event. Exiting.")
            return WatchResult(
                status="aborted",
                detail="Watcher cancelled.",
                matched_rules=matched_rules_ordered,
                screenshots=screenshots,
                elapsed_sec=elapsed,
                exit_code=1,
            )

        elapsed = time.monotonic() - start_time
        if max_timeout_sec is not None and max_timeout_sec > 0 and elapsed >= max_timeout_sec:
            print(f"[WATCH] Hard max-timeout reached ({max_timeout_sec}s). Exiting.")
            return WatchResult(
                status="timeout",
                detail=f"Hard max-timeout ({max_timeout_sec}s) reached.",
                matched_rules=matched_rules_ordered,
                screenshots=screenshots,
                elapsed_sec=round(elapsed, 3),
                exit_code=1,
            )

        if timeout_sec > 0 and elapsed >= timeout_sec:
            if stop_on_main_ready and is_process_running(wait_while_running):
                if not extended_for_process:
                    extended_for_process = True
                    print(
                        f"[WATCH] Timeout ({timeout_sec}s) reached, but process "
                        f"{wait_while_running} is still running — waiting until ready or exit..."
                    )
            else:
                if extended_for_process:
                    detail = f"Process {wait_while_running} exited after timeout."
                    print(f"[WATCH] {detail} Exiting.")
                else:
                    detail = f"Timeout reached ({timeout_sec}s)."
                    print(f"[WATCH] {detail} Exiting.")
                return WatchResult(
                    status="timeout",
                    detail=detail,
                    matched_rules=matched_rules_ordered,
                    screenshots=screenshots,
                    elapsed_sec=round(elapsed, 3),
                    exit_code=1,
                )

        if fail_if_process_exits:
            running_now = is_process_running(wait_while_running)
            if running_now:
                process_seen = True
            elif process_seen or elapsed >= 5.0:
                detail = f"Target process {wait_while_running} exited before main workspace became ready."
                print(f"[WATCH] {detail}")
                return WatchResult(
                    status="failed",
                    detail=detail,
                    matched_rules=matched_rules_ordered,
                    screenshots=screenshots,
                    elapsed_sec=round(elapsed, 3),
                    exit_code=1,
                )

        snapshots = list_visible_windows(process_filters=config.target_processes)
        tick_matched_any_rule = False
        main_workspace_title: str | None = None

        for snap in snapshots:
            controls_loaded = False

            def _ensure_controls() -> None:
                nonlocal controls_loaded
                if not controls_loaded:
                    populate_snapshot_controls(snap)
                    controls_loaded = True

            skip_completely = False
            is_ignored = False
            for ig in config.ignore_windows:
                if not _matches_win32_predicates(snap, ig):
                    continue
                if _rule_needs_ui_scan(ig):
                    _ensure_controls()
                if _matches_rule(snap, ig):
                    is_ignored = True
                    if ig.get("skip_scan"):
                        skip_completely = True
                        break

            if _is_main_workspace_window(snap, config.ignore_windows):
                main_workspace_title = snap.display_title

            if skip_completely:
                continue

            matched = False
            matched_pending_direct = False
            for rule in active_rules:
                if is_ignored and not rule.get("run_before_ignore", False):
                    continue
                if not _matches_win32_predicates(snap, rule):
                    continue
                needs_scan = _rule_needs_ui_scan(rule)
                if needs_scan:
                    _ensure_controls()
                if not _matches_rule(snap, rule):
                    continue

                rule_name = rule.get("name", "unnamed")
                if normalized_profile == "client" and _is_client_licensing_rule(rule, snap):
                    _ensure_controls()
                    json_p, png_p = save_unknown_window_dump(snap, dump_dir)
                    if png_p is not None:
                        screenshots.append(str(png_p))
                    if rule_name not in matched_rules_ordered:
                        matched_rules_ordered.append(rule_name)
                    detail = (
                        f"Licensing/sign-in dialog detected ({rule_name}: "
                        f"{snap.display_title or snap.process_name})."
                    )
                    print(f"[WATCH] [CLIENT] {detail} Dump: {json_p}")
                    return WatchResult(
                        status="licensing_required",
                        detail=detail,
                        matched_rules=matched_rules_ordered,
                        screenshots=screenshots,
                        elapsed_sec=round(time.monotonic() - start_time, 3),
                        exit_code=10,
                    )

                cooldown = float(rule.get("cooldown_sec", 2.0))
                key = f"{rule_name}:{snap.hwnd}"
                now = time.monotonic()
                if now - last_rule_hit.get(key, 0.0) < cooldown:
                    matched = True
                    tick_matched_any_rule = True
                    break

                action = rule.get("action", {})
                if _execute_action(snap, action, rule_name, ahk):
                    last_rule_hit[key] = now
                    completed_rules.add(rule_name)
                    if rule_name not in matched_rules_ordered:
                        matched_rules_ordered.append(rule_name)
                    matched = True
                    tick_matched_any_rule = True
                    if stop_after_all and len(completed_rules) >= len(active_rules):
                        print(
                            f"[WATCH] All target rules executed "
                            f"({len(completed_rules)}/{len(active_rules)}). Stopping watcher."
                        )
                        return WatchResult(
                            status="ready",
                            detail=f"All target rules executed ({len(completed_rules)}/{len(active_rules)}).",
                            matched_rules=matched_rules_ordered,
                            screenshots=screenshots,
                            main_workspace_title=main_workspace_title,
                            elapsed_sec=round(time.monotonic() - start_time, 3),
                            exit_code=0,
                        )
                    break

                if not needs_scan:
                    matched_pending_direct = True
                    tick_matched_any_rule = True
                    break

            if is_ignored or matched or matched_pending_direct:
                continue

            if learn:
                _ensure_controls()
                fp = snap.fingerprint()
                if fp not in seen_unknown_fps and (snap.elements or snap.texts or snap.child_titles):
                    seen_unknown_fps.add(fp)
                    json_p, png_p = save_unknown_window_dump(snap, dump_dir)
                    if png_p is not None:
                        screenshots.append(str(png_p))
                    btns = [f"'{e.name}' ({e.control_type})" for e in snap.elements if e.name]
                    print(
                        f"[UNKNOWN POPUP] '{snap.display_title}' ({snap.process_name}) | "
                        f"Controls: {', '.join(btns[:8]) or 'none'}\n"
                        f"  -> Saved UI dump: {json_p}"
                        + (f"\n  -> Saved screenshot: {png_p}" if png_p else "")
                    )

        if stop_on_main_ready:
            if main_workspace_title is not None and not tick_matched_any_rule:
                now_ready = time.monotonic()
                if main_ready_since is None:
                    main_ready_since = now_ready
                    print(
                        f"[WATCH] Main window detected: '{main_workspace_title}'. "
                        f"Waiting {ready_grace_sec:.0f}s for delayed popups..."
                    )
                elif now_ready - main_ready_since >= ready_grace_sec:
                    print(f"[WATCH] Main window ready: '{main_workspace_title}'. Stopping watcher.")
                    return WatchResult(
                        status="ready",
                        detail=f"Main workspace ready: {main_workspace_title}",
                        matched_rules=matched_rules_ordered,
                        screenshots=screenshots,
                        main_workspace_title=main_workspace_title,
                        elapsed_sec=round(time.monotonic() - start_time, 3),
                        exit_code=0,
                    )
            else:
                main_ready_since = None

        time.sleep(poll_interval)
