"""Stuck detection — structural loop detection.

Three signals (all deterministic, no LLM):
    1. retry_count >= threshold → probably stuck
    2. steps_in_task >= threshold → task taking too long
    3. tool_repeat_count >= threshold → deadlock (same action repeated)

These counters are NOT accessible to the LLM — only the Dispatcher reads them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from tero2.config import StuckDetectionConfig
from tero2.state import AgentState


class StuckSignal(str, Enum):
    """Type of stuck signal detected."""

    NONE = "none"
    RETRY_EXHAUSTED = "retry_exhausted"
    STEP_LIMIT = "step_limit"
    TOOL_REPEAT = "tool_repeat"


@dataclass
class StuckResult:
    """Result of stuck detection check."""

    signal: StuckSignal
    details: str  # human-readable explanation
    severity: int  # 0=none, 1=warning, 2=escalate


def check_stuck(state: AgentState, config: StuckDetectionConfig) -> StuckResult:
    """Check if the agent is stuck.

    Checked at phase boundaries and after each step in the execution loop.
    Priority: RETRY_EXHAUSTED > STEP_LIMIT > TOOL_REPEAT.

    Returns StuckResult with signal and severity.
    Severity 0 = no problem, 2 = escalate now.
    """
    if state.retry_count >= config.max_retries:
        return StuckResult(
            signal=StuckSignal.RETRY_EXHAUSTED,
            details=(f"retry_count={state.retry_count} >= max_retries={config.max_retries}"),
            severity=2,
        )
    if state.steps_in_task >= config.max_steps_per_task:
        return StuckResult(
            signal=StuckSignal.STEP_LIMIT,
            details=(
                f"steps_in_task={state.steps_in_task} >= "
                f"max_steps_per_task={config.max_steps_per_task}"
            ),
            severity=2,
        )
    if state.tool_repeat_count > 0 and config.tool_repeat_threshold > 0 and state.tool_repeat_count >= config.tool_repeat_threshold:
        return StuckResult(
            signal=StuckSignal.TOOL_REPEAT,
            details=(
                f"same tool call repeated {state.tool_repeat_count} times "
                f"(hash={state.last_tool_hash})"
            ),
            severity=2,
        )

    return StuckResult(signal=StuckSignal.NONE, details="", severity=0)


def compute_tool_hash(tool_call: str) -> str:
    """Compute a hash of a tool call for repeat detection.

    Uses first 16 chars of SHA-256 of the tool call string.
    """
    return hashlib.sha256(tool_call.encode()).hexdigest()[:16]


def update_tool_hash(state: AgentState, tool_call: str) -> tuple[AgentState, bool]:
    """Update the tool hash in state and check for repeat.

    Args:
        state: Current agent state.
        tool_call: String representation of the current tool call.

    Returns:
        (updated_state, is_repeat) — is_repeat is True if hash matches previous.
        The original state is NOT mutated; a new state object is returned.
    """
    from dataclasses import replace
    new_hash = compute_tool_hash(tool_call)
    is_repeat = new_hash == state.last_tool_hash
    new_count = state.tool_repeat_count + 1 if is_repeat else 0
    new_state = replace(state, last_tool_hash=new_hash, tool_repeat_count=new_count)
    return new_state, is_repeat
