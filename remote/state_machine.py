"""Thread-safe 7-stage deployment state machine with atomic JSON persistence and recovery."""

from __future__ import annotations

import copy
import json
import logging
import math
import os
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from remote.protocol import (
    STAGE_ORDER,
    VALID_POST_CONFIG_STATUSES,
    IsoSpec,
    PostConfigStepState,
    SessionState,
    Stage,
    StageStatus,
    TransferProgress,
    utc_now_iso,
)

logger = logging.getLogger(__name__)


class InvalidTransitionError(ValueError):
    """Raised when a requested state machine transition or update is not allowed."""


STAGE_INDEX: dict[Stage, int] = {stage: idx for idx, stage in enumerate(STAGE_ORDER)}

WORK_STAGES: frozenset[Stage] = frozenset(
    {
        Stage.DOWNLOAD_ISO,
        Stage.INSTALL_MAX,
        Stage.FIRST_RUN,
        Stage.POST_CONFIG,
    }
)

SKIP_TARGET_STATUS: dict[Stage, StageStatus] = {
    Stage.WAIT_LICENSE: StageStatus.WAITING_OPERATOR,
    Stage.POST_CONFIG: StageStatus.WAITING_OPERATOR,
    Stage.DONE: StageStatus.COMPLETED,
}

RESET_TARGET_STATUS: dict[Stage, StageStatus] = {
    Stage.PREP: StageStatus.IDLE,
    Stage.WAIT_LICENSE: StageStatus.WAITING_OPERATOR,
    Stage.POST_CONFIG: StageStatus.WAITING_OPERATOR,
}

VALID_POST_CONFIG_STEP_STATUSES: frozenset[str] = VALID_POST_CONFIG_STATUSES

