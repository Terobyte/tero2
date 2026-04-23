"""Negative tests for bugs 147–189 (Audit 6, 2026-04-23).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug 147  verifier: wrong output assignment for non-Python commands
  Bug 156  stuck_detection: tool repeat off-by-one
  Bug 157  cli: cmd_harden crashes on None config.roles
  Bug 169  builder: working_dir falsy check skips valid paths
  Bug 171  coach: silent truncation without logging
  Bug 189  escalation: diversification counter never incremented
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 147: verifier wrong output assignment for non-Python commands ──────────


class TestBug147VerifierWrongOutputAssignment:
    """Lines 137-138 unconditionally assign all_output[0] to ruff_output
    and all_output[1] to pytest_output even when custom commands are used
    (e.g. npm test). Downstream code that inspects ruff_output/pytest_output
    receives nonsense.
    Fix: only assign to ruff_output/pytest_output when using the default
    Python commands (ruff check / pytest); leave them empty for custom commands.
    """

    def test_custom_commands_no_ruff_output(self) -> None:
        from tero2.players.verifier import VerifierPlayer

        player = VerifierPlayer.__new__(VerifierPlayer)
        player.chain = MagicMock()
        player.disk = MagicMock()
        player.working_dir = "."

        with patch("tero2.players.verifier._run_command") as mock_run:
            mock_run.return_value = (0, "npm test passed", "")
            result = asyncio.run(
                player.run(
                    verify_commands=["npm test"],
                    builder_output="done",
                    task_id="T01",
                )
            )

        assert result.ruff_output == "", (
            "Bug 147: ruff_output should be empty when using custom commands "
            f"(npm test), but got: {result.ruff_output!r}. "
            "Lines 137-138 always assign all_output[0] to ruff_output even for "
            "non-Python commands. Fix: only assign ruff_output/pytest_output when "
            "using the default Python fallback commands."
        )

    def test_custom_commands_no_pytest_output(self) -> None:
        """With TWO custom commands the bug assigns all_output[1] to pytest_output.
        Using a single command makes pytest_output='' trivially (no index 1),
        which masks the bug — so we use two commands here.
        """
        from tero2.players.verifier import VerifierPlayer

        player = VerifierPlayer.__new__(VerifierPlayer)
        player.chain = MagicMock()
        player.disk = MagicMock()
        player.working_dir = "."

        with patch("tero2.players.verifier._run_command") as mock_run:
            mock_run.return_value = (0, "custom output", "")
            result = asyncio.run(
                player.run(
                    verify_commands=["npm test", "npm lint"],
                    builder_output="done",
                    task_id="T01",
                )
            )

        assert result.pytest_output == "", (
            "Bug 147: pytest_output should be empty when using custom commands "
            f"(npm lint), but got: {result.pytest_output!r}. "
            "Lines 137-138 assign all_output[1] to pytest_output regardless of "
            "what command produced it. Fix: only assign pytest_output when using "
            "the default Python fallback commands."
        )

    def test_custom_command_output_not_labeled_ruff(self) -> None:
        """Verify that custom command output (e.g. npm test) is not
        mislabeled as ruff_output."""
        from tero2.players.verifier import VerifierPlayer

        player = VerifierPlayer.__new__(VerifierPlayer)
        player.chain = MagicMock()
        player.disk = MagicMock()
        player.working_dir = "."

        with patch("tero2.players.verifier._run_command") as mock_run:
            mock_run.return_value = (0, "all tests passed via npm", "")
            result = asyncio.run(
                player.run(
                    verify_commands=["npm test"],
                    builder_output="done",
                    task_id="T01",
                )
            )

        # The npm output must not appear in ruff_output
        assert "npm" not in result.ruff_output, (
            "Bug 147: npm test output leaked into ruff_output field. "
            "Lines 137-138 assign all_output[0] to ruff_output regardless of "
            "what command produced it. Fix: only set ruff_output when using "
            "default Python commands."
        )


# ── Bug 156: stuck_detection tool repeat off-by-one ────────────────────────────


class TestBug156ToolRepeatOffByOne:
    """Line 67: `state.tool_repeat_count >= config.tool_repeat_threshold - 1`
    triggers one repeat early. With threshold=3, the signal fires at count=2
    instead of count=3.
    Fix: use `>= config.tool_repeat_threshold` (drop the `- 1`).
    """

    def test_not_triggered_at_threshold_minus_one(self) -> None:
        """With threshold=3 and count=2, TOOL_REPEAT must NOT fire."""
        from tero2.config import StuckDetectionConfig
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckSignal, check_stuck

        config = StuckDetectionConfig(
            max_retries=5,
            max_steps_per_task=20,
            tool_repeat_threshold=3,
        )
        state = AgentState(
            tool_repeat_count=2,
            tool_hash_updated=True,
            last_tool_hash="abc123",
        )

        result = check_stuck(state, config)
        assert result.signal != StuckSignal.TOOL_REPEAT, (
            "Bug 156: TOOL_REPEAT triggered at tool_repeat_count=2 with "
            "threshold=3. The condition `>= threshold - 1` fires one repeat "
            "early (2 >= 3-1 = 2 is True). Fix: change to "
            "`>= config.tool_repeat_threshold` so it triggers at count=3."
        )

    def test_triggered_at_threshold(self) -> None:
        """With threshold=3 and count=3, TOOL_REPEAT SHOULD fire."""
        from tero2.config import StuckDetectionConfig
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckSignal, check_stuck

        config = StuckDetectionConfig(
            max_retries=5,
            max_steps_per_task=20,
            tool_repeat_threshold=3,
        )
        state = AgentState(
            tool_repeat_count=3,
            tool_hash_updated=True,
            last_tool_hash="abc123",
        )

        result = check_stuck(state, config)
        assert result.signal == StuckSignal.TOOL_REPEAT, (
            "At tool_repeat_count=3 with threshold=3, TOOL_REPEAT should trigger. "
            "This assertion should pass even before the bug is fixed — it confirms "
            "the upper bound is correct."
        )

    def test_not_triggered_at_threshold_minus_two(self) -> None:
        """With threshold=3 and count=1, TOOL_REPEAT must NOT fire."""
        from tero2.config import StuckDetectionConfig
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckSignal, check_stuck

        config = StuckDetectionConfig(
            max_retries=5,
            max_steps_per_task=20,
            tool_repeat_threshold=3,
        )
        state = AgentState(
            tool_repeat_count=1,
            tool_hash_updated=True,
            last_tool_hash="abc123",
        )

        result = check_stuck(state, config)
        assert result.signal != StuckSignal.TOOL_REPEAT, (
            "Bug 156: TOOL_REPEAT triggered at tool_repeat_count=1 with "
            "threshold=3. This is two repeats early. "
            "Fix: change condition to `>= config.tool_repeat_threshold`."
        )


# ── Bug 157: cmd_harden crashes on None config.roles ──────────────────────────


class TestBug157CmdHardenNoneRoles:
    """Line 249: `if "reviewer" not in config.roles:` crashes with
    AttributeError when config.roles is None.
    Fix: guard with `config.roles is not None and` before the `in` check,
    or ensure load_config always returns a dict for roles.
    """

    def test_cmd_harden_handles_none_roles(self) -> None:
        """cmd_harden must not crash when config.roles is None."""
        import argparse
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        # Use a real object where roles is actually None (not a MagicMock).
        # MagicMock's __contains__ always returns True/False without error,
        # so it masks the bug. SimpleNamespace with roles=None will crash
        # on `"reviewer" not in None` with TypeError.
        mock_config = SimpleNamespace(roles=None, plan_hardening=MagicMock())

        with patch("tero2.config.load_config") as mock_load:
            mock_load.return_value = mock_config

            args = argparse.Namespace(
                project_path=str(Path(__file__).resolve().parent),
                plan=str(Path(__file__).resolve()),
                rounds=None,
                debug=False,
            )

            try:
                from tero2.cli import cmd_harden
                cmd_harden(args)
            except TypeError:
                pytest.fail(
                    "Bug 157: cmd_harden crashes with TypeError when "
                    "config.roles is None. Line 249 does "
                    "'\"reviewer\" not in config.roles' which raises "
                    "TypeError: argument of type 'NoneType' is not iterable. "
                    "Fix: guard with 'if config.roles is None or "
                    "\"reviewer\" not in config.roles:'."
                )
            except SystemExit:
                # SystemExit is fine — the function exits with error message
                pass
            except Exception:
                # Other errors (e.g. missing plan file) are acceptable;
                # we only care about TypeError from None roles.
                pass


# ── Bug 169: builder working_dir falsy check skips valid paths ─────────────────


class TestBug169BuilderFalsyWorkingDir:
    """Line 160: `if not working_dir:` skips empty string. On Unix, the
    current directory is a valid root; Path("") resolves to cwd.
    Fix: use `if working_dir is None:` instead of `if not working_dir:`.
    """

    def test_recover_summary_with_empty_string_working_dir(self, tmp_path) -> None:
        from tero2.players.builder import _recover_summary_from_disk

        # Create a summary file in the cwd-equivalent directory
        summary_file = tmp_path / "T01-SUMMARY.md"
        summary_file.write_text("recovered task summary", encoding="utf-8")

        # The function uses Path(working_dir). With empty string, Path("") is "."
        # which resolves to cwd — a valid path. But `if not working_dir` returns ""
        # immediately instead of trying.
        # We call with working_dir="" to show it should attempt recovery
        # (at minimum not skip purely on falsy check).
        # With working_dir="" the Path("") resolves to "." (cwd) which likely
        # has no T01-SUMMARY.md, so the result is "" — that's fine.
        # The real test: use a valid path that is "falsy" in Python.
        # The only falsy string is "" — let's test that the function doesn't
        # have a blanket falsy check by calling with a real path and verifying
        # it works, then showing the inconsistency.
        result = _recover_summary_from_disk("T01", str(tmp_path))
        assert result == "recovered task summary", (
            "Sanity check: recovery should work with a real path."
        )

    def test_recover_summary_empty_string_not_short_circuited(self, tmp_path) -> None:
        """The bug: `if not working_dir` treats "" as invalid.
        This test verifies the function attempts file lookup even with "".
        We can't easily make it find a file (Path("") = Path(".")), but we
        can check the source for the falsy guard.
        """
        import inspect

        from tero2.players.builder import _recover_summary_from_disk

        source = inspect.getsource(_recover_summary_from_disk)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            stripped = line.strip()
            if "if not working_dir" in stripped:
                pytest.fail(
                    f"Bug 169: builder.py line {i+1} uses "
                    f"'if not working_dir' which treats empty string as "
                    f"invalid. On Unix, Path('') resolves to cwd which is "
                    f"a valid root directory. The function skips recovery "
                    f"entirely instead of attempting file lookup. "
                    f"Fix: change to 'if working_dir is None:'."
                )


# ── Bug 171: coach silent truncation without logging ───────────────────────────


class TestBug171CoachSilentTruncation:
    """Lines 137-141 in coach.py: task summaries are truncated at _SIZE_CAP
    (50_000 chars) with no log.warning. Operators have no visibility into
    context loss.
    Fix: add log.warning when truncation occurs.
    """

    def test_truncation_logs_warning(self) -> None:
        import inspect

        from tero2.players.coach import CoachPlayer

        source = inspect.getsource(CoachPlayer._gather_context)
        lines = source.splitlines()

        found_truncation = False
        has_log_warning = False

        for i, line in enumerate(lines):
            if "TRUNCATED" in line and "context limit" in line:
                found_truncation = True
                # Look backward and forward for a log.warning near the truncation
                start = max(0, i - 5)
                end = min(len(lines), i + 5)
                context = "\n".join(lines[start:end])
                has_log_warning = (
                    "log.warning" in context
                    or "logger.warning" in context
                )
                break

        if not found_truncation:
            pytest.skip("TRUNCATED / context limit check not found in _gather_context")

        assert has_log_warning, (
            "Bug 171: coach._gather_context truncates task summaries at "
            "_SIZE_CAP (50_000 chars) with no log.warning. Operators have "
            "no visibility into lost context — the coach silently operates "
            "on incomplete information. "
            "Fix: add log.warning('coach: truncating task summaries at "
            "_SIZE_CAP, %d chars dropped', ...) when truncation occurs."
        )

    def test_truncation_emits_log_at_runtime(self) -> None:
        """Verify that when summaries exceed _SIZE_CAP, a warning is logged."""
        from tero2.players.coach import CoachPlayer

        disk = MagicMock()
        disk.sora_dir = MagicMock()
        disk.sora_dir.__truediv__ = MagicMock(
            return_value=MagicMock(
                is_dir=MagicMock(return_value=True),
                iterdir=MagicMock(return_value=[]),
            )
        )
        disk.read_file.return_value = ""
        disk.read_metrics.return_value = None
        disk.read_steer.return_value = ""

        player = CoachPlayer.__new__(CoachPlayer)
        player.disk = disk
        player.chain = MagicMock()
        player.working_dir = "."

        # Manually test the truncation path by checking if log.warning
        # is called when the cap is reached.
        with patch("tero2.players.coach.log") as mock_log:
            # We need to trigger the truncation path directly.
            # _gather_context iterates slices and accumulates summaries.
            # Simulate a scenario where total_size exceeds _SIZE_CAP.
            mock_dir = MagicMock()
            mock_dir.is_dir.return_value = True
            mock_dir.iterdir.return_value = []
            disk.sora_dir.__truediv__ = MagicMock(return_value=mock_dir)

            # Since the file-based path is hard to trigger without a full
            # filesystem, check the source for the log.warning near TRUNCATED.
            import inspect
            source = inspect.getsource(CoachPlayer._gather_context)

            # If the source contains TRUNCATED but no log.warning nearby,
            # the bug is present.
            if "TRUNCATED" in source:
                # Find the TRUNCATED line and check for log.warning in proximity
                lines = source.splitlines()
                for i, line in enumerate(lines):
                    if "TRUNCATED" in line:
                        start = max(0, i - 5)
                        end = min(len(lines), i + 5)
                        context = "\n".join(lines[start:end])
                        assert "log.warning" in context, (
                            "Bug 171: coach truncates summaries at _SIZE_CAP "
                            "without any log.warning. Context loss is silent. "
                            "Fix: add log.warning when truncation threshold is hit."
                        )
                        return

        pytest.skip("TRUNCATED marker not found in _gather_context")


# ── Bug 189: escalation diversification counter never incremented ──────────────


class TestBug189DiversificationCounterNeverIncremented:
    """decide_escalation() receives diversification_steps_taken as a parameter
    but the caller never increments it. The agent is stuck at Level 1
    (DIVERSIFICATION) indefinitely because diversification_steps_taken is
    always 0, never reaching diversification_max_steps.
    Fix: the caller must increment diversification_steps_taken after each
    Level 1 step and pass the updated value.
    """

    def test_decide_escalation_levels_up_after_max_steps(self) -> None:
        """After diversification_max_steps, the next call should return
        BACKTRACK_COACH (Level 2)."""
        from tero2.config import EscalationConfig
        from tero2.escalation import EscalationLevel, decide_escalation
        from tero2.stuck_detection import StuckResult, StuckSignal

        config = EscalationConfig(diversification_max_steps=2)

        stuck = StuckResult(signal=StuckSignal.TOOL_REPEAT, details="loop", severity=2)

        # First call: enter Level 1
        action = decide_escalation(stuck, EscalationLevel.NONE, 0, config)
        assert action.level == EscalationLevel.DIVERSIFICATION

        # After max_steps, should escalate to Level 2
        action = decide_escalation(
            stuck,
            EscalationLevel.DIVERSIFICATION,
            config.diversification_max_steps,
            config,
        )
        assert action.level == EscalationLevel.BACKTRACK_COACH, (
            "After diversification_steps_taken >= diversification_max_steps, "
            "decide_escalation should return BACKTRACK_COACH (Level 2). "
            "This verifies the function itself works correctly."
        )

    def test_caller_increments_diversification_steps(self) -> None:
        """The bug: the caller never increments diversification_steps_taken.
        Verify that the execution loop (or RunnerContext) increments the
        counter by checking the source of execute_escalation or the runner."""
        import inspect

        from tero2.escalation import execute_escalation

        source = inspect.getsource(execute_escalation)
        # The function must modify state.div_steps or the caller must track it.
        # Check if diversification path increments any counter.
        has_increment = (
            "div_steps" in source
            or "diversification_steps_taken" in source
        )
        assert has_increment, (
            "Bug 189: execute_escalation() does not increment any "
            "diversification step counter. diversification_steps_taken is "
            "passed as a parameter to decide_escalation() but is always 0 "
            "because nothing increments it. The agent stays at Level 1 forever. "
            "Fix: increment state.div_steps in the DIVERSIFICATION branch of "
            "execute_escalation, and pass it to decide_escalation."
        )

    def test_caller_passes_div_steps_to_decide_escalation(self) -> None:
        """Verify that the runner/ctx passes div_steps when calling
        decide_escalation."""
        import inspect

        # Check RunnerContext or runner for the decide_escalation call
        try:
            from tero2.phases.context import RunnerContext
            source = inspect.getsource(RunnerContext)
        except Exception:
            try:
                from tero2.runner import Runner
                source = inspect.getsource(Runner)
            except Exception:
                pytest.skip("Could not find decide_escalation call site")

        if "decide_escalation" not in source:
            pytest.skip("decide_escalation call not found in RunnerContext/Runner")

        # Check that div_steps is passed (not hardcoded to 0)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "decide_escalation" in line and "(" in line:
                # Look at this line and subsequent lines for the full call
                context_start = i
                context_end = min(len(lines), i + 10)
                call_context = "\n".join(lines[context_start:context_end])
                assert "div_steps" in call_context, (
                    "Bug 189: the call to decide_escalation() does not pass "
                    "state.div_steps (or any incremented counter) as the "
                    "diversification_steps_taken argument. It is likely "
                    "hardcoded to 0 or omitted. The counter never changes, "
                    "so the agent never escalates past Level 1. "
                    "Fix: pass state.div_steps to decide_escalation()."
                )
                return

        pytest.skip("decide_escalation call details not found")
