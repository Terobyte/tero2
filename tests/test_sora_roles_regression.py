"""Regression tests for SORA role bugs B5, B6, B7, B8, B11.

B5  — Verifier was hardcoded to ruff/pytest; now uses verify_commands kwarg.
B6  — Harden/scout/coach phase failures were silently ignored.
B7  — Builder accepted empty summary as success.
B8  — Builder called ctx.run_agent without role="builder".
B11 — Builder prompt instructed agent to write a file; now returns text.
      Fallback _recover_summary_from_disk picks up agent-written files.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, RoleConfig, VerifierConfig
from tero2.disk_layer import DiskLayer
from tero2.phases.context import PhaseResult
from tero2.players.architect import Task
from tero2.players.builder import BuilderPlayer, _recover_summary_from_disk
from tero2.players.verifier import Verdict, VerifierPlayer


# ─── helpers ─────────────────────────────────────────────────────────────────


def _make_disk(tmp_path: Path) -> DiskLayer:
    disk = DiskLayer(tmp_path)
    disk.init()
    return disk


def _fake_chain(response: str = "") -> MagicMock:
    chain = MagicMock()
    chain.run_prompt_collected = AsyncMock(return_value=response)
    return chain


# ─── B7: empty summary guard ──────────────────────────────────────────────────


class TestB7EmptySummaryGuard:
    @pytest.mark.asyncio
    async def test_empty_reply_returns_failure_not_success(self, tmp_path: Path) -> None:
        """Builder must return success=False when the agent reply is empty."""
        disk = _make_disk(tmp_path)
        chain = _fake_chain(response="")
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))

        result = await player.run(
            task_plan="## T01: do something\n**Must-haves:**\n- it works",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
        )

        assert not result.success
        assert "empty summary" in result.error

    @pytest.mark.asyncio
    async def test_empty_reply_does_not_write_file(self, tmp_path: Path) -> None:
        """An empty summary must not create a SUMMARY.md file."""
        disk = _make_disk(tmp_path)
        chain = _fake_chain(response="")
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))

        await player.run(
            task_plan="## T01: do something",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
        )

        assert not (disk.sora_dir / "milestones/M001/S01/T01-SUMMARY.md").exists()

    @pytest.mark.asyncio
    async def test_non_empty_reply_writes_and_succeeds(self, tmp_path: Path) -> None:
        disk = _make_disk(tmp_path)
        chain = _fake_chain(response="# T01 Summary\n## What was done\n- added feature")
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))

        result = await player.run(
            task_plan="## T01: do something",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
        )

        assert result.success
        assert result.summary.startswith("# T01 Summary")


# ─── B11: _recover_summary_from_disk ─────────────────────────────────────────


class TestB11RecoverSummaryFromDisk:
    def test_recover_finds_file_in_working_dir(self, tmp_path: Path) -> None:
        """When agent wrote T01-SUMMARY.md to project root, recovery returns content."""
        (tmp_path / "T01-SUMMARY.md").write_text("# T01 Summary\n## What was done\n- added X")

        content = _recover_summary_from_disk("T01", str(tmp_path))

        assert content.startswith("# T01 Summary")

    def test_recover_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        content = _recover_summary_from_disk("T01", str(tmp_path))
        assert content == ""

    def test_recover_empty_working_dir_returns_empty(self) -> None:
        content = _recover_summary_from_disk("T01", "")
        assert content == ""

    @pytest.mark.asyncio
    async def test_builder_uses_recovered_summary_from_disk(self, tmp_path: Path) -> None:
        """When agent returns empty text but wrote T01-SUMMARY.md, builder should succeed."""
        # Simulate agent writing file to project dir instead of returning text
        (tmp_path / "T01-SUMMARY.md").write_text("# T01 Summary\nDone via tool")

        disk = _make_disk(tmp_path)
        chain = _fake_chain(response="")  # agent returned empty text
        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))

        result = await player.run(
            task_plan="## T01: do something",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
        )

        assert result.success
        assert "Done via tool" in result.summary


# ─── B8: role kwarg in ctx.run_agent ─────────────────────────────────────────


class TestB8RoleKwarg:
    @pytest.mark.asyncio
    async def test_run_agent_called_with_builder_role(self, tmp_path: Path) -> None:
        """Builder must pass role='builder' when delegating to ctx.run_agent."""
        disk = _make_disk(tmp_path)
        chain = MagicMock()

        ctx = MagicMock()
        ctx.run_agent = AsyncMock(return_value=(True, "# T01 Summary\nDone"))

        player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))
        await player.run(
            task_plan="## T01: do something",
            task_id="T01",
            milestone_path="milestones/M001",
            slice_id="S01",
            ctx=ctx,
        )

        ctx.run_agent.assert_called_once()
        _, kwargs = ctx.run_agent.call_args
        assert kwargs.get("role") == "builder"


# ─── B5: Verifier uses configured commands ────────────────────────────────────


class TestB5VerifierConfigDriven:
    @pytest.mark.asyncio
    async def test_verify_commands_kwarg_overrides_default(self, tmp_path: Path) -> None:
        """When verify_commands is passed, those commands run instead of ruff/pytest."""
        disk = _make_disk(tmp_path)
        chain = _fake_chain()
        player = VerifierPlayer(chain, disk, working_dir=str(tmp_path))

        captured: list[list] = []

        def fake_run_command(cmd_str: str, cwd: str) -> tuple[int, str, str]:
            captured.append([cmd_str, cwd])
            return 0, "ok", ""

        with patch("tero2.players.verifier._run_command", side_effect=fake_run_command):
            result = await player.run(
                builder_output="",
                task_id="T01",
                verify_commands=["swift test", "ctest -R prompt_builder"],
            )

        assert result.verdict == Verdict.PASS
        assert any("swift test" in c[0] for c in captured)
        assert any("ctest" in c[0] for c in captured)
        # Must NOT have called ruff or pytest
        all_cmds = " ".join(c[0] for c in captured)
        assert "ruff" not in all_cmds
        assert "pytest" not in all_cmds

    @pytest.mark.asyncio
    async def test_empty_verify_commands_falls_back_to_ruff_pytest(self, tmp_path: Path) -> None:
        """When no commands given, verifier falls back to ruff + pytest."""
        disk = _make_disk(tmp_path)
        chain = _fake_chain()
        player = VerifierPlayer(chain, disk, working_dir=str(tmp_path))

        captured: list[str] = []

        def fake_run_command(cmd_str: str, cwd: str) -> tuple[int, str, str]:
            captured.append(cmd_str)
            return 0, "ok", ""

        with patch("tero2.players.verifier._run_command", side_effect=fake_run_command):
            await player.run(builder_output="", task_id="T01", verify_commands=[])

        all_cmds = " ".join(captured)
        assert "ruff" in all_cmds
        assert "pytest" in all_cmds

    @pytest.mark.asyncio
    async def test_non_zero_exit_gives_fail_verdict(self, tmp_path: Path) -> None:
        disk = _make_disk(tmp_path)
        chain = _fake_chain()
        player = VerifierPlayer(chain, disk, working_dir=str(tmp_path))

        with patch("tero2.players.verifier._run_command", return_value=(1, "", "error")):
            result = await player.run(
                builder_output="",
                task_id="T01",
                verify_commands=["swift test"],
            )

        assert result.verdict == Verdict.FAIL
        assert not result.success

    @pytest.mark.asyncio
    async def test_anomaly_keyword_in_output_with_rc0_gives_pass(self, tmp_path: Path) -> None:
        # After R1 fix: the word "ANOMALY" in output no longer triggers ANOMALY verdict.
        # rc=0 means the command succeeded — verdict is PASS regardless of output text.
        disk = _make_disk(tmp_path)
        chain = _fake_chain()
        player = VerifierPlayer(chain, disk, working_dir=str(tmp_path))

        with patch(
            "tero2.players.verifier._run_command",
            return_value=(0, "tests passed but ANOMALY detected", ""),
        ):
            result = await player.run(
                builder_output="",
                task_id="T01",
                verify_commands=["swift test"],
            )

        assert result.verdict == Verdict.PASS

    @pytest.mark.asyncio
    async def test_negative_rc_gives_anomaly_verdict(self, tmp_path: Path) -> None:
        # ANOMALY is triggered by negative rc (timeout or command not found).
        disk = _make_disk(tmp_path)
        chain = _fake_chain()
        player = VerifierPlayer(chain, disk, working_dir=str(tmp_path))

        with patch(
            "tero2.players.verifier._run_command",
            return_value=(-1, "", "command timed out: swift test"),
        ):
            result = await player.run(
                builder_output="",
                task_id="T01",
                verify_commands=["swift test"],
            )

        assert result.verdict == Verdict.ANOMALY


# ─── B5: VerifierConfig parsed from config ────────────────────────────────────


class TestB5VerifierConfig:
    def test_verifier_config_defaults(self) -> None:
        from tero2.config import _parse_config

        cfg = _parse_config({})
        assert cfg.verifier.commands == []

    def test_verifier_config_parsed_from_toml(self) -> None:
        from tero2.config import _parse_config

        cfg = _parse_config({"verifier": {"commands": ["swift test", "ctest -R X"]}})
        assert cfg.verifier.commands == ["swift test", "ctest -R X"]


# ─── B5: _extract_must_have_commands ─────────────────────────────────────────


class TestB5MustHaveExtraction:
    def test_backtick_command_extracted(self) -> None:
        from tero2.phases.execute_phase import _extract_must_have_commands

        task = Task(
            index=0,
            id="T01",
            description="setup",
            must_haves=["`swift test` passes in app/", "no compilation errors"],
        )
        cmds = _extract_must_have_commands(task)
        assert "swift test" in cmds

    def test_bullet_swift_command_extracted(self) -> None:
        from tero2.phases.execute_phase import _extract_must_have_commands

        # must_haves are already stripped of "- " by _parse_slice_plan
        task = Task(
            index=0,
            id="T01",
            description="setup",
            must_haves=["swift test passes in app/", "ctest -R prompt_builder exits 0"],
        )
        cmds = _extract_must_have_commands(task)
        assert any("swift" in c for c in cmds)

    def test_prose_must_have_not_extracted(self) -> None:
        from tero2.phases.execute_phase import _extract_must_have_commands

        task = Task(
            index=0,
            id="T01",
            description="setup",
            must_haves=["All existing tests remain green", "No new compiler warnings"],
        )
        cmds = _extract_must_have_commands(task)
        assert cmds == []


# ─── B6: Harden failure stops pipeline ───────────────────────────────────────


class TestB6PhaseResultsChecked:
    @pytest.mark.asyncio
    async def test_harden_failure_stops_runner(self, tmp_path: Path) -> None:
        """When run_harden returns success=False, _execute_sora must not proceed to architect."""
        from tero2.config import RoleConfig
        from tero2.runner import Runner
        from tero2.state import Phase, SoraPhase

        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        plan = project / "plan.md"
        plan.write_text("# Plan")

        config = Config()
        config.roles["builder"] = RoleConfig(provider="fake")
        config.roles["reviewer"] = RoleConfig(provider="fake")

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = AsyncMock(return_value=True)

        architect_called = False

        async def fake_harden(ctx):
            return PhaseResult(success=False, error="harden failed: LLM error")

        async def fake_architect(ctx, slice_id="S01"):
            nonlocal architect_called
            architect_called = True
            return PhaseResult(success=True, data={"slice_plan": None})

        with (
            patch("tero2.runner.run_harden", new=fake_harden),
            patch("tero2.runner.run_architect", new=fake_architect),
        ):
            state = runner.checkpoint.restore()
            from tero2.state import Phase
            state.phase = Phase.RUNNING
            await runner._execute_sora(state)

        assert not architect_called, "architect must not run when harden fails"

    @pytest.mark.asyncio
    async def test_scout_failure_does_not_stop_runner(self, tmp_path: Path) -> None:
        """When run_scout returns success=False, pipeline must continue (non-fatal)."""
        from tero2.config import RoleConfig
        from tero2.runner import Runner
        from tero2.state import Phase

        project = tmp_path / "project"
        project.mkdir()
        disk = DiskLayer(project)
        disk.init()
        plan = project / "plan.md"
        plan.write_text("# Plan")

        config = Config()
        config.roles["builder"] = RoleConfig(provider="fake")
        config.roles["scout"] = RoleConfig(provider="fake")

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = AsyncMock(return_value=True)

        architect_called = False

        async def fake_scout(ctx):
            return PhaseResult(success=False, error="skipped: too small")

        async def fake_architect(ctx, slice_id="S01"):
            nonlocal architect_called
            architect_called = True
            return PhaseResult(success=False, error="stop here")

        with (
            patch("tero2.runner.run_scout", new=fake_scout),
            patch("tero2.runner.run_architect", new=fake_architect),
        ):
            state = runner.checkpoint.restore()
            state.phase = Phase.RUNNING
            await runner._execute_sora(state)

        assert architect_called, "architect must still run after non-fatal scout failure"
