"""Tests for tero2.events — EventDispatcher backpressure and bookkeeping."""

from __future__ import annotations

import asyncio

import pytest

from tero2.events import Command, Event, EventDispatcher, make_event


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ev(kind: str = "step", *, priority: bool = False) -> Event:
    return make_event(kind, priority=priority)


def _pri(kind: str = "done") -> Event:
    return make_event(kind, priority=True)


# ---------------------------------------------------------------------------
# Dataclass construction
# ---------------------------------------------------------------------------


class TestEventDataclass:
    def test_defaults(self) -> None:
        e = _ev()
        assert e.kind == "step"
        assert e.role == ""
        assert e.data == {}
        assert e.priority is False
        assert e.timestamp is not None

    def test_priority_flag(self) -> None:
        e = _pri()
        assert e.priority is True


class TestCommandDataclass:
    def test_defaults(self) -> None:
        c = Command(kind="pause")
        assert c.source == ""
        assert c.data == {}

    def test_custom_fields(self) -> None:
        c = Command(kind="steer", data={"text": "go"}, source="tui")
        assert c.source == "tui"
        assert c.data == {"text": "go"}


class TestMakeEvent:
    def test_timestamp_is_utc(self) -> None:
        from datetime import timezone

        e = make_event("log")
        assert e.timestamp.tzinfo == timezone.utc

    def test_data_defaults_to_empty_dict(self) -> None:
        e = make_event("log", data=None)
        assert e.data == {}


# ---------------------------------------------------------------------------
# Subscribe / unsubscribe
# ---------------------------------------------------------------------------


class TestSubscribeUnsubscribe:
    async def test_subscribe_returns_queue(self) -> None:
        d = EventDispatcher()
        q = d.subscribe()
        assert isinstance(q, asyncio.Queue)

    async def test_multiple_subscribers(self) -> None:
        d = EventDispatcher()
        q1 = d.subscribe()
        q2 = d.subscribe()
        assert q1 is not q2
        assert len(d._subscribers) == 2

    async def test_unsubscribe_removes_queue(self) -> None:
        d = EventDispatcher()
        q = d.subscribe()
        d.unsubscribe(q)
        assert q not in d._subscribers

    async def test_unsubscribe_unknown_queue_is_silent(self) -> None:
        d = EventDispatcher()
        q: asyncio.Queue[Event] = asyncio.Queue()
        d.unsubscribe(q)  # must not raise


# ---------------------------------------------------------------------------
# Fan-out
# ---------------------------------------------------------------------------


class TestFanOut:
    async def test_all_subscribers_receive_event(self) -> None:
        d = EventDispatcher()
        q1 = d.subscribe()
        q2 = d.subscribe()
        e = _ev("phase_change")
        await d.emit(e)
        assert q1.get_nowait() is e
        assert q2.get_nowait() is e

    async def test_no_subscribers_does_not_raise(self) -> None:
        d = EventDispatcher()
        await d.emit(_ev())  # no subscribers — should be a no-op


# ---------------------------------------------------------------------------
# Backpressure: non-priority events
# ---------------------------------------------------------------------------


class TestBackpressureNonPriority:
    async def test_full_queue_drops_oldest_non_priority(self) -> None:
        """When queue is full the oldest item is discarded for a new non-priority event."""
        d = EventDispatcher()
        q = d.subscribe()
        first = _ev("step")
        await d.emit(first)
        for _ in range(499):
            await d.emit(_ev("log"))
        assert q.full()

        new_ev = _ev("usage_update")
        await d.emit(new_ev)  # must not raise

        assert q.full()
        items = [q.get_nowait() for _ in range(q.qsize())]
        assert first not in items, "oldest item must be dropped"
        assert new_ev in items, "new event must be present"

    async def test_unfinished_tasks_not_inflated_non_priority(self) -> None:
        """_unfinished_tasks must equal qsize after non-priority backpressure."""
        d = EventDispatcher()
        q = d.subscribe()
        for _ in range(500):
            await d.emit(_ev("log"))
        assert q.full()

        await d.emit(_ev("usage_update"))
        assert q.qsize() == q._unfinished_tasks  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Backpressure: priority events
# ---------------------------------------------------------------------------


