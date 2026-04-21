"""End-to-end stream flow tests.

Validates the complete path:
    raw provider dict event
        → normalize_raw() → StreamEvent
        → StreamBus.publish()
        → subscriber asyncio.Queue receives it

Covers:
  Step 1 — A single text event flows subscriber-to-queue end-to-end
  Step 2 — Multiple events arrive in order
  Step 3 — tool_use and tool_result events carry correct fields
  Step 4 — thinking events preserved end-to-end
  Step 5 — status events (start/end markers) flow correctly
  Step 6 — error events preserved end-to-end
  Step 7 — turn_end marks stream end
  Step 8 — Unknown event kind falls back to text
  Step 9 — Multiple subscribers each receive all events (fan-out)
  Step 10 — Unsubscribed queue stops receiving after unsubscribe
  Step 11 — Ring-buffer drop: oldest event is dropped when queue is full
  Step 12 — StreamBus.publish() is safe to call with no subscribers
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from tero2.stream_bus import StreamBus, StreamEvent, make_stream_event
from tests.normalizers import normalize_raw


# ── Step 1: Single text event flows end-to-end ───────────────────────────────


class TestSingleTextEvent:
    async def test_text_event_arrives_in_queue(self) -> None:
        """A text event published to the bus must appear in the subscriber queue."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "text", "text": "hello world"}
        event = normalize_raw(raw, role="builder")
        bus.publish(event)

        received = q.get_nowait()
        assert received.kind == "text"
        assert received.content == "hello world"
        assert received.role == "builder"

    async def test_text_event_timestamp_is_utc(self) -> None:
        """Timestamps on stream events must be timezone-aware UTC."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "text", "text": "ts check"}
        event = normalize_raw(raw, role="scout")
        bus.publish(event)

        received = q.get_nowait()
        assert received.timestamp.tzinfo is not None
        assert received.timestamp.tzinfo == timezone.utc


# ── Step 2: Multiple events arrive in order ───────────────────────────────────


class TestMultipleEventOrder:
    async def test_events_arrive_in_publish_order(self) -> None:
        """Events must be dequeued in FIFO order."""
        bus = StreamBus()
        q = bus.subscribe()

        raws = [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
            {"type": "text", "text": "third"},
        ]
        for raw in raws:
            bus.publish(normalize_raw(raw, role="builder"))

        contents = [q.get_nowait().content for _ in raws]
        assert contents == ["first", "second", "third"]

    async def test_five_events_all_delivered(self) -> None:
        """All five events must be present in the queue."""
        bus = StreamBus()
        q = bus.subscribe()

        for i in range(5):
            bus.publish(normalize_raw({"type": "text", "text": f"msg-{i}"}, role="verifier"))

        assert q.qsize() == 5


# ── Step 3: tool_use and tool_result fields ───────────────────────────────────


class TestToolEvents:
    async def test_tool_use_fields_preserved(self) -> None:
        """tool_use event carries tool_name, tool_args, and tool_id."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {
            "type": "tool_use",
            "name": "bash",
            "id": "tid-001",
            "input": {"command": "ls -la"},
        }
        bus.publish(normalize_raw(raw, role="builder"))

        received = q.get_nowait()
        assert received.kind == "tool_use"
        assert received.tool_name == "bash"
        assert received.tool_id == "tid-001"
        assert received.tool_args == {"command": "ls -la"}

    async def test_tool_result_fields_preserved(self) -> None:
        """tool_result event carries tool_output and tool_id."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {
            "type": "tool_result",
            "tool_use_id": "tid-001",
            "content": "output-data\n",
        }
        bus.publish(normalize_raw(raw, role="builder"))

        received = q.get_nowait()
        assert received.kind == "tool_result"
        assert received.tool_output == "output-data\n"
        assert received.tool_id == "tid-001"


# ── Step 4: thinking events ───────────────────────────────────────────────────


class TestThinkingEvents:
    async def test_thinking_content_preserved(self) -> None:
        """Chain-of-thought text must flow through as kind='thinking'."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "thinking", "thinking": "this is internal reasoning"}
        bus.publish(normalize_raw(raw, role="architect"))

        received = q.get_nowait()
        assert received.kind == "thinking"
        assert "internal reasoning" in received.content

    async def test_thinking_role_preserved(self) -> None:
        """Role attribute must survive normalization."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "thinking", "thinking": "thought"}
        bus.publish(normalize_raw(raw, role="coach"))

        received = q.get_nowait()
        assert received.role == "coach"


# ── Step 5: status events ─────────────────────────────────────────────────────


class TestStatusEvents:
    async def test_status_content_preserved(self) -> None:
        """Status marker events (start/end) must flow with kind='status'."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "status", "text": "task started"}
        bus.publish(normalize_raw(raw, role="builder"))

        received = q.get_nowait()
        assert received.kind == "status"
        assert "started" in received.content


# ── Step 6: error events ──────────────────────────────────────────────────────


