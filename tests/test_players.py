"""Tests for player modules: mocked deps, architect validation, verifier verdict."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

from tero2.disk_layer import DiskLayer
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain


class _StubProvider(BaseProvider):
    @property
    def display_name(self) -> str:
        return "stub"

    async def run(self, **kwargs: Any):
        yield ""


def _make_chain(response: str = "") -> ProviderChain:
    chain = ProviderChain([_StubProvider()])
    chain.run_prompt_collected = AsyncMock(return_value=response)  # type: ignore[attr-defined]
    return chain


def _make_disk(tmp_path: Path) -> DiskLayer:
    return DiskLayer(tmp_path)


def run(coro):
    return asyncio.run(coro)


# ── PlayerResult contract ──────────────────────────────────────────────


class TestPlayerResultContract:
    def test_fields(self):
        from tero2.players.base import PlayerResult

        r = PlayerResult(success=True, output_file="a.md", captured_output="text", error="")
        assert r.success is True
        assert r.output_file == "a.md"
        assert r.captured_output == "text"
        assert r.error == ""

    def test_defaults(self):
        from tero2.players.base import PlayerResult

        r = PlayerResult(success=False, error="boom")
        assert r.output_file == ""
        assert r.captured_output == ""


# ── Scout ──────────────────────────────────────────────────────────────


class TestScoutPlayer:
    def test_run_writes_context_map(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain("mapped codebase")
        disk = _make_disk(tmp_path)
        scout = ScoutPlayer(chain, disk, working_dir=str(tmp_path))
        result = run(scout.run(milestone_path="milestones/M001"))
        assert result.success is True
        assert result.captured_output == "mapped codebase"
        assert "CONTEXT_MAP.md" in result.output_file
        assert disk.read_file("milestones/M001/CONTEXT_MAP.md") == "mapped codebase"

    def test_run_failure(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        chain = _make_chain("x")
        chain.run_prompt_collected = AsyncMock(side_effect=RuntimeError("fail"))  # type: ignore[attr-defined]
        disk = _make_disk(tmp_path)
        scout = ScoutPlayer(chain, disk)
        result = run(scout.run())
        assert result.success is False
        assert "fail" in result.error

    def test_run_injects_project_md_into_prompt(self, tmp_path: Path) -> None:
        """Scout must include PROJECT.md content in the LLM prompt when present."""
        from tero2.players.scout import ScoutPlayer

        disk = _make_disk(tmp_path)
        disk.write_file("persistent/PROJECT.md", "Python 3.12 async project, no ORM")

        captured: list[str] = []

        async def _capture(prompt: str) -> str:
            captured.append(prompt)
            return "context map"

        chain = _make_chain("")
        scout = ScoutPlayer(chain, disk, working_dir=str(tmp_path))
        scout._run_prompt = _capture  # type: ignore[method-assign]
        result = run(scout.run(milestone_path="milestones/M001"))
        assert result.success is True
        assert captured, "prompt was never built"
        assert "Python 3.12 async project, no ORM" in captured[0]

    def test_run_skips_project_md_when_absent(self, tmp_path: Path) -> None:
        """Scout must degrade gracefully when PROJECT.md is missing."""
        from tero2.players.scout import ScoutPlayer

        disk = _make_disk(tmp_path)
        captured: list[str] = []

        async def _capture(prompt: str) -> str:
            captured.append(prompt)
            return "context map"

        chain = _make_chain("")
        scout = ScoutPlayer(chain, disk, working_dir=str(tmp_path))
        scout._run_prompt = _capture  # type: ignore[method-assign]
        result = run(scout.run())
        assert result.success is True
        assert captured
        # No PROJECT.md section in the prompt.
        assert "PROJECT.md" not in captured[0]

    def test_should_skip_below_threshold(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        assert ScoutPlayer.should_skip(str(tmp_path), skip_threshold=100) is True

    def test_should_not_skip_above_threshold(self, tmp_path: Path) -> None:
        from tero2.players.scout import ScoutPlayer

        # Create a file so count > 0.
        (tmp_path / "main.py").write_text("x = 1")
        assert ScoutPlayer.should_skip(str(tmp_path), skip_threshold=0) is False


# ── Architect ──────────────────────────────────────────────────────────


class TestArchitectPlayer:
    def test_run_writes_plan_to_correct_path(self, tmp_path: Path) -> None:
        from tero2.players.architect import ArchitectPlayer

        plan = "## T01: Setup\nMust-haves: init module"
        chain = _make_chain(plan)
        disk = _make_disk(tmp_path)
        arch = ArchitectPlayer(chain, disk)
        result = run(arch.run(slice_id="S01", milestone_path="milestones/M001"))
        assert result.success is True
        assert result.output_file == "milestones/M001/S01/S01-PLAN.md"
        assert disk.read_file("milestones/M001/S01/S01-PLAN.md") == plan

    def test_run_failure(self, tmp_path: Path) -> None:
        from tero2.players.architect import ArchitectPlayer

        chain = _make_chain("x")
        chain.run_prompt_collected = AsyncMock(side_effect=RuntimeError("fail"))  # type: ignore[attr-defined]
        disk = _make_disk(tmp_path)
        arch = ArchitectPlayer(chain, disk)
        result = run(arch.run())
        assert result.success is False


# ── Architect validation ───────────────────────────────────────────────


class TestArchitectValidation:
    def test_valid_plan(self):
        from tero2.players.architect import validate_plan

        plan = "## T01: Setup\nMust-haves: init module\n## T02: Test\nMust-haves: tests pass\n"
        assert validate_plan(plan) == []

    def test_no_tasks(self):
        from tero2.players.architect import validate_plan

        assert "no tasks" in " ".join(validate_plan("no headings here"))

    def test_too_many_tasks(self):
        from tero2.players.architect import validate_plan

        plan = "\n".join(f"## T{i:02d}: Task\nMust-haves: mh{i}\n" for i in range(1, 9))
        errs = validate_plan(plan)
        assert any("max" in e for e in errs)

    def test_task_missing_must_haves(self):
        from tero2.players.architect import validate_plan

        plan = "## T01: Setup\nJust some description with no requirements listed\n"
        errs = validate_plan(plan)
        assert any("missing must-haves" in e for e in errs)

    def test_spec_format_counted(self):
        from tero2.players.architect import _count_tasks

        plan = "## T01: Setup\nMust-haves: x\n## T02: Build\nMust-haves: y\n"
        assert _count_tasks(plan) == 2

    def test_old_format_not_counted(self):
        from tero2.players.architect import _count_tasks

        plan = "### Task T01\nMust-haves: x\n"
        assert _count_tasks(plan) == 0

    def test_dependencies_plural_unknown_ref(self):
        from tero2.players.architect import validate_plan

        plan = "## T01: Setup\nMust-haves: init module\nDependencies: T99\n"
        errs = validate_plan(plan)
        assert any("unknown task T99" in e for e in errs)

    def test_depends_on_valid_ref(self):
        from tero2.players.architect import validate_plan

        plan = (
            "## T01: Base\nMust-haves: core module\n"
            "## T02: Build\nMust-haves: feature\nDepends on: T01\n"
        )
        assert validate_plan(plan) == []


# ── SlicePlan parser ──────────────────────────────────────────────────


_VALID_SLICE_PLAN = """\
## T01: Init module
Set up the base package structure.

