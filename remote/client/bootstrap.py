"""Elevated supervisor & rollback bootstrap for MaxAgent (F-01, F-12, F-19).

Self-contained pure-stdlib script designed to run both as ``runtime/bootstrap.py``
inside a portable ``python-embed`` bundle and as ``python -m remote.client.bootstrap``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
import uuid

UPDATE_RESTART_EXIT_CODE = 42
MAX_VERIFY_ATTEMPTS = 2

_PRE_UAC_TITLE = "MaxAgent — Elevation Required"
_PRE_UAC_BODY = (
    "MaxAgent requires Administrator privileges to install 3ds Max, "
    "configure Windows security settings, and run automation scripts.\n\n"
    "Click OK to continue to the Windows UAC prompt."
)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON via same-directory ``.tmp`` + ``fsync`` + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp_path = path.parent / tmp_name
    payload = json.dumps(data, indent=2, sort_keys=False) + "\n"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _quarantine_corrupt_file(path: Path) -> None:
    """Back up a corrupt JSON file to ``<name>.corrupt.<ts>`` so bootstrap can recover."""
    backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
    if backup.exists():
        backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}.{uuid.uuid4().hex[:6]}")
    try:
        os.replace(path, backup)
    except OSError:
        pass


def load_active_json(active_path: Path) -> dict[str, Any]:
    """Load ``app/active.json`` with normalized defaults, quarantining corrupt files."""
    state: dict[str, Any] = {
        "version": "1.0.0",
        "previous": None,
        "pending_verify": False,
        "attempts": 0,
        "blocked_versions": [],
    }
    if not active_path.is_file():
        return state
    try:
        raw = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _quarantine_corrupt_file(active_path)
        return state
    if not isinstance(raw, dict):
        _quarantine_corrupt_file(active_path)
        return state
    state["version"] = str(raw.get("version") or "1.0.0")
    prev = raw.get("previous")
    state["previous"] = str(prev) if prev else None
    state["pending_verify"] = bool(raw.get("pending_verify", False))
    raw_attempts = raw.get("attempts", 0)
    try:
        state["attempts"] = max(0, int(raw_attempts if raw_attempts is not None else 0))
    except (TypeError, ValueError):
        state["attempts"] = 0
    blocked = raw.get("blocked_versions")
    if isinstance(blocked, list):
        state["blocked_versions"] = [str(v) for v in blocked if v]
    return state


def rollback_active_state(active_path: Path, broken_version: str) -> dict[str, Any]:
    """Roll back ``app/active.json`` to ``previous`` and blacklist ``broken_version``."""
    state = load_active_json(active_path)
    previous = state.get("previous")
    if not previous:
        raise RuntimeError(f"Cannot roll back from {broken_version}: no previous version set")
    blocked = list(state.get("blocked_versions", []))
    if broken_version and broken_version not in blocked:
        blocked.append(broken_version)
    new_state: dict[str, Any] = {
        "version": str(previous),
        "previous": None,
        "pending_verify": False,
        "attempts": 0,
        "blocked_versions": blocked,
    }
    atomic_write_json(active_path, new_state)
    return new_state


def get_session_id() -> int:
    """Return the current OS session ID (Windows ProcessIdToSessionId; 1 on POSIX)."""
    if os.name != "nt":
        return 1
    try:
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        pid = kernel32.GetCurrentProcessId()
        sid = ctypes.c_uint32(0)
        ok = kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
        if ok:
            return int(sid.value)
    except Exception:
        pass
    return 1


def is_user_admin() -> bool:
    """Return True if the current process has Administrator/root privileges."""
    if os.name == "nt":
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
        except Exception:
            return False
    return hasattr(os, "geteuid") and os.geteuid() == 0


def show_pre_uac_notice(
    title: str = _PRE_UAC_TITLE,
    body: str = _PRE_UAC_BODY,
) -> None:
    """Display a pre-UAC informational dialog on Windows interactive sessions."""
    if os.name != "nt":
        return
    try:
        # MB_OK (0x0) | MB_ICONINFORMATION (0x40) | MB_SETFOREGROUND (0x10000)
        ctypes.windll.user32.MessageBoxW(None, body, title, 0x00010040)  # type: ignore[attr-defined]
    except Exception:
        pass


def ensure_elevated_interactive(
    *,
    no_elevate: bool = False,
    interactive: bool = True,
    argv: list[str] | None = None,
) -> bool:
    """Verify interactive desktop session (SessionId > 0) and elevate once via UAC on Windows.

    Returns ``True`` if execution should continue in this process, or ``False`` if
    an elevated child was launched and the non-elevated parent should exit.
    """
    if os.name != "nt":
        return True

    session_id = get_session_id()
    if session_id == 0:
        raise RuntimeError(
            "Refusing to run in non-interactive Session 0. "
            "Start MaxAgent inside an interactive desktop session."
        )

    if no_elevate or is_user_admin():
        return True

    if interactive:
        show_pre_uac_notice()

    script_path = os.path.abspath(__file__)
    forwarded = list(argv) if argv is not None else sys.argv[1:]
    params = subprocess.list2cmdline([script_path, *forwarded])
    rc = ctypes.windll.shell32.ShellExecuteW(  # type: ignore[attr-defined]
        None,
        "runas",
        sys.executable,
        params,
        os.getcwd(),
        1,
    )
    if int(rc) <= 32:
        raise RuntimeError(f"UAC elevation failed or was cancelled (ShellExecuteW rc={rc})")
    return False


def resolve_root_dir(explicit_root: str | Path | None = None) -> Path:
    """Resolve the portable application root containing ``app/`` and ``runtime/``."""
    if explicit_root:
        return Path(explicit_root).resolve()
    here = Path(__file__).resolve()
    # Case 1: <root>/runtime/bootstrap.py -> parent.parent == <root>
    candidate = here.parent.parent
    if (candidate / "app").is_dir():
        return candidate
    # Case 2: <root>/remote/client/bootstrap.py -> parent.parent.parent == <root>
    repo_candidate = here.parent.parent.parent
    if (repo_candidate / "app").is_dir():
        return repo_candidate
    return candidate


def build_child_env(
    release_dir: Path,
    root_dir: Path,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build environment variables for the supervised ``remote.client.main`` child."""
    env = dict(base_env if base_env is not None else os.environ)
    existing_pp = env.get("PYTHONPATH", "")
    rel_str = str(release_dir)
    env["PYTHONPATH"] = f"{rel_str}{os.pathsep}{existing_pp}" if existing_pp else rel_str
    env["MAX_APP_ROOT"] = str(root_dir)
    env["MAX_APP_RELEASE_DIR"] = rel_str

    ahk_dir = root_dir / "tools" / "ahk"
    if ahk_dir.is_dir():
        existing_path = env.get("PATH", "")
        env["PATH"] = f"{ahk_dir}{os.pathsep}{existing_path}" if existing_path else str(ahk_dir)
    return env


