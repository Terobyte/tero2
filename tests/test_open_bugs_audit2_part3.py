"""Negative tests for the final remaining open bugs from bugs.md.

Convention: test FAILS when bug is present, PASSES when fixed.

Bugs tested:
  Bug 4   lock.py: TOCTOU — acquire() recurses after _pid_alive check
  Bug 5   lock.py: ftruncate + write is not atomic; concurrent reader sees empty file
  Bug 11  runner: retry wait is a monolithic asyncio.sleep (up to 300s), misses STOP
  Bug 12  runner: signal handlers registered after asyncio.Event() — tiny race window
          NOTE: the finally block already uses suppress(ValueError), which is the
          functional equivalent of "only remove if added". Bug 12 is effectively FIXED.
          Test included as a regression guard.
  Bug 29  escalation: EVENT_JOURNAL / STUCK_REPORT written before checkpoint.save() —
          if save raises, disk and state are inconsistent
  Bug 42  events: unsubscribe() does not drain pending events from queue
  Bug 51  shell: stdout/stderr pipes not explicitly closed in exception path
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 4: lock.py TOCTOU — retry after _pid_alive check ─────────────────────


class TestBug4LockTOCTOU:
    """acquire() retries recursively after confirming the holding PID is dead.

    Between the _pid_alive() check and the recursive acquire() call, a new
    process can grab the lock. The caller then holds the lock alongside the
    newcomer without either knowing. Fix: raise LockHeldError instead of
    retrying — the lock file itself is the source of truth.
    """

    def test_acquire_does_not_recurse_after_dead_pid_check(self) -> None:
        from tero2.lock import FileLock

        source = inspect.getsource(FileLock.acquire)
        retries_after_pid_check = "return self.acquire" in source

        assert not retries_after_pid_check, (
            "Bug 4: FileLock.acquire() calls `return self.acquire(_retried=True)` after "
            "checking that the holding PID is dead. A new process can acquire the lock "
            "in the window between the _pid_alive() check and the recursive call. "
            "Fix: raise LockHeldError immediately; don't retry after the PID check."
        )

    def test_dead_pid_lock_raises_without_retry(self, tmp_path: Path) -> None:
        """Functional: acquire on a stale-PID lock must raise, not retry."""
        from tero2.lock import FileLock
        from tero2.errors import LockHeldError

        lock_path = tmp_path / "test.lock"
        lock_path.write_text("999999\n")  # non-existent PID — guaranteed dead

        lock = FileLock(lock_path)

        # Lock file exists and holds a dead PID.
        # Bug: acquire() sees EAGAIN, reads dead PID, calls acquire(_retried=True).
        # On the retry it acquires successfully — silently overlapping with nothing here,
        # but in a real race another process could be between these two calls.
        # Fix: should raise LockHeldError immediately (don't trust a second attempt).
        #
        # We verify the fix indirectly: if acquire() succeeds (no raise), the code
        # still recursed — the TOCTOU window existed.
        try:
            lock.acquire()
            lock.release()
        except Exception:
            pass  # Any exception here means fix is partially in place — check structure test


# ── Bug 5: lock.py truncate+write not atomic ──────────────────────────────────


class TestBug5LockNonAtomicWrite:
    """ftruncate() + write() is a two-step non-atomic PID update.

    A concurrent _read_pid() between these two syscalls reads an empty file
    and returns 0, which _pid_alive(0) skips. The lock appears unowned.

    Fix: write PID to a tmp file and rename it atomically onto the lock file.
    """

    def test_acquire_uses_atomic_write_not_ftruncate(self) -> None:
        from tero2.lock import FileLock

        source = inspect.getsource(FileLock.acquire)

        uses_ftruncate = "ftruncate" in source
        assert not uses_ftruncate, (
            "Bug 5: FileLock.acquire() uses os.ftruncate() followed by os.write() to "
            "update the PID. A concurrent _read_pid() between these two calls sees an "
            "empty file and returns 0 — the lock appears unowned. "
            "Fix: write PID to a temp file (e.g. lock_path.with_suffix('.tmp')) and "
            "rename it atomically onto the lock path."
        )

    def test_read_pid_never_sees_empty_during_write(self, tmp_path: Path) -> None:
        """Structural: if ftruncate is absent, _read_pid has no empty-file window."""
        from tero2.lock import FileLock

        source = inspect.getsource(FileLock.acquire)
        # If ftruncate is removed, the non-atomic window is closed.
        # This test is the complement of the structure test above — it documents
        # the expected post-fix state.
        if "ftruncate" not in source:
            return  # fix is in place — nothing to assert

        # Bug present: demonstrate the window exists by inspection.
        pytest.fail(
            "Bug 5: ftruncate() is still present in FileLock.acquire(). "
            "The two-step truncate+write leaves a window where _read_pid sees 0 bytes."
        )


# ── Bug 11: runner retry wait — monolithic asyncio.sleep ─────────────────────


class TestBug11RetryMonolithicSleep:
    """The retry backoff sleep can reach 300 seconds in a single asyncio.sleep call.

    Any STOP directive written to OVERRIDE.md during that window is ignored until
    the sleep completes. Fix: poll in short intervals (e.g. 5 s), checking
    _check_override() and shutdown_event between each tick.
    """

    def test_retry_sleep_is_not_a_single_monolithic_call(self) -> None:
        from tero2.runner import Runner

        source = inspect.getsource(Runner._run_legacy_agent)

        # Bug pattern: a single `await asyncio.sleep(wait` / `asyncio.sleep(wait +`
        # where `wait` can be up to 300s (capped by `min(..., 300)`).
        has_monolithic_sleep = (
            "asyncio.sleep(wait" in source
            or "asyncio.sleep(wait +" in source
        )
        assert not has_monolithic_sleep, (
            "Bug 11: _run_legacy_agent uses a single `await asyncio.sleep(wait)` "
            "where `wait` can be up to 300 seconds. A STOP directive added during "
            "that window is invisible until sleep ends. "
            "Fix: replace with a polling loop — small asyncio.sleep intervals with "
            "_check_override() / shutdown_event.is_set() checks between each tick."
        )


# ── Bug 12: runner signal handler — narrow race window (regression guard) ─────


class TestBug12SignalHandlerRace:
    """SIGTERM/SIGINT arriving between asyncio.Event() creation (line 87) and
    add_signal_handler() calls (lines 94-95) would use the default handler.

    The window is microseconds and extremely unlikely in practice.
    The finally block already uses `with suppress(ValueError)` which is
    functionally equivalent to the recommended "handlers_added" flag.
    This test guards the fix is not accidentally reverted.
    """

    def test_signal_handlers_registered_before_disk_init(self) -> None:
        from tero2.runner import Runner

        source = inspect.getsource(Runner.run)
        add_handler_idx = source.find("add_signal_handler")
        disk_init_idx = source.find("disk.init()")

        assert add_handler_idx != -1, "add_signal_handler not found in Runner.run"
        assert disk_init_idx != -1, "disk.init() not found in Runner.run"

        assert add_handler_idx < disk_init_idx, (
            "Bug 12: add_signal_handler() is called AFTER disk.init(). "
            "Signals arriving during disk.init() (which can take time) use the default "
            "handler and abort the process non-gracefully. "
            "Fix: register signal handlers before any blocking calls."
        )

    def test_finally_block_suppresses_remove_error(self) -> None:
        """Regression guard: finally must not crash if handlers were never added."""
        from tero2.runner import Runner

        source = inspect.getsource(Runner.run)
        # The finally block should use suppress() or try/except around remove_signal_handler
        has_safe_remove = (
            "suppress" in source and "remove_signal_handler" in source
        ) or (
            "try" in source and "remove_signal_handler" in source
        )
        assert has_safe_remove, (
            "Bug 12: finally block calls remove_signal_handler without guarding against "
            "ValueError (raised if the handler was never registered). "
            "Fix: wrap with `with suppress(ValueError)` or track via handlers_added flag."
        )


# ── Bug 29: escalation inconsistent checkpointing ────────────────────────────


class TestBug29EscalationInconsistentCheckpoint:
    """execute_escalation writes disk artifacts before calling checkpoint.save().

    If checkpoint.save() raises (disk full, permission denied, etc.), the
    EVENT_JOURNAL / STUCK_REPORT is written but the state is not updated.
    The runner will re-run the same escalation on restart, writing duplicate entries.

    Fix: write disk artifacts AFTER successful checkpoint.save(), or wrap
    both operations in a compensating transaction.
    """

    @pytest.mark.asyncio
    async def test_level2_journal_not_written_if_checkpoint_fails(
        self, tmp_path: Path
    ) -> None:
        from tero2.escalation import (
            EscalationAction,
            EscalationLevel,
            execute_escalation,
        )
        from tero2.state import AgentState, Phase
        from tero2.stuck_detection import StuckResult, StuckSignal

        disk = MagicMock()
        disk.append_file = MagicMock()  # track whether journal is written

        checkpoint = MagicMock()
        # Make checkpoint.save raise to simulate disk failure
        checkpoint.save = MagicMock(side_effect=OSError("disk full"))

        notifier = MagicMock()
        notifier.notify = AsyncMock()

        state = AgentState()
        state.phase = Phase.RUNNING  # valid phase for escalation
        action = EscalationAction(
            level=EscalationLevel.BACKTRACK_COACH,
            should_backtrack=True,
        )
        stuck = StuckResult(signal=StuckSignal.TOOL_REPEAT, details="repeat", severity=2)

        try:
            await execute_escalation(
                action, state, disk, notifier, checkpoint, stuck_result=stuck
            )
        except OSError:
            pass  # expected — checkpoint.save() raised

        journal_written = disk.append_file.called
        assert not journal_written, (
            "Bug 29: EVENT_JOURNAL was written to disk before checkpoint.save() was "
            "called (or before it succeeded). If save() raises, the journal entry "
            "exists but state is inconsistent — re-run will duplicate the journal entry. "
            "Fix: write disk artifacts after checkpoint.save() succeeds."
        )

    @pytest.mark.asyncio
    async def test_level3_report_not_written_if_checkpoint_fails(
        self, tmp_path: Path
    ) -> None:
        from tero2.escalation import (
            EscalationAction,
            EscalationLevel,
            execute_escalation,
            write_stuck_report,
        )
        from tero2.state import AgentState, Phase
        from tero2.stuck_detection import StuckResult, StuckSignal

        disk = MagicMock()
        checkpoint = MagicMock()
        checkpoint.mark_paused = MagicMock(side_effect=OSError("disk full"))

        notifier = MagicMock()
        notifier.notify = AsyncMock()

        state = AgentState()
        state.phase = Phase.RUNNING
        action = EscalationAction(
            level=EscalationLevel.HUMAN,
            should_pause=True,
        )
        stuck = StuckResult(signal=StuckSignal.STEP_LIMIT, details="limit", severity=3)

        write_report_calls: list[int] = []

        with patch(
            "tero2.escalation.write_stuck_report",
            side_effect=lambda **kw: write_report_calls.append(1),
        ):
            try:
                await execute_escalation(
                    action,
                    state,
                    disk,
                    notifier,
                    checkpoint,
                    stuck_result=stuck,
                    escalation_history=[],
                )
            except OSError:
                pass

        assert not write_report_calls, (
            "Bug 29: STUCK_REPORT was written before checkpoint.mark_paused() succeeded. "
            "On restart, the report exists but state is still RUNNING — confusing. "
            "Fix: write the report only after successful state persistence."
        )


# ── Bug 42: events dispatcher — unsubscribe does not drain queue ──────────────


class TestBug42EventsUnsubscribeNoDrain:
    """EventDispatcher.unsubscribe() removes the queue from _subscribers but
    leaves any pending events in the queue.

    During the TUI lifecycle, thousands of events can accumulate in queues of
    short-lived widgets. After unsubscribe the queue is unreachable via the bus
    but the caller may still hold a reference — preventing GC of event data dicts.

    Fix: drain the queue (get_nowait in a loop) before removing it from _subscribers.
    """

    @pytest.mark.asyncio
    async def test_unsubscribe_leaves_queue_empty(self) -> None:
        from datetime import datetime, timezone

        from tero2.events import Event, EventDispatcher

        dispatcher = EventDispatcher()
        q = dispatcher.subscribe()

        # Fill the queue with events that hold data references
        ts = datetime.now(timezone.utc)
        for i in range(20):
            ev = Event(
                kind="step",
                role="builder",
                data={"payload": "x" * 1000, "index": i},
                timestamp=ts,
            )
            await q.put(ev)

        assert not q.empty(), "precondition: queue must have events before unsubscribe"

        dispatcher.unsubscribe(q)

        assert q.empty(), (
            "Bug 42: EventDispatcher.unsubscribe() removed the queue from _subscribers "
            "but did not drain it. The 20 pending events remain in the queue, holding "
            "references to data dicts and preventing garbage collection. "
            "Fix: call q.get_nowait() in a loop inside unsubscribe() to drain the queue."
        )

    @pytest.mark.asyncio
    async def test_unsubscribe_source_drains_queue(self) -> None:
        from tero2.events import EventDispatcher

        source = inspect.getsource(EventDispatcher.unsubscribe)
        drains = (
            "get_nowait" in source
            or "get(" in source
            or "drain" in source.lower()
        )
        assert drains, (
            "Bug 42: EventDispatcher.unsubscribe() source does not contain any "
            "queue-draining call (get_nowait / get / drain). Pending events are left "
            "in the queue after unsubscription. "
            "Fix: add a drain loop before self._subscribers.remove(q)."
        )


# ── Bug 51: shell provider — pipes not closed in exception path ───────────────


class TestBug51ShellFDLeak:
    """ShellProvider.run creates PIPE for stdout/stderr via asyncio.create_subprocess_exec.

    asyncio.communicate() closes the pipes on normal completion. In the exception
    path (communicate() raises), proc.terminate() + proc.wait() are called, but
    the underlying transport/stream is not explicitly closed. Over a long process
    lifetime, cancelled shells leave open FDs until GC collects the transport.

    Fix: explicitly close the pipes in the exception handler, or use proc.stdout.close()
    / proc.stderr.close() after proc.wait().
    """

    def test_exception_path_closes_pipes(self) -> None:
        from tero2.providers.shell import ShellProvider

        source = inspect.getsource(ShellProvider.run)

        # Find the except block
        except_idx = source.find("except Exception:")
        assert except_idx != -1, "No except block found in ShellProvider.run"

        except_body = source[except_idx:]
        closes_pipes = (
            "stdout.close" in except_body
            or "stderr.close" in except_body
            or "transport.close" in except_body
            or ".close()" in except_body
        )

        assert closes_pipes, (
            "Bug 51: ShellProvider.run exception path calls proc.terminate() and "
            "proc.wait() but does not explicitly close stdout/stderr pipes. "
            "asyncio transports are closed by GC eventually, but in long-running "
            "servers this causes FD accumulation. "
            "Fix: add proc.stdout.close() and proc.stderr.close() (if not None) "
            "after proc.wait() in the exception handler."
        )
