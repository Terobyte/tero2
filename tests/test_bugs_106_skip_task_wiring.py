"""Bug 106: TUI ``k`` (Skip) binding posted Command("skip_task") but nothing
in the pipeline consumed it.

User flow that was broken:
    1. User presses 'k' during an execute-phase task that's spinning on a
       flaky attempt they don't want to wait out.
    2. TUI emits ``Command("skip_task", source="tui")``.
    3. Runner's ``_drain_commands`` is called only at phase boundaries, so
       while ``execute_phase.run_execute`` is grinding through reflexion
       retries for T02, the command sits in the queue unread.
    4. By the time _drain_commands fires (after the slice ends), the task
       has already completed. The command is then logged as "unsupported"
       (bug 104) and discarded.

Fix has two parts:
    * Runner's ``_drain_commands`` sets ``ctx.skip_requested = True`` when
      it sees a ``skip_task`` command (covers between-phase skips).
    * ``execute_phase._drain_skip_commands`` is called at every attempt
      boundary, sifting ``skip_task`` commands out of the queue mid-slice
      while re-queueing everything else (so stop/pause/switch_provider
      still reach the runner's phase-boundary drain intact).

At the attempt boundary, ``execute_phase`` checks ``ctx.skip_requested``,
writes a placeholder ``*-SUMMARY.md`` (so bug 66's invariant holds), adds
the task to ``completed`` with a soft-pass, and breaks out of the
attempt loop to advance to the next task.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.checkpoint import CheckpointManager
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, ReflexionConfig, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.events import Command
from tero2.notifier import Notifier
from tero2.phases.context import RunnerContext
from tero2.phases.execute_phase import _drain_skip_commands, run_execute
from tero2.players.architect import SlicePlan, Task
from tero2.players.builder import BuilderResult
from tero2.runner import Runner
from tero2.state import AgentState, Phase, SoraPhase


def _ctx(tmp_path: Path) -> RunnerContext:
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    config = Config()
    config.telegram = TelegramConfig()
    config.reflexion = ReflexionConfig(max_cycles=3)
    config.roles["builder"] = RoleConfig(provider="fake")
    cq: asyncio.Queue = asyncio.Queue()
    ctx = RunnerContext(
        config,
        disk,
        CheckpointManager(disk),
        Notifier(TelegramConfig()),
        AgentState(sora_phase=SoraPhase.EXECUTE),
        CircuitBreakerRegistry(),
        command_queue=cq,
    )
    ctx.build_chain = MagicMock(return_value=MagicMock())
    return ctx


class TestDrainSkipCommandsHelper:
    """The helper must extract skip_task commands without touching others."""

    def test_sets_flag_on_skip_task(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        ctx.command_queue.put_nowait(Command("skip_task", source="tui"))

        assert not ctx.skip_requested
        _drain_skip_commands(ctx)
        assert ctx.skip_requested is True
        assert ctx.command_queue.empty(), "skip_task must be consumed"

    def test_other_commands_re_queued(self, tmp_path: Path) -> None:
        """A pause command posted mid-execution must survive the drain so
        the next phase-boundary _drain_commands picks it up intact."""
        ctx = _ctx(tmp_path)
        ctx.command_queue.put_nowait(Command("pause", source="tui"))
        ctx.command_queue.put_nowait(
            Command("switch_provider", data={"role": "x"}, source="tui")
        )

        _drain_skip_commands(ctx)

        assert ctx.skip_requested is False
        # both commands must still be there
        kinds = []
        while not ctx.command_queue.empty():
            kinds.append(ctx.command_queue.get_nowait().kind)
        assert sorted(kinds) == ["pause", "switch_provider"], (
            f"non-skip commands must be re-queued. got: {kinds!r}"
        )

    def test_mixed_skip_and_others(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        ctx.command_queue.put_nowait(Command("pause", source="tui"))
        ctx.command_queue.put_nowait(Command("skip_task", source="tui"))
        ctx.command_queue.put_nowait(Command("switch_provider", source="tui"))

        _drain_skip_commands(ctx)

        assert ctx.skip_requested is True
        kinds = []
        while not ctx.command_queue.empty():
            kinds.append(ctx.command_queue.get_nowait().kind)
        assert "skip_task" not in kinds, "skip_task must be consumed"
        assert sorted(kinds) == ["pause", "switch_provider"]

    def test_no_queue_is_noop(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        ctx.command_queue = None
        # must not raise
        _drain_skip_commands(ctx)
        assert ctx.skip_requested is False


class TestRunnerSetsSkipRequested:
    """Bug 106 runner half: _drain_commands must propagate skip_task into
    ctx.skip_requested so execute_phase's attempt loop sees it."""

    async def test_drain_commands_sets_ctx_flag(self, tmp_path: Path) -> None:
        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        (project / "plan.md").write_text("# plan")
        config = Config()
        config.telegram = TelegramConfig()
        cq: asyncio.Queue[Command] = asyncio.Queue()
        runner = Runner(project, project / "plan.md", config=config, command_queue=cq)
        runner.notifier = MagicMock(spec=Notifier)
        runner.notifier.notify = AsyncMock()
        # Minimal RunnerContext with the same queue the runner owns.
        runner._ctx = RunnerContext(
            config,
            disk,
            CheckpointManager(disk),
            Notifier(TelegramConfig()),
            AgentState(sora_phase=SoraPhase.EXECUTE),
            CircuitBreakerRegistry(),
            command_queue=cq,
        )
        assert runner._ctx.skip_requested is False

        cq.put_nowait(Command("skip_task", source="tui"))
        await runner._drain_commands(AgentState(phase=Phase.RUNNING))

        assert runner._ctx.skip_requested is True, (
            "runner must set ctx.skip_requested so execute_phase sees it"
        )


