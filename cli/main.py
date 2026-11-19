"""CLI entrypoint for 3ds Max 2025 VM automation."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from cli.ahk_bridge import AHKBridge
from cli.max_deployer import DEFAULT_3DSMAX_EXE, deploy_scripts, launch_3dsmax
from cli.ui_inspector import scan_windows
from cli.watcher import WatcherConfig, wait_for_window, watch_loop

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"


@click.group(invoke_without_command=True)
@click.pass_context
def cli(ctx: click.Context) -> None:
    """3ds Max 2025 VM Automation CLI (UIA Inspector + AutoHotkey v2)."""
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
        click.echo(f"\n=== [{snap.process_name} | PID {snap.pid} | HWND {snap.hwnd}] '{snap.title}' ===")
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
@click.option("--timeout", "-t", type=float, default=20.0)
@click.option("--dry-run", is_flag=True)
def run(
    config: Path,
    ahk_exe: Path | None,
    timeout: float,
    dry_run: bool,
) -> None:
    """Watch an already-started 3ds Max (2025/2027) instance and handle popups (no deploy/launch)."""
    cfg = WatcherConfig.from_yaml(config)
    ahk = AHKBridge(ahk_exe=ahk_exe, dry_run=dry_run)
    watch_loop(
        config=cfg,
        ahk=ahk,
        poll_interval=1.0,
        timeout_sec=timeout,
        learn=True,
        stop_on_main_ready=True,
    )


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