def build_child_command(
    python_exe: str,
    child_args: list[str] | None = None,
) -> list[str]:
    """Build the command line for launching ``remote.client.main``."""
    cmd = [python_exe, "-u", "-m", "remote.client.main"]
    if child_args:
        cmd.extend(child_args)
    return cmd


def run_supervisor(
    root_dir: Path,
    *,
    child_args: list[str] | None = None,
    no_elevate: bool = False,
    python_exe: str | None = None,
    max_attempts: int = MAX_VERIFY_ATTEMPTS,
    raw_argv: list[str] | None = None,
) -> int:
    """Supervise ``remote.client.main`` with exit-42 hot-swap and crash rollback."""
    should_continue = ensure_elevated_interactive(
        no_elevate=no_elevate,
        interactive=True,
        argv=raw_argv,
    )
    if not should_continue:
        return 0

    app_dir = root_dir / "app"
    active_path = app_dir / "active.json"
    releases_dir = app_dir / "releases"
    exe = python_exe or sys.executable

    while True:
        active = load_active_json(active_path)
        version = str(active["version"])
        previous = active.get("previous")
        pending_verify = bool(active.get("pending_verify", False))
        attempts = int(active.get("attempts", 0))

        prev_dir = releases_dir / str(previous) if previous else None
        has_valid_prev = bool(previous and prev_dir and prev_dir.is_dir())

        if pending_verify and attempts >= max_attempts:
            if has_valid_prev:
                sys.stderr.write(
                    f"[bootstrap] Release {version} exceeded {max_attempts} verification attempts; "
                    f"rolling back to {previous}\n"
                )
                sys.stderr.flush()
                rollback_active_state(active_path, version)
                continue
            sys.stderr.write(
                f"[bootstrap] Release {version} failed verification and no previous release exists.\n"
            )
            sys.stderr.flush()
            return 1

        release_dir = releases_dir / version
        if not release_dir.is_dir():
            if pending_verify and has_valid_prev:
                rollback_active_state(active_path, version)
                continue
            sys.stderr.write(f"[bootstrap] Active release directory not found: {release_dir}\n")
            sys.stderr.flush()
            return 1

        if pending_verify:
            active["attempts"] = attempts + 1
            atomic_write_json(active_path, active)

        cmd = build_child_command(exe, child_args)
        env = build_child_env(release_dir, root_dir)
        proc = subprocess.run(cmd, cwd=str(release_dir), env=env)
        rc = int(proc.returncode)

        if rc == UPDATE_RESTART_EXIT_CODE:
            # Hot-swap: re-read app/active.json and restart without second UAC.
            continue

        if rc == 0:
            return 0

        # Non-zero crash
        latest = load_active_json(active_path)
        if latest.get("pending_verify"):
            latest_attempts = int(latest.get("attempts", 0))
            latest_prev = latest.get("previous")
            latest_prev_dir = releases_dir / str(latest_prev) if latest_prev else None
            if latest_attempts >= max_attempts:
                if latest_prev and latest_prev_dir and latest_prev_dir.is_dir():
                    sys.stderr.write(
                        f"[bootstrap] Release {latest['version']} crashed {latest_attempts} times "
                        f"during verification; rolling back to {latest_prev}\n"
                    )
                    sys.stderr.flush()
                    rollback_active_state(active_path, str(latest["version"]))
                    continue
                sys.stderr.write(
                    f"[bootstrap] Release {latest['version']} crashed {latest_attempts} times "
                    "and no previous release is available for rollback.\n"
                )
                sys.stderr.flush()
                return rc
            continue

        return rc


