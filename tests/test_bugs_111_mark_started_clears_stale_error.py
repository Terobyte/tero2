"""Bug 111: ``mark_started`` preserved the previous run's error_message.

When a run ends in FAILED or PAUSED the error/reason text is recorded on
``AgentState.error_message``.  ``mark_started`` intentionally preserves
the prior state so counters, current task, and checkpoint bookkeeping
survive across restarts — but it also preserved ``error_message``.

``tero2 status`` prints the field verbatim when non-empty
(``tero2/cli.py:90-91``), so a user resuming from a FAILED run sees the
old failure reason splashed across the status output even though the new
run is healthy.

Fix: clear ``error_message`` on the RUNNING transition inside
``mark_started``.  ``mark_running`` already does this for the resume-from-
PAUSED path; ``mark_started`` just has to match.
"""

from __future__ import annotations

from pathlib import Path

from tero2.checkpoint import CheckpointManager
from tero2.disk_layer import DiskLayer
from tero2.state import AgentState, Phase


def _cm(tmp_path: Path) -> CheckpointManager:
    project = tmp_path / "p"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    return CheckpointManager(disk)


class TestMarkStartedClearsErrorMessage:
    def test_after_failed_error_is_cleared(self, tmp_path: Path) -> None:
        cm = _cm(tmp_path)
        # Drive a FAILED state on disk with a distinctive error.
        state = cm.mark_started(str(tmp_path / "plan.md"))
        state = cm.mark_failed(state, "builder exploded with anguish")
        assert state.phase == Phase.FAILED
        assert state.error_message == "builder exploded with anguish"

        # Now start fresh — error_message must be cleared.
        new = cm.mark_started(str(tmp_path / "plan.md"))
        assert new.phase == Phase.RUNNING
        assert new.error_message == "", (
            f"stale error text survived into the new RUNNING state: "
            f"{new.error_message!r}"
        )

    def test_after_paused_reason_is_cleared(self, tmp_path: Path) -> None:
        cm = _cm(tmp_path)
        state = cm.mark_started(str(tmp_path / "plan.md"))
        state = cm.mark_paused(state, "paused via tui")
        assert state.error_message == "paused via tui"

        new = cm.mark_started(str(tmp_path / "plan.md"))
        assert new.error_message == "", (
            "pause reason must not persist into a fresh RUNNING state"
        )

    def test_fresh_idle_start_has_empty_error(self, tmp_path: Path) -> None:
        """Regression: IDLE → RUNNING has always been the happy path. It must
        continue to start with an empty error_message."""
        cm = _cm(tmp_path)
        state = cm.mark_started(str(tmp_path / "plan.md"))
        assert state.phase == Phase.RUNNING
        assert state.error_message == ""


class TestOtherStateSurvivesMarkStarted:
    """Regression: mark_started's whole point is to preserve accumulated
    context across restarts. bug 111's fix touches ONLY error_message."""

    def test_counters_and_task_id_survive_failed_restart(
        self, tmp_path: Path
    ) -> None:
        cm = _cm(tmp_path)
        state = cm.mark_started(str(tmp_path / "plan.md"))
        state.current_task = "T07"
        state.current_task_index = 6
        state.retry_count = 2
        cm.save(state)
        cm.mark_failed(cm.restore(), "boom")

        restarted = cm.mark_started(str(tmp_path / "plan.md"))
        assert restarted.current_task == "T07", "task id must survive restart"
        assert restarted.current_task_index == 6
        assert restarted.retry_count == 2
        assert restarted.error_message == ""
