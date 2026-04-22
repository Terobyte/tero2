"""Bug 108: TUI ``l`` (Смена плана) did nothing mid-execution.

``action_change_plan`` in the dashboard opens ``PlanPickScreen``; when the
user selects a plan file the TUI posts::

    Command("new_plan", data={"text": str(plan_file)}, source="tui")

Before bug 108, the runner handled ``new_plan`` only in ``_idle_loop`` —
so the command was a no-op while a plan was already executing. The user
would pick a new plan mid-run, see the dashboard keep running the OLD
plan, and eventually hit bug 104's "unsupported" warning in the logs.

Fix: ``_drain_commands`` now recognises ``new_plan`` as an abort-and-
restart signal. It marks the current state FAILED (with a reason that
names the source so history is auditable), re-queues the command so
``_idle_loop`` can resolve it against the new plan file, and returns
``should_continue=False`` so the phase loop exits cleanly into idle.

The actual plan-swap happens in the idle-loop branch that already exists
— we just unblock the path to it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tero2.config import Config, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.events import Command
from tero2.notifier import Notifier
from tero2.runner import Runner
from tero2.state import AgentState, Phase


def _make_runner(tmp_path: Path) -> tuple[Runner, asyncio.Queue[Command]]:
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    (project / "old-plan.md").write_text("# old plan")
    (project / "new-plan.md").write_text("# new plan")
    config = Config()
    config.telegram = TelegramConfig()
    cq: asyncio.Queue[Command] = asyncio.Queue()
    runner = Runner(project, project / "old-plan.md", config=config, command_queue=cq)
    runner.notifier = MagicMock(spec=Notifier)
    runner.notifier.notify = AsyncMock()
    return runner, cq


def _running_state() -> AgentState:
    return AgentState(phase=Phase.RUNNING)


class TestNewPlanAbortsAndRequeues:
    async def test_returns_should_continue_false(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("new_plan", data={"text": "new-plan.md"}, source="tui")
        )

        _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is False, (
            "new_plan mid-execution must halt the current phase loop"
        )

    async def test_state_marked_failed(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("new_plan", data={"text": "new-plan.md"}, source="tui")
        )

        state, _ = await runner._drain_commands(_running_state())

        assert state.phase == Phase.FAILED, (
            f"old plan must be marked FAILED so the _idle_loop can restart "
            f"with the new one. got: {state.phase!r}"
        )

    async def test_command_requeued_for_idle_loop(self, tmp_path: Path) -> None:
        """_idle_loop picks up the new_plan after _execute_plan returns. The
        command must therefore be back in the queue when drain returns."""
        runner, cq = _make_runner(tmp_path)
        new_plan = Command(
            "new_plan", data={"text": "new-plan.md"}, source="tui"
        )
        cq.put_nowait(new_plan)

        await runner._drain_commands(_running_state())

        assert not cq.empty(), "new_plan must be re-queued for idle-loop handler"
        requeued = cq.get_nowait()
        assert requeued.kind == "new_plan"
        assert requeued.data.get("text") == "new-plan.md"

    async def test_empty_text_is_logged_not_crash(
        self, tmp_path: Path
    ) -> None:
        """A malformed new_plan with no text must not abort or requeue —
        it's ignored with a warning (same shape as the existing idle-loop
        guard)."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("new_plan", data={"text": ""}, source="tui"))

        _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True, (
            "empty new_plan must not halt the run"
        )
        assert cq.empty(), "empty new_plan must not be re-queued"

    async def test_reason_surfaces_source(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("new_plan", data={"text": "new-plan.md"}, source="tui")
        )

        state, _ = await runner._drain_commands(_running_state())

        assert "tui" in (state.error_message or "").lower() or "new_plan" in (
            state.error_message or ""
        ), (
            f"failure reason must name the source so run history is auditable. "
            f"got: {state.error_message!r}"
        )


class TestBug104RegressionNewPlanNotUnsupported:
    """Once new_plan has a handler, it must NOT fall through to the bug 104
    'unsupported' warning."""

    async def test_new_plan_does_not_emit_unsupported_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        import logging

        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("new_plan", data={"text": "new-plan.md"}, source="tui")
        )

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            await runner._drain_commands(_running_state())

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any(
            "unsupported" in m and "new_plan" in m for m in warnings
        ), (
            f"new_plan must not produce 'unsupported' warning — it has a "
            f"handler. got: {warnings!r}"
        )
