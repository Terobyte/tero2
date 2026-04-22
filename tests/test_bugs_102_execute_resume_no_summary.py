"""Bug 102: execute_phase fails the slice when a resumed task has no SUMMARY.md.

When a previous run advanced ``current_task_index`` past a task but didn't
manage to write the per-task ``T0X-SUMMARY.md`` file (typical aftermath of
a crash, or of the bug-101 false-negative empty-summary path), the resume
logic in ``run_execute`` would:

    1. See ``task_index < start_index`` → "skipping already-completed"
    2. Check the .sora path for the summary file
    3. Not find it → log warning + set ``all_passed = False``
    4. Continue without actually running the task

Result: the whole slice is marked failed on a resume, even though the
individual tasks could succeed fresh. Observed live in night-loop iter-6
where iter-5's false-negative run left ``current_task_index = 3`` with
no summary files, so iter-6 reported ``0/3 task(s) passed`` in under a
second without invoking the builder at all.

Fix: if no summary is found at the expected path, re-run the task through
the normal execution path instead of silently marking it failed. Tasks
that ACTUALLY completed (summary on disk) are still skipped as before.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tero2.checkpoint import CheckpointManager
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, ReflexionConfig, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.notifier import Notifier
from tero2.phases.context import RunnerContext
from tero2.phases.execute_phase import run_execute
from tero2.players.architect import SlicePlan, Task
from tero2.players.builder import BuilderResult
from tero2.state import AgentState, SoraPhase


def _make_ctx(tmp_path: Path, state: AgentState | None = None) -> RunnerContext:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    disk = DiskLayer(project)
    disk.init()
    config = Config()
    config.telegram = TelegramConfig()
    config.reflexion = ReflexionConfig(max_cycles=1)
    config.roles["builder"] = RoleConfig(provider="fake")
    checkpoint = CheckpointManager(disk)
    notifier = Notifier(TelegramConfig())
    _state = state if state is not None else AgentState(sora_phase=SoraPhase.EXECUTE)
    ctx = RunnerContext(config, disk, checkpoint, notifier, _state, CircuitBreakerRegistry())
    ctx.build_chain = MagicMock(return_value=MagicMock())
    return ctx


def _three_task_plan() -> SlicePlan:
    return SlicePlan(
        slice_id="S01",
        slice_dir="milestones/M001/S01",
        tasks=[
            Task(id="T01", description="first"),
            Task(id="T02", description="second"),
            Task(id="T03", description="third"),
        ],
    )


def _builder_ok(task_id: str) -> BuilderResult:
    return BuilderResult(
        success=True,
        output_file=f"milestones/M001/S01/{task_id}-SUMMARY.md",
        captured_output="done",
        summary=f"# {task_id} Summary\n- ok",
    )


class TestResumeWithNoSummary:
    """When start_index > 0 but SUMMARY.md missing, tasks must re-run."""

    async def test_all_tasks_rerun_when_state_says_done_but_no_summaries(
        self, tmp_path: Path
    ) -> None:
        """Simulates iter-6 crash-recovery: state says index=3, 0 summaries."""
        state = AgentState(sora_phase=SoraPhase.EXECUTE, current_task_index=3)
        ctx = _make_ctx(tmp_path, state)
        plan = _three_task_plan()

        builder_calls: list[str] = []

        def _track(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                tid = kw.get("task_id", "?")
                builder_calls.append(tid)
                return _builder_ok(tid)

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_track):
            result = await run_execute(ctx, plan)

        assert builder_calls == ["T01", "T02", "T03"], (
            f"expected all 3 tasks to re-run, got builder_calls={builder_calls!r}"
        )
        assert result.success, (
            f"expected success after re-running all tasks, got error={result.error!r}"
        )

    async def test_mixed_some_summaries_present(self, tmp_path: Path) -> None:
        """T01 has summary → skip; T02 missing → re-run; T03 in new territory → run."""
        state = AgentState(sora_phase=SoraPhase.EXECUTE, current_task_index=2)
        ctx = _make_ctx(tmp_path, state)
        plan = _three_task_plan()

        # Only T01 has a summary on disk.
        t01_summary = ctx.disk.sora_dir / "milestones/M001/S01/T01-SUMMARY.md"
        t01_summary.parent.mkdir(parents=True, exist_ok=True)
        t01_summary.write_text("# T01 Summary\nreal work")

        builder_calls: list[str] = []

        def _track(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                tid = kw.get("task_id", "?")
                builder_calls.append(tid)
                return _builder_ok(tid)

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_track):
            result = await run_execute(ctx, plan)

        # T01 skipped (summary exists), T02 re-run (no summary), T03 run normally
        assert "T01" not in builder_calls, "T01 with summary must NOT re-run"
        assert "T02" in builder_calls, "T02 without summary must re-run"
        assert "T03" in builder_calls, "T03 in new territory must run"
        assert result.success


class TestResumeRegressionOriginalBehavior:
    """Pre-existing resume behaviour when summaries ARE present must still work."""

    async def test_all_summaries_present_skips_all(self, tmp_path: Path) -> None:
        """Every skipped task has a summary → all skipped, none re-run, slice passes."""
        state = AgentState(sora_phase=SoraPhase.EXECUTE, current_task_index=3)
        ctx = _make_ctx(tmp_path, state)
        plan = _three_task_plan()

        for tid in ("T01", "T02", "T03"):
            p = ctx.disk.sora_dir / f"milestones/M001/S01/{tid}-SUMMARY.md"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"# {tid} Summary\nok")

        builder_calls: list[str] = []

        def _track(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                builder_calls.append(kw.get("task_id", "?"))
                return _builder_ok(kw.get("task_id", "?"))

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_track):
            result = await run_execute(ctx, plan)

        assert builder_calls == [], (
            f"tasks with summaries on disk must not be re-run, got {builder_calls!r}"
        )
        assert result.success

    async def test_fresh_run_from_zero(self, tmp_path: Path) -> None:
        """start_index=0 + no state → every task runs from scratch."""
        ctx = _make_ctx(tmp_path)
        plan = _three_task_plan()

        builder_calls: list[str] = []

        def _track(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                builder_calls.append(kw.get("task_id", "?"))
                return _builder_ok(kw.get("task_id", "?"))

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_track):
            result = await run_execute(ctx, plan)

        assert builder_calls == ["T01", "T02", "T03"]
        assert result.success
