"""Tests for stream panel pipeline — StreamBus → format_event integration.

Coverage:
- StreamBus publishes events that format_event renders correctly
- Multiple event kinds flow through the pipeline
- Raw mode vs normal mode differences preserved through the pipeline
- Ring-buffer overflow still produces valid formatted output
- Subscriber receives every event kind and each formats without error
- Empty-role events format cleanly
- Mixed role stream from multiple agents formats independently
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from rich.text import Text

from tero2.stream_bus import StreamBus, StreamEvent, make_stream_event
from tero2.tui.widgets.stream_event_formatter import format_event


def _ev(
    role: str = "builder",
    kind: str = "text",
    *,
    content: str = "",
    tool_name: str = "",
    tool_args: dict | None = None,
    tool_output: str = "",
) -> StreamEvent:
    return StreamEvent(
        role=role,
        kind=kind,  # type: ignore[arg-type]
        timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
        content=content,
        tool_name=tool_name,
        tool_args=tool_args or {},
        tool_output=tool_output,
    )


def _plain(text: Text) -> str:
    return text.plain


class TestBusToFormatterPipeline:
    async def test_published_text_event_formats(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(kind="text", content="building module")
        bus.publish(e)
        received = q.get_nowait()
        result = format_event(received)
        assert "building module" in _plain(result)

    async def test_published_tool_use_formats(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(kind="tool_use", tool_name="bash")
        bus.publish(e)
        received = q.get_nowait()
        result = format_event(received)
        assert "bash" in _plain(result)
        assert "⚙" in _plain(result)

    async def test_published_tool_result_formats_with_truncation(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(kind="tool_result", tool_output="a\nb\nc\nd")
        bus.publish(e)
        received = q.get_nowait()
        result = format_event(received, raw_mode=False)
        plain = _plain(result)
        assert "a" in plain
        assert "bytes" in plain

    async def test_published_thinking_collapsed(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(kind="thinking", content="deep thoughts")
        bus.publish(e)
        received = q.get_nowait()
        result = format_event(received, raw_mode=False)
        assert "thinking" in _plain(result).lower()
        assert "chars" in _plain(result)

    async def test_published_error_formats_with_glyph(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(kind="error", content="crash")
        bus.publish(e)
        received = q.get_nowait()
        result = format_event(received)
        assert "crash" in _plain(result)
        assert "✗" in _plain(result)

    async def test_published_turn_end_formats(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(kind="turn_end")
        bus.publish(e)
        received = q.get_nowait()
        result = format_event(received)
        assert "turn end" in _plain(result).lower()


class TestMixedRoleStream:
    async def test_multiple_roles_format_independently(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        events = [
            _ev(role="scout", kind="text", content="scouting"),
            _ev(role="builder", kind="tool_use", tool_name="write"),
            _ev(role="architect", kind="thinking", content="designing"),
        ]
        for e in events:
            bus.publish(e)

        results = []
        for _ in range(3):
            received = q.get_nowait()
            results.append(_plain(format_event(received)))

        assert "scouting" in results[0]
        assert "write" in results[1]
        assert "thinking" in results[2].lower()

    async def test_role_prefix_present_in_formatted_output(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(role="scout", kind="text", content="found file")
        bus.publish(e)
        received = q.get_nowait()
        plain = _plain(format_event(received))
        assert "scout" in plain

    async def test_empty_role_event_formats_without_brackets(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(role="", kind="status", content="system ready")
        bus.publish(e)
        received = q.get_nowait()
        plain = _plain(format_event(received))
        assert "[]" not in plain
        assert "system ready" in plain


class TestRawModePipeline:
    async def test_raw_mode_shows_full_tool_output(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        output = "\n".join(f"line{i}" for i in range(10))
        e = _ev(kind="tool_result", tool_output=output)
        bus.publish(e)
        received = q.get_nowait()

        normal = _plain(format_event(received, raw_mode=False))
        raw = _plain(format_event(received, raw_mode=True))

        assert "line5" not in normal
        assert "line5" in raw

    async def test_raw_mode_shows_full_thinking(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        content = "A" * 500
        e = _ev(kind="thinking", content=content)
        bus.publish(e)
        received = q.get_nowait()

        raw = _plain(format_event(received, raw_mode=True))
        assert content in raw

    async def test_raw_mode_shows_tool_args(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = _ev(kind="tool_use", tool_name="bash", tool_args={"cmd": "ls -la"})
        bus.publish(e)
        received = q.get_nowait()

        raw = _plain(format_event(received, raw_mode=True))
        assert "ls -la" in raw


class TestRingBufferFormattedOutput:
    async def test_overflow_events_still_format_correctly(self) -> None:
        bus = StreamBus(max_queue_size=3)
        q = bus.subscribe()

        for i in range(10):
            bus.publish(_ev(kind="text", content=f"event-{i}"))

        remaining = []
        while not q.empty():
            e = q.get_nowait()
            result = format_event(e)
            remaining.append(_plain(result))

        for plain in remaining:
            assert "12:00:00" in plain

    async def test_dropped_events_do_not_affect_remaining_format(self) -> None:
        bus = StreamBus(max_queue_size=2)
        q = bus.subscribe()

        bus.publish(_ev(kind="error", content="dropped error"))
        bus.publish(_ev(kind="tool_use", tool_name="grep"))
        bus.publish(_ev(kind="text", content="survivor"))

        items = []
        while not q.empty():
            items.append(q.get_nowait())

        assert len(items) == 2
        for item in items:
            formatted = format_event(item)
            assert isinstance(formatted, Text)


class TestAllKindsFlow:
    @pytest.mark.parametrize(
        "kind",
        [
            "text",
            "tool_use",
            "tool_result",
            "thinking",
            "status",
            "error",
            "turn_end",
        ],
    )
    async def test_every_kind_publishes_and_formats(self, kind: str) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = StreamEvent(
            role="builder",
            kind=kind,  # type: ignore[arg-type]
            timestamp=datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc),
            content="test",
            tool_name="bash",
            tool_output="out",
        )
        bus.publish(e)
        received = q.get_nowait()
        result = format_event(received)
        assert isinstance(result, Text)
        assert "12:00:00" in _plain(result)


class TestMakeStreamEventThroughPipeline:
    async def test_factory_event_flows_through_bus(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        e = make_stream_event("builder", "tool_use", tool_name="read", tool_args={"path": "/tmp"})
        bus.publish(e)
        received = q.get_nowait()
        raw = _plain(format_event(received, raw_mode=True))
        assert "read" in raw
        assert "/tmp" in raw

    async def test_factory_timestamp_preserved(self) -> None:
        bus = StreamBus()
        q = bus.subscribe()
        ts = datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        e = make_stream_event("scout", "text", content="msg", timestamp=ts)
        bus.publish(e)
        received = q.get_nowait()
        plain = _plain(format_event(received))
        assert "10:30:00" in plain
