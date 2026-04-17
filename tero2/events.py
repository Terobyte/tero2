"""Event bus for tero2 — Event/Command dataclasses + EventDispatcher."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class Event:
    """An event emitted by an agent component."""

    timestamp: datetime
    kind: str  # "phase_change", "step", "stuck", "done", "error", "log", "escalation", …
    role: str
    data: dict[str, Any]
    priority: bool = False

    @classmethod
    def now(
        cls,
        kind: str,
        role: str = "",
        data: dict[str, Any] | None = None,
        priority: bool = False,
    ) -> Event:
        return cls(
            timestamp=datetime.now(timezone.utc),
            kind=kind,
            role=role,
            data=data or {},
            priority=priority,
        )


@dataclass
class Command:
    """A command sent from the TUI or Telegram to the runner."""

    kind: str  # "pause", "resume", "skip", "rollback", "escalate", "option", …
    data: dict[str, Any] = field(default_factory=dict)
    source: str = ""  # "tui", "telegram", …


_DEFAULT_QUEUE_MAXSIZE = 256


class EventDispatcher:
    """Fan-out event bus.

    Subscribers get their own ``asyncio.Queue``. When a subscriber's queue is
    full, the oldest *non-priority* event is dropped to make room.  Priority
    events are never dropped.
    """

    def __init__(self, maxsize: int = _DEFAULT_QUEUE_MAXSIZE) -> None:
        self._maxsize = maxsize
        self._subscribers: list[asyncio.Queue[Event]] = []

    # ── subscribe / unsubscribe ─────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[Event]:
        """Create and register a new subscriber queue."""
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, queue: asyncio.Queue[Event]) -> None:
        """Remove a subscriber queue (no-op if not registered)."""
        try:
            self._subscribers.remove(queue)
        except ValueError:
            pass

    # ── emit ────────────────────────────────────────────────────────────

    def emit(self, event: Event) -> None:
        """Deliver *event* to all subscriber queues.

        If a queue is full:
        - Priority events: drop the oldest non-priority event to make room.
          If there is no non-priority event to drop, the priority event is
          still delivered by dropping the oldest event unconditionally.
        - Non-priority events: silently dropped.
        """
        for q in self._subscribers:
            if not q.full():
                q.put_nowait(event)
                continue

            if not event.priority:
                # non-priority dropped
                continue

            # priority: make room by removing oldest non-priority item
            items: list[Event] = []
            while not q.empty():
                items.append(q.get_nowait())

            # find first non-priority to drop
            drop_idx: int | None = None
            for i, item in enumerate(items):
                if not item.priority:
                    drop_idx = i
                    break

            if drop_idx is not None:
                items.pop(drop_idx)
            else:
                # all items are priority — drop oldest anyway
                items.pop(0)

            items.append(event)
            for item in items:
                q.put_nowait(item)
