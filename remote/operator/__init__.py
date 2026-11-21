"""Operator Control Panel Server, Web UI, and CLI entrypoint."""

from __future__ import annotations

from typing import Any

from remote.operator.server import OperatorServer

__all__ = ["OperatorServer", "build_arg_parser", "main"]


def __getattr__(name: str) -> Any:
    if name in ("build_arg_parser", "main"):
        from remote.operator import main as _main_mod

        return getattr(_main_mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
