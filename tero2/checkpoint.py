"""Checkpoint management for crash recovery."""

from __future__ import annotations

from datetime import datetime, timezone

from tero2.constants import MAX_STEPS_PER_TASK
from tero2.disk_layer import DiskLayer
from tero2.errors import StateTransitionError
from tero2.state import AgentState, Phase


_VALID_TRANSITIONS: set[tuple[Phase, Phase]] = {
    (Phase.IDLE, Phase.RUNNING),
    (Phase.RUNNING, Phase.COMPLETED),
    (Phase.RUNNING, Phase.FAILED),
    (Phase.RUNNING, Phase.PAUSED),
    (Phase.PAUSED, Phase.RUNNING),
    (Phase.PAUSED, Phase.FAILED),
    (Phase.FAILED, Phase.RUNNING),
    (Phase.COMPLETED, Phase.RUNNING),
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
        state = AgentState()
        state = self._transition(state, Phase.RUNNING)
        state.plan_file = str(plan_file)
        state.started_at = datetime.now(timezone.utc).isoformat()
        state.touch()
        self.disk.write_state(state)
        return state

    def mark_completed(self, state: AgentState) -> AgentState:
        state = self._transition(state, Phase.COMPLETED)
        state.touch()
        self.save(state)
        return state

    def mark_failed(self, state: AgentState, error: str) -> AgentState:
        state = self._transition(state, Phase.FAILED)
        state.error_message = error
        state.touch()
        self.save(state)
        return state

    def mark_paused(self, state: AgentState, reason: str) -> AgentState:
        state = self._transition(state, Phase.PAUSED)
        state.error_message = reason
        state.touch()
        self.save(state)
        return state

    def mark_running(self, state: AgentState) -> AgentState:
        state = self._transition(state, Phase.RUNNING)
        state.error_message = ""
        state.touch()
        self.save(state)
        return state

    def increment_retry(self, state: AgentState) -> AgentState:
        state.retry_count += 1
        state.steps_in_task = 0
        state.provider_index = 0
        state.touch()
        self.save(state)
        return state

    def increment_step(self, state: AgentState) -> AgentState:
        state.steps_in_task += 1
        if state.steps_in_task > self.max_steps_per_task:
            raise RuntimeError(f"max_steps_per_task exceeded ({self.max_steps_per_task})")
        self.save(state)
        return state
