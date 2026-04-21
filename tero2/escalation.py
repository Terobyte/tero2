"""Escalation — 3-level response to stuck agents.

Level 1: Diversification (automatic)
    - Inject "previous path was a dead end, try a different approach"
    - 2 more steps to recover

Level 2: Backtrack + Resume (automatic)
    - Reset step/retry counters to last checkpoint
    - Write stuck details to EVENT_JOURNAL
    - Resume (Coach is deferred — not triggered in MVP2)

Level 3: Human escalation
    - Write STUCK_REPORT.md to .sora/human/
    - Send Telegram notification (text + voice)
    - PAUSE execution
    - Wait for STEER.md or OVERRIDE.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace as dataclasses_replace
from datetime import datetime, timezone
from enum import Enum

from tero2.checkpoint import CheckpointManager
from tero2.config import EscalationConfig
from tero2.disk_layer import DiskLayer
from tero2.notifier import Notifier, NotifyLevel
from tero2.state import AgentState
from tero2.stuck_detection import StuckResult, StuckSignal

log = logging.getLogger(__name__)

_DIVERSIFICATION_INJECT = (
    "⚠️ Previous approach hit a dead end. "
    "Try a completely different strategy to accomplish the task."
)


class EscalationLevel(int, Enum):
    NONE = 0
    DIVERSIFICATION = 1
    BACKTRACK_COACH = 2
    HUMAN = 3


@dataclass
class EscalationAction:
    """Action to take in response to stuck detection."""

    level: EscalationLevel
    inject_prompt: str = ""  # injected into next agent call (Level 1)
    should_backtrack: bool = False
    should_trigger_coach: bool = False  # Coach deferred in MVP2, always False
    should_pause: bool = False


def decide_escalation(
    stuck_result: StuckResult,
    current_level: EscalationLevel,
    diversification_steps_taken: int,
    config: EscalationConfig,
) -> EscalationAction:
    """Decide what escalation action to take.

    Progression:
        No stuck → Level 0 (do nothing)
        First stuck signal → Level 1 (diversification)
        Level 1 didn't help (N steps) → Level 2 (backtrack)
        Level 2 didn't help → Level 3 (human)

    Args:
        stuck_result: Current stuck detection result.
        current_level: Current escalation level (tracks progression).
        diversification_steps_taken: Attempts since Level 1 started.
        config: Escalation config.

    Returns:
        EscalationAction describing what to do.
    """
    if stuck_result.signal == StuckSignal.NONE:
        return EscalationAction(level=EscalationLevel.NONE)

    # Already at Level 2 (BACKTRACK_COACH) or above → escalate to Level 3 (human)
    if current_level >= EscalationLevel.BACKTRACK_COACH:
        return EscalationAction(level=EscalationLevel.HUMAN, should_pause=True)

    # At Level 1 → check if diversification window is exhausted
    if current_level == EscalationLevel.DIVERSIFICATION:
        if diversification_steps_taken >= config.diversification_max_steps:
            # Diversification didn't help → Level 2
            return EscalationAction(
                level=EscalationLevel.BACKTRACK_COACH,
                should_backtrack=config.backtrack_to_last_checkpoint,
                should_trigger_coach=False,  # Coach deferred in MVP2
            )
        # Still within diversification window — keep trying
        return EscalationAction(
            level=EscalationLevel.DIVERSIFICATION,
            inject_prompt=_DIVERSIFICATION_INJECT,
        )

    # First stuck signal → Level 1
    return EscalationAction(
        level=EscalationLevel.DIVERSIFICATION,
        inject_prompt=_DIVERSIFICATION_INJECT,
    )


async def execute_escalation(
    action: EscalationAction,
    state: AgentState,
    disk: DiskLayer,
    notifier: Notifier,
    checkpoint: CheckpointManager,
    stuck_result: StuckResult | None = None,
    escalation_history: list[EscalationLevel] | None = None,
) -> AgentState:
    """Execute the escalation action.

    Level 1: update state, inject prompt handled by caller
    Level 2: reset counters, write EVENT_JOURNAL, resume
    Level 3: write STUCK_REPORT.md, notify Telegram, pause

    Returns updated AgentState.
    """
    if action.level == EscalationLevel.NONE:
        return state

    if action.level == EscalationLevel.DIVERSIFICATION:
        log.info("escalation Level 1: diversification — injecting new-approach prompt")
        state = dataclasses_replace(
            state,
            escalation_level=EscalationLevel.DIVERSIFICATION.value,
            tool_repeat_count=0,
            last_tool_hash="",
        )
        state = checkpoint.save(state)
        await notifier.notify("stuck detected — diversifying approach", NotifyLevel.STUCK)
        return state

    if action.level == EscalationLevel.BACKTRACK_COACH:
        log.info("escalation Level 2: backtrack + resume (Coach deferred in MVP2)")
        timestamp = datetime.now(timezone.utc).isoformat()
        sr = stuck_result
        if action.should_backtrack:
            state = dataclasses_replace(
                state,
                steps_in_task=0,
                retry_count=0,
                tool_repeat_count=0,
                last_tool_hash="",
            )
        state = dataclasses_replace(state, escalation_level=EscalationLevel.BACKTRACK_COACH.value)
        state = checkpoint.save(state)
        disk.append_file(
            "persistent/EVENT_JOURNAL.md",
            f"\n## Stuck Event — {timestamp}\n"
            f"Level 2 escalation triggered. Resetting to last checkpoint.\n"
            f"Signal: {sr.signal.value if sr else 'unknown'}\n"
            f"Details: {sr.details if sr else ''}\n",
        )
        await notifier.notify("stuck — backtracking to last checkpoint", NotifyLevel.STUCK)
        return state

    if action.level == EscalationLevel.HUMAN:
        log.info("escalation Level 3: human escalation — pausing runner")
        _sr = stuck_result or StuckResult(signal=StuckSignal.NONE, details="unknown", severity=0)
        new_state = dataclasses_replace(state, escalation_level=EscalationLevel.HUMAN.value)
        new_state = checkpoint.mark_paused(new_state, "stuck — waiting for human input")
        write_stuck_report(
            disk=disk,
            state=new_state,
            stuck_result=_sr,
            escalation_history=escalation_history or [],
        )
        await notifier.notify(
            "🛑 Stuck — waiting for human input (edit STEER.md to resume)",
            NotifyLevel.STUCK,
        )
        return new_state

    return state


def write_stuck_report(
    disk: DiskLayer,
    state: AgentState,
    stuck_result: StuckResult,
    escalation_history: list[EscalationLevel],
) -> None:
    """Write STUCK_REPORT.md for human review.

    Format:
        # Stuck Report — {timestamp}
        **Task:** {task_id}
        **Signal:** {signal}
        **Steps:** {steps_in_task}
        **Retry count:** {retry_count}
        **Details:** {details}
        **What was tried:** Level 1 diversification, Level 2 backtrack
        **Needed:** STEER.md / OVERRIDE.md
    """
    timestamp = datetime.now(timezone.utc).isoformat()
    tried = []
    if EscalationLevel.DIVERSIFICATION in escalation_history:
        tried.append("Level 1 diversification")
    if EscalationLevel.BACKTRACK_COACH in escalation_history:
        tried.append("Level 2 backtrack")
    tried_str = ", ".join(tried) if tried else "none"

    report = (
        f"# Stuck Report — {timestamp}\n\n"
        f"**Task:** {state.current_task or '(unknown)'}\n"
        f"**Signal:** {stuck_result.signal.value}\n"
        f"**Steps in task:** {state.steps_in_task}\n"
        f"**Retry count:** {state.retry_count}\n"
        f"**Details:** {stuck_result.details}\n\n"
        f"**What was tried:** {tried_str}\n\n"
        "**Needed:** Edit `.sora/human/STEER.md` with new direction, "
        "or `.sora/human/OVERRIDE.md` with `PAUSE`/`STOP`.\n"
    )
    disk.write_file("human/STUCK_REPORT.md", report)
