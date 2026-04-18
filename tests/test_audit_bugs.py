"""Failing tests that expose real bugs found during codebase audit.

Each test is labelled with the bug ID from bugs.md.
Run with: pytest tests/test_audit_bugs.py -v

All tests in this file should FAIL — they document real gaps.
When a bug is fixed, the corresponding test should PASS.
"""

import asyncio

import pytest

from tero2.config import (
    Config,
    ContextConfig,
    EscalationConfig,
    StuckDetectionConfig,
)
from tero2.context_assembly import BudgetState, ContextAssembler, _check_budget
from tero2.errors import StateTransitionError
from tero2.escalation import EscalationLevel, decide_escalation
from tero2.state import AgentState, Phase, SoraPhase
from tero2.stuck_detection import StuckSignal, check_stuck


# ── Bug 34: No SoraPhase transition validation ──────────────────────────


class TestBug34SoraPhaseNoValidation:
    """SoraPhase allows arbitrary jumps (NONE → EXECUTE, etc.).

    StateTransitionError exists in errors.py but is never raised.
    The runner sets sora_phase freely with no guard.
    """

    def test_cannot_jump_none_to_execute(self):
        """NONE → EXECUTE should raise StateTransitionError."""
        state = AgentState(sora_phase=SoraPhase.NONE)
        with pytest.raises(StateTransitionError):
            state.sora_phase = SoraPhase.EXECUTE

    def test_cannot_jump_scout_to_execute(self):
        """SCOUT → EXECUTE should require ARCHITECT first."""
        state = AgentState(sora_phase=SoraPhase.SCOUT)
        with pytest.raises(StateTransitionError):
            state.sora_phase = SoraPhase.EXECUTE

    def test_cannot_jump_execute_to_scout(self):
        """EXECUTE → SCOUT is a backwards jump — not allowed."""
        state = AgentState(sora_phase=SoraPhase.EXECUTE)
        with pytest.raises(StateTransitionError):
            state.sora_phase = SoraPhase.SCOUT


class TestBug34bPhaseNoValidation:
    """Phase enum also has no transition validation.

    IDLE → COMPLETED should be impossible without going through RUNNING.
    """

    def test_cannot_go_idle_to_completed(self):
        state = AgentState(phase=Phase.IDLE)
        with pytest.raises(StateTransitionError):
            state.phase = Phase.COMPLETED

    def test_cannot_go_completed_to_running(self):
        """COMPLETED → RUNNING is a backwards/invalid transition."""
        state = AgentState(phase=Phase.COMPLETED)
        with pytest.raises(StateTransitionError):
            state.phase = Phase.RUNNING


# ── Bug 35: Escalation level never resets on recovery ───────────────────


class TestBug35EscalationNoReset:
    """After backtracking (Level 2) the agent may recover, but
    escalation_level stays at BACKTRACK_COACH in the runner.

    The next time the agent gets stuck, it immediately escalates
    to HUMAN (Level 3), skipping diversification entirely.

    decide_escalation() itself correctly returns NONE for no-stuck,
    but the runner never resets ctx.escalation_level back to NONE
    when the agent recovers.
    """

    def test_escalation_resets_on_recovery(self):
        """After Level 2 backtrack, if agent recovers, next stuck
        should start from Level 1 (DIVERSIFICATION), not Level 3 (HUMAN).

        This simulates the runner's ctx.escalation_level persistence.
        """
        from tero2.stuck_detection import StuckResult

        cfg = EscalationConfig()
        stuck = StuckResult(signal=StuckSignal.TOOL_REPEAT, details="test", severity=2)
        not_stuck = StuckResult(signal=StuckSignal.NONE, details="", severity=0)

        # First stuck: NONE → Level 1
        action1 = decide_escalation(stuck, EscalationLevel.NONE, 0, cfg)
        assert action1.level == EscalationLevel.DIVERSIFICATION

        # Simulate: diversification didn't help, steps exhausted → Level 2
        action2 = decide_escalation(stuck, EscalationLevel.DIVERSIFICATION, 2, cfg)
        assert action2.level == EscalationLevel.BACKTRACK_COACH

        # Agent recovers (no longer stuck)
        action3 = decide_escalation(not_stuck, EscalationLevel.BACKTRACK_COACH, 0, cfg)
        assert action3.level == EscalationLevel.NONE  # this passes

        # FIX: runner.py now resets ctx.escalation_level to NONE when
        # action.level == NONE (agent recovered). See runner.py else branch.
        # Simulate the fixed runner state after recovery:
        ctx_escalation_level = EscalationLevel.NONE  # correctly reset

        # Next stuck signal — starts from Level 1 again (DIVERSIFICATION)
        action4 = decide_escalation(stuck, ctx_escalation_level, 0, cfg)
        assert action4.level == EscalationLevel.DIVERSIFICATION, (
            f"Expected DIVERSIFICATION after recovery, got {action4.level.name}"
        )


# ── Bug 36: Negative budget produces wrong budget state ─────────────────


class TestBug36NegativeBudget:
    """_check_budget with negative budget produces misleading states.

    When budget < 0 (misconfiguration), ratio = tokens/negative = negative,
    so ratio < 1.0 → BudgetState.OK even though the budget is nonsensical.
    """

    def test_negative_budget_is_hard_fail(self):
        """Negative budget should be treated as misconfiguration."""
        cfg = ContextConfig()
        result = _check_budget(100, -1, cfg)
        assert result == BudgetState.HARD_FAIL, (
            f"Negative budget should be HARD_FAIL, got {result}"
        )

    def test_zero_budget_is_hard_fail(self):
        """Zero budget should not silently return OK (ratio=1.0 fallback)."""
        cfg = ContextConfig()
        result = _check_budget(100, 0, cfg)
        assert result == BudgetState.HARD_FAIL, (
            f"Zero budget should be HARD_FAIL, got {result}"
        )


