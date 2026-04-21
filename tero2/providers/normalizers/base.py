"""StreamNormalizer Protocol.

Per-provider normalizers are pure functions: one ``raw`` input (a ``dict``
for CLI providers, or an SDK Message object for zai) → zero or more
``StreamEvent`` instances.  No I/O, no global state.  On parse failure yield
exactly one ``StreamEvent(kind="error", …)``; never raise.

Any class that implements::

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = ...,
    ) -> Iterable[StreamEvent]: ...

satisfies this Protocol structurally — no inheritance required.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Protocol

from tero2.stream_bus import StreamEvent


class StreamNormalizer(Protocol):
    """Structural protocol for per-provider stream normalizers.

    Any object with a compatible ``normalize`` method satisfies this Protocol
    without needing to inherit from it.  Existing normalizers
    (``ClaudeNormalizer``, ``ZaiNormalizer``, …) conform automatically.

    Contract:
    - ``normalize(raw, role)`` → ``Iterable[StreamEvent]``
    - One raw line may yield *zero or many* events.
    - On parse failure: yield ONE ``StreamEvent(kind="error", …)``.
    - Pure function — no I/O, no global state, no mutation of *raw*.
    """

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = ...,  # type: ignore[assignment]
    ) -> Iterable[StreamEvent]:
        """Convert one raw provider line into zero or more StreamEvents.

        Args:
            raw:  Raw data from the provider — a ``dict`` (CLI providers) or
                  a duck-typed SDK object (zai).
            role: SORA role name (e.g. ``"builder"``).
            now:  Callable returning the current UTC datetime.  Injectable
                  for deterministic testing.

        Yields:
            Zero or more :class:`~tero2.stream_bus.StreamEvent` instances.
            Unknown or unhandled input should yield nothing, not raise.
        """
        ...
