"""Tests for bugs 56, 57, 66, 70.

Bug 56 — AgentState.from_json uses blanket except; a bad enum field silently
         resets ALL fields to defaults (2 tests).
Bug 57 — runner.py declares its own _PHASE_ORDER duplicating the ordering
         already encoded in state.py's SORA_PHASE_ORDER (1 test).
Bug 66 — execute_phase skips crash-recovery tasks into `completed` without
         checking that the summary file actually exists on disk (1 test).
Bug 70 — cli.cmd_run resolves the plan path but never checks it stays inside
         project_path, allowing path traversal (1 test).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 56: per-field try/except in from_json ─────────────────────────────


class TestBug56FromJsonPerFieldExcept:
    """Blanket except in from_json resets ALL fields when one enum is bad."""

    def test_invalid_phase_preserves_valid_fields(self):
        """A garbage phase value must not wipe retry_count or other good fields."""
        from tero2.state import AgentState, Phase

        bad = json.dumps({
            "phase": "totally_invalid_phase",
            "retry_count": 7,
            "steps_in_task": 3,
            "current_task": "T02",
        })
        state = AgentState.from_json(bad)

        assert state.retry_count == 7, (
            f"retry_count should survive bad phase, got {state.retry_count}. "
            "Blanket except returns cls() which resets retry_count to 0."
        )
        assert state.steps_in_task == 3, (
            f"steps_in_task should survive bad phase, got {state.steps_in_task}"
        )
        assert state.phase == Phase.IDLE, "bad phase must fall back to IDLE"

    def test_invalid_sora_phase_preserves_valid_fields(self):
        """A garbage sora_phase value must not wipe retry_count etc."""
        from tero2.state import AgentState, SoraPhase

        bad = json.dumps({
            "sora_phase": "not_a_real_phase",
            "retry_count": 5,
            "current_task": "T01",
            "steps_in_task": 11,
        })
        state = AgentState.from_json(bad)

        assert state.retry_count == 5, (
            f"retry_count should survive bad sora_phase, got {state.retry_count}"
        )
        assert state.steps_in_task == 11
        assert state.sora_phase == SoraPhase.NONE, "bad sora_phase must fall back to NONE"


# ── Bug 57: _PHASE_ORDER imported from state.py, not redeclared ───────────


class TestBug57PhaseOrderSingleSource:
    """runner._PHASE_ORDER must be the same object as state.SORA_PHASE_ORDER."""

    def test_runner_phase_order_is_state_phase_order(self):
        """_PHASE_ORDER in runner must not be a locally-defined duplicate list."""
        import tero2.runner as runner_mod
        from tero2.state import SORA_PHASE_ORDER

        assert runner_mod._PHASE_ORDER is SORA_PHASE_ORDER, (
            "runner._PHASE_ORDER is a separate list, not an import from state.py. "
            "Adding a phase to SORA_PHASE_ORDER would not update runner's copy."
        )


# ── Bug 66: exists() check before adding to completed ────────────────────


class TestBug66CompletedExistsCheck:
    """Crash-recovery must NOT add a task to completed if SUMMARY.md is missing."""

    async def test_missing_summary_not_in_completed(self, tmp_path):
        from tero2.checkpoint import CheckpointManager
        from tero2.circuit_breaker import CircuitBreakerRegistry
        from tero2.config import Config, ReflexionConfig, RoleConfig, TelegramConfig
        from tero2.disk_layer import DiskLayer
        from tero2.notifier import Notifier
        from tero2.phases.context import RunnerContext
        from tero2.phases.execute_phase import run_execute
        from tero2.players.architect import SlicePlan, Task
        from tero2.state import AgentState

        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        config = Config()
        config.telegram = TelegramConfig()
        config.reflexion = ReflexionConfig(max_cycles=0)
        config.roles["builder"] = RoleConfig(provider="fake")
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(TelegramConfig())

        from tero2.state import SoraPhase

        # Crash recovery: current_task_index=1 means T01 is "done", T02 needs to run.
        # But the T01-SUMMARY.md was never written to disk (process crashed before it).
        # sora_phase=ARCHITECT so the ARCHITECT→EXECUTE transition is valid when T02 runs.
        state = AgentState(
            current_task_index=1,
            task_in_progress=False,
            sora_phase=SoraPhase.ARCHITECT,
        )
        ctx = RunnerContext(
            config, disk, checkpoint, notifier, state, CircuitBreakerRegistry()
        )
        ctx.build_chain = MagicMock(return_value=MagicMock())

        from tero2.players.builder import BuilderResult

        tasks = [
            Task(id="T01", description="already done (no summary on disk)"),
            Task(id="T02", description="runs now"),
        ]
        slice_plan = SlicePlan(
            slice_id="S01",
            slice_dir="milestones/M001/S01",
            tasks=tasks,
        )

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(
                return_value=BuilderResult(
                    success=True,
                    output_file="milestones/M001/S01/T02-SUMMARY.md",
                    captured_output="done",
                )
            )
            MockB.return_value = inst
            result = await run_execute(ctx, slice_plan)

        completed = result.data["completed"]
        assert "T01" not in completed, (
            f"T01 SUMMARY.md does not exist on disk; must not appear in completed. "
            f"Got completed={completed}"
        )
        assert "T02" in completed, "T02 ran and passed — must be in completed"


# ── Bug 70: resolve().is_relative_to(project_path) guard ─────────────────


class TestBug70PlanFilePathTraversal:
    """cmd_run must reject plan paths that escape the project directory."""

    def test_traversal_plan_path_exits_with_error(self, tmp_path, capsys):
        """--plan ../../etc/passwd must be rejected before any file access."""
        from tero2.cli import cmd_run

        project = tmp_path / "project"
        project.mkdir()

        # Absolute path that is outside project_path
        outside = tmp_path / "outside.md"
        outside.write_text("evil plan", encoding="utf-8")

        args = argparse.Namespace(
            project_path=str(project),
            plan=str(outside),
            config=None,
            verbose=False,
        )

        with pytest.raises(SystemExit) as exc_info:
            cmd_run(args)

        assert exc_info.value.code != 0, "Should exit with non-zero code"
        captured = capsys.readouterr()
        assert "project directory" in captured.out, (
            f"Expected path-traversal error message, got: {captured.out!r}"
        )
