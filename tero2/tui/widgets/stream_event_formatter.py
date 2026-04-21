"""StreamEventFormatter — converts StreamEvent → rich.Text for TUI display.

Pure function: no I/O, no state, no side effects.

Usage::

    from tero2.tui.widgets.stream_event_formatter import format_event
    text = format_event(event)                    # normal (truncated)
    text = format_event(event, raw_mode=True)     # full content
"""

from __future__ import annotations

from rich.text import Text

from tero2.stream_bus import StreamEvent

# ── colour tables ────────────────────────────────────────────────────────────

# Event kind → style string (Rich markup style syntax)
_KIND_STYLE: dict[str, str] = {
    "text": "yellow",
    "tool_use": "bold green",
    "tool_result": "dim white",
    "thinking": "dim",
    "status": "cyan",
    "error": "bold red",
    "turn_end": "dim cyan",
}

_DEFAULT_KIND_STYLE = "white"

# Role name → colour (matches log_view.py for visual consistency)
_ROLE_COLOUR: dict[str, str] = {
    "scout": "cyan",
    "architect": "blue",
    "builder": "green",
    "coach": "yellow",
    "verifier": "magenta",
    "reviewer": "purple",
    "executor": "white",
}

_DEFAULT_ROLE_COLOUR = "white"

# ── truncation helpers ────────────────────────────────────────────────────────

_TOOL_OUTPUT_MAX_LINES = 2


def _truncate_tool_output(output: str) -> str:
    """Keep first *_TOOL_OUTPUT_MAX_LINES* lines; append byte-count suffix."""
    lines = output.splitlines()
    if len(lines) <= _TOOL_OUTPUT_MAX_LINES:
        return output
    head = "\n".join(lines[:_TOOL_OUTPUT_MAX_LINES])
    remaining_bytes = len(output.encode()) - len(head.encode())
    return f"{head}\n… +{remaining_bytes} bytes"


def _collapse_thinking(content: str) -> str:
    """Collapse thinking content to a single badge line."""
    char_count = len(content)
    return f"💭 thinking… ({char_count} chars)"


# ── public API ────────────────────────────────────────────────────────────────

def format_event(event: StreamEvent, *, raw_mode: bool = False) -> Text:
    """Convert a *StreamEvent* into a styled ``rich.Text`` object.

    In normal mode (*raw_mode=False*):
    - ``tool_output`` is truncated to the first 2 lines with a byte-count suffix.
    - ``thinking`` content is collapsed to ``💭 thinking… (N chars)``.
    - ``text`` content is shown in full.

    In raw mode (*raw_mode=True*):
    - All content shown without truncation or collapsing.

    The role name (if present) is prepended in its role colour.
    Timestamp is prepended in dim style.
    """
    kind_style = _KIND_STYLE.get(event.kind, _DEFAULT_KIND_STYLE)
    role_colour = _ROLE_COLOUR.get(event.role, _DEFAULT_ROLE_COLOUR)

    result = Text()

    # Timestamp prefix (dim)
    ts = event.timestamp.strftime("%H:%M:%S")
    result.append(f"[{ts}] ", style="dim")

    # Role prefix (role colour, bracketed)
    if event.role:
        result.append(f"[{event.role}] ", style=role_colour)

    # Kind-specific body
    if event.kind == "tool_use":
        result.append(f"⚙ {event.tool_name}", style=kind_style)
        if event.tool_args and raw_mode:
            import json
            args_str = json.dumps(event.tool_args, ensure_ascii=False)
            result.append(f"  {args_str}", style="dim")

    elif event.kind == "tool_result":
        output = event.tool_output
        if not raw_mode:
            output = _truncate_tool_output(output)
        if output:
            # indent each line for visual grouping under its tool_use
            indented = "\n  ".join(output.splitlines())
            result.append(f"  {indented}", style=kind_style)
        else:
            result.append("  (empty)", style="dim")

    elif event.kind == "thinking":
        if raw_mode:
            result.append(event.content, style=kind_style)
        else:
            result.append(_collapse_thinking(event.content), style=kind_style)

    elif event.kind == "turn_end":
        result.append("── turn end ──", style=kind_style)

    elif event.kind == "error":
        result.append(f"✗ {event.content}", style=kind_style)

    elif event.kind == "status":
        result.append(event.content, style=kind_style)

    else:
        # text and fallback
        result.append(event.content, style=kind_style)

    return result
