"""Scout phase — fast codebase reconnaissance.

``run_scout(ctx)`` instantiates :class:`~tero2.players.scout.ScoutPlayer`,
runs it, and returns a :class:`~tero2.phases.context.PhaseResult`.

Scout is **non-fatal**: if the Scout player fails or is skipped (project
too small), the phase returns ``PhaseResult(success=False)`` and the SORA
pipeline continues with reduced context quality.
"""

from __future__ import annotations

import logging

from tero2.phases.context import PhaseResult, RunnerContext
from tero2.players.scout import ScoutPlayer

log = logging.getLogger(__name__)


async def run_scout(ctx: RunnerContext) -> PhaseResult:
    """Run Scout reconnaissance and write ``CONTEXT_MAP.md``.

    Skips scouting when the project has fewer than
    ``config.context.skip_scout_if_files_lt`` tracked files.

    Args:
        ctx: Shared runner context.  Requires a ``"scout"`` role in config.

    Returns:
        :class:`~tero2.phases.context.PhaseResult` — ``success=False`` is
        non-fatal; the SORA pipeline will proceed without a context map.
    """
    if ctx.shutdown_event and ctx.shutdown_event.is_set():
        log.info("scout: shutdown requested — aborting")
        return PhaseResult(success=False, error="shutdown requested")

    working_dir = str(ctx.disk.project_path)
    skip_threshold = ctx.config.context.skip_scout_if_files_lt

    if ScoutPlayer.should_skip(working_dir, skip_threshold):
        log.info(
            "scout skipped — project has < %d files (threshold)",
            skip_threshold,
        )
        return PhaseResult(
            success=False,
            error=f"skipped: fewer than {skip_threshold} files",
        )

    try:
        chain = ctx.build_chain("scout")
    except Exception as exc:
        log.warning("scout: cannot build chain: %s — skipping", exc)
        return PhaseResult(success=False, error=str(exc))

    player = ScoutPlayer(chain, ctx.disk, working_dir=working_dir)
    persona_prompt = ctx.personas.load_or_default("scout").system_prompt

    result = await player.run(
        milestone_path=ctx.milestone_path,
        persona_prompt=persona_prompt,
    )

    if result.success:
        log.info(
            "scout complete — CONTEXT_MAP.md written (%d files mapped)",
            result.file_count,
        )
    else:
        log.warning("scout failed (non-fatal): %s", result.error)

    return PhaseResult(
        success=result.success,
        error=result.error,
        data=result.context_map if result.success else None,
    )
