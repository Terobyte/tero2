"""Bug 107: stuck-dialog options 1..5 had no real semantics.

After bug 105 wired the ``Command("steer", data={"text": "stuck_option_N"})``
payload through ``DiskLayer.write_steer``, the codes still made it to
``STEER.md`` but as opaque markers — the agent reading ``effective_hints``
on the next attempt saw ``[stuck-option] stuck_option_3`` and had no way
to know this meant "diversify your approach".

Bug 107 ships two improvements:
    1. A module-level ``_STUCK_OPTION_HINTS`` dict translates each code into
       a concrete English instruction the agent can act on. Every hint also
       carries an ``option-N`` tag so operators can still trace which button
       fired from the contents of ``STEER.md``.
    2. Option 5 ("Эскалация к человеку") additionally pauses the run — marks
       the agent state as PAUSED and sends a Telegram notify, exactly like
       an ``OVERRIDE.md`` ``PAUSE`` directive would. The caller receives
       ``should_continue=False`` so the phase loop halts.

These tests pin the translation and the pause behaviour; they would fail
if the ``_STUCK_OPTION_HINTS`` mapping were removed or option 5 stopped
pausing.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tero2.config import Config, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.events import Command
from tero2.notifier import Notifier
from tero2.runner import Runner, _STUCK_OPTION_HINTS
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


class TestStuckOptionHintTranslation:
    """Every stuck option code translates to a distinct, actionable hint."""

    def test_all_five_codes_present(self) -> None:
        for n in range(1, 6):
            key = f"stuck_option_{n}"
            assert key in _STUCK_OPTION_HINTS, (
                f"runner must define a hint for {key!r}"
            )

    def test_each_hint_is_substantial(self) -> None:
        """Hints must be long enough to actually guide behaviour — not just
        a short code rename."""
        for key, text in _STUCK_OPTION_HINTS.items():
            assert len(text) >= 80, (
                f"{key} hint is too short to carry meaning: {text!r}"
            )

    def test_each_hint_is_unique(self) -> None:
        seen = set()
        for text in _STUCK_OPTION_HINTS.values():
            assert text not in seen, f"duplicate hint: {text!r}"
            seen.add(text)

    def test_each_hint_carries_option_tag(self) -> None:
        """Operators reading STEER.md must still be able to tell which
        button fired. The ``option-N`` tag is the trace."""
        for n in range(1, 6):
            text = _STUCK_OPTION_HINTS[f"stuck_option_{n}"]
            assert f"option-{n}" in text, (
                f"stuck_option_{n} hint must carry option-{n} tag: {text!r}"
            )

    async def test_option_3_contains_diversification_marker(
        self, tmp_path: Path
    ) -> None:
        """Option 3 = Diversification — the canonical escalation prompt uses
        a dead-end warning; bug 107 preserves this in the hint so the agent
        treats it the same way as automatic diversification escalation."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_3"}, source="tui")
        )
        await runner._drain_commands(_running_state())

        content = runner.disk.read_steer().lower()
        assert "different" in content and "dead end" in content, (
            f"option 3 hint must say 'previous approach hit a dead end, try "
            f"different strategy'. got: {content!r}"
        )


class TestOption5PausesRun:
    """Option 5 (human escalation) must halt the phase loop."""

    async def test_should_continue_is_false(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_5"}, source="tui")
        )

        _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is False, (
            "option 5 must halt the phase loop (mark paused, notify, stop)"
        )

    async def test_state_marked_paused(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_5"}, source="tui")
        )

        state, _ = await runner._drain_commands(_running_state())

        assert state.phase == Phase.PAUSED, (
            f"option 5 must transition state to PAUSED, got {state.phase!r}"
        )

    async def test_telegram_notify_called(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_5"}, source="tui")
        )

        await runner._drain_commands(_running_state())

        runner.notifier.notify.assert_called_once()
        call_args = runner.notifier.notify.call_args
        # Message text should mention the pause reason so the Telegram
        # operator knows what just happened.
        message = call_args.args[0] if call_args.args else call_args.kwargs.get("message", "")
        assert "Пауза" in message or "pause" in message.lower(), (
            f"notify message must surface the pause. got: {message!r}"
        )

    async def test_option_5_hint_still_written(self, tmp_path: Path) -> None:
        """Pause is the gate, but STEER.md still gets the human-pause hint
        so if the operator resumes, the agent knows why it was paused."""
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_5"}, source="tui")
        )

        await runner._drain_commands(_running_state())

        assert "[stuck-recovery option-5" in runner.disk.read_steer()


class TestOption5DoesNotBreakOtherCommands:
    """Regression: commands queued AFTER stuck_option_5 are drained but the
    loop exits early — they stay in the queue for a future drain call."""

    async def test_later_commands_remain_queued(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_5"}, source="tui")
        )
        cq.put_nowait(Command("skip_task", source="tui"))

        _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is False
        # skip_task should still be in queue — option 5 short-circuits the drain.
        remaining = []
        while not cq.empty():
            remaining.append(cq.get_nowait().kind)
        assert "skip_task" in remaining, (
            f"option 5's early return must not consume later commands. "
            f"remaining queue: {remaining!r}"
        )


class TestUnknownStuckOptionFallback:
    """If a future UI adds ``stuck_option_6`` without updating the mapping,
    the runner falls back to the opaque marker + WARNING log rather than
    crashing."""

    async def test_unknown_code_persists_raw(self, tmp_path: Path) -> None:
        runner, cq = _make_runner(tmp_path)
        cq.put_nowait(
            Command("steer", data={"text": "stuck_option_99"}, source="tui")
        )

        _, should_continue = await runner._drain_commands(_running_state())

        assert should_continue is True, "unknown code must not trigger pause"
        content = runner.disk.read_steer()
        assert "stuck_option_99" in content, (
            f"unknown code must be preserved for operator visibility. got: {content!r}"
        )
