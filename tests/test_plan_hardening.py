"""Tests for plan hardening convergence behaviour.

Complements test_phases.py::TestRunHarden which covers:
  - no plan found → failure
  - chain build failure
  - NO ISSUES FOUND immediate convergence
  - COSMETIC with stop_on_cosmetic=True → stops
  - two consecutive malformed → stops
  - _parse_verdict / _combine_prompt internals

This file covers the remaining gaps:
  - single malformed review → treated as CRITICAL (fix pass runs)
  - max_rounds exhausted → returns success with last best-effort plan
  - COSMETIC with stop_on_cosmetic=False → fix pass runs
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.checkpoint import CheckpointManager
from tero2.config import Config, PlanHardeningConfig, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.notifier import Notifier
from tero2.phases.context import RunnerContext
from tero2.phases.harden_phase import run_harden
from tero2.state import AgentState

_INITIAL_PLAN = "# Original Plan\n1. do stuff"


def _make_ctx(
    tmp_path: Path,
    *,
    max_rounds: int = 5,
    stop_on_cosmetic: bool = True,
) -> tuple[RunnerContext, MagicMock]:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    disk = DiskLayer(project)
    disk.init()
    disk.write_file("milestones/M001/PLAN.md", _INITIAL_PLAN)

    config = Config()
    config.plan_hardening = PlanHardeningConfig(
        max_rounds=max_rounds, stop_on_cosmetic_only=stop_on_cosmetic
    )
    config.roles["reviewer"] = RoleConfig(provider="fake")
    config.telegram = TelegramConfig()

    checkpoint = CheckpointManager(disk)
    notifier = Notifier(config.telegram)
    cb_registry = CircuitBreakerRegistry()
    ctx = RunnerContext(config, disk, checkpoint, notifier, AgentState(), cb_registry)

    mock_chain = MagicMock()
    ctx.build_chain = MagicMock(return_value=mock_chain)

    return ctx, mock_chain


class TestSingleMalformedAsCritical:
    """Single malformed review output → treated as CRITICAL → fix pass runs."""

    async def test_single_malformed_triggers_fix_pass(self, tmp_path: Path) -> None:
        ctx, chain = _make_ctx(tmp_path)
        chain.run_prompt_collected = AsyncMock(
            side_effect=[
                "no recognizable verdict here",  # round 1 review → malformed → CRITICAL
                "# Improved Plan\nwith error handling",  # round 1 fix
                "NO ISSUES FOUND",  # round 2 review → converged
            ]
        )

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success
        # review + fix + review = 3 calls
        assert chain.run_prompt_collected.call_count == 3

    async def test_single_malformed_fix_updates_plan(self, tmp_path: Path) -> None:
        ctx, chain = _make_ctx(tmp_path, max_rounds=1)
        chain.run_prompt_collected = AsyncMock(
            side_effect=[
                "no verdict",  # malformed → CRITICAL → fix runs
                "# Fixed Plan\nwith improvements",
            ]
        )

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success
        written = ctx.disk.read_file("milestones/M001/PLAN.md")
        assert "Fixed Plan" in written


class TestMaxRoundsExhausted:
    """Loop terminates after max_rounds without error."""

    async def test_max_rounds_returns_success(self, tmp_path: Path) -> None:
        ctx, chain = _make_ctx(tmp_path, max_rounds=2)
        chain.run_prompt_collected = AsyncMock(return_value="CRITICAL: always issues")

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        # Does not fail — writes best-effort plan and stops
        assert result.success
        # 2 rounds × (review + fix) = 4 calls
        assert chain.run_prompt_collected.call_count == 4

    async def test_max_rounds_writes_last_plan(self, tmp_path: Path) -> None:
        ctx, chain = _make_ctx(tmp_path, max_rounds=1)
        chain.run_prompt_collected = AsyncMock(
            side_effect=[
                "CRITICAL: still issues",
                "# Partially fixed plan",
            ]
        )

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success
        written = ctx.disk.read_file("milestones/M001/PLAN.md")
        assert "Partially fixed plan" in written

    async def test_max_rounds_intermediate_versions_written(self, tmp_path: Path) -> None:
        ctx, chain = _make_ctx(tmp_path, max_rounds=2)
        chain.run_prompt_collected = AsyncMock(
            side_effect=[
                "CRITICAL: round 1",
                "# Plan v1",
                "CRITICAL: round 2",
                "# Plan v2",
            ]
        )

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            await run_harden(ctx)

        assert "Plan v1" in ctx.disk.read_file("milestones/M001/plan_v1.md")
        assert "Plan v2" in ctx.disk.read_file("milestones/M001/plan_v2.md")


class TestCosmeticWithoutStop:
    """COSMETIC verdict with stop_on_cosmetic=False → fix pass runs."""

    async def test_cosmetic_without_stop_triggers_fix(self, tmp_path: Path) -> None:
        ctx, chain = _make_ctx(tmp_path, stop_on_cosmetic=False, max_rounds=2)
        chain.run_prompt_collected = AsyncMock(
            side_effect=[
                "COSMETIC: minor wording",  # round 1 review → cosmetic, no stop
                "# Plan with cleaner wording",  # round 1 fix
                "NO ISSUES FOUND",  # round 2 review → converged
            ]
        )

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success
        assert chain.run_prompt_collected.call_count == 3
