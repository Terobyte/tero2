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
import threading
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
        # threading.Lock guards _subscribers so concurrent subscribe/unsubscribe
        # from worker threads cannot mutate the list while emit() iterates it.
        self._emit_lock = threading.Lock()

    def subscribe(self) -> asyncio.Queue[Event]:
        """Register a new consumer and return its dedicated event queue.

        The returned queue has maxsize=500. The caller owns the queue and
        should call unsubscribe() when done to release the reference.
        Acquires _emit_lock to serialise list mutation against emit().
        """
        q: asyncio.Queue[Event] = asyncio.Queue(maxsize=500)
        with self._emit_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        """Remove a previously subscribed queue.

        Drains any pending events so the queue releases references to event
        data immediately. Silently does nothing if the queue is not subscribed.
        Acquires _emit_lock to serialise list mutation against emit().
        """
        while True:
            try:
                q.get_nowait()
            except asyncio.QueueEmpty:
                break
        with self._emit_lock:
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
        with self._emit_lock:
            for q in self._subscribers:
                if not q.full():
                    # Fast path: there is room; put_nowait handles _unfinished_tasks
                    # bookkeeping and wakes up any consumers blocked on an empty queue.
                    try:
                        q.put_nowait(event)
                    except asyncio.QueueFull:
                        pass  # TOCTOU: queue filled between full() check and put_nowait
                    continue

                # Slow path: queue is at or above capacity. Manipulate the internal
                # deque directly so that _unfinished_tasks is never inflated by a
                # get_nowait/put_nowait drain-and-requeue cycle. (get_nowait does not
                # decrement _unfinished_tasks, so re-putting survivors via put_nowait
                # would double-count every re-enqueued item.)
                inner = q._queue  # type: ignore[attr-defined]  # collections.deque

                if event.priority:
                    # Discard the oldest non-priority item to free a slot.
                    # del + append is a net-zero swap: _unfinished_tasks unchanged.
                    for i, existing in enumerate(inner):
                        if not existing.priority:
                            del inner[i]
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
                            dropped = True
                            break
                    if not dropped:
                        continue

                # Swap: append the new event in place of the deleted item.
                # _unfinished_tasks is unchanged — one item in, one item out.
                inner.append(event)
