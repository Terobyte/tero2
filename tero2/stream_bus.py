"""StreamBus — fan-out dispatcher for live agent stream content.

Parallel to EventDispatcher but tuned for higher volume:
- maxsize=2000 per subscriber (vs 500 for EventDispatcher)
- ring-buffer semantics: drop oldest on full (no priority concept)
- publish() is SYNC — safe to call in a tight async-for loop
- no Telegram subscription (stream volume would spam)

Usage::

    bus = StreamBus()
    q = bus.subscribe()
    bus.publish(make_stream_event("builder", "tool_use", tool_name="bash"))
    event = await q.get()
    bus.unsubscribe(q)
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

StreamEventKind = Literal[
    "text",       # agent narration
    "tool_use",   # tool invocation
    "tool_result",  # tool result
    "thinking",   # chain-of-thought block
    "status",     # start / end / turn_boundary marker
    "error",      # stream or parse error
    "turn_end",   # CLIProvider emits after proc.wait() completes
]


# ── StreamEvent dataclass ────────────────────────────────────────────────────


@dataclass
class StreamEvent:
    """Normalized stream event from an agent CLI.

    Produced by per-provider normalizers, published via StreamBus, and
    consumed by TUI widgets (RoleStreamPanel, HeartbeatSidebar).

    Fields:
        role:        SORA role ("builder", "scout", …) or "" for system events.
        kind:        Event type — see Literal values.
        timestamp:   UTC time the event was created.
        content:     Free-form text for text/thinking/status/error kinds.
        tool_name:   Name of the tool (tool_use and tool_result kinds).
        tool_args:   Tool input dict (tool_use kind).
        tool_output: Full tool output string (tool_result kind — NOT truncated).
        tool_id:     Matching token for tool_use ↔ tool_result pairing.
        raw:         Original dict from the provider (preserved for raw-mode).
    """

    role: str
    kind: StreamEventKind
    timestamp: datetime
    content: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    tool_output: str = ""
    tool_id: str = ""
    raw: dict = field(default_factory=dict)


def make_stream_event(
    role: str,
    kind: StreamEventKind,
    *,
    timestamp: datetime | None = None,
    content: str = "",
    tool_name: str = "",
    tool_args: dict | None = None,
    tool_output: str = "",
    tool_id: str = "",
    raw: dict | None = None,
) -> StreamEvent:
    """Factory with ``datetime.now(timezone.utc)`` as default timestamp."""
    return StreamEvent(
        role=role,
        kind=kind,
        timestamp=timestamp or datetime.now(timezone.utc),
        content=content,
        tool_name=tool_name,
        tool_args=tool_args or {},
        tool_output=tool_output,
        tool_id=tool_id,
        raw=raw or {},
    )


# ── StreamBus ────────────────────────────────────────────────────────────────


class StreamBus:
    """Fan-out dispatcher for agent stream content.

    Design notes:
    - Each subscriber gets its own ``asyncio.Queue`` with maxsize=*max_queue_size*.
    - When a subscriber queue is full, the **oldest** item is dropped (ring-buffer).
    - ``publish()`` is **synchronous** — safe to call inside a tight async loop
      without yielding control back to the event loop.
    - One bad subscriber (e.g. a cancelled widget) must not affect others;
      exceptions in ``put_nowait`` are swallowed per-subscriber.
    - Cross-thread publish is supported via ``loop.call_soon_threadsafe``, but
      in normal tero2 operation all publish sites run on the main asyncio loop.
    """

    def __init__(self, max_queue_size: int = 2000) -> None:
        self._subscribers: list[asyncio.Queue[StreamEvent]] = []
        self._max = max_queue_size
        self._loop: asyncio.AbstractEventLoop | None = None
        # threading.Lock guards _subscribers so subscribe/unsubscribe cannot
        # mutate the list while _publish_impl iterates it.
        self._sub_lock = threading.Lock()

    # ── subscriber management ────────────────────────────────────────────────

    def subscribe(self) -> asyncio.Queue[StreamEvent]:
        """Register a new consumer and return its dedicated queue.

        The caller owns the queue and must call ``unsubscribe()`` when done to
        release the reference and stop receiving events. Acquires the
        subscriber lock to serialise list mutation against publish.
        """
        q: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=self._max)
        with self._sub_lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[StreamEvent]) -> None:
        """Remove a previously subscribed queue. Acquires the subscriber lock.

        Silently does nothing if the queue is not currently registered.
        """
        with self._sub_lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    # ── publishing ───────────────────────────────────────────────────────────

    def publish(self, event: StreamEvent) -> None:
        """Publish *event* to all registered subscribers.

        Called synchronously from within async provider loops. Captures the
        running event loop on the first call. If called from a worker thread
        (no running loop in that thread), delegates to the captured main loop
        via ``call_soon_threadsafe``. If no loop has been captured yet, the
        call is silently dropped.
        """
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            # Worker-thread path: no event loop running in this thread.
            # Forward to the captured main loop if one is available and still open.
            # Guard against the asyncio.run()-restart scenario: each asyncio.run()
            # call closes the previous loop, so self._loop may point to a closed
            # loop if the StreamBus outlives one asyncio.run() session.
            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._publish_impl, event)
            return
        if self._loop is None:
            # First call from an event loop — capture it as the authoritative loop.
            self._loop = current_loop
        elif current_loop is not self._loop:
            # Foreign event-loop thread: marshal onto the captured main loop so
            # _publish_impl always executes on the same loop that owns the queues.
            # Exception: if the cached loop has been closed (e.g. after an
            # asyncio.run() restart), adopt the new loop instead of forwarding
            # to the dead one, which would raise RuntimeError or silently drop.
            if self._loop.is_closed():
                self._loop = current_loop
            else:
                self._loop.call_soon_threadsafe(self._publish_impl, event)
                return
        self._publish_impl(event)

    def _publish_impl(self, event: StreamEvent) -> None:
        """Internal fan-out: put *event* into every subscriber queue.

        Ring-buffer drop policy: if a queue is full, ``get_nowait()`` discards
        the oldest item before ``put_nowait()`` inserts the new one.
        Any exception from a single queue is swallowed so other subscribers
        are not affected.
        """
        with self._sub_lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            if q.full():
                try:
                    q.get_nowait()  # drop oldest
                except (asyncio.QueueEmpty, Exception):
                    pass
            try:
                q.put_nowait(event)
            except (asyncio.QueueFull, Exception):
                pass  # one bad subscriber must not poison others
