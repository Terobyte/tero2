"""TDD proof-of-bug tests for OPEN bugs (6–10). All must be RED."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.config import Config, RoleConfig, TelegramConfig, _parse_config
from tero2.disk_layer import DiskLayer
from tero2.errors import ProviderError, RateLimitError
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain
from tero2.state import AgentState, Phase


class FakeProvider(BaseProvider):
    def __init__(self, name: str, items: list[Any] | None = None, error=None):
        self._name = name
        self._items = items or []
        self._error = error

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs):
        for item in self._items:
            yield item
        if self._error:
            raise self._error


class _ImmediateChain:
    current_provider_index = 0

    async def run_prompt(self, prompt: str):
        yield {"type": "tool_result", "content": "ok"}


async def _fake_notify(text: str, level=None) -> bool:
    return True


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


# ── Bug 6: _execute_plan ignores existing retry_count ──────────────────


class TestBug6RetryLoopIgnoresRetryCount:
    async def test_retry_count_respects_max_after_crash_recovery(self, tmp_path: Path):
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 3
        config.retry.chain_retry_wait_s = 0.0

        failing_chain = _AlwaysFailChain()

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        state = AgentState(
            phase=Phase.RUNNING,
            retry_count=2,
            steps_in_task=0,
            plan_file=str(plan),
            started_at="2025-01-01T00:00:00",
        )
        state.save(disk.sora_dir / "runtime" / "STATE.json")

        with patch.object(runner, "_build_chain", return_value=failing_chain):
            await runner._execute_plan(state)

        final = disk.read_state()
        assert final.retry_count <= config.retry.max_retries, (
            f"retry_count={final.retry_count} exceeds max_retries={config.retry.max_retries}. "
            f"_execute_plan uses range(max_retries) ignoring existing retry_count=2, "
            f"so after crash recovery the runner gets 3 extra attempts (total 5) instead of 1."
        )

    def test_execute_plan_source_accounts_for_retry_count(self):
        from tero2.runner import Runner

        source = inspect.getsource(Runner._execute_plan)
        loop_line = None
        for line in source.split("\n"):
            if "range(" in line and ("max_retries" in line or "max_attempts" in line):
                loop_line = line.strip()
                break
        assert loop_line is not None, "could not find retry loop"
        assert (
            "retry_count" in loop_line
            or "state.retry_count" in source.partition("range(")[0][-80:]
        ), (
            f"Retry loop: `{loop_line}` starts from 0 regardless of state.retry_count. "
            f"After crash recovery with retry_count=2 and max_retries=3, the loop "
            f"runs 3 more times for a total of 5 attempts. Should subtract existing "
            f"retry_count: range(state.retry_count, max_retries)."
        )


class _AlwaysFailChain:
    current_provider_index = 0

    async def run_prompt(self, prompt: str):
        raise RateLimitError("always fail")
        yield  # noqa: unreachable — make this async generator


# ── Bug 7: max_steps_per_task RuntimeError causes infinite crash loop ──


class TestBug7MaxStepsRuntimeErrorCrashLoop:
    async def test_max_steps_exceeded_marks_failed_not_crash(self, tmp_path: Path):
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_steps_per_task = 1
        config.retry.chain_retry_wait_s = 0.0

        msgs = [
            {"type": "tool_result", "content": "step1"},
            {"type": "tool_result", "content": "step2"},
        ]
        chain = _SlowChain(msgs, delay=0.01)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        with patch.object(runner, "_build_chain", return_value=chain):
            await runner.run()

        final = disk.read_state()
        assert final.phase == Phase.FAILED, (
            f"State is {final.phase.value} after max_steps exceeded. "
            f"Expected FAILED — runner should abort attempt at step limit and "
            f"exhaust retries rather than marking COMPLETED."
        )

    def test_increment_step_does_not_raise_on_limit(self):
        from tero2.checkpoint import CheckpointManager

        source = inspect.getsource(CheckpointManager.increment_step)
        assert "raise RuntimeError" not in source, (
            "increment_step must NOT raise RuntimeError when max_steps_per_task is exceeded. "
            "STEP_LIMIT is detected mid-attempt in _run_agent and handled gracefully."
        )


class _SlowChain:
    def __init__(self, messages: list[dict], delay: float = 0.1):
        self._messages = list(messages)
        self._delay = delay
        self.current_provider_index = 0

    async def run_prompt(self, prompt: str):
        for msg in self._messages:
            yield msg
            await asyncio.sleep(self._delay)


# ── Bug 8: ProviderChain yields duplicate messages on retry ─────────────


class TestBug8DuplicateMessagesOnRetry:
    async def test_no_duplicate_messages_on_retry(self):
        provider = FakeProvider(
            "partial",
            items=[
                {"type": "tool_result", "content": "step1"},
                {"type": "tool_result", "content": "step2"},
            ],
            error=RateLimitError("429"),
        )
        chain = ProviderChain(
            [provider],
            cb_registry=CircuitBreakerRegistry(),
            rate_limit_max_retries=1,
        )

        collected = []
        with pytest.raises(RateLimitError):
            async for msg in chain.run(prompt="x"):
                collected.append(msg)

        # With buffering: messages are only forwarded to the consumer on success.
        # When the provider always raises after yielding, all retries fail and
        # the buffer is discarded — consumer receives 0 messages (not 4 duplicates).
        assert len(collected) <= 2, (
            f"Consumer received {len(collected)} messages. "
            f"Provider yields 2 msgs then raises RateLimitError on every attempt. "
            f"With buffering, failed attempts must not forward messages — "
            f"consumer should see at most 2 messages (0 if all retries fail). "
            f"Got: {collected}."
        )

    async def test_runner_receives_duplicate_tool_results_via_chain(self, tmp_path: Path):
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_steps_per_task = 10
        config.retry.chain_retry_wait_s = 0.0
        config.retry.rate_limit_max_retries = 1
        config.retry.rate_limit_wait_s = 0.0

        partial_provider = FakeProvider(
            "partial",
            items=[
                {"type": "tool_result", "content": "step1"},
                {"type": "tool_result", "content": "step2"},
            ],
            error=RateLimitError("429"),
        )
        chain = ProviderChain(
            [partial_provider],
            cb_registry=CircuitBreakerRegistry(),
            rate_limit_max_retries=1,
            rate_limit_wait_s=0.0,
        )

        step_calls = 0
        orig_increment = None

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify
        orig_increment = runner.checkpoint.increment_step

        def counting_increment(state):
            nonlocal step_calls
            step_calls += 1
            return orig_increment(state)

        runner.checkpoint.increment_step = counting_increment

        with patch.object(runner, "_build_chain", return_value=chain):
            try:
                await runner.run()
            except Exception:
                pass

        assert step_calls <= 2, (
            f"increment_step called {step_calls} times in a single _run_agent call. "
            f"Provider yields 2 tool_results then raises RateLimitError. Chain retries "
            f"internally (rate_limit_max_retries=1) and yields the SAME 2 messages again. "
            f"Runner calls increment_step for each duplicate = {step_calls} calls instead of 2."
        )


class _PartialYieldThenFailChain:
    def __init__(self, messages: list[dict], fail_after: bool = True):
        self._messages = list(messages)
        self._fail_after = fail_after
        self.current_provider_index = 0

    async def run_prompt(self, prompt: str):
        for msg in self._messages:
            yield msg
        if self._fail_after:
            raise RateLimitError("transient error")


# ── Bug 9: Non-recoverable exceptions leave state RUNNING ──────────────


class TestBug9UnhandledExceptionLeavesStateRunning:
    async def test_config_error_marks_state_failed(self, tmp_path: Path):
        from tero2.errors import ConfigError

        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        async def boom(state, shutdown_event=None):
            raise ConfigError("something broke")

        with patch.object(runner, "_execute_plan", side_effect=boom):
            try:
                await runner.run()
            except ConfigError:
                pass

        final = disk.read_state()
        assert final.phase == Phase.FAILED, (
            f"State is {final.phase.value} after ConfigError. run() has no "
            f"catch-all to save FAILED state — only finally (lock.release) runs. "
            f"On next run, the state is RUNNING with stale data."
        )

    async def test_runtime_error_marks_state_failed(self, tmp_path: Path):
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        async def boom(state, shutdown_event=None):
            raise RuntimeError("unexpected")

        with patch.object(runner, "_execute_plan", side_effect=boom):
            try:
                await runner.run()
            except RuntimeError:
                pass

        final = disk.read_state()
        assert final.phase == Phase.FAILED, (
            f"State is {final.phase.value} after RuntimeError. Any non-recoverable "
            f"exception bypasses the retry loop, propagates out of run(), and "
            f"leaves the state as RUNNING on disk. Should be FAILED."
        )

    def test_run_source_has_catch_all_for_failed_state(self):
        from tero2.runner import Runner

        source = inspect.getsource(Runner.run)
        has_generic_except = False
        for line in source.split("\n"):
            stripped = line.strip()
            if stripped.startswith("except") and "LockHeldError" not in stripped:
                if "Exception" in stripped or "BaseException" in stripped:
                    has_generic_except = True
        assert has_generic_except, (
            "Runner.run() only catches LockHeldError. Non-recoverable exceptions "
            "(RuntimeError, ConfigError, etc.) propagate to the caller without "
            "saving FAILED state. Need a catch-all except that calls "
            "checkpoint.mark_failed() before re-raising."
        )


# ── Bug 10: CLIProvider yields stdout before checking return code ───────


class TestBug10CLIProviderYieldsBeforeExitCodeCheck:
    def test_cli_provider_source_checks_exit_code_before_yield(self):
        from tero2.providers.cli import CLIProvider

        source = inspect.getsource(CLIProvider.run)
        yield_lines = []
        exit_check_lines = []
        for i, line in enumerate(source.split("\n")):
            stripped = line.strip()
            if stripped.startswith("yield "):
                yield_lines.append(i)
            if "returncode" in stripped:
                exit_check_lines.append(i)
        assert exit_check_lines and yield_lines and min(exit_check_lines) < min(yield_lines), (
            f"CLIProvider.run() yields stdout lines (line {min(yield_lines)}) BEFORE "
            f"checking returncode (line {min(exit_check_lines)}). If the process "
            f"exits non-zero, all output has already been sent to the consumer, who "
            f"processes it as valid. Then ProviderError is raised and the chain "
            f"retries — causing all those actions to be duplicated."
        )

    async def test_cli_provider_does_not_yield_on_nonzero_exit(self):
        from tero2.providers.cli import CLIProvider

        provider = CLIProvider("test")
        provider._working_dir = "/tmp"

        proc_mock = MagicMock()
        proc_mock.returncode = 1
        proc_mock.stdin = MagicMock()
        proc_mock.stdin.write = MagicMock()
        proc_mock.stdin.drain = AsyncMock()
        proc_mock.stdin.close = MagicMock()
        proc_mock.stdin.wait_closed = AsyncMock()

        async def fake_stdout():
            yield b'{"type":"tool_result","content":"did_something"}\n'
            yield b'{"type":"tool_result","content":"did_more"}\n'

        proc_mock.stdout = fake_stdout()

        async def fake_create_task(coro, **kw):
            return await coro

        async def fake_wait(proc):
            pass

        proc_mock.wait = AsyncMock()
        proc_mock.stderr = MagicMock()
        proc_mock.stderr.read = AsyncMock(return_value=b"error msg")

        with patch("tero2.providers.cli.asyncio.create_subprocess_exec", return_value=proc_mock):
            with patch("tero2.providers.cli.asyncio.create_task", side_effect=fake_create_task):
                collected = []
                with pytest.raises(ProviderError):
                    async for msg in provider.run(prompt="do stuff"):
                        collected.append(msg)

                assert len(collected) == 0, (
                    f"CLIProvider yielded {len(collected)} messages before raising "
                    f"ProviderError for non-zero exit code. Output from a failed "
                    f"command should not be delivered to the consumer — it may be "
                    f"partial, corrupt, or represent actions that were rolled back."
                )