**Must-haves:**
- `tero2/__init__.py` exists
- package importable

## T02: Add config loader
Parse TOML config.

**Must-haves:**
- `Config` dataclass with all required fields
- defaults match spec
"""


class TestParseSlicePlan:
    def test_parses_two_tasks(self) -> None:
        from tero2.players.architect import SlicePlan, _parse_slice_plan

        result = _parse_slice_plan(_VALID_SLICE_PLAN, "S01")
        assert isinstance(result, SlicePlan)
        assert result.slice_id == "S01"
        assert result.slice_dir == "milestones/M001/S01"
        assert len(result.tasks) == 2

    def test_task_ids_extracted(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        result = _parse_slice_plan(_VALID_SLICE_PLAN, "S01")
        assert result.tasks[0].id == "T01"
        assert result.tasks[1].id == "T02"

    def test_descriptions_stripped(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        result = _parse_slice_plan(_VALID_SLICE_PLAN, "S01")
        assert "base package structure" in result.tasks[0].description

    def test_must_haves_parsed(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        result = _parse_slice_plan(_VALID_SLICE_PLAN, "S01")
        mh = result.tasks[0].must_haves
        assert any("__init__.py" in item for item in mh)

    def test_slice_dir_uses_milestone_path(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        result = _parse_slice_plan(_VALID_SLICE_PLAN, "S02", milestone_path="milestones/M002")
        assert result.slice_dir == "milestones/M002/S02"

    def test_empty_plan_returns_empty_tasks(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        result = _parse_slice_plan("No task headers here.", "S01")
        assert result.tasks == []

    def test_single_task_no_must_haves_section(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        plan = "## T01: Do something\nJust a description with no must-haves marker.\n"
        result = _parse_slice_plan(plan, "S01")
        assert len(result.tasks) == 1
        assert result.tasks[0].id == "T01"
        assert result.tasks[0].must_haves == []

    def test_architect_result_contains_slice_plan(self, tmp_path: Path) -> None:
        """ArchitectPlayer.run() must embed a SlicePlan in the result."""
        from tero2.players.architect import ArchitectPlayer, SlicePlan

        chain = _make_chain(_VALID_SLICE_PLAN)
        disk = _make_disk(tmp_path)
        arch = ArchitectPlayer(chain, disk)
        result = run(arch.run(slice_id="S01", milestone_path="milestones/M001"))
        assert result.success is True
        assert isinstance(result.slice_plan, SlicePlan)
        assert len(result.slice_plan.tasks) == 2

    def test_architect_reads_roadmap_fallback(self, tmp_path: Path) -> None:
        """Architect must fall back to ROADMAP.md when PLAN.md is absent."""
        from tero2.players.architect import ArchitectPlayer

        disk = _make_disk(tmp_path)
        disk.write_file("milestones/M001/ROADMAP.md", "Build the core module")

        captured: list[str] = []

        async def _capture(prompt: str) -> str:
            captured.append(prompt)
            return _VALID_SLICE_PLAN

        chain = _make_chain(_VALID_SLICE_PLAN)
        arch = ArchitectPlayer(chain, disk)
        arch._run_prompt = _capture  # type: ignore[method-assign]
        run(arch.run(slice_id="S01", milestone_path="milestones/M001"))
        assert captured, "prompt was never built"
        assert "Build the core module" in captured[0]

    def test_architect_reads_plan_over_roadmap(self, tmp_path: Path) -> None:
        """PLAN.md takes precedence over ROADMAP.md when both exist."""
        from tero2.players.architect import ArchitectPlayer

        disk = _make_disk(tmp_path)
        disk.write_file("milestones/M001/PLAN.md", "Hardened plan content")
        disk.write_file("milestones/M001/ROADMAP.md", "Old roadmap — must not appear")

        captured: list[str] = []

        async def _capture(prompt: str) -> str:
            captured.append(prompt)
            return _VALID_SLICE_PLAN

        chain = _make_chain(_VALID_SLICE_PLAN)
        arch = ArchitectPlayer(chain, disk)
        arch._run_prompt = _capture  # type: ignore[method-assign]
        run(arch.run(slice_id="S01", milestone_path="milestones/M001"))
        assert captured
        assert "Hardened plan content" in captured[0]
        assert "Old roadmap" not in captured[0]


# ── Builder ────────────────────────────────────────────────────────────


class TestBuilderPlayer:
    def test_run_writes_summary(self, tmp_path: Path) -> None:
        from tero2.players.builder import BuilderPlayer

        chain = _make_chain("done: implemented feature")
        disk = _make_disk(tmp_path)
        builder = BuilderPlayer(chain, disk)
        result = run(
            builder.run(
                task_id="T01",
                slice_id="S01",
                milestone_path="milestones/M001",
            )
        )
        assert result.success is True
        assert result.output_file == "milestones/M001/S01/T01-SUMMARY.md"
        assert "implemented feature" in result.captured_output

    def test_run_failure(self, tmp_path: Path) -> None:
        from tero2.players.builder import BuilderPlayer

        chain = _make_chain("x")
        chain.run_prompt_collected = AsyncMock(side_effect=RuntimeError("fail"))  # type: ignore[attr-defined]
        disk = _make_disk(tmp_path)
        builder = BuilderPlayer(chain, disk)
        result = run(builder.run())
        assert result.success is False

    def test_run_uses_ctx_run_agent_when_provided(self, tmp_path: Path) -> None:
        """When ctx is provided with run_agent, Builder delegates to ctx.run_agent."""
        from tero2.players.builder import BuilderPlayer

        chain = _make_chain("")
        disk = _make_disk(tmp_path)
        builder = BuilderPlayer(chain, disk)

        captured_calls: list[tuple] = []

        async def _fake_run_agent(_ctx, ch, prompt: str, **kwargs) -> tuple[bool, str]:
            captured_calls.append((ch, prompt))
            return True, "agent wrote the files\n\nSummary: done"

        ctx = type("Ctx", (), {"run_agent": _fake_run_agent})()
        result = run(
            builder.run(
                task_plan="implement X",
                context_hints="use async",
                task_id="T02",
                slice_id="S01",
                milestone_path="milestones/M001",
                ctx=ctx,
            )
        )
        assert result.success is True
        assert result.task_id == "T02"
        assert result.output_file == "milestones/M001/S01/T02-SUMMARY.md"
        assert "Summary: done" in result.captured_output
        assert len(captured_calls) == 1
        # ctx.run_agent received the builder's own chain and a non-empty prompt
        assert captured_calls[0][0] is chain
        assert "implement X" in captured_calls[0][1]
        assert "use async" in captured_calls[0][1]
        # Summary is written to disk
        assert "Summary: done" in disk.read_file("milestones/M001/S01/T02-SUMMARY.md")

    def test_run_with_ctx_failure(self, tmp_path: Path) -> None:
        """When ctx.run_agent returns (False, ...), Builder propagates the failure."""
        from tero2.players.builder import BuilderPlayer

        chain = _make_chain("")
        disk = _make_disk(tmp_path)
        builder = BuilderPlayer(chain, disk)

        async def _failing_run_agent(_ctx, ch, prompt: str, **kwargs) -> tuple[bool, str]:
            return False, "partial output before step limit"

        ctx = type("Ctx", (), {"run_agent": _failing_run_agent})()
        result = run(
            builder.run(
                task_id="T03",
                ctx=ctx,
            )
        )
        assert result.success is False
        assert result.task_id == "T03"
        assert "partial output before step limit" in result.captured_output
        assert "agent run did not succeed" in result.error

    def test_run_without_ctx_uses_chain_fallback(self, tmp_path: Path) -> None:
        """Without ctx, Builder falls back to chain.run_prompt_collected."""
        from tero2.players.builder import BuilderPlayer

        chain = _make_chain("fallback output")
        disk = _make_disk(tmp_path)
        builder = BuilderPlayer(chain, disk)
        result = run(builder.run(task_id="T01", slice_id="S01", milestone_path="milestones/M001"))
        assert result.success is True
        assert "fallback output" in result.captured_output


# ── Verifier ───────────────────────────────────────────────────────────


class TestVerifierVerdictParsing:
    def test_pass_when_both_succeed(self):
        from tero2.players.verifier import Verdict, _parse_verdict

        assert _parse_verdict("all good", [0, 0]) == Verdict.PASS

    def test_fail_on_ruff_error(self):
        from tero2.players.verifier import Verdict, _parse_verdict

        assert _parse_verdict("lint issue", [1, 0]) == Verdict.FAIL

    def test_fail_on_pytest_error(self):
        from tero2.players.verifier import Verdict, _parse_verdict

        assert _parse_verdict("test failed", [0, 1]) == Verdict.FAIL

    def test_anomaly_detected_first(self):
        from tero2.players.verifier import Verdict, _parse_verdict

        assert _parse_verdict("ANOMALY detected", [1, 1]) == Verdict.ANOMALY

    def test_fail_does_not_match_pass_substring(self):
        from tero2.players.verifier import Verdict, _parse_verdict

        assert _parse_verdict("PASS was not achieved", [0, 1]) == Verdict.FAIL


class TestVerifierPlayer:
    def test_run_with_mocked_subprocess(self, tmp_path: Path) -> None:
        from tero2.players.verifier import VerifierPlayer

        chain = _make_chain("")
        disk = _make_disk(tmp_path)
        verifier = VerifierPlayer(chain, disk, working_dir=str(tmp_path))
        with patch("tero2.players.verifier._run_subprocess") as mock_sub:
            mock_sub.side_effect = [
                (0, "All checks passed", ""),
                (0, "3 passed", ""),
            ]
            result = run(verifier.run())
            assert result.success is True
            assert result.verdict == "PASS"
            assert mock_sub.call_count == 2

    def test_run_ruff_fails(self, tmp_path: Path) -> None:
        from tero2.players.verifier import VerifierPlayer

        chain = _make_chain("")
        disk = _make_disk(tmp_path)
        verifier = VerifierPlayer(chain, disk, working_dir=str(tmp_path))
        with patch("tero2.players.verifier._run_subprocess") as mock_sub:
            mock_sub.side_effect = [
                (1, "", "unused import"),
                (0, "3 passed", ""),
            ]
            result = run(verifier.run())
            assert result.success is False
            assert result.verdict == "FAIL"


# ── Coach ───────────────────────────────────────────────────────────────


_FULL_COACH_RESPONSE = """\
## STRATEGY
Focus on stability.

