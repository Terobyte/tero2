"""Tests for bugs 26-33.

Each test is written RED first -- it fails against the current buggy code and
passes after the corresponding fix is applied.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# -- Bug 26 ----------------------------------------------------------------
# execute_phase.py: skipped (already-completed) tasks not added to `completed`


class TestBug26CrashRecoveryCompletedMap:
    """Bug 26: skipped tasks (crash recovery) must populate the completed dict."""

    async def _run_with_start_index(self, tmp_path, start_index, num_tasks=2):
        from tero2.checkpoint import CheckpointManager
        from tero2.circuit_breaker import CircuitBreakerRegistry
        from tero2.config import Config, ReflexionConfig, RoleConfig, TelegramConfig
        from tero2.disk_layer import DiskLayer
        from tero2.notifier import Notifier
        from tero2.phases.context import RunnerContext
        from tero2.phases.execute_phase import run_execute
        from tero2.players.architect import SlicePlan, Task
        from tero2.players.builder import BuilderResult
        from tero2.state import AgentState

        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        config = Config()
        config.telegram = TelegramConfig()
        config.reflexion = ReflexionConfig(max_cycles=0)
        config.roles["builder"] = RoleConfig(provider="fake")
        checkpoint = CheckpointManager(disk)
        notifier = Notifier(TelegramConfig())
        cb_registry = CircuitBreakerRegistry()
        state = AgentState(current_task_index=start_index, task_in_progress=False)
        ctx = RunnerContext(config, disk, checkpoint, notifier, state, cb_registry)
        ctx.build_chain = MagicMock(return_value=MagicMock())

        tasks = [Task(id=f"T{i:02d}", description=f"task {i}") for i in range(1, num_tasks + 1)]
        slice_plan = SlicePlan(slice_id="S01", slice_dir="milestones/M001/S01", tasks=tasks)

        # Write summary files for tasks that will be skipped (crash recovery implies
        # a previous run wrote them; Bug 66 fix now requires them to exist on disk).
        for i in range(1, start_index + 1):
            task_id = f"T{i:02d}"
            disk.write_file(
                f"milestones/M001/S01/{task_id}-SUMMARY.md",
                f"summary for {task_id}",
            )

        with patch("tero2.phases.execute_phase.BuilderPlayer") as MockB:
            inst = MagicMock()
            inst.run = AsyncMock(
                return_value=BuilderResult(
                    success=True,
                    output_file="milestones/M001/S01/T02-SUMMARY.md",
                    captured_output="done",
                )
            )
            MockB.return_value = inst
            result = await run_execute(ctx, slice_plan)

        return result

    async def test_skipped_tasks_appear_in_completed(self, tmp_path):
        """All tasks completed in a prior run must appear in the completed dict."""
        result = await self._run_with_start_index(tmp_path, start_index=2, num_tasks=2)

        assert result.success
        completed = result.data["completed"]
        assert "T01" in completed, (
            f"T01 was skipped (crash recovery) but missing from completed: {completed}"
        )
        assert "T02" in completed, (
            f"T02 was skipped (crash recovery) but missing from completed: {completed}"
        )

    async def test_skipped_task_summary_path_contains_task_id(self, tmp_path):
        """Summary path for a skipped task must embed the task ID."""
        result = await self._run_with_start_index(tmp_path, start_index=2, num_tasks=2)

        path = result.data["completed"]["T01"]
        assert "T01" in path, f"Expected T01 in summary path, got: {path!r}"
        assert "SUMMARY" in path, f"Expected SUMMARY in path, got: {path!r}"

    async def test_all_skipped_completed_count_is_two(self, tmp_path):
        """When all tasks were already done, completed dict must have all entries."""
        result = await self._run_with_start_index(tmp_path, start_index=2, num_tasks=2)

        assert result.success
        assert len(result.data["completed"]) == 2, (
            f"Expected 2 tasks in completed, got: {result.data['completed']}"
        )


# -- Bug 27 ----------------------------------------------------------------
# harden_phase.py: _parse_verdict checks NO ISSUES FOUND before CRITICAL


class TestBug27ParseVerdictPriority:
    """Bug 27: CRITICAL must beat NO ISSUES FOUND when both appear in output."""

    def test_critical_beats_no_issues_found(self):
        """Text with both markers must return 'critical', not 'no_issues'."""
        from tero2.phases.harden_phase import _parse_verdict

        output = "CRITICAL: missing error handling. No other NO ISSUES FOUND."
        verdict = _parse_verdict(output)
        assert verdict == "critical", (
            f"CRITICAL must take priority over NO ISSUES FOUND, got: {verdict!r}"
        )

    def test_no_issues_alone_returns_no_issues(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("NO ISSUES FOUND") == "no_issues"

    def test_critical_alone_returns_critical(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("CRITICAL: bad stuff") == "critical"

    def test_cosmetic_alone_returns_cosmetic(self):
        from tero2.phases.harden_phase import _parse_verdict

        assert _parse_verdict("COSMETIC: rename variable") == "cosmetic"

    def test_critical_with_no_issues_in_sentence(self):
        """'CRITICAL ... no issues found in module' must not trigger no_issues early."""
        from tero2.phases.harden_phase import _parse_verdict

        output = "CRITICAL bug found. No Issues Found in the test suite."
        assert _parse_verdict(output) == "critical"


# -- Bug 28 ----------------------------------------------------------------
# context_assembly.py: assemble_reviewer passes "reviewer" not role_key


class TestBug28AssembleReviewerRoleKey:
    """Bug 28: assemble_reviewer must use reviewer_review/reviewer_fix budget."""

    def test_review_mode_uses_reviewer_review_budget(self):
        """With a tiny reviewer_review context window, a large plan must hard-fail."""
        from tero2.config import Config, ContextConfig, RoleConfig
        from tero2.context_assembly import ContextAssembler
        from tero2.errors import ContextWindowExceededError

        cfg = Config()
        cfg.context = ContextConfig(
            target_ratio=0.70,
            warning_ratio=0.80,
            hard_fail_ratio=0.95,
        )
        cfg.roles = {"reviewer_review": RoleConfig(provider="openai", context_window=50)}
        asm = ContextAssembler(cfg, system_prompts={"reviewer_review": "sys"})
        big_plan = "x" * 800  # ~200 tokens >> 50 * 0.95 = 47.5 hard-fail threshold

        with pytest.raises(ContextWindowExceededError):
            asm.assemble_reviewer(big_plan, mode="review")

    def test_fix_mode_uses_reviewer_fix_budget(self):
        """With a tiny reviewer_fix context window, a large plan must hard-fail."""
        from tero2.config import Config, ContextConfig, RoleConfig
        from tero2.context_assembly import ContextAssembler
        from tero2.errors import ContextWindowExceededError

        cfg = Config()
        cfg.context = ContextConfig(
            target_ratio=0.70,
            warning_ratio=0.80,
            hard_fail_ratio=0.95,
        )
        cfg.roles = {"reviewer_fix": RoleConfig(provider="openai", context_window=50)}
        asm = ContextAssembler(cfg, system_prompts={"reviewer_fix": "sys"})
        big_plan = "x" * 800

        with pytest.raises(ContextWindowExceededError):
            asm.assemble_reviewer(big_plan, mode="fix")

    def test_review_mode_does_not_fall_back_to_reviewer_key(self):
        """reviewer_review budget must not silently fall back to 'reviewer' key."""
        from tero2.config import Config, ContextConfig, RoleConfig
        from tero2.context_assembly import ContextAssembler
        from tero2.errors import ContextWindowExceededError

        cfg = Config()
        cfg.context = ContextConfig(
            target_ratio=0.70,
            warning_ratio=0.80,
            hard_fail_ratio=0.95,
        )
        # Large "reviewer" key present -- must NOT be used; reviewer_review is tiny
        cfg.roles = {
            "reviewer": RoleConfig(provider="openai", context_window=128_000),
            "reviewer_review": RoleConfig(provider="openai", context_window=50),
        }
        asm = ContextAssembler(cfg, system_prompts={"reviewer_review": "sys"})
        big_plan = "x" * 800

        with pytest.raises(ContextWindowExceededError):
            asm.assemble_reviewer(big_plan, mode="review")


# -- Bug 29 ----------------------------------------------------------------
# telegram_input.py: _launch_runner blocks consumer for up to 30s


def _make_telegram_config(tmp_path):
    from tero2.config import Config, TelegramConfig

    config = Config()
    config.projects_dir = str(tmp_path / "projects")
    config.telegram = TelegramConfig(
        bot_token="test-token",
        chat_id="123",
        allowed_chat_ids=["123"],
    )
    return config


def _make_bot(config):
    from tero2.telegram_input import TelegramInputBot

    bot = TelegramInputBot(config)
    bot.notifier = MagicMock()
    bot.notifier.send = AsyncMock(return_value=True)
    return bot


class TestBug29LaunchRunnerNonBlocking:
    """Bug 29: _launch_runner must NOT block the consumer for 30 seconds."""

    async def test_launch_runner_returns_before_process_finishes(self, tmp_path):
        """_launch_runner must return within 5s even if the process runs for minutes."""
        bot = _make_bot(_make_telegram_config(tmp_path))

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None

        async def slow_wait():
            await asyncio.sleep(100)
            return 0

        mock_proc.wait = slow_wait
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            # Must complete well under 30s; the startup-failure watcher is off the critical path
            await asyncio.wait_for(
                bot._launch_runner(tmp_path / "project"),
                timeout=5.0,
            )
        # Reaching here means _launch_runner returned before the 30s watcher completed


# -- Bug 30 ----------------------------------------------------------------
# telegram_input.py: _launch_runner creates subprocess without stderr capture


class TestBug30LaunchRunnerCapturesStderr:
    """Bug 30: _launch_runner must capture stderr and include it in error notifications."""

    async def test_stderr_content_in_error_notification(self, tmp_path):
        """When runner exits non-zero, error notification must include stderr text."""
        bot = _make_bot(_make_telegram_config(tmp_path))

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = 1

        watcher_done = asyncio.Event()

        async def fast_wait():
            watcher_done.set()
            return 1

        mock_proc.wait = fast_wait
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(
            return_value=b"ModuleNotFoundError: No module named tero2.cli"
        )

        with patch("asyncio.create_subprocess_exec", new=AsyncMock(return_value=mock_proc)):
            await bot._launch_runner(tmp_path / "project")
            await asyncio.wait_for(watcher_done.wait(), timeout=5.0)
            await asyncio.sleep(0.05)  # let watcher coroutine reach notifier.send

        call_strs = [str(c) for c in bot.notifier.send.call_args_list]
        assert any("ModuleNotFoundError" in s for s in call_strs), (
            f"Expected stderr content in error notification, got: {call_strs}"
        )

    async def test_subprocess_created_with_stderr_pipe(self, tmp_path):
        """create_subprocess_exec must be called with stderr=PIPE."""
        bot = _make_bot(_make_telegram_config(tmp_path))

        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.returncode = None
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        async def slow_wait():
            await asyncio.sleep(100)
            return 0

        mock_proc.wait = slow_wait

        captured_kwargs: list = []

        async def capturing_exec(*args, **kwargs):
            captured_kwargs.append(kwargs)
            return mock_proc

        with patch("asyncio.create_subprocess_exec", new=capturing_exec):
            await asyncio.wait_for(
                bot._launch_runner(tmp_path / "project"),
                timeout=5.0,
            )

        assert captured_kwargs, "create_subprocess_exec was never called"
        assert captured_kwargs[0].get("stderr") == asyncio.subprocess.PIPE, (
            f"Expected stderr=PIPE in kwargs, got: {captured_kwargs[0]}"
        )


# -- Bug 31 ----------------------------------------------------------------
# verifier.py: _extract_list splits on whitespace, producing garbage entries


class TestBug31ExtractListTestIdOnly:
    """Bug 31: _extract_list must return only the test identifier, not trailing words."""

    def test_failed_line_returns_only_test_id(self):
        """Pytest FAILED line: only the test node ID, no noise after ' - '."""
        from tero2.players.verifier import _extract_list

        output = "FAILED tests/foo.py::test_bar - AssertionError: expected 1 got 2"
        result = _extract_list(output, "FAILED")

        assert result == ["tests/foo.py::test_bar"], f"Expected only test ID, got: {result}"

    def test_no_garbage_words_in_result(self):
        """No '-', error words, or plain numbers should appear in failed_tests."""
        from tero2.players.verifier import _extract_list

        output = "FAILED tests/bar.py::test_thing - ValueError: bad value 42"
        result = _extract_list(output, "FAILED")

        assert "-" not in result, f"'-' must not appear in result: {result}"
        assert "ValueError:" not in result, f"Error word must not appear: {result}"
        assert "42" not in result, f"Number must not appear: {result}"

    def test_multiple_failed_lines(self):
        """Multiple FAILED lines: one entry per test, no noise."""
        from tero2.players.verifier import _extract_list

        output = (
            "FAILED tests/a.py::test_one - AssertionError: oops\n"
            "FAILED tests/b.py::test_two - RuntimeError: boom\n"
        )
        result = _extract_list(output, "FAILED")

        assert result == ["tests/a.py::test_one", "tests/b.py::test_two"], (
            f"Expected two clean test IDs, got: {result}"
        )

    def test_empty_output_returns_empty_list(self):
        from tero2.players.verifier import _extract_list

        assert _extract_list("", "FAILED") == []

    def test_no_match_returns_empty_list(self):
        from tero2.players.verifier import _extract_list

        assert _extract_list("all tests passed", "FAILED") == []


# -- Bug 32 ----------------------------------------------------------------
# architect.py: validate_plan errors use '#1' instead of 'T01'


class TestBug32ValidatePlanTaskIdInErrors:
    """Bug 32: validate_plan errors must reference the task ID (T01), not '#1'."""

    def test_error_contains_task_id_not_index(self):
        """Missing must-haves error must say 'T01', not '#1'."""
        from tero2.players.architect import validate_plan

        plan = "## T01: do something\nJust a description with no requirements listed.\n"
        errors = validate_plan(plan)

        assert errors, "Expected validation errors for a task with no must-haves"
        error_text = " ".join(errors)
        assert "T01" in error_text, f"Expected 'T01' in error message, got: {error_text!r}"
        assert "#1" not in error_text, f"Must not use '#1' in error message, got: {error_text!r}"

    def test_error_for_second_task_contains_t02(self):
        """Missing must-haves for T02 must say 'T02', not '#2'."""
        from tero2.players.architect import validate_plan

        plan = (
            "## T01: first task\nMust-haves:\n- does something\n"
            "## T02: second task\nNo requirements here.\n"
        )
        errors = validate_plan(plan)
        error_text = " ".join(errors)

        assert "T02" in error_text, f"Expected 'T02' in error for second task, got: {error_text!r}"
        assert "#2" not in error_text, f"Must not use '#2' in error message, got: {error_text!r}"

    def test_valid_plan_still_returns_no_errors(self):
        """Sanity: well-formed plan must still produce no errors."""
        from tero2.players.architect import validate_plan

        plan = (
            "## T01: task one\nDo task one.\nMust-haves:\n- item a\n"
            "## T02: task two\nDo task two.\nMust-haves:\n- item b\n"
        )
        assert validate_plan(plan) == []


# -- Bug 33 ----------------------------------------------------------------
# persona.py: PersonaRegistry.get() ignores local .sora/prompts/ overrides


class TestBug33PersonaRegistryGetRespectLocal:
    """Bug 33: PersonaRegistry.get() must respect local .sora/prompts/ overrides."""

    def test_get_returns_local_prompt_over_bundled(self, tmp_path, monkeypatch):
        """get() must return local .sora/prompts/scout.md, not the bundled prompt."""
        from tero2.persona import PersonaRegistry, clear_cache

        monkeypatch.chdir(tmp_path)
        clear_cache()

        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "scout.md").write_text("local scout override via get", encoding="utf-8")

        reg = PersonaRegistry()
        p = reg.get("scout")

        assert p.system_prompt == "local scout override via get", (
            f"get() must respect local prompts, got: {p.system_prompt!r}"
        )

    def test_get_respects_constructor_override(self, tmp_path, monkeypatch):
        """Constructor overrides must still be honoured by get()."""
        from tero2.persona import PersonaRegistry, clear_cache

        monkeypatch.chdir(tmp_path)
        clear_cache()

        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "scout.md").write_text("local scout", encoding="utf-8")

        reg = PersonaRegistry(overrides={"scout": "constructor override"})
        p = reg.get("scout")

        assert p.system_prompt == "constructor override", (
            f"Constructor override must win over local file, got: {p.system_prompt!r}"
        )

    def test_get_consistent_with_load_or_default(self, tmp_path, monkeypatch):
        """get() and load_or_default() must return the same prompt for the same role."""
        from tero2.persona import PersonaRegistry, clear_cache

        monkeypatch.chdir(tmp_path)
        clear_cache()

        local_dir = tmp_path / ".sora" / "prompts"
        local_dir.mkdir(parents=True)
        (local_dir / "builder.md").write_text("consistent builder", encoding="utf-8")

        reg = PersonaRegistry()
        via_get = reg.get("builder")
        via_load = reg.load_or_default("builder")

        assert via_get.system_prompt == via_load.system_prompt, (
            f"get() and load_or_default() must agree: "
            f"{via_get.system_prompt!r} != {via_load.system_prompt!r}"
        )
