"""Regression tests for the Architect-fails-with-empty-output incident.

Covers four bugs that together caused ``Architect failed: plan contains
no tasks`` / ``CLI stream error: Model not found: z.ai/glm-5.1.`` even
when the user thought they had selected a different model via RoleSwap:

* **B2** ``chain.run_prompt_collected`` detected ``{"type":"error"}`` events
  AFTER ``chain.run()`` returned, so the stream-error never triggered the
  per-provider retry or the fallback chain.
* **B3** ``Runner._idle_loop`` silently consumed ``switch_provider`` commands
  (only ``stop``/``steer``/``new_plan`` were handled), so any RoleSwap issued
  before the first plan was picked was lost.
* **B4** Agent-mode CLIs (opencode, claude, codex) honored the "write a
  plan file" wording in the prompt literally: they called a file-writing
  tool and replied with conversational narration. The architect parsed the
  narration and validation failed even though a valid plan existed on disk.

Run: ``pytest tests/test_architect_failure_regression.py -v``
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from tero2.config import Config, RetryConfig, RoleConfig
from tero2.errors import ProviderError, RateLimitError
from tero2.events import Command
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain


# ── B2: stream errors trigger retry + fallback ───────────────────────────


class _AlwaysStreamErrors(BaseProvider):
    """Yields a single ``{"type":"error"}`` event on every call.

    Mirrors opencode's behavior when asked for an unknown model: exit=0,
    but stdout contains an ``{"type":"error", "error":{...}}`` line.
    """

    def __init__(self, name: str = "bad-oc") -> None:
        self._name = name
        self.calls = 0

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        yield {
            "type": "error",
            "error": {"data": {"message": "Model not found: z.ai/glm-5.1."}},
        }


class _YieldsPlan(BaseProvider):
    """Yields a valid architect plan as dict content."""

    @property
    def display_name(self) -> str:
        return "good"

    async def run(self, **kwargs: Any):
        yield {"content": "## T01: do thing\n\n**Must-haves:**\n- it works\n"}


class TestB2StreamErrorTriggersRetryAndFallback:
    async def test_stream_error_triggers_per_provider_retry(self):
        """Primary provider emitting stream-error must be retried up to
        ``rate_limit_max_retries + 1`` times before falling through."""
        primary = _AlwaysStreamErrors("primary")
        backup = _YieldsPlan()
        chain = ProviderChain(
            [primary, backup],
            rate_limit_max_retries=2,
            rate_limit_wait_s=0.0,
        )
        result = await chain.run_prompt_collected("prompt")
        # 1 initial + 2 retries = 3 calls before fallback
        assert primary.calls == 3
        # Fallback produced the valid plan
        assert "T01" in result

    async def test_stream_error_falls_through_chain_to_success(self):
        """After primary exhausts retries, second provider succeeds and
        its output reaches the caller as a plain string."""
        chain = ProviderChain(
            [_AlwaysStreamErrors(), _YieldsPlan()],
            rate_limit_max_retries=1,
            rate_limit_wait_s=0.0,
        )
        result = await chain.run_prompt_collected("prompt")
        assert "## T01" in result
        assert "Must-haves" in result

    async def test_all_providers_stream_error_raises_rate_limit(self):
        """If every provider fails with stream-error, chain raises
        RateLimitError (same shape as when every provider rate-limits)."""
        chain = ProviderChain(
            [_AlwaysStreamErrors("a"), _AlwaysStreamErrors("b")],
            rate_limit_max_retries=0,
            rate_limit_wait_s=0.0,
        )
        with pytest.raises(RateLimitError):
            await chain.run_prompt_collected("prompt")

    async def test_single_provider_all_retries_fail_raises_rate_limit(self):
        """With a single bad provider, chain still surfaces RateLimitError
        rather than ProviderError, so the caller sees the canonical
        "all providers exhausted" signal."""
        chain = ProviderChain(
            [_AlwaysStreamErrors()],
            rate_limit_max_retries=1,
            rate_limit_wait_s=0.0,
        )
        with pytest.raises(RateLimitError):
            await chain.run_prompt_collected("prompt")


# ── B3: switch_provider handled in idle_loop ─────────────────────────────


def _make_cfg(architect_model: str = "zai/glm-5.1") -> Config:
    cfg = Config()
    cfg.roles["architect"] = RoleConfig(
        provider="opencode",
        model=architect_model,
        fallback=["codex", "kilo"],
    )
    cfg.retry = RetryConfig(chain_retry_wait_s=0.0, rate_limit_wait_s=0.0)
    return cfg


class TestB3IdleLoopSwitchProvider:
    async def test_switch_provider_in_idle_mutates_config(self, tmp_path: Path):
        """RoleSwap sent before any plan is picked must update
        ``self.config.roles[role]`` in-place, not be silently dropped."""
        from tero2.runner import Runner

        (tmp_path / ".sora").mkdir()
        runner = Runner(tmp_path, plan_file=None, config=_make_cfg())
        cmd = Command(
            "switch_provider",
            data={"role": "architect", "provider": "claude", "model": "opus"},
            source="test",
        )
        await runner._apply_switch_provider(cmd)
        assert runner.config.roles["architect"].provider == "claude"
        assert runner.config.roles["architect"].model == "opus"

    async def test_switch_provider_ignores_unknown_role(self, tmp_path: Path):
        from tero2.runner import Runner

        (tmp_path / ".sora").mkdir()
        runner = Runner(tmp_path, plan_file=None, config=_make_cfg())
        cmd = Command(
            "switch_provider",
            data={"role": "does-not-exist", "provider": "claude", "model": "opus"},
            source="test",
        )
        await runner._apply_switch_provider(cmd)
        assert runner.config.roles["architect"].provider == "opencode"

    async def test_switch_provider_without_model_key_preserves_model(
        self, tmp_path: Path
    ):
        """If the command omits ``model``, only provider is swapped — the
        previous model stays (mirrors old in-execution drain behavior)."""
        from tero2.runner import Runner

        (tmp_path / ".sora").mkdir()
        runner = Runner(tmp_path, plan_file=None, config=_make_cfg())
        cmd = Command(
            "switch_provider",
            data={"role": "architect", "provider": "codex"},
            source="test",
        )
        await runner._apply_switch_provider(cmd)
        assert runner.config.roles["architect"].provider == "codex"
        assert runner.config.roles["architect"].model == "zai/glm-5.1"

    async def test_idle_loop_consumes_switch_provider_then_new_plan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """End-to-end: idle_loop receives switch_provider first, then
        new_plan. The switch must stick (config mutated) BEFORE
        ``_execute_plan`` starts — otherwise the runner uses the stale
        provider/model and the whole RoleSwap-from-idle flow is broken."""
        from tero2.runner import Runner
        from tero2.state import AgentState, Phase

        (tmp_path / ".sora").mkdir()
        cfg = _make_cfg()
        cfg.idle_timeout_s = 1  # exit loop quickly if nothing happens

        runner = Runner(tmp_path, plan_file=None, config=cfg)
        runner._command_queue = asyncio.Queue()

        # Stub _execute_plan — we only care that config was already swapped
        # by the time the runner tries to execute the plan.
        seen_provider: list[str] = []

        async def fake_execute_plan(state: AgentState, shutdown_event=None) -> None:
            seen_provider.append(runner.config.roles["architect"].provider)

        monkeypatch.setattr(runner, "_execute_plan", fake_execute_plan)
        monkeypatch.setattr(
            runner, "_resolve_plan", lambda text: str(tmp_path / "plan.md")
        )
        (tmp_path / "plan.md").write_text("dummy plan")

        # Enqueue commands in the order the user would send them:
        #  1. r   → RoleSwap posts switch_provider
        #  2. l   → PlanPick posts new_plan
        await runner._command_queue.put(
            Command(
                "switch_provider",
                data={"role": "architect", "provider": "claude", "model": "opus"},
                source="tui",
            )
        )
        await runner._command_queue.put(
            Command("new_plan", data={"text": "plan.md"}, source="tui")
        )
        await runner._command_queue.put(Command("stop", source="tui"))

        await runner._idle_loop()

        # _execute_plan ran once, after switch_provider was applied.
        assert seen_provider == ["claude"]
        assert runner.config.roles["architect"].model == "opus"


# ── B4: architect recovers when agent writes plan to a file ──────────────


_VALID_PLAN = """# S01 Plan

