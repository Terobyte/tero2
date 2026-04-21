"""HeartbeatSidebar — 7 mini-cells showing per-role vital signs.

One cell per SORA role (scout → executor). Each cell shows:
- status dot: 🟢 running  🟡 async  ⚪ idle  🔴 error  ✓ done
- role name
- elapsed seconds since first event
- tool call count
- last text or tool name (one-line preview)

The sidebar is updated by the DashboardApp's stream consumer via
``on_stream_event(event)`` and ``on_phase_event(event)``.

Clicking a cell emits a ``RolePinRequest`` message that the app uses to
pin the active role in the stream panel.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from tero2.stream_bus import StreamEvent

# ── role ordering ─────────────────────────────────────────────────────────────

#: Display order (top → bottom) used for hotkey mapping 1–7.
SIDEBAR_ROLE_ORDER: list[str] = [
    "scout",
    "architect",
    "builder",
    "coach",
    "verifier",
    "reviewer",
    "executor",
]

# ── status dot glyphs ─────────────────────────────────────────────────────────

_STATUS_DOT: dict[str, str] = {
    "idle": "⚪",
    "running": "🟢",
    "async": "🟡",
    "error": "🔴",
    "done": "✓",
}


# ── per-role state ────────────────────────────────────────────────────────────

@dataclass
class RoleMetrics:
    """Aggregated vital signs for a single SORA role.

    Updated by ``HeartbeatSidebar.on_stream_event()`` as stream events arrive.
    """

    status: str = "idle"          # "idle" | "running" | "async" | "error" | "done"
    elapsed_s: float = 0.0        # seconds since the first event
    tool_count: int = 0           # number of tool_use events received
    last_line: str = ""           # last text content or tool name (one-line preview)
    provider: str = ""            # e.g. "claude", "zai"
    model: str = ""               # e.g. "claude-sonnet-4-6"
    started_at: datetime | None = None   # timestamp of first event (for elapsed calc)


# ── per-cell widget ───────────────────────────────────────────────────────────

class _RoleCell(Static):
    """Single role mini-cell inside HeartbeatSidebar."""

    DEFAULT_CSS: ClassVar[str] = """
    _RoleCell {
        height: 4;
        border: solid $panel;
        padding: 0 1;
    }
    _RoleCell.active {
        border: solid $accent;
        background: $boost;
    }
    """

    def __init__(self, role: str, **kwargs: object) -> None:
        super().__init__("", **kwargs)
        self._role = role
        self._metrics = RoleMetrics()

    def refresh_metrics(self, metrics: RoleMetrics) -> None:
        """Re-render the cell with updated *metrics*."""
        self._metrics = metrics
        self._render_cell()

    def _render_cell(self) -> None:
        m = self._metrics
        dot = _STATUS_DOT.get(m.status, "⚪")
        elapsed = f"{m.elapsed_s:.0f}s" if m.elapsed_s >= 1 else ""
        tools = f"{m.tool_count}t" if m.tool_count else ""
        meta = "  ".join(filter(None, [elapsed, tools]))
        last = m.last_line[:22] + "…" if len(m.last_line) > 22 else m.last_line

        lines = [
            f"{dot} {self._role}",
            meta or "—",
            last or "—",
        ]
        self.update("\n".join(lines))


# ── sidebar ───────────────────────────────────────────────────────────────────

class HeartbeatSidebar(Widget):
    """7 mini-cells, one per SORA role, showing live vital signs.

    Updated by calling ``on_stream_event(event)`` with StreamEvents from the
    StreamBus and ``on_phase_event(event)`` with Events from the EventDispatcher
    (for done/error/phase_change status transitions).

    Emits ``RolePinRequest(role)`` when a cell is clicked.
    """

    DEFAULT_CSS: ClassVar[str] = """
    HeartbeatSidebar {
        width: 26;
        height: 1fr;
        border: solid $accent;
        padding: 0;
    }
    HeartbeatSidebar #sidebar-title {
        height: 1;
        color: $text-muted;
        content-align: center middle;
    }
    """

    # ── messages ──────────────────────────────────────────────────────────────

    class RolePinRequest(Message):
        """Emitted when the user clicks a role cell."""

        def __init__(self, role: str) -> None:
            super().__init__()
            self.role = role

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._metrics: dict[str, RoleMetrics] = {
            role: RoleMetrics() for role in SIDEBAR_ROLE_ORDER
        }
        self._cells: dict[str, _RoleCell] = {}

    def compose(self) -> ComposeResult:
        yield Static("● roles", id="sidebar-title")
        for role in SIDEBAR_ROLE_ORDER:
            cell = _RoleCell(role, id=f"cell-{role}")
            self._cells[role] = cell
            yield cell

    # ── public update API ─────────────────────────────────────────────────────

    def on_stream_event(self, event: StreamEvent) -> None:
        """Update per-role metrics when a StreamEvent arrives from StreamBus."""
        role = event.role
        if role not in self._metrics:
            return

        m = self._metrics[role]

        # Record first-seen timestamp for elapsed calculation
        if m.started_at is None:
            m.started_at = event.timestamp
            m.status = "running"

        # Update elapsed (seconds from started_at to this event's timestamp)
        m.elapsed_s = (event.timestamp - m.started_at).total_seconds()

        if event.kind == "tool_use":
            m.tool_count += 1
            m.last_line = f"⚙ {event.tool_name}"
        elif event.kind == "text" and event.content:
            # One-line preview: use first non-empty line
            first_line = event.content.splitlines()[0] if event.content else ""
            m.last_line = first_line
        elif event.kind == "error":
            m.status = "error"
            m.last_line = event.content[:30] if event.content else "error"
        elif event.kind == "turn_end":
            m.status = "done"

        self._refresh_cell(role)

    def on_phase_event(self, event: object) -> None:
        """Update role status from coarse EventDispatcher events.

        Accepts any object with ``kind`` and ``role`` attributes — matches
        the ``Event`` dataclass from ``tero2.events``.
        """
        kind = getattr(event, "kind", None)
        role = getattr(event, "role", "")

        if role and role in self._metrics:
            if kind == "done":
                self._metrics[role].status = "done"
                self._refresh_cell(role)
            elif kind == "error":
                self._metrics[role].status = "error"
                self._refresh_cell(role)
            elif kind == "phase_change":
                # Mark role as running when a phase activates it
                if self._metrics[role].status == "idle":
                    self._metrics[role].status = "running"
                    self._refresh_cell(role)

    def get_metrics(self, role: str) -> RoleMetrics | None:
        """Return current metrics for *role*, or None if role is unknown."""
        return self._metrics.get(role)

    # ── cell click → pin request ──────────────────────────────────────────────

    def on__role_cell_click(self, message: Message) -> None:
        """Bubble up a RolePinRequest when a cell is clicked."""
        # Textual routes click events via CSS; we handle via on_click on cells.
        # This is a placeholder — actual click routing is wired in DashboardApp.
        pass

    # ── internal ──────────────────────────────────────────────────────────────

    def _refresh_cell(self, role: str) -> None:
        """Push latest metrics to the visual cell."""
        cell = self._cells.get(role)
        if cell is not None:
            cell.refresh_metrics(self._metrics[role])
