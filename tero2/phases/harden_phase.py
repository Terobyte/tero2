"""Plan Hardening phase — Reviewer convergence loop.

``run_harden(ctx)`` drives the hardening cycle:

    Round 1 … N:
        Reviewer(find-issues) → parse verdict
        if CRITICAL → Reviewer(apply-fixes) → update plan
        if NO ISSUES / COSMETIC-only → stop (converged)

Intermediate plan versions are written to
``{milestone_path}/plan_v{n}.md`` for debugging.
The final hardened plan is written to ``{milestone_path}/PLAN.md``.

Malformed output handling:
    - Single malformed response → treat as CRITICAL (apply fixes).
    - 2 consecutive malformed responses → treat as NO ISSUES FOUND (stop).
"""

from __future__ import annotations

import logging
import re

from tero2.context_assembly import ContextAssembler
from tero2.phases.context import PhaseResult, RunnerContext
from tero2.players.reviewer import ReviewerPlayer

log = logging.getLogger(__name__)

# ── Verdict patterns ──────────────────────────────────────────────────────

_CRITICAL_RE = re.compile(r"\bCRITICAL\b", re.IGNORECASE)
_COSMETIC_RE = re.compile(r"\bCOSMETIC\b", re.IGNORECASE)
_NO_ISSUES_RE = re.compile(r"\bNO\s+ISSUES?\s+FOUND\b", re.IGNORECASE)


# ── Public entry point ────────────────────────────────────────────────────


async def run_harden(ctx: RunnerContext) -> PhaseResult:
    """Run the Plan Hardening convergence loop.

    Reads the current plan from ``{milestone_path}/PLAN.md`` (falls back to
    the ``state.plan_file`` if PLAN.md is absent), then iterates through up
    to ``config.plan_hardening.max_rounds`` Reviewer cycles.

    Args:
        ctx: Shared runner context.  Requires a ``"reviewer"`` role in config.

    Returns:
        :class:`~tero2.phases.context.PhaseResult` with ``data`` set to the
        final hardened plan string on success.
    """
    plan = ctx.disk.read_file(f"{ctx.milestone_path}/PLAN.md")
    if not plan and ctx.state.plan_file:
        plan = ctx.disk.read_plan(ctx.state.plan_file)
    if not plan:
        return PhaseResult(success=False, error="no plan found to harden")

    try:
        chain = ctx.build_chain("reviewer")
    except Exception as exc:
        log.error("harden: cannot build reviewer chain: %s", exc)
        return PhaseResult(success=False, error=f"reviewer role not configured: {exc}")

    player = ReviewerPlayer(chain, ctx.disk)
    assembler = ContextAssembler(
        ctx.config,
        system_prompts={
            "reviewer_review": ctx.personas.load_or_default("reviewer_review").system_prompt,
            "reviewer_fix": ctx.personas.load_or_default("reviewer_fix").system_prompt,
        },
    )
    max_rounds = ctx.config.plan_hardening.max_rounds
    debug = ctx.config.plan_hardening.debug
    stop_on_cosmetic = ctx.config.plan_hardening.stop_on_cosmetic_only

    current_plan = plan
    consecutive_malformed = 0

    for round_num in range(1, max_rounds + 1):
        if ctx.shutdown_event and ctx.shutdown_event.is_set():
            log.info("harden: shutdown requested — stopping at round %d", round_num)
            return PhaseResult(success=False, error="shutdown requested")

        log.info("harden round %d/%d", round_num, max_rounds)

        # ── Review pass ───────────────────────────────────────────────────
        review_assembled = assembler.assemble_reviewer(current_plan, mode="review")
        review_prompt = _combine_prompt(review_assembled)

        review_result = await player.run(mode="review", prompt=review_prompt)
        if ctx.shutdown_event and ctx.shutdown_event.is_set():
            return PhaseResult(success=False, error="shutdown requested")
        if not review_result.success:
            log.error("harden: reviewer (find-issues) failed in round %d: %s", round_num, review_result.error)
            return PhaseResult(success=False, error=review_result.error)

        review_output = review_result.verdict
        verdict = _parse_verdict(review_output)
        if debug:
            log.info("harden round %d verdict: %s", round_num, verdict)

        # ── Malformed handling ────────────────────────────────────────────
        if verdict == "malformed":
            consecutive_malformed += 1
            log.warning(
                "harden: malformed reviewer output in round %d (%d consecutive)",
                round_num,
                consecutive_malformed,
            )
            if consecutive_malformed >= 2:
                log.info("harden: 2 consecutive malformed → stopping (treat as NO ISSUES)")
                break
            # Single malformed → treat as CRITICAL
            verdict = "critical"
        else:
            consecutive_malformed = 0

        # ── Convergence check ─────────────────────────────────────────────
        if verdict == "no_issues":
            log.info("harden converged at round %d: NO ISSUES FOUND", round_num)
            break

        if verdict == "cosmetic" and stop_on_cosmetic:
            log.info("harden stopping at round %d: cosmetic-only issues", round_num)
            break

        # ── Fix pass (CRITICAL or cosmetic without stop_on_cosmetic) ─────
        fix_assembled = assembler.assemble_reviewer(current_plan, mode="fix")
        fix_prompt = _combine_prompt(fix_assembled)

        fix_result = await player.run(mode="fix", prompt=fix_prompt, review_findings=review_output)
        if ctx.shutdown_event and ctx.shutdown_event.is_set():
            return PhaseResult(success=False, error="shutdown requested")
        if not fix_result.success:
            log.error("harden: reviewer (apply-fixes) failed in round %d: %s", round_num, fix_result.error)
            return PhaseResult(success=False, error=fix_result.error)

        if fix_result.fixed_plan:
            current_plan = fix_result.fixed_plan

        # Write intermediate version for debugging / recovery
        try:
            ctx.disk.write_file(f"{ctx.milestone_path}/plan_v{round_num}.md", current_plan)
        except OSError as e:
            log.warning("harden: intermediate write failed (non-fatal): %s", e)
        if debug:
            log.info(
                "harden round %d: applied fixes, plan is %d chars", round_num, len(current_plan)
            )

    # Write the final hardened plan
    try:
        ctx.disk.write_file(f"{ctx.milestone_path}/PLAN.md", current_plan)
    except OSError as e:
        log.error("harden: failed to write PLAN.md: %s", e)
        return PhaseResult(success=False, error=f"PLAN.md write failed: {e}")
    log.info("harden complete — PLAN.md written (%d chars)", len(current_plan))
    return PhaseResult(success=True, data=current_plan)


# ── Internal helpers ──────────────────────────────────────────────────────


def _parse_verdict(output: str) -> str:
    """Parse the Reviewer LLM output into a verdict string.

    Returns:
        ``"no_issues"`` — ``NO ISSUES FOUND`` present.
        ``"cosmetic"``  — only ``COSMETIC`` found (no ``CRITICAL``).
        ``"critical"``  — ``CRITICAL`` present.
        ``"malformed"`` — none of the expected markers found.
    """
    has_critical = bool(_CRITICAL_RE.search(output))
    if has_critical:
        return "critical"
    if _NO_ISSUES_RE.search(output):
        return "no_issues"
    if _COSMETIC_RE.search(output):
        return "cosmetic"
    return "malformed"


def _combine_prompt(assembled: object) -> str:
    """Combine system + user prompt sections from an AssembledPrompt."""
    system = getattr(assembled, "system_prompt", "") or ""
    user = getattr(assembled, "user_prompt", "") or ""
    if system and user:
        return f"{system}\n\n---\n\n{user}"
    return system or user
