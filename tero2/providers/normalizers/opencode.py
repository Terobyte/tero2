"""Normalizer for OpenCode CLI ``opencode run --format json`` output.

Two wire formats are supported:

**Real OpenCode 1.4.0 format** (``"type"`` discriminator, ``"part"`` envelope):

  step_start  {"type":"step_start","part":{...}}
              → silently skipped

  tool_use    {"type":"tool_use","part":{"tool":"...","callID":"...","state":{"input":{...},"output":"..."}}}
              → kind="tool_use", tool_name=part.tool, tool_args=part.state.input, tool_id=part.callID

  text        {"type":"text","part":{"text":"...",...}}
              → kind="text", content=part.text

  step_finish {"type":"step_finish","part":{"reason":"stop"|"tool-calls",...}}
              → reason="stop" → kind="turn_end"; anything else → silently skipped

  error       {"type":"error","error":{"name":"...","data":{"message":"..."}}}
              → kind="error", content=error.data.message (falls back to error.name)

  Anything else with ``"type"`` → silently skipped.

**Legacy / synthetic format** (``"event"`` discriminator, kept for backward compatibility):

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
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.providers.normalizers import register
from tero2.stream_bus import StreamEvent


class OpenCodeNormalizer:
    """Converts OpenCode ``--format json`` stream lines into StreamEvents.

    Supports both the real OpenCode 1.4.0 ``"type"``/``"part"`` wire format
    and the legacy synthetic ``"event"`` format (kept for backward compatibility).
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

        ts = now()

        # ── Real OpenCode 1.4.0 format: "type" discriminator with "part" envelope ──
        if "event" not in raw and "type" in raw:
            yield from self._normalize_type_format(raw, role, ts)
            return

        # ── Legacy / synthetic format: "event" discriminator ──────────────────────
        event = raw.get("event")

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

    def _normalize_type_format(
        self,
        raw: dict,
        role: str,
        ts: datetime,
    ) -> Iterable[StreamEvent]:
        """Handle the real OpenCode 1.4.0 ``"type"``/``"part"`` wire format."""
        event_type = raw.get("type")
        part: dict = raw.get("part") or {}

        if event_type == "text" and "part" in raw:
            yield StreamEvent(
                role=role, kind="text", timestamp=ts,
                content=part.get("text", ""), raw=raw,
            )
        elif event_type == "tool_use" and "part" in raw:
            state: dict = part.get("state") or {}
            yield StreamEvent(
                role=role, kind="tool_use", timestamp=ts,
                tool_name=part.get("tool", ""),
                tool_args=state.get("input") or {},
                tool_id=part.get("callID", ""),
                raw=raw,
            )
        elif event_type == "step_finish":
            if part.get("reason") == "stop":
                yield StreamEvent(role=role, kind="turn_end", timestamp=ts, raw=raw)
            # reason="tool-calls" and anything else → skip
        elif event_type == "error" and "error" in raw:
            err_obj: dict = raw.get("error") or {}
            data: dict = err_obj.get("data") or {}
            msg = data.get("message") or err_obj.get("name") or ""
            yield StreamEvent(
                role=role, kind="error", timestamp=ts,
                content=msg, raw=raw,
            )
        # step_start and anything else → skip


register("opencode", OpenCodeNormalizer())
