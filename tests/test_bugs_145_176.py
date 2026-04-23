"""Halal tests for bugs 148-176 (Audit 6, 2026-04-23).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug 163  runner: _skip_current_task flag never cleared
  Bug 164  runner: _handle_override returns wrong state object
  Bug 148  phases/coach_phase: no exception handling for player.run()
  Bug 149  phases/scout_phase: no exception handling for player.run()
  Bug 150  phases/architect_phase: no exception handling for player.run()
  Bug 176  phases/execute: ANOMALY path uses stale context_hints
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 163: runner _skip_current_task flag never cleared ──────────────────────


class TestBug163SkipCurrentTaskNeverCleared:
    """_skip_current_task is set True by the 'skip_task' command handler but
    never reset to False after consumption. Once set, every subsequent task is
    skipped until the process restarts.
    Fix: clear _skip_current_task after it is consumed in _drain_commands or
    at the phase boundary.
    """

    def test_skip_current_task_reset_after_consume(self) -> None:
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._drain_commands)
        lines = source.splitlines()

        # Find where _skip_current_task is set to True
        set_true_lines = [
            i for i, line in enumerate(lines)
            if "_skip_current_task" in line and "True" in line
        ]

        if not set_true_lines:
            pytest.skip("_skip_current_task = True not found in _drain_commands")

        # After the set-to-True, there must be an explicit reset line
        # of the form: self._skip_current_task = False
        # somewhere in the same method body.
        for i in range(set_true_lines[0] + 1, len(lines)):
            line = lines[i].strip()
            if "_skip_current_task" in line and "= False" in line:
                return  # Found the reset -- test passes

        pytest.fail(
            "Bug 163: _skip_current_task is set to True in _drain_commands but "
            "never reset to False. Once a skip_task command is received, the flag "
            "remains True forever, causing every subsequent task to be skipped. "
            "Fix: add 'self._skip_current_task = False' after the flag is consumed."
        )


# ── Bug 164: runner _handle_override returns wrong state object ────────────────


class TestBug164HandleOverrideReturnsWrongState:
    """_handle_override modifies the passed-in `state` via object.__setattr__
    but returns self._current_state (the checkpointed copy) instead of the
    modified `state` parameter. Callers assign the return value, so the
    in-memory state diverges from what the caller believes happened.
    Fix: return `state` (the modified parameter), not self._current_state.
    """

    def test_handle_override_returns_state_on_stop(self) -> None:
        """_handle_override returns self._current_state (from checkpoint) but
        callers expect the `state` parameter back. When checkpoint returns a
        different object identity, the caller's reference diverges."""
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._handle_override)
        lines = source.splitlines()

        # Find the STOP branch: should return `state`, not self._current_state
        for i, line in enumerate(lines):
            if "return self._current_state" in line.strip():
                # Check the surrounding context: is this inside the STOP or PAUSE branch?
                context_before = "\n".join(lines[max(0, i - 5):i])
                if "RE_STOP" in context_before:
                    pytest.fail(
                        "Bug 164: _handle_override STOP branch returns "
                        "self._current_state instead of `state`. The caller "
                        "assigns the return value to its local `state` variable, "
                        "so the checkpointed copy and the caller's reference "
                        "diverge. Fix: return `state` instead of "
                        "self._current_state."
                    )
                if "RE_PAUSE" in context_before:
                    pytest.fail(
                        "Bug 164: _handle_override PAUSE branch returns "
                        "self._current_state instead of `state`. "
                        "Fix: return `state` instead of self._current_state."
                    )

    def test_handle_override_returns_state_on_pause(self) -> None:
        """Same as STOP test but for the PAUSE branch."""
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._handle_override)
        lines = source.splitlines()

        # Walk through and track which branch we are in
        in_pause_branch = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if "RE_PAUSE" in stripped and "if" in stripped:
                in_pause_branch = True
            elif in_pause_branch and stripped.startswith("if ") and "RE_STOP" not in stripped:
                # New if-block, check if we left PAUSE branch
                pass
            if in_pause_branch and "return self._current_state" in stripped:
                pytest.fail(
                    "Bug 164: _handle_override PAUSE branch returns "
                    "self._current_state instead of `state`. "
                    "Fix: return `state` instead of self._current_state."
                )
            if in_pause_branch and stripped == "return state":
                return  # Good -- PAUSE branch returns the parameter


