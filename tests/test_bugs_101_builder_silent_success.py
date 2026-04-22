"""Bug 101: builder treats silent-but-successful agents as failures.

When the builder runs via ``ctx.run_agent`` (the real agentic path used by
opencode/codex), the agent may complete the task by writing files, running
commands, and committing — without producing any text summary on stdout. The
old code saw an empty ``output.strip()``, failed recovery from disk, and
returned ``success=False`` with error ``"builder returned empty summary"``
even though ``run_agent`` already reported ``success=True``.

Observed live in night-loop iter-5: the testbed ended up with 3 valid commits
('add reverse_string utility', etc.) and 11/11 pytest passing, yet tero2
reported ``0/3 tasks passed`` because every builder call returned empty text.

The fix: when ``run_agent`` reports success but summary is empty after disk
recovery, trust the success signal and synthesize a minimal placeholder
summary so downstream phases don't see a false-negative failure.

Regression guard: the pre-existing B7 behavior — fallback path without
``ctx`` still fails on empty — must remain, so these tests exercise only
the ``ctx.run_agent`` path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tero2.disk_layer import DiskLayer
from tero2.players.builder import BuilderPlayer
from tero2.providers.chain import ProviderChain


def _make_disk(tmp_path: Path) -> DiskLayer:
    disk = DiskLayer(tmp_path)
    disk.init()
    return disk


def _make_ctx(run_agent_return: tuple[bool, str]) -> SimpleNamespace:
    """Mock a RunnerContext whose run_agent returns the given tuple."""
    ctx = SimpleNamespace()
    ctx.run_agent = AsyncMock(return_value=run_agent_return)
    return ctx


class TestAgentSilentSuccess:
    """When ctx.run_agent reports success=True with empty output, builder must succeed."""

    async def test_empty_output_with_agent_success_synthesizes_summary(
        self, tmp_path: Path
    ) -> None:
        disk = _make_disk(tmp_path)
        chain = MagicMock(spec=ProviderChain)
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))
        ctx = _make_ctx((True, ""))

        result = await player.run(
            task_plan="## T01: do something",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
            ctx=ctx,
        )

        assert result.success, (
            f"agent reported success=True but builder still failed: {result.error!r}"
        )
        assert result.summary, "synthesized summary must be non-empty"
        assert "T01" in result.summary
        # Summary was written to the expected .sora path.
        assert (disk.sora_dir / "milestones/M001/S01/T01-SUMMARY.md").exists()

    async def test_whitespace_only_output_with_agent_success(
        self, tmp_path: Path
    ) -> None:
        """Whitespace output is semantically empty — still synthesize."""
        disk = _make_disk(tmp_path)
        chain = MagicMock(spec=ProviderChain)
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))
        ctx = _make_ctx((True, "   \n\n  \t\n"))

        result = await player.run(
            task_plan="## T02: another",
            task_id="T02",
            milestone_path="milestones/M001",
            slice_id="S01",
            ctx=ctx,
        )

        assert result.success
        assert result.summary
        assert "T02" in result.summary

    async def test_agent_success_with_real_output_uses_that_output(
        self, tmp_path: Path
    ) -> None:
        """Regression: when agent DOES return text, that text wins over the placeholder."""
        disk = _make_disk(tmp_path)
        chain = MagicMock(spec=ProviderChain)
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))
        ctx = _make_ctx((True, "# Done\n- wrote file\n"))

        result = await player.run(
            task_plan="## T01: do",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
            ctx=ctx,
        )

        assert result.success
        assert result.summary.startswith("# Done")
        assert "synthesized" not in result.summary  # placeholder token absent

    async def test_agent_success_prefers_disk_recovery_over_placeholder(
        self, tmp_path: Path
    ) -> None:
        """When empty stdout but a SUMMARY.md exists on disk, recovered content wins."""
        (tmp_path / "T01-SUMMARY.md").write_text("# T01 Summary\nfrom disk\n")

        disk = _make_disk(tmp_path)
        chain = MagicMock(spec=ProviderChain)
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))
        ctx = _make_ctx((True, ""))

        result = await player.run(
            task_plan="## T01: do",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
            ctx=ctx,
        )

        assert result.success
        assert "from disk" in result.summary


class TestAgentFailureStillFails:
    """Regression: run_agent returning success=False must still fail."""

    async def test_agent_failure_is_propagated(self, tmp_path: Path) -> None:
        disk = _make_disk(tmp_path)
        chain = MagicMock(spec=ProviderChain)
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))
        ctx = _make_ctx((False, ""))

        result = await player.run(
            task_plan="## T01: do",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
            ctx=ctx,
        )

        assert not result.success
        assert result.error == "agent run did not succeed"

    async def test_agent_failure_with_text_output_does_not_synthesize(
        self, tmp_path: Path
    ) -> None:
        """success=False + text must still fail; synthesis is only for success=True+empty."""
        disk = _make_disk(tmp_path)
        chain = MagicMock(spec=ProviderChain)
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))
        ctx = _make_ctx((False, "partial output before failure"))

        result = await player.run(
            task_plan="## T01: do",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
            ctx=ctx,
        )

        assert not result.success


class TestFallbackPathUnchanged:
    """Regression guard for pre-existing B7 behaviour on the non-ctx path."""

    async def test_no_ctx_empty_response_still_fails(self, tmp_path: Path) -> None:
        """Without ctx, empty response from chain → failure, as before bug 101."""
        disk = _make_disk(tmp_path)
        chain = MagicMock(spec=ProviderChain)
        chain.run_prompt_collected = AsyncMock(return_value="")
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))

        result = await player.run(
            task_plan="## T01: do",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
        )

        assert not result.success
        assert "empty summary" in (result.error or "")
