"""CLI entrypoint for 3ds Max 2025 VM automation."""

from __future__ import annotations

import json
from pathlib import Path

import click

from cli.ahk_bridge import AHKBridge
from cli.max_deployer import DEFAULT_3DSMAX_EXE, deploy_scripts, launch_3dsmax
from cli.ui_inspector import scan_windows
from cli.watcher import WatcherConfig, watch_loop

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config" / "rules.yaml"


@click.group()
def cli() -> None:
    """3ds Max 2025 VM Automation CLI (UIA Inspector + AutoHotkey v2)."""


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
@click.option("--timeout", "-t", type=float, default=300.0, help="Watcher timeout in seconds (0 = infinite).")
@click.option("--interval", type=float, default=1.0, help="Polling interval in seconds.")
@click.option("--learn/--no-learn", default=True, help="Save UI dumps & screenshots of unknown popups.")
@click.option("--dry-run", is_flag=True, help="Log matched rules without clicking.")
@click.option("--stop-on-ready", is_flag=True, help="Stop watcher once main 3ds Max window is visible.")
def watch(
    config: Path,
    ahk_exe: Path | None,
    timeout: float,
    interval: float,
    learn: bool,
    dry_run: bool,
    stop_on_ready: bool,
) -> None:
    """Watch for 3ds Max / Autodesk popups and handle them via AHK v2."""
    cfg = WatcherConfig.from_yaml(config)
    ahk = AHKBridge(ahk_exe=ahk_exe, dry_run=dry_run)
    watch_loop(
        config=cfg,
        ahk=ahk,
        poll_interval=interval,
        timeout_sec=timeout,
        learn=learn,
        stop_on_main_ready=stop_on_ready,
    )


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
@click.option("--version", default="2025", show_default=True, help="3ds Max version year (e.g. 2025 or 2027).")
@click.option("--max-exe", type=click.Path(path_type=Path), default=None, help="Optional override path to 3dsmax.exe.")
@click.option("--scripts-dir", type=click.Path(path_type=Path), default=Path("max_scripts"))
@click.option("--config", "-c", type=click.Path(exists=True, path_type=Path), default=DEFAULT_CONFIG)
@click.option("--ahk-exe", type=click.Path(path_type=Path), default=None)
@click.option("--timeout", "-t", type=float, default=300.0)
@click.option("--dry-run", is_flag=True)
def run(
    version: str,
    max_exe: Path | None,
    scripts_dir: Path,
    config: Path,
    ahk_exe: Path | None,
    timeout: float,
    dry_run: bool,
) -> None:
    """Full flow: deploy scripts -> launch 3ds Max -> watch & handle popups."""
    if max_exe is None:
        max_exe = Path(rf"C:\Program Files\Autodesk\3ds Max {version}\3dsmax.exe")

    if scripts_dir.exists():
        deployed = deploy_scripts(scripts_dir, version=version, startup=True, dry_run=dry_run)
        click.echo(f"[DEPLOY] {len(deployed)} script item(s) deployed.")

    click.echo(f"[LAUNCH] Starting {max_exe}...")
    launch_3dsmax(max_exe=max_exe, dry_run=dry_run)

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


if __name__ == "__main__":
    cli()
