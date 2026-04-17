"""SORA runner tests — activation, control-flow, crash recovery, events, commands."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, RoleConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.events import Command, EventDispatcher, make_event
from tero2.notifier import NotifyLevel
from tero2.phases.context import PhaseResult
from tero2.players.architect import SlicePlan, Task
from tero2.runner import Runner
from tero2.state import AgentState, Phase, SoraPhase


def _make_sora_project(tmp_path: Path) -> tuple[Path, Path, Config, DiskLayer]:
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    plan = project / "plan.md"
    plan.write_text("# Plan\n1. do something")
    config = Config()
    config.roles["builder"] = RoleConfig(provider="fake", timeout_s=5)
    config.telegram = TelegramConfig(bot_token="tok", chat_id="chat")
    return project, plan, config, disk


def _slice_plan() -> SlicePlan:
    return SlicePlan(
        slice_id="S01",
        slice_dir="milestones/M001/S01",
        tasks=[Task(index=0, id="T01", description="do thing")],
    )


async def _noop_notify(text: str, level=None) -> bool:
    return True


class TestSoraActivation:
    async def test_builder_role_activates_sora_path(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_harden", new_callable=AsyncMock) as mock_harden,
            patch("tero2.runner.run_scout", new_callable=AsyncMock) as mock_scout,
            patch("tero2.runner.run_coach", new_callable=AsyncMock) as mock_coach,
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
        ):
            config.roles["reviewer"] = RoleConfig(provider="fake", timeout_s=5)
            config.roles["scout"] = RoleConfig(provider="fake", timeout_s=5)
            config.roles["coach"] = RoleConfig(provider="fake", timeout_s=5)
            config.roles["verifier"] = RoleConfig(provider="fake", timeout_s=5)

            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )
            mock_execute.return_value = PhaseResult(
                success=True, data={"slice_id": "S01", "completed": {}}
            )

            await runner._execute_sora(state)

            mock_harden.assert_awaited_once()
            mock_scout.assert_awaited_once()
            assert mock_coach.await_count == 2
            mock_architect.assert_awaited_once()
            mock_execute.assert_awaited_once()


class TestExecuteFailureStopsAdvancement:
    async def test_execute_failure_returns_without_coach(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock) as mock_coach,
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )
            mock_execute.return_value = PhaseResult(success=False, error="builder crashed")

            await runner._execute_sora(state)

            mock_execute.assert_awaited_once()
            mock_coach.assert_not_awaited()

            saved = disk.read_state()
            assert saved.sora_phase == SoraPhase.EXECUTE

    async def test_execute_pause_stops_advancement(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock) as mock_coach,
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )
            mock_execute.return_value = PhaseResult(
                success=False, error="PAUSE requested via OVERRIDE.md"
            )

            await runner._execute_sora(state)

            mock_execute.assert_awaited_once()
            mock_coach.assert_not_awaited()

    async def test_execute_shutdown_stops_advancement(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock) as mock_coach,
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )
            mock_execute.return_value = PhaseResult(success=False, error="shutdown requested")

            await runner._execute_sora(state)

            mock_execute.assert_awaited_once()
            mock_coach.assert_not_awaited()


class TestSliceLoopControlFlow:
    async def test_execute_failure_in_loop_breaks_not_continues(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        config.roles["coach"] = RoleConfig(provider="fake", timeout_s=5)
        config.roles["verifier"] = RoleConfig(provider="fake", timeout_s=5)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        execute_call_count = 0

        async def _architect_side_effect(ctx, slice_id):
            return PhaseResult(success=True, data={"slice_plan": _slice_plan()})

        async def _execute_side_effect(ctx, slice_plan):
            nonlocal execute_call_count
            execute_call_count += 1
            if execute_call_count == 1:
                return PhaseResult(success=True, data={"slice_id": "S01", "completed": {}})
            return PhaseResult(success=False, error="tasks failed")

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock),
            patch("tero2.runner._read_next_slice", return_value="S02"),
        ):
            mock_architect.side_effect = _architect_side_effect
            mock_execute.side_effect = _execute_side_effect

            await runner._execute_sora(state)

            assert execute_call_count == 2


class TestCrashRecoverySliceDone:
    async def test_resume_from_slice_done_skips_execute(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        config.roles["coach"] = RoleConfig(provider="fake", timeout_s=5)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            sora_phase=SoraPhase.SLICE_DONE,
            current_slice="S01",
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock) as mock_coach,
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_harden", new_callable=AsyncMock),
            patch("tero2.runner.run_scout", new_callable=AsyncMock),
            patch("tero2.runner._read_next_slice", return_value=None),
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )

            await runner._execute_sora(state)

            mock_execute.assert_not_awaited()
            mock_coach.assert_awaited_once()

    async def test_resume_from_slice_done_loads_next_slice(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        config.roles["coach"] = RoleConfig(provider="fake", timeout_s=5)
        config.roles["verifier"] = RoleConfig(provider="fake", timeout_s=5)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            sora_phase=SoraPhase.SLICE_DONE,
            current_slice="S01",
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock) as mock_coach,
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_harden", new_callable=AsyncMock),
            patch("tero2.runner.run_scout", new_callable=AsyncMock),
            patch("tero2.runner._read_next_slice", side_effect=["S02", None]),
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )
            mock_execute.return_value = PhaseResult(
                success=True, data={"slice_id": "S02", "completed": {"T01": "out.md"}}
            )

            await runner._execute_sora(state)

            mock_execute.assert_awaited_once()
            assert mock_coach.await_count == 2
            mock_architect.assert_awaited_once()

    async def test_resume_from_execute_reruns_execute(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            sora_phase=SoraPhase.EXECUTE,
            current_slice="S01",
            started_at="2026-01-01T00:00:00Z",
        )

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock),
            patch("tero2.runner.run_architect", new_callable=AsyncMock),
            patch("tero2.runner.run_harden", new_callable=AsyncMock),
            patch("tero2.runner.run_scout", new_callable=AsyncMock),
            patch("tero2.runner._read_next_slice", return_value=None),
        ):
            mock_execute.return_value = PhaseResult(
                success=True, data={"slice_id": "S01", "completed": {}}
            )

            await runner._execute_sora(state)

            mock_execute.assert_awaited_once()


class TestEventEmissions:
    async def test_phase_change_events_emitted(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        dispatcher = EventDispatcher()
        event_queue = dispatcher.subscribe()

        runner = Runner(project, plan, config=config, dispatcher=dispatcher)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock),
            patch("tero2.runner._read_next_slice", return_value=None),
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )
            mock_execute.return_value = PhaseResult(
                success=True, data={"slice_id": "S01", "completed": {}}
            )

            await runner._execute_sora(state)

            phases_seen = []
            while not event_queue.empty():
                event = event_queue.get_nowait()
                if event.kind == "phase_change":
                    phases_seen.append(event.data["sora_phase"])

            assert "architect" in phases_seen
            assert "execute" in phases_seen
            assert "slice_done" in phases_seen


class TestCommandQueue:
    async def test_stop_command_halts_at_phase_boundary(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        cmd_queue: asyncio.Queue[Command] = asyncio.Queue()
        await cmd_queue.put(Command(kind="stop", source="test"))

        runner = Runner(project, plan, config=config, command_queue=cmd_queue)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )

            await runner._execute_sora(state)

            mock_architect.assert_not_awaited()
            mock_execute.assert_not_awaited()

            saved = disk.read_state()
            assert saved.phase == Phase.FAILED

    async def test_pause_command_halts_at_phase_boundary(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        cmd_queue: asyncio.Queue[Command] = asyncio.Queue()
        await cmd_queue.put(Command(kind="pause", source="test"))

        runner = Runner(project, plan, config=config, command_queue=cmd_queue)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )

            await runner._execute_sora(state)

            mock_architect.assert_not_awaited()
            mock_execute.assert_not_awaited()

            saved = disk.read_state()
            assert saved.phase == Phase.PAUSED

    async def test_no_commands_runs_normally(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            started_at="2026-01-01T00:00:00Z",
        )

        cmd_queue: asyncio.Queue[Command] = asyncio.Queue()

        runner = Runner(project, plan, config=config, command_queue=cmd_queue)
        runner.notifier.notify = _noop_notify

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock),
            patch("tero2.runner._read_next_slice", return_value=None),
        ):
            mock_architect.return_value = PhaseResult(
                success=True, data={"slice_plan": _slice_plan()}
            )
            mock_execute.return_value = PhaseResult(
                success=True, data={"slice_id": "S01", "completed": {}}
            )

            await runner._execute_sora(state)

            mock_architect.assert_awaited_once()
            mock_execute.assert_awaited_once()

    async def test_stop_mid_loop_halts(self, tmp_path: Path) -> None:
        project, plan, config, disk = _make_sora_project(tmp_path)

        config.roles["coach"] = RoleConfig(provider="fake", timeout_s=5)
        config.roles["verifier"] = RoleConfig(provider="fake", timeout_s=5)

        state = AgentState(
            phase=Phase.RUNNING,
            plan_file=str(plan),
            sora_phase=SoraPhase.SLICE_DONE,
            current_slice="S01",
            started_at="2026-01-01T00:00:00Z",
        )

        cmd_queue: asyncio.Queue[Command] = asyncio.Queue()

        runner = Runner(project, plan, config=config, command_queue=cmd_queue)
        runner.notifier.notify = _noop_notify

        architect_call_count = 0

        async def _architect_with_stop(ctx, slice_id):
            nonlocal architect_call_count
            architect_call_count += 1
            if architect_call_count >= 1:
                await cmd_queue.put(Command(kind="stop", source="test"))
            return PhaseResult(success=True, data={"slice_plan": _slice_plan()})

        with (
            patch("tero2.runner.run_architect", new_callable=AsyncMock) as mock_architect,
            patch("tero2.runner.run_execute", new_callable=AsyncMock) as mock_execute,
            patch("tero2.runner.run_coach", new_callable=AsyncMock),
            patch("tero2.runner.run_harden", new_callable=AsyncMock),
            patch("tero2.runner.run_scout", new_callable=AsyncMock),
            patch("tero2.runner._read_next_slice", return_value="S02"),
        ):
            mock_architect.side_effect = _architect_with_stop
            mock_execute.return_value = PhaseResult(
                success=True, data={"slice_id": "S02", "completed": {}}
            )

            await runner._execute_sora(state)

            saved = disk.read_state()
            assert saved.phase == Phase.FAILED
