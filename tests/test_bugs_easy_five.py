"""Tests for Bugs 43, 50, 63, 69, 74 — easy fixes."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ── Bug 43: _check_override uses loose "STOP"/"PAUSE" matching ──────────


class TestBug43CheckOverrideLooseMatch:
    """Bug 43: _check_override must require STOP/PAUSE on their own line."""

    @pytest.fixture()
    def ctx(self):
        from tero2.phases.context import RunnerContext

        ctx = RunnerContext()
        ctx.disk = MagicMock()
        ctx.checkpoint = MagicMock()
        return ctx

    def test_stopped_as_substring_does_not_trigger_stop(self, ctx):
        from tero2.phases.execute_phase import _check_override

        ctx.disk.read_override.return_value = "# Process STOPPED"
        result = _check_override(ctx, "S1", {})
        assert result is None, "'STOPPED' as substring must not trigger STOP"

    def test_unstoppable_does_not_trigger_stop(self, ctx):
        from tero2.phases.execute_phase import _check_override

        ctx.disk.read_override.return_value = "unstoppable progress"
        result = _check_override(ctx, "S1", {})
        assert result is None, "'unstoppable' must not trigger STOP"

    def test_paused_pending_does_not_trigger_pause(self, ctx):
        from tero2.phases.execute_phase import _check_override

        ctx.disk.read_override.return_value = "Review PAUSED pending approval"
        result = _check_override(ctx, "S1", {})
        assert result is None, "'PAUSED' as substring must not trigger PAUSE"

    def test_stop_alone_on_line_triggers_stop(self, ctx):
        from tero2.phases.execute_phase import _check_override

        ctx.disk.read_override.return_value = "STOP"
        result = _check_override(ctx, "S1", {})
        assert result is not None
        assert "STOP" in result.error

    def test_pause_alone_on_line_triggers_pause(self, ctx):
        from tero2.phases.execute_phase import _check_override

        ctx.disk.read_override.return_value = "PAUSE"
        result = _check_override(ctx, "S1", {})
        assert result is not None
        assert "PAUSE" in result.error


# ── Bug 50: plan_file.read_text() without FileNotFoundError handling ────


class TestBug50PlanFileReadTextNoGuard:
    """Bug 50: _execute_sora must catch FileNotFoundError with a clear message."""

    @pytest.mark.asyncio()
    async def test_missing_plan_file_gives_clear_error(self):
        from pathlib import Path

        from tero2.runner import Runner

        runner = Runner.__new__(Runner)
        runner.plan_file = Path("/no/such/plan/file.md")
        runner.config = MagicMock()
        runner.config.roles = {}
        runner.checkpoint = MagicMock()
        runner.disk = MagicMock()
        runner.notifier = MagicMock()
        runner.cb_registry = MagicMock()
        runner.project_path = Path("/tmp")
        runner._dispatcher = None
        runner._command_queue = None

        state = MagicMock()
        state.sora_phase = None

        with pytest.raises(FileNotFoundError, match="plan file not found"):
            await runner._execute_sora(state)


# ── Bug 63: _parse_verdict false positive ANOMALY ───────────────────────


class TestBug63ParseVerdictAnomalyFalsePositive:
    """Bug 63: _parse_verdict must not fire ANOMALY on test names containing 'anomaly'."""

    def test_anomaly_in_test_name_with_rc0_returns_pass(self):
        from tero2.players.verifier import _parse_verdict, Verdict

        output = "test_anomaly_detection PASSED\n2 passed"
        assert _parse_verdict(output, [0, 0]) == Verdict.PASS

    def test_anomaly_in_module_path_with_rc0_returns_pass(self):
        from tero2.players.verifier import _parse_verdict, Verdict

        output = "tests/test_anomaly_utils.py::test_ok PASSED"
        assert _parse_verdict(output, [0, 0]) == Verdict.PASS

    def test_real_anomaly_keyword_returns_anomaly(self):
        from tero2.players.verifier import _parse_verdict, Verdict

        output = "ANOMALY detected in output"
        assert _parse_verdict(output, [0, 0]) == Verdict.ANOMALY


# ── Bug 69: BrokenPipeError on stdin write ──────────────────────────────


class TestBug69StdinWriteNoBrokenPipeGuard:
    """Bug 69: CLIProvider.run must catch BrokenPipeError and raise ProviderError."""

    @pytest.mark.asyncio()
    async def test_broken_pipe_on_stdin_write_raises_provider_error(self):
        from tero2.errors import ProviderError
        from tero2.providers.cli import CLIProvider

        provider = CLIProvider("claude", MagicMock())

        async def fake_create(*a, **kw):
            proc = MagicMock()

            # stdin that raises BrokenPipeError on write
            stdin = MagicMock()
            stdin.write.side_effect = BrokenPipeError("pipe closed")

            async def drain_broken():
                raise BrokenPipeError("pipe closed")

            stdin.drain = drain_broken
            stdin.close = MagicMock()

            async def wait_closed():
                pass

            stdin.wait_closed = wait_closed
            proc.stdin = stdin

            # stdout as async iterator (empty)
            proc.stdout = MagicMock()

            async def empty_lines():
                return
                yield  # makes empty_lines() an async generator, not a coroutine

            proc.stdout.__aiter__ = lambda self: empty_lines()

            # stderr
            proc.stderr = MagicMock()

            async def read_stderr():
                return b""

            proc.stderr.read = read_stderr
            proc.returncode = 0
            return proc

        with patch("asyncio.create_subprocess_exec", side_effect=fake_create):
            with pytest.raises(ProviderError, match="[Bb]roken [Pp]ipe"):
                async for _ in provider.run(prompt="test", stdin_data=b"hello"):
                    pass


# ── Bug 74: empty tasks list silent success ─────────────────────────────


class TestBug74EmptyTasksSilentSuccess:
    """Bug 74: run_execute with empty tasks must return failure, not silent success."""

    @pytest.mark.asyncio()
    async def test_empty_tasks_returns_failure(self):
        from tero2.phases.context import RunnerContext
        from tero2.phases.execute_phase import run_execute
        from tero2.players.architect import SlicePlan

        ctx = RunnerContext()
        ctx.disk = MagicMock()
        ctx.disk.project_path = MagicMock()
        ctx.disk.project_path.__str__ = lambda s: "/tmp"
        ctx.checkpoint = MagicMock()
        ctx.notifier = MagicMock()
        ctx.config = MagicMock()
        ctx.config.reflexion.max_cycles = 0
        ctx.personas = MagicMock()
        ctx.state = MagicMock()
        ctx.state.current_task_index = 0
        ctx.state.task_in_progress = False

        empty_plan = SlicePlan(slice_id="S1", slice_dir="/tmp", tasks=[])
        result = await run_execute(ctx, empty_plan)
        assert result.success is False, "Empty tasks must not return success"
