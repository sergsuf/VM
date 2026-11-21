"""Unit tests for DeploymentStateMachine, persistence, recovery, and concurrency."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from remote.protocol import (
    IsoSpec,
    PostConfigStepState,
    SessionState,
    Stage,
    StageStatus,
    TransferProgress,
)
from remote.state_machine import DeploymentStateMachine, InvalidTransitionError


def _sample_iso() -> IsoSpec:
    return IsoSpec(
        file_id="iso-max-2025",
        filename="Autodesk_3ds_Max_2025.iso",
        version="2025",
        size_bytes=5_000_000_000,
        sha256="a" * 64,
        download_url=None,
    )


def _sample_steps() -> list[PostConfigStepState]:
    return [
        PostConfigStepState("renderer_corona", "Install Corona", "renderer"),
        PostConfigStepState("renderer_vray", "Install V-Ray", "renderer"),
        PostConfigStepState("ui_theme", "Apply Theme", "theme"),
        PostConfigStepState("verify_install", "Verify", "verify"),
    ]


def _advance_to(
    sm: DeploymentStateMachine,
    target_stage: Stage,
    target_status: StageStatus,
) -> DeploymentStateMachine:
    """Drive a fresh PREP/idle state machine to any of the 20 reachable states."""
    if (target_stage, target_status) == (Stage.PREP, StageStatus.IDLE):
        return sm

    sm.start_install("2025", _sample_iso())
    if target_stage == Stage.DOWNLOAD_ISO:
        if target_status == StageStatus.PAUSED:
            sm.pause("Paused download")
        elif target_status == StageStatus.FAILED:
            sm.fail("Network error")
        elif target_status == StageStatus.ABORTED:
            sm.abort("Operator cancelled")
        return sm

    sm.complete_iso_download(r"C:\Install\Autodesk_3ds_Max_2025.iso", skipped=False)
    if target_stage == Stage.INSTALL_MAX:
        if target_status == StageStatus.PAUSED:
            sm.pause("Paused install")
        elif target_status == StageStatus.FAILED:
            sm.fail("MSI 1603")
        elif target_status == StageStatus.ABORTED:
            sm.abort("Aborted install")
        return sm

    sm.complete_install()
    if target_stage == Stage.WAIT_LICENSE:
        return sm

    sm.confirm_license()
    if target_stage == Stage.FIRST_RUN:
        if target_status == StageStatus.PAUSED:
            sm.pause("Paused first run")
        elif target_status == StageStatus.FAILED:
            sm.fail("Timeout")
        elif target_status == StageStatus.ABORTED:
            sm.abort("Aborted first run")
        return sm

    sm.complete_first_run(auto_post_config=False)
    if target_stage == Stage.POST_CONFIG:
        if target_status == StageStatus.WAITING_OPERATOR:
            return sm
        sm.start_post_config(_sample_steps())
        if target_status == StageStatus.PAUSED:
            sm.pause("Paused post config")
        elif target_status == StageStatus.FAILED:
            sm.fail("Step failed")
        elif target_status == StageStatus.ABORTED:
            sm.abort("Aborted post config")
        return sm

    sm.start_post_config(_sample_steps())
    sm.complete_post_config({"ok": True})
    return sm


class TestHappyPathLifecycle(unittest.TestCase):
    def test_full_lifecycle_manual_post_config(self) -> None:
        sm = DeploymentStateMachine(session_id="S-ABCD", client_id="client-1")
        self.assertEqual(sm.state.stage, Stage.PREP)
        self.assertEqual(sm.state.status, StageStatus.IDLE)
        self.assertEqual(sm.state.session_id, "S-ABCD")
        self.assertEqual(sm.state.client_id, "client-1")

        iso = _sample_iso()
        s = sm.start_install(" 2025 ", iso)
        self.assertEqual(s.stage, Stage.DOWNLOAD_ISO)
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertEqual(s.target_version, "2025")
        self.assertEqual(s.iso_spec, iso)
        self.assertEqual(s.retry_count, 0)

        prog = TransferProgress(
            phase="downloading",
            bytes_done=2_500_000_000,
            bytes_total=5_000_000_000,
            percent=50.0,
            speed_bps=125_000_000.0,
            eta_seconds=20.0,
            local_path=r"C:\Install\Autodesk_3ds_Max_2025.iso.part",
        )
        s = sm.update_transfer_progress(prog)
        self.assertEqual(s.stage, Stage.DOWNLOAD_ISO)
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertEqual(s.transfer, prog)

        s = sm.complete_iso_download(r"C:\Install\Autodesk_3ds_Max_2025.iso", skipped=False)
        self.assertEqual(s.stage, Stage.INSTALL_MAX)
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertEqual(s.iso_local_path, r"C:\Install\Autodesk_3ds_Max_2025.iso")
        self.assertFalse(s.iso_skipped_download)
        assert s.transfer is not None
        self.assertEqual(s.transfer.phase, "done")
        self.assertEqual(s.transfer.local_path, r"C:\Install\Autodesk_3ds_Max_2025.iso")

        s = sm.update_install_progress(45, "Installing files...")
        self.assertEqual(s.install_percent, 45)
        self.assertEqual(s.install_message, "Installing files...")

        s = sm.complete_install()
        self.assertEqual(s.stage, Stage.WAIT_LICENSE)
        self.assertEqual(s.status, StageStatus.WAITING_OPERATOR)
        self.assertEqual(s.install_percent, 100)
        self.assertIsNone(s.last_error)

        s = sm.confirm_license()
        self.assertEqual(s.stage, Stage.FIRST_RUN)
        self.assertEqual(s.status, StageStatus.RUNNING)

        s = sm.complete_first_run(auto_post_config=False)
        self.assertEqual(s.stage, Stage.POST_CONFIG)
        self.assertEqual(s.status, StageStatus.WAITING_OPERATOR)

        steps = _sample_steps()
        s = sm.start_post_config(steps)
        self.assertEqual(s.stage, Stage.POST_CONFIG)
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertEqual(len(s.post_config_steps), 4)

        s = sm.update_post_config_step("renderer_corona", "done", "Installed Corona 12")
        s = sm.update_post_config_step("renderer_vray", "skipped", "Already installed")
        s = sm.update_post_config_step("ui_theme", "done", "Applied theme")
        s = sm.update_post_config_step("verify_install", "done", "All checks passed")
        self.assertEqual(s.post_config_steps[0].status, "done")
        self.assertEqual(s.post_config_steps[1].status, "skipped")
        self.assertEqual(s.post_config_steps[3].detail, "All checks passed")

        summary = {"ok": True, "version": "2025", "renderers": ["Chaos Corona", "V-Ray"]}
        s = sm.complete_post_config(summary)
        self.assertEqual(s.stage, Stage.DONE)
        self.assertEqual(s.status, StageStatus.COMPLETED)
        self.assertEqual(s.summary, summary)
        self.assertIsNone(s.last_error)

    def test_iso_precheck_skipped_download(self) -> None:
        sm = DeploymentStateMachine()
        sm.start_install("2025", _sample_iso())
        sm.update_transfer_progress(
            TransferProgress(phase="checking", bytes_done=0, bytes_total=100, percent=0.0)
        )
        s = sm.complete_iso_download(r"D:\Install\3dsMax2025.iso", skipped=True)
        self.assertEqual(s.stage, Stage.INSTALL_MAX)
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertTrue(s.iso_skipped_download)
        self.assertEqual(s.iso_local_path, r"D:\Install\3dsMax2025.iso")
        assert s.transfer is not None
        self.assertEqual(s.transfer.phase, "skipped")
        self.assertEqual(s.transfer.local_path, r"D:\Install\3dsMax2025.iso")

    def test_auto_post_config_flow(self) -> None:
        sm = _advance_to(DeploymentStateMachine(), Stage.FIRST_RUN, StageStatus.RUNNING)
        s = sm.complete_first_run(auto_post_config=True)
        self.assertEqual(s.stage, Stage.POST_CONFIG)
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertEqual(s.post_config_steps, [])

        # Initializing steps once when POST_CONFIG/running and steps are empty is allowed
        s = sm.start_post_config(_sample_steps())
        self.assertEqual(len(s.post_config_steps), 4)
        sm.update_post_config_step("verify_install", "done", "OK")
        s = sm.complete_post_config({"ok": True})
        self.assertEqual(s.stage, Stage.DONE)
        self.assertEqual(s.status, StageStatus.COMPLETED)

    def test_return_to_wait_license_and_reconfirm(self) -> None:
        sm = _advance_to(DeploymentStateMachine(), Stage.FIRST_RUN, StageStatus.RUNNING)
        s = sm.return_to_wait_license("License dialog detected")
        self.assertEqual(s.stage, Stage.WAIT_LICENSE)
        self.assertEqual(s.status, StageStatus.WAITING_OPERATOR)
        self.assertEqual(s.last_error, "License dialog detected")

        s = sm.confirm_license()
        self.assertEqual(s.stage, Stage.FIRST_RUN)
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertIsNone(s.last_error)

    def test_install_progress_clamping(self) -> None:
        sm = _advance_to(DeploymentStateMachine(), Stage.INSTALL_MAX, StageStatus.RUNNING)
        s = sm.update_install_progress(-20, "Negative")
        self.assertEqual(s.install_percent, 0)
        s = sm.update_install_progress(150, "Over")
        self.assertEqual(s.install_percent, 100)
        s = sm.update_install_progress(67, "Normal")
        self.assertEqual(s.install_percent, 67)
        self.assertEqual(s.install_message, "Normal")


class TestInvalidTransitions(unittest.TestCase):
    def _assert_rejected(
        self, sm: DeploymentStateMachine, fn, *args, **kwargs
    ) -> None:
        before = sm.state
        with self.assertRaises(InvalidTransitionError) as ctx:
            fn(*args, **kwargs)
        self.assertIsInstance(ctx.exception, ValueError)
        self.assertEqual(sm.state, before)

    def test_invalid_start_install(self) -> None:
        sm = DeploymentStateMachine()
        self._assert_rejected(sm, sm.start_install, "")
        self._assert_rejected(sm, sm.start_install, "   ")
        self._assert_rejected(sm, sm.start_install, "2025", "not-an-iso")  # type: ignore[arg-type]

        for stage, status in [
            (Stage.DOWNLOAD_ISO, StageStatus.RUNNING),
            (Stage.DOWNLOAD_ISO, StageStatus.PAUSED),
            (Stage.INSTALL_MAX, StageStatus.RUNNING),
            (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            (Stage.FIRST_RUN, StageStatus.RUNNING),
            (Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR),
            (Stage.DONE, StageStatus.COMPLETED),
        ]:
            other = _advance_to(DeploymentStateMachine(), stage, status)
            self._assert_rejected(other, other.start_install, "2025")

    def test_invalid_stage_specific_methods(self) -> None:
        sm_prep = DeploymentStateMachine()
        prog = TransferProgress(phase="downloading")
        self._assert_rejected(sm_prep, sm_prep.update_transfer_progress, prog)
        self._assert_rejected(sm_prep, sm_prep.complete_iso_download, "/tmp/x.iso")
        self._assert_rejected(sm_prep, sm_prep.update_install_progress, 10, "msg")
        self._assert_rejected(sm_prep, sm_prep.complete_install)
        self._assert_rejected(sm_prep, sm_prep.confirm_license)
        self._assert_rejected(sm_prep, sm_prep.return_to_wait_license, "reason")
        self._assert_rejected(sm_prep, sm_prep.complete_first_run)
        self._assert_rejected(sm_prep, sm_prep.start_post_config, _sample_steps())
        self._assert_rejected(sm_prep, sm_prep.update_post_config_step, "ui_theme", "done")
        self._assert_rejected(sm_prep, sm_prep.complete_post_config)

        sm_dl = _advance_to(DeploymentStateMachine(), Stage.DOWNLOAD_ISO, StageStatus.RUNNING)
        self._assert_rejected(sm_dl, sm_dl.complete_iso_download, "   ")
        self._assert_rejected(sm_dl, sm_dl.update_transfer_progress, {"phase": "downloading"})  # type: ignore[arg-type]

        sm_inst = _advance_to(DeploymentStateMachine(), Stage.INSTALL_MAX, StageStatus.RUNNING)
        self._assert_rejected(sm_inst, sm_inst.update_install_progress, True, "bad bool")  # type: ignore[arg-type]

        sm_pc_wait = _advance_to(
            DeploymentStateMachine(), Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR
        )
        dup_steps = [
            PostConfigStepState("dup", "A", "theme"),
            PostConfigStepState("dup", "B", "theme"),
        ]
        self._assert_rejected(sm_pc_wait, sm_pc_wait.start_post_config, dup_steps)
        self._assert_rejected(sm_pc_wait, sm_pc_wait.update_post_config_step, "dup", "done")

        sm_pc_run = _advance_to(
            DeploymentStateMachine(), Stage.POST_CONFIG, StageStatus.RUNNING
        )
        # Second start_post_config while steps already populated must fail
        self._assert_rejected(sm_pc_run, sm_pc_run.start_post_config, _sample_steps())
        self._assert_rejected(sm_pc_run, sm_pc_run.update_post_config_step, "unknown_id", "done")
        self._assert_rejected(
            sm_pc_run, sm_pc_run.update_post_config_step, "ui_theme", "bogus_status"
        )

        sm_pc_paused = _advance_to(
            DeploymentStateMachine(), Stage.POST_CONFIG, StageStatus.PAUSED
        )
        self._assert_rejected(sm_pc_paused, sm_pc_paused.start_post_config, _sample_steps())

    def test_update_install_progress_rejects_non_finite_and_bool(self) -> None:
        sm_inst = _advance_to(DeploymentStateMachine(), Stage.INSTALL_MAX, StageStatus.RUNNING)
        sm_inst.update_install_progress(25, "Copying files")
        for bad_percent in (
            float("inf"),
            float("-inf"),
            float("nan"),
            True,
            False,
        ):
            self._assert_rejected(
                sm_inst,
                sm_inst.update_install_progress,
                bad_percent,  # type: ignore[arg-type]
                "bad percent",
            )
        self.assertEqual(sm_inst.state.install_percent, 25)
        self.assertEqual(sm_inst.state.install_message, "Copying files")

    def test_invalid_lifecycle_controls(self) -> None:
        # pause rejected on non-running states
        for stage, status in [
            (Stage.PREP, StageStatus.IDLE),
            (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            (Stage.DOWNLOAD_ISO, StageStatus.PAUSED),
            (Stage.INSTALL_MAX, StageStatus.FAILED),
            (Stage.FIRST_RUN, StageStatus.ABORTED),
            (Stage.DONE, StageStatus.COMPLETED),
        ]:
            sm = _advance_to(DeploymentStateMachine(), stage, status)
            self._assert_rejected(sm, sm.pause, "nope")
            self._assert_rejected(sm, sm.fail, "nope")

        # resume rejected on non-paused states
        for stage, status in [
            (Stage.PREP, StageStatus.IDLE),
            (Stage.DOWNLOAD_ISO, StageStatus.RUNNING),
            (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            (Stage.INSTALL_MAX, StageStatus.FAILED),
            (Stage.FIRST_RUN, StageStatus.ABORTED),
            (Stage.DONE, StageStatus.COMPLETED),
        ]:
            sm = _advance_to(DeploymentStateMachine(), stage, status)
            self._assert_rejected(sm, sm.resume)

        # abort rejected on idle/waiting/failed/aborted/completed
        for stage, status in [
            (Stage.PREP, StageStatus.IDLE),
            (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            (Stage.INSTALL_MAX, StageStatus.FAILED),
            (Stage.FIRST_RUN, StageStatus.ABORTED),
            (Stage.DONE, StageStatus.COMPLETED),
        ]:
            sm = _advance_to(DeploymentStateMachine(), stage, status)
            self._assert_rejected(sm, sm.abort, "nope")

        # retry rejected on idle/running/waiting/paused/completed
        for stage, status in [
            (Stage.PREP, StageStatus.IDLE),
            (Stage.DOWNLOAD_ISO, StageStatus.RUNNING),
            (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            (Stage.FIRST_RUN, StageStatus.PAUSED),
            (Stage.DONE, StageStatus.COMPLETED),
        ]:
            sm = _advance_to(DeploymentStateMachine(), stage, status)
            self._assert_rejected(sm, sm.retry)

    def test_invalid_skip_and_reset(self) -> None:
        # Running stages cannot skip or reset
        for work_stage in (
            Stage.DOWNLOAD_ISO,
            Stage.INSTALL_MAX,
            Stage.FIRST_RUN,
            Stage.POST_CONFIG,
        ):
            sm = _advance_to(DeploymentStateMachine(), work_stage, StageStatus.RUNNING)
            self._assert_rejected(sm, sm.skip_stage, Stage.DONE)
            self._assert_rejected(sm, sm.reset, Stage.PREP)

        # Invalid skip targets, same stage, backward stage, unknown stage
        sm_prep = DeploymentStateMachine()
        for bad_target in (Stage.PREP, Stage.DOWNLOAD_ISO, Stage.INSTALL_MAX, Stage.FIRST_RUN):
            self._assert_rejected(sm_prep, sm_prep.skip_stage, bad_target)
        self._assert_rejected(sm_prep, sm_prep.skip_stage, "BOGUS")  # type: ignore[arg-type]

        sm_pc = _advance_to(
            DeploymentStateMachine(), Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR
        )
        self._assert_rejected(sm_pc, sm_pc.skip_stage, Stage.WAIT_LICENSE)
        self._assert_rejected(sm_pc, sm_pc.skip_stage, Stage.POST_CONFIG)

        sm_done = _advance_to(DeploymentStateMachine(), Stage.DONE, StageStatus.COMPLETED)
        self._assert_rejected(sm_done, sm_done.skip_stage, Stage.DONE)

        # Invalid reset targets
        for bad_reset in (
            Stage.DOWNLOAD_ISO,
            Stage.INSTALL_MAX,
            Stage.FIRST_RUN,
            Stage.DONE,
        ):
            self._assert_rejected(sm_done, sm_done.reset, bad_reset)
        self._assert_rejected(sm_done, sm_done.reset, "UNKNOWN")  # type: ignore[arg-type]


class TestLifecycleControls(unittest.TestCase):
    def test_pause_and_resume_all_work_stages(self) -> None:
        for work_stage in (
            Stage.DOWNLOAD_ISO,
            Stage.INSTALL_MAX,
            Stage.FIRST_RUN,
            Stage.POST_CONFIG,
        ):
            sm = _advance_to(DeploymentStateMachine(), work_stage, StageStatus.RUNNING)
            paused = sm.pause("Operator break")
            self.assertEqual(paused.stage, work_stage)
            self.assertEqual(paused.status, StageStatus.PAUSED)
            self.assertEqual(paused.last_error, "Operator break")

            resumed = sm.resume()
            self.assertEqual(resumed.stage, work_stage)
            self.assertEqual(resumed.status, StageStatus.RUNNING)
            self.assertIsNone(resumed.last_error)

    def test_fail_and_retry_increments_counter_and_preserves_post_config_done_steps(self) -> None:
        # INSTALL_MAX retry resets install_percent and install_message
        sm_inst = _advance_to(DeploymentStateMachine(), Stage.INSTALL_MAX, StageStatus.RUNNING)
        sm_inst.update_install_progress(60, "Extracting...")
        sm_inst.fail("MSI 1603")
        self.assertEqual(sm_inst.state.status, StageStatus.FAILED)
        self.assertEqual(sm_inst.state.last_error, "MSI 1603")

        s = sm_inst.retry()
        self.assertEqual(s.status, StageStatus.RUNNING)
        self.assertEqual(s.retry_count, 1)
        self.assertEqual(s.install_percent, 0)
        self.assertEqual(s.install_message, "")
        self.assertIsNone(s.last_error)

        # POST_CONFIG retry preserves done/skipped steps and resets failed/running to pending
        sm_pc = _advance_to(DeploymentStateMachine(), Stage.POST_CONFIG, StageStatus.RUNNING)
        sm_pc.update_post_config_step("renderer_corona", "done", "Installed")
        sm_pc.update_post_config_step("renderer_vray", "skipped", "Skipped")
        sm_pc.update_post_config_step("ui_theme", "failed", "Disk error")
        sm_pc.update_post_config_step("verify_install", "running", "In progress")
        sm_pc.fail("Theme failed")
        s1 = sm_pc.retry()
        self.assertEqual(s1.retry_count, 1)
        by_id = {st.step_id: st for st in s1.post_config_steps}
        self.assertEqual(by_id["renderer_corona"].status, "done")
        self.assertEqual(by_id["renderer_corona"].detail, "Installed")
        self.assertEqual(by_id["renderer_vray"].status, "skipped")
        self.assertEqual(by_id["renderer_vray"].detail, "Skipped")
        self.assertEqual(by_id["ui_theme"].status, "pending")
        self.assertEqual(by_id["ui_theme"].detail, "")
        self.assertEqual(by_id["verify_install"].status, "pending")
        self.assertEqual(by_id["verify_install"].detail, "")

        sm_pc.fail("Second failure")
        s2 = sm_pc.retry()
        self.assertEqual(s2.retry_count, 2)

    def test_abort_from_running_and_paused_then_retry_or_reset(self) -> None:
        sm = _advance_to(DeploymentStateMachine(), Stage.DOWNLOAD_ISO, StageStatus.RUNNING)
        aborted = sm.abort("User stopped")
        self.assertEqual(aborted.status, StageStatus.ABORTED)
        self.assertEqual(aborted.last_error, "User stopped")

        retried = sm.retry()
        self.assertEqual(retried.status, StageStatus.RUNNING)
        self.assertEqual(retried.retry_count, 1)

        sm.pause("Hold")
        aborted2 = sm.abort("")
        self.assertEqual(aborted2.status, StageStatus.ABORTED)
        self.assertEqual(aborted2.last_error, "Aborted by operator")

        reset_state = sm.reset(Stage.PREP)
        self.assertEqual(reset_state.stage, Stage.PREP)
        self.assertEqual(reset_state.status, StageStatus.IDLE)
        self.assertEqual(reset_state.retry_count, 0)

    def test_skip_stage_valid_paths(self) -> None:
        valid_skips = [
            ((Stage.PREP, StageStatus.IDLE), Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            ((Stage.PREP, StageStatus.IDLE), Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR),
            ((Stage.PREP, StageStatus.IDLE), Stage.DONE, StageStatus.COMPLETED),
            ((Stage.DOWNLOAD_ISO, StageStatus.PAUSED), Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            ((Stage.DOWNLOAD_ISO, StageStatus.FAILED), Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR),
            ((Stage.DOWNLOAD_ISO, StageStatus.ABORTED), Stage.DONE, StageStatus.COMPLETED),
            ((Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR), Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR),
            ((Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR), Stage.DONE, StageStatus.COMPLETED),
            ((Stage.FIRST_RUN, StageStatus.FAILED), Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR),
            ((Stage.FIRST_RUN, StageStatus.FAILED), Stage.DONE, StageStatus.COMPLETED),
            ((Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR), Stage.DONE, StageStatus.COMPLETED),
            ((Stage.POST_CONFIG, StageStatus.PAUSED), Stage.DONE, StageStatus.COMPLETED),
            ((Stage.POST_CONFIG, StageStatus.FAILED), Stage.DONE, StageStatus.COMPLETED),
            ((Stage.POST_CONFIG, StageStatus.ABORTED), Stage.DONE, StageStatus.COMPLETED),
        ]
        for (start_stage, start_status), target, expected_status in valid_skips:
            with self.subTest(start=(start_stage, start_status), target=target):
                sm = _advance_to(DeploymentStateMachine(), start_stage, start_status)
                res = sm.skip_stage(target)
                self.assertEqual(res.stage, target)
                self.assertEqual(res.status, expected_status)
                self.assertIsNone(res.last_error)

    def test_reset_to_prep_wait_license_and_post_config(self) -> None:
        sm = _advance_to(
            DeploymentStateMachine(session_id="S-1111", client_id="C-1"),
            Stage.DONE,
            StageStatus.COMPLETED,
        )

        s_pc = sm.reset(Stage.POST_CONFIG)
        self.assertEqual(s_pc.stage, Stage.POST_CONFIG)
        self.assertEqual(s_pc.status, StageStatus.WAITING_OPERATOR)
        self.assertEqual(s_pc.session_id, "S-1111")
        self.assertEqual(s_pc.client_id, "C-1")
        self.assertEqual(s_pc.target_version, "2025")
        self.assertIsNotNone(s_pc.iso_local_path)
        self.assertEqual(s_pc.post_config_steps, [])
        self.assertEqual(s_pc.summary, {})

        s_wl = sm.reset(Stage.WAIT_LICENSE)
        self.assertEqual(s_wl.stage, Stage.WAIT_LICENSE)
        self.assertEqual(s_wl.status, StageStatus.WAITING_OPERATOR)
        self.assertEqual(s_wl.target_version, "2025")
        self.assertIsNotNone(s_wl.iso_spec)

        s_prep = sm.reset(Stage.PREP)
        self.assertEqual(s_prep.stage, Stage.PREP)
        self.assertEqual(s_prep.status, StageStatus.IDLE)
        self.assertEqual(s_prep.session_id, "S-1111")
        self.assertEqual(s_prep.client_id, "C-1")
        self.assertIsNone(s_prep.target_version)
        self.assertIsNone(s_prep.iso_spec)
        self.assertIsNone(s_prep.iso_local_path)
        self.assertFalse(s_prep.iso_skipped_download)
        self.assertIsNone(s_prep.transfer)
        self.assertEqual(s_prep.install_percent, 0)
        self.assertEqual(s_prep.install_message, "")

        # Can immediately start a fresh install after reset(PREP)
        started = sm.start_install("2026")
        self.assertEqual(started.stage, Stage.DOWNLOAD_ISO)
        self.assertEqual(started.target_version, "2026")


class TestCanAcceptUpdateTruthTable(unittest.TestCase):
    def test_all_20_reachable_states(self) -> None:
        reachable: list[tuple[Stage, StageStatus, bool]] = [
            (Stage.PREP, StageStatus.IDLE, True),
            (Stage.DOWNLOAD_ISO, StageStatus.RUNNING, False),
            (Stage.DOWNLOAD_ISO, StageStatus.PAUSED, True),
            (Stage.DOWNLOAD_ISO, StageStatus.FAILED, True),
            (Stage.DOWNLOAD_ISO, StageStatus.ABORTED, True),
            (Stage.INSTALL_MAX, StageStatus.RUNNING, False),
            (Stage.INSTALL_MAX, StageStatus.PAUSED, True),
            (Stage.INSTALL_MAX, StageStatus.FAILED, True),
            (Stage.INSTALL_MAX, StageStatus.ABORTED, True),
            (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR, True),
            (Stage.FIRST_RUN, StageStatus.RUNNING, False),
            (Stage.FIRST_RUN, StageStatus.PAUSED, True),
            (Stage.FIRST_RUN, StageStatus.FAILED, True),
            (Stage.FIRST_RUN, StageStatus.ABORTED, True),
            (Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR, True),
            (Stage.POST_CONFIG, StageStatus.RUNNING, False),
            (Stage.POST_CONFIG, StageStatus.PAUSED, True),
            (Stage.POST_CONFIG, StageStatus.FAILED, True),
            (Stage.POST_CONFIG, StageStatus.ABORTED, True),
            (Stage.DONE, StageStatus.COMPLETED, True),
        ]
        self.assertEqual(len(reachable), 20)
        for stage, status, expected in reachable:
            with self.subTest(stage=stage.value, status=status.value):
                sm = _advance_to(DeploymentStateMachine(), stage, status)
                self.assertEqual(sm.state.stage, stage)
                self.assertEqual(sm.state.status, status)
                self.assertEqual(sm.can_accept_update(), expected)


class TestPersistenceAndRecovery(unittest.TestCase):
    def test_load_or_create_missing_creates_parents_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "nested" / "dir" / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-9999", "client-a")
            self.assertTrue(state_path.is_file())
            self.assertEqual(sm.state_path, state_path)
            loaded = SessionState.from_dict(json.loads(state_path.read_text("utf-8")))
            self.assertEqual(loaded.stage, Stage.PREP)
            self.assertEqual(loaded.status, StageStatus.IDLE)
            self.assertEqual(loaded.session_id, "S-9999")
            self.assertEqual(loaded.client_id, "client-a")

    def test_atomic_write_uses_fsync_and_replace_with_no_tmp_leftover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-1234", "c1")

            real_fsync = os.fsync
            real_replace = os.replace
            with patch("remote.state_machine.os.fsync", wraps=real_fsync) as mock_fsync, patch(
                "remote.state_machine.os.replace", wraps=real_replace
            ) as mock_replace:
                sm.start_install("2025", _sample_iso())
                self.assertGreaterEqual(mock_fsync.call_count, 1)
                self.assertGreaterEqual(mock_replace.call_count, 1)

            on_disk = json.loads(state_path.read_text("utf-8"))
            self.assertEqual(on_disk, sm.state.to_dict())
            tmp_files = list(Path(tmp).glob("*.tmp"))
            self.assertEqual(tmp_files, [])

    def test_persist_failure_rolls_back_memory_and_cleans_tmp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-1234", "c1")
            before_disk = state_path.read_text("utf-8")

            with patch("remote.state_machine.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    sm.start_install("2025", _sample_iso())

            self.assertEqual(sm.state.stage, Stage.PREP)
            self.assertEqual(sm.state.status, StageStatus.IDLE)
            self.assertEqual(state_path.read_text("utf-8"), before_disk)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_recovery_download_iso_running_default_pauses_and_auto_resumes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-DL", "c1")
            sm.start_install("2025", _sample_iso())
            sm.update_transfer_progress(
                TransferProgress(
                    phase="downloading",
                    bytes_done=1024,
                    bytes_total=4096,
                    percent=25.0,
                )
            )

            # Auto-resume keeps DOWNLOAD_ISO running
            sm_auto = DeploymentStateMachine.load_or_create(
                state_path, "S-DL", "c1", auto_resume_download=True
            )
            self.assertEqual(sm_auto.state.stage, Stage.DOWNLOAD_ISO)
            self.assertEqual(sm_auto.state.status, StageStatus.RUNNING)

            # Default pauses and preserves transfer & iso_spec
            sm_paused = DeploymentStateMachine.load_or_create(
                state_path, "S-DL", "c1", auto_resume_download=False
            )
            self.assertEqual(sm_paused.state.stage, Stage.DOWNLOAD_ISO)
            self.assertEqual(sm_paused.state.status, StageStatus.PAUSED)
            self.assertIsNotNone(sm_paused.state.iso_spec)
            assert sm_paused.state.transfer is not None
            self.assertEqual(sm_paused.state.transfer.bytes_done, 1024)
            self.assertIn("Resume", sm_paused.state.last_error or "")

    def test_recovery_install_max_running_all_four_flag_combos(self) -> None:
        cases = [
            (True, False, Stage.INSTALL_MAX, StageStatus.RUNNING),
            (True, True, Stage.INSTALL_MAX, StageStatus.RUNNING),
            (False, True, Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            (False, False, Stage.INSTALL_MAX, StageStatus.FAILED),
        ]
        for running_flag, exe_flag, exp_stage, exp_status in cases:
            with self.subTest(installer_running=running_flag, max_exe=exe_flag):
                with tempfile.TemporaryDirectory() as tmp:
                    state_path = Path(tmp) / "session_state.json"
                    sm = DeploymentStateMachine.load_or_create(state_path, "S-IN", "c1")
                    sm.start_install("2025")
                    sm.complete_iso_download(r"C:\Install\max.iso")
                    sm.update_install_progress(50, "Installing...")

                    rec = DeploymentStateMachine.load_or_create(
                        state_path,
                        "S-IN",
                        "c1",
                        installer_still_running=running_flag,
                        max_exe_exists=exe_flag,
                    )
                    self.assertEqual(rec.state.stage, exp_stage)
                    self.assertEqual(rec.state.status, exp_status)
                    if exp_stage == Stage.WAIT_LICENSE:
                        self.assertEqual(rec.state.install_percent, 100)
                        self.assertIsNone(rec.state.last_error)
                    if exp_status == StageStatus.FAILED:
                        self.assertIn("Retry", rec.state.last_error or "")

    def test_recovery_first_run_running_pauses(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-FR", "c1")
            _advance_to(sm, Stage.FIRST_RUN, StageStatus.RUNNING)

            rec = DeploymentStateMachine.load_or_create(state_path, "S-FR", "c1")
            self.assertEqual(rec.state.stage, Stage.FIRST_RUN)
            self.assertEqual(rec.state.status, StageStatus.PAUSED)
            self.assertIn("Resume", rec.state.last_error or "")

    def test_recovery_post_config_running_pauses_and_resets_inflight_step(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-PC", "c1")
            _advance_to(sm, Stage.POST_CONFIG, StageStatus.RUNNING)
            sm.update_post_config_step("renderer_corona", "done", "Installed")
            sm.update_post_config_step("renderer_vray", "skipped", "Already present")
            sm.update_post_config_step("ui_theme", "running", "Copying...")

            rec = DeploymentStateMachine.load_or_create(state_path, "S-PC", "c1")
            self.assertEqual(rec.state.stage, Stage.POST_CONFIG)
            self.assertEqual(rec.state.status, StageStatus.PAUSED)
            by_id = {st.step_id: st for st in rec.state.post_config_steps}
            self.assertEqual(by_id["renderer_corona"].status, "done")
            self.assertEqual(by_id["renderer_vray"].status, "skipped")
            self.assertEqual(by_id["ui_theme"].status, "pending")
            self.assertEqual(by_id["ui_theme"].detail, "")
            self.assertEqual(by_id["verify_install"].status, "pending")

    def test_recovery_preserves_all_non_running_states_verbatim(self) -> None:
        states = [
            (Stage.PREP, StageStatus.IDLE),
            (Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR),
            (Stage.POST_CONFIG, StageStatus.WAITING_OPERATOR),
            (Stage.DONE, StageStatus.COMPLETED),
            (Stage.DOWNLOAD_ISO, StageStatus.PAUSED),
            (Stage.INSTALL_MAX, StageStatus.FAILED),
            (Stage.FIRST_RUN, StageStatus.ABORTED),
        ]
        for stage, status in states:
            with self.subTest(stage=stage.value, status=status.value):
                with tempfile.TemporaryDirectory() as tmp:
                    state_path = Path(tmp) / "session_state.json"
                    sm = DeploymentStateMachine.load_or_create(state_path, "S-NR", "c1")
                    _advance_to(sm, stage, status)
                    expected = sm.state

                    rec = DeploymentStateMachine.load_or_create(state_path, "S-NR", "c1")
                    self.assertEqual(rec.state, expected)

    def test_load_or_create_session_client_id_reconciliation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            # Preserve existing non-empty IDs
            sm = DeploymentStateMachine.load_or_create(state_path, "S-ORIG", "C-ORIG")
            rec = DeploymentStateMachine.load_or_create(state_path, "S-NEW", "C-NEW")
            self.assertEqual(rec.state.session_id, "S-ORIG")
            self.assertEqual(rec.state.client_id, "C-ORIG")

            # Backfill empty IDs
            empty_path = Path(tmp) / "empty_ids.json"
            DeploymentStateMachine.load_or_create(empty_path, "", "")
            rec2 = DeploymentStateMachine.load_or_create(empty_path, "S-FILL", "C-FILL")
            self.assertEqual(rec2.state.session_id, "S-FILL")
            self.assertEqual(rec2.state.client_id, "C-FILL")

    def test_load_or_create_corrupt_json_quarantines_and_recreates(self) -> None:
        corrupt_samples = [
            ("truncated", '{"stage": "PREP", '),
            ("empty", ""),
            ("json_array", "[]"),
            ("bad_stage", json.dumps({"stage": "INVALID_STAGE", "status": "idle"})),
        ]
        for label, payload in corrupt_samples:
            with self.subTest(sample=label):
                with tempfile.TemporaryDirectory() as tmp:
                    state_path = Path(tmp) / "session_state.json"
                    state_path.write_text(payload, encoding="utf-8")

                    rec = DeploymentStateMachine.load_or_create(state_path, "S-OK", "c1")
                    self.assertEqual(rec.state.stage, Stage.PREP)
                    self.assertEqual(rec.state.status, StageStatus.IDLE)
                    backups = list(Path(tmp).glob("session_state.json.corrupt.*"))
                    self.assertEqual(len(backups), 1)
                    self.assertEqual(backups[0].read_text("utf-8"), payload)
                    reloaded = SessionState.from_dict(
                        json.loads(state_path.read_text("utf-8"))
                    )
                    self.assertEqual(reloaded.stage, Stage.PREP)


class TestSnapshotIsolationAndConcurrency(unittest.TestCase):
    def test_snapshot_isolation_outbound_and_inbound(self) -> None:
        iso = _sample_iso()
        steps = _sample_steps()
        summary = {"nested": {"count": 1}}

        sm = DeploymentStateMachine()
        s = sm.start_install("2025", iso)
        # Mutate inbound iso after call
        iso.filename = "MUTATED.iso"
        assert sm.state.iso_spec is not None
        self.assertEqual(sm.state.iso_spec.filename, "Autodesk_3ds_Max_2025.iso")

        # Mutate outbound snapshot
        assert s.iso_spec is not None
        s.iso_spec.filename = "OUTBOUND.iso"
        self.assertEqual(sm.state.iso_spec.filename, "Autodesk_3ds_Max_2025.iso")

        prog = TransferProgress(phase="downloading", bytes_done=10, bytes_total=100)
        s_prog = sm.update_transfer_progress(prog)
        prog.bytes_done = 999
        assert s_prog.transfer is not None
        s_prog.transfer.bytes_done = 888
        assert sm.state.transfer is not None
        self.assertEqual(sm.state.transfer.bytes_done, 10)

        sm.complete_iso_download(r"C:\Install\a.iso")
        sm.complete_install()
        sm.confirm_license()
        sm.complete_first_run()
        s_steps = sm.start_post_config(steps)
        steps[0].status = "failed"
        s_steps.post_config_steps[0].status = "done"
        self.assertEqual(sm.state.post_config_steps[0].status, "pending")

        s_done = sm.complete_post_config(summary)
        summary["nested"]["count"] = 999
        s_done.summary["nested"]["count"] = 888
        self.assertEqual(sm.state.summary["nested"]["count"], 1)

    def test_concurrent_barrier_single_winner_transition(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-CONC", "c1")
            _advance_to(sm, Stage.WAIT_LICENSE, StageStatus.WAITING_OPERATOR)

            n_threads = 10
            barrier = threading.Barrier(n_threads)
            winners = 0
            rejects = 0
            unexpected: list[Exception] = []
            count_lock = threading.Lock()

            def _worker() -> None:
                nonlocal winners, rejects
                barrier.wait()
                try:
                    sm.confirm_license()
                    with count_lock:
                        winners += 1
                except InvalidTransitionError:
                    with count_lock:
                        rejects += 1
                except Exception as exc:  # pragma: no cover
                    with count_lock:
                        unexpected.append(exc)

            threads = [threading.Thread(target=_worker) for _ in range(n_threads)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(unexpected, [])
            self.assertEqual(winners, 1)
            self.assertEqual(rejects, n_threads - 1)
            self.assertEqual(sm.state.stage, Stage.FIRST_RUN)
            self.assertEqual(sm.state.status, StageStatus.RUNNING)
            disk_state = SessionState.from_dict(json.loads(state_path.read_text("utf-8")))
            self.assertEqual(disk_state.stage, Stage.FIRST_RUN)
            self.assertEqual(disk_state.status, StageStatus.RUNNING)

    def test_concurrent_progress_writers_and_readers_with_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-IO", "c1")
            sm.start_install("2025", _sample_iso())

            errors: list[Exception] = []

            def _writer(tid: int) -> None:
                try:
                    for i in range(15):
                        sm.update_transfer_progress(
                            TransferProgress(
                                phase="downloading",
                                bytes_done=tid * 1000 + i,
                                bytes_total=100_000,
                                percent=float((i + 1) % 100),
                            )
                        )
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            def _reader() -> None:
                try:
                    for _ in range(30):
                        snap = sm.state
                        self.assertEqual(snap.stage, Stage.DOWNLOAD_ISO)
                        self.assertFalse(sm.can_accept_update())
                except Exception as exc:  # pragma: no cover
                    errors.append(exc)

            threads = [
                threading.Thread(target=_writer, args=(0,)),
                threading.Thread(target=_writer, args=(1,)),
                threading.Thread(target=_writer, args=(2,)),
                threading.Thread(target=_reader),
                threading.Thread(target=_reader),
                threading.Thread(target=_reader),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            self.assertEqual(errors, [])
            disk_state = SessionState.from_dict(json.loads(state_path.read_text("utf-8")))
            self.assertEqual(disk_state.stage, Stage.DOWNLOAD_ISO)
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])

    def test_concurrent_pause_resume_and_progress_updates(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "session_state.json"
            sm = DeploymentStateMachine.load_or_create(state_path, "S-PR", "c1")
            sm.start_install("2025", _sample_iso())

            stop = threading.Event()
            unexpected: list[Exception] = []

            def _updater() -> None:
                i = 0
                while not stop.is_set() and i < 60:
                    i += 1
                    try:
                        sm.update_transfer_progress(
                            TransferProgress(
                                phase="downloading",
                                bytes_done=i * 10,
                                bytes_total=1000,
                                percent=min(100.0, float(i)),
                            )
                        )
                    except InvalidTransitionError:
                        pass
                    except Exception as exc:  # pragma: no cover
                        unexpected.append(exc)

            def _toggler() -> None:
                for _ in range(15):
                    try:
                        sm.pause("toggle")
                    except InvalidTransitionError:
                        pass
                    except Exception as exc:  # pragma: no cover
                        unexpected.append(exc)
                    try:
                        sm.resume()
                    except InvalidTransitionError:
                        pass
                    except Exception as exc:  # pragma: no cover
                        unexpected.append(exc)
                stop.set()

            t1 = threading.Thread(target=_updater)
            t2 = threading.Thread(target=_toggler)
            t1.start()
            t2.start()
            t1.join()
            t2.join()

            self.assertEqual(unexpected, [])
            disk_state = SessionState.from_dict(json.loads(state_path.read_text("utf-8")))
            self.assertEqual(disk_state.stage, Stage.DOWNLOAD_ISO)
            self.assertIn(disk_state.status, (StageStatus.RUNNING, StageStatus.PAUSED))
            self.assertEqual(list(Path(tmp).glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
