"""Normalizer for Kilo CLI ``kilo run --format json`` output.

Raw line shapes emitted by ``kilo run --format json``:

  text        {"type":"text","text":"..."}
              → kind="text", content=text

  status      {"type":"status","text":"..."}
              → kind="status", content=text

  tool_use    {"type":"tool_use","id":"kilo_NN","name":"...","input":{}}
              → kind="tool_use", tool_id=id, tool_name=name, tool_args=input
              Note: Kilo uses sequential numeric IDs prefixed with "kilo_"

  tool_result {"type":"tool_result","tool_use_id":"...","content":"..."|[...]}
              → kind="tool_result", tool_id, tool_output=<stringified>
              Note: Kilo may wrap output in a content list of text blocks

  thinking    {"type":"thinking","thinking":"..."}
              → kind="thinking", content=thinking

  error       {"type":"error","error":"..." | "text":"..."}
              → kind="error", content=message

  turn_end    {"type":"turn_end"}
              → kind="turn_end"

  Anything else (e.g. "kilo_internal_checkpoint") → kind="text" with text
  content if present (graceful fallback, never raises).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.providers.normalizers import register
from tero2.stream_bus import StreamEvent

_KNOWN = frozenset({
    "text", "status", "tool_use", "tool_result", "thinking", "error", "turn_end",
})


class KiloNormalizer:
    """Converts Kilo ``--format json`` stream lines into StreamEvents.

    Kilo uses the ``"type"`` discriminator key (same as Claude/Codex) but with
    its own field conventions:

    - Sequential numeric IDs prefixed ``kilo_NN`` for tool correlation.
    - ``"input"`` for tool arguments (same as Claude, unlike OpenCode ``"args"``).
    - ``"tool_use_id"`` for result correlation (same as Claude).
    - ``"content"`` may be a ``list`` of ``{"type":"text","text":"..."}`` blocks
      (Kilo wraps multi-line output in a content list).
    - Unknown ``"type"`` values fall back to ``kind="text"`` rather than raising.
    """

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        """Convert one Kilo raw dict to zero or more StreamEvents.

        Args:
            raw:  Dict as yielded by ``kilo run --format json`` (one JSON line).
            role: SORA role name (e.g. ``"builder"``).
            now:  Callable returning current UTC datetime.

        Yields:
            Zero or more :class:`~tero2.stream_bus.StreamEvent` instances.
            Unknown event types produce one ``kind="text"`` fallback event.
        """
        if not isinstance(raw, dict):
            return

        kind = raw.get("type", "")
        ts = now()

        if kind == "text":
            yield StreamEvent(
                role=role, kind="text", timestamp=ts,
                content=raw.get("text", ""), raw=raw,
            )

        elif kind == "status":
            yield StreamEvent(
                role=role, kind="status", timestamp=ts,
                content=raw.get("text", "") or raw.get("status", ""), raw=raw,
            )

        elif kind == "tool_use":
            yield StreamEvent(
                role=role, kind="tool_use", timestamp=ts,
                tool_name=raw.get("name", ""),
                tool_args=raw.get("input") or {},
                tool_id=raw.get("id", ""),
                raw=raw,
            )

        elif kind == "tool_result":
            content = raw.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    item.get("text", "") if isinstance(item, dict) else str(item)
                    for item in content
                    if item is not None
                )
            yield StreamEvent(
                role=role, kind="tool_result", timestamp=ts,
                tool_output=content,
                tool_id=raw.get("tool_use_id", ""),
                raw=raw,
            )

        elif kind == "thinking":
            yield StreamEvent(
                role=role, kind="thinking", timestamp=ts,
                content=raw.get("thinking", "") or raw.get("text", ""), raw=raw,
            )

        elif kind == "error":
            msg = raw.get("text", "") or raw.get("error", "")
            yield StreamEvent(
                role=role, kind="error", timestamp=ts, content=msg, raw=raw,
            )

        elif kind == "turn_end":
            yield StreamEvent(role=role, kind="turn_end", timestamp=ts, raw=raw)

        else:
            # Unknown Kilo-specific event type (e.g. "kilo_internal_checkpoint"):
            # fall back to text so new event kinds degrade gracefully.
            yield StreamEvent(
                role=role, kind="text", timestamp=ts,
                content=raw.get("text", ""), raw=raw,
            )


register("kilo", KiloNormalizer())