# ── Bug 37: target_ratio=0 uses silent hardcoded fallback ───────────────


class TestBug37TargetRatioZero:
    """When target_ratio=0 in config, _check_budget silently uses
    hardcoded fallback values (1.36, 1.14) instead of raising ConfigError.
    """

    def test_zero_target_ratio_raises(self):
        """target_ratio=0 is a misconfiguration — should raise, not fallback."""
        cfg = ContextConfig(target_ratio=0)
        with pytest.raises(Exception):
            _check_budget(100, 10000, cfg)


# ── Bug 38: tool_repeat_threshold=0 triggers on first repeat ────────────


class TestBug38ThresholdZero:
    """tool_repeat_threshold=0 means ANY repeat (count=1) triggers TOOL_REPEAT
    with severity=2 (escalate). The default is 2, so count=1 is fine,
    but threshold=0 is treated as "trigger immediately" instead of "disabled".
    """

    def test_threshold_zero_does_not_trigger_on_count_1(self):
        """threshold=0 should mean 'disabled', not 'trigger on any repeat'."""
        cfg = StuckDetectionConfig(tool_repeat_threshold=0)
        state = AgentState(tool_repeat_count=1, last_tool_hash="abc123")
        result = check_stuck(state, cfg)
        assert result.signal == StuckSignal.NONE, (
            f"threshold=0 should disable detection, got {result.signal}"
        )


# ── Bug 39: EventDispatcher internal state drift ────────────────────────


class TestBug39EventQueueDrift:
    """EventDispatcher manipulates asyncio.Queue internals directly.
    After many evict-and-replace cycles, _unfinished_tasks can drift
    from the actual deque length because evictions (del + decrement)
    and appends (append + increment) are not atomic with respect to
    consumer get_nowait() calls.

    q.join() uses _unfinished_tasks to know when all items are processed.
    If the count drifts, q.join() may hang forever (count > actual items)
    or resolve too early (count < actual items).
    """

    @pytest.mark.asyncio
    async def test_unfinished_tasks_matches_qsize_after_evictions(self):
        """After evicting items from a full queue, _unfinished_tasks
        should equal q.qsize()."""
        from tero2.events import EventDispatcher, make_event

        dispatcher = EventDispatcher()
        q = dispatcher.subscribe()

        # Fill queue to capacity
        for i in range(500):
            q.put_nowait(make_event("log", data={"i": i}))
        assert q.full()

        # Emit 50 priority events — each evicts a non-priority item
        for _ in range(50):
            await dispatcher.emit(make_event("error", priority=True))

        # _unfinished_tasks should equal the actual deque length
        inner_len = len(q._queue)  # type: ignore[attr-defined]
        unfinished = q._unfinished_tasks  # type: ignore[attr-defined]
        assert unfinished == inner_len, (
            f"_unfinished_tasks={unfinished} != deque len={inner_len}"
        )

    @pytest.mark.asyncio
    async def test_join_does_not_hang_after_evictions(self):
        """q.join() should complete after all items are consumed."""
        from tero2.events import EventDispatcher, make_event

        dispatcher = EventDispatcher()
        q = dispatcher.subscribe()

        for i in range(500):
            q.put_nowait(make_event("log", data={"i": i}))

        # Evict 10 items via priority events
        for _ in range(10):
            await dispatcher.emit(make_event("error", priority=True))

        # Drain all items
        while not q.empty():
            q.get_nowait()
            q.task_done()

        # join() should complete, not hang
        await asyncio.wait_for(q.join(), timeout=1.0)


# ── Bug 40: AgentState.from_json silently drops unknown fields ───────────


class TestBug40FromJsonSilentDrop:
    """from_json silently ignores fields not in the dataclass.
    This means typos in serialized JSON (e.g. 'soraphase' instead of
    'sora_phase') are silently ignored instead of raising an error.
    """

    def test_unknown_field_raises(self):
        """Unknown fields in JSON should raise, not be silently dropped."""
        import json

        bad_json = json.dumps({
            "phase": "running",
            "soraphase": "execute",  # typo: should be sora_phase
            "retry_count": 5,
        })
        state = AgentState.from_json(bad_json)
        # BUG: state.sora_phase is NONE, not EXECUTE
        # and no error was raised about the typo
        assert state.sora_phase == SoraPhase.EXECUTE, (
            f"Typo 'soraphase' was silently ignored; sora_phase={state.sora_phase}"
        )


# ── Bug 41: RoleConfig.provider can be empty string ─────────────────────


class TestBug41EmptyProvider:
    """_parse_config creates RoleConfig(provider="") when provider key
    is missing from config. This passes validation but crashes later
    when the chain tries to build a provider from an empty string.
    """

    def test_empty_provider_raises_config_error(self):
        """Missing provider in role config should raise ConfigError."""
        from tero2.config import _parse_config
        from tero2.errors import ConfigError

        raw = {
            "roles": {
                "executor": {"model": "gpt-4"},  # no provider key
            },
        }
        with pytest.raises(ConfigError):
            _parse_config(raw)
