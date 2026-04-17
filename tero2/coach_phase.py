"""Coach phase — episodic strategic advisor.

``run_coach(ctx, trigger)`` instantiates
:class:`~tero2.players.coach.CoachPlayer`, runs it, and returns a
:class:`~tero2.phases.context.PhaseResult`.

Coach is **non-fatal**: the SORA pipeline continues even when Coach fails
(previous strategic documents remain on disk from the last successful run).
"""

from __future__ import annotations

import logging

from tero2.phases.context import PhaseResult, RunnerContext
from tero2.players.coach import CoachPlayer
from tero2.triggers import CoachTrigger

log = logging.getLogger(__name__)


async def run_coach(
    ctx: RunnerContext,
    trigger: CoachTrigger = CoachTrigger.FIRST_RUN,
) -> PhaseResult:
    """Run the Coach strategic advisor for the given *trigger*.

    Reads all relevant project context from disk (roadmap, context map,
    task summaries, decisions, event journal, metrics, steer) and produces
    up to four strategic documents:
    ``strategic/STRATEGY.md``, ``strategic/TASK_QUEUE.md``,
    ``strategic/RISK.md``, ``strategic/CONTEXT_HINTS.md``.

    Args:
        ctx:     Shared runner context.  Requires a ``"coach"`` role in config.
        trigger: Reason Coach was invoked (e.g.
                 :attr:`~tero2.triggers.CoachTrigger.FIRST_RUN`,
                 :attr:`~tero2.triggers.CoachTrigger.END_OF_SLICE`).

    Returns:
        :class:`~tero2.phases.context.PhaseResult` — ``success=False`` is
        non-fatal; existing strategic documents remain in place.
    """
    if ctx.shutdown_event and ctx.shutdown_event.is_set():
        log.info("coach: shutdown requested — aborting")
        return PhaseResult(success=False, error="shutdown requested")

    try:
        chain = ctx.build_chain("coach")
    except Exception as exc:
        log.warning("coach: cannot build chain: %s — skipping", exc)
        return PhaseResult(success=False, error=str(exc))

    player = CoachPlayer(
        chain,
        ctx.disk,
        working_dir=str(ctx.disk.project_path),
    )
    persona_prompt = ctx.personas.load_or_default("coach").system_prompt

    # Determine the current slice from state (default to S01 on first run)
    slice_id = ctx.state.current_slice or "S01"

    result = await player.run(
        trigger=trigger.value,
        persona_prompt=persona_prompt,
        slice_id=slice_id,
        milestone_path=ctx.milestone_path,
    )

    if result.success:
        log.info(
            "coach complete (trigger=%s) — strategy docs written",
            trigger.value,
        )
    else:
        log.warning("coach failed (non-fatal, trigger=%s): %s", trigger.value, result.error)

    return PhaseResult(
        success=result.success,
        error=result.error,
        data={
            "strategy": result.strategy,
            "task_queue": result.task_queue,
            "risk": result.risk,
            "context_hints": result.context_hints,
        }
        if result.success
        else None,
    )