## TASK_QUEUE
1. Fix linter issues

## RISK
Low risk.

## CONTEXT_HINTS
Use async patterns.
"""


class TestCoachPlayer:
    def test_run_writes_all_four_sections(self, tmp_path: Path) -> None:
        from tero2.players.coach import CoachPlayer

        chain = _make_chain(_FULL_COACH_RESPONSE)
        disk = _make_disk(tmp_path)
        coach = CoachPlayer(chain, disk)
        result = run(coach.run(milestone_path="milestones/M001", slice_id="S01"))
        assert result.success is True
        assert result.strategy == "Focus on stability."
        assert result.task_queue == "1. Fix linter issues"
        assert result.risk == "Low risk."
        assert result.context_hints == "Use async patterns."
        assert disk.read_file("strategic/STRATEGY.md") == "Focus on stability."
        assert disk.read_file("strategic/TASK_QUEUE.md") == "1. Fix linter issues"
        assert disk.read_file("strategic/RISK.md") == "Low risk."
        assert disk.read_file("strategic/CONTEXT_HINTS.md") == "Use async patterns."

    def test_run_reads_roadmap_from_disk(self, tmp_path: Path) -> None:
        """_gather_context must read ROADMAP.md from the milestone directory."""
        from tero2.players.coach import CoachPlayer

        disk = _make_disk(tmp_path)
        disk.write_file("milestones/M001/ROADMAP.md", "Build great software")

        captured: list[str] = []

        async def _capture_prompt(prompt: str) -> str:
            captured.append(prompt)
            return _FULL_COACH_RESPONSE

        chain = _make_chain(_FULL_COACH_RESPONSE)
        coach = CoachPlayer(chain, disk)
        coach._run_prompt = _capture_prompt  # type: ignore[method-assign]
        run(coach.run(milestone_path="milestones/M001", slice_id="S01"))
        assert captured, "prompt was never built"
        assert "Build great software" in captured[0]

    def test_run_reads_context_map_from_disk(self, tmp_path: Path) -> None:
        """_gather_context must read CONTEXT_MAP.md produced by Scout."""
        from tero2.players.coach import CoachPlayer

        disk = _make_disk(tmp_path)
        disk.write_file("milestones/M001/CONTEXT_MAP.md", "src/ → main module")

        captured: list[str] = []

        async def _capture_prompt(prompt: str) -> str:
            captured.append(prompt)
            return _FULL_COACH_RESPONSE

        chain = _make_chain(_FULL_COACH_RESPONSE)
        coach = CoachPlayer(chain, disk)
        coach._run_prompt = _capture_prompt  # type: ignore[method-assign]
        run(coach.run(milestone_path="milestones/M001", slice_id="S01"))
        assert "src/ → main module" in captured[0]

    def test_run_reads_metrics_from_disk(self, tmp_path: Path) -> None:
        """_gather_context must read metrics.json for budget awareness."""
        from tero2.players.coach import CoachPlayer

        disk = _make_disk(tmp_path)
        disk.write_metrics({"total_cost_usd": 1.23, "tokens": 50000})

        captured: list[str] = []

        async def _capture_prompt(prompt: str) -> str:
            captured.append(prompt)
            return _FULL_COACH_RESPONSE

        chain = _make_chain(_FULL_COACH_RESPONSE)
        coach = CoachPlayer(chain, disk)
        coach._run_prompt = _capture_prompt  # type: ignore[method-assign]
        run(coach.run(milestone_path="milestones/M001", slice_id="S01"))
        assert "1.23" in captured[0]

    def test_run_reads_steer_from_disk(self, tmp_path: Path) -> None:
        """_gather_context must read human/STEER.md when present."""
        from tero2.players.coach import CoachPlayer

        disk = _make_disk(tmp_path)
        disk.write_file("human/STEER.md", "Change direction: focus on tests")

        captured: list[str] = []

        async def _capture_prompt(prompt: str) -> str:
            captured.append(prompt)
            return _FULL_COACH_RESPONSE

        chain = _make_chain(_FULL_COACH_RESPONSE)
        coach = CoachPlayer(chain, disk)
        coach._run_prompt = _capture_prompt  # type: ignore[method-assign]
        run(coach.run(milestone_path="milestones/M001", slice_id="S01"))
        assert "Change direction: focus on tests" in captured[0]

    def test_malformed_response_does_not_overwrite_existing_files(self, tmp_path: Path) -> None:
        """Empty parsed sections must NOT overwrite existing strategic files."""
        from tero2.players.coach import CoachPlayer

        disk = _make_disk(tmp_path)
        # Pre-seed existing strategy files.
        disk.write_file("strategic/STRATEGY.md", "existing strategy")
        disk.write_file("strategic/TASK_QUEUE.md", "existing queue")
        disk.write_file("strategic/RISK.md", "existing risk")
        disk.write_file("strategic/CONTEXT_HINTS.md", "existing hints")

        # LLM returns a response with no parseable sections.
        chain = _make_chain("sorry, I cannot help with that today")
        coach = CoachPlayer(chain, disk)
        result = run(coach.run(milestone_path="milestones/M001", slice_id="S01"))

        # Run succeeds (non-fatal), but existing files are untouched.
        assert result.success is True
        assert disk.read_file("strategic/STRATEGY.md") == "existing strategy"
        assert disk.read_file("strategic/TASK_QUEUE.md") == "existing queue"
        assert disk.read_file("strategic/RISK.md") == "existing risk"
        assert disk.read_file("strategic/CONTEXT_HINTS.md") == "existing hints"

    def test_run_failure(self, tmp_path: Path) -> None:
        from tero2.players.coach import CoachPlayer

        chain = _make_chain("x")
        chain.run_prompt_collected = AsyncMock(side_effect=RuntimeError("coach boom"))  # type: ignore[attr-defined]
        disk = _make_disk(tmp_path)
        coach = CoachPlayer(chain, disk)
        result = run(coach.run())
        assert result.success is False
        assert "coach boom" in result.error


# ── Coach section parser ────────────────────────────────────────────────


class TestCoachParseSections:
    def test_all_four_sections_parsed(self):
        from tero2.players.coach import _parse_sections

        out = _FULL_COACH_RESPONSE
        sections = _parse_sections(out)
        assert sections["STRATEGY"] == "Focus on stability."
        assert sections["TASK_QUEUE"] == "1. Fix linter issues"
        assert sections["RISK"] == "Low risk."
        assert sections["CONTEXT_HINTS"] == "Use async patterns."

    def test_partial_response_returns_only_present_sections(self):
        from tero2.players.coach import _parse_sections

        out = "## STRATEGY\nOnly strategy here.\n"
        sections = _parse_sections(out)
        assert sections["STRATEGY"] == "Only strategy here."
        assert "TASK_QUEUE" not in sections
        assert "RISK" not in sections

    def test_empty_response_returns_empty_dict(self):
        from tero2.players.coach import _parse_sections

        assert _parse_sections("no headings at all") == {}
