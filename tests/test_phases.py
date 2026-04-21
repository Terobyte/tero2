"""Tests for SORA phase handlers and context helpers.

Covers:
    - PhaseResult dataclass
    - RunnerContext construction and build_chain delegation
    - run_scout / run_coach / run_architect / run_harden phase handlers
    - _read_next_slice / _load_slice_plan_from_disk helpers
    - _parse_verdict / _combine_prompt (harden internals)
    - _check_override / _format_task_plan / _update_task_metrics (execute internals)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from tero2.checkpoint import CheckpointManager
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, ReflexionConfig, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.notifier import Notifier
from tero2.phases.context import (
    PhaseResult,
    RunnerContext,
    _read_next_slice,
    _load_slice_plan_from_disk,
)
from tero2.state import AgentState


def _make_ctx(
    tmp_path: Path,
    state: AgentState | None = None,
    milestone_path: str = "milestones/M001",
) -> RunnerContext:
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    disk = DiskLayer(project)
    disk.init()
    config = Config()
    config.telegram = TelegramConfig()
    _state = state if state is not None else AgentState()
    checkpoint = CheckpointManager(disk)
    notifier = Notifier(TelegramConfig())
    cb_registry = CircuitBreakerRegistry()
    ctx = RunnerContext(
        config,
        disk,
        checkpoint,
        notifier,
        _state,
        cb_registry,
        milestone_path=milestone_path,
    )
    ctx.build_chain = MagicMock(return_value=MagicMock())
    return ctx


# ── PhaseResult ──────────────────────────────────────────────────────────


class TestPhaseResult:
    def test_defaults(self):
        r = PhaseResult(success=True)
        assert r.error == ""
        assert r.data is None

    def test_with_error(self):
        r = PhaseResult(success=False, error="boom")
        assert not r.success
        assert r.error == "boom"

    def test_with_data(self):
        r = PhaseResult(success=True, data={"key": "val"})
        assert r.data == {"key": "val"}


# ── RunnerContext construction ───────────────────────────────────────────


class TestRunnerContext:
    def test_default_escalation_fields(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        assert ctx.escalation_level.value == 0
        assert ctx.div_steps == 0
        assert ctx.escalation_history == []

    def test_milestone_path(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path, milestone_path="milestones/M002")
        assert ctx.milestone_path == "milestones/M002"

    def test_persona_default(self, tmp_path: Path):
        from tero2.persona import PersonaRegistry

        ctx = _make_ctx(tmp_path)
        assert isinstance(ctx.personas, PersonaRegistry)


# ── run_scout ────────────────────────────────────────────────────────────


class TestRunScout:
    async def test_skips_when_below_threshold(self, tmp_path: Path):
        from tero2.phases.scout_phase import run_scout

        ctx = _make_ctx(tmp_path)
        ctx.config.context.skip_scout_if_files_lt = 100

        result = await run_scout(ctx)
        assert not result.success
        assert "skipped" in result.error

    async def test_chain_build_failure_is_nonfatal(self, tmp_path: Path):
        from tero2.phases.scout_phase import run_scout

        ctx = _make_ctx(tmp_path)
        ctx.config.context.skip_scout_if_files_lt = 0
        ctx.config.roles["scout"] = RoleConfig(provider="fake")
        ctx.build_chain = MagicMock(side_effect=RuntimeError("no provider"))

        result = await run_scout(ctx)
        assert not result.success
        assert "no provider" in result.error

    async def test_successful_scout(self, tmp_path: Path):
        from tero2.phases.scout_phase import run_scout

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["scout"] = RoleConfig(provider="fake")

        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.file_count = 5
        mock_player_result.context_map = "## Context Map\n..."

        with patch("tero2.phases.scout_phase.ScoutPlayer") as MockScout:
            MockScout.should_skip = staticmethod(lambda *a: False)
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockScout.return_value = inst

            result = await run_scout(ctx)

        assert result.success
        assert result.data == "## Context Map\n..."

    async def test_failed_scout_is_nonfatal(self, tmp_path: Path):
        from tero2.phases.scout_phase import run_scout

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["scout"] = RoleConfig(provider="fake")

        mock_player_result = MagicMock()
        mock_player_result.success = False
        mock_player_result.error = "LLM timeout"
        mock_player_result.context_map = None

        with patch("tero2.phases.scout_phase.ScoutPlayer") as MockScout:
            MockScout.should_skip = staticmethod(lambda *a: False)
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockScout.return_value = inst

            result = await run_scout(ctx)

        assert not result.success
        assert result.data is None


# ── run_coach ────────────────────────────────────────────────────────────


class TestRunCoach:
    async def test_chain_build_failure_is_nonfatal(self, tmp_path: Path):
        from tero2.phases.coach_phase import run_coach
        from tero2.triggers import CoachTrigger

        ctx = _make_ctx(tmp_path)
        ctx.build_chain = MagicMock(side_effect=RuntimeError("no coach provider"))

        result = await run_coach(ctx, CoachTrigger.FIRST_RUN)
        assert not result.success
        assert "no coach provider" in result.error

    async def test_successful_coach(self, tmp_path: Path):
        from tero2.phases.coach_phase import run_coach
        from tero2.triggers import CoachTrigger

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["coach"] = RoleConfig(provider="fake")

        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.strategy = "Focus on quality"
        mock_player_result.task_queue = "1. Fix bugs"
        mock_player_result.risk = "Low"
        mock_player_result.context_hints = "Use async"

        with patch("tero2.phases.coach_phase.CoachPlayer") as MockCoach:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockCoach.return_value = inst

            result = await run_coach(ctx, CoachTrigger.FIRST_RUN)

        assert result.success
        assert result.data["strategy"] == "Focus on quality"

    async def test_failed_coach_is_nonfatal(self, tmp_path: Path):
        from tero2.phases.coach_phase import run_coach
        from tero2.triggers import CoachTrigger

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["coach"] = RoleConfig(provider="fake")

        mock_player_result = MagicMock()
        mock_player_result.success = False
        mock_player_result.error = "LLM error"

        with patch("tero2.phases.coach_phase.CoachPlayer") as MockCoach:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockCoach.return_value = inst

            result = await run_coach(ctx, CoachTrigger.END_OF_SLICE)

        assert not result.success
        assert result.data is None

    async def test_default_slice_id(self, tmp_path: Path):
        from tero2.phases.coach_phase import run_coach
        from tero2.triggers import CoachTrigger

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["coach"] = RoleConfig(provider="fake")
        ctx.state.current_slice = "S03"

        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.strategy = "s"
        mock_player_result.task_queue = "t"
        mock_player_result.risk = "r"
        mock_player_result.context_hints = "c"

        with patch("tero2.phases.coach_phase.CoachPlayer") as MockCoach:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockCoach.return_value = inst

            await run_coach(ctx, CoachTrigger.FIRST_RUN)

        call_kwargs = inst.run.call_args[1]
        assert call_kwargs["slice_id"] == "S03"


# ── run_architect ────────────────────────────────────────────────────────


class TestRunArchitect:
    async def test_chain_build_failure_is_fatal(self, tmp_path: Path):
        from tero2.phases.architect_phase import run_architect

        ctx = _make_ctx(tmp_path)
        ctx.build_chain = MagicMock(side_effect=RuntimeError("no architect provider"))

        result = await run_architect(ctx, "S01")
        assert not result.success
        assert "no architect provider" in result.error

    async def test_successful_architect(self, tmp_path: Path):
        from tero2.phases.architect_phase import run_architect
        from tero2.players.architect import SlicePlan, Task

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["architect"] = RoleConfig(provider="fake")

        plan = SlicePlan(
            slice_id="S01",
            slice_dir="milestones/M001/S01",
            tasks=[Task(id="T01", description="do thing")],
        )

        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.task_count = 1
        mock_player_result.slice_plan = plan

        with patch("tero2.phases.architect_phase.ArchitectPlayer") as MockArch:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockArch.return_value = inst

            result = await run_architect(ctx, "S01")

        assert result.success
        assert result.data["slice_plan"] is plan
        assert ctx.state.current_slice == "S01"

    async def test_failed_architect(self, tmp_path: Path):
        from tero2.phases.architect_phase import run_architect

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["architect"] = RoleConfig(provider="fake")

        mock_player_result = MagicMock()
        mock_player_result.success = False
        mock_player_result.error = "malformed plan"
        mock_player_result.task_count = 0

        with patch("tero2.phases.architect_phase.ArchitectPlayer") as MockArch:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockArch.return_value = inst

            result = await run_architect(ctx, "S01")

        assert not result.success
        assert "malformed plan" in result.error

    async def test_default_slice_id(self, tmp_path: Path):
        from tero2.phases.architect_phase import run_architect
        from tero2.players.architect import SlicePlan

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["architect"] = RoleConfig(provider="fake")

        plan = SlicePlan(slice_id="S01", slice_dir="milestones/M001/S01")
        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.task_count = 0
        mock_player_result.slice_plan = plan

        with patch("tero2.phases.architect_phase.ArchitectPlayer") as MockArch:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockArch.return_value = inst

            result = await run_architect(ctx)

        assert result.success


# ── run_harden ───────────────────────────────────────────────────────────


class TestRunHarden:
    async def test_no_plan_returns_failure(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        result = await run_harden(ctx)
        assert not result.success
        assert "no plan" in result.error

    async def test_chain_build_failure(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file("milestones/M001/PLAN.md", "some plan")
        ctx.build_chain = MagicMock(side_effect=RuntimeError("no reviewer"))

        result = await run_harden(ctx)
        assert not result.success
        assert "reviewer" in result.error

    async def test_converges_on_no_issues(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["reviewer"] = RoleConfig(provider="fake")
        ctx.disk.write_file("milestones/M001/PLAN.md", "original plan")

        mock_chain = MagicMock()
        mock_chain.run_prompt_collected = AsyncMock(return_value="NO ISSUES FOUND")
        ctx.build_chain = MagicMock(return_value=mock_chain)

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success
        assert result.data == "original plan"

    async def test_converges_on_cosmetic_when_stop_on_cosmetic(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["reviewer"] = RoleConfig(provider="fake")
        ctx.config.plan_hardening.stop_on_cosmetic_only = True
        ctx.disk.write_file("milestones/M001/PLAN.md", "original plan")

        mock_chain = MagicMock()
        mock_chain.run_prompt_collected = AsyncMock(return_value="COSMETIC: minor wording")
        ctx.build_chain = MagicMock(return_value=mock_chain)

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success

    async def test_writes_hardened_plan(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["reviewer"] = RoleConfig(provider="fake")
        ctx.config.plan_hardening.max_rounds = 1
        ctx.disk.write_file("milestones/M001/PLAN.md", "draft")

        mock_chain = MagicMock()
        mock_chain.run_prompt_collected = AsyncMock(
            side_effect=["CRITICAL: missing tests", "improved plan with tests"]
        )
        ctx.build_chain = MagicMock(return_value=mock_chain)

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success
        written = ctx.disk.read_file("milestones/M001/PLAN.md")
        assert "improved plan with tests" in written

    async def test_malformed_then_malformed_stops(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["reviewer"] = RoleConfig(provider="fake")
        ctx.config.plan_hardening.max_rounds = 5
        ctx.disk.write_file("milestones/M001/PLAN.md", "plan text")

        mock_chain = MagicMock()
        mock_chain.run_prompt_collected = AsyncMock(return_value="gibberish response")
        ctx.build_chain = MagicMock(return_value=mock_chain)

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success


# ── _parse_verdict (harden) ─────────────────────────────────────────────


class TestParseVerdict:
    def test_no_issues(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("NO ISSUES FOUND") == "no_issues"

    def test_critical(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("CRITICAL: missing tests") == "critical"

    def test_cosmetic_only(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("COSMETIC: wording") == "cosmetic"

    def test_malformed(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("random gibberish") == "malformed"

    def test_critical_overrides_cosmetic(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("CRITICAL and COSMETIC issues") == "critical"

    def test_case_insensitive(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("no issues found") == "no_issues"


# ── _combine_prompt (harden) ────────────────────────────────────────────


class TestCombinePrompt:
    def test_both_parts(self):
        from tero2.phases.harden_phase import _combine_prompt

        assembled = MagicMock()
        assembled.system_prompt = "system"
        assembled.user_prompt = "user"
        result = _combine_prompt(assembled)
        assert "system" in result
        assert "user" in result
        assert "---" in result

    def test_system_only(self):
        from tero2.phases.harden_phase import _combine_prompt

        assembled = MagicMock()
        assembled.system_prompt = "system"
        assembled.user_prompt = ""
        assert _combine_prompt(assembled) == "system"

    def test_user_only(self):
        from tero2.phases.harden_phase import _combine_prompt

        assembled = MagicMock()
        assembled.system_prompt = ""
        assembled.user_prompt = "user"
        assert _combine_prompt(assembled) == "user"


# ── _read_next_slice ────────────────────────────────────────────────────


class TestReadNextSlice:
    def test_returns_slice_id(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file(
            "strategic/TASK_QUEUE.md",
            "# Queue\n- [ ] S01: First slice\n- [ ] S02: Second slice\n",
        )
        result = _read_next_slice(ctx)
        assert result == "S01"

    def test_marks_as_in_progress(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file(
            "strategic/TASK_QUEUE.md",
            "- [ ] S01: First slice\n",
        )
        _read_next_slice(ctx)
        content = ctx.disk.read_file("strategic/TASK_QUEUE.md")
        assert "[~]" in content
        assert "[ ]" not in content

    def test_skips_claimed(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file(
            "strategic/TASK_QUEUE.md",
            "- [~] S01: In progress\n- [ ] S02: Next\n",
        )
        result = _read_next_slice(ctx)
        assert result == "S02"

    def test_returns_none_when_empty(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file("strategic/TASK_QUEUE.md", "")
        assert _read_next_slice(ctx) is None

    def test_returns_none_when_no_file(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        assert _read_next_slice(ctx) is None

    def test_returns_none_when_all_claimed(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file(
            "strategic/TASK_QUEUE.md",
            "- [~] S01: Done\n- [~] S02: Done\n",
        )
        assert _read_next_slice(ctx) is None


# ── _load_slice_plan_from_disk ──────────────────────────────────────────


class TestLoadSlicePlanFromDisk:
    def test_returns_empty_plan_when_no_file(self, tmp_path: Path):
        import pytest

        ctx = _make_ctx(tmp_path)
        with pytest.raises(ValueError):
            _load_slice_plan_from_disk(ctx, "S01")

    def test_parses_plan_from_disk(self, tmp_path: Path):
        ctx = _make_ctx(tmp_path)
        plan_content = (
            "## T01: Init\n**Must-haves:**\n- create module\n"
            "## T02: Test\n**Must-haves:**\n- tests pass\n"
        )
        ctx.disk.write_file("milestones/M001/S01/S01-PLAN.md", plan_content)
        plan = _load_slice_plan_from_disk(ctx, "S01")
        assert len(plan.tasks) == 2
        assert plan.tasks[0].id == "T01"
        assert plan.tasks[1].id == "T02"


# ── _check_override (execute) ───────────────────────────────────────────


class TestCheckOverride:
    def test_none_when_no_override(self, tmp_path: Path):
        from tero2.phases.execute_phase import _check_override

        ctx = _make_ctx(tmp_path)
        result = _check_override(ctx, "S01", {})
        assert result is None

    def test_stop_override(self, tmp_path: Path):
        from tero2.phases.execute_phase import _check_override

        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file("human/OVERRIDE.md", "STOP")
        result = _check_override(ctx, "S01", {"T01": "out.md"})
        assert result is not None
        assert not result.success
        assert "STOP" in result.error

    def test_pause_override(self, tmp_path: Path):
        from tero2.phases.execute_phase import _check_override
        from tero2.state import Phase

        ctx = _make_ctx(tmp_path, state=AgentState(phase=Phase.RUNNING))
        ctx.disk.write_file("human/OVERRIDE.md", "PAUSE")
        result = _check_override(ctx, "S01", {})
        assert result is not None
        assert not result.success
        assert "PAUSE" in result.error

    def test_unknown_override_returns_none(self, tmp_path: Path):
        from tero2.phases.execute_phase import _check_override

        ctx = _make_ctx(tmp_path)
        ctx.disk.write_file("human/OVERRIDE.md", "random text")
        result = _check_override(ctx, "S01", {})
        assert result is None


# ── _format_task_plan (execute) ─────────────────────────────────────────


class TestFormatTaskPlan:
    def test_basic_task(self):
        from tero2.phases.execute_phase import _format_task_plan
        from tero2.players.architect import Task

        task = Task(id="T01", description="Write module")
        result = _format_task_plan(task)
        assert "## T01: Write module" in result
        assert "Must-haves" not in result

    def test_with_must_haves(self):
        from tero2.phases.execute_phase import _format_task_plan
        from tero2.players.architect import Task

        task = Task(
            id="T02",
            description="Add tests",
            must_haves=["pytest passes", "coverage > 80%"],
        )
        result = _format_task_plan(task)
        assert "## T02: Add tests" in result
        assert "**Must-haves:**" in result
        assert "- pytest passes" in result
        assert "- coverage > 80%" in result


# ── _update_task_metrics (execute) ──────────────────────────────────────


class TestUpdateTaskMetrics:
    def test_creates_metrics_on_first_call(self, tmp_path: Path):
        from tero2.phases.execute_phase import _update_task_metrics

        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()

        _update_task_metrics(disk, "T01", True)
        metrics = disk.read_metrics()
        assert metrics["tasks_attempted"] == 1
        assert metrics["tasks_passed"] == 1

    def test_increments_counters(self, tmp_path: Path):
        from tero2.phases.execute_phase import _update_task_metrics

        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        disk.write_metrics({"tasks_attempted": 2, "tasks_passed": 1})

        _update_task_metrics(disk, "T03", False)
        metrics = disk.read_metrics()
        assert metrics["tasks_attempted"] == 3
        assert metrics["tasks_passed"] == 1

    def test_passed_increments_both(self, tmp_path: Path):
        from tero2.phases.execute_phase import _update_task_metrics

        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        disk.write_metrics({"tasks_attempted": 1, "tasks_passed": 0})

        _update_task_metrics(disk, "T02", True)
        metrics = disk.read_metrics()
        assert metrics["tasks_attempted"] == 2
        assert metrics["tasks_passed"] == 1


# ── shutdown_event checks ───────────────────────────────────────────────


class TestShutdownEventScout:
    async def test_scout_returns_on_shutdown(self, tmp_path: Path):
        from tero2.phases.scout_phase import run_scout

        ctx = _make_ctx(tmp_path)
        ctx.config.context.skip_scout_if_files_lt = 0
        ctx.config.roles["scout"] = RoleConfig(provider="fake")
        event = asyncio.Event()
        event.set()
        ctx.shutdown_event = event

        result = await run_scout(ctx)
        assert not result.success
        assert "shutdown" in result.error

    async def test_scout_proceeds_when_not_set(self, tmp_path: Path):
        from tero2.phases.scout_phase import run_scout

        ctx = _make_ctx(tmp_path)
        ctx.config.context.skip_scout_if_files_lt = 0
        ctx.config.roles["scout"] = RoleConfig(provider="fake")
        ctx.shutdown_event = asyncio.Event()

        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.file_count = 1
        mock_player_result.context_map = "map"

        with patch("tero2.phases.scout_phase.ScoutPlayer") as MockScout:
            MockScout.should_skip = staticmethod(lambda *a: False)
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockScout.return_value = inst

            result = await run_scout(ctx)

        assert result.success

    async def test_scout_proceeds_when_event_is_none(self, tmp_path: Path):
        from tero2.phases.scout_phase import run_scout

        ctx = _make_ctx(tmp_path)
        ctx.config.context.skip_scout_if_files_lt = 0
        ctx.config.roles["scout"] = RoleConfig(provider="fake")
        ctx.shutdown_event = None

        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.file_count = 1
        mock_player_result.context_map = "map"

        with patch("tero2.phases.scout_phase.ScoutPlayer") as MockScout:
            MockScout.should_skip = staticmethod(lambda *a: False)
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockScout.return_value = inst

            result = await run_scout(ctx)

        assert result.success


class TestShutdownEventCoach:
    async def test_coach_returns_on_shutdown(self, tmp_path: Path):
        from tero2.phases.coach_phase import run_coach
        from tero2.triggers import CoachTrigger

        ctx = _make_ctx(tmp_path)
        event = asyncio.Event()
        event.set()
        ctx.shutdown_event = event

        result = await run_coach(ctx, CoachTrigger.FIRST_RUN)
        assert not result.success
        assert "shutdown" in result.error

    async def test_coach_proceeds_when_not_set(self, tmp_path: Path):
        from tero2.phases.coach_phase import run_coach
        from tero2.triggers import CoachTrigger

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["coach"] = RoleConfig(provider="fake")
        ctx.shutdown_event = asyncio.Event()

        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.strategy = "s"
        mock_player_result.task_queue = "t"
        mock_player_result.risk = "r"
        mock_player_result.context_hints = "c"

        with patch("tero2.phases.coach_phase.CoachPlayer") as MockCoach:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockCoach.return_value = inst

            result = await run_coach(ctx, CoachTrigger.FIRST_RUN)

        assert result.success


class TestShutdownEventArchitect:
    async def test_architect_returns_on_shutdown(self, tmp_path: Path):
        from tero2.phases.architect_phase import run_architect

        ctx = _make_ctx(tmp_path)
        event = asyncio.Event()
        event.set()
        ctx.shutdown_event = event

        result = await run_architect(ctx, "S01")
        assert not result.success
        assert "shutdown" in result.error

    async def test_architect_proceeds_when_not_set(self, tmp_path: Path):
        from tero2.phases.architect_phase import run_architect
        from tero2.players.architect import SlicePlan

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["architect"] = RoleConfig(provider="fake")
        ctx.shutdown_event = asyncio.Event()

        plan = SlicePlan(slice_id="S01", slice_dir="milestones/M001/S01")
        mock_player_result = MagicMock()
        mock_player_result.success = True
        mock_player_result.error = ""
        mock_player_result.task_count = 0
        mock_player_result.slice_plan = plan

        with patch("tero2.phases.architect_phase.ArchitectPlayer") as MockArch:
            inst = MagicMock()
            inst.run = AsyncMock(return_value=mock_player_result)
            MockArch.return_value = inst

            result = await run_architect(ctx, "S01")

        assert result.success


class TestShutdownEventHarden:
    async def test_harden_returns_on_shutdown_at_round_boundary(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["reviewer"] = RoleConfig(provider="fake")
        ctx.config.plan_hardening.max_rounds = 3
        ctx.disk.write_file("milestones/M001/PLAN.md", "plan")

        event = asyncio.Event()
        event.set()
        ctx.shutdown_event = event

        mock_chain = MagicMock()
        mock_chain.run_prompt_collected = AsyncMock(return_value="NO ISSUES FOUND")
        ctx.build_chain = MagicMock(return_value=mock_chain)

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert not result.success
        assert "shutdown" in result.error

    async def test_harden_proceeds_when_not_set(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["reviewer"] = RoleConfig(provider="fake")
        ctx.config.plan_hardening.max_rounds = 1
        ctx.disk.write_file("milestones/M001/PLAN.md", "plan")
        ctx.shutdown_event = asyncio.Event()

        mock_chain = MagicMock()
        mock_chain.run_prompt_collected = AsyncMock(return_value="NO ISSUES FOUND")
        ctx.build_chain = MagicMock(return_value=mock_chain)

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert result.success

    async def test_harden_shutdown_mid_loop(self, tmp_path: Path):
        from tero2.phases.harden_phase import run_harden

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["reviewer"] = RoleConfig(provider="fake")
        ctx.config.plan_hardening.max_rounds = 3
        ctx.disk.write_file("milestones/M001/PLAN.md", "plan")

        event = asyncio.Event()
        ctx.shutdown_event = event

        call_count = 0

        async def collect_and_shutdown(_prompt):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                event.set()
            return "CRITICAL: fix needed" if call_count <= 2 else "NO ISSUES FOUND"

        mock_chain = MagicMock()
        mock_chain.run_prompt_collected = AsyncMock(side_effect=collect_and_shutdown)
        ctx.build_chain = MagicMock(return_value=mock_chain)

        with patch("tero2.phases.harden_phase.ContextAssembler"):
            result = await run_harden(ctx)

        assert not result.success
        assert "shutdown" in result.error


class TestShutdownEventExecute:
    async def test_execute_returns_on_shutdown_at_task_boundary(self, tmp_path: Path):
        from tero2.phases.execute_phase import run_execute
        from tero2.players.architect import SlicePlan, Task
        from tero2.players.builder import BuilderResult

        ctx = _make_ctx(tmp_path)
        ctx.config.roles["builder"] = RoleConfig(provider="fake")
        ctx.config.reflexion = ReflexionConfig(max_cycles=0)

        event = asyncio.Event()
        event.set()
        ctx.shutdown_event = event

        plan = SlicePlan(
            slice_id="S01",
            slice_dir="milestones/M001/S01",
            tasks=[Task(id="T01", description="do thing")],
        )

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(
                return_value=BuilderResult(
                    success=True, output_file="out.md", captured_output="ok"
                )
            )
            MockB.return_value = inst

            result = await run_execute(ctx, plan)

        assert not result.success
        assert "shutdown" in result.error