# ── Bug 148: coach_phase no exception handling for player.run() ────────────────


class TestBug148CoachPhaseNoExceptionHandling:
    """player.run() in coach_phase is not wrapped in try/except.
    If player.run() raises an unexpected exception (network error, parser
    crash), it propagates instead of returning PhaseResult(success=False).
    Coach is documented as non-fatal — it should never crash the pipeline.
    Fix: wrap player.run() in try/except, return PhaseResult(success=False).
    """

    @pytest.mark.asyncio
    async def test_coach_phase_catches_player_run_exception(self) -> None:
        from tero2.phases.context import PhaseResult, RunnerContext
        from tero2.phases.coach_phase import run_coach

        ctx = MagicMock(spec=RunnerContext)
        ctx.shutdown_event = None
        ctx.build_chain = MagicMock()
        ctx.disk = MagicMock()
        ctx.disk.project_path = "/tmp/test_project"
        ctx.personas = MagicMock()
        ctx.personas.load_or_default = MagicMock(return_value=MagicMock(system_prompt=""))
        ctx.milestone_path = "milestones/M001"
        ctx.state = MagicMock()
        ctx.state.current_slice = "S01"

        # Mock the CoachPlayer so player.run() raises
        with patch("tero2.phases.coach_phase.CoachPlayer") as MockPlayer:
            mock_player = MockPlayer.return_value
            mock_player.run = AsyncMock(side_effect=RuntimeError("unexpected crash"))

            result = await run_coach(ctx)

            assert isinstance(result, PhaseResult), (
                "Bug 148: run_coach propagated an exception from player.run() "
                "instead of catching it and returning PhaseResult(success=False). "
                "Coach is documented as non-fatal and must never crash the pipeline. "
                "Fix: wrap player.run() in try/except, return "
                "PhaseResult(success=False, error=str(exc))."
            )
            assert result.success is False, (
                "Bug 148: run_coach should return PhaseResult(success=False) "
                "when player.run() raises. Fix: catch the exception."
            )

    @pytest.mark.asyncio
    async def test_coach_phase_exception_returns_error_message(self) -> None:
        from tero2.phases.context import PhaseResult, RunnerContext
        from tero2.phases.coach_phase import run_coach

        ctx = MagicMock(spec=RunnerContext)
        ctx.shutdown_event = None
        ctx.build_chain = MagicMock()

        with patch("tero2.phases.coach_phase.CoachPlayer") as MockPlayer:
            mock_player = MockPlayer.return_value
            mock_player.run = AsyncMock(side_effect=RuntimeError("unexpected crash"))

            result = await run_coach(ctx)

            if isinstance(result, PhaseResult):
                assert result.error, (
                    "Bug 148: run_coach catches the exception but returns an empty "
                    "error string. The error message should contain the exception text. "
                    "Fix: set error=str(exc)."
                )


# ── Bug 149: scout_phase no exception handling for player.run() ────────────────


class TestBug149ScoutPhaseNoExceptionHandling:
    """player.run() in scout_phase is not wrapped in try/except.
    Same pattern as bug 148. Scout is non-fatal but an unhandled exception
    propagates instead of returning PhaseResult(success=False).
    Fix: wrap player.run() in try/except, return PhaseResult(success=False).
    """

    @pytest.mark.asyncio
    async def test_scout_phase_catches_player_run_exception(self) -> None:
        from tero2.phases.context import PhaseResult, RunnerContext
        from tero2.phases.scout_phase import run_scout

        ctx = MagicMock(spec=RunnerContext)
        ctx.shutdown_event = None
        ctx.config = MagicMock()
        ctx.config.context = MagicMock()
        ctx.config.context.skip_scout_if_files_lt = 100
        ctx.build_chain = MagicMock()
        ctx.disk = MagicMock()
        ctx.disk.project_path = "/tmp/test_project"
        ctx.personas = MagicMock()
        ctx.personas.load_or_default = MagicMock(return_value=MagicMock(system_prompt=""))
        ctx.milestone_path = "milestones/M001"

        with patch("tero2.phases.scout_phase.ScoutPlayer") as MockPlayer:
            MockPlayer.should_skip = staticmethod(lambda *a, **kw: False)
            mock_player = MockPlayer.return_value
            mock_player.run = AsyncMock(side_effect=RuntimeError("unexpected crash"))

            result = await run_scout(ctx)

            assert isinstance(result, PhaseResult), (
                "Bug 149: run_scout propagated an exception from player.run() "
                "instead of catching it. Scout is non-fatal. "
                "Fix: wrap player.run() in try/except, return "
                "PhaseResult(success=False, error=str(exc))."
            )
            assert result.success is False, (
                "Bug 149: run_scout should return PhaseResult(success=False) "
                "when player.run() raises."
            )


