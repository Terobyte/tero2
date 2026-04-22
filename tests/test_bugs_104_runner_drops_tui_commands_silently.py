"""Bug 104: Runner silently drops unknown commands from the TUI.

The Textual TUI (``tero2/tui/app.py``) binds several keys that post ``Command``
objects onto ``self._command_queue``. At the runner side the phase-boundary
drain loop (``_drain_commands``) and the idle-mode loop (``_idle_loop``) each
only recognise a fixed subset of command kinds. Any command kind not in those
subsets used to fall through the if-chain and be dropped on the floor with no
log record.

After bug 104 was fixed, steer (bug 105) and skip_task (bug 106) got real
handlers, but the original diagnostic contract still applies to every OTHER
unhandled kind — most notably ``new_project`` (which remains feature work)
and any fresh binding a future TUI author might add.

This test pins the minimum defensive behaviour: **truly unhandled** commands
must produce a visible log warning so dead bindings surface in logs and
future operators notice the gap. It also guarantees the new handlers do NOT
emit the 'unsupported' warning — because they ARE supported now.
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


class TestUnhandledCommandsStillLogged:
    """Commands without a handler must still produce a WARNING log."""

    async def test_new_project_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("new_project", data={"path": "/tmp/x"}, source="tui")
        )

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("new_project" in m and "unsupported" in m for m in warnings), (
            f"new_project must surface in logs, got: {warnings!r}"
        )

    async def test_fresh_unknown_kind_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Any future TUI binding that adds a new Command.kind without a
        matching runner handler must also produce a warning — the guard is
        kind-agnostic."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("unknown_future_kind", source="tui"))

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            await runner._drain_commands(_running_state())

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "unknown_future_kind" in m and "unsupported" in m for m in warnings
        ), f"got warnings={warnings!r}"

    async def test_warning_includes_source(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The warning must name the Command.source so operators can trace
        which subsystem fired the dead binding."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("new_project", source="tui"))

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            await runner._drain_commands(_running_state())

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("tui" in m for m in warnings), (
            f"warning must name the source 'tui'. got: {warnings!r}"
        )

    async def test_multiple_unknowns_each_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("new_project", data={"path": "/x"}, source="tui"))
        cq.put_nowait(Command("hypothetical_future_cmd", source="tui"))

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True
        assert cq.empty()
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        for kind in ("new_project", "hypothetical_future_cmd"):
            assert any(kind in m for m in warnings), (
                f"expected warning mentioning {kind!r}, got: {warnings!r}"
            )


class TestKnownCommandsDoNotTriggerUnsupportedWarning:
    """Handled commands must NOT produce an 'unsupported' warning. Regression
    guard — if someone accidentally removes a handler, the commands it used
    to accept will start emitting unsupported warnings and fail these tests.
    """

    async def test_pause_is_handled(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("pause", source="tui"))

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is False
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any("unsupported" in m for m in warnings), (
            f"pause must not produce 'unsupported' warning. got: {warnings!r}"
        )

    async def test_switch_provider_is_handled(
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

    async def test_steer_and_skip_task_do_not_emit_unsupported(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """steer (bug 105) and skip_task (bug 106) have real handlers now;
        they must NOT fall through to the 'unsupported' branch."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(Command("steer", data={"text": "focus on X"}, source="tui"))
        cq.put_nowait(Command("skip_task", source="tui"))

        with caplog.at_level(logging.WARNING, logger="tero2.runner"):
            await runner._drain_commands(_running_state())

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any(
            "unsupported" in m and ("steer" in m or "skip_task" in m) for m in warnings
        ), (
            f"steer/skip_task must not produce 'unsupported' warning — they have "
            f"handlers. got: {warnings!r}"
        )
