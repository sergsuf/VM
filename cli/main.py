"""CLI entrypoint for 3ds Max (2025/2027) VM automation."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import click

from cli.ahk_bridge import AHKBridge
from cli.max_deployer import (
    deploy_scripts,
    find_all_installed_3dsmax,
    find_installed_3dsmax_exe,
    get_default_3dsmax_exe,
    get_installed_3dsmax_exe,
    launch_3dsmax,
)
from cli.ui_inspector import scan_windows
from cli.watcher import (
    DEFAULT_DUMP_DIR,
    WatcherConfig,
    is_process_running,
    wait_for_window,
    watch_loop,
)

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """3ds Max (2025/2027) VM Automation CLI (UIA Inspector + AutoHotkey v2)."""
    if ctx.invoked_subcommand is None:
        ctx.invoke(watch)


@cli.command()
@click.option("--process", "-p", multiple=True, help="Filter by process name substring (e.g. 3dsmax).")
@click.option("--all-windows", is_flag=True, help="Scan all visible top-level windows.")
@click.option("--json-out", type=click.Path(path_type=Path), help="Optional path to save JSON dump.")
def inspect(process: tuple[str, ...], all_windows: bool, json_out: Path | None) -> None:
    """Scan currently visible windows and print their clickable UI controls."""
    filters = list(process) if process else ["3dsmax", "Autodesk", "Adsk"]
    snapshots = scan_windows(process_filters=filters, include_all=all_windows)

    if not snapshots:
        click.echo("No matching visible windows found.")
        return

    for snap in snapshots:
        wv_info = f" | WebView HWND {snap.webview_hwnd}" if snap.webview_hwnd else ""
        click.echo(
            f"\n=== [{snap.process_name} | PID {snap.pid} | HWND {snap.hwnd}{wv_info}] "
            f"'{snap.display_title}' (cls={snap.class_name!r}) ==="
        )
        if snap.child_titles:
            click.echo(f"  Child titles: {' | '.join(snap.child_titles[:6])}")
        if snap.texts:
            click.echo(f"  Texts: {' | '.join(snap.texts[:6])}")
        for el in snap.elements:
            state = "enabled" if el.is_enabled else "disabled"
            click.echo(
                f"  - {el.control_type}: name={el.name!r} id={el.automation_id!r} "
                f"center={el.center} [{state}]"
            )

    if json_out:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(
            json.dumps([s.to_dict() for s in snapshots], indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        click.echo(f"\nSaved JSON dump to: {json_out}")


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), default=DEFAULT_CONFIG)
@click.option("--ahk-exe", type=click.Path(path_type=Path), default=None, help="Path to AutoHotkey64.exe.")
@click.option("--timeout", "-t", type=float, default=20.0, help="Watcher timeout in seconds (0 = infinite).")
@click.option("--interval", type=float, default=1.0, help="Polling interval in seconds.")
@click.option("--learn/--no-learn", default=True, help="Save UI dumps & screenshots of unknown popups.")
@click.option("--dry-run", is_flag=True, help="Log matched rules without clicking.")
@click.option("--stop-on-ready/--no-stop-on-ready", default=True, help="Stop watcher once main 3ds Max window is visible.")
@click.option("--only-rule", "-r", multiple=True, help="Only watch/execute specific rule(s) from rules.yaml.")
@click.option("--stop-after-all", is_flag=True, help="Stop once all --only-rule rules have executed.")
def watch(
    config: Path,
    ahk_exe: Path | None,
    timeout: float,
    interval: float,
    learn: bool,
    dry_run: bool,
    stop_on_ready: bool,
    only_rule: tuple[str, ...],
    stop_after_all: bool,
) -> None:
    """Watch for 3ds Max / Autodesk popups and handle them via AHK v2."""
    cfg = WatcherConfig.from_yaml(config)
    ahk = AHKBridge(ahk_exe=ahk_exe, dry_run=dry_run)
    try:
        watch_loop(
            config=cfg,
            ahk=ahk,
            poll_interval=interval,
            timeout_sec=timeout,
            learn=learn,
            stop_on_main_ready=stop_on_ready,
            only_rules=list(only_rule) if only_rule else None,
            stop_after_all=stop_after_all,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@cli.command("deploy-scripts")
@click.option(
    "--source",
    "-s",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path("max_scripts"),
    help="Directory with .ms/.mse/.mcr/.mzp scripts.",
)
@click.option("--version", default="2025", help="3ds Max version year.")
@click.option("--startup/--no-startup", default=True, help="Deploy .ms/.mse into scripts/startup.")
@click.option("--dry-run", is_flag=True, help="Show target paths without copying files.")
def deploy_cmd(source: Path, version: str, startup: bool, dry_run: bool) -> None:
    """Deploy MAXScript / MacroScript / MZP files into 3ds Max user folders."""
    deployed = deploy_scripts(source, version=version, startup=startup, dry_run=dry_run)
    if not deployed:
        click.echo(f"No .ms/.mse/.mcr/.mzp files found in {source}")
        return
    for p in deployed:
        click.echo(f"Deployed -> {p}")


@cli.command()
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), default=DEFAULT_CONFIG)
@click.option("--ahk-exe", type=click.Path(path_type=Path), default=None)
@click.option("--max-exe", type=click.Path(path_type=Path), default=None, help="Explicit path to 3dsmax.exe.")
@click.option("--version", default=None, help="3ds Max version year (e.g. 2027 or 2025). Auto-detected if omitted.")
@click.option("--deploy/--no-deploy", default=False, help="Deploy scripts from max_scripts/ before launch.")
@click.option("--launch/--no-launch", default=True, help="Launch 3ds Max if 3dsmax.exe is not already running.")
@click.option("--interactive", "-i", is_flag=True, help="Interactively choose which installed 3ds Max version to launch.")
@click.option("--timeout", "-t", type=float, default=60.0)
@click.option("--dry-run", is_flag=True)
@click.option(
    "--profile",
    type=click.Choice(["client", "vm-test"], case_sensitive=False),
    default="vm-test",
    help="Execution profile: 'vm-test' auto-dismisses licensing; 'client' stops with exit code 10 on licensing.",
)
@click.option("--max-timeout", type=float, default=None, help="Hard upper-bound timeout in seconds even if 3dsmax.exe is still running.")
@click.option("--dump-dir", type=click.Path(path_type=Path), default=None, help="Directory for popup dumps and licensing screenshots.")
@click.option("--json-result", type=click.Path(path_type=Path), default=None, help="Optional path to write structured WatchResult JSON.")
@click.option("--de-elevate/--no-de-elevate", default=None, help="Launch 3ds Max at Medium integrity (defaults to True in client profile, False in vm-test).")
def run(
    config: Path,
    ahk_exe: Path | None,
    max_exe: Path | None,
    version: str | None,
    deploy: bool,
    launch: bool,
    interactive: bool,
    timeout: float,
    dry_run: bool,
    profile: str,
    max_timeout: float | None,
    dump_dir: Path | None,
    json_result: Path | None,
    de_elevate: bool | None,
) -> None:
    """Launch 3ds Max (2024-2028) if not running, then watch and handle startup popups."""
    normalized_profile = profile.lower()
    effective_de_elevate = (normalized_profile == "client") if de_elevate is None else de_elevate
    already_running = is_process_running(("3dsmax",))
    resolved_version = version
    resolved_exe = max_exe

    if interactive and launch and not already_running and resolved_exe is None and not resolved_version:
        installed = find_all_installed_3dsmax()
        if not installed:
            click.echo("[MAX-AUTO] Не знайдено встановлених версій 3ds Max (2024–2028). Вмикаю режим очікування (watch)...")
            launch = False
        elif len(installed) == 1:
            ver, exe = installed[0]
            if click.confirm(f"Запустити Autodesk 3ds Max {ver} зараз?", default=True):
                resolved_version, resolved_exe = ver, exe
            else:
                click.echo("[MAX-AUTO] Запуск скасовано. Вмикаю режим очікування (watch)...")
                launch = False
        else:
            click.echo("[MAX-AUTO] Встановлені версії 3ds Max:")
            for idx, (ver, exe) in enumerate(installed, start=1):
                click.echo(f"  [{idx}] Autodesk 3ds Max {ver} ({exe})")
            click.echo("  [W] Лише очікувати (watch, без запуску)")
            while True:
                choice = click.prompt("Оберіть варіант", default="1").strip().lower()
                if choice in ("w", "watch"):
                    click.echo("[MAX-AUTO] Вмикаю режим очікування (watch)...")
                    launch = False
                    break
                if choice.isdigit() and 1 <= int(choice) <= len(installed):
                    resolved_version, resolved_exe = installed[int(choice) - 1]
                    break
                click.echo(f"Введіть число від 1 до {len(installed)} або W.")

    if resolved_exe is None:
        if resolved_version:
            resolved_exe = get_installed_3dsmax_exe(resolved_version) or get_default_3dsmax_exe(resolved_version)
        else:
            detected = find_installed_3dsmax_exe()
            if detected:
                resolved_version, resolved_exe = detected

    if deploy:
        scripts_dir = Path(__file__).resolve().parent.parent / "max_scripts"
        if scripts_dir.exists():
            deployed = deploy_scripts(
                scripts_dir,
                version=resolved_version or "2025",
                startup=True,
                dry_run=dry_run,
            )
            for p in deployed:
                click.echo(f"[DEPLOY] -> {p}")

    if launch:
        if already_running:
            click.echo("[RUN] 3dsmax.exe is already running; attaching watcher...")
        else:
            if resolved_exe is None:
                raise click.ClickException(
                    "No installed 3ds Max (2024-2028) found. Pass --max-exe or --no-launch."
                )
            click.echo(f"[RUN] Launching 3ds Max: {resolved_exe}")
            try:
                launch_3dsmax(
                    max_exe=resolved_exe,
                    dry_run=dry_run,
                    de_elevate=effective_de_elevate,
                )
            except FileNotFoundError as exc:
                raise click.ClickException(str(exc)) from exc

    def _write_json_result_atomic(path: Path, payload: dict[str, object]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        try:
            tmp_path.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, path)
        except BaseException:
            tmp_path.unlink(missing_ok=True)
            raise

    cfg = WatcherConfig.from_yaml(config)
    ahk = AHKBridge(ahk_exe=ahk_exe, dry_run=dry_run)
    try:
        result = watch_loop(
            config=cfg,
            ahk=ahk,
            poll_interval=1.0,
            timeout_sec=timeout,
            learn=True,
            dump_dir=dump_dir or DEFAULT_DUMP_DIR,
            stop_on_main_ready=True,
            profile=normalized_profile,
            max_timeout_sec=max_timeout,
            fail_if_process_exits=(normalized_profile == "client" and launch),
        )
    except Exception as exc:
        if json_result is not None:
            _write_json_result_atomic(
                json_result,
                {
                    "status": "failed",
                    "exit_code": 1,
                    "detail": str(exc),
                    "matched_rules": [],
                    "screenshots": [],
                },
            )
        raise click.ClickException(str(exc)) from exc
    if json_result is not None:
        _write_json_result_atomic(json_result, result.to_dict())
    if result.exit_code != 0:
        sys.exit(result.exit_code)


@cli.command()
@click.option("--rule", "-r", multiple=True, help="Rule name (or substring) from rules.yaml.")
@click.option("--process", "-p", multiple=True, help="Filter by process name substring.")
@click.option("--title", default=None, help="Regex to match window title.")
@click.option("--text", multiple=True, help="Text substring(s) expected in the window.")
@click.option("--act/--no-act", default=False, help="Execute the matched rule's action once found.")
@click.option("--click", "click_control", default=None, help="Control name to click when found.")
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), default=DEFAULT_CONFIG)
@click.option("--ahk-exe", type=click.Path(path_type=Path), default=None)
@click.option("--timeout", "-t", type=float, default=60.0, help="Wait timeout in seconds (0 = infinite).")
@click.option("--interval", type=float, default=0.5, help="Polling interval in seconds.")
@click.option("--dry-run", is_flag=True, help="Log matched action without clicking.")
def wait(
    rule: tuple[str, ...],
    process: tuple[str, ...],
    title: str | None,
    text: tuple[str, ...],
    act: bool,
    click_control: str | None,
    config: Path,
    ahk_exe: Path | None,
    timeout: float,
    interval: float,
    dry_run: bool,
) -> None:
    """Wait for a specific window/rule to appear (optionally act on it) and exit."""
    if not rule and not title and not text and not process:
        raise click.UsageError("Specify at least one of --rule, --title, --text, or --process.")

    cfg = WatcherConfig.from_yaml(config)
    ahk = AHKBridge(ahk_exe=ahk_exe, dry_run=dry_run)
    try:
        found = wait_for_window(
            config=cfg,
            ahk=ahk,
            rule_names=list(rule) if rule else None,
            process_filters=list(process) if process else None,
            title_regex=title,
            text_contains=list(text) if text else None,
            act=act,
            click_control=click_control,
            poll_interval=interval,
            timeout_sec=timeout,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc

    if not found:
        sys.exit(1)


if __name__ == "__main__":
    cli()
