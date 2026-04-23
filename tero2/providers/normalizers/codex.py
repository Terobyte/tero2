"""Normalizer for OpenAI Codex CLI ``codex --json`` output.

Raw line shapes emitted by ``codex exec --json``:

  text        {"type":"text","content":"..."}
              → kind="text", content=content

  tool        {"type":"tool","name":"...","id":"...","input":{...}}
              → kind="tool_use", tool_name, tool_id, tool_args=input

  tool_output {"type":"tool_output","id":"...","output":"..."}
              → kind="tool_result", tool_id, tool_output=output

  done        {"type":"done"}
              → kind="turn_end"

  error       {"type":"error","message":"..."}
              → kind="error", content=message

  Anything else → empty iterable (silently skipped).

Note: Codex uses ``"content"`` (not ``"text"``) for text events, and
``"tool"`` / ``"tool_output"`` (not ``"tool_use"`` / ``"tool_result"``) for
tool events.  The ``"id"`` key is used on both ``tool`` and ``tool_output``
for correlation (vs Claude's ``"tool_use_id"``).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.providers.normalizers import register
from tero2.stream_bus import StreamEvent


class CodexNormalizer:
    """Converts Codex ``--json`` stream lines into StreamEvents.

    Unlike Claude, Codex never emits multi-block lines so ``normalize()``
    yields exactly 0 or 1 event per raw dict.
    """

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        """Convert one Codex raw dict to zero or one StreamEvents.

        Args:
            raw:  Dict as yielded by ``codex exec --json`` (one JSON line).
            role: SORA role name (e.g. ``"builder"``).
            now:  Callable returning current UTC datetime.

        Yields:
            Zero or one :class:`~tero2.stream_bus.StreamEvent`.
        """
        if not isinstance(raw, dict):
            yield StreamEvent(
                role=role, kind="error", timestamp=now(),
                content=f"codex: expected dict, got {type(raw).__name__}",
            )
            return

        kind = raw.get("type")
        ts = now()

        if kind == "text":
            yield StreamEvent(
                role=role, kind="text", timestamp=ts,
                content=raw.get("content", ""), raw=raw,
            )
        elif kind == "tool":
            yield StreamEvent(
                role=role, kind="tool_use", timestamp=ts,
                tool_name=raw.get("name", ""),
                tool_args=raw.get("input") or {},
                tool_id=raw.get("id", ""),
                raw=raw,
            )
        elif kind == "tool_output":
            yield StreamEvent(
                role=role, kind="tool_result", timestamp=ts,
                tool_output=raw.get("output", ""),
                tool_id=raw.get("id", ""),
                raw=raw,
            )
        elif kind == "done":
            yield StreamEvent(role=role, kind="turn_end", timestamp=ts, raw=raw)
        elif kind == "error":
            msg = raw.get("message", "") or raw.get("error", "")
            yield StreamEvent(
                role=role, kind="error", timestamp=ts, content=msg, raw=raw,
            )
        # Anything else → empty (no event)


register("codex", CodexNormalizer())