class TestErrorEvents:
    async def test_error_content_preserved(self) -> None:
        """Error events must carry the error message in content."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "error", "error": "permission denied", "text": "permission denied"}
        bus.publish(normalize_raw(raw, role="builder"))

        received = q.get_nowait()
        assert received.kind == "error"
        assert "permission denied" in received.content


# ── Step 7: turn_end marks stream end ────────────────────────────────────────


class TestTurnEndEvent:
    async def test_turn_end_kind_set(self) -> None:
        """turn_end events must normalize to kind='turn_end'."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "turn_end", "text": ""}
        bus.publish(normalize_raw(raw, role="builder"))

        received = q.get_nowait()
        assert received.kind == "turn_end"

    async def test_turn_end_after_text_events(self) -> None:
        """turn_end must arrive as the last event in a complete response sequence."""
        bus = StreamBus()
        q = bus.subscribe()

        sequence = [
            {"type": "text", "text": "line one"},
            {"type": "text", "text": "line two"},
            {"type": "turn_end", "text": ""},
        ]
        for raw in sequence:
            bus.publish(normalize_raw(raw, role="reviewer"))

        events = [q.get_nowait() for _ in sequence]
        assert events[-1].kind == "turn_end"
        assert events[0].kind == "text"


# ── Step 8: Unknown kind falls back to text ───────────────────────────────────


class TestUnknownKindFallback:
    async def test_unknown_type_becomes_text(self) -> None:
        """A raw dict with an unrecognised 'type' must fall back to kind='text'."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"type": "some_future_type", "text": "future content"}
        bus.publish(normalize_raw(raw, role="scout"))

        received = q.get_nowait()
        assert received.kind == "text"

    async def test_dict_without_type_key(self) -> None:
        """A raw dict missing the 'type' key is treated as a plain text event."""
        bus = StreamBus()
        q = bus.subscribe()

        raw = {"text": "no type key"}
        bus.publish(normalize_raw(raw, role="scout"))

        received = q.get_nowait()
        assert received.kind == "text"
        assert "no type key" in received.content


# ── Step 9: Fan-out to multiple subscribers ───────────────────────────────────


class TestFanOut:
    async def test_two_subscribers_both_receive_event(self) -> None:
        """Two subscribers must independently receive the same event."""
        bus = StreamBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()

        raw = {"type": "text", "text": "broadcast"}
        bus.publish(normalize_raw(raw, role="builder"))

        e1 = q1.get_nowait()
        e2 = q2.get_nowait()
        assert e1.kind == "text"
        assert e1.content == "broadcast"
        assert e2.kind == "text"
        assert e2.content == "broadcast"

    async def test_three_subscribers_all_receive_all_events(self) -> None:
        """Three subscribers must each get the same two events."""
        bus = StreamBus()
        queues = [bus.subscribe() for _ in range(3)]

        for i in range(2):
            bus.publish(normalize_raw({"type": "text", "text": f"msg-{i}"}, role="builder"))

        for q in queues:
            assert q.qsize() == 2


# ── Step 10: Unsubscribed queue stops receiving ───────────────────────────────


class TestUnsubscribe:
    async def test_unsubscribed_queue_does_not_receive_events(self) -> None:
        """After unsubscribe(), the queue must not receive new events."""
        bus = StreamBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()

        bus.unsubscribe(q1)

        raw = {"type": "text", "text": "after unsubscribe"}
        bus.publish(normalize_raw(raw, role="builder"))

        assert q1.empty(), "q1 received an event after being unsubscribed"
        assert not q2.empty(), "q2 (still subscribed) should have received the event"

    async def test_double_unsubscribe_is_safe(self) -> None:
        """Calling unsubscribe twice on the same queue must not raise."""
        bus = StreamBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.unsubscribe(q)  # must not raise


# ── Step 11: Ring-buffer drop-oldest ─────────────────────────────────────────


class TestRingBuffer:
    async def test_oldest_event_dropped_when_queue_full(self) -> None:
        """When the subscriber queue is full, the oldest event is evicted."""
        bus = StreamBus(max_queue_size=3)
        q = bus.subscribe()

        for i in range(4):
            bus.publish(normalize_raw({"type": "text", "text": f"evt-{i}"}, role="builder"))

        assert q.qsize() == 3
        # evt-0 should have been dropped; evt-1, evt-2, evt-3 remain
        events = [q.get_nowait() for _ in range(3)]
        contents = [e.content for e in events]
        assert "evt-0" not in contents
        assert "evt-3" in contents


# ── Step 12: publish with no subscribers ──────────────────────────────────────


class TestPublishNoSubscribers:
    async def test_publish_with_no_subscribers_is_safe(self) -> None:
        """Publishing to an empty bus must not raise."""
        bus = StreamBus()
        raw = {"type": "text", "text": "nobody home"}
        event = normalize_raw(raw, role="builder")
        bus.publish(event)  # must not raise

    async def test_publish_after_all_unsubscribed(self) -> None:
        """Unsubscribing all subscribers and then publishing must be safe."""
        bus = StreamBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        event = normalize_raw({"type": "text", "text": "empty bus"}, role="builder")
        bus.publish(event)  # must not raise
