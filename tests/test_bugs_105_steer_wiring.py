"""Bug 105: TUI 'Steer' binding posted `Command("steer", ...)` that the
runner never handled.

User flow that was broken:
    1. User presses 's' in the dashboard → SteerScreen opens.
    2. User types a human hint ("focus on edge cases") and submits.
    3. TUI emits ``Command("steer", data={"text": "focus on edge cases"})``
       onto the runner's command queue.
    4. Runner's ``_drain_commands`` does NOT match ``kind == "steer"`` →
       falls through → command is discarded.
    5. The hint never reaches the agent; the next attempt runs as if the
       user had said nothing.

Fix: add a ``steer`` branch to ``_drain_commands`` that persists the text
to ``.sora/human/STEER.md`` via the freshly-added ``DiskLayer.write_steer``.
The existing ``execute_phase`` attempt loop already reloads ``STEER.md`` on
every attempt (``steer_content = ctx.disk.read_steer()``), so the hint
takes effect on the very next attempt in the current task.

Stuck-option codes (``stuck_option_1`` … ``stuck_option_5``) are opaque
markers produced by the stuck-dialog bindings. Their full recovery
semantics live in the escalation subsystem and are tracked separately;
for now the handler prefixes them with ``[stuck-option]`` and writes them
through the same channel so the operator sees *which option* fired.
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
    (project / "plan.md").write_text("# plan")
    config = Config()
    config.telegram = TelegramConfig()
    cq: asyncio.Queue[Command] = asyncio.Queue()
    runner = Runner(project, project / "plan.md", config=config, command_queue=cq)
    runner.notifier = MagicMock(spec=Notifier)
    runner.notifier.notify = AsyncMock()
    return runner, cq


def _running_state() -> AgentState:
    return AgentState(phase=Phase.RUNNING)


class TestFreeFormSteerPersistsToSteerMd:
    """The text typed into the SteerScreen must end up in STEER.md so the
    execute-phase attempt loop can pick it up via ``disk.read_steer()``."""

    async def test_plain_text_written_verbatim(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "focus on edge cases"}, source="tui")
        )

        _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True, (
            "steer is a non-halting hint — the runner must keep running"
        )
        assert runner.disk.read_steer() == "focus on edge cases", (
            "free-form steer text must land in STEER.md verbatim"
        )

    async def test_overwrites_previous_steer_content(self, tmp_path: Path) -> None:
        """Each new steer replaces the old. STEER.md is a current-hint slot,
        not a log — keeping stale hints would re-inject obsolete guidance."""
        runner, cq = _make_runner(tmp_path)
        runner.disk.write_steer("old hint")

        cq.put_nowait(Command("steer", data={"text": "new hint"}, source="tui"))
        await runner._drain_commands(_running_state())

        assert runner.disk.read_steer() == "new hint"
        assert "old hint" not in runner.disk.read_steer()

    async def test_empty_text_is_noop(self, tmp_path: Path) -> None:
        """Empty text must NOT wipe existing STEER.md — that would silently
        clear a hint the user deliberately set earlier."""
        runner, cq = _make_runner(tmp_path)
        runner.disk.write_steer("keep me")

        cq.put_nowait(Command("steer", data={"text": ""}, source="tui"))
        await runner._drain_commands(_running_state())

        assert runner.disk.read_steer() == "keep me", (
            "empty-text steer must not clobber existing hint"
        )

    async def test_missing_data_dict_is_noop(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        runner.disk.write_steer("keep me")

        cq.put_nowait(Command("steer", source="tui"))
        await runner._drain_commands(_running_state())

        assert runner.disk.read_steer() == "keep me"


class TestStuckOptionCodesMarshalledThroughSteerMd:
    """Stuck-dialog options 1..5 reach the human-hint channel too, but tagged
    so downstream consumers (or the operator reading STEER.md) can tell they
    came from the stuck dialog rather than a free-form hint."""

    async def test_stuck_option_1_prefixed(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_1"}, source="tui")
        )
        await runner._drain_commands(_running_state())

        content = runner.disk.read_steer()
        assert content.startswith("[stuck-option]"), (
            f"stuck-option payloads must be tagged so they can be distinguished "
            f"from free-form hints. got: {content!r}"
        )
        assert "stuck_option_1" in content

    async def test_each_stuck_option_surfaces_code(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        for n in range(1, 6):
            cq.put_nowait(
                Command(
                    "steer", data={"text": f"stuck_option_{n}"}, source="tui"
                )
            )

        await runner._drain_commands(_running_state())

        # Last one wins (current slot semantics) — verify it's tagged.
        content = runner.disk.read_steer()
        assert content == "[stuck-option] stuck_option_5"


class TestDiskLayerWriteSteerApi:
    """The new ``write_steer`` helper must round-trip cleanly with
    ``read_steer`` and survive idempotent overwrites."""

    def test_round_trip(self, tmp_path: Path) -> None:
        disk = DiskLayer(tmp_path)
        disk.init()

        disk.write_steer("hello")
        assert disk.read_steer() == "hello"

    def test_overwrite(self, tmp_path: Path) -> None:
        disk = DiskLayer(tmp_path)
        disk.init()

        disk.write_steer("one")
        disk.write_steer("two")
        assert disk.read_steer() == "two"

    def test_clear_steer_removes_file(self, tmp_path: Path) -> None:
        disk = DiskLayer(tmp_path)
        disk.init()
        disk.write_steer("hint")

        disk.clear_steer()
        assert disk.read_steer() == ""

    def test_clear_steer_on_absent_file_is_noop(self, tmp_path: Path) -> None:
        disk = DiskLayer(tmp_path)
        disk.init()
        # file never existed — must not raise
        disk.clear_steer()
        assert disk.read_steer() == ""
