"""Bug 100: architect disk recovery misses plans/{slice_id}-PLAN.md.

When the architect's LLM runs in agent mode (opencode / codex), it often
writes the plan file to ``{working_dir}/plans/{slice_id}-PLAN.md`` rather
than returning it inline. Before the fix, ``_recover_plan_from_disk``
only checked::

    {working_dir}/{slice_id}-PLAN.md
    {sora_dir}/{milestone_path}/{slice_id}/{slice_id}-PLAN.md

so a perfectly-valid plan sitting in the ``plans/`` folder was invisible
to recovery. The LLM response itself was empty ("ok, I wrote the plan"),
``validate_plan`` returned "plan contains no tasks", recovery found
nothing, and the architect aborted. Observed live in night-loop iter-2
and iter-3.

Halal negative tests pin the new recovery path while protecting the
existing two locations.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tero2.disk_layer import DiskLayer
from tero2.players.architect import ArchitectPlayer, validate_plan
from tero2.providers.chain import ProviderChain


VALID_PLAN = textwrap.dedent(
    """\
    # S01 Plan

    ## Task T01: `reverse_string(s: str) -> str`
    - **Description:** implement `reverse_string`.
    - **Must-haves:**
      - [ ] `stringy/utils.py` defines reverse_string

    ## Task T02: `is_palindrome(s: str) -> bool`
    - **Description:** implement `is_palindrome`.
    - **Must-haves:**
      - [ ] `stringy/utils.py` defines is_palindrome
    """
)

INVALID_PLAN = "I wrote the plan. Please proceed.\n"


def _make_player(tmp_path: Path, llm_output: str) -> ArchitectPlayer:
    chain = MagicMock(spec=ProviderChain)
    chain.run_prompt_collected = AsyncMock(return_value=llm_output)
    disk = MagicMock(spec=DiskLayer)
    disk.read_file.return_value = ""
    disk.write_file.return_value = None
    disk.sora_dir = tmp_path / ".sora"
    return ArchitectPlayer(chain, disk, working_dir=str(tmp_path))


class TestPlansDirRecovery:
    """The ``plans/{slice_id}-PLAN.md`` location must be a recovery candidate."""

    async def test_recovers_plan_written_to_plans_subdir(
        self, tmp_path: Path
    ) -> None:
        """Halal: LLM returned no tasks, but wrote valid plan to plans/S01-PLAN.md."""
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "S01-PLAN.md").write_text(VALID_PLAN)

        # Pre-condition: the file exists and is a valid plan.
        assert validate_plan(VALID_PLAN) == []

        player = _make_player(tmp_path, llm_output=INVALID_PLAN)
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

        assert result.success, (
            f"expected architect to recover plan from plans/S01-PLAN.md, "
            f"got error: {result.error!r}"
        )
        assert result.task_count == 2
        assert "T01" in result.plan and "T02" in result.plan

    async def test_recovery_prefers_working_dir_root_over_plans(
        self, tmp_path: Path
    ) -> None:
        """Existing order: root-level {slice_id}-PLAN.md is tried BEFORE plans/ subdir.

        This protects code paths that already work today — tools that write
        to the project root should still be recovered first.
        """
        # Root file gets a distinctive task ID so we can tell them apart.
        root_plan = VALID_PLAN.replace("T01:", "T05:").replace("T02:", "T06:")
        (tmp_path / "S01-PLAN.md").write_text(root_plan)

        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "S01-PLAN.md").write_text(VALID_PLAN)

        player = _make_player(tmp_path, llm_output=INVALID_PLAN)
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

        assert result.success
        assert "T05" in result.plan, (
            "root-level plan must take precedence over plans/ subdir — "
            f"got plan containing: {[t for t in ['T01','T05'] if t in result.plan]}"
        )

    async def test_falls_back_to_plans_when_root_missing(
        self, tmp_path: Path
    ) -> None:
        """No root-level file, only plans/{slice_id}-PLAN.md → must still recover."""
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "S01-PLAN.md").write_text(VALID_PLAN)

        player = _make_player(tmp_path, llm_output=INVALID_PLAN)
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

        assert result.success
        assert result.task_count == 2

    async def test_no_recovery_when_plans_plan_invalid_and_no_others(
        self, tmp_path: Path
    ) -> None:
        """plans/{slice_id}-PLAN.md exists but is invalid → architect must still fail.

        Recovery must not silently accept garbage just because a file was
        found at the new candidate location.
        """
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir()
        (plans_dir / "S01-PLAN.md").write_text("just some words, no tasks\n")

        player = _make_player(tmp_path, llm_output=INVALID_PLAN)
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

        assert not result.success
        assert "plan contains no tasks" in (result.error or "")

    async def test_no_candidates_anywhere(self, tmp_path: Path) -> None:
        """No recovery files anywhere → architect fails with original error."""
        player = _make_player(tmp_path, llm_output=INVALID_PLAN)
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

        assert not result.success
        assert "plan contains no tasks" in (result.error or "")


class TestRegressionOriginalPaths:
    """Both existing recovery locations must still work unchanged."""

    async def test_still_recovers_from_working_dir_root(
        self, tmp_path: Path
    ) -> None:
        """Regression: {working_dir}/{slice_id}-PLAN.md was a candidate before the fix
        and must remain one."""
        (tmp_path / "S01-PLAN.md").write_text(VALID_PLAN)

        player = _make_player(tmp_path, llm_output=INVALID_PLAN)
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

        assert result.success
        assert result.task_count == 2
