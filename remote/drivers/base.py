"""Platform driver Protocols and bundle container for remote 3ds Max deployment."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from remote.protocol import PostConfigStepState, SystemStatus


@runtime_checkable
class SystemDriver(Protocol):
    """OS-level inspection, UAC elevation, Smart App Control, Defender, and screenshots."""

    def collect_status(
        self, agent_version: str, runtime_version: str
    ) -> SystemStatus: ...

    def request_elevation(
        self, executable: str, args: list[str], cwd: str
    ) -> dict[str, Any]: ...

    def get_smart_app_control_status(self) -> dict[str, Any]: ...

    def disable_smart_app_control(
        self, mode: str = "guided"
    ) -> dict[str, Any]: ...

    def get_defender_status(self) -> dict[str, Any]: ...

    def configure_defender(
        self, action: str, exclusion_paths: list[str] | None = None
    ) -> dict[str, Any]: ...

    def capture_screenshot(self, dest_path: Path) -> Path | None: ...


@runtime_checkable
class InstallerDriver(Protocol):
    """3ds Max ISO silent installation and installed-state probing."""

    def install_3dsmax(
        self,
        version: str,
        iso_path: Path,
        *,
        progress_cb: Callable[[int, str], None],
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        timeout_sec: int = 2400,
    ) -> dict[str, Any]: ...

    def is_max_installed(self, version: str) -> bool: ...

    def is_installer_running(self) -> bool: ...


@runtime_checkable
class FirstRunDriver(Protocol):
    """Automated 3ds Max first launch and startup popup dismissal (`max-auto`)."""

    def run_first_run(
        self,
        version: str,
        *,
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        timeout_sec: int = 300,
        dump_dir: Path | None = None,
    ) -> dict[str, Any]: ...


@runtime_checkable
class PostConfigDriver(Protocol):
    """1-click post-configuration (renderers, themes, quad-menus, hotkeys, scripts, verify)."""

    def load_steps(
        self, config_path: Path | None = None, version: str | None = None
    ) -> list[PostConfigStepState]: ...

    def run_step(
        self,
        step_id: str,
        version: str,
        *,
        assets_dir: Path | None,
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        config_path: Path | None = None,
    ) -> dict[str, Any]: ...

    def verify_installation(self, version: str) -> dict[str, Any]: ...


@dataclass
class DriverBundle:
    """Grouped platform drivers injected into the Client Agent."""

    system: SystemDriver
    installer: InstallerDriver
    first_run: FirstRunDriver
    post_config: PostConfigDriver


__all__ = [
    "SystemDriver",
    "InstallerDriver",
    "FirstRunDriver",
    "PostConfigDriver",
    "DriverBundle",
]
