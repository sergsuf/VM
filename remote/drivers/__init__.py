"""Cross-platform driver contracts and simulated implementations."""

from __future__ import annotations

from remote.drivers.base import (
    DriverBundle,
    FirstRunDriver,
    InstallerDriver,
    PostConfigDriver,
    SystemDriver,
)
from remote.drivers.simulated import (
    SimulatedFirstRunDriver,
    SimulatedInstallerDriver,
    SimulatedPostConfigDriver,
    SimulatedSystemDriver,
    create_simulated_bundle,
)

__all__ = [
    "DriverBundle",
    "FirstRunDriver",
    "InstallerDriver",
    "PostConfigDriver",
    "SystemDriver",
    "SimulatedFirstRunDriver",
    "SimulatedInstallerDriver",
    "SimulatedPostConfigDriver",
    "SimulatedSystemDriver",
    "create_simulated_bundle",
]
