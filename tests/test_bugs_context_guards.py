"""Negative tests for bugs 26–29: verifier check order + RunnerContext None guards.

Each test is written RED first — it fails against the current buggy code and
passes after the corresponding fix is applied.

Bug 26 — tero2/players/verifier.py:_parse_verdict
Bug 27 — tero2/phases/context.py:_heartbeat_loop (notifier None guard)
Bug 28 — tero2/phases/context.py:build_chain (disk None guard)
Bug 29 — tero2/phases/context.py:run_agent (checkpoint None guard)
"""

from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 26: _parse_verdict wrong check order ──────────────────────────────


class TestBug26ParseVerdictCheckOrder:
    """Bug 26: empty output + rc=0,0 must return PASS, not ANOMALY.

    Current code checks `not output.strip()` before the rc check.
    When ruff runs clean (no output, rc=0) and pytest exits 0 with minimal
    output, the combined string strips to "" — the empty-output guard fires
    first and returns ANOMALY instead of PASS.
    """

    def test_empty_output_both_rc_zero_returns_pass(self):
        """_parse_verdict('', [0, 0]) must return PASS, not ANOMALY."""
        from tero2.players.verifier import Verdict, _parse_verdict

        verdict = _parse_verdict("", [0, 0])
        assert verdict == Verdict.PASS, (
            "Bug 26: empty output with all rc=0 must return PASS. "
            f"Got: {verdict!r}"
        )

    def test_newline_only_output_both_rc_zero_returns_pass(self):
        """ruff on a clean codebase produces no output; combined becomes '\\n'."""
        from tero2.players.verifier import Verdict, _parse_verdict

        verdict = _parse_verdict("\n", [0, 0])
        assert verdict == Verdict.PASS, (
            "Bug 26: '\\n' output with rc=[0,0] must be PASS. "
            f"Got: {verdict!r}"
        )

    def test_empty_output_nonzero_ruff_rc_returns_fail(self):
        """After fix (rc check first), empty output + ruff_rc=1 must be FAIL."""
        from tero2.players.verifier import Verdict, _parse_verdict

        verdict = _parse_verdict("", [1, 0])
        assert verdict == Verdict.FAIL, (
            "Bug 26: empty output + rc=[1,0] must return FAIL. "
            f"Got: {verdict!r}"
        )


# ── Bug 27: _heartbeat_loop None notifier ──────────────────────────────────


class TestBug27HeartbeatNoneNotifier:
    """Bug 27: _heartbeat_loop must silently skip notify when notifier is None.

    `notifier` is declared `Notifier | None = None`.  The heartbeat loop calls
    `self.notifier.notify(...)` with no None guard, crashing with AttributeError
    whenever a RunnerContext is constructed without a notifier.
    """

    async def test_heartbeat_loop_does_not_crash_with_none_notifier(self):
        """heartbeat_loop with notifier=None must not raise AttributeError.

        Currently fails with:
            AttributeError: 'NoneType' object has no attribute 'notify'
        """
        from tero2.config import Config, TelegramConfig
        from tero2.phases.context import RunnerContext
        from tero2.state import AgentState

        config = Config()
        config.telegram = TelegramConfig(heartbeat_interval_s=0)
        ctx = RunnerContext(config=config, notifier=None)
        state_ref = [AgentState()]

        task = asyncio.create_task(ctx._heartbeat_loop(state_ref))
        await asyncio.sleep(0)  # yield — task reaches its asyncio.sleep(0)
        await asyncio.sleep(0)  # yield — task runs the loop body (notify call)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass  # expected: loop cancelled cleanly after the fix

    def test_heartbeat_loop_source_guards_notifier(self):
        """Source must guard the notify call with `if self.notifier is not None:`."""
        from tero2.phases.context import RunnerContext

        source = inspect.getsource(RunnerContext._heartbeat_loop)
        assert "notifier is not None" in source, (
            "Bug 27: _heartbeat_loop calls `self.notifier.notify()` unconditionally. "
            "Must add `if self.notifier is not None:` guard before the call."
        )


# ── Bug 28: build_chain None disk ─────────────────────────────────────────


