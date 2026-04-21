"""Normalizer for Claude CLI ``--output-format stream-json`` output.

Raw line shapes emitted by ``claude --output-format stream-json``:

  system     {"type":"system","tools":[...]}
             → kind="status", content="init: N tools"

  assistant  {"type":"assistant","message":{"content":[block,...]}}
             Blocks (each yields ONE event):
               text     {"type":"text","text":"..."}       → kind="text"
               tool_use {"type":"tool_use","id","name","input":{}}
                                                           → kind="tool_use"
               thinking {"type":"thinking","thinking":"..."} → kind="thinking"

  user       {"type":"user","message":{"content":[{type:"tool_result",...}]}}
             → kind="tool_result", tool_id, tool_output=<stringified>

  result     {"type":"result","subtype":"success"} → kind="turn_end"

  error      {"type":"error","error":{"message":"..."}}
             → kind="error", content=message

  Anything else → empty iterable (silently skipped).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.providers.normalizers import register
from tero2.stream_bus import StreamEvent


class ClaudeNormalizer:
    """Converts Claude stream-JSON lines into :class:`~tero2.stream_bus.StreamEvent` objects.

    Implements the ``StreamNormalizer`` protocol: one raw dict may produce
    zero or *many* events (e.g. an assistant message with text + tool_use
    blocks yields two events).
    """

    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        """Convert one Claude raw dict to zero or more StreamEvents.

        Args:
            raw:  Dict as yielded by the Claude CLI (one JSON line).
            role: SORA role name (e.g. ``"builder"``).
            now:  Callable returning current UTC datetime (injectable for tests).

        Yields:
            :class:`~tero2.stream_bus.StreamEvent` instances, one per logical
            event encoded in *raw*.  May yield nothing for unrecognised types.
        """
        if not isinstance(raw, dict):
            yield self._err(role, now(), f"non-dict raw: {type(raw).__name__}", raw)
            return

        kind = raw.get("type")
        try:
            if kind == "system":
                tools = raw.get("tools") or []
                yield StreamEvent(
                    role=role, kind="status", timestamp=now(),
                    content=f"init: {len(tools)} tools", raw=raw,
                )
            elif kind == "assistant":
                yield from self._assistant(raw, role, now())
            elif kind == "user":
                yield from self._user(raw, role, now())
            elif kind == "result" and raw.get("subtype") == "success":
                yield StreamEvent(role=role, kind="turn_end", timestamp=now(), raw=raw)
            elif kind == "error":
                err = raw.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                yield StreamEvent(
                    role=role, kind="error", timestamp=now(),
                    content=msg or "unknown error", raw=raw,
                )
            # Anything else (e.g. "result" with subtype="error") → empty
        except Exception as exc:
            yield self._err(role, now(), f"parse: {exc}", raw)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _assistant(self, raw: dict, role: str, ts: datetime) -> Iterable[StreamEvent]:
        msg = raw.get("message")
        if not isinstance(msg, dict):
            raise ValueError("assistant.message missing or not a dict")
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                yield StreamEvent(
                    role=role, kind="text", timestamp=ts,
                    content=block.get("text") or "", raw=block,
                )
            elif btype == "tool_use":
                yield StreamEvent(
                    role=role, kind="tool_use", timestamp=ts,
                    tool_name=block.get("name") or "",
                    tool_args=block.get("input") or {},
                    tool_id=block.get("id") or "",
                    raw=block,
                )
            elif btype == "thinking":
                yield StreamEvent(
                    role=role, kind="thinking", timestamp=ts,
                    content=block.get("thinking") or "", raw=block,
                )

    def _user(self, raw: dict, role: str, ts: datetime) -> Iterable[StreamEvent]:
        msg = raw.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, list):
                parts = []
                for sub in content:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(sub.get("text") or "")
                    else:
                        parts.append(str(sub))
                output = "\n".join(parts)
            else:
                output = str(content) if content is not None else ""
            yield StreamEvent(
                role=role, kind="tool_result", timestamp=ts,
                tool_id=block.get("tool_use_id") or "",
                tool_output=output,
                raw=block,
            )

    def _err(self, role: str, ts: datetime, msg: str, raw: Any) -> StreamEvent:
        raw_dict = raw if isinstance(raw, dict) else {"repr": repr(raw)[:200]}
        return StreamEvent(role=role, kind="error", timestamp=ts, content=msg, raw=raw_dict)


register("claude", ClaudeNormalizer())
