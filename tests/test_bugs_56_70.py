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
    """Crash-recovery must NOT add a task to completed without confirming work.

    Bug 66 forbids phantom ``completed`` entries: if a task is "skipped" because
    start_index advanced past it, we MUST either (a) see a real ``*-SUMMARY.md``
    on disk before trusting the skip, or (b) actually re-run the task through
    the builder. What we MUST NOT do is add a task to ``completed`` with a
    fabricated or inherited path when neither condition holds.

    Post-bug-102 contract: path (b) now re-runs instead of hard-failing. The
    spirit of bug 66 — no phantom completions — is preserved because the only
    way ``completed[task]`` gets populated is from a real builder invocation.
    """

    async def test_skipped_task_without_summary_reruns_through_builder(
        self, tmp_path
    ):
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
            Task(id="T01", description="state says done but no summary on disk"),
            Task(id="T02", description="runs now"),
        ]
        slice_plan = SlicePlan(
            slice_id="S01",
            slice_dir="milestones/M001/S01",
            tasks=tasks,
        )

        calls: list[str] = []

        def _builder_factory(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                tid = kw.get("task_id", "?")
                calls.append(tid)
                return BuilderResult(
                    success=True,
                    output_file=f"milestones/M001/S01/{tid}-SUMMARY.md",
                    captured_output="done",
                    summary=f"# {tid}\nok",
                )

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_builder_factory):
            result = await run_execute(ctx, slice_plan)

        # Spirit of bug 66: every entry in `completed` must come from either a
        # real summary on disk (skip path) or a real builder invocation (re-run
        # path). Since T01's summary is missing, the only legitimate way T01
        # can end up in `completed` is by being actually re-run.
        assert "T01" in calls, (
            "bug 66 invariant: T01 had no summary on disk, so it must be re-run "
            f"through the builder before being marked complete. calls={calls!r}"
        )
        completed = result.data["completed"]
        assert completed["T01"] == "milestones/M001/S01/T01-SUMMARY.md", (
            "T01's completed path must come from its own builder run — never "
            f"inherited or fabricated. got: {completed!r}"
        )
        assert completed["T02"] == "milestones/M001/S01/T02-SUMMARY.md"
        assert result.success, (
            "after re-running T01 and running T02 the slice must succeed"
        )

    async def test_completed_entry_requires_real_source(self, tmp_path):
        """Direct regression: no task is ever written to `completed` based on
        an inferred/fabricated path when neither the on-disk summary exists nor
        a builder result has been produced for it. Enforced by construction —
        the only writers to `completed` are (1) the verified-summary skip path
        and (2) the post-builder success path.
        """
        import inspect
        import re

        from tero2.phases import execute_phase as ep_mod

        src = inspect.getsource(ep_mod.run_execute)
        # Every occurrence of `completed[...] = ...` in run_execute must be
        # gated by either an exists()-verified summary or a builder result.
        writes = re.findall(r"completed\[[^\]]+\]\s*=\s*[^\n]+", src)
        assert writes, "sanity: run_execute must write to `completed` somewhere"
        for w in writes:
            # The only acceptable RHS shapes are:
            #   - summary_path  (gated by exists() check above it)
            #   - builder_result.output_file  (gated by builder success)
            assert "summary_path" in w or "builder_result.output_file" in w, (
                f"bug 66 regression risk: suspicious `completed` write: {w!r}"
            )


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
