"""Negative tests for open bugs from bugs.md Audit 2 (2026-04-20).

Convention: test FAILS when the bug is present (red), PASSES when fixed (green).

Pre-verified as already fixed — not tested here:
  Bug 21  shell subprocess cleanup (proc.terminate in except — already present)
  Bug 28  project_init empty name (ValueError guard already in place)
  Bug 40  stuck_detection off-by-one (fixed b8c0aa3)
  Bug 44  config_writer lock file leak (lock_path.unlink in finally — already present)
  Bug 46  escalation Level-2 skip (fixed b8c0aa3)
  Bug 53  runner UTF-8 truncation (fixed b8c0aa3)
  Bug 54  reflexion UTF-8 truncation (fixed b8c0aa3)
  Bug 57  runner dead setattr (fixed b8c0aa3)
  Bug 59  persona crash on missing prompts dir (None guard already present)
  Bug 60  providers_pick queries non-existent #pp-title (fixed b8c0aa3)

False positive — not a bug:
  Bug 58  circuit_breaker HALF_OPEN stuck with recovery_timeout_s=0.
          OPEN→HALF_OPEN fires because time.monotonic() - 0 >= 0 is always True.
          After record_failure() resets state to OPEN, the next check() allows a
          new probe immediately. No permanent stuck state exists.

Bugs tested here:
  Bug 41  shell provider: arbitrary command injection via bash -c <prompt>
  Bug 45  disk_layer: write_metrics without prior read_metrics silently over-counts
  Bug 49  stream_bus: stale event loop handle after asyncio.run() restart
  Bug 55  state.touch() mutates in-memory timestamp but never persists to disk
  Bug 56  checkpoint.mark_started() always creates fresh AgentState(), discarding
          retry_count and other context accumulated in a prior FAILED/PAUSED run
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tero2.stream_bus import StreamBus, make_stream_event


# ── Bug 41: shell provider command injection ──────────────────────────────────


class TestBug41ShellInjection:
    """bash -c <prompt> passes untrusted prompt directly to bash.

    Shell metacharacters (;, &&, |, $(), etc.) in the prompt text execute freely.
    Fix: don't construct a bash -c invocation from user-supplied text.
    """

    async def test_semicolon_does_not_execute_injected_command(self, tmp_path: Path) -> None:
        from tero2.providers.shell import ShellProvider

        marker = tmp_path / "injected.txt"
        provider = ShellProvider()
        injected_prompt = f"true; touch {marker}"

        try:
            async for _ in provider.run(prompt=injected_prompt):
                pass
        except Exception:
            pass

        assert not marker.exists(), (
            f"Bug 41: shell injection via semicolon succeeded — touch {marker} was executed. "
            "The prompt must not be passed raw to 'bash -c'."
        )

    async def test_subshell_does_not_execute_injected_command(self, tmp_path: Path) -> None:
        from tero2.providers.shell import ShellProvider

        marker = tmp_path / "subshell.txt"
        provider = ShellProvider()
        injected_prompt = f"echo safe $(touch {marker})"

        try:
            async for _ in provider.run(prompt=injected_prompt):
                pass
        except Exception:
            pass

        assert not marker.exists(), (
            f"Bug 41: subshell injection succeeded — touch {marker} was executed via $(). "
            "The prompt must not be passed raw to 'bash -c'."
        )


# ── Bug 45: disk_layer metrics contract violation without read_metrics ────────


class TestBug45MetricsWithoutRead:
    """write_metrics() computes delta against thread-local last_read.

    Without a prior read_metrics() call, last_read defaults to {}, so every
    value is treated as an absolute increment from 0 and ADDED to whatever
    already exists in the metrics file — silently over-counting.

    Fix: raise ValueError or call read_metrics() automatically when last_read
    is not set.
    """

    def test_write_without_read_raises_or_reads_automatically(self, tmp_path: Path) -> None:
        from tero2.disk_layer import DiskLayer

        disk_a = DiskLayer(tmp_path)
        disk_a.init()

        # Establish an existing value via a proper read→write cycle.
        disk_a.read_metrics()
        disk_a.write_metrics({"steps": 5})

        # Fresh DiskLayer simulates a new process / thread with no baseline.
        fresh = DiskLayer(tmp_path)

        # Bug: no exception is raised, and the write silently over-counts.
        with pytest.raises(Exception):
            fresh.write_metrics({"steps": 3})

    def test_write_without_read_does_not_silently_over_count(self, tmp_path: Path) -> None:
        from tero2.disk_layer import DiskLayer

        disk = DiskLayer(tmp_path)
        disk.init()
        disk.read_metrics()
        disk.write_metrics({"steps": 5})

        fresh = DiskLayer(tmp_path)
        try:
            fresh.write_metrics({"steps": 3})
        except Exception:
            # Acceptable: raising is one valid fix.
            return

        # If no exception, the value must not have been silently incremented.
        result = disk.read_metrics()
        assert result.get("steps") != 8, (
            f"Bug 45: write_metrics without read_metrics silently added 3 to the "
            f"existing 5 → steps=8. Expected the call to raise or auto-read baseline."
        )


# ── Bug 49: stream_bus stale event loop after asyncio.run() restart ───────────


class TestBug49StaleEventLoop:
    """StreamBus caches the first event loop it sees and never updates it.

    When the bus is reused across two asyncio.run() calls (e.g. server restart
    or sequential pytest sessions sharing a singleton bus), bus._loop points to
    the closed first loop. The second publish() forwards events to the dead loop
    via call_soon_threadsafe, either raising RuntimeError or silently dropping.

    Fix: in publish(), when current_loop is not self._loop and self._loop is
    closed, update self._loop to current_loop instead of forwarding to the old one.
    """

    def test_publish_delivers_event_after_loop_restart(self) -> None:
        bus = StreamBus()

        async def first_session() -> None:
            q = bus.subscribe()
            bus.publish(make_stream_event("builder", "text", content="first"))
            q.get_nowait()
            bus.unsubscribe(q)

        asyncio.run(first_session())
        # bus._loop now points to the closed first event loop.

        received: list[object] = []
        publish_errors: list[Exception] = []

        async def second_session() -> None:
            q = bus.subscribe()
            try:
                bus.publish(make_stream_event("builder", "text", content="second"))
            except Exception as exc:
                publish_errors.append(exc)
                return
            # Give call_soon_threadsafe a tick to deliver.
            await asyncio.sleep(0.05)
            if not q.empty():
                received.append(q.get_nowait())

        asyncio.run(second_session())

        assert not publish_errors, (
            f"Bug 49: publish() raised on stale event loop: {publish_errors[0]!r}. "
            "Fix: detect closed loop and update bus._loop to the current loop."
        )
        assert len(received) == 1, (
            f"Bug 49: event was silently lost after asyncio.run() restart. "
            f"bus._loop still points to the closed first loop. received={received}"
        )

    def test_subscriber_count_consistent_after_loop_restart(self) -> None:
        """Bus internal state (subscribers) must survive event loop restart."""
        bus = StreamBus()

        async def first_session() -> None:
            bus.subscribe()
            bus.subscribe()

        asyncio.run(first_session())
        assert len(bus._subscribers) == 2

        async def second_session() -> None:
            bus.subscribe()

        asyncio.run(second_session())
        assert len(bus._subscribers) == 3, (
            "Bus subscriber list corrupted across asyncio.run() boundaries."
        )


# ── Bug 55: state.touch() does not persist ────────────────────────────────────


class TestBug55TouchNoPersist:
    """AgentState.touch() updates updated_at in memory but never calls save().

    A concurrent save() from another thread/task will persist the old timestamp,
    making the in-memory and on-disk states silently diverge.

    Fix: call self.save(path) inside touch(), or document that callers must
    save() explicitly after touch() and enforce that contract.
    """

    def test_touch_timestamp_reflected_on_disk(self, tmp_path: Path) -> None:
        from tero2.state import AgentState
        import time

        state = AgentState()
        state_path = tmp_path / "STATE.json"
        state.save(state_path)

        saved_updated_at = AgentState.from_file(state_path).updated_at

        time.sleep(0.02)  # ensure monotonic time advances
        state.touch()

        assert state.updated_at != saved_updated_at, (
            "touch() did not update the in-memory timestamp — precondition failed."
        )

        on_disk = AgentState.from_file(state_path)
        assert on_disk.updated_at == state.updated_at, (
            f"Bug 55: touch() updated in-memory timestamp to {state.updated_at!r} "
            f"but disk still has {on_disk.updated_at!r}. touch() never calls save()."
        )



# ── Bug 56: checkpoint.mark_started() discards prior state ───────────────────


class TestBug56MarkStartedDiscardsState:
    """CheckpointManager.mark_started() always constructs AgentState().

    When a plan is restarted after a FAILED or PAUSED run, all accumulated
    context (retry_count, steps_in_task, current_task, etc.) is discarded.
    The agent restarts blind, unable to escalate properly based on prior failures.

    Fix: load state from disk via restore() and transition it, instead of
    creating a fresh AgentState().
    """

    def test_mark_started_preserves_retry_count(self, tmp_path: Path) -> None:
        from tero2.checkpoint import CheckpointManager
        from tero2.disk_layer import DiskLayer
        from tero2.state import AgentState, Phase

        disk = DiskLayer(tmp_path)
        disk.init()
        cp = CheckpointManager(disk)

        # Simulate a plan that previously failed with retry context.
        # Must go through valid transitions: IDLE → RUNNING → FAILED.
        prior = AgentState()
        prior.phase = Phase.RUNNING
        prior.retry_count = 3
        prior.steps_in_task = 12
        prior.current_task = "T04"
        prior.phase = Phase.FAILED
        disk.write_state(prior)

        # Restarting the plan — mark_started should continue from the prior state.
        restarted = cp.mark_started("plan.md")

        assert restarted.retry_count == 3, (
            f"Bug 56: mark_started() created AgentState() from scratch — "
            f"retry_count reset to {restarted.retry_count} (expected 3). "
            "Prior FAILED context is silently discarded."
        )

    def test_mark_started_preserves_current_task(self, tmp_path: Path) -> None:
        from tero2.checkpoint import CheckpointManager
        from tero2.disk_layer import DiskLayer
        from tero2.state import AgentState, Phase

        disk = DiskLayer(tmp_path)
        disk.init()
        cp = CheckpointManager(disk)

        prior = AgentState()
        prior.phase = Phase.RUNNING
        prior.current_task = "T07"
        prior.phase = Phase.FAILED
        disk.write_state(prior)

        restarted = cp.mark_started("plan.md")

        assert restarted.current_task == "T07", (
            f"Bug 56: mark_started() lost current_task — "
            f"got {restarted.current_task!r}, expected 'T07'."
        )

    def test_mark_started_from_idle_is_ok(self, tmp_path: Path) -> None:
        """Fresh start from IDLE should not crash even after the fix."""
        from tero2.checkpoint import CheckpointManager
        from tero2.disk_layer import DiskLayer

        disk = DiskLayer(tmp_path)
        disk.init()
        cp = CheckpointManager(disk)

        # No prior state on disk — fresh project.
        state = cp.mark_started("plan.md")
        assert state.plan_file == "plan.md"
