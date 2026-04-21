"""Tests for RunnerContext.run_agent — streaming integration.

Covers:
  Step 1 — Successful run: (True, collected output) returned
  Step 2 — tool_result events trigger step counting via checkpoint
  Step 3 — turn_end event completes the run successfully
  Step 4 — STEP_LIMIT stuck signal aborts early with (False, partial)
  Step 5 — TOOL_REPEAT stuck signal aborts early with (False, partial)
  Step 6 — max_steps_per_task threshold aborts run with (False, partial)
  Step 7 — TimeoutError returns (False, partial) without re-raising
  Step 8 — RateLimitError returns (False, partial) without re-raising
  Step 9 — Heartbeat task is cancelled even on failure paths
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.checkpoint import CheckpointManager
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, RetryConfig, RoleConfig, StuckDetectionConfig
from tero2.disk_layer import DiskLayer
from tero2.errors import RateLimitError
from tero2.phases.context import RunnerContext
from tero2.providers.chain import ProviderChain
from tero2.state import AgentState, Phase
from tero2.stuck_detection import StuckSignal


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_config(*, max_steps: int = 50, timeout_s: float = 30.0) -> Config:
    cfg = Config()
    cfg.roles["executor"] = RoleConfig(provider="fake", timeout_s=timeout_s)
    cfg.retry = RetryConfig(max_steps_per_task=max_steps)
    cfg.stuck_detection = StuckDetectionConfig(
        max_steps_per_task=max_steps,
        max_retries=10,
        tool_repeat_threshold=5,
    )
    return cfg


def _make_ctx(tmp_path: Path, config: Config | None = None) -> RunnerContext:
    cfg = config or _make_config()
    disk = DiskLayer(tmp_path)
    disk.init()
    checkpoint = CheckpointManager(disk, max_steps_per_task=cfg.retry.max_steps_per_task)
    state = AgentState(phase=Phase.RUNNING)
    return RunnerContext(
        config=cfg,
        disk=disk,
        checkpoint=checkpoint,
        state=state,
        cb_registry=CircuitBreakerRegistry(),
        project_path=str(tmp_path),
    )


class _FakeChain:
    """Minimal provider chain: emits a pre-configured sequence of messages."""

    current_provider_index = 0

    def __init__(self, messages: list[Any]) -> None:
        self._messages = messages

    async def run_prompt(self, prompt: str):
        for msg in self._messages:
            yield msg


# ── Step 1: Successful run ────────────────────────────────────────────────────


class TestRunAgentSuccess:
    async def test_returns_true_with_collected_output(self, tmp_path: Path) -> None:
        """A plain text message followed by turn_end should return (True, text)."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([
            "hello from the agent",
            {"type": "turn_end", "text": ""},
        ])
        ok, output = await ctx.run_agent(chain, "do the thing")
        assert ok is True
        assert "hello from the agent" in output

    async def test_dict_text_content_collected(self, tmp_path: Path) -> None:
        """Dict messages with 'text' key are included in collected output."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([
            {"type": "text", "text": "step one complete"},
            {"type": "turn_end", "text": ""},
        ])
        ok, output = await ctx.run_agent(chain, "build something")
        assert ok is True
        assert "step one complete" in output

    async def test_dict_content_key_collected(self, tmp_path: Path) -> None:
        """Dict messages with 'content' key (not 'text') are also collected."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([
            {"type": "text", "content": "content key value"},
            {"type": "turn_end", "text": ""},
        ])
        ok, output = await ctx.run_agent(chain, "run")
        assert ok is True
        assert "content key value" in output

    async def test_empty_messages_returns_empty_output(self, tmp_path: Path) -> None:
        """A chain that yields nothing produces (True, '')."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([])
        ok, output = await ctx.run_agent(chain, "empty run")
        assert ok is True
        assert output == ""


# ── Step 2: tool_result events trigger step counting ─────────────────────────


class TestRunAgentStepCounting:
    async def test_tool_result_increments_steps_in_task(self, tmp_path: Path) -> None:
        """Each tool_result *and* turn_end event calls checkpoint.increment_step."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([
            {"type": "tool_result", "content": "tool output A"},
            {"type": "tool_result", "content": "tool output B"},
            {"type": "turn_end", "text": ""},
        ])
        assert ctx.state.steps_in_task == 0
        await ctx.run_agent(chain, "run tools")
        # 2 tool_results + 1 turn_end each call increment_step → 3 total
        assert ctx.state.steps_in_task == 3

    async def test_tool_result_without_turn_end_still_counts(self, tmp_path: Path) -> None:
        """Step counting must not require turn_end to fire."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([
            {"type": "tool_result", "content": "result"},
        ])
        await ctx.run_agent(chain, "run one tool")
        assert ctx.state.steps_in_task >= 1


# ── Step 3: turn_end completes successfully ───────────────────────────────────


class TestRunAgentTurnEnd:
    async def test_turn_end_increments_step_count(self, tmp_path: Path) -> None:
        """turn_end also calls increment_step (consistent with implementation)."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([
            {"type": "turn_end", "text": ""},
        ])
        await ctx.run_agent(chain, "turn end only")
        # turn_end triggers one increment_step call
        assert ctx.state.steps_in_task == 1

    async def test_turn_end_returns_success(self, tmp_path: Path) -> None:
        """A lone turn_end produces (True, '')."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([{"type": "turn_end", "text": ""}])
        ok, output = await ctx.run_agent(chain, "p")
        assert ok is True


# ── Step 4: STEP_LIMIT aborts early ──────────────────────────────────────────


class TestRunAgentStepLimit:
    async def test_step_limit_returns_false(self, tmp_path: Path) -> None:
        """When stuck detection fires STEP_LIMIT, run_agent returns (False, partial)."""
        # max_steps=1 → after first tool_result the limit is hit
        cfg = _make_config(max_steps=1)
        ctx = _make_ctx(tmp_path, config=cfg)
        chain = _FakeChain([
            {"type": "tool_result", "content": "step1"},
            {"type": "tool_result", "content": "step2"},
            {"type": "turn_end", "text": ""},
        ])
        ok, output = await ctx.run_agent(chain, "long task")
        assert ok is False
        assert "step1" in output

    async def test_step_limit_output_is_partial(self, tmp_path: Path) -> None:
        """Output collected before abort must be preserved in the return value."""
        cfg = _make_config(max_steps=1)
        ctx = _make_ctx(tmp_path, config=cfg)
        chain = _FakeChain([
            "preamble text",
            {"type": "tool_result", "content": "step-one"},
            {"type": "tool_result", "content": "step-two"},
        ])
        ok, output = await ctx.run_agent(chain, "run limited")
        assert ok is False
        assert "preamble text" in output


# ── Step 5: TOOL_REPEAT aborts early ─────────────────────────────────────────


class TestRunAgentToolRepeat:
    async def test_tool_repeat_returns_false(self, tmp_path: Path) -> None:
        """When TOOL_REPEAT fires, run_agent returns (False, partial) — not just any tuple.

        With tool_repeat_threshold=1, check_stuck fires TOOL_REPEAT after the second
        identical tool_result (tool_repeat_count reaches 1 >= threshold-1 == 0, and
        tool_repeat_count > 0). run_agent must abort early with ok=False and preserve
        the partial output captured before the abort.
        """
        cfg = _make_config(max_steps=50)
        cfg.stuck_detection = StuckDetectionConfig(
            max_steps_per_task=50,
            max_retries=10,
            tool_repeat_threshold=1,  # fires on first repeat
        )
        ctx = _make_ctx(tmp_path, config=cfg)

        repeated_content = "identical-output"
        chain = _FakeChain([
            {"type": "tool_result", "content": repeated_content},
            {"type": "tool_result", "content": repeated_content},  # triggers TOOL_REPEAT
            {"type": "turn_end", "text": ""},  # must not be reached — abort fires first
        ])
        ok, output = await ctx.run_agent(chain, "repeat tools")
        # TOOL_REPEAT must abort early → False, partial output captured before abort
        assert ok is False
        assert repeated_content in output


# ── Step 6: max_steps_per_task checkpoint abort ───────────────────────────────


class TestRunAgentCheckpointStepLimit:
    async def test_checkpoint_max_steps_aborts(self, tmp_path: Path) -> None:
        """Secondary step-limit guard (context.py:196) fires independently of check_stuck().

        Isolation strategy: set stuck_detection.max_steps_per_task=100 so check_stuck()
        returns NONE for STEP_LIMIT (primary path is not taken), while
        checkpoint.max_steps_per_task=2 (from cfg.retry.max_steps_per_task). With
        steps_in_task pre-set to 1, one increment_step call brings it to 2 == max,
        triggering the secondary guard at context.py:196 rather than context.py:193.
        """
        cfg = _make_config(max_steps=2)  # checkpoint.max_steps_per_task=2
        # Override stuck_detection so check_stuck() will NOT fire STEP_LIMIT first,
        # isolating the secondary checkpoint guard.
        cfg.stuck_detection = StuckDetectionConfig(
            max_steps_per_task=100,  # far above checkpoint limit — primary path skipped
            max_retries=10,
            tool_repeat_threshold=5,
        )
        ctx = _make_ctx(tmp_path, config=cfg)
        # Pre-fill so that after one increment_step, steps_in_task == 2 == max
        ctx.state.steps_in_task = 1
        chain = _FakeChain([
            {"type": "tool_result", "content": "any"},
            {"type": "turn_end", "text": ""},
        ])
        ok, _ = await ctx.run_agent(chain, "already at limit")
        assert ok is False


# ── Step 7: TimeoutError handled gracefully ───────────────────────────────────


class TestRunAgentTimeout:
    async def test_timeout_returns_false(self, tmp_path: Path) -> None:
        """asyncio.TimeoutError must be caught and returned as (False, partial)."""
        cfg = _make_config(timeout_s=0.001)  # almost-zero timeout → fires instantly
        ctx = _make_ctx(tmp_path, config=cfg)

        async def _slow_chain():
            await asyncio.sleep(10)
            yield {"type": "turn_end"}

        class _SlowChain:
            current_provider_index = 0

            async def run_prompt(self, prompt: str):
                await asyncio.sleep(10)
                yield {"type": "turn_end"}

        ok, output = await ctx.run_agent(_SlowChain(), "slow")
        assert ok is False
        assert isinstance(output, str)


# ── Step 8: RateLimitError handled gracefully ────────────────────────────────


class TestRunAgentRateLimit:
    async def test_rate_limit_error_returns_false(self, tmp_path: Path) -> None:
        """RateLimitError from chain must be swallowed and return (False, partial)."""
        ctx = _make_ctx(tmp_path)

        class _RateLimitChain:
            current_provider_index = 0

            async def run_prompt(self, prompt: str):
                raise RateLimitError("all providers exhausted")
                yield  # make it a generator

        ok, output = await ctx.run_agent(_RateLimitChain(), "anything")
        assert ok is False
        assert isinstance(output, str)


# ── Step 9: Heartbeat task cleanup ───────────────────────────────────────────


class TestRunAgentHeartbeatCleanup:
    async def test_heartbeat_cancelled_on_success(self, tmp_path: Path) -> None:
        """run_agent must not leave a pending heartbeat task after returning."""
        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([{"type": "turn_end", "text": ""}])

        tasks_before = len([t for t in asyncio.all_tasks() if not t.done()])
        await ctx.run_agent(chain, "clean run")
        # Give event loop a tick to let cancellation propagate
        await asyncio.sleep(0)
        tasks_after = len([t for t in asyncio.all_tasks() if not t.done()])
        assert tasks_after <= tasks_before, (
            "run_agent left uncancelled background tasks"
        )

    async def test_heartbeat_cancelled_on_failure(self, tmp_path: Path) -> None:
        """Heartbeat task must also be cleaned up when run_agent returns False."""
        ctx = _make_ctx(tmp_path)

        class _ErrorChain:
            current_provider_index = 0

            async def run_prompt(self, prompt: str):
                raise RateLimitError("exhausted")
                yield

        tasks_before = len([t for t in asyncio.all_tasks() if not t.done()])
        await ctx.run_agent(_ErrorChain(), "error run")
        await asyncio.sleep(0)
        tasks_after = len([t for t in asyncio.all_tasks() if not t.done()])
        assert tasks_after <= tasks_before


# ── role kwarg forwarded to timeout config ────────────────────────────────────


class TestRunAgentRoleTimeout:
    async def test_role_kwarg_selects_correct_timeout(self, tmp_path: Path) -> None:
        """run_agent(role='executor') must use the executor role timeout, not the default."""
        cfg = _make_config(timeout_s=30.0)
        ctx = _make_ctx(tmp_path, config=cfg)
        chain = _FakeChain([{"type": "turn_end", "text": ""}])
        # Should complete without timing out (executor timeout is 30s, run is instant)
        ok, _ = await ctx.run_agent(chain, "prompt", role="executor")
        assert ok is True

    async def test_unknown_role_uses_hard_timeout(self, tmp_path: Path) -> None:
        """An unknown role falls back to HARD_TIMEOUT_S without raising ConfigError."""
        from tero2.constants import HARD_TIMEOUT_S

        ctx = _make_ctx(tmp_path)
        chain = _FakeChain([{"type": "turn_end", "text": ""}])
        # Should not raise — HARD_TIMEOUT_S is used as the fallback
        ok, _ = await ctx.run_agent(chain, "prompt", role="nonexistent_role")
        assert ok is True
