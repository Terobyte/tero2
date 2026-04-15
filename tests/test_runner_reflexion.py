"""Tests for runner reflexion integration — retry prompts include failure context."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tero2.config import Config, RoleConfig, StuckDetectionConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.errors import RateLimitError
from tero2.runner import Runner
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
        yield  # unreachable — make this async generator


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
    async def test_run_agent_captures_plain_string_output(self, tmp_path: Path):
        """_run_agent must capture bare str messages (e.g. ShellProvider output)."""
        project, plan, config, disk = _make_project(tmp_path)
        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        class _PlainStringChain:
            """Simulates ShellProvider: yields a plain decoded string."""
            current_provider_index = 0

            async def run_prompt(self, prompt):
                yield "plain string output from provider"
                yield {"type": "turn_end"}

        state = AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
        success, output = await runner._run_agent(_PlainStringChain(), "test plan", state)

        assert "plain string output from provider" in output, (
            f"Plain string output not captured. Got: {output!r}"
        )

    @pytest.mark.asyncio
    async def test_run_agent_captures_content_keyed_dict(self, tmp_path: Path):
        """_run_agent must capture dict messages with 'content' key (not just 'text')."""
        project, plan, config, disk = _make_project(tmp_path)
        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        class _ContentDictChain:
            current_provider_index = 0

            async def run_prompt(self, prompt):
                yield {"type": "assistant", "content": "content-keyed output here"}
                yield {"type": "turn_end"}

        state = AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
        success, output = await runner._run_agent(_ContentDictChain(), "test plan", state)

        assert "content-keyed output here" in output, (
            f"content-key dict output not captured. Got: {output!r}"
        )

    @pytest.mark.asyncio
    async def test_run_agent_captures_content_attribute_object(self, tmp_path: Path):
        """_run_agent must capture objects with .content attribute (not just .text)."""
        project, plan, config, disk = _make_project(tmp_path)
        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        class _Msg:
            def __init__(self, content, msg_type="assistant"):
                self.content = content
                self.type = msg_type

        class _ContentAttrChain:
            current_provider_index = 0

            async def run_prompt(self, prompt):
                yield _Msg("object .content output")
                yield _Msg("", msg_type="turn_end")

        state = AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
        success, output = await runner._run_agent(_ContentAttrChain(), "test plan", state)

        assert "object .content output" in output, (
            f"object .content output not captured. Got: {output!r}"
        )

    @pytest.mark.asyncio
    async def test_add_attempt_receives_truncated_output(self, tmp_path: Path):
        """Output longer than MAX_BUILDER_OUTPUT_CHARS must be truncated before add_attempt."""
        from unittest.mock import patch

        from tero2.reflexion import MAX_BUILDER_OUTPUT_CHARS, add_attempt

        project, plan, config, disk = _make_project(tmp_path)
        config.retry.max_retries = 2
        config.retry.chain_retry_wait_s = 0.0

        captured_attempts: list[str] = []
        original_add_attempt = add_attempt

        def _spy_add_attempt(ctx, builder_output, **kwargs):
            captured_attempts.append(builder_output)
            return original_add_attempt(ctx, builder_output=builder_output, **kwargs)

        big_output = "x" * (MAX_BUILDER_OUTPUT_CHARS + 500)

        class _BigOutputChain:
            current_provider_index = 0

            async def run_prompt(self, prompt):
                yield big_output
                yield {"type": "turn_end"}
                raise RateLimitError("fail")

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify

        with (
            patch.object(runner, "_build_chain", return_value=_BigOutputChain()),
            patch("tero2.runner.add_attempt", side_effect=_spy_add_attempt),
        ):
            await runner._execute_plan(
                AgentState(phase=Phase.RUNNING, plan_file=str(plan), started_at="2025-01-01T00:00:00")
            )

        assert captured_attempts, "add_attempt was never called"
        for stored in captured_attempts:
            assert len(stored) <= MAX_BUILDER_OUTPUT_CHARS + len("... [truncated]"), (
                f"Stored output too long: {len(stored)} chars"
            )
            assert stored.endswith("... [truncated]"), (
                "Truncated output must end with '... [truncated]'"
            )

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


@pytest.mark.asyncio
async def test_reflexion_prompt_injected_on_second_attempt(tmp_path: Path) -> None:
    """After first failure, second attempt plan must include reflexion context."""
    project = tmp_path / "project"
    project.mkdir()
    DiskLayer(project).init()
    plan = project / "plan.md"
    plan.write_text("# Build auth\nImplement JWT.")

    cfg = Config()
    cfg.retry.max_retries = 2
    cfg.retry.chain_retry_wait_s = 0
    cfg.stuck_detection = StuckDetectionConfig(max_retries=999, max_steps_per_task=999)
    cfg.telegram = TelegramConfig()
    cfg.roles["executor"] = RoleConfig(provider="fake", timeout_s=5)

    capturing = _CapturingChain()
    runner = Runner(project, plan, config=cfg)
    runner.notifier.notify = _fake_notify  # type: ignore[method-assign]

    with patch.object(runner, "_build_chain", return_value=capturing):
        await runner.run()

    assert len(capturing.received_prompts) >= 2
    second_prompt = capturing.received_prompts[1]
    assert "Previous Attempts" in second_prompt or "FAILED" in second_prompt, (
        f"reflexion not injected in second attempt. Got: {second_prompt[:200]}"
    )