def build_arg_parser() -> argparse.ArgumentParser:
    """Create CLI argument parser for ``bootstrap.py``."""
    parser = argparse.ArgumentParser(
        prog="bootstrap.py",
        description="MaxAgent elevated supervisor and atomic update/rollback runner.",
    )
    parser.add_argument(
        "--root",
        "--app-root",
        "--base-dir",
        dest="root_dir",
        default=None,
        help="Portable application root containing app/ and runtime/.",
    )
    parser.add_argument(
        "--no-elevate",
        "--skip-elevation",
        "--no-uac",
        dest="no_elevate",
        action="store_true",
        help="Skip Windows UAC self-elevation check.",
    )
    parser.add_argument(
        "--python-exe",
        dest="python_exe",
        default=None,
        help="Optional Python executable path for launching child processes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the elevated supervisor."""
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_arg_parser()
    known, extra_args = parser.parse_known_args(raw_argv)
    root_dir = resolve_root_dir(known.root_dir)
    try:
        return run_supervisor(
            root_dir,
            child_args=extra_args,
            no_elevate=bool(known.no_elevate),
            python_exe=known.python_exe,
            raw_argv=raw_argv,
        )
    except RuntimeError as exc:
        sys.stderr.write(f"[bootstrap] ERROR: {exc}\n")
        sys.stderr.flush()
        return 1


__all__ = [
    "UPDATE_RESTART_EXIT_CODE",
    "MAX_VERIFY_ATTEMPTS",
    "atomic_write_json",
    "load_active_json",
    "rollback_active_state",
    "get_session_id",
    "is_user_admin",
    "show_pre_uac_notice",
    "ensure_elevated_interactive",
    "resolve_root_dir",
    "build_child_env",
    "build_child_command",
    "run_supervisor",
    "build_arg_parser",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