class TestExecutePhaseHonoursSkipRequested:
    """End-to-end: skip_requested flag causes the current task to soft-pass
    without further retries, a placeholder summary is written, and the slice
    continues with the next task."""

    async def test_skip_advances_to_next_task(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        plan = SlicePlan(
            slice_id="S01",
            slice_dir="milestones/M001/S01",
            tasks=[
                Task(id="T01", description="flaky — will be skipped"),
                Task(id="T02", description="runs normally"),
            ],
        )

        call_log: list[str] = []

        def _builder(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                tid = kw.get("task_id", "?")
                call_log.append(tid)
                # pretend T01 would fail forever; the skip must short-circuit
                if tid == "T01":
                    # Before the builder runs the attempt, press 'k'.
                    ctx.command_queue.put_nowait(Command("skip_task", source="tui"))
                    return BuilderResult(success=False, error="fake flake")
                return BuilderResult(
                    success=True,
                    output_file=f"milestones/M001/S01/{tid}-SUMMARY.md",
                    captured_output="ok",
                    summary=f"# {tid}\nok",
                )

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_builder):
            result = await run_execute(ctx, plan)

        # T01 ran once (queued skip during its first attempt), then the NEXT
        # attempt boundary saw skip_requested=True and bailed out.
        # T02 ran normally.
        assert "T02" in call_log, "slice must advance past skipped T01"
        assert result.success, (
            f"soft-skip must let the slice succeed, got error={result.error!r}"
        )

        # Placeholder summary must exist so bug 66's exists-check is satisfied
        # on any future resume.
        skip_sum = ctx.disk.sora_dir / "milestones/M001/S01/T01-SUMMARY.md"
        assert skip_sum.is_file(), (
            "skip must write a placeholder SUMMARY.md so resume doesn't re-run"
        )
        assert "skipped via TUI" in skip_sum.read_text()

    async def test_skip_flag_cleared_after_consumption(self, tmp_path: Path) -> None:
        """The flag is single-shot: after honouring it, execute_phase clears
        it so the NEXT task is not also auto-skipped."""
        ctx = _ctx(tmp_path)
        ctx.skip_requested = True

        plan = SlicePlan(
            slice_id="S01",
            slice_dir="milestones/M001/S01",
            tasks=[
                Task(id="T01", description="skipped up-front"),
                Task(id="T02", description="runs normally, flag must be cleared"),
            ],
        )

        call_log: list[str] = []

        def _builder(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                call_log.append(kw.get("task_id", "?"))
                tid = kw.get("task_id", "?")
                return BuilderResult(
                    success=True,
                    output_file=f"milestones/M001/S01/{tid}-SUMMARY.md",
                    captured_output="ok",
                    summary=f"# {tid}\nok",
                )

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_builder):
            result = await run_execute(ctx, plan)

        # T01 is skipped before its first attempt (flag=True at entry) → no
        # builder call for T01. T02 runs (flag cleared).
        assert call_log == ["T02"], (
            f"T01 pre-flagged skip → no builder call; T02 normal. got: {call_log!r}"
        )
        assert result.success
        assert ctx.skip_requested is False, (
            "skip_requested must be cleared after honouring it"
        )


class TestRegressionNoSkipWhenFlagFalse:
    """If the flag is never set, execute_phase behaves exactly as before."""

    async def test_normal_run_unaffected(self, tmp_path: Path) -> None:
        ctx = _ctx(tmp_path)
        assert ctx.skip_requested is False

        plan = SlicePlan(
            slice_id="S01",
            slice_dir="milestones/M001/S01",
            tasks=[Task(id="T01", description="normal")],
        )
        summary_rel = "milestones/M001/S01/T01-SUMMARY.md"

        def _builder(*args, **kwargs):
            inst = MagicMock()

            async def _run(**kw):
                # Real builder writes the summary file — mirror that so the
                # resume-skip branch in bug 102 doesn't trigger.
                abs_path = ctx.disk.sora_dir / summary_rel
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_text("# T01\nreal builder output", encoding="utf-8")
                return BuilderResult(
                    success=True,
                    output_file=summary_rel,
                    captured_output="ok",
                    summary="# T01\nreal builder output",
                )

            inst.run = AsyncMock(side_effect=_run)
            return inst

        with patch("tero2.phases.execute_phase.BuilderPlayer", side_effect=_builder):
            result = await run_execute(ctx, plan)

        assert result.success
        # The SUMMARY must come from the real builder, never from the
        # skip-placeholder branch.
        summary_text = (ctx.disk.sora_dir / summary_rel).read_text()
        assert "skipped via TUI" not in summary_text
        assert "real builder output" in summary_text
