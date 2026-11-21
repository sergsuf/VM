"""Client agent, local status UI, and CLI entrypoint."""

from __future__ import annotations

from typing import Any

from remote.client.agent import ClientAgent
from remote.client.status_ui import ClientStatusServer, launch_status_ui_window

__all__ = [
    "ClientAgent",
    "ClientStatusServer",
    "launch_status_ui_window",
    "build_arg_parser",
    "main",
]


def __getattr__(name: str) -> Any:
    if name in ("build_arg_parser", "main"):
        from remote.client import main as _main_mod

        return getattr(_main_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
