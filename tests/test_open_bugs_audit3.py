"""Negative tests for open bugs from bugs.md Audit 3 (2026-04-21).

Convention: test FAILS when the bug is present (red), PASSES when fixed (green).

False positive — not a bug:
  Bug 70  scout: build_file_tree marks dirs as visited before depth check.
          Symlinks are NOT traversed (line 178: continue after showing ->).
          In a regular tree each dir appears at exactly one depth, and visited
          is local to each build_file_tree() call, so the ordering of
          visited.add vs depth check has no observable effect.

Bugs tested here (trivial / pure-function tests):
  Bug 61  architect: _recover_plan_from_disk returns plan that FAILS validation
  Bug 64  coach: _parse_sections silently overwrites duplicate headers
  Bug 73  verifier: string verify_commands iterated as characters (no TypeError)
  Bug 85  architect: validate_plan accepts tasks with empty descriptions
"""

from __future__ import annotations

import pathlib
from unittest.mock import MagicMock

import pytest

from tero2.players.architect import ArchitectPlayer, validate_plan
from tero2.players.coach import _parse_sections


# ── Bug 61 ────────────────────────────────────────────────────────────────


class TestBug61ArchitectInvertedRecover:
    """_recover_plan_from_disk returns the first plan that FAILS validation
    instead of the first one that PASSES."""

    def test_returns_valid_plan_not_invalid(self, tmp_path: pathlib.Path):
        # Write two candidates: invalid first, valid second.
        # The bug returns the invalid one because of the `not`.
        #
        # "valid" must pass validate_plan: needs a non-empty body description.
        valid = (
            "## T01: Do thing\n"
            "Implement the feature.\n"
            "**Must-haves:**\n"
            "- implement feature\n"
        )
        invalid = "this is not a plan at all"

        # Create the files architect scans.
        # _recover_plan_from_disk constructs the sora path as:
        #   disk.sora_dir / milestone_path / slice_id / f"{slice_id}-PLAN.md"
        # With milestone_path="milestones/S01" and slice_id="S01" that is:
        #   disk.sora_dir / "milestones/S01/S01/S01-PLAN.md"
        (tmp_path / "S01-PLAN.md").write_text(invalid)
        sora_dir = tmp_path / ".sora" / "milestones" / "S01" / "S01"
        sora_dir.mkdir(parents=True)
        (sora_dir / "S01-PLAN.md").write_text(valid)

        disk = MagicMock()
        disk.sora_dir = tmp_path / ".sora"

        player = ArchitectPlayer.__new__(ArchitectPlayer)
        player.working_dir = str(tmp_path)
        player.disk = disk

        result = player._recover_plan_from_disk("S01", "milestones/S01")
        assert result is not None, "should find at least one candidate"
        _path, content = result

        # Bug present: returns invalid (first that fails validation).
        # Fixed: returns valid (first that passes validation).
        # Note: _recover_plan_from_disk strips whitespace from file content.
        assert content == valid.strip(), (
            "should return the valid plan, not the invalid one"
        )


# ── Bug 64 ────────────────────────────────────────────────────────────────


class TestBug64CoachDuplicateSections:
    """_parse_sections overwrites duplicate headers silently."""

    def test_duplicate_headers_both_preserved(self):
        text = (
            "## STRATEGY\nFirst strategy content\n"
            "## TASK_QUEUE\nT01\n"
            "## STRATEGY\nSecond strategy content\n"
        )
        result = _parse_sections(text)

        # Bug present: result["STRATEGY"] == "Second strategy content" only.
        # Fixed: both chunks appear (concatenated or error raised).
        assert "First strategy content" in result.get("STRATEGY", ""), (
            "first STRATEGY section must not be silently dropped"
        )
        assert "Second strategy content" in result.get("STRATEGY", ""), (
            "second STRATEGY section must also be present"
        )


# ── Bug 73 ────────────────────────────────────────────────────────────────


class TestBug73VerifierNoTypeCheck:
    """Passing a string as verify_commands iterates characters instead of
    raising TypeError."""

    @pytest.mark.asyncio
    async def test_string_verify_commands_raises_type_error(self):
        from tero2.players.verifier import VerifierPlayer

        disk = MagicMock()
        chain = MagicMock()
        player = VerifierPlayer(chain, disk, working_dir=".")

        # Bug present: string is accepted, iterated as individual chars.
        # Fixed: TypeError is raised.
        with pytest.raises(TypeError, match="verify_commands"):
            await player.run(
                builder_output="ok",
                task_id="T01",
                verify_commands="ruff check .",
            )


# ── Bug 85 ────────────────────────────────────────────────────────────────


class TestBug85ArchitectEmptyDescription:
    """validate_plan accepts tasks with empty descriptions."""

    def test_empty_description_fails_validation(self):
        plan = (
            "## T01: Setup module\n"
            "**Must-haves:**\n"
            "- create file\n"
        )
        errors = validate_plan(plan)

        # Bug present: errors == [] (empty description passes).
        # Fixed: errors contains a message about empty description.
        assert len(errors) > 0, (
            "task with empty description should fail validation"
        )
