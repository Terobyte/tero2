"""Bug 109: pause/resume UX gaps — no visible indicator, no in-process resume.

Two related gaps bundled as one bug because they compound:

1. **Pause is invisible to the TUI**. ``_drain_commands`` used to mark the
   agent state as PAUSED and exit the phase loop silently; the only trace
   was a log line. The dashboard didn't receive any event, so the user
   pressed 'p' and saw nothing change.

2. **There was no way to resume**. The TUI's ``action_pause`` always sent
   ``Command("pause")``. Once paused, pressing 'p' again sent another
   pause — which the runner silently dropped (the runner was in
   ``_idle_loop``, which only handled stop/switch_provider/new_plan).
   Users had to kill the process and restart to un-pause.

Fix:
    * ``_drain_commands`` now emits a visible ``error``-kind dispatcher
      event on pause and stop so the log shows "Пауза через tui — runner
      ушёл в idle" in red. Error-kind is used because it's already wired
      to the red-bold style in LogView; semantically it's not an error,
      but it is a high-attention state transition.
    * ``_idle_loop`` treats ``pause`` and ``resume`` commands as a toggle
      when state is PAUSED — marks the state RUNNING, notifies Telegram,
      emits a phase_change, and re-enters ``_execute_plan``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tero2.config import Config, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.events import Command, EventDispatcher
from tero2.notifier import Notifier
from tero2.runner import Runner
from tero2.state import AgentState, Phase


def _make_runner(tmp_path: Path) -> tuple[Runner, asyncio.Queue[Command], EventDispatcher]:
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    (project / "plan.md").write_text("# plan")
    config = Config()
    config.telegram = TelegramConfig()
    cq: asyncio.Queue[Command] = asyncio.Queue()
    dispatcher = EventDispatcher()
    runner = Runner(
        project,
        project / "plan.md",
        config=config,
        dispatcher=dispatcher,
        command_queue=cq,
    )
    runner.notifier = MagicMock(spec=Notifier)
    runner.notifier.notify = AsyncMock()
    return runner, cq, dispatcher


def _running_state() -> AgentState:
    return AgentState(phase=Phase.RUNNING)


class TestPauseEmitsVisibleEvent:
    """Pressing 'p' must produce a dispatcher event so the dashboard log
    shows the pause in red. Without this the user had no visual feedback."""

    async def test_pause_emits_error_event_with_pause_text(
        self, tmp_path: Path
    ) -> None:
        runner, cq, dispatcher = _make_runner(tmp_path)
        received = dispatcher.subscribe()

        cq.put_nowait(Command("pause", source="tui"))
        await runner._drain_commands(_running_state())

        # Collect any events that came through
        events = []
        while not received.empty():
            events.append(received.get_nowait())
        assert events, "pause must produce at least one dispatcher event"
        pause_event = next(
            (e for e in events if "ауза" in (e.data.get("message") or "")),
            None,
        )
        assert pause_event is not None, (
            f"pause event must carry a human-readable 'Пауза' message. "
            f"got events={[(e.kind, e.data) for e in events]!r}"
        )
        assert pause_event.priority, (
            "pause event must be priority so it survives queue backpressure"
        )

    async def test_stop_also_emits_event(self, tmp_path: Path) -> None:
        runner, cq, dispatcher = _make_runner(tmp_path)
        received = dispatcher.subscribe()

        cq.put_nowait(Command("stop", source="tui"))
        await runner._drain_commands(_running_state())

        events = []
        while not received.empty():
            events.append(received.get_nowait())
        assert events, "stop must produce at least one dispatcher event"
        stop_event = next(
            (
                e
                for e in events
                if "становлен" in (e.data.get("message") or "")
            ),
            None,
        )
        assert stop_event is not None, (
            f"stop event must carry a human-readable 'Остановлено' message. "
            f"got events={[(e.kind, e.data) for e in events]!r}"
        )


class TestIdleLoopTreatsPauseAsResumeWhenPaused:
    """Pressing 'p' again while paused should resume. The TUI doesn't have
    a separate 'resume' binding — 'p' is a toggle."""

    async def test_restore_reads_paused_state(self, tmp_path: Path) -> None:
        """Sanity check: after mark_paused, checkpoint.restore reads PAUSED."""
        runner, cq, dispatcher = _make_runner(tmp_path)
        runner.checkpoint.mark_started(str(runner.plan_file))
        paused = runner.checkpoint.mark_paused(
            runner.checkpoint.restore(), "test pause"
        )
        assert paused.phase == Phase.PAUSED

        restored = runner.checkpoint.restore()
        assert restored.phase == Phase.PAUSED

    async def test_idle_loop_pause_when_already_paused_triggers_resume(
        self, tmp_path: Path
    ) -> None:
        """Puts state in PAUSED, then posts a pause command to the queue,
        then runs _idle_loop briefly and verifies it called _execute_plan."""
        runner, cq, dispatcher = _make_runner(tmp_path)
        runner.checkpoint.mark_started(str(runner.plan_file))
        runner.checkpoint.mark_paused(
            runner.checkpoint.restore(), "pre-test pause"
        )

        # Swap _execute_plan so we can detect it being called.
        call_flag = asyncio.Event()

        async def _fake_execute(state, shutdown_event=None):
            call_flag.set()

        runner._execute_plan = _fake_execute  # type: ignore[method-assign]
        # Short idle so the loop exits if no resume happens.
        runner.config.idle_timeout_s = 1

        # Post resume-style toggle
        cq.put_nowait(Command("pause", source="tui"))

        await asyncio.wait_for(runner._idle_loop(), timeout=5.0)

        assert call_flag.is_set(), (
            "idle loop must call _execute_plan when pause-as-resume fires "
            "while state is PAUSED"
        )

    async def test_idle_loop_pause_when_not_paused_is_noop(
        self, tmp_path: Path
    ) -> None:
        """If state is not PAUSED (e.g. IDLE after normal completion), a pause
        command in the idle loop is a no-op — doesn't trigger _execute_plan."""
        runner, cq, dispatcher = _make_runner(tmp_path)
        # State stays IDLE — never started

        call_flag = asyncio.Event()

        async def _fake_execute(state, shutdown_event=None):
            call_flag.set()

        runner._execute_plan = _fake_execute  # type: ignore[method-assign]
        runner.config.idle_timeout_s = 1

        cq.put_nowait(Command("pause", source="tui"))

        await asyncio.wait_for(runner._idle_loop(), timeout=5.0)

        assert not call_flag.is_set(), (
            "pause in idle when NOT paused must not trigger _execute_plan"
        )


class TestResumeCommandAliasesPause:
    """A future TUI could bind a separate 'Resume' key that emits
    ``Command("resume")``. Both kinds must take the same path."""

    async def test_resume_command_resumes(self, tmp_path: Path) -> None:
        runner, cq, dispatcher = _make_runner(tmp_path)
        runner.checkpoint.mark_started(str(runner.plan_file))
        runner.checkpoint.mark_paused(
            runner.checkpoint.restore(), "pre-test pause"
        )

        call_flag = asyncio.Event()

        async def _fake_execute(state, shutdown_event=None):
            call_flag.set()

        runner._execute_plan = _fake_execute  # type: ignore[method-assign]
        runner.config.idle_timeout_s = 1

        cq.put_nowait(Command("resume", source="tui"))

        await asyncio.wait_for(runner._idle_loop(), timeout=5.0)

        assert call_flag.is_set()
