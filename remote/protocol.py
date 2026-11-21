"""Shared wire protocol models, enums, and strict serialization for remote deployment."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, TypeVar

E = TypeVar("E", bound=Enum)


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format ending with 'Z'."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class Stage(str, Enum):
    """7-stage remote 3ds Max deployment pipeline."""

    PREP = "PREP"
    DOWNLOAD_ISO = "DOWNLOAD_ISO"
    INSTALL_MAX = "INSTALL_MAX"
    WAIT_LICENSE = "WAIT_LICENSE"
    FIRST_RUN = "FIRST_RUN"
    POST_CONFIG = "POST_CONFIG"
    DONE = "DONE"

    def __str__(self) -> str:
        return self.value


STAGE_ORDER: tuple[Stage, ...] = (
    Stage.PREP,
    Stage.DOWNLOAD_ISO,
    Stage.INSTALL_MAX,
    Stage.WAIT_LICENSE,
    Stage.FIRST_RUN,
    Stage.POST_CONFIG,
    Stage.DONE,
)


class StageStatus(str, Enum):
    """Orthogonal execution status for the current deployment stage."""

    IDLE = "idle"
    RUNNING = "running"
    WAITING_OPERATOR = "waiting_operator"
    PAUSED = "paused"
    FAILED = "failed"
    ABORTED = "aborted"
    COMPLETED = "completed"

    def __str__(self) -> str:
        return self.value


class CommandType(str, Enum):
    """Commands issued by the Operator to the Client Agent."""

    REFRESH_STATUS = "refresh_status"
    REQUEST_ELEVATION = "request_elevation"
    SMART_APP_CONTROL = "smart_app_control"
    DEFENDER_CONTROL = "defender_control"
    START_INSTALL = "start_install"
    CONFIRM_LICENSE = "confirm_license"
    TRIGGER_FIRST_RUN = "trigger_first_run"
    TRIGGER_POST_CONFIG = "trigger_post_config"
    SKIP_STAGE = "skip_stage"
    PAUSE = "pause"
    RESUME = "resume"
    RETRY = "retry"
    ABORT = "abort"
    RESET = "reset"
    PUSH_UPDATE = "push_update"
    CAPTURE_SCREENSHOT = "capture_screenshot"

    def __str__(self) -> str:
        return self.value


VALID_SAC_STATES: frozenset[str] = frozenset({"off", "enforced", "evaluation", "unknown"})
VALID_TRANSFER_PHASES: frozenset[str] = frozenset(
    {"checking", "downloading", "verifying", "skipped", "done", "failed"}
)
VALID_POST_CONFIG_CATEGORIES: frozenset[str] = frozenset(
    {"renderer", "theme", "quad_menu", "hotkey", "scripts", "verify"}
)
VALID_POST_CONFIG_STATUSES: frozenset[str] = frozenset(
    {"pending", "running", "done", "skipped", "failed"}
)
VALID_ACK_STATUSES: frozenset[str] = frozenset(
    {"accepted", "completed", "rejected", "failed"}
)
VALID_LOG_LEVELS: frozenset[str] = frozenset({"DEBUG", "INFO", "WARN", "ERROR"})


def _require_mapping(data: Any, cls_name: str) -> Mapping[str, Any]:
    if not isinstance(data, Mapping):
        raise ValueError(f"{cls_name}.from_dict expects a mapping, got {type(data).__name__}")
    return data


def _require_field(data: Mapping[str, Any], key: str, cls_name: str) -> Any:
    if key not in data:
        raise ValueError(f"{cls_name} missing required field: {key!r}")
    return data[key]


def _validate_str(val: Any, field_name: str, *, non_empty: bool = False) -> str:
    if not isinstance(val, str):
        raise ValueError(f"Field {field_name!r} must be a str, got {type(val).__name__}")
    if non_empty and not val.strip():
        raise ValueError(f"Field {field_name!r} must be a non-empty string")
    return val


def _validate_optional_str(
    val: Any, field_name: str, *, non_empty: bool = False
) -> str | None:
    if val is None:
        return None
    return _validate_str(val, field_name, non_empty=non_empty)


def _validate_bool(val: Any, field_name: str) -> bool:
    if not isinstance(val, bool):
        raise ValueError(f"Field {field_name!r} must be a bool, got {type(val).__name__}")
    return val


def _validate_optional_bool(val: Any, field_name: str) -> bool | None:
    if val is None:
        return None
    return _validate_bool(val, field_name)


def _validate_int(
    val: Any,
    field_name: str,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise ValueError(f"Field {field_name!r} must be an int, got {type(val).__name__}")
    if min_value is not None and val < min_value:
        raise ValueError(f"Field {field_name!r} must be >= {min_value}, got {val}")
    if max_value is not None and val > max_value:
        raise ValueError(f"Field {field_name!r} must be <= {max_value}, got {val}")
    return val


def _validate_float(
    val: Any,
    field_name: str,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ValueError(f"Field {field_name!r} must be a number, got {type(val).__name__}")
    fval = float(val)
    if not math.isfinite(fval):
        raise ValueError(f"Field {field_name!r} must be finite, got {val!r}")
    if min_value is not None and fval < min_value:
        raise ValueError(f"Field {field_name!r} must be >= {min_value}, got {fval}")
    if max_value is not None and fval > max_value:
        raise ValueError(f"Field {field_name!r} must be <= {max_value}, got {fval}")
    return fval


def _validate_optional_float(
    val: Any,
    field_name: str,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float | None:
    if val is None:
        return None
    return _validate_float(val, field_name, min_value=min_value, max_value=max_value)


def _validate_choice(val: Any, allowed: frozenset[str], field_name: str) -> str:
    if not isinstance(val, str) or val not in allowed:
        raise ValueError(
            f"Field {field_name!r} must be one of {sorted(allowed)}, got {val!r}"
        )
    return val


def _validate_enum(val: Any, enum_cls: type[E], field_name: str) -> E:
    if isinstance(val, enum_cls):
        return val
    if isinstance(val, str):
        try:
            return enum_cls(val)
        except ValueError as exc:
            raise ValueError(
                f"Invalid value for {field_name!r}: {val!r}"
            ) from exc
    raise ValueError(
        f"Field {field_name!r} must be {enum_cls.__name__} or str, got {type(val).__name__}"
    )


def _validate_str_list(val: Any, field_name: str) -> list[str]:
    if not isinstance(val, list):
        raise ValueError(f"Field {field_name!r} must be a list, got {type(val).__name__}")
    result: list[str] = []
    for idx, item in enumerate(val):
        if not isinstance(item, str):
            raise ValueError(
                f"Field {field_name!r}[{idx}] must be a str, got {type(item).__name__}"
            )
        result.append(item)
    return result


def _validate_str_dict(val: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(val, dict):
        raise ValueError(f"Field {field_name!r} must be a dict, got {type(val).__name__}")
    for key in val:
        if not isinstance(key, str):
            raise ValueError(
                f"Field {field_name!r} keys must be str, got {type(key).__name__}"
            )
    return copy.deepcopy(val)


@dataclass
class SystemStatus:
    """Snapshot of client OS security, privilege, disk, and 3ds Max environment state."""

    hostname: str
    os_version: str
    is_admin: bool
    session_id: int
    smart_app_control_state: str = "unknown"
    smart_app_control_reboot_required: bool = False
    defender_rtp_enabled: bool | None = None
    defender_tamper_protected: bool | None = None
    defender_exclusions: list[str] = field(default_factory=list)
    third_party_av: list[str] = field(default_factory=list)
    free_disk_bytes: int = 0
    installed_max_versions: list[str] = field(default_factory=list)
    ahk_available: bool = False
    agent_version: str = ""
    runtime_version: str = ""

    def __post_init__(self) -> None:
        self.hostname = _validate_str(self.hostname, "hostname", non_empty=True)
        self.os_version = _validate_str(self.os_version, "os_version")
        self.is_admin = _validate_bool(self.is_admin, "is_admin")
        self.session_id = _validate_int(self.session_id, "session_id", min_value=0)
        self.smart_app_control_state = _validate_choice(
            self.smart_app_control_state, VALID_SAC_STATES, "smart_app_control_state"
        )
        self.smart_app_control_reboot_required = _validate_bool(
            self.smart_app_control_reboot_required,
            "smart_app_control_reboot_required",
        )
        self.defender_rtp_enabled = _validate_optional_bool(
            self.defender_rtp_enabled, "defender_rtp_enabled"
        )
        self.defender_tamper_protected = _validate_optional_bool(
            self.defender_tamper_protected, "defender_tamper_protected"
        )
        self.defender_exclusions = _validate_str_list(
            self.defender_exclusions, "defender_exclusions"
        )
        self.third_party_av = _validate_str_list(self.third_party_av, "third_party_av")
        self.free_disk_bytes = _validate_int(
            self.free_disk_bytes, "free_disk_bytes", min_value=0
        )
        self.installed_max_versions = _validate_str_list(
            self.installed_max_versions, "installed_max_versions"
        )
        self.ahk_available = _validate_bool(self.ahk_available, "ahk_available")
        self.agent_version = _validate_str(self.agent_version, "agent_version")
        self.runtime_version = _validate_str(self.runtime_version, "runtime_version")

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostname": self.hostname,
            "os_version": self.os_version,
            "is_admin": self.is_admin,
            "session_id": self.session_id,
            "smart_app_control_state": self.smart_app_control_state,
            "smart_app_control_reboot_required": self.smart_app_control_reboot_required,
            "defender_rtp_enabled": self.defender_rtp_enabled,
            "defender_tamper_protected": self.defender_tamper_protected,
            "defender_exclusions": list(self.defender_exclusions),
            "third_party_av": list(self.third_party_av),
            "free_disk_bytes": self.free_disk_bytes,
            "installed_max_versions": list(self.installed_max_versions),
            "ahk_available": self.ahk_available,
            "agent_version": self.agent_version,
            "runtime_version": self.runtime_version,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SystemStatus:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            hostname=_require_field(mapping, "hostname", cls.__name__),
            os_version=_require_field(mapping, "os_version", cls.__name__),
            is_admin=_require_field(mapping, "is_admin", cls.__name__),
            session_id=_require_field(mapping, "session_id", cls.__name__),
            smart_app_control_state=mapping.get("smart_app_control_state", "unknown"),
            smart_app_control_reboot_required=mapping.get(
                "smart_app_control_reboot_required", False
            ),
            defender_rtp_enabled=mapping.get("defender_rtp_enabled", None),
            defender_tamper_protected=mapping.get("defender_tamper_protected", None),
            defender_exclusions=mapping.get("defender_exclusions", []),
            third_party_av=mapping.get("third_party_av", []),
            free_disk_bytes=mapping.get("free_disk_bytes", 0),
            installed_max_versions=mapping.get("installed_max_versions", []),
            ahk_available=mapping.get("ahk_available", False),
            agent_version=mapping.get("agent_version", ""),
            runtime_version=mapping.get("runtime_version", ""),
        )


@dataclass
class IsoSpec:
    """Metadata describing an Autodesk 3ds Max ISO image in the operator catalog."""

    file_id: str
    filename: str
    version: str
    size_bytes: int
    sha256: str
    download_url: str | None = None

    def __post_init__(self) -> None:
        self.file_id = _validate_str(self.file_id, "file_id", non_empty=True)
        self.filename = _validate_str(self.filename, "filename", non_empty=True)
        self.version = _validate_str(self.version, "version", non_empty=True)
        self.size_bytes = _validate_int(self.size_bytes, "size_bytes", min_value=0)
        self.sha256 = _validate_str(self.sha256, "sha256")
        self.download_url = _validate_optional_str(
            self.download_url, "download_url", non_empty=True
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_id": self.file_id,
            "filename": self.filename,
            "version": self.version,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "download_url": self.download_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> IsoSpec:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            file_id=_require_field(mapping, "file_id", cls.__name__),
            filename=_require_field(mapping, "filename", cls.__name__),
            version=_require_field(mapping, "version", cls.__name__),
            size_bytes=_require_field(mapping, "size_bytes", cls.__name__),
            sha256=_require_field(mapping, "sha256", cls.__name__),
            download_url=mapping.get("download_url", None),
        )


@dataclass
class TransferProgress:
    """Real-time telemetry for ISO pre-check, chunked download, and SHA-256 verification."""

    phase: str
    bytes_done: int = 0
    bytes_total: int = 0
    percent: float = 0.0
    speed_bps: float = 0.0
    eta_seconds: float | None = None
    local_path: str | None = None

    def __post_init__(self) -> None:
        self.phase = _validate_choice(self.phase, VALID_TRANSFER_PHASES, "phase")
        self.bytes_done = _validate_int(self.bytes_done, "bytes_done", min_value=0)
        self.bytes_total = _validate_int(self.bytes_total, "bytes_total", min_value=0)
        self.percent = _validate_float(
            self.percent, "percent", min_value=0.0, max_value=100.0
        )
        self.speed_bps = _validate_float(self.speed_bps, "speed_bps", min_value=0.0)
        self.eta_seconds = _validate_optional_float(
            self.eta_seconds, "eta_seconds", min_value=0.0
        )
        self.local_path = _validate_optional_str(self.local_path, "local_path")

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "bytes_done": self.bytes_done,
            "bytes_total": self.bytes_total,
            "percent": self.percent,
            "speed_bps": self.speed_bps,
            "eta_seconds": self.eta_seconds,
            "local_path": self.local_path,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TransferProgress:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            phase=_require_field(mapping, "phase", cls.__name__),
            bytes_done=mapping.get("bytes_done", 0),
            bytes_total=mapping.get("bytes_total", 0),
            percent=mapping.get("percent", 0.0),
            speed_bps=mapping.get("speed_bps", 0.0),
            eta_seconds=mapping.get("eta_seconds", None),
            local_path=mapping.get("local_path", None),
        )


@dataclass
class PostConfigStepState:
    """Execution status of a single 1-click post-configuration step."""

    step_id: str
    name: str
    category: str
    status: str = "pending"
    detail: str = ""

    def __post_init__(self) -> None:
        self.step_id = _validate_str(self.step_id, "step_id", non_empty=True)
        self.name = _validate_str(self.name, "name", non_empty=True)
        self.category = _validate_choice(
            self.category, VALID_POST_CONFIG_CATEGORIES, "category"
        )
        self.status = _validate_choice(self.status, VALID_POST_CONFIG_STATUSES, "status")
        self.detail = _validate_str(self.detail, "detail")

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_id": self.step_id,
            "name": self.name,
            "category": self.category,
            "status": self.status,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PostConfigStepState:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            step_id=_require_field(mapping, "step_id", cls.__name__),
            name=_require_field(mapping, "name", cls.__name__),
            category=_require_field(mapping, "category", cls.__name__),
            status=mapping.get("status", "pending"),
            detail=mapping.get("detail", ""),
        )


@dataclass
class SessionState:
    """Persistent state of a remote 3ds Max deployment session."""

    session_id: str = ""
    client_id: str = ""
    stage: Stage = Stage.PREP
    status: StageStatus = StageStatus.IDLE
    target_version: str | None = None
    iso_spec: IsoSpec | None = None
    iso_local_path: str | None = None
    iso_skipped_download: bool = False
    transfer: TransferProgress | None = None
    install_percent: int = 0
    install_message: str = ""
    post_config_steps: list[PostConfigStepState] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    last_error: str | None = None
    retry_count: int = 0
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.session_id = _validate_str(self.session_id, "session_id")
        self.client_id = _validate_str(self.client_id, "client_id")
        self.stage = _validate_enum(self.stage, Stage, "stage")
        self.status = _validate_enum(self.status, StageStatus, "status")
        self.target_version = _validate_optional_str(
            self.target_version, "target_version"
        )

        if self.iso_spec is None:
            pass
        elif isinstance(self.iso_spec, IsoSpec):
            self.iso_spec = copy.deepcopy(self.iso_spec)
        elif isinstance(self.iso_spec, Mapping):
            self.iso_spec = IsoSpec.from_dict(dict(self.iso_spec))
        else:
            raise ValueError(
                f"Field 'iso_spec' must be IsoSpec, dict, or None, got {type(self.iso_spec).__name__}"
            )

        self.iso_local_path = _validate_optional_str(
            self.iso_local_path, "iso_local_path"
        )
        self.iso_skipped_download = _validate_bool(
            self.iso_skipped_download, "iso_skipped_download"
        )

        if self.transfer is None:
            pass
        elif isinstance(self.transfer, TransferProgress):
            self.transfer = copy.deepcopy(self.transfer)
        elif isinstance(self.transfer, Mapping):
            self.transfer = TransferProgress.from_dict(dict(self.transfer))
        else:
            raise ValueError(
                f"Field 'transfer' must be TransferProgress, dict, or None, got {type(self.transfer).__name__}"
            )

        self.install_percent = _validate_int(
            self.install_percent, "install_percent", min_value=0, max_value=100
        )
        self.install_message = _validate_str(self.install_message, "install_message")

        if not isinstance(self.post_config_steps, list):
            raise ValueError(
                f"Field 'post_config_steps' must be a list, got {type(self.post_config_steps).__name__}"
            )
        normalized_steps: list[PostConfigStepState] = []
        for idx, step in enumerate(self.post_config_steps):
            if isinstance(step, PostConfigStepState):
                normalized_steps.append(copy.deepcopy(step))
            elif isinstance(step, Mapping):
                normalized_steps.append(PostConfigStepState.from_dict(dict(step)))
            else:
                raise ValueError(
                    f"Field 'post_config_steps'[{idx}] must be PostConfigStepState or dict, "
                    f"got {type(step).__name__}"
                )
        self.post_config_steps = normalized_steps

        self.summary = _validate_str_dict(self.summary, "summary")
        self.last_error = _validate_optional_str(self.last_error, "last_error")
        self.retry_count = _validate_int(self.retry_count, "retry_count", min_value=0)
        self.updated_at = _validate_str(self.updated_at, "updated_at", non_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "client_id": self.client_id,
            "stage": self.stage.value,
            "status": self.status.value,
            "target_version": self.target_version,
            "iso_spec": self.iso_spec.to_dict() if self.iso_spec is not None else None,
            "iso_local_path": self.iso_local_path,
            "iso_skipped_download": self.iso_skipped_download,
            "transfer": self.transfer.to_dict() if self.transfer is not None else None,
            "install_percent": self.install_percent,
            "install_message": self.install_message,
            "post_config_steps": [step.to_dict() for step in self.post_config_steps],
            "summary": copy.deepcopy(self.summary),
            "last_error": self.last_error,
            "retry_count": self.retry_count,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SessionState:
        mapping = _require_mapping(data, cls.__name__)
        raw_stage = _require_field(mapping, "stage", cls.__name__)
        raw_status = _require_field(mapping, "status", cls.__name__)
        return cls(
            session_id=mapping.get("session_id", ""),
            client_id=mapping.get("client_id", ""),
            stage=raw_stage,
            status=raw_status,
            target_version=mapping.get("target_version", None),
            iso_spec=mapping.get("iso_spec", None),
            iso_local_path=mapping.get("iso_local_path", None),
            iso_skipped_download=mapping.get("iso_skipped_download", False),
            transfer=mapping.get("transfer", None),
            install_percent=mapping.get("install_percent", 0),
            install_message=mapping.get("install_message", ""),
            post_config_steps=mapping.get("post_config_steps", []),
            summary=mapping.get("summary", {}),
            last_error=mapping.get("last_error", None),
            retry_count=mapping.get("retry_count", 0),
            updated_at=mapping.get("updated_at", utc_now_iso()),
        )


@dataclass
class Command:
    """Operator-issued command delivered to the Client Agent."""

    cmd_id: str
    seq: int
    cmd_type: CommandType
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        self.cmd_id = _validate_str(self.cmd_id, "cmd_id", non_empty=True)
        self.seq = _validate_int(self.seq, "seq", min_value=0)
        self.cmd_type = _validate_enum(self.cmd_type, CommandType, "cmd_type")
        self.payload = _validate_str_dict(self.payload, "payload")
        self.created_at = _validate_str(self.created_at, "created_at", non_empty=True)

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmd_id": self.cmd_id,
            "seq": self.seq,
            "cmd_type": self.cmd_type.value,
            "payload": copy.deepcopy(self.payload),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Command:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            cmd_id=_require_field(mapping, "cmd_id", cls.__name__),
            seq=_require_field(mapping, "seq", cls.__name__),
            cmd_type=_require_field(mapping, "cmd_type", cls.__name__),
            payload=mapping.get("payload", {}),
            created_at=mapping.get("created_at", utc_now_iso()),
        )


@dataclass
class CommandAck:
    """Client Agent acknowledgment for a received or completed command."""

    cmd_id: str
    status: str
    detail: str = ""
    result: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cmd_id = _validate_str(self.cmd_id, "cmd_id", non_empty=True)
        self.status = _validate_choice(self.status, VALID_ACK_STATUSES, "status")
        self.detail = _validate_str(self.detail, "detail")
        self.result = _validate_str_dict(self.result, "result")

    def to_dict(self) -> dict[str, Any]:
        return {
            "cmd_id": self.cmd_id,
            "status": self.status,
            "detail": self.detail,
            "result": copy.deepcopy(self.result),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CommandAck:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            cmd_id=_require_field(mapping, "cmd_id", cls.__name__),
            status=_require_field(mapping, "status", cls.__name__),
            detail=mapping.get("detail", ""),
            result=mapping.get("result", {}),
        )


@dataclass
class LogEntry:
    """Structured log line streamed from the Client Agent to the Operator."""

    ts: str
    level: str
    stage: str
    msg: str

    def __post_init__(self) -> None:
        self.ts = _validate_str(self.ts, "ts", non_empty=True)
        self.level = _validate_choice(self.level, VALID_LOG_LEVELS, "level")
        if isinstance(self.stage, Stage):
            self.stage = self.stage.value
        self.stage = _validate_str(self.stage, "stage")
        self.msg = _validate_str(self.msg, "msg")

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "level": self.level,
            "stage": self.stage,
            "msg": self.msg,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LogEntry:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            ts=_require_field(mapping, "ts", cls.__name__),
            level=_require_field(mapping, "level", cls.__name__),
            stage=_require_field(mapping, "stage", cls.__name__),
            msg=_require_field(mapping, "msg", cls.__name__),
        )


@dataclass
class UpdateManifest:
    """Descriptor for an atomic app/ auto-update release bundle."""

    version: str
    min_runtime_version: str
    file_id: str
    filename: str
    size_bytes: int
    sha256: str
    notes: str = ""
    download_url: str | None = None

    def __post_init__(self) -> None:
        self.version = _validate_str(self.version, "version", non_empty=True)
        self.min_runtime_version = _validate_str(
            self.min_runtime_version, "min_runtime_version", non_empty=True
        )
        self.file_id = _validate_str(self.file_id, "file_id", non_empty=True)
        self.filename = _validate_str(self.filename, "filename", non_empty=True)
        self.size_bytes = _validate_int(self.size_bytes, "size_bytes", min_value=0)
        self.sha256 = _validate_str(self.sha256, "sha256", non_empty=True)
        self.notes = _validate_str(self.notes, "notes")
        self.download_url = _validate_optional_str(
            self.download_url, "download_url", non_empty=True
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "min_runtime_version": self.min_runtime_version,
            "file_id": self.file_id,
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "notes": self.notes,
            "download_url": self.download_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> UpdateManifest:
        mapping = _require_mapping(data, cls.__name__)
        return cls(
            version=_require_field(mapping, "version", cls.__name__),
            min_runtime_version=_require_field(
                mapping, "min_runtime_version", cls.__name__
            ),
            file_id=_require_field(mapping, "file_id", cls.__name__),
            filename=_require_field(mapping, "filename", cls.__name__),
            size_bytes=_require_field(mapping, "size_bytes", cls.__name__),
            sha256=_require_field(mapping, "sha256", cls.__name__),
            notes=mapping.get("notes", ""),
            download_url=mapping.get("download_url", None),
        )


__all__ = [
    "Stage",
    "STAGE_ORDER",
    "StageStatus",
    "CommandType",
    "VALID_SAC_STATES",
    "VALID_TRANSFER_PHASES",
    "VALID_POST_CONFIG_CATEGORIES",
    "VALID_POST_CONFIG_STATUSES",
    "VALID_ACK_STATUSES",
    "VALID_LOG_LEVELS",
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
]