class TestBug28BuildChainNoneDisk:
    """Bug 28: build_chain must not crash when disk is None.

    `disk` is declared `DiskLayer | None = None`.  build_chain accesses
    `self.disk.project_path` without a None check; a minimal RunnerContext
    (e.g. for `cmd_harden`) that omits `disk` crashes immediately.
    """

    def test_build_chain_with_none_disk_does_not_crash(self):
        """build_chain with disk=None must use '' as working_dir, not raise.

        Currently fails with:
            AttributeError: 'NoneType' object has no attribute 'project_path'
        """
        from tero2.config import Config, RoleConfig
        from tero2.phases.context import RunnerContext

        config = Config()
        config.roles["builder"] = RoleConfig(provider="cli")
        ctx = RunnerContext(config=config, disk=None)

        with patch("tero2.phases.context.create_provider", return_value=MagicMock()):
            ctx.build_chain("builder")  # must not raise AttributeError

    def test_build_chain_none_disk_passes_empty_working_dir(self):
        """With disk=None, create_provider must receive working_dir=''."""
        from tero2.config import Config, RoleConfig
        from tero2.phases.context import RunnerContext

        config = Config()
        config.roles["builder"] = RoleConfig(provider="cli")
        ctx = RunnerContext(config=config, disk=None)

        with patch("tero2.phases.context.create_provider", return_value=MagicMock()) as mock_cp:
            ctx.build_chain("builder")

        assert mock_cp.called, "create_provider was never called"
        call_kwargs = mock_cp.call_args_list[0][1]
        assert call_kwargs.get("working_dir") == "", (
            "Bug 28: with disk=None, working_dir must be '', "
            f"got: {call_kwargs.get('working_dir')!r}"
        )

    def test_build_chain_source_guards_disk(self):
        """Source must contain a None check before accessing self.disk.project_path."""
        from tero2.phases.context import RunnerContext

        source = inspect.getsource(RunnerContext.build_chain)
        assert "disk is not None" in source, (
            "Bug 28: build_chain accesses `self.disk.project_path` without a None check. "
            "Must add `if self.disk is not None else ''` guard."
        )


# ── Bug 29: run_agent None checkpoint ─────────────────────────────────────


class TestBug29RunAgentNoneCheckpoint:
    """Bug 29: run_agent must not crash when checkpoint is None.

    `checkpoint` is declared `CheckpointManager | None = None`.  run_agent
    calls `self.checkpoint.increment_step(state)` on every tool_result or
    turn_end message with no None check.
    """

    async def test_run_agent_with_none_checkpoint_does_not_crash(self):
        """run_agent with checkpoint=None must skip increment_step and return.

        Currently fails with:
            AttributeError: 'NoneType' object has no attribute 'increment_step'
        """
        from tero2.config import Config, RoleConfig, TelegramConfig
        from tero2.phases.context import RunnerContext
        from tero2.state import AgentState

        config = Config()
        config.roles["test_role"] = RoleConfig(provider="cli", timeout_s=5)
        config.telegram = TelegramConfig()
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=True)

        ctx = RunnerContext(
            config=config,
            checkpoint=None,
            notifier=notifier,
            state=AgentState(),
        )

        class _TurnEndChain:
            current_provider_index = 0

            async def run_prompt(self, prompt: str):
                yield {"type": "turn_end", "content": "done"}

        # Currently raises: AttributeError: 'NoneType' has no attribute 'increment_step'
        await ctx.run_agent(_TurnEndChain(), "test", role="test_role")

    async def test_run_agent_none_checkpoint_returns_success(self):
        """run_agent must return (True, output) when chain completes and checkpoint is None."""
        from tero2.config import Config, RoleConfig, TelegramConfig
        from tero2.phases.context import RunnerContext
        from tero2.state import AgentState

        config = Config()
        config.roles["test_role"] = RoleConfig(provider="cli", timeout_s=5)
        config.telegram = TelegramConfig()
        notifier = MagicMock()
        notifier.notify = AsyncMock(return_value=True)

        ctx = RunnerContext(
            config=config,
            checkpoint=None,
            notifier=notifier,
            state=AgentState(),
        )

        class _TurnEndChain:
            current_provider_index = 0

            async def run_prompt(self, prompt: str):
                yield {"type": "turn_end", "content": "done"}

        success, _output = await ctx.run_agent(_TurnEndChain(), "test", role="test_role")
        assert success, (
            "run_agent with checkpoint=None must return success=True on a clean turn"
        )

    def test_run_agent_source_guards_checkpoint(self):
        """Source must contain `if self.checkpoint is not None:` before increment_step."""
        from tero2.phases.context import RunnerContext

        source = inspect.getsource(RunnerContext.run_agent)
        assert "checkpoint is not None" in source, (
            "Bug 29: run_agent calls `self.checkpoint.increment_step(state)` "
            "unconditionally. Must add `if self.checkpoint is not None:` guard."
        )
