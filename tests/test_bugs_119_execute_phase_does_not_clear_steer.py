"""Bug 119: ``execute_phase`` reads ``.sora/human/STEER.md`` at every task
boundary and every attempt, but never clears it, so a one-shot operator
directive (or the auto-generated ``[stuck-recovery option-N …]`` text from
bug 107) keeps leaking into every subsequent task's ``context_hints``.

Flow on the broken path::

    task boundary  →  read_steer()  →  context_hints = steer_content
    attempt 0      →  read_steer()  →  effective_hints = steer_content
    attempt 1      →  read_steer()  →  effective_hints = steer_content
    NEXT TASK
    task boundary  →  read_steer()  →  context_hints = steer_content  (!)
    attempt 0      →  read_steer()  →  effective_hints = steer_content (!)
    ...

The "option-5 manual control requested" string from the bug 107 auto-
write path is particularly harmful: it was meant to flag "pause and
wait" for the runner/notifier, not to be re-fed to the builder as a
directive every turn.

Contract after the fix: **once ``execute_phase`` has applied a STEER.md
directive as ``context_hints`` for a task, it is cleared**. A new
operator-written steer between tasks takes effect on the next boundary.
Mirrors the consume-and-clear pattern already in place for Coach
(bug 116).

Test strategy: drive the specific consumption site directly by running
a minimal task through ``run_execute`` and asserting STEER.md is empty
once the phase returns. Verifies behaviour end-to-end without needing
full Runner scaffolding.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_steer_cleared_after_execute_phase_applies_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The observable contract: after ``run_execute`` consumes a STEER
    directive, the file is empty for the next phase."""
    from tero2.disk_layer import DiskLayer
    from tero2.phases.execute_phase import run_execute
    from tero2.players.architect import SlicePlan, Task

    disk = DiskLayer(tmp_path)
    disk.init()
    disk.write_steer("focus on edge cases")
    assert disk.read_steer() == "focus on edge cases"

    ctx = MagicMock()
    ctx.disk = disk
    ctx.state = MagicMock()
    ctx.state.current_task = ""
    ctx.state.current_task_index = 0
    ctx.state.steps_in_task = 0
    ctx.state.retry_count = 0
    ctx.state.tool_repeat_count = 0
    ctx.state.last_tool_hash = ""
    ctx.state.task_in_progress = False
    ctx.state.escalation_level = 0
    ctx.checkpoint = MagicMock()
    ctx.checkpoint.max_steps_per_task = 100
    ctx.checkpoint.save = MagicMock()
    ctx.checkpoint.mark_paused = MagicMock(side_effect=lambda s, e: s)
    ctx.config = MagicMock()
    ctx.config.roles = {"builder": MagicMock(), "verifier": None}
    ctx.config.stuck_detection = MagicMock(
        max_steps_per_task=100, max_retries=3, tool_repeat_threshold=2
    )
    ctx.config.escalation = MagicMock(
        diversification_temp_delta=0.3,
        diversification_max_steps=2,
        backtrack_to_last_checkpoint=True,
    )
    ctx.config.reflexion = MagicMock(max_cycles=0)
    ctx.config.verifier = MagicMock(commands=[])
    ctx.notifier = AsyncMock()
    ctx.shutdown_event = None
    ctx.command_queue = None
    ctx.dispatcher = None
    ctx.skip_requested = False
    ctx.escalation_level = 0
    ctx.div_steps = 0
    ctx.escalation_history = []
    ctx.milestone_path = "milestones/M001"
    ctx.personas = MagicMock()
    ctx.personas.load_or_default = MagicMock(
        return_value=MagicMock(system_prompt="", name="builder")
    )
    ctx.build_chain = MagicMock(return_value=MagicMock())
    ctx.run_agent = AsyncMock(return_value=(True, "done"))

    # Stub BuilderPlayer to short-circuit the agent stack: write a SUMMARY
    # and succeed immediately.
    async def fake_builder_run(self, **kwargs):
        summary_rel = (
            f"{kwargs.get('slice_id', 'S01')}/"
            f"{kwargs['task_id']}-SUMMARY.md"
        )
        full_rel = f"{kwargs.get('milestone_path', 'milestones/M001')}/{summary_rel}"
        disk.write_file(full_rel, f"# {kwargs['task_id']}\ndone\n")
        from tero2.players.builder import BuilderResult
        return BuilderResult(
            success=True,
            output_file=full_rel,
            captured_output="done",
            summary="done",
            task_id=kwargs["task_id"],
        )

    from tero2.players import builder as builder_mod
    monkeypatch.setattr(builder_mod.BuilderPlayer, "run", fake_builder_run)

    # Stub VerifierPlayer to always pass.
    async def fake_verifier_run(self, **kwargs):
        from tero2.players.verifier import VerifierResult, Verdict
        return VerifierResult(success=True, verdict=Verdict.PASS, captured_output="PASS")

    from tero2.players import verifier as verifier_mod
    monkeypatch.setattr(verifier_mod.VerifierPlayer, "run", fake_verifier_run)

    slice_plan = SlicePlan(
        slice_id="S01",
        slice_dir="milestones/M001/S01",
        tasks=[Task(id="T01", description="do stuff", must_haves=["pass"])],
    )

    ctx.state.current_task_index = 0
    result = await run_execute(ctx, slice_plan)

    assert result.success, f"expected success, error={result.error!r}"
    assert disk.read_steer() == "", (
        "bug 119 contract: after execute_phase has applied STEER.md as "
        "context_hints, the file must be cleared so the next task does "
        "not re-consume the same directive. the bug 107 'option-5 manual "
        "control requested' text in particular is worst-case — it is an "
        "auto-write meant as a pause flag, not as a persistent builder "
        "hint. got STEER: "
        f"{disk.read_steer()!r}"
    )


