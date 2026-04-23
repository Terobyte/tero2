"""Normalizer for the Zai provider (Claude Agent SDK via api.z.ai).

Zai uses the Claude Agent SDK, which yields SDK message objects rather than
plain dicts.  This normalizer handles both cases via duck-typing:

  - ``isinstance(raw, dict)`` → access via ``raw.get(key)``
  - Otherwise                 → access via ``getattr(raw, key, default)``

Raw event shapes (as dicts or duck-typed SDK objects):

  text        type="text",    text="..."
              → kind="text", content=text

  thinking    type="thinking", thinking="..."      (extended thinking)
              → kind="thinking", content=thinking

  tool_use    type="tool_use", id="zai_toolu_NN", name="...", input={...}
              → kind="tool_use", tool_id=id, tool_name=name, tool_args=input

  tool_result type="tool_result", tool_use_id="...", content="..."|[...]
              → kind="tool_result", tool_id, tool_output=<stringified>
              Note: SDK may wrap content in a list of text blocks

  status      type="status", text="..."
              → kind="status", content=text

  error       type="error", error="..."
              → kind="error", content=message
              Note: Zai API errors use "error" key (not "text")

  turn_end    type="turn_end"
              → kind="turn_end"

  Anything else → empty iterable (silently skipped).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.providers.normalizers import register
from tero2.stream_bus import StreamEvent


def _get(obj: Any, key: str, default: Any = "") -> Any:
    """Duck-typed attribute access — works for both dicts and SDK objects."""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


class ZaiNormalizer:
    """Converts Zai SDK stream events into StreamEvents.

    Supports both plain ``dict`` inputs (e.g. from fixtures or intermediate
    pipeline stages) and duck-typed SDK message objects (e.g.
    ``anthropic.types.TextBlock``, ``anthropic.types.ToolUseBlock``).

    The ``_get()`` helper provides unified attribute access so the same
    normalization logic handles both input forms transparently.
    """

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        """Convert one Zai SDK event to zero or more StreamEvents.

        Args:
            raw:  Dict or duck-typed SDK object from the Zai pipeline.
            role: SORA role name (e.g. ``"builder"``).
            now:  Callable returning current UTC datetime.

        Yields:
            Zero or more :class:`~tero2.stream_bus.StreamEvent` instances.
            Unrecognised types yield nothing (no exception).
        """
        if raw is None:
            return
        if not isinstance(raw, dict) and not hasattr(raw, "__dict__") and not hasattr(raw, "type"):
            yield StreamEvent(
                role=role, kind="error", timestamp=now(),
                content=f"zai: expected dict or SDK object, got {type(raw).__name__}",
            )
            return

        kind = _get(raw, "type", "")
        ts = now()

        if kind == "text":
            yield StreamEvent(
                role=role, kind="text", timestamp=ts,
                content=_get(raw, "text", ""), raw=self._to_raw(raw),
            )

        elif kind == "thinking":
            yield StreamEvent(
                role=role, kind="thinking", timestamp=ts,
                content=_get(raw, "thinking", "") or _get(raw, "text", ""),
                raw=self._to_raw(raw),
            )

        elif kind == "tool_use":
            yield StreamEvent(
                role=role, kind="tool_use", timestamp=ts,
                tool_name=_get(raw, "name", ""),
                tool_args=_get(raw, "input", None) or {},
                tool_id=_get(raw, "id", ""),
                raw=self._to_raw(raw),
            )

        elif kind == "tool_result":
            content = _get(raw, "content", "")
            if isinstance(content, list):
                content = " ".join(
                    item.get("text", "") if isinstance(item, dict)
                    else getattr(item, "text", str(item))
                    for item in content
                )
            yield StreamEvent(
                role=role, kind="tool_result", timestamp=ts,
                tool_output=content,
                tool_id=_get(raw, "tool_use_id", ""),
                raw=self._to_raw(raw),
            )

        elif kind == "status":
            yield StreamEvent(
                role=role, kind="status", timestamp=ts,
                content=_get(raw, "text", "") or _get(raw, "status", ""),
                raw=self._to_raw(raw),
            )

        elif kind == "error":
            msg = _get(raw, "error", "") or _get(raw, "text", "")
            yield StreamEvent(
                role=role, kind="error", timestamp=ts,
                content=msg, raw=self._to_raw(raw),
            )

        elif kind == "turn_end":
            yield StreamEvent(role=role, kind="turn_end", timestamp=ts,
                              raw=self._to_raw(raw))

        # Anything else → empty (no event)

    def _to_raw(self, obj: Any) -> dict:
        """Return a dict suitable for StreamEvent.raw.

        For dicts, returns the dict unchanged.  For SDK objects, builds a
        minimal dict from the object's ``__dict__`` (or type name fallback).
        """
        if isinstance(obj, dict):
            return obj
        # SDK object: materialise a shallow dict snapshot
        try:
            return dict(vars(obj))
        except TypeError:
            return {"_repr": repr(obj)[:200]}


register("zai", ZaiNormalizer())
