"""Tests for tero2.stream_bus — StreamEvent dataclass + StreamBus fan-out.

Coverage:
- StreamEvent construction and factory defaults
- StreamBus.subscribe / unsubscribe
- Fan-out to multiple subscribers
- Ring-buffer drop-oldest when queue is full
- Unsubscribed queues no longer receive events
- Dead/failing subscriber tolerance (one bad queue must not poison others)
- publish() no-op when no running loop
- publish() from worker thread after asyncio.run() restart (stale loop guard)
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timezone

from tero2.stream_bus import StreamBus, StreamEvent, make_stream_event


# ── helpers ──────────────────────────────────────────────────────────────────


def _ev(role: str = "builder", kind: str = "text", content: str = "hi") -> StreamEvent:
    return make_stream_event(role, kind, content=content)  # type: ignore[arg-type]


# ── StreamEvent construction ──────────────────────────────────────────────────


class TestStreamEvent:
    def test_required_fields(self) -> None:
        now = datetime.now(timezone.utc)
        e = StreamEvent(role="scout", kind="text", timestamp=now)
        assert e.role == "scout"
        assert e.kind == "text"
        assert e.timestamp is now

    def test_optional_fields_have_safe_defaults(self) -> None:
        e = StreamEvent(role="", kind="status", timestamp=datetime.now(timezone.utc))
        assert e.content == ""
        assert e.tool_name == ""
        assert e.tool_args == {}
        assert e.tool_output == ""
        assert e.tool_id == ""
        assert e.raw == {}

    def test_mutable_defaults_are_independent(self) -> None:
        """Each instance must have its own dict — not a shared mutable default."""
        e1 = StreamEvent(role="a", kind="text", timestamp=datetime.now(timezone.utc))
        e2 = StreamEvent(role="b", kind="text", timestamp=datetime.now(timezone.utc))
        e1.tool_args["x"] = 1
        assert "x" not in e2.tool_args

    def test_all_kind_literals_accepted(self) -> None:
        kinds = ["text", "tool_use", "tool_result", "thinking", "status", "error", "turn_end"]
        now = datetime.now(timezone.utc)
        for k in kinds:
            e = StreamEvent(role="builder", kind=k, timestamp=now)  # type: ignore[arg-type]
            assert e.kind == k


class TestMakeStreamEvent:
    def test_timestamp_defaults_to_utc_now(self) -> None:
        before = datetime.now(timezone.utc)
        e = make_stream_event("builder", "text")
        after = datetime.now(timezone.utc)
        assert before <= e.timestamp <= after
        assert e.timestamp.tzinfo == timezone.utc

    def test_explicit_timestamp_preserved(self) -> None:
        ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
        e = make_stream_event("scout", "status", timestamp=ts)
        assert e.timestamp is ts

    def test_tool_args_none_becomes_empty_dict(self) -> None:
        e = make_stream_event("builder", "tool_use", tool_args=None)
        assert e.tool_args == {}

    def test_raw_none_becomes_empty_dict(self) -> None:
        e = make_stream_event("builder", "tool_use", raw=None)
        assert e.raw == {}

    def test_all_kwargs_forwarded(self) -> None:
        e = make_stream_event(
            "builder", "tool_use",
            tool_name="bash",
            tool_args={"cmd": "ls"},
            tool_output="file.txt\n",
            tool_id="id-1",
            content="running",
            raw={"type": "tool_use"},
        )
        assert e.tool_name == "bash"
        assert e.tool_args == {"cmd": "ls"}
        assert e.tool_output == "file.txt\n"
        assert e.tool_id == "id-1"
        assert e.content == "running"
        assert e.raw == {"type": "tool_use"}


# ── StreamBus — subscribe / unsubscribe ──────────────────────────────────────


class TestSubscribeUnsubscribe:
    async def test_subscribe_returns_queue(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        assert isinstance(q, asyncio.Queue)

    async def test_subscribe_sets_maxsize(self) -> None:
        bus = StreamBus(max_queue_size=100)
        q = bus.subscribe()
        assert q.maxsize == 100

    async def test_default_maxsize_is_2000(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        assert q.maxsize == 2000

    async def test_multiple_subscribers_are_independent(self) -> None:
        bus = StreamBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        assert q1 is not q2
        assert len(bus._subscribers) == 2

    async def test_unsubscribe_removes_queue(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        assert q not in bus._subscribers

    async def test_unsubscribe_unknown_queue_is_silent(self) -> None:
        bus = StreamBus()
        q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        bus.unsubscribe(q)   # must not raise

    async def test_unsubscribe_only_removes_one_instance(self) -> None:
        """If somehow the same queue is added twice, only one copy is removed."""
        bus = StreamBus()
        q = bus.subscribe()
        bus._subscribers.append(q)   # manually duplicate
        assert len(bus._subscribers) == 2
        bus.unsubscribe(q)
        assert len(bus._subscribers) == 1


# ── StreamBus — fan-out publishing ───────────────────────────────────────────


class TestFanOut:
    async def test_single_subscriber_receives_event(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev()
        bus.publish(e)
        assert q.get_nowait() is e

    async def test_all_subscribers_receive_event(self) -> None:
        bus = StreamBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        q3 = bus.subscribe()
        e = _ev()
        bus.publish(e)
        assert q1.get_nowait() is e
        assert q2.get_nowait() is e
        assert q3.get_nowait() is e

    async def test_no_subscribers_does_not_raise(self) -> None:
        bus = StreamBus()
        bus.publish(_ev())   # no subscribers — must be a no-op

    async def test_multiple_events_ordered(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        events = [_ev(content=str(i)) for i in range(5)]
        for e in events:
            bus.publish(e)
        received = [q.get_nowait() for _ in range(5)]
        assert received == events

    async def test_unsubscribed_queue_receives_no_events(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish(_ev())
        assert q.empty()


# ── StreamBus — ring-buffer drop-oldest ──────────────────────────────────────


class TestRingBuffer:
    async def test_queue_full_drops_oldest(self) -> None:
        """When queue is full, the oldest event must be dropped for the newest."""
        bus = StreamBus(max_queue_size=3)
        q = bus.subscribe()

        # Fill the queue exactly
        e1 = _ev(content="first")
        e2 = _ev(content="second")
        e3 = _ev(content="third")
        bus.publish(e1)
        bus.publish(e2)
        bus.publish(e3)
        assert q.full()

        # Overflow: e4 should displace e1
        e4 = _ev(content="fourth")
        bus.publish(e4)

        items = [q.get_nowait() for _ in range(q.qsize())]
        assert e1 not in items, "oldest event must be dropped"
        assert e4 in items, "newest event must be present"
        assert e2 in items
        assert e3 in items

    async def test_ring_buffer_size_stays_bounded(self) -> None:
        """Publishing beyond capacity must not grow the queue."""
        bus = StreamBus(max_queue_size=5)
        q = bus.subscribe()

        for i in range(20):
            bus.publish(_ev(content=str(i)))

        # Queue must never exceed max
        assert q.qsize() <= 5

    async def test_ring_buffer_at_default_capacity(self) -> None:
        """Ring-buffer with default 2000-item capacity."""
        bus = StreamBus()   # default maxsize=2000
        q = bus.subscribe()

        # Fill completely
        for i in range(2000):
            bus.publish(_ev(content=str(i)))
        assert q.qsize() == 2000

        first_event = _ev(content="overflow")
        # Fetch oldest item before overflow so we can assert it's gone
        oldest = q.get_nowait()
        q.put_nowait(oldest)   # put it back to keep queue full
        assert q.full()

        bus.publish(first_event)
        items = [q.get_nowait() for _ in range(q.qsize())]
        assert first_event in items

    async def test_multiple_overflows_keep_latest_n(self) -> None:
        """After N overflows the queue should hold the last *maxsize* events."""
        bus = StreamBus(max_queue_size=3)
        q = bus.subscribe()

        events = [_ev(content=str(i)) for i in range(10)]
        for e in events:
            bus.publish(e)

        remaining = [q.get_nowait() for _ in range(q.qsize())]
        # Last 3 published events: events[7], events[8], events[9]
        assert events[9] in remaining
        assert events[8] in remaining
        assert events[7] in remaining
        assert events[0] not in remaining


# ── StreamBus — dead subscriber tolerance ────────────────────────────────────


class TestDeadSubscriberTolerance:
    async def test_bad_subscriber_does_not_block_others(self) -> None:
        """A subscriber whose put_nowait raises must not prevent delivery to others."""
        bus = StreamBus()

        # Create a healthy subscriber
        good_q = bus.subscribe()

        # Inject a broken queue that raises on put_nowait
        class _BrokenQueue:
            def full(self) -> bool:
                return False

            def put_nowait(self, item: object) -> None:
                raise RuntimeError("simulated failure")

            def get_nowait(self) -> StreamEvent:
                raise asyncio.QueueEmpty

        broken: asyncio.Queue[StreamEvent] = _BrokenQueue()  # type: ignore[assignment]
        bus._subscribers.append(broken)

        e = _ev()
        bus.publish(e)   # must not raise

        # Good subscriber must still receive the event
        assert good_q.get_nowait() is e

    async def test_publish_survives_empty_subscriber_list(self) -> None:
        bus = StreamBus()
        # No subscribers at all
        bus.publish(_ev())   # no crash

    async def test_multiple_events_after_bad_subscriber(self) -> None:
        """Subsequent publishes work normally even after a bad subscriber was encountered."""
        bus = StreamBus()
        good_q = bus.subscribe()

        class _AlwaysFull:
            def full(self) -> bool:
                return True

            def get_nowait(self) -> StreamEvent:
                raise RuntimeError("broken get")

            def put_nowait(self, item: object) -> None:
                raise RuntimeError("broken put")

        bus._subscribers.append(_AlwaysFull())  # type: ignore[arg-type]

        events = [_ev(content=str(i)) for i in range(5)]
        for e in events:
            bus.publish(e)

        received = [good_q.get_nowait() for _ in range(good_q.qsize())]
        assert received == events


# ── StreamBus — stale loop guard (worker-thread after asyncio.run() restart) ──


class TestStaleLoopGuard:
    def test_worker_thread_publish_after_loop_restart_does_not_raise(self) -> None:
        """publish() from a plain thread must not raise RuntimeError when the
        cached event loop was closed by a previous asyncio.run() call.

        Reproducer from review feedback:
        1. Run asyncio.run() once — StreamBus captures that loop.
        2. asyncio.run() exits, closing that loop.
        3. A second thread calls publish() — previously raised
           ``RuntimeError: Event loop is closed`` inside call_soon_threadsafe.
        """
        bus = StreamBus()

        # First asyncio.run() — lets the bus capture its loop.
        async def first_run() -> None:
            bus.subscribe()
            bus.publish(_ev(content="first"))

        asyncio.run(first_run())
        # The loop captured by `bus._loop` is now closed.
        assert bus._loop is not None
        assert bus._loop.is_closed()

        # Publish from a plain worker thread — must be silent (no RuntimeError).
        errors: list[Exception] = []

        def thread_publish() -> None:
            try:
                bus.publish(_ev(content="from thread after restart"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=thread_publish, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert not errors, f"publish() raised from worker thread: {errors}"

    def test_worker_thread_publish_after_two_restarts_does_not_raise(self) -> None:
        """Stale-loop guard holds across multiple asyncio.run() restarts."""
        bus = StreamBus()

        for _ in range(3):
            asyncio.run(asyncio.sleep(0))

        errors: list[Exception] = []

        def thread_publish() -> None:
            try:
                bus.publish(_ev(content="thread"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        t = threading.Thread(target=thread_publish, daemon=True)
        t.start()
        t.join(timeout=2.0)

        assert not errors, f"publish() raised: {errors}"


# ── Additional spec-pinned tests (Task 3 from plan) ─────────────────────────


class TestSpecPinned:
    """Function-level test bodies from the live-agent-stream plan, Task 3.

    These verify the same ring-buffer and fault-tolerance guarantees as the
    class-based suites above, but express assertions in the plan's exact terms
    (content strings, ordering) to prevent silent regressions.
    """

    async def test_ring_buffer_drops_oldest_content_sequence(self) -> None:
        """Ring-buffer with size 3: publishing 5 events leaves content "2","3","4"."""
        bus = StreamBus(max_queue_size=3)
        q = bus.subscribe()
        events = [make_stream_event("r", "text", content=str(i)) for i in range(5)]
        for ev in events:
            bus.publish(ev)
        received = []
        for _ in range(3):
            received.append(await asyncio.wait_for(q.get(), timeout=0.05))
        assert [e.content for e in received] == ["2", "3", "4"]

    async def test_survives_subscriber_with_broken_queue(self) -> None:
        """A subscriber whose put_nowait raises must not block good subscribers."""
        bus = StreamBus(max_queue_size=3)
        good = bus.subscribe()

        class _BadQueue:
            def full(self) -> bool:
                return False

            def put_nowait(self, item: object) -> None:
                raise RuntimeError("boom")

            def get_nowait(self) -> StreamEvent:
                raise asyncio.QueueEmpty()

        bus._subscribers.append(_BadQueue())  # type: ignore[arg-type]
        ev = make_stream_event("r", "text", content="x")
        bus.publish(ev)  # must not raise
        received = await asyncio.wait_for(good.get(), timeout=0.05)
        assert received is ev
