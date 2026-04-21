"""Tests for tero2.tui.widgets.log_view — LogView widget and _format_event helper.

Coverage:
- _format_event returns rich.Text with correct styling
- Timestamp formatting (HH:MM:SS)
- Kind-specific colour mapping
- Role colour prefix when role is set
- Message extraction from event.data ("message", "msg", "text" keys)
- Fallback to first key=value pair when no message key
- Priority star glyph appended
- LogView.push_event formats and writes without error
- LogView.push_message appends styled text
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from rich.text import Text
from textual.app import App, ComposeResult

from tero2.events import Event, make_event
from tero2.tui.widgets.log_view import LogView, _format_event


def _ts() -> datetime:
    return datetime(2026, 3, 15, 14, 30, 45, tzinfo=timezone.utc)


def _event(
    kind: str = "step",
    role: str = "",
    data: dict | None = None,
    *,
    priority: bool = False,
) -> Event:
    return Event(
        timestamp=_ts(),
        kind=kind,
        role=role,
        data=data if data is not None else {},
        priority=priority,
    )


def _plain(text: Text) -> str:
    return text.plain


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield LogView(id="test-log")


class TestFormatEventReturnType:
    def test_returns_rich_text(self) -> None:
        e = _event()
        result = _format_event(e)
        assert isinstance(result, Text)

    def test_plain_string_not_empty(self) -> None:
        e = _event(kind="step", data={"message": "working"})
        result = _format_event(e)
        assert len(_plain(result)) > 0


class TestTimestampFormatting:
    def test_timestamp_included_in_output(self) -> None:
        e = _event()
        result = _format_event(e)
        assert "14:30:45" in _plain(result)

    def test_timestamp_prefix_format(self) -> None:
        e = _event()
        result = _format_event(e)
        plain = _plain(result)
        assert plain.startswith("[14:30:45]")


class TestKindColourMapping:
    def test_step_kind_shown(self) -> None:
        e = _event(kind="step")
        plain = _plain(_format_event(e))
        assert "step" in plain

    def test_phase_change_shown(self) -> None:
        e = _event(kind="phase_change")
        plain = _plain(_format_event(e))
        assert "phase_change" in plain

    def test_stuck_kind_shown(self) -> None:
        e = _event(kind="stuck")
        plain = _plain(_format_event(e))
        assert "stuck" in plain

    def test_done_kind_shown(self) -> None:
        e = _event(kind="done")
        plain = _plain(_format_event(e))
        assert "done" in plain

    def test_error_kind_shown(self) -> None:
        e = _event(kind="error")
        plain = _plain(_format_event(e))
        assert "error" in plain

    def test_log_kind_shown(self) -> None:
        e = _event(kind="log")
        plain = _plain(_format_event(e))
        assert "log" in plain

    def test_escalation_kind_shown(self) -> None:
        e = _event(kind="escalation")
        plain = _plain(_format_event(e))
        assert "escalation" in plain

    def test_unknown_kind_does_not_crash(self) -> None:
        e = _event(kind="custom_unknown")
        _format_event(e)

    def test_kind_padded_in_output(self) -> None:
        e = _event(kind="step")
        plain = _plain(_format_event(e))
        assert "step" in plain


class TestRolePrefix:
    def test_role_shown_when_set(self) -> None:
        e = _event(role="builder")
        plain = _plain(_format_event(e))
        assert "builder" in plain

    def test_role_not_shown_when_empty(self) -> None:
        e = _event(role="")
        plain = _plain(_format_event(e))
        assert "[]" not in plain

    def test_scout_role_shown(self) -> None:
        e = _event(role="scout")
        plain = _plain(_format_event(e))
        assert "scout" in plain

    def test_architect_role_shown(self) -> None:
        e = _event(role="architect")
        plain = _plain(_format_event(e))
        assert "architect" in plain


class TestMessageExtraction:
    def test_message_key_extracted(self) -> None:
        e = _event(data={"message": "hello world"})
        plain = _plain(_format_event(e))
        assert "hello world" in plain

    def test_msg_key_extracted(self) -> None:
        e = _event(data={"msg": "short message"})
        plain = _plain(_format_event(e))
        assert "short message" in plain

    def test_text_key_extracted(self) -> None:
        e = _event(data={"text": "raw text"})
        plain = _plain(_format_event(e))
        assert "raw text" in plain

    def test_message_preferred_over_msg(self) -> None:
        e = _event(data={"message": "primary", "msg": "secondary"})
        plain = _plain(_format_event(e))
        assert "primary" in plain

    def test_msg_preferred_over_text(self) -> None:
        e = _event(data={"msg": "from_msg", "text": "from_text"})
        plain = _plain(_format_event(e))
        assert "from_msg" in plain

    def test_empty_data_does_not_crash(self) -> None:
        e = _event(data={})
        _format_event(e)

    def test_fallback_shows_first_key_value(self) -> None:
        e = _event(data={"count": 42})
        plain = _plain(_format_event(e))
        assert "count" in plain
        assert "42" in plain


class TestPriorityStar:
    def test_priority_event_has_star(self) -> None:
        e = _event(priority=True)
        plain = _plain(_format_event(e))
        assert "★" in plain

    def test_non_priority_no_star(self) -> None:
        e = _event(priority=False, data={"message": "normal"})
        plain = _plain(_format_event(e))
        assert "★" not in plain


def _kind_span_style(event: Event) -> str:
    result = _format_event(event)
    assert len(result.spans) >= 2
    return str(result.spans[1].style)


class TestKindColourStyleApplied:
    def test_step_kind_style_is_green(self) -> None:
        style = _kind_span_style(_event(kind="step"))
        assert "green" in style

    def test_error_kind_style_is_red(self) -> None:
        style = _kind_span_style(_event(kind="error"))
        assert "red" in style

    def test_stuck_kind_style_is_red_bold(self) -> None:
        style = _kind_span_style(_event(kind="stuck"))
        assert "red" in style
        assert "bold" in style

    def test_done_kind_style_is_green_bold(self) -> None:
        style = _kind_span_style(_event(kind="done"))
        assert "green" in style
        assert "bold" in style

    def test_phase_change_style_is_cyan(self) -> None:
        style = _kind_span_style(_event(kind="phase_change"))
        assert "cyan" in style

    def test_escalation_style_is_magenta(self) -> None:
        style = _kind_span_style(_event(kind="escalation"))
        assert "magenta" in style

    def test_log_style_is_white(self) -> None:
        style = _kind_span_style(_event(kind="log"))
        assert "white" in style

    def test_unknown_kind_uses_default_white(self) -> None:
        style = _kind_span_style(_event(kind="custom_unknown"))
        assert "white" in style


def _role_span_style(event: Event) -> str:
    result = _format_event(event)
    for span in result.spans:
        seg = result.plain[span.start : span.end]
        if event.role in seg:
            return str(span.style)
    return ""


class TestRoleColourStyleApplied:
    def test_scout_role_style_is_cyan(self) -> None:
        style = _role_span_style(_event(role="scout"))
        assert "cyan" in style

    def test_coach_role_style_is_yellow(self) -> None:
        style = _role_span_style(_event(role="coach"))
        assert "yellow" in style

    def test_architect_role_style_is_blue(self) -> None:
        style = _role_span_style(_event(role="architect"))
        assert "blue" in style

    def test_execute_role_style_is_green(self) -> None:
        style = _role_span_style(_event(role="execute"))
        assert "green" in style

    def test_verifier_role_style_is_magenta(self) -> None:
        style = _role_span_style(_event(role="verifier"))
        assert "magenta" in style

    def test_unknown_role_uses_default_white(self) -> None:
        style = _role_span_style(_event(role="unknown_role"))
        assert "white" in style


class TestLogViewWidget:
    @pytest.mark.asyncio
    async def test_push_event_writes_formatted_content(self) -> None:
        app = _HostApp()
        async with app.run_test(headless=True):
            lv = app.query_one("#test-log", LogView)
            original_write = lv.write
            written: list[object] = []

            def _capture(renderable: object) -> None:
                written.append(renderable)
                original_write(renderable)

            lv.write = _capture  # type: ignore[assignment]
            lv.push_event(_event(kind="step", data={"message": "test content"}))
            assert len(written) == 1
            assert isinstance(written[0], Text)
            assert "test content" in written[0].plain

    @pytest.mark.asyncio
    async def test_push_event_with_role_writes_content(self) -> None:
        app = _HostApp()
        async with app.run_test(headless=True):
            lv = app.query_one("#test-log", LogView)
            original_write = lv.write
            written: list[object] = []

            def _capture(renderable: object) -> None:
                written.append(renderable)
                original_write(renderable)

            lv.write = _capture  # type: ignore[assignment]
            lv.push_event(_event(role="builder", data={"message": "building"}))
            assert len(written) == 1
            assert "building" in written[0].plain
            assert "builder" in written[0].plain

    @pytest.mark.asyncio
    async def test_push_event_with_priority_writes_content(self) -> None:
        app = _HostApp()
        async with app.run_test(headless=True):
            lv = app.query_one("#test-log", LogView)
            original_write = lv.write
            written: list[object] = []

            def _capture(renderable: object) -> None:
                written.append(renderable)
                original_write(renderable)

            lv.write = _capture  # type: ignore[assignment]
            lv.push_event(_event(kind="done", priority=True, data={"message": "complete"}))
            assert len(written) == 1
            assert "complete" in written[0].plain
            assert "★" in written[0].plain

    @pytest.mark.asyncio
    async def test_push_message_writes_with_style(self) -> None:
        app = _HostApp()
        async with app.run_test(headless=True):
            lv = app.query_one("#test-log", LogView)
            original_write = lv.write
            written: list[object] = []

            def _capture(renderable: object) -> None:
                written.append(renderable)
                original_write(renderable)

            lv.write = _capture  # type: ignore[assignment]
            lv.push_message("Status update", style="green")
            assert len(written) == 1
            assert isinstance(written[0], Text)
            assert "Status update" in written[0].plain
            assert "green" in str(written[0].style)

    @pytest.mark.asyncio
    async def test_push_message_default_style(self) -> None:
        app = _HostApp()
        async with app.run_test(headless=True):
            lv = app.query_one("#test-log", LogView)
            original_write = lv.write
            written: list[object] = []

            def _capture(renderable: object) -> None:
                written.append(renderable)
                original_write(renderable)

            lv.write = _capture  # type: ignore[assignment]
            lv.push_message("Default styled message")
            assert len(written) == 1
            assert "Default styled message" in written[0].plain

    @pytest.mark.asyncio
    async def test_multiple_push_events_all_written(self) -> None:
        app = _HostApp()
        async with app.run_test(headless=True):
            lv = app.query_one("#test-log", LogView)
            original_write = lv.write
            written: list[object] = []

            def _capture(renderable: object) -> None:
                written.append(renderable)
                original_write(renderable)

            lv.write = _capture  # type: ignore[assignment]
            for i in range(5):
                lv.push_event(_event(data={"message": f"event {i}"}))
            assert len(written) == 5
            for i in range(5):
                assert f"event {i}" in written[i].plain

    @pytest.mark.asyncio
    async def test_all_event_kinds_accepted(self) -> None:
        app = _HostApp()
        async with app.run_test(headless=True):
            lv = app.query_one("#test-log", LogView)
            original_write = lv.write
            written: list[object] = []

            def _capture(renderable: object) -> None:
                written.append(renderable)
                original_write(renderable)

            lv.write = _capture  # type: ignore[assignment]
            kinds = [
                "phase_change",
                "step",
                "stuck",
                "done",
                "error",
                "log",
                "escalation",
            ]
            for kind in kinds:
                lv.push_event(_event(kind=kind, data={"message": f"{kind} event"}))
            assert len(written) == len(kinds)
            for i, kind in enumerate(kinds):
                assert kind in written[i].plain
