"""TDD proof-of-bug tests for OPEN bugs (11–20). All must be RED."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.checkpoint import CheckpointManager
from tero2.config import (
    Config,
    EscalationConfig,
    RoleConfig,
    StuckDetectionConfig,
    TelegramConfig,
)
from tero2.disk_layer import DiskLayer
from tero2.errors import RateLimitError
from tero2.escalation import (
    EscalationAction,
    EscalationLevel,
    decide_escalation,
    execute_escalation,
)
from tero2.notifier import Notifier
from tero2.state import AgentState, Phase
from tero2.stuck_detection import StuckResult, StuckSignal, check_stuck, update_tool_hash


def _make_project(tmp_path: Path):
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    plan = project / "plan.md"
    plan.write_text("# Plan\n1. do stuff\n2. more stuff\n3. done")
    config = Config()
    config.roles["executor"] = RoleConfig(provider="fake", timeout_s=30)
    config.telegram = TelegramConfig(bot_token="", chat_id="")
    return project, plan, config, disk


async def _fake_notify(text: str, level=None) -> bool:
    return True


class _AlwaysFailChain:
    current_provider_index = 0

    async def run_prompt(self, prompt: str):
        raise RateLimitError("always fail")
        yield


class _YieldOnceChain:
    current_provider_index = 0

    async def run_prompt(self, prompt: str):
        yield {"type": "tool_result", "content": "done"}


class _FailAfterOneStepChain:
    current_provider_index = 0

    async def run_prompt(self, prompt: str):
        yield {"type": "tool_result", "content": "step1"}
        raise RateLimitError("fail after one step")


# ── Bug 11: Off-by-one in TOOL_REPEAT threshold ──────────────────────────


class TestBug11OffByOneToolRepeatThreshold:
    def test_tool_repeat_fires_one_step_too_early(self):
        config = StuckDetectionConfig(tool_repeat_threshold=2)
        state = AgentState(tool_repeat_count=1, last_tool_hash="abc123")
        result = check_stuck(state, config)
        assert result.signal != StuckSignal.TOOL_REPEAT, (
            f"TOOL_REPEAT fired at tool_repeat_count=1 with threshold=2. "
            f"The check `count >= threshold - 1` -> `1 >= 1` triggers after "
            f"just 1 repeat (2 identical calls). Should require "
            f"`count >= threshold` -> `1 >= 2` -> False."
        )

    def test_update_tool_hash_fires_on_second_repeat(self):
        config = StuckDetectionConfig(tool_repeat_threshold=2)
        state = AgentState()
        state, _ = update_tool_hash(state, "same_tool_call")
        state, _ = update_tool_hash(state, "same_tool_call")
        assert state.tool_repeat_count == 1
        result = check_stuck(state, config)
        assert result.signal == StuckSignal.TOOL_REPEAT, (
            f"After 2 identical tool calls, tool_repeat_count=1 with "
            f"threshold=2. check_stuck should fire via `1 >= 2-1` "
            f"(fires after N identical calls where N=threshold)."
        )

    def test_source_uses_threshold_minus_one(self):
        source = inspect.getsource(check_stuck)
        tool_section = source[source.index("tool_repeat_count") :]
        tool_section = tool_section[: tool_section.index("\n\n")]
        assert "tool_repeat_count >= config.tool_repeat_threshold - 1" in tool_section, (
            "check_stuck should use `>= threshold - 1` so that threshold=2 "
            "fires after 2 identical calls (count=1), not 3."
        )


# ── Bug 12: _current_state not updated after _handle_override PAUSE ──────


class TestBug12CurrentStateNotUpdatedAfterOverridePause:
    def test_handle_override_assigns_state_for_both_paths(self):
        from tero2.runner import Runner

        source = inspect.getsource(Runner._handle_override)
        pause_block = source[source.index("_RE_PAUSE") :]
        assert "self._current_state" in pause_block, (
            "_handle_override calls self.checkpoint.mark_paused() but "
            "discards the return value. The PAUSE branch must assign: "
            "`self._current_state = self.checkpoint.mark_paused(...)`"
        )


# ── Bug 13: escalation_level persists across _execute_plan calls ─────────


class TestBug13EscalationCrossPlanBleed:
    def test_decide_escalation_from_previous_plan_carries_over(self):
        config = EscalationConfig(diversification_max_steps=2)
        stuck = StuckResult(signal=StuckSignal.TOOL_REPEAT, details="repeat", severity=2)
        action_fresh = decide_escalation(
            stuck,
            current_level=EscalationLevel.NONE,
            diversification_steps_taken=0,
            config=config,
        )
        assert action_fresh.level == EscalationLevel.DIVERSIFICATION, (
            f"decide_escalation with current_level=NONE (fresh plan) correctly "
            f"returns DIVERSIFICATION. The runner must reset _escalation_level "
            f"to NONE before each _execute_plan call so stale levels from "
            f"previous plans don't cause immediate HUMAN escalation."
        )

    async def test_second_plan_immediately_paused_at_level3(self, tmp_path: Path):
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 3
        config.retry.chain_retry_wait_s = 0.0
        config.stuck_detection.max_retries = 1
        config.reflexion.max_cycles = 10

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        with patch.object(runner, "_build_chain", return_value=_AlwaysFailChain()):
            await runner._execute_legacy(
                AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01"),
            )

        assert runner._ctx is not None
        assert runner._ctx.escalation_level != EscalationLevel.BACKTRACK_COACH, (
            f"After _execute_plan the escalation_level is BACKTRACK_COACH. "
            f"_execute_legacy must call ctx.reset() at the start so the new plan gets "
            f"a fresh escalation progression instead of inheriting the previous plan's state."
        )
        assert EscalationLevel.DIVERSIFICATION in runner._ctx.escalation_history, (
            f"Escalation history {runner._ctx.escalation_history} lacks DIVERSIFICATION. "
            f"A fresh plan must start at Level 1 (DIVERSIFICATION) before reaching "
            f"higher levels. If stale BACKTRACK_COACH persisted, the first stuck "
            f"signal would skip straight to HUMAN, bypassing Level 1 entirely."
        )

    def test_execute_plan_resets_escalation_level(self):
        from tero2.runner import Runner

        source = inspect.getsource(Runner._execute_legacy)
        assert "ctx.reset()" in source, (
            "_execute_legacy must call ctx.reset() early to reset escalation "
            "state (escalation_level, diversification_steps, escalation_history) "
            "before each execution."
        )


# ── Bug 14: disk.write_state vs checkpoint.save in execute_escalation ────


class TestBug14LastCheckpointNotUpdatedForLevel12:
    async def test_level1_does_not_update_last_checkpoint(self, tmp_path: Path):
        project, plan, config, disk = _make_project(tmp_path)
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(config.telegram)
        notifier.notify = _fake_notify

        state = AgentState(phase=Phase.RUNNING)
        state.last_checkpoint = ""

        action = EscalationAction(
            level=EscalationLevel.DIVERSIFICATION, inject_prompt="try different"
        )
        state = await execute_escalation(
            action,
            state,
            disk,
            notifier,
            checkpoint,
            stuck_result=StuckResult(signal=StuckSignal.TOOL_REPEAT, details="repeat", severity=2),
        )

        assert state.last_checkpoint != "", (
            f"After Level 1 escalation, last_checkpoint is empty. "
            f"execute_escalation uses disk.write_state(state) for Level 1, "
            f"which does NOT set last_checkpoint. Level 3 uses "
            f"checkpoint.mark_paused() which calls self.save() and sets it. "
            f"Inconsistency makes last_checkpoint unreliable for backtracking."
        )

    async def test_level2_does_not_update_last_checkpoint(self, tmp_path: Path):
        project, plan, config, disk = _make_project(tmp_path)
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(config.telegram)
        notifier.notify = _fake_notify

        state = AgentState(phase=Phase.RUNNING)
        state.last_checkpoint = ""

        action = EscalationAction(level=EscalationLevel.BACKTRACK_COACH, should_backtrack=True)
        state = await execute_escalation(
            action,
            state,
            disk,
            notifier,
            checkpoint,
            stuck_result=StuckResult(signal=StuckSignal.TOOL_REPEAT, details="repeat", severity=2),
        )

        assert state.last_checkpoint != "", (
            f"After Level 2 escalation, last_checkpoint is empty. "
            f"Level 2 uses disk.write_state() — same inconsistency as Level 1."
        )

    def test_escalation_source_uses_save_for_all_levels(self):
        source = inspect.getsource(execute_escalation)
        level1_block_start = source.index("DIVERSIFICATION")
        level2_block_start = source.index("BACKTRACK_COACH")
        level1_block = source[level1_block_start:level2_block_start]
        assert "checkpoint.save" in level1_block or "self.save" in level1_block, (
            "Level 1 escalation uses disk.write_state(state) instead of "
            "checkpoint.save(state). Only Level 3 correctly uses "
            "checkpoint.mark_paused() -> self.save(). All levels should use "
            "checkpoint.save() for consistent last_checkpoint updates."
        )


# ── Bug 16: tool_repeat_count and last_tool_hash not reset in increment_retry


class TestBug16ToolRepeatNotResetOnRetry:
    def test_increment_retry_preserves_tool_repeat_count(self, tmp_path: Path):
        project, plan, config, disk = _make_project(tmp_path)
        checkpoint = CheckpointManager(disk)

        state = AgentState(phase=Phase.RUNNING)
        state.tool_repeat_count = 3
        state.last_tool_hash = "deadbeef1234"
        state = checkpoint.increment_retry(state)

        assert state.tool_repeat_count == 0, (
            f"After increment_retry, tool_repeat_count is "
            f"{state.tool_repeat_count} (should be 0). A new retry attempt "
            f"starts with a fresh provider but inherits the stuck counter "
            f"from the failed attempt."
        )
        assert state.last_tool_hash == "", (
            f"After increment_retry, last_tool_hash is "
            f"'{state.last_tool_hash}' (should be empty). Combined with "
            f"Bug 11's off-by-one, one more repeat in the new attempt "
            f"immediately fires TOOL_REPEAT."
        )

    def test_increment_retry_source_resets_tool_state(self):
        source = inspect.getsource(CheckpointManager.increment_retry)
        assert "tool_repeat_count" in source, (
            "increment_retry resets steps_in_task and provider_index but "
            "NOT tool_repeat_count or last_tool_hash. Compare with Level 2 "
            "backtrack in escalation.py which correctly resets all four."
        )


# ── Bug 17: _override_contains_pause ignores STOP ────────────────────────


class TestBug17PauseResumeIgnoresStop:
    async def test_resume_on_stop_instead_of_halting(self, tmp_path: Path):
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 2
        config.retry.chain_retry_wait_s = 0.0

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        mark_running_called = []

        original_mark_running = runner.checkpoint.mark_running

        def tracking_mark_running(state):
            mark_running_called.append(state.phase)
            return original_mark_running(state)

        runner.checkpoint.mark_running = tracking_mark_running

        override_sequence = iter(["PAUSE", "STOP"])
        pause_sequence = iter([True, False])

        async def mock_check_override():
            try:
                return next(override_sequence)
            except StopIteration:
                return None

        async def mock_contains_pause():
            try:
                return next(pause_sequence)
            except StopIteration:
                return False

        runner._check_override = mock_check_override
        runner._override_contains_pause = mock_contains_pause

        with patch("tero2.runner.asyncio.sleep", new_callable=AsyncMock):
            with patch.object(runner, "_build_chain", return_value=_YieldOnceChain()):
                await runner._execute_legacy(
                    AgentState(
                        phase=Phase.RUNNING,
                        plan_file=str(plan),
                        started_at="2025-01-01",
                    ),
                )

        assert not mark_running_called, (
            f"mark_running was called after PAUSE was replaced with STOP. "
            f"The PAUSE wait loop only checks for PAUSE keyword — when PAUSE "
            f"is removed and replaced with STOP, the loop exits and the "
            f"runner calls mark_running, resuming execution instead of halting."
        )

    def test_pause_loop_checks_stop_directive(self):
        from tero2.runner import Runner

        source = inspect.getsource(Runner._run_legacy_agent)
        pause_idx = source.index("_override_contains_pause")
        log_idx = source.index("log.info", pause_idx)
        pause_loop = source[pause_idx:log_idx]
        assert "_RE_STOP" in pause_loop or "STOP" in pause_loop, (
            "The PAUSE wait loop (`while await self._override_contains_pause()`) "
            "only checks for PAUSE keyword. After the loop exits, there's no "
            "check for STOP directive before calling mark_running(). "
            "If user replaces PAUSE with STOP, the runner resumes instead of halting."
        )


# ── Bug 18: mark_started doesn't use self.save() ─────────────────────────


class TestBug18MarkStartedLastCheckpointEmpty:
    def test_mark_started_last_checkpoint_is_empty(self, tmp_path: Path):
        project, plan, config, disk = _make_project(tmp_path)
        checkpoint = CheckpointManager(disk)

        state = checkpoint.mark_started(str(plan))

        assert state.last_checkpoint != "", (
            f"After mark_started, last_checkpoint is empty. "
            f"mark_started uses self.disk.write_state(state) instead of "
            f"self.save(state). Every other mutating method (mark_completed, "
            f"mark_failed, mark_paused, mark_running, increment_retry, "
            f"increment_step) correctly uses self.save()."
        )

    def test_mark_started_source_uses_save(self):
        source = inspect.getsource(CheckpointManager.mark_started)
        assert "self.save(" in source, (
            "mark_started uses self.disk.write_state(state) instead of "
            "self.save(state). The save() method sets last_checkpoint before "
            "writing; write_state() does not. This is the only mutating "
            "method that bypasses save()."
        )


# ── Bug 19: _escalation_level not reset on OVERRIDE PAUSE resume ─────────


class TestBug19EscalationNotResetOnPauseResume:
    async def test_escalation_level_survives_pause_resume(self, tmp_path: Path):
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 2
        config.retry.chain_retry_wait_s = 0.0

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        override_sequence = iter(["PAUSE", None])
        pause_sequence = iter([True, False])

        async def mock_check_override():
            try:
                return next(override_sequence)
            except StopIteration:
                return None

        async def mock_contains_pause():
            try:
                return next(pause_sequence)
            except StopIteration:
                return False

        runner._check_override = mock_check_override
        runner._override_contains_pause = mock_contains_pause

        with patch("tero2.runner.asyncio.sleep", new_callable=AsyncMock):
            with patch.object(runner, "_build_chain", return_value=_YieldOnceChain()):
                await runner._execute_legacy(
                    AgentState(
                        phase=Phase.RUNNING,
                        plan_file=str(plan),
                        started_at="2025-01-01",
                    ),
                )

        assert runner._ctx is not None
        assert runner._ctx.escalation_level == EscalationLevel.NONE, (
            f"After PAUSE resume, escalation_level is still "
            f"{runner._ctx.escalation_level.name}. Should be reset to NONE. "
            f"If the next stuck signal fires, it will immediately "
            f"re-escalate to HUMAN instead of starting fresh at Level 1."
        )
        assert runner._ctx.div_steps == 0, (
            f"After PAUSE resume, div_steps is still {runner._ctx.div_steps}. Should be reset to 0."
        )

    def test_pause_resume_resets_escalation_in_source(self):
        from tero2.runner import Runner

        source = inspect.getsource(Runner._run_legacy_agent)
        mr_idx = source.index("mark_running")
        end_idx = source.index("\n\n", mr_idx)
        resume_section = source[mr_idx:end_idx]
        assert "ctx.reset()" in resume_section, (
            "After PAUSE resume (mark_running), the runner does not reset "
            "RunnerContext via self._ctx.reset(). User's STEER.md input is wasted — "
            "the first stuck signal re-triggers the previous escalation level."
        )


# ── Bug 20: _launch_runner fire-and-forget ───────────────────────────────


class TestBug20LaunchRunnerFireAndForget:
    def test_launch_runner_awaits_subprocess(self):
        from tero2.telegram_input import TelegramInputBot

        source = inspect.getsource(TelegramInputBot._launch_runner)
        assert "await proc.wait()" in source or "proc.returncode" in source, (
            "_launch_runner creates a subprocess but never awaits "
            "proc.wait() or checks proc.returncode. If the runner crashes "
            "immediately (import error, missing module), the error is "
            "silently lost. The user already got 'starting runner' "
            "confirmation in Telegram."
        )

    async def test_launch_runner_detects_immediate_failure(self, tmp_path: Path):
        from tero2.telegram_input import TelegramInputBot

        config = Config()
        config.telegram = TelegramConfig(bot_token="fake", chat_id="123")
        bot = TelegramInputBot(config)

        project_path = tmp_path / "project"
        project_path.mkdir()

        with patch("tero2.telegram_input.asyncio.create_subprocess_exec") as mock_exec:
            mock_proc = MagicMock()
            mock_proc.pid = 12345
            mock_proc.wait = AsyncMock()
            mock_proc.returncode = 1
            mock_proc.stderr = MagicMock()
            mock_proc.stderr.read = AsyncMock(return_value=b"ModuleNotFoundError")
            mock_exec.return_value = mock_proc

            await bot._launch_runner(project_path)

            assert mock_proc.wait.called, (
                "_launch_runner never calls proc.wait(). The subprocess "
                "could fail immediately with exit code 1 but the error is "
                "silently discarded. No stderr capture, no exit code check, "
                "no notification on failure."
            )
