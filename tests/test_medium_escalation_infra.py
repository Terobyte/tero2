"""
Failing tests demonstrating 4 medium bugs from bugs.md.

  A18 — escalation.py Level 1 DIVERSIFICATION never resets tool_repeat_count / last_tool_hash
  A28 — events.py TOCTOU race: q.full() check + q.put_nowait() not atomic → unhandled QueueFull
  A29 — project_lock.py finally block raises on lock.release(), masking original exception
  A15 — disk_layer.py write_metrics() has no lock — concurrent calls silently lose updates

Each test FAILs against current code and would pass once the bug is fixed.
"""

from __future__ import annotations

import asyncio
import threading
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from tero2.escalation import EscalationAction, EscalationLevel, execute_escalation
from tero2.state import AgentState


# ─────────────────────────────────────────────────────────────────────────────
# A18 — Level 1 DIVERSIFICATION never resets tool_repeat_count / last_tool_hash
# ─────────────────────────────────────────────────────────────────────────────


async def test_a18_diversification_resets_stuck_counters():
    """A18 — escalation.py lines 131-136: Level 1 DIVERSIFICATION does not reset
    tool_repeat_count or last_tool_hash, so the same stuck signal fires again
    immediately after diversification.

    Current buggy code::

        if action.level == EscalationLevel.DIVERSIFICATION:
            log.info("escalation Level 1: diversification...")
            state.escalation_level = EscalationLevel.DIVERSIFICATION.value
            state = checkpoint.save(state)
            await notifier.notify(...)
            return state
            # ← tool_repeat_count NOT reset to 0
            # ← last_tool_hash NOT cleared to ""

    Compare with Level 2 (lines 150-157) which correctly does::

        new_state = dataclasses_replace(state, ..., tool_repeat_count=0, last_tool_hash="", ...)

    Bug: after Level 1 escalation, tool_repeat_count and last_tool_hash remain
    at their pre-escalation values. The stuck-detection check runs again on the
    very next step and immediately fires the same signal — diversification loops
    forever instead of giving the agent a clean window to recover.
    """
    state = AgentState()
    # Simulate a stuck agent: non-zero repeat count and a known hash
    state.tool_repeat_count = 5
    state.last_tool_hash = "deadbeef1234"

    checkpoint = MagicMock()
    # checkpoint.save returns the state it receives (mutated in-place above)
    checkpoint.save.side_effect = lambda s: s

    notifier = MagicMock()
    notifier.notify = AsyncMock()

    action = EscalationAction(
        level=EscalationLevel.DIVERSIFICATION,
        inject_prompt="try a different approach",
    )

    result = await execute_escalation(
        action,
        state,
        disk=MagicMock(),
        notifier=notifier,
        checkpoint=checkpoint,
    )

    # After Level 1 escalation the stuck counters MUST be cleared so that the
    # next stuck-detection cycle starts fresh.
    assert result.tool_repeat_count == 0, (
        f"BUG A18: after DIVERSIFICATION escalation, tool_repeat_count is "
        f"{result.tool_repeat_count!r} instead of 0. "
        "The same stuck signal will fire again immediately on the next step."
    )
    assert result.last_tool_hash == "", (
        f"BUG A18: after DIVERSIFICATION escalation, last_tool_hash is "
        f"{result.last_tool_hash!r} instead of ''. "
        "Level 1 escalation does not clear the hash, so stuck detection sees "
        "the same hash on the next step and fires again without waiting."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A28 — TOCTOU race: q.full() then q.put_nowait() not atomic → unhandled QueueFull
# ─────────────────────────────────────────────────────────────────────────────


async def test_a28_emit_handles_queue_full_gracefully():
    """A28 — events.py lines 148-154: TOCTOU race between q.full() check and
    q.put_nowait() call — the queue can fill between the two, raising an
    unhandled asyncio.QueueFull that propagates to the Runner.

    Current buggy code::

        async with self._emit_lock:
            for q in self._subscribers:
                if not q.full():
                    # Fast path: there is room; put_nowait handles ...
                    q.put_nowait(event)   # ← can still raise QueueFull
                    continue

    Bug: the full() check and put_nowait() are not atomic. In theory another
    coroutine could insert between the two (or the check could be stale).
    The correct fix is to wrap put_nowait() in try/except QueueFull. Currently
    the exception propagates uncaught.

    To reproduce deterministically: mock the queue so full() returns False but
    put_nowait() raises asyncio.QueueFull. Assert emit() does not propagate it.
    """
    from tero2.events import EventDispatcher, make_event

    dispatcher = EventDispatcher()
    q = asyncio.Queue(maxsize=1)
    dispatcher._subscribers.append(q)

    event = make_event("step", role="builder", data={}, priority=False)

    # Simulate the TOCTOU: full() reports False, but put_nowait raises QueueFull
    with patch.object(q, "full", return_value=False), \
         patch.object(q, "put_nowait", side_effect=asyncio.QueueFull()):
        # BUG: current code does not wrap put_nowait in try/except,
        # so QueueFull propagates out of emit() to the caller.
        try:
            await dispatcher.emit(event)
        except asyncio.QueueFull:
            pytest.fail(
                "BUG A28: emit() propagated asyncio.QueueFull to the caller. "
                "The TOCTOU race between q.full() and q.put_nowait() causes an "
                "unhandled exception that crashes the Runner. "
                "emit() must catch QueueFull and drop the event gracefully."
            )


# ─────────────────────────────────────────────────────────────────────────────
# A29 — project_lock finally raises on lock.release(), masking original exception
# ─────────────────────────────────────────────────────────────────────────────


def test_a29_project_lock_original_exception_not_masked():
    """A29 — project_lock.py lines 63-66: the finally block calls lock.release()
    unconditionally; if release() raises, Python replaces the in-flight exception
    with the release error, silently discarding the original.

    Current buggy code::

        try:
            yield
        finally:
            lock.release()   # ← if this raises, original exception is lost

    Bug: when the body of the `with project_lock(...)` block raises ExceptionA
    and then lock.release() raises ExceptionB in finally, Python surfaces
    ExceptionB and discards ExceptionA entirely. The caller has no way to know
    what originally went wrong inside the locked section.

    The correct fix: wrap release() in a try/except inside the finally block,
    log the release error, and let the original exception propagate.
    """
    from tero2.project_lock import ProjectLock, project_lock

    class OriginalError(Exception):
        """Raised inside the locked block."""

    class ReleaseError(Exception):
        """Raised by lock.release()."""

    with tempfile.TemporaryDirectory() as tmpdir:
        sora_dir = Path(tmpdir)
        (sora_dir / "runtime").mkdir()

        with patch("tero2.project_lock.ProjectLock") as MockLock:
            mock_lock_instance = MagicMock()
            mock_lock_instance.acquire.return_value = None
            mock_lock_instance.release.side_effect = ReleaseError("release failed")
            MockLock.return_value = mock_lock_instance

            # The original exception raised inside the with block
            original = OriginalError("something went wrong inside lock")

            raised = None
            try:
                with project_lock(sora_dir):
                    raise original
            except Exception as exc:
                raised = exc

            # The caller should see the ORIGINAL exception, not the release error.
            # BUG: current code lets the finally raise propagate, so raised is
            # ReleaseError instead of OriginalError.
            assert isinstance(raised, OriginalError), (
                f"BUG A29: project_lock() surfaced {type(raised).__name__!r} "
                f"({raised!r}) instead of the original OriginalError. "
                "The finally block raised ReleaseError which masked the original "
                "exception — the caller cannot diagnose what actually went wrong."
            )


# ─────────────────────────────────────────────────────────────────────────────
# A15 — disk_layer.py write_metrics() has no lock — concurrent calls lose updates
# ─────────────────────────────────────────────────────────────────────────────


def test_a15_disk_layer_write_metrics_has_no_lock():
    """A15 — disk_layer.py lines 61-72: read_metrics() + write_metrics() implement
    a read-modify-write cycle with no threading lock, so concurrent calls
    silently overwrite each other's updates.

    Current buggy code (no lock present in DiskLayer)::

        def write_metrics(self, metrics: dict) -> None:
            path = self.sora_dir / "reports" / "metrics.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")

    Bug: two threads both call read_metrics(), get the same baseline, both
    increment a counter independently, and both call write_metrics(). The
    second write overwrites the first — net increment is 1 instead of 2.
    With N threads you lose N-1 increments.

    Simpler invariant test: DiskLayer must expose a threading.Lock (or equivalent)
    attribute that callers can use to serialize read-modify-write cycles.
    Currently no such lock exists — assert it does (failing the test now).

    Concurrent regression test: run N threads each doing read+increment+write;
    assert final count == N (any value < N proves a lost update).
    """
    from tero2.disk_layer import DiskLayer

    with tempfile.TemporaryDirectory() as tmpdir:
        disk = DiskLayer(Path(tmpdir))
        disk.init()

        N = 40
        errors: list[Exception] = []

        def increment_metric():
            try:
                # Classic read-modify-write — no lock → race condition
                m = disk.read_metrics()
                current = m.get("steps", 0)
                m["steps"] = current + 1
                disk.write_metrics(m)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=increment_metric) for _ in range(N)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"unexpected errors during concurrent writes: {errors}"

        final = disk.read_metrics().get("steps", 0)

        # With no lock some increments are silently lost — final < N
        assert final == N, (
            f"BUG A15: write_metrics() has no lock — concurrent read-modify-write "
            f"lost updates. Expected steps={N} after {N} concurrent increments, "
            f"got steps={final}. Some writes silently overwrote each other."
        )