class TestBackpressurePriority:
    async def test_priority_displaces_oldest_non_priority(self) -> None:
        """Priority event must discard the oldest non-priority item, not itself."""
        d = EventDispatcher()
        q = d.subscribe()
        first_np = _ev("step")
        await d.emit(first_np)
        for _ in range(499):
            await d.emit(_ev("log"))
        assert q.full()

        p = _pri("done")
        await d.emit(p)  # must not raise

        items = [q.get_nowait() for _ in range(q.qsize())]
        assert first_np not in items, "oldest non-priority must be dropped"
        assert p in items, "priority event must survive"

    # ------------------------------------------------------------------
    # Regression #1 — _unfinished_tasks bookkeeping
    # ------------------------------------------------------------------

    async def test_regression_unfinished_tasks_500_nonpri_plus_1_pri(self) -> None:
        """
        Repro from review: 500 non-priority events fill the queue; then one
        priority event displaces one non-priority item.

        Expected: q.qsize() == 500 and q._unfinished_tasks == 500.
        Broken behaviour (drain-and-requeue): _unfinished_tasks becomes 1000.
        """
        d = EventDispatcher()
        q = d.subscribe()
        for _ in range(500):
            await d.emit(_ev("step", priority=False))
        assert q.qsize() == 500

        await d.emit(_pri("done"))

        assert q.qsize() == 500
        assert q._unfinished_tasks == 500  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Regression #2 — QueueFull after priority overflow
    # ------------------------------------------------------------------

    async def test_regression_no_queue_full_after_overflow(self) -> None:
        """
        Repro from review: fill with 500 priority events, emit one more
        priority event (triggers 1-item overflow to 501), then emit a
        non-priority event.  Must NOT raise QueueFull.
        """
        d = EventDispatcher()
        q = d.subscribe()
        for _ in range(500):
            await d.emit(_pri("phase_change"))
        assert q.qsize() == 500

        # Overflow: all items are priority so queue grows by 1
        await d.emit(_pri("stuck"))
        assert q.qsize() == 501

        # This must NOT raise QueueFull
        try:
            await d.emit(_ev("usage_update", priority=False))
        except asyncio.QueueFull:
            pytest.fail("emit raised QueueFull after priority overflow")

    async def test_all_priority_overflows_by_one(self) -> None:
        """When all queued items are priority, overflow by exactly one is allowed."""
        d = EventDispatcher()
        q = d.subscribe()
        for _ in range(500):
            await d.emit(_pri("phase_change"))
        assert q.qsize() == 500

        await d.emit(_pri("done"))
        assert q.qsize() == 501

    async def test_unfinished_tasks_after_priority_backpressure(self) -> None:
        """_unfinished_tasks must stay in sync after repeated priority backpressure."""
        d = EventDispatcher()
        q = d.subscribe()
        for _ in range(500):
            await d.emit(_ev("step", priority=False))
        for _ in range(10):
            await d.emit(_pri("done"))
        assert q.qsize() == q._unfinished_tasks  # type: ignore[attr-defined]

    # ------------------------------------------------------------------
    # Regression #3 — non-priority event must not displace priority head
    # ------------------------------------------------------------------

    async def test_non_priority_preserves_priority_head(self) -> None:
        """
        Queue has a priority event as the oldest item, followed by 499
        non-priority events (full).  Emitting a new non-priority event
        must drop the oldest NON-priority item, not the priority event.
        """
        d = EventDispatcher()
        q = d.subscribe()

        pri_event = _pri("done")
        await d.emit(pri_event)
        for _ in range(499):
            await d.emit(_ev("log"))
        assert q.full()

        new_np = _ev("step")
        await d.emit(new_np)

        items = [q.get_nowait() for _ in range(q.qsize())]
        assert pri_event in items, "priority event at head must be preserved"
        assert new_np in items, "new non-priority event must be enqueued"

    async def test_non_priority_dropped_when_all_priority(self) -> None:
        """
        Queue is full of 500 priority events.  A non-priority event
        must be silently dropped rather than displacing any priority event.
        """
        d = EventDispatcher()
        q = d.subscribe()
        for _ in range(500):
            await d.emit(_pri("phase_change"))
        assert q.full()

        await d.emit(_ev("step"))

        assert q.qsize() == 500
        items = [q.get_nowait() for _ in range(q.qsize())]
        assert all(e.priority for e in items)
