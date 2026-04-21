"""FallbackNormalizer for unknown or unregistered provider kinds.

When ``get_normalizer()`` receives a ``provider_kind`` that has no registered
normalizer it returns a singleton :class:`FallbackNormalizer` rather than
``None``.  This avoids ``if normalizer is None`` guards at every call site and
ensures every raw line produces *at least* a ``kind="status"`` event that the
TUI can surface without crashing.

The emitted content is ``"raw: <repr>"`` (truncated to 200 chars) so the user
can still see what the unknown provider sent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.stream_bus import StreamEvent


class FallbackNormalizer:
    """Safety-net normalizer used when no provider-specific normalizer exists.

    Emits a single ``kind="status"`` event for every raw input, with the repr
    of the raw value in ``content``.  Never raises; handles non-dict inputs
    gracefully.

    Design rationale:
    - ``kind="status"`` (not ``"error"``) because unknown output is not
      necessarily an error — it may be a new event type from a future CLI
      version or a provider that simply hasn't been registered yet.
    - Returns a real event (not an empty iterable) so callers can always see
      *something* arrived from the provider rather than experiencing a silent
      gap in the stream.
    """

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        """Emit one ``kind="status"`` event carrying the repr of *raw*.

        Args:
            raw:  Any value — dict, SDK object, string, or ``None``.
            role: SORA role name (e.g. ``"builder"``).
            now:  Callable returning current UTC datetime.

        Yields:
            Exactly one :class:`~tero2.stream_bus.StreamEvent` with
            ``kind="status"`` and ``content="raw: <repr>"``.
        """
        preview = repr(raw)
        if len(preview) > 200:
            preview = preview[:200] + "…"
        raw_dict = raw if isinstance(raw, dict) else {"repr": preview}
        yield StreamEvent(
            role=role,
            kind="status",
            timestamp=now(),
            content=f"raw: {preview}",
            raw=raw_dict,
        )
