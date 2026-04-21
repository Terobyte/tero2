"""Architect phase — decomposes a Slice into atomic Tasks.

``run_architect(ctx, slice_id)`` instantiates
:class:`~tero2.players.architect.ArchitectPlayer`, runs it, and returns a
:class:`~tero2.phases.context.PhaseResult` whose ``data`` field holds the
parsed :class:`~tero2.players.architect.SlicePlan`.

Architect failure is **fatal for the current Slice** — the runner should
re-invoke Architect (with a retry) rather than proceeding to Execute with
no plan.
"""

from __future__ import annotations

import logging

from tero2.phases.context import PhaseResult, RunnerContext
from tero2.players.architect import ArchitectPlayer

log = logging.getLogger(__name__)


async def run_architect(
    ctx: RunnerContext,
    slice_id: str = "S01",
) -> PhaseResult:
    """Decompose *slice_id* into atomic Tasks and write ``{slice_id}-PLAN.md``.

    Reads:
        - ``strategic/STRATEGY.md``
        - ``{milestone_path}/CONTEXT_MAP.md``
        - ``{milestone_path}/PLAN.md`` → fallback ``{milestone_path}/ROADMAP.md``

    Writes:
        - ``{milestone_path}/{slice_id}/{slice_id}-PLAN.md``

    Args:
        ctx:      Shared runner context.  Requires an ``"architect"`` role.
        slice_id: Slice to decompose (e.g. ``"S01"``).

    Returns:
        :class:`~tero2.phases.context.PhaseResult` with ``data`` set to the
        parsed :class:`~tero2.players.architect.SlicePlan` on success, or
        ``success=False`` (fatal) on failure.
    """
    if ctx.shutdown_event and ctx.shutdown_event.is_set():
        log.info("architect: shutdown requested — aborting")
        return PhaseResult(success=False, error="shutdown requested")

    try:
        chain = ctx.build_chain("architect")
    except Exception as exc:
        log.error("architect: cannot build chain: %s", exc)
        return PhaseResult(success=False, error=str(exc))

    player = ArchitectPlayer(
        chain,
        ctx.disk,
        working_dir=str(ctx.disk.project_path),
    )
    persona_prompt = ctx.personas.load_or_default("architect").system_prompt

    result = await player.run(
        slice_id=slice_id,
        milestone_path=ctx.milestone_path,
        persona_prompt=persona_prompt,
    )

    if result.success:
        log.info(
            "architect complete — %d tasks in slice %s",
            result.task_count,
            slice_id,
        )
        # Update state to track which slice we just planned
        ctx.state.current_slice = slice_id
        ctx.checkpoint.save(ctx.state)
        return PhaseResult(
            success=True,
            error=result.error,
            data={"slice_plan": result.slice_plan},
        )
    else:
        log.error("architect failed (fatal for slice %s): %s", slice_id, result.error)
        return PhaseResult(
            success=False,
            error=result.error,
            data=None,
        )
