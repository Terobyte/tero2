"""
Failing tests demonstrating 8 critical bugs from bugs.md.

  A1  — runner.py _handle_override() modifies state locally but never returns it
  A26 — runner.py pause/stop handling code is entirely unreachable
  A2  — runner.py execute phase runs with empty SlicePlan without raising an error
  A34 — runner.py runner's local state is stale after run_architect() updates ctx.state
  A7  — execute_phase.py checkpoint not saved after execute_escalation() updates ctx.state
  A33 — execute_phase.py PAUSE via OVERRIDE.md does not save checkpoint
  A8  — providers/chain.py circuit-broken providers skipped, RateLimitError raised instead of distinct error
  A10 — state.py __setattr__ skips validation on first assignment

Each test FAILs against current code and would pass once the bug is fixed.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.circuit_breaker import CBState, CircuitBreaker, CircuitBreakerRegistry
from tero2.errors import CircuitOpenError, RateLimitError
from tero2.phases.context import PhaseResult, RunnerContext
from tero2.players.architect import SlicePlan
from tero2.providers.chain import ProviderChain
from tero2.state import AgentState, Phase, SoraPhase


# ─────────────────────────────────────────────────────────────────────────────
# A1 — _handle_override() modifies state locally but never returns it
# ─────────────────────────────────────────────────────────────────────────────


def test_handle_override_stop_returns_updated_state():
    """A1 — _handle_override() must return the modified state so the caller sees
    the STOP/PAUSE transition.

    Current code::

        def _handle_override(self, content: str, state: AgentState) -> None:
            if self._RE_STOP.search(content):
                state = self.checkpoint.mark_failed(state, "STOP directive in OVERRIDE.md")
                self._current_state = state
                return
            if self._RE_PAUSE.search(content) and state.phase != Phase.PAUSED:
                state = self.checkpoint.mark_paused(state, "PAUSE directive in OVERRIDE.md")
                self._current_state = state

    Bug: ``state`` inside ``_handle_override`` is a local variable (the parameter is
    rebound by ``state = self.checkpoint.mark_failed(...)``). Because Python passes
    by object reference, reassigning the *name* ``state`` does NOT update the
    caller's variable. The caller's ``state.phase`` remains unchanged — PAUSE/STOP
    transitions are silently discarded.
    """
    from tero2.runner import Runner

    checkpoint = MagicMock()

    # mark_failed returns a new state with phase=FAILED
    failed_state = AgentState()
    object.__setattr__(failed_state, "phase", Phase.FAILED)
    checkpoint.mark_failed.return_value = failed_state

    runner = MagicMock(spec=Runner)
    runner.checkpoint = checkpoint
    runner._RE_STOP = Runner._RE_STOP
    runner._RE_PAUSE = Runner._RE_PAUSE
    runner._handle_override = Runner._handle_override.__get__(runner, Runner)

    original_state = AgentState()
    # Phase starts at IDLE — we'll fake it as RUNNING to allow FAILED transition
    object.__setattr__(original_state, "phase", Phase.RUNNING)

    # Call the buggy method — it reassigns the local `state` but returns None
    result = runner._handle_override("STOP", original_state)

    # BUG: _handle_override returns None, caller can't get the updated state
    assert result is not None, (
        "BUG: _handle_override() returns None — the caller never gets the "
        "updated state after a STOP directive. STOP is silently ignored."
    )
    assert result.phase == Phase.FAILED, (
        f"BUG: _handle_override() should return state with phase=FAILED after STOP, "
        f"got {result!r}. Return value is None, so check above fires first."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A26 — pause/stop handling code is entirely unreachable
# ─────────────────────────────────────────────────────────────────────────────


def test_handle_override_pause_updates_caller_state():
    """A26 — after _handle_override() call, checks state.phase == Phase.PAUSED
    on the unmodified local variable.

    Current code (runner.py lines 288–345)::

        self._handle_override(override, state)
        if state.phase == Phase.FAILED:   # ← always False — state was NOT updated
            ...
            return
        if state.phase == Phase.PAUSED:   # ← always False — unreachable
            ...

    Bug: ``_handle_override`` reassigns its local ``state`` parameter but the
    caller's ``state`` variable is never updated. The phase checks after the call
    always evaluate against the original (RUNNING) phase, so the PAUSE/STOP
    handling branches are dead code.
    """
    from tero2.runner import Runner

    checkpoint = MagicMock()

    # mark_paused returns a new state with phase=PAUSED
    paused_state = AgentState()
    object.__setattr__(paused_state, "phase", Phase.PAUSED)
    checkpoint.mark_paused.return_value = paused_state

    runner = MagicMock(spec=Runner)
    runner.checkpoint = checkpoint
    runner._RE_STOP = Runner._RE_STOP
    runner._RE_PAUSE = Runner._RE_PAUSE
    runner._handle_override = Runner._handle_override.__get__(runner, Runner)

    caller_state = AgentState()
    object.__setattr__(caller_state, "phase", Phase.RUNNING)

    # Simulate the caller pattern: call _handle_override, then check state.phase
    runner._handle_override("PAUSE", caller_state)

    # BUG: caller_state.phase is still RUNNING because _handle_override rebinds
    # its local `state` variable without returning it.
    assert caller_state.phase == Phase.PAUSED, (
        f"BUG: after _handle_override('PAUSE', state), caller's state.phase is "
        f"{caller_state.phase!r} instead of Phase.PAUSED. "
        "The pause/stop handling branches (lines 296–344) are entirely unreachable."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A2 — execute phase runs with empty SlicePlan, no error raised by runner
# ─────────────────────────────────────────────────────────────────────────────


def test_load_slice_plan_from_disk_empty_raises_error_before_execute():
    """A2 — runner.py lines 490–505: execute phase is entered with an empty
    SlicePlan when the plan file is missing; the runner does not validate this
    before proceeding.

    Current code::

        else:
            slice_plan = _load_slice_plan_from_disk(ctx, state.current_slice or "S01")
        # ← no check that slice_plan.tasks is non-empty here
        if not _phase_already_done(state.sora_phase, SoraPhase.EXECUTE):
            ...
            exec_result = await run_execute(ctx, slice_plan)  # ← called with empty plan

    Bug: ``_load_slice_plan_from_disk`` documents that it returns an empty
    SlicePlan when the file is missing (crash before Architect wrote the file).
    The runner passes this empty plan to run_execute without validating first.
    The caller should detect the empty plan BEFORE entering the execute phase
    and raise an error rather than letting run_execute handle it silently.
    """
    from tero2.phases.context import _load_slice_plan_from_disk

    # Simulate a ctx where the plan file doesn't exist (architect crashed)
    ctx = MagicMock()
    ctx.disk.read_file.return_value = None  # file missing → returns falsy
    ctx.milestone_path = "milestones/M001"

    # Fix: _load_slice_plan_from_disk must raise when the plan file is missing,
    # so the runner never reaches run_execute with an empty SlicePlan.
    with pytest.raises(ValueError, match="plan file missing"):
        _load_slice_plan_from_disk(ctx, "S01")


# ─────────────────────────────────────────────────────────────────────────────
# A34 — runner's local state is stale after run_architect() updates ctx.state
# ─────────────────────────────────────────────────────────────────────────────


def test_runner_state_refreshed_from_ctx_after_run_architect():
    """A34 — runner.py must have `state = ctx.state` after run_architect().

    Source check: look for the refresh assignment in _execute_sora.
    """
    import inspect
    from tero2.runner import Runner

    source = inspect.getsource(Runner._execute_sora)
    # Find run_architect call and check that state = ctx.state follows it
    lines = source.splitlines()
    found_refresh = False
    for i, line in enumerate(lines):
        if "run_architect" in line and "await" in line:
            # Look in the next 10 lines for state = ctx.state
            for j in range(i + 1, min(i + 10, len(lines))):
                if "state = ctx.state" in lines[j]:
                    found_refresh = True
                    break
            break

    assert found_refresh, (
        "BUG (A34): runner.py _execute_sora lacks `state = ctx.state` after "
        "run_architect() — the runner's local state variable is stale after the "
        "architect updates ctx.state via checkpoint.save(). "
        "Fix: add `state = ctx.state` immediately after the run_architect success path."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A7 — checkpoint not saved after execute_escalation() updates ctx.state
# ─────────────────────────────────────────────────────────────────────────────


def test_execute_phase_saves_checkpoint_after_escalation():
    """A7 — execute_phase.py must call checkpoint.save() after execute_escalation().

    Source check: verify `checkpoint.save` follows `execute_escalation` in source.
    """
    import inspect
    import tero2.phases.execute_phase as ep

    source = inspect.getsource(ep)
    # Find execute_escalation call and verify checkpoint.save follows within 15 lines
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if "execute_escalation" in line and "await" in line:
            for j in range(i + 1, min(i + 15, len(lines))):
                if "checkpoint.save" in lines[j]:
                    return  # found — test passes
            break

    assert False, (
        "BUG (A7): execute_phase.py lacks ctx.checkpoint.save() after "
        "execute_escalation() updates ctx.state. Escalation level is lost on crash."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A33 — PAUSE via OVERRIDE.md updates ctx.state but checkpoint not saved
# ─────────────────────────────────────────────────────────────────────────────


def test_execute_phase_pause_override_saves_checkpoint():
    """A33 — execute_phase.py must call checkpoint.save() after mark_paused() in PAUSE path.

    Source check: verify `checkpoint.save` follows `mark_paused` in _check_override.
    """
    import inspect
    import tero2.phases.execute_phase as ep

    source = inspect.getsource(ep._check_override)
    lines = source.splitlines()
    for i, line in enumerate(lines):
        # Match actual code line (not docstring): ctx.state = ctx.checkpoint.mark_paused
        if "checkpoint.mark_paused" in line and "ctx.state" in line and "=" in line:
            for j in range(i + 1, min(i + 4, len(lines))):
                if "checkpoint.save" in lines[j]:
                    return  # found — test passes
            break

    assert False, (
        "BUG (A33): execute_phase._check_override lacks ctx.checkpoint.save() "
        "after mark_paused() in the PAUSE override path. The PAUSE mark may be "
        "lost on crash — an explicit save() must follow ctx.state assignment."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A8 — circuit-broken providers skipped, RateLimitError raised instead of
#      distinct unavailability error
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chain_raises_distinct_error_when_all_providers_circuit_broken():
    """A8 — providers/chain.py lines 70–118: circuit-broken providers are silently
    skipped; when ALL providers are circuit-broken the final error is
    RateLimitError("all providers exhausted"), which is indistinguishable from
    a rate-limit failure where providers were actually called.

    Current code::

        for idx, provider in enumerate(self.providers):
            cb = self.cb_registry.get(provider.display_name)
            if not cb.is_available:
                continue            # ← silently skipped, no record of why
            ...
        raise RateLimitError("all providers in chain exhausted")
                              # ← same error as "tried but rate-limited"

    Bug: callers (e.g. retry logic) treat RateLimitError as "tried and failed" and
    apply exponential backoff. When providers are circuit-broken (unavailable),
    the correct signal is "unavailable — don't retry yet" — not "rate limited".
    The caller cannot distinguish the two cases, so it backs off unnecessarily
    and may also incorrectly count a circuit-open failure as a rate-limit.
    """
    # Build a chain of two providers both with open circuit breakers
    provider_a = MagicMock()
    provider_a.display_name = "provider_a"
    provider_b = MagicMock()
    provider_b.display_name = "provider_b"

    cb_registry = CircuitBreakerRegistry(failure_threshold=1)
    # Force both circuit breakers to OPEN state
    cb_a = cb_registry.get("provider_a")
    cb_b = cb_registry.get("provider_b")

    # Open the circuit breakers without waiting for timeout
    for cb in (cb_a, cb_b):
        cb.record_failure()  # failure_threshold=1 → OPEN after one failure

    chain = ProviderChain(
        providers=[provider_a, provider_b],
        cb_registry=cb_registry,
        rate_limit_max_retries=0,
        rate_limit_wait_s=0.0,
    )

    assert cb_a.state == CBState.OPEN, "setup: cb_a should be OPEN"
    assert cb_b.state == CBState.OPEN, "setup: cb_b should be OPEN"

    # BUG: raises RateLimitError — same as "all retried but rate limited"
    # Expected: raises CircuitOpenError (or a distinct error signalling unavailability)
    with pytest.raises(CircuitOpenError):
        async for _ in chain.run(prompt="test"):
            pass
    # If we reach here, the correct error was raised — test passes (post-fix).
    # Against current code the assertion inside pytest.raises fails because
    # RateLimitError is raised, not CircuitOpenError.


# ─────────────────────────────────────────────────────────────────────────────
# A10 — __setattr__ skips validation on first assignment
# ─────────────────────────────────────────────────────────────────────────────


def test_agent_state_rejects_invalid_initial_phase():
    """A10 — state.py lines 96–105: AgentState.__setattr__ skips validation when
    the attribute is assigned for the first time (during __init__).

    Current code::

        def __setattr__(self, name: str, value: object) -> None:
            if name == "phase" and "phase" in self.__dict__:   # ← skipped on first assignment!
                target: Phase = value
                if target not in _PHASE_VALID_NEXT.get(self.phase, frozenset()):
                    raise StateTransitionError(...)
            ...
            object.__setattr__(self, name, value)

    Bug: the guard ``"phase" not in self.__dict__`` means validation is skipped
    entirely on the very first write (dataclass __init__ calls __setattr__ for
    each field). An invalid Phase value can therefore be injected at construction
    time without raising StateTransitionError — e.g. creating an AgentState with
    phase=Phase.COMPLETED directly bypasses the normal transition guard.

    The correct behavior: validate ALL assignments, including the initial one —
    only the *starting* phase (IDLE) should be a valid initial value.
    """
    from tero2.errors import StateTransitionError

    # AgentState default starts at Phase.IDLE — that is valid
    # But what if someone passes phase=Phase.COMPLETED at construction?
    # __setattr__ skips validation on first write, so this silently succeeds.
    invalid_initial = AgentState(phase=Phase.COMPLETED)

    # BUG: no error was raised — COMPLETED is an invalid starting phase
    assert invalid_initial.phase != Phase.COMPLETED, (
        f"BUG: AgentState(phase=Phase.COMPLETED) was silently accepted — "
        f"got phase={invalid_initial.phase!r}. "
        "__setattr__ skips validation on the first assignment so any Phase "
        "value can be injected at construction. Only Phase.IDLE should be "
        "a valid initial phase."
    )
