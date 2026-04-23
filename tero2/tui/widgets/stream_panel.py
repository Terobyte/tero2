"""Main panel showing the live stream of the currently-active role.

One :class:`RichLog` subclass with per-role ring buffers. Visible content
follows ``active_role``, which is recomputed on every incoming event using
priority + recency. Pin (``pinned_role``) overrides auto-switch. ``raw_mode``
re-renders the active buffer with full tool output / thinking.

Method naming:
    ``push_stream_event`` — call site: anywhere that wants to feed an event
    into the panel. Deliberately avoids the ``on_*`` prefix so Textual's
    message-routing machinery does not hijack the call.
"""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from textual.reactive import reactive
from textual.widgets import RichLog

from tero2.stream_bus import StreamEvent
from tero2.tui.widgets.stream_event_formatter import format_event

_PRIORITY: dict[str, int] = {
    "builder": 100,
    "verifier": 90,
    "architect": 80,
    "scout": 70,
    "reviewer": 60,
    "coach": 50,
    "executor": 40,
}
_ACTIVE_WINDOW_S = 5.0
_PER_ROLE_BUFFER = 500


class RoleStreamPanel(RichLog):
    """Priority-aware, pin-able, raw-mode-toggleable stream viewer."""

    active_role: reactive[str] = reactive("")
    pinned_role: reactive[str | None] = reactive(None)
    raw_mode: reactive[bool] = reactive(False)

    _MAX_ROLES = 50

    DEFAULT_CSS = """
    RoleStreamPanel {
        border: round $accent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(markup=False, highlight=False, wrap=True, **kwargs)
        self._buffers: dict[str, deque[StreamEvent]] = {}
        self._last_seen: dict[str, datetime] = {}

    def push_stream_event(self, ev: StreamEvent) -> None:
        if ev.role not in self._buffers:
            if len(self._buffers) >= self._MAX_ROLES:
                oldest = min(self._last_seen, key=self._last_seen.get)
                del self._buffers[oldest]
                del self._last_seen[oldest]
            self._buffers[ev.role] = deque(maxlen=_PER_ROLE_BUFFER)
        self._buffers[ev.role].append(ev)
        self._last_seen[ev.role] = ev.timestamp
        prior_active = self.active_role
        self._recompute_active_role()
        if ev.role == self.active_role and self.active_role == prior_active:
            self.write(format_event(ev, raw_mode=self.raw_mode))

    def watch_active_role(self, old: str, new: str) -> None:
        if old == new:
            return
        self.clear()
        for ev in self._buffers.get(new, ()):
            self.write(format_event(ev, raw_mode=self.raw_mode))

    def watch_raw_mode(self, old: bool, new: bool) -> None:
        if old == new:
            return
        self.clear()
        for ev in self._buffers.get(self.active_role, ()):
            self.write(format_event(ev, raw_mode=new))

    def watch_pinned_role(self, old: str | None, new: str | None) -> None:
        if old == new:
            return
        self._recompute_active_role()

    def _recompute_active_role(self) -> None:
        if self.pinned_role:
            self.active_role = self.pinned_role
            return
        if not self._last_seen:
            return
        now = datetime.now(timezone.utc)
        candidates = [
            r for r, ts in self._last_seen.items()
            if (now - ts).total_seconds() < _ACTIVE_WINDOW_S
        ]
        if not candidates:
            self.active_role = max(self._last_seen, key=self._last_seen.get)
            return
        self.active_role = max(candidates, key=lambda r: _PRIORITY.get(r, 0))

    def _render_active_buffer_plain(self) -> str:
        parts = []
        for ev in self._buffers.get(self.active_role, ()):
            parts.append(format_event(ev, raw_mode=self.raw_mode).plain)
        return "\n".join(parts)
