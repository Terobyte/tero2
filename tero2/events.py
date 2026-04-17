"""Event Bus — Runner ↔ consumer communication.

Two-way communication channel:
    Events (Runner → consumers): Runner emits status/progress events.
    Commands (consumers → Runner): TUI/Telegram sends control commands.

EventDispatcher provides fan-out: all subscribers receive every event.
Each subscriber gets its own asyncio.Queue — consumers drain independently.

Event kinds (Runner emits):
    "phase_change"   — SORA phase transition       priority=True
    "step"           — step within a task           priority=False
    "stuck"          — runner is stuck              priority=True
    "done"           — plan completed               priority=True
    "error"          — non-recoverable error        priority=True
    "provider_switch"— provider changed             priority=True
    "usage_update"   — usage data refreshed         priority=False
    "escalation"     — escalation level changed     priority=True
    "log"            — general log message          priority=False

Command kinds (consumers send):
    "switch_provider" — data: {"role": "builder", "provider": "claude"}
    "skip_task"       — data: {}
    "steer"           — data: {"text": "..."}
    "pause"           — data: {}
    "resume"          — data: {}
    "stop"            — data: {}
    "retry"           — data: {}
    "new_plan"        — data: {"text": "plan text or file path"}
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Event:
    """A status/progress event emitted by the Runner to consumers.

    Args:
        timestamp: UTC time the event was created.
        kind: Event type string (e.g. "phase_change", "step", "stuck").
        role: The role that generated this event ("builder", "scout", etc.)
            or "" for system-level events.
        data: Event-specific payload dict (schema varies by kind).
        priority: When True, the event is never dropped by the dispatcher
            even when a subscriber queue is full.
    """

    timestamp: datetime
    kind: str
    role: str
    data: dict = field(default_factory=dict)
    priority: bool = False


@dataclass
class Command:
    """A control command sent by a consumer (TUI, Telegram) to the Runner.

    Args:
        kind: Command type string (e.g. "pause", "stop", "steer").
        data: Command-specific payload dict (schema varies by kind).
        source: Origin of the command — "tui", "telegram", or "system".
            Used for logging and debugging; Runner behaviour is the same
            regardless of source.
    """

    kind: str
    data: dict = field(default_factory=dict)
    source: str = ""


def make_event(
    kind: str, role: str = "", data: dict | None = None, *, priority: bool = False
) -> Event:
    """Convenience factory: create an Event with the current UTC timestamp."""
    return Event(
        timestamp=datetime.now(timezone.utc),
        kind=kind,
        role=role,
        data=data if data is not None else {},
        priority=priority,
    )


class EventDispatcher:
    """Fan-out dispatcher. Runner calls emit(); consumers call subscribe().

    Each subscriber gets its own asyncio.Queue[Event]. emit() copies the
    event to every queue so all consumers see every event independently.

    Backpressure:
        Queues are created with maxsize=500. When a queue is full:
        - Priority events are NEVER dropped. The oldest NON-priority item
          is discarded first. If all 500 items are priority events, the
          queue grows by one (intentional one-item overflow) — bounded in
          practice because high-volume non-priority events normally dominate.
        - Non-priority event: the oldest NON-priority item is discarded.
          If every queued item is priority, the incoming non-priority event
          is silently dropped (priority events are always protected).

    emit() never blocks the caller (no await on queue operations).
    """

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[Event]] = []
        self._emit_lock = asyncio.Lock()

    def subscribe(self) -> asyncio.Queue[Event]:
        """Register a new consumer and return its dedicated event queue.

        The returned queue has maxsize=500. The caller owns the queue and
        should call unsubscribe() when done to release the reference.
        """
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        """Remove a previously subscribed queue.

        Silently does nothing if the queue is not currently subscribed.
        """
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def emit(self, event: Event) -> None:
        """Copy event to all subscriber queues.

        Priority events (done, error, stuck, escalation, phase_change,
        provider_switch) are never dropped. If the queue is full and the
        event is priority, the oldest NON-priority event is discarded to
        make room. If all 500 items are priority events, the queue grows
        by one (unbounded in that extreme case).

        Non-priority events (step, log, usage_update): oldest NON-priority
        event discarded if full. If every queued item is priority the
        incoming non-priority event is silently dropped.

        Never blocks the Runner.
        """
        async with self._emit_lock:
            for q in self._subscribers:
                if not q.full():
                    # Fast path: there is room; put_nowait handles _unfinished_tasks
                    # bookkeeping and wakes up any consumers blocked on an empty queue.
                    q.put_nowait(event)
                    continue

                # Slow path: queue is at or above capacity. Manipulate the internal
                # deque directly so that _unfinished_tasks is never inflated by a
                # get_nowait/put_nowait drain-and-requeue cycle. (get_nowait does not
                # decrement _unfinished_tasks, so re-putting survivors via put_nowait
                # would double-count every re-enqueued item.)
                inner = q._queue  # type: ignore[attr-defined]  # collections.deque

                if event.priority:
                    # Discard the oldest non-priority item to free a slot.
                    for i, existing in enumerate(inner):
                        if not existing.priority:
                            del inner[i]
                            q._unfinished_tasks -= 1  # type: ignore[attr-defined]
                            break
                    else:
                        # Every slot holds a priority event — intentional one-item
                        # overflow; bounded in practice (high-volume non-priority
                        # events normally dominate the queue).
                        inner.append(event)
                        q._unfinished_tasks += 1  # type: ignore[attr-defined]
                        continue  # skip the append below
                else:
                    # Non-priority: discard the oldest non-priority item.
                    # If all items are priority, silently drop the incoming event.
                    dropped = False
                    for i, existing in enumerate(inner):
                        if not existing.priority:
                            del inner[i]
                            q._unfinished_tasks -= 1  # type: ignore[attr-defined]
                            dropped = True
                            break
                    if not dropped:
                        continue

                # Enqueue the new event without going through put_nowait so that a
                # prior one-item overflow (qsize == maxsize + 1) never triggers
                # QueueFull: after popping one item the deque still has maxsize items
                # and put_nowait would see full() == True and raise.
                inner.append(event)
                q._unfinished_tasks += 1  # type: ignore[attr-defined]