_UPDATE_SAFE_PAIRS: frozenset[tuple[Stage, StageStatus]] = frozenset(
    {
        (Stage.PREP, StageStatus.IDLE),
        (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
        (Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR),
        (Stage.DONE, StageStatus.COMPLETED),
    }
)

_UPDATE_SAFE_STATUSES: frozenset[StageStatus] = frozenset(
    {
        StageStatus.PAUSED,
        StageStatus.FAILED,
        StageStatus.ABORTED,
    }
)


def _coerce_stage(value: Stage | str) -> Stage:
    if isinstance(value, Stage):
        return value
    if isinstance(value, str):
        try:
            return Stage(value)
        except ValueError as exc:
            raise InvalidTransitionError(f"Unknown stage: {value!r}") from exc
    raise InvalidTransitionError(f"Unknown stage: {value!r}")


def _make_initial_state(session_id: str = "", client_id: str = "") -> SessionState:
    return SessionState(
        session_id=str(session_id),
        client_id=str(client_id),
        stage=Stage.PREP,
        status=StageStatus.IDLE,
        target_version=None,
        iso_spec=None,
        iso_local_path=None,
        iso_skipped_download=False,
        transfer=None,
        install_percent=0,
        install_message="",
        post_config_steps=[],
        summary={},
        last_error=None,
        retry_count=0,
        updated_at=utc_now_iso(),
    )


class DeploymentStateMachine:
    """Persistent, thread-safe 7-stage state machine for remote 3ds Max deployment."""

    def __init__(
        self,
        state_path: Path | None = None,
        session_id: str = "",
        client_id: str = "",
        auto_resume_download: bool = False,
    ) -> None:
        self._lock = threading.RLock()
        self._state_path: Path | None = (
            Path(state_path) if state_path is not None else None
        )
        self._auto_resume_download: bool = bool(auto_resume_download)
        self._state: SessionState = _make_initial_state(
            session_id=session_id,
            client_id=client_id,
        )

    @property
    def state_path(self) -> Path | None:
        """Return configured state file path, if persistence is enabled."""
        return self._state_path

    @property
    def state(self) -> SessionState:
        """Return an isolated deep copy of the current SessionState."""
        with self._lock:
            return copy.deepcopy(self._state)

    def _persist_state(self, state: SessionState) -> None:
        assert self._state_path is not None
        parent = self._state_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(state.to_dict(), indent=2, ensure_ascii=False) + "\n"
        tmp_name = (
            f".{self._state_path.name}.{os.getpid()}."
            f"{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
        )
        tmp_path = parent / tmp_name
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self._state_path)
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def _commit(self, mutator: Callable[[SessionState], None]) -> SessionState:
        with self._lock:
            candidate = copy.deepcopy(self._state)
            mutator(candidate)
            candidate.updated_at = utc_now_iso()
            if self._state_path is not None:
                self._persist_state(candidate)
            self._state = candidate
            return copy.deepcopy(self._state)

    def _require(
        self,
        candidate: SessionState,
        stage: Stage,
        status: StageStatus,
        action: str,
    ) -> None:
        if candidate.stage != stage or candidate.status != status:
            raise InvalidTransitionError(
                f"Cannot {action} in ({candidate.stage.value}, {candidate.status.value}); "
                f"expected ({stage.value}, {status.value})"
            )

    def start_install(
        self, version: str, iso_spec: IsoSpec | None = None
    ) -> SessionState:
        if not isinstance(version, str) or not version.strip():
            raise InvalidTransitionError("Target version must be a non-empty string")
        if iso_spec is not None and not isinstance(iso_spec, IsoSpec):
            raise InvalidTransitionError(
                f"iso_spec must be an IsoSpec or None, got {type(iso_spec).__name__}"
            )
        clean_version = version.strip()
        spec_copy = copy.deepcopy(iso_spec) if iso_spec is not None else None

        def _mutate(candidate: SessionState) -> None:
            self._require(candidate, Stage.PREP, StageStatus.IDLE, "start_install")
            candidate.stage = Stage.DOWNLOAD_ISO
            candidate.status = StageStatus.RUNNING
            candidate.target_version = clean_version
            candidate.iso_spec = spec_copy
            candidate.iso_local_path = None
            candidate.iso_skipped_download = False
            candidate.transfer = None
            candidate.install_percent = 0
            candidate.install_message = ""
            candidate.post_config_steps = []
            candidate.summary = {}
            candidate.last_error = None
            candidate.retry_count = 0

        return self._commit(_mutate)

    def update_transfer_progress(self, progress: TransferProgress) -> SessionState:
        if not isinstance(progress, TransferProgress):
            raise InvalidTransitionError(
                f"progress must be a TransferProgress, got {type(progress).__name__}"
            )
        progress_copy = copy.deepcopy(progress)

        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate,
                Stage.DOWNLOAD_ISO,
                StageStatus.RUNNING,
                "update_transfer_progress",
            )
            candidate.transfer = progress_copy

        return self._commit(_mutate)

    def complete_iso_download(
        self, local_path: str, skipped: bool = False
    ) -> SessionState:
        if not isinstance(local_path, str) or not local_path.strip():
            raise InvalidTransitionError("local_path must be a non-empty string")
        clean_path = local_path.strip()
        skipped_flag = bool(skipped)

        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate,
                Stage.DOWNLOAD_ISO,
                StageStatus.RUNNING,
                "complete_iso_download",
            )
            candidate.stage = Stage.INSTALL_MAX
            candidate.status = StageStatus.RUNNING
            candidate.iso_local_path = clean_path
            candidate.iso_skipped_download = skipped_flag
            if candidate.transfer is not None:
                candidate.transfer.phase = "skipped" if skipped_flag else "done"
                candidate.transfer.local_path = clean_path
            candidate.install_percent = 0
            candidate.install_message = ""
            candidate.last_error = None

        return self._commit(_mutate)

    def update_install_progress(self, percent: int, message: str) -> SessionState:
        if isinstance(percent, bool) or not isinstance(percent, (int, float)):
            raise InvalidTransitionError(
                f"percent must be an integer, got {type(percent).__name__}"
            )
        if not math.isfinite(percent):
            raise InvalidTransitionError("percent must be a finite number")
        clamped = max(0, min(100, int(percent)))
        clean_msg = str(message)

        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate,
                Stage.INSTALL_MAX,
                StageStatus.RUNNING,
                "update_install_progress",
            )
            candidate.install_percent = clamped
            candidate.install_message = clean_msg

        return self._commit(_mutate)

    def complete_install(self) -> SessionState:
        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate, Stage.INSTALL_MAX, StageStatus.RUNNING, "complete_install"
            )
            candidate.stage = Stage.WAIT_LICENSE
            candidate.status = StageStatus.WAITING_OPERATOR
            candidate.install_percent = 100
            if not candidate.install_message:
                candidate.install_message = "Installation completed"
            candidate.last_error = None

        return self._commit(_mutate)

    def confirm_license(self) -> SessionState:
        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate,
                Stage.WAIT_LICENSE,
                StageStatus.WAITING_OPERATOR,
                "confirm_license",
            )
            candidate.stage = Stage.FIRST_RUN
            candidate.status = StageStatus.RUNNING
            candidate.last_error = None

        return self._commit(_mutate)

    def return_to_wait_license(self, reason: str) -> SessionState:
        clean_reason = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else "Licensing required"
        )

        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate,
                Stage.FIRST_RUN,
                StageStatus.RUNNING,
                "return_to_wait_license",
            )
            candidate.stage = Stage.WAIT_LICENSE
            candidate.status = StageStatus.WAITING_OPERATOR
            candidate.last_error = clean_reason

        return self._commit(_mutate)

    def complete_first_run(self, auto_post_config: bool = False) -> SessionState:
        next_status = (
            StageStatus.RUNNING if auto_post_config else StageStatus.WAITING_OPERATOR
        )

        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate, Stage.FIRST_RUN, StageStatus.RUNNING, "complete_first_run"
            )
            candidate.stage = Stage.POST_CONFIG
            candidate.status = next_status
            candidate.last_error = None

        return self._commit(_mutate)

    def start_post_config(
        self, steps: list[PostConfigStepState] | None = None
    ) -> SessionState:
        validated_steps: list[PostConfigStepState] | None = None
        if steps is not None:
            if not isinstance(steps, list):
                raise InvalidTransitionError(
                    f"steps must be a list or None, got {type(steps).__name__}"
                )
            seen_ids: set[str] = set()
            validated_steps = []
            for step in steps:
                if not isinstance(step, PostConfigStepState):
                    raise InvalidTransitionError(
                        "Each step must be a PostConfigStepState instance"
                    )
                if not isinstance(step.step_id, str) or not step.step_id.strip():
                    raise InvalidTransitionError(
                        "PostConfigStepState.step_id must be a non-empty string"
                    )
                if step.step_id in seen_ids:
                    raise InvalidTransitionError(
                        f"Duplicate post-config step_id: {step.step_id!r}"
                    )
                if step.status not in VALID_POST_CONFIG_STEP_STATUSES:
                    raise InvalidTransitionError(
                        f"Invalid post-config step status: {step.status!r}"
                    )
                seen_ids.add(step.step_id)
                validated_steps.append(copy.deepcopy(step))

        def _mutate(candidate: SessionState) -> None:
            if candidate.stage != Stage.POST_CONFIG:
                raise InvalidTransitionError(
                    f"Cannot start_post_config in ({candidate.stage.value}, {candidate.status.value}); "
                    "expected stage POST_CONFIG"
                )
            if candidate.status == StageStatus.WAITING_OPERATOR:
                pass
            elif (
                candidate.status == StageStatus.RUNNING
                and len(candidate.post_config_steps) == 0
                and validated_steps is not None
            ):
                pass
            else:
                raise InvalidTransitionError(
                    f"Cannot start_post_config in ({candidate.stage.value}, {candidate.status.value})"
                )

            candidate.status = StageStatus.RUNNING
            if validated_steps is not None:
                candidate.post_config_steps = validated_steps
            candidate.last_error = None

        return self._commit(_mutate)

    def update_post_config_step(
        self, step_id: str, status: str, detail: str = ""
    ) -> SessionState:
        if not isinstance(status, str) or status not in VALID_POST_CONFIG_STEP_STATUSES:
            raise InvalidTransitionError(
                f"Invalid post-config step status: {status!r}; "
                f"expected one of {sorted(VALID_POST_CONFIG_STEP_STATUSES)}"
            )
        clean_detail = str(detail)

        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate,
                Stage.POST_CONFIG,
                StageStatus.RUNNING,
                "update_post_config_step",
            )
            for step in candidate.post_config_steps:
                if step.step_id == step_id:
                    step.status = status
                    step.detail = clean_detail
                    return
            raise InvalidTransitionError(f"Unknown post-config step_id: {step_id!r}")

        return self._commit(_mutate)

    def complete_post_config(
        self, summary: dict[str, Any] | None = None
    ) -> SessionState:
        if summary is not None and not isinstance(summary, dict):
            raise InvalidTransitionError(
                f"summary must be a dict or None, got {type(summary).__name__}"
            )
        summary_copy = copy.deepcopy(summary) if summary is not None else None

        def _mutate(candidate: SessionState) -> None:
            self._require(
                candidate,
                Stage.POST_CONFIG,
                StageStatus.RUNNING,
                "complete_post_config",
            )
            candidate.stage = Stage.DONE
            candidate.status = StageStatus.COMPLETED
            if summary_copy is not None:
                candidate.summary = summary_copy
            candidate.last_error = None

        return self._commit(_mutate)

    def pause(self, reason: str = "") -> SessionState:
        clean_reason = (
            reason.strip() if isinstance(reason, str) and reason.strip() else None
        )

        def _mutate(candidate: SessionState) -> None:
            if (
                candidate.stage not in WORK_STAGES
                or candidate.status != StageStatus.RUNNING
            ):
                raise InvalidTransitionError(
                    f"Cannot pause in ({candidate.stage.value}, {candidate.status.value})"
                )
            candidate.status = StageStatus.PAUSED
            candidate.last_error = clean_reason

        return self._commit(_mutate)

    def resume(self) -> SessionState:
        def _mutate(candidate: SessionState) -> None:
            if (
                candidate.stage not in WORK_STAGES
                or candidate.status != StageStatus.PAUSED
            ):
                raise InvalidTransitionError(
                    f"Cannot resume in ({candidate.stage.value}, {candidate.status.value})"
                )
            candidate.status = StageStatus.RUNNING
            candidate.last_error = None

        return self._commit(_mutate)

    def fail(self, error: str) -> SessionState:
        clean_error = (
            error.strip()
            if isinstance(error, str) and error.strip()
            else "Unknown error"
        )

        def _mutate(candidate: SessionState) -> None:
            if (
                candidate.stage not in WORK_STAGES
                or candidate.status != StageStatus.RUNNING
            ):
                raise InvalidTransitionError(
                    f"Cannot fail in ({candidate.stage.value}, {candidate.status.value})"
                )
            candidate.status = StageStatus.FAILED
            candidate.last_error = clean_error

        return self._commit(_mutate)

    def abort(self, reason: str = "") -> SessionState:
        clean_reason = (
            reason.strip()
            if isinstance(reason, str) and reason.strip()
            else "Aborted by operator"
        )

        def _mutate(candidate: SessionState) -> None:
            if candidate.stage not in WORK_STAGES or candidate.status not in (
                StageStatus.RUNNING,
                StageStatus.PAUSED,
            ):
                raise InvalidTransitionError(
                    f"Cannot abort in ({candidate.stage.value}, {candidate.status.value})"
                )
            candidate.status = StageStatus.ABORTED
            candidate.last_error = clean_reason

        return self._commit(_mutate)

    def retry(self) -> SessionState:
        def _mutate(candidate: SessionState) -> None:
            if candidate.stage not in WORK_STAGES or candidate.status not in (
                StageStatus.FAILED,
                StageStatus.ABORTED,
            ):
                raise InvalidTransitionError(
                    f"Cannot retry in ({candidate.stage.value}, {candidate.status.value})"
                )
            candidate.status = StageStatus.RUNNING
            candidate.last_error = None
            candidate.retry_count += 1
            if candidate.stage == Stage.INSTALL_MAX:
                candidate.install_percent = 0
                candidate.install_message = ""
            elif candidate.stage == Stage.POST_CONFIG:
                for step in candidate.post_config_steps:
                    if step.status in ("failed", "running"):
                        step.status = "pending"
                        step.detail = ""

        return self._commit(_mutate)

    def skip_stage(self, target_stage: Stage) -> SessionState:
        target = _coerce_stage(target_stage)
        if target not in SKIP_TARGET_STATUS:
            raise InvalidTransitionError(
                f"Cannot skip directly to stage {target.value}; "
                "allowed targets: WAIT_LICENSE, POST_CONFIG, DONE"
            )

        def _mutate(candidate: SessionState) -> None:
            if candidate.status == StageStatus.RUNNING:
                raise InvalidTransitionError(
                    "Cannot skip while a stage is running; pause or abort first"
                )
            if STAGE_INDEX[target] <= STAGE_INDEX[candidate.stage]:
                raise InvalidTransitionError(
                    f"Cannot skip from {candidate.stage.value} to {target.value}; "
                    "target must be strictly ahead"
                )
            candidate.stage = target
            candidate.status = SKIP_TARGET_STATUS[target]
            candidate.last_error = None

        return self._commit(_mutate)

    def reset(self, target_stage: Stage = Stage.PREP) -> SessionState:
        target = _coerce_stage(target_stage)
        if target not in RESET_TARGET_STATUS:
            raise InvalidTransitionError(
                f"Cannot reset to stage {target.value}; "
                "allowed targets: PREP, WAIT_LICENSE, POST_CONFIG"
            )

        def _mutate(candidate: SessionState) -> None:
            if candidate.status == StageStatus.RUNNING:
                raise InvalidTransitionError(
                    "Cannot reset while a stage is running; pause or abort first"
                )
            candidate.stage = target
            candidate.status = RESET_TARGET_STATUS[target]
            candidate.post_config_steps = []
            candidate.summary = {}
            candidate.last_error = None
            candidate.retry_count = 0
            if target == Stage.PREP:
                candidate.target_version = None
                candidate.iso_spec = None
                candidate.iso_local_path = None
                candidate.iso_skipped_download = False
                candidate.transfer = None
                candidate.install_percent = 0
                candidate.install_message = ""

        return self._commit(_mutate)

    def can_accept_update(self) -> bool:
        """Return True if it is safe to apply an app/ auto-update and restart now."""
        with self._lock:
            pair = (self._state.stage, self._state.status)
            if pair in _UPDATE_SAFE_PAIRS:
                return True
            return self._state.status in _UPDATE_SAFE_STATUSES

    @classmethod
    def load_or_create(
        cls,
        state_path: Path,
        session_id: str,
        client_id: str,
        *,
        installer_still_running: bool = False,
        max_exe_exists: bool = False,
        auto_resume_download: bool = False,
    ) -> DeploymentStateMachine:
        """Load persisted state (applying crash/restart recovery) or initialize a new file."""
        path = Path(state_path)
        if not path.exists():
            sm = cls(
                state_path=path,
                session_id=session_id,
                client_id=client_id,
                auto_resume_download=auto_resume_download,
            )
            sm._persist_state(sm._state)
            return sm

        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError(f"State file root must be a JSON object, got {type(data).__name__}")
            loaded = SessionState.from_dict(data)
            if not isinstance(loaded.stage, Stage) or not isinstance(
                loaded.status, StageStatus
            ):
                raise ValueError("Invalid stage/status types in state file")
        except (
            json.JSONDecodeError,
            UnicodeDecodeError,
            KeyError,
            TypeError,
            ValueError,
        ) as exc:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = path.parent / f"{path.name}.corrupt.{stamp}.{uuid.uuid4().hex[:6]}"
            os.replace(path, backup)
            logger.warning(
                "Corrupt state file %s backed up to %s (%s); initializing fresh PREP/idle state",
                path,
                backup,
                exc,
            )
            sm = cls(
                state_path=path,
                session_id=session_id,
                client_id=client_id,
                auto_resume_download=auto_resume_download,
            )
            sm._persist_state(sm._state)
            return sm

        dirty = False
        if not loaded.session_id and session_id:
            loaded.session_id = str(session_id)
            dirty = True
        if not loaded.client_id and client_id:
            loaded.client_id = str(client_id)
            dirty = True

        if loaded.status == StageStatus.RUNNING:
            if loaded.stage == Stage.DOWNLOAD_ISO:
                if not auto_resume_download:
                    loaded.status = StageStatus.PAUSED
                    loaded.last_error = (
                        "Interrupted by restart — click Resume to continue ISO download"
                    )
                    dirty = True
            elif loaded.stage == Stage.INSTALL_MAX:
                if installer_still_running:
                    pass
                elif max_exe_exists:
                    loaded.stage = Stage.WAIT_LICENSE
                    loaded.status = StageStatus.WAITING_OPERATOR
                    loaded.install_percent = 100
                    loaded.install_message = (
                        loaded.install_message
                        or "Installation completed (detected on restart)"
                    )
                    loaded.last_error = None
                    dirty = True
                else:
                    loaded.status = StageStatus.FAILED
                    loaded.last_error = (
                        "Installation interrupted by restart — click Retry"
                    )
                    dirty = True
            elif loaded.stage == Stage.FIRST_RUN:
                loaded.status = StageStatus.PAUSED
                loaded.last_error = "First run interrupted by restart — click Resume"
                dirty = True
            elif loaded.stage == Stage.POST_CONFIG:
                loaded.status = StageStatus.PAUSED
                loaded.last_error = (
                    "Post-configuration interrupted by restart — click Resume"
                )
                for step in loaded.post_config_steps:
                    if step.status == "running":
                        step.status = "pending"
                        step.detail = ""
                dirty = True
            elif loaded.stage == Stage.PREP:
                loaded.status = StageStatus.IDLE
                dirty = True
            elif loaded.stage == Stage.WAIT_LICENSE:
                loaded.status = StageStatus.WAITING_OPERATOR
                dirty = True
            elif loaded.stage == Stage.DONE:
                loaded.status = StageStatus.COMPLETED
                dirty = True

        sm = cls(
            state_path=path,
            session_id=loaded.session_id,
            client_id=loaded.client_id,
            auto_resume_download=auto_resume_download,
        )
        if dirty:
            loaded.updated_at = utc_now_iso()
            sm._persist_state(loaded)
        sm._state = loaded
        return sm


__all__ = [
    "InvalidTransitionError",
    "DeploymentStateMachine",
    "STAGE_ORDER",
    "STAGE_INDEX",
    "WORK_STAGES",
    "SKIP_TARGET_STATUS",
    "RESET_TARGET_STATUS",
    "VALID_POST_CONFIG_STEP_STATUSES",
]
