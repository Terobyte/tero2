"""Log panel widget — scrollable RichLog that formats tero2 Event objects."""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.widgets import RichLog

from tero2.events import Event

# ── event kind → display colour ─────────────────────────────────────────────

_KIND_COLOUR: dict[str, str] = {
    "phase_change": "cyan",
    "step": "green",
    "stuck": "red bold",
    "done": "green bold",
    "error": "red",
    "log": "white",
    "escalation": "magenta",
}

_DEFAULT_COLOUR = "white"

# ── role → display colour ────────────────────────────────────────────────────

_ROLE_COLOUR: dict[str, str] = {
    "scout": "cyan",
    "coach": "yellow",
    "architect": "blue",
    "execute": "green",
    "verifier": "magenta",
}

_DEFAULT_ROLE_COLOUR = "white"


def _format_event(event: Event) -> Text:
    """Convert an Event into a styled Rich Text line."""
    ts = event.timestamp.strftime("%H:%M:%S")
    kind_colour = _KIND_COLOUR.get(event.kind, _DEFAULT_COLOUR)
    role_colour = _ROLE_COLOUR.get(event.role, _DEFAULT_ROLE_COLOUR)

    text = Text()
    text.append(f"[{ts}] ", style="dim")
    text.append(f"{event.kind:<14}", style=kind_colour)

    if event.role:
        text.append(f" [{event.role}]", style=role_colour)

    # append a short summary from data if available
    msg = event.data.get("message") or event.data.get("msg") or event.data.get("text")
    if msg:
        text.append(f"  {msg}", style="default")
    elif event.data:
        # fallback: show first key=value pair
        first_key = next(iter(event.data))
        text.append(f"  {first_key}={event.data[first_key]!r}", style="dim")

    if event.priority:
        text.append("  ★", style="yellow")

    return text


class LogView(RichLog):
    """Scrollable log panel for tero2 events.

    Usage::

        log_view = LogView()
        log_view.push_event(some_event)
    """

    DEFAULT_CSS: ClassVar[str] = """
    LogView {
        border: solid $accent;
        height: 1fr;
    }
    """

    def __init__(self, max_lines: int = 500, **kwargs: object) -> None:
        super().__init__(max_lines=max_lines, markup=False, highlight=True, **kwargs)  # type: ignore[call-arg]

    # ── public API ───────────────────────────────────────────────────────────

    def push_event(self, event: Event) -> None:
        """Format *event* and append it to the log."""
        self.write(_format_event(event))

    def push_message(self, message: str, style: str = "white") -> None:
        """Append a plain text message (no Event object required)."""
        text = Text(message, style=style)
        self.write(text)
