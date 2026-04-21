"""Test helpers for stream event normalization.

``normalize_raw`` converts a raw dict — as yielded by CLIProvider._stream_events
or any compatible provider — into a :class:`~tero2.stream_bus.StreamEvent`.

This module is the *canonical normalizer* used by the test suite.  Production
callers that want to publish to :class:`~tero2.stream_bus.StreamBus` should
use the same mapping logic here or extract it into tero2.stream_bus when a
per-provider normalizer is added to the source tree.

Supported raw event shapes (all from provider CLI JSON output):

    text       {"type": "text", "text": "..."}
    tool_use   {"type": "tool_use", "id": "...", "name": "...", "input": {...}}
    tool_result{"type": "tool_result", "tool_use_id": "...", "content": "..."}
    thinking   {"type": "thinking", "thinking": "..."}
    status     {"type": "status", "text": "..."}
    error      {"type": "error", "text": "..." | "error": "..."}
    turn_end   {"type": "turn_end"}

Any other ``type`` value is treated as a plain text event so that new provider
event kinds degrade gracefully rather than raising.
"""

from __future__ import annotations

from tero2.stream_bus import StreamEvent, make_stream_event

# ── Recognised kind literals ──────────────────────────────────────────────────

_KNOWN_KINDS = frozenset({
    "text",
    "tool_use",
    "tool_result",
    "thinking",
    "status",
    "error",
    "turn_end",
})


def normalize_raw(raw: dict, *, role: str = "") -> StreamEvent:
    """Convert a raw provider dict to a :class:`~tero2.stream_bus.StreamEvent`.

    Args:
        raw:  Raw dict as yielded by ``CLIProvider._stream_events`` or any
              compatible provider.  Must be a ``dict``; callers are responsible
              for filtering non-dict values upstream.
        role: SORA role name to attach to the event (e.g. ``"builder"``).

    Returns:
        A fully-populated :class:`~tero2.stream_bus.StreamEvent`.

    Notes:
        - Unknown ``type`` values fall back to ``kind="text"``.
        - ``raw`` is stored on the event unchanged for debug / raw-mode consumers.
    """
    raw_kind = raw.get("type", "")
    kind = raw_kind if raw_kind in _KNOWN_KINDS else "text"  # type: ignore[arg-type]

    if kind == "text":
        content = raw.get("text", "") or raw.get("content", "")
        return make_stream_event(role, "text", content=content, raw=raw)

    if kind == "tool_use":
        return make_stream_event(
            role,
            "tool_use",
            tool_name=raw.get("name", ""),
            tool_args=raw.get("input") or {},
            tool_id=raw.get("id", ""),
            raw=raw,
        )

    if kind == "tool_result":
        content = raw.get("content", "")
        if isinstance(content, list):
            # Some providers wrap content in a list of {"type":"text","text":"..."} objects.
            content = " ".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        return make_stream_event(
            role,
            "tool_result",
            tool_output=content,
            tool_id=raw.get("tool_use_id", ""),
            raw=raw,
        )

    if kind == "thinking":
        return make_stream_event(
            role,
            "thinking",
            content=raw.get("thinking", "") or raw.get("text", ""),
            raw=raw,
        )

    if kind == "status":
        return make_stream_event(
            role,
            "status",
            content=raw.get("text", "") or raw.get("status", ""),
            raw=raw,
        )

    if kind == "error":
        content = raw.get("text", "") or raw.get("error", "")
        return make_stream_event(role, "error", content=content, raw=raw)

    if kind == "turn_end":
        return make_stream_event(role, "turn_end", raw=raw)

    # Should be unreachable given the fallback above, but kept for safety.
    return make_stream_event(role, "text", content=raw.get("text", ""), raw=raw)
