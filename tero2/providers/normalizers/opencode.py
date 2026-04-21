"""Normalizer for OpenCode CLI ``opencode run --format json`` output.

Raw line shapes emitted by ``opencode run --format json``:

  message     {"event":"message","role":"assistant","text":"..."}
              → kind="text", content=text
              (role="user" messages are silently skipped)

  tool_call   {"event":"tool_call","name":"...","args":{...},"id":"..."}
              → kind="tool_use", tool_name=name, tool_args=args, tool_id=id

  tool_result {"event":"tool_result","id":"...","result":"..."}
              → kind="tool_result", tool_id=id, tool_output=result

  end         {"event":"end"}
              → kind="turn_end"

  error       {"event":"error","message":"..."}
              → kind="error", content=message

  Anything else → empty iterable (silently skipped).

Note: OpenCode uses ``"event"`` as the discriminator key (not ``"type"``),
``"args"`` instead of ``"input"`` for tool arguments, and ``"result"``
instead of ``"content"``/``"output"`` for tool results.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.providers.normalizers import register
from tero2.stream_bus import StreamEvent


class OpenCodeNormalizer:
    """Converts OpenCode ``--format json`` stream lines into StreamEvents.

    OpenCode uses an ``"event"`` key as the discriminator (not ``"type"``).
    Each raw line yields exactly 0 or 1 event.
    """

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        """Convert one OpenCode raw dict to zero or one StreamEvents.

        Args:
            raw:  Dict as yielded by ``opencode run --format json`` (one JSON line).
            role: SORA role name (e.g. ``"builder"``).
            now:  Callable returning current UTC datetime.

        Yields:
            Zero or one :class:`~tero2.stream_bus.StreamEvent`.
        """
        if not isinstance(raw, dict):
            return

        event = raw.get("event")
        ts = now()

        if event == "message":
            if raw.get("role") == "assistant":
                yield StreamEvent(
                    role=role, kind="text", timestamp=ts,
                    content=raw.get("text", ""), raw=raw,
                )
            # user messages → skip (no event)
        elif event == "tool_call":
            yield StreamEvent(
                role=role, kind="tool_use", timestamp=ts,
                tool_name=raw.get("name", ""),
                tool_args=raw.get("args") or {},
                tool_id=raw.get("id", ""),
                raw=raw,
            )
        elif event == "tool_result":
            yield StreamEvent(
                role=role, kind="tool_result", timestamp=ts,
                tool_output=raw.get("result", ""),
                tool_id=raw.get("id", ""),
                raw=raw,
            )
        elif event == "end":
            yield StreamEvent(role=role, kind="turn_end", timestamp=ts, raw=raw)
        elif event == "error":
            yield StreamEvent(
                role=role, kind="error", timestamp=ts,
                content=raw.get("message", ""), raw=raw,
            )
        # Anything else → empty (no event)


register("opencode", OpenCodeNormalizer())
