"""Tests for runner reflexion integration — retry prompts include failure context."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.errors import RateLimitError
from tero2.state import AgentState, Phase


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
    config.retry.chain_retry_wait_s = 0.0
    return project, plan, config, disk


async def _fake_notify(text: str, level=None) -> bool:
    return True


class _CapturingChain:
    """Chain that captures the prompt it receives and always fails."""

    current_provider_index = 0

    def __init__(self):
        self.received_prompts: list[str] = []

    async def run_prompt(self, prompt: str):
        self.received_prompts.append(prompt)
        raise RateLimitError("fail for test")
        yield  # noqa: unreachable — make this async generator


class TestRunnerReflexionIntegration:
    @pytest.mark.asyncio
    async def test_second_attempt_contains_previous_attempts(self, tmp_path: Path):
        """After a forced failure, the second prompt should contain 'Previous Attempts'."""
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 3
        config.retry.chain_retry_wait_s = 0.0

        chain = _CapturingChain()

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        with patch.object(runner, "_build_chain", return_value=chain):
            await runner._execute_plan(
                AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
            )

        # Should have at least 2 attempts
        assert len(chain.received_prompts) >= 2, (
            f"Expected >= 2 attempts, got {len(chain.received_prompts)}"
        )
        # First prompt should NOT contain reflexion
        assert "Previous Attempts" not in chain.received_prompts[0], (
            "First attempt should not contain reflexion context"
        )
        # Second prompt SHOULD contain reflexion from first failure
        assert "Previous Attempts" in chain.received_prompts[1], (
            f"Second attempt prompt does not contain 'Previous Attempts'. "
            f"Got: {chain.received_prompts[1][:200]}"
        )

    @pytest.mark.asyncio
    async def test_run_agent_returns_captured_output(self, tmp_path: Path):
        """_run_agent should return (bool, str) with captured text output."""
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        chain = MagicMock()
        chain.current_provider_index = 0

        async def _yield_messages(prompt):
            yield {"type": "text", "text": "I am doing task 1"}
            yield {"type": "tool_result", "content": "result"}
            yield {"type": "turn_end"}

        chain.run_prompt = _yield_messages

        state = AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
        success, output = await runner._run_agent(chain, "test plan", state)

        assert success is True
        assert "I am doing task 1" in output

    @pytest.mark.asyncio
    async def test_run_agent_returns_output_on_failure(self, tmp_path: Path):
        """_run_agent should return captured output even on failure."""
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        class _FailChain:
            current_provider_index = 0

            async def run_prompt(self, prompt):
                yield {"type": "text", "text": "tried to build auth module"}
                raise RateLimitError("exhausted")

        state = AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
        success, output = await runner._run_agent(_FailChain(), "test plan", state)

        assert success is False
        assert "tried to build auth module" in output

    @pytest.mark.asyncio
    async def test_reflexion_includes_builder_output_from_previous(self, tmp_path: Path):
        """Reflexion context should include the builder's output from the failed attempt."""
        from tero2.runner import Runner

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 3
        config.retry.chain_retry_wait_s = 0.0

        chain = _CapturingChain()

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        with patch.object(runner, "_build_chain", return_value=chain):
            await runner._execute_plan(
                AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
            )

        if len(chain.received_prompts) >= 2:
            second_prompt = chain.received_prompts[1]
            # The reflexion section should reference the failure
            assert "FAILED" in second_prompt or "What was tried" in second_prompt, (
                f"Second prompt does not contain reflexion failure info. Got: {second_prompt[:300]}"
            )