@pytest.mark.asyncio
async def test_empty_steer_stays_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: when STEER is empty to start with, execute_phase
    must not error out trying to clear a non-existent file."""
    from tero2.disk_layer import DiskLayer
    from tero2.phases.execute_phase import run_execute
    from tero2.players.architect import SlicePlan, Task

    disk = DiskLayer(tmp_path)
    disk.init()
    assert disk.read_steer() == ""

    ctx = MagicMock()
    ctx.disk = disk
    ctx.state = MagicMock(
        current_task="", current_task_index=0, steps_in_task=0,
        retry_count=0, tool_repeat_count=0, last_tool_hash="",
        task_in_progress=False, escalation_level=0,
    )
    ctx.checkpoint = MagicMock(max_steps_per_task=100, save=MagicMock(),
                                mark_paused=MagicMock(side_effect=lambda s, e: s))
    ctx.config = MagicMock()
    ctx.config.roles = {"builder": MagicMock(), "verifier": None}
    ctx.config.stuck_detection = MagicMock(
        max_steps_per_task=100, max_retries=3, tool_repeat_threshold=2
    )
    ctx.config.escalation = MagicMock(
        diversification_temp_delta=0.3,
        diversification_max_steps=2,
        backtrack_to_last_checkpoint=True,
    )
    ctx.config.reflexion = MagicMock(max_cycles=0)
    ctx.config.verifier = MagicMock(commands=[])
    ctx.notifier = AsyncMock()
    ctx.shutdown_event = None
    ctx.command_queue = None
    ctx.dispatcher = None
    ctx.skip_requested = False
    ctx.escalation_level = 0
    ctx.div_steps = 0
    ctx.escalation_history = []
    ctx.milestone_path = "milestones/M001"
    ctx.personas = MagicMock()
    ctx.personas.load_or_default = MagicMock(
        return_value=MagicMock(system_prompt="", name="builder")
    )
    ctx.build_chain = MagicMock(return_value=MagicMock())
    ctx.run_agent = AsyncMock(return_value=(True, "done"))

    async def fake_builder_run(self, **kwargs):
        full_rel = f"{kwargs.get('milestone_path', 'milestones/M001')}/{kwargs.get('slice_id', 'S01')}/{kwargs['task_id']}-SUMMARY.md"
        disk.write_file(full_rel, f"# {kwargs['task_id']}\ndone\n")
        from tero2.players.builder import BuilderResult
        return BuilderResult(success=True, output_file=full_rel, captured_output="done",
                              summary="done", task_id=kwargs["task_id"])

    async def fake_verifier_run(self, **kwargs):
        from tero2.players.verifier import VerifierResult, Verdict
        return VerifierResult(success=True, verdict=Verdict.PASS, captured_output="PASS")

    from tero2.players import builder as builder_mod, verifier as verifier_mod
    monkeypatch.setattr(builder_mod.BuilderPlayer, "run", fake_builder_run)
    monkeypatch.setattr(verifier_mod.VerifierPlayer, "run", fake_verifier_run)

    slice_plan = SlicePlan(
        slice_id="S01",
        slice_dir="milestones/M001/S01",
        tasks=[Task(id="T01", description="do stuff", must_haves=["pass"])],
    )

    ctx.state.current_task_index = 0
    result = await run_execute(ctx, slice_plan)
    assert result.success
    assert disk.read_steer() == ""
