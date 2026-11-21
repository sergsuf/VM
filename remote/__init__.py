"""Pure-Python remote 3ds Max deployment package."""

from __future__ import annotations

from remote.auth import (
    PairingConfig,
    generate_session_id,
    generate_token,
    verify_token,
)
from remote.protocol import (
    Command,
    CommandAck,
    CommandType,
    IsoSpec,
    LogEntry,
    PostConfigStepState,
    SessionState,
    Stage,
    StageStatus,
    SystemStatus,
    TransferProgress,
    UpdateManifest,
    utc_now_iso,
)

__version__ = "0.2.0"
RUNTIME_VERSION = "1.0.0"

__all__ = [
    "__version__",
    "RUNTIME_VERSION",
    "Stage",
    "StageStatus",
    "CommandType",
    "SystemStatus",
    "IsoSpec",
    "TransferProgress",
    "PostConfigStepState",
    "SessionState",
    "Command",
    "CommandAck",
    "LogEntry",
    "UpdateManifest",
    "utc_now_iso",
    "PairingConfig",
    "generate_session_id",
    "generate_token",
    "verify_token",
]