## T01: First step

Do the thing.

**Must-haves:**
- it works
"""

_AGENT_NARRATION = (
    "Let me check the context.\n"
    "I have enough context now, writing the plan.\n"
    "Plan written to S01-PLAN.md.\n"
)


class TestB4AgentWrotePlanToFile:
    async def test_recovery_from_project_root(self, tmp_path: Path):
        """The common case: opencode/claude/codex agent writes the plan
        to ``<project_root>/S01-PLAN.md`` (their CLI working dir) and
        replies with narration. The architect must detect the file and
        use its content instead of the unparseable narration."""
        from tero2.disk_layer import DiskLayer
        from tero2.players.architect import ArchitectPlayer
        from tero2.providers.chain import ProviderChain

        class _Narrates(BaseProvider):
            @property
            def display_name(self) -> str:
                return "narrator"

            async def run(self, **kwargs: Any):
                yield _AGENT_NARRATION

        (tmp_path / ".sora").mkdir()
        (tmp_path / "S01-PLAN.md").write_text(_VALID_PLAN)

        player = ArchitectPlayer(
            ProviderChain([_Narrates()]),
            DiskLayer(tmp_path),
            working_dir=str(tmp_path),
        )
        result = await player.run(
            slice_id="S01",
            milestone_path="milestones/M001",
            roadmap="roadmap content",
        )
        assert result.success, result.error
        assert result.task_count == 1
        assert result.slice_plan is not None
        assert len(result.slice_plan.tasks) == 1
        assert result.slice_plan.tasks[0].id == "T01"

    async def test_recovery_prefers_valid_over_invalid(self, tmp_path: Path):
        """If project root has a junk ``S01-PLAN.md`` but the expected
        ``.sora/milestones/.../S01-PLAN.md`` has a valid one, recovery
        must skip invalid candidates and find the valid one."""
        from tero2.disk_layer import DiskLayer
        from tero2.players.architect import ArchitectPlayer
        from tero2.providers.chain import ProviderChain

        class _Narrates(BaseProvider):
            @property
            def display_name(self) -> str:
                return "narrator"

            async def run(self, **kwargs: Any):
                yield "just narration, no plan"

        (tmp_path / ".sora").mkdir()
        (tmp_path / "S01-PLAN.md").write_text("random junk, no T01 header")
        slice_dir = tmp_path / ".sora" / "milestones" / "M001" / "S01"
        slice_dir.mkdir(parents=True)
        (slice_dir / "S01-PLAN.md").write_text(_VALID_PLAN)

        player = ArchitectPlayer(
            ProviderChain([_Narrates()]),
            DiskLayer(tmp_path),
            working_dir=str(tmp_path),
        )
        result = await player.run(
            slice_id="S01",
            milestone_path="milestones/M001",
            roadmap="roadmap content",
        )
        assert result.success, result.error
        assert result.task_count == 1

    async def test_no_recovery_when_no_file(self, tmp_path: Path):
        """If the agent replied with narration AND didn't write a file,
        architect still fails with the validation error."""
        from tero2.disk_layer import DiskLayer
        from tero2.players.architect import ArchitectPlayer
        from tero2.providers.chain import ProviderChain

        class _Narrates(BaseProvider):
            @property
            def display_name(self) -> str:
                return "narrator"

            async def run(self, **kwargs: Any):
                yield _AGENT_NARRATION

        (tmp_path / ".sora").mkdir()

        player = ArchitectPlayer(
            ProviderChain([_Narrates()]),
            DiskLayer(tmp_path),
            working_dir=str(tmp_path),
        )
        result = await player.run(
            slice_id="S01",
            milestone_path="milestones/M001",
            roadmap="roadmap content",
        )
        assert not result.success
        assert "plan contains no tasks" in result.error

    async def test_text_reply_still_works(self, tmp_path: Path):
        """Golden path: when the agent correctly replies with plan text
        (not a file), the reply is used and no recovery runs."""
        from tero2.disk_layer import DiskLayer
        from tero2.players.architect import ArchitectPlayer
        from tero2.providers.chain import ProviderChain

        class _TextOnly(BaseProvider):
            @property
            def display_name(self) -> str:
                return "text-only"

            async def run(self, **kwargs: Any):
                yield _VALID_PLAN

        (tmp_path / ".sora").mkdir()
        # Write a stale file at project root — recovery should NOT be
        # preferred over the valid text reply.
        (tmp_path / "S01-PLAN.md").write_text("stale invalid content")

        player = ArchitectPlayer(
            ProviderChain([_TextOnly()]),
            DiskLayer(tmp_path),
            working_dir=str(tmp_path),
        )
        result = await player.run(
            slice_id="S01",
            milestone_path="milestones/M001",
            roadmap="roadmap content",
        )
        assert result.success, result.error
        assert result.plan.strip() == _VALID_PLAN.strip()
