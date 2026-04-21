"""Tests for tero2.tui.widgets.stream_event_formatter — format_event() pure function.

Coverage:
- Each event kind produces styled rich.Text
- Role colour prefix present when role is set
- Truncation: tool_output capped at 2 lines with byte-count suffix (normal mode)
- Truncation: thinking collapsed to badge (normal mode)
- raw_mode=True bypasses truncation and shows full content
- tool_use shows tool name and (in raw mode) tool_args
- error uses bold-red style; content prefixed with ✗
- turn_end renders separator line
- Unknown kind falls back to default style
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from rich.text import Text

from tero2.stream_bus import StreamEvent, make_stream_event
from tero2.tui.widgets.stream_event_formatter import format_event


# ── helpers ──────────────────────────────────────────────────────────────────


def _ts() -> datetime:
    return datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)


def _ev(
    role: str = "builder",
    kind: str = "text",
    *,
    content: str = "",
    tool_name: str = "",
    tool_args: dict | None = None,
    tool_output: str = "",
    tool_id: str = "",
    raw: dict | None = None,
) -> StreamEvent:
    return StreamEvent(
        role=role,
        kind=kind,  # type: ignore[arg-type]
        timestamp=_ts(),
        content=content,
        tool_name=tool_name,
        tool_args=tool_args or {},
        tool_output=tool_output,
        tool_id=tool_id,
        raw=raw or {},
    )


def _plain(text: Text) -> str:
    """Strip rich styles; return the raw string content."""
    return text.plain


# ── return type ──────────────────────────────────────────────────────────────


class TestReturnType:
    def test_returns_rich_text(self) -> None:
        result = format_event(_ev())
        assert isinstance(result, Text)

    def test_timestamp_in_output(self) -> None:
        result = format_event(_ev())
        assert "12:00:00" in _plain(result)

    def test_role_in_output_when_set(self) -> None:
        result = format_event(_ev(role="builder"))
        assert "builder" in _plain(result)

    def test_no_role_prefix_when_role_empty(self) -> None:
        e = _ev(role="")
        result = format_event(e)
        plain = _plain(result)
        # No "[" before a role name — just the timestamp
        # We don't expect any role bracket for empty string
        assert "[]" not in plain


# ── kind=text ────────────────────────────────────────────────────────────────


class TestKindText:
    def test_content_shown(self) -> None:
        e = _ev(kind="text", content="Hello world")
        assert "Hello world" in _plain(format_event(e))

    def test_no_truncation_in_normal_mode(self) -> None:
        long_text = "line\n" * 20
        e = _ev(kind="text", content=long_text)
        result = _plain(format_event(e))
        assert "bytes" not in result   # text kind is never truncated

    def test_empty_content_does_not_crash(self) -> None:
        e = _ev(kind="text", content="")
        format_event(e)   # must not raise


# ── kind=tool_use ─────────────────────────────────────────────────────────────


class TestKindToolUse:
    def test_tool_name_shown(self) -> None:
        e = _ev(kind="tool_use", tool_name="bash")
        assert "bash" in _plain(format_event(e))

    def test_glyph_present(self) -> None:
        e = _ev(kind="tool_use", tool_name="read_file")
        assert "⚙" in _plain(format_event(e))

    def test_args_hidden_in_normal_mode(self) -> None:
        e = _ev(kind="tool_use", tool_name="write", tool_args={"path": "/tmp/x", "content": "y"})
        plain = _plain(format_event(e, raw_mode=False))
        assert "path" not in plain   # args suppressed in normal mode

    def test_args_shown_in_raw_mode(self) -> None:
        e = _ev(kind="tool_use", tool_name="write", tool_args={"path": "/tmp/x"})
        plain = _plain(format_event(e, raw_mode=True))
        assert "path" in plain


# ── kind=tool_result ─────────────────────────────────────────────────────────


class TestKindToolResult:
    def test_output_shown(self) -> None:
        e = _ev(kind="tool_result", tool_output="line1\nline2")
        assert "line1" in _plain(format_event(e))

    def test_truncated_to_two_lines_in_normal_mode(self) -> None:
        output = "line1\nline2\nline3\nline4\nline5"
        e = _ev(kind="tool_result", tool_output=output)
        plain = _plain(format_event(e, raw_mode=False))
        assert "line1" in plain
        assert "line2" in plain
        assert "line3" not in plain, "third line must be truncated"
        assert "bytes" in plain, "byte-count suffix must be present"

    def test_no_truncation_in_raw_mode(self) -> None:
        output = "line1\nline2\nline3\nline4\nline5"
        e = _ev(kind="tool_result", tool_output=output)
        plain = _plain(format_event(e, raw_mode=True))
        assert "line3" in plain
        assert "bytes" not in plain

    def test_exactly_two_lines_not_truncated(self) -> None:
        output = "line1\nline2"
        e = _ev(kind="tool_result", tool_output=output)
        plain = _plain(format_event(e, raw_mode=False))
        assert "line1" in plain
        assert "line2" in plain
        assert "bytes" not in plain

    def test_empty_output_shows_placeholder(self) -> None:
        e = _ev(kind="tool_result", tool_output="")
        plain = _plain(format_event(e))
        assert "empty" in plain

    def test_single_line_not_truncated(self) -> None:
        e = _ev(kind="tool_result", tool_output="just one line")
        plain = _plain(format_event(e))
        assert "bytes" not in plain
        assert "just one line" in plain


# ── kind=thinking ─────────────────────────────────────────────────────────────


class TestKindThinking:
    def test_collapsed_in_normal_mode(self) -> None:
        e = _ev(kind="thinking", content="I am thinking about this carefully...")
        plain = _plain(format_event(e, raw_mode=False))
        assert "thinking" in plain.lower()
        assert "chars" in plain

    def test_badge_contains_char_count(self) -> None:
        content = "X" * 100
        e = _ev(kind="thinking", content=content)
        plain = _plain(format_event(e, raw_mode=False))
        assert "100" in plain

    def test_full_content_in_raw_mode(self) -> None:
        content = "Deep chain-of-thought reasoning here."
        e = _ev(kind="thinking", content=content)
        plain = _plain(format_event(e, raw_mode=True))
        assert content in plain

    def test_thinking_badge_glyph(self) -> None:
        e = _ev(kind="thinking", content="abc")
        plain = _plain(format_event(e, raw_mode=False))
        assert "💭" in plain


# ── kind=status ───────────────────────────────────────────────────────────────


class TestKindStatus:
    def test_content_shown(self) -> None:
        e = _ev(kind="status", content="starting up")
        assert "starting up" in _plain(format_event(e))

    def test_empty_content_does_not_crash(self) -> None:
        format_event(_ev(kind="status", content=""))


# ── kind=error ────────────────────────────────────────────────────────────────


class TestKindError:
    def test_content_shown(self) -> None:
        e = _ev(kind="error", content="rate limit exceeded")
        assert "rate limit exceeded" in _plain(format_event(e))

    def test_error_glyph_present(self) -> None:
        e = _ev(kind="error", content="oops")
        assert "✗" in _plain(format_event(e))


# ── kind=turn_end ─────────────────────────────────────────────────────────────


class TestKindTurnEnd:
    def test_separator_shown(self) -> None:
        e = _ev(kind="turn_end")
        plain = _plain(format_event(e))
        # Should have some separator-like content
        assert "turn end" in plain.lower() or "──" in plain


# ── colour mapping ────────────────────────────────────────────────────────────


class TestColourMapping:
    """Verify that styled spans are present for known roles and kinds."""

    def _has_style_span(self, text: Text, style: str) -> bool:
        """Return True if any span in *text* contains *style*."""
        for span in text._spans:
            if style in str(span.style):
                return True
        return False

    def test_text_kind_has_yellow(self) -> None:
        e = _ev(kind="text", content="hi")
        result = format_event(e)
        assert self._has_style_span(result, "yellow")

    def test_tool_use_kind_has_green(self) -> None:
        e = _ev(kind="tool_use", tool_name="bash")
        result = format_event(e)
        assert self._has_style_span(result, "green")

    def test_error_kind_has_red(self) -> None:
        e = _ev(kind="error", content="fail")
        result = format_event(e)
        assert self._has_style_span(result, "red")

    def test_status_kind_has_cyan(self) -> None:
        e = _ev(kind="status", content="ok")
        result = format_event(e)
        assert self._has_style_span(result, "cyan")

    def test_builder_role_has_green_prefix(self) -> None:
        e = _ev(role="builder", kind="text", content="x")
        result = format_event(e)
        assert self._has_style_span(result, "green")

    def test_scout_role_has_cyan_prefix(self) -> None:
        e = _ev(role="scout", kind="text", content="x")
        result = format_event(e)
        assert self._has_style_span(result, "cyan")

    def test_unknown_role_does_not_crash(self) -> None:
        e = _ev(role="unknown_role_xyz", kind="text", content="x")
        format_event(e)   # must not raise


# ── raw_mode default ──────────────────────────────────────────────────────────


class TestRawModeDefault:
    def test_default_is_normal_mode(self) -> None:
        """format_event(event) behaves like format_event(event, raw_mode=False)."""
        long_output = "\n".join(f"line{i}" for i in range(10))
        e = _ev(kind="tool_result", tool_output=long_output)
        default = _plain(format_event(e))
        explicit_false = _plain(format_event(e, raw_mode=False))
        assert default == explicit_false

    def test_raw_mode_true_differs_for_truncatable_content(self) -> None:
        long_output = "\n".join(f"line{i}" for i in range(10))
        e = _ev(kind="tool_result", tool_output=long_output)
        normal = _plain(format_event(e, raw_mode=False))
        raw = _plain(format_event(e, raw_mode=True))
        assert normal != raw