# ── Bug 150: architect_phase no exception handling for player.run() ────────────


class TestBug150ArchitectPhaseNoExceptionHandling:
    """player.run() in architect_phase is not wrapped in try/except.
    Same pattern as bugs 148/149. Architect failure is fatal for the current
    slice but an unhandled exception propagates instead of returning
    PhaseResult(success=False).
    Fix: wrap player.run() in try/except, return PhaseResult(success=False).
    """

    @pytest.mark.asyncio
    async def test_architect_phase_catches_player_run_exception(self) -> None:
        from tero2.phases.context import PhaseResult, RunnerContext
        from tero2.phases.architect_phase import run_architect

        ctx = MagicMock(spec=RunnerContext)
        ctx.shutdown_event = None
        ctx.build_chain = MagicMock()
        ctx.disk = MagicMock()
        ctx.disk.project_path = "/tmp/test_project"
        ctx.personas = MagicMock()
        ctx.personas.load_or_default = MagicMock(return_value=MagicMock(system_prompt=""))
        ctx.milestone_path = "milestones/M001"
        ctx.state = MagicMock()
        ctx.state.current_slice = "S01"
        ctx.checkpoint = MagicMock()

        with patch("tero2.phases.architect_phase.ArchitectPlayer") as MockPlayer:
            mock_player = MockPlayer.return_value
            mock_player.run = AsyncMock(side_effect=RuntimeError("unexpected crash"))

            result = await run_architect(ctx)

            assert isinstance(result, PhaseResult), (
                "Bug 150: run_architect propagated an exception from player.run() "
                "instead of catching it. Architect failure should be reported as "
                "PhaseResult(success=False), not crash the pipeline. "
                "Fix: wrap player.run() in try/except, return "
                "PhaseResult(success=False, error=str(exc))."
            )
            assert result.success is False, (
                "Bug 150: run_architect should return PhaseResult(success=False) "
                "when player.run() raises."
            )


# ── Bug 176: execute_phase ANOMALY path uses stale context_hints ──────────────


class TestBug176AnomalyStaleContextHints:
    """After Coach updates CONTEXT_HINTS.md during ANOMALY handling, the
    refreshed hints are written to `context_hints` but `effective_hints` (the
    variable actually passed to the next builder attempt) is NOT updated.
    The next attempt uses the stale pre-coach hints.
    Fix: update `effective_hints = context_hints` after the refresh.
    """

    def test_anomaly_path_updates_effective_hints_after_coach(self) -> None:
        import tero2.phases.execute_phase as ep_module

        source = inspect.getsource(ep_module.run_execute)
        lines = source.splitlines()

        # Find the ANOMALY context_hints refresh block
        found_anomaly_hints_refresh = False
        found_effective_hints_update = False

        for i, line in enumerate(lines):
            if "context_hints = new_hints" in line:
                # Found where context_hints is refreshed after Coach
                found_anomaly_hints_refresh = True
                # Check if effective_hints is also updated in the next few lines
                # within the same if block
                for j in range(i, min(i + 5, len(lines))):
                    if "effective_hints" in lines[j]:
                        found_effective_hints_update = True
                        break
                break

        if not found_anomaly_hints_refresh:
            pytest.skip("context_hints = new_hints not found in ANOMALY path")

        assert found_effective_hints_update, (
            "Bug 176: After refreshing context_hints from CONTEXT_HINTS.md in the "
            "ANOMALY path, effective_hints is NOT updated. The next builder attempt "
            "uses the stale pre-coach hints, ignoring the freshly-written strategy. "
            "Fix: add 'effective_hints = context_hints' after "
            "'context_hints = new_hints' in the ANOMALY block."
        )
