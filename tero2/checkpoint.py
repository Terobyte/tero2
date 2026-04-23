"""Checkpoint management for crash recovery."""

from __future__ import annotations

from datetime import datetime, timezone

from tero2.constants import MAX_STEPS_PER_TASK
from tero2.disk_layer import DiskLayer
from tero2.errors import StateTransitionError
from tero2.state import AgentState, Phase, SoraPhase


_VALID_TRANSITIONS: set[tuple[Phase, Phase]] = {
    (Phase.IDLE, Phase.RUNNING),
    (Phase.RUNNING, Phase.COMPLETED),
    (Phase.RUNNING, Phase.FAILED),
    (Phase.RUNNING, Phase.PAUSED),
    (Phase.PAUSED, Phase.RUNNING),
    (Phase.PAUSED, Phase.FAILED),
    (Phase.FAILED, Phase.RUNNING),
}


class CheckpointManager:
    def __init__(self, disk: DiskLayer, max_steps_per_task: int = MAX_STEPS_PER_TASK) -> None:
        self.disk = disk
        self.max_steps_per_task = max_steps_per_task

    def save(self, state: AgentState) -> AgentState:
        state.last_checkpoint = datetime.now(timezone.utc).isoformat()
        state.touch()
        self.disk.write_state(state)
        return state

    def restore(self) -> AgentState:
        return self.disk.read_state()

    def _transition(self, state: AgentState, target: Phase) -> AgentState:
        if (state.phase, target) not in _VALID_TRANSITIONS:
            raise StateTransitionError(state.phase.value, target.value)
        state.phase = target
        return state

    def mark_started(self, plan_file: str) -> AgentState:
        # Restore prior state so accumulated context (retry_count, current_task,
        # steps_in_task, etc.) survives a restart after FAILED or PAUSED.
        # IDLE is safe too — it's a valid IDLE → RUNNING transition.
        # If the on-disk phase is RUNNING or COMPLETED (unexpected), fall back to
        # a clean state to avoid invalid double-start transitions.
        prior = self.restore()
        if prior.phase in (Phase.IDLE, Phase.FAILED, Phase.PAUSED):
            state = prior
        else:
            state = AgentState()
        state = self._transition(state, Phase.RUNNING)
        # Clear any stale error_message left over from a previous FAILED or
        # PAUSED phase — the run is starting fresh, the old reason no longer
        # applies, and `tero2 status` would otherwise show the old text.
        state.error_message = ""
        state.plan_file = str(plan_file)
        state.started_at = datetime.now(timezone.utc).isoformat()
        state = self.save(state)
        return state

    def mark_completed(self, state: AgentState) -> AgentState:
        state = self._transition(state, Phase.COMPLETED)
        self.save(state)
        return state

    def mark_failed(self, state: AgentState, error: str) -> AgentState:
        state = self._transition(state, Phase.FAILED)
        state.error_message = error
        self.save(state)
        return state

    def mark_paused(self, state: AgentState, reason: str) -> AgentState:
        state = self._transition(state, Phase.PAUSED)
        state.error_message = reason
        self.save(state)
        return state

    def mark_running(self, state: AgentState) -> AgentState:
        state = self._transition(state, Phase.RUNNING)
        state.error_message = ""
        self.save(state)
        return state

    def increment_retry(self, state: AgentState) -> AgentState:
        state.retry_count += 1
        state.steps_in_task = 0
        state.provider_index = 0
        state.tool_repeat_count = 0
        state.last_tool_hash = ""
        state.touch()
        self.save(state)
        return state

    def increment_step(self, state: AgentState) -> AgentState:
        state.steps_in_task += 1
        state.touch()
        self.save(state)
        return state

    def set_sora_phase(self, state: AgentState, phase: SoraPhase) -> AgentState:
        """Update the SORA pipeline phase and persist it to disk.

        Call this at each SORA phase boundary (HARDENING → SCOUT → COACH →
        ARCHITECT → EXECUTE → SLICE_DONE) so that crash recovery can resume
        at the correct phase rather than restarting from the beginning.

        Args:
            state: Current agent state.
            phase: Target SoraPhase value.

        Returns:
            Updated AgentState with sora_phase set and checkpoint written.
        """
        state.sora_phase = phase
        self.save(state)
        return state
