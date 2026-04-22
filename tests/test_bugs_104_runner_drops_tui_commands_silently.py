"""Bug 104: Runner silently drops unknown commands from the TUI.

The Textual TUI (``tero2/tui/app.py``) binds several keys that post ``Command``
objects onto ``self._command_queue``:

    * ``k`` → ``Command("skip_task")``        (action_skip)
    * ``n`` → ``Command("new_project")``      (action_new_project)
    * ``s``/1–5 → ``Command("steer", ...)``   (SteerScreen + action_stuck_option_N)

At the runner side the phase-boundary drain loop (``_drain_commands``) and
the idle-mode loop (``_idle_loop``) each only recognise a fixed subset of
command kinds:

    * ``_drain_commands`` handles:   stop, pause, switch_provider
    * ``_idle_loop`` handles:        stop, switch_provider, new_plan

Every other command is dequeued but falls through the if-chain and is
dropped on the floor without any log record. The user presses ``k`` to
skip a task, sees nothing change, and has zero diagnostic signal. The
footer advertises bindings that do nothing.

This test pins the **diagnostic** contract: while the UX gap (actually
wiring those commands to runner behaviour) is a larger piece of work, the
minimum defensive behaviour is that an unhandled command MUST produce a
visible log warning so that dead bindings surface in logs and future
operators notice the gap.

Halal pair: would fail without the added ``log.warning(...)`` at the end
of each command loop.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tero2.checkpoint import CheckpointManager
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
    (project / "plan.md").write_text("# Plan\n- step")
    config = Config()
    config.telegram = TelegramConfig()
    cq: asyncio.Queue[Command] = asyncio.Queue()
    runner = Runner(project, project / "plan.md", config=config, command_queue=cq)
    runner.notifier = MagicMock(spec=Notifier)
    runner.notifier.notify = AsyncMock()
    return runner, cq


def _running_state() -> AgentState:
    """AgentState in RUNNING phase — required for pause/fail transitions."""
    return AgentState(phase=Phase.RUNNING)


class TestDrainCommandsLogsUnknown:
    """Unknown commands at the phase boundary must produce a WARNING log."""

    async def test_skip_task_command_is_logged_not_silently_dropped(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("skip_task", source="tui"))

        state = _running_state()
        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            new_state, should_continue = await runner._drain_commands(state)

        assert should_continue is True, (
            "skip_task is not a stop/pause signal — runner must keep going"
        )
        assert cq.empty(), "unhandled command must still be drained from the queue"
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("skip_task" in m and "unsupported" in m for m in warnings), (
            "unknown command must produce a warning containing the kind. "
            f"got warnings={warnings!r}"
        )
        assert any("tui" in m for m in warnings), (
            "warning must name the source (tui) so the operator can trace "
            f"which binding fired. got warnings={warnings!r}"
        )

    async def test_steer_stuck_option_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_2"}, source="tui")
        )

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("steer" in m for m in warnings), (
            f"steer command must be logged, got: {warnings!r}"
        )

    async def test_new_project_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("new_project", data={"path": "/tmp/x"}, source="tui")
        )

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            await runner._drain_commands(_running_state())

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("new_project" in m for m in warnings), (
            f"new_project must surface in logs, got: {warnings!r}"
        )

    async def test_multiple_unknown_drained_each_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Three unknown commands in a row must produce three warnings."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("skip_task", source="tui"))
        cq.put_nowait(Command("steer", data={"text": "x"}, source="tui"))
        cq.put_nowait(Command("new_project", data={"path": "/x"}, source="tui"))

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True
        assert cq.empty()
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        for kind in ("skip_task", "steer", "new_project"):
            assert any(kind in m for m in warnings), (
                f"expected warning mentioning {kind!r}, got: {warnings!r}"
            )

    async def test_known_commands_still_work_without_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Regression: stop/pause must still short-circuit; no stray warnings."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("pause", source="tui"))

        state = _running_state()
        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            _, should_continue = await runner._drain_commands(state)

        assert should_continue is False, (
            "pause must halt the phase loop (regression check)"
        )
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any("unsupported" in m for m in warnings), (
            f"pause must not produce 'unsupported' warning. got: {warnings!r}"
        )

    async def test_switch_provider_still_works_without_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        from tero2.config import RoleConfig

        runner, cq = _make_runner(tmp_path)
        runner.config.roles["builder"] = RoleConfig(provider="zai")
        cq.put_nowait(
            Command(
                "switch_provider",
                data={"role": "builder", "provider": "claude"},
                source="tui",
            )
        )

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True
        assert runner.config.roles["builder"].provider == "claude"
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any("unsupported" in m for m in warnings), (
            f"switch_provider must not produce 'unsupported' warning. got: {warnings!r}"
        )


class TestSourceAnnotationRegression:
    """The warning must include the Command.source so operators can trace
    which subsystem fired the dead binding. A TUI-sourced command that's
    dropped must produce a warning that names the TUI."""

    async def test_warning_includes_source(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("skip_task", source="tui"))

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            await runner._drain_commands(_running_state())

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        # The specific source string must appear — this is the diagnostic
        # payload that lets the operator attribute the dropped command.
        assert any("tui" in m for m in warnings), (
            f"warning must name the source 'tui'. got: {warnings!r}"
        )
