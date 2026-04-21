"""Normalizer tests for the Claude CLI provider (stream-json format).

Tests ``ClaudeNormalizer`` against the real raw shapes emitted by
``claude --output-format stream-json``.  Each raw line is a nested dict
whose outer ``type`` field selects the message kind, and whose content is
embedded in ``message.content[...]`` blocks — not a flat ``{"type":"text",...}``
dict.

Key contracts validated here:
  - One raw ``assistant`` message may produce *multiple* events (one per block).
  - ``system`` lines → a single ``kind="status"`` event.
  - ``user`` messages containing ``tool_result`` blocks → ``kind="tool_result"``.
  - ``result`` with ``subtype="success"`` → ``kind="turn_end"``.
  - ``error`` dict with nested ``error.message`` → ``kind="error"``.
  - Malformed ``assistant`` (missing ``message``) → exactly one ``kind="error"``.
  - Golden-fixture round-trip covers all kinds + the multi-block case.
"""

from __future__ import annotations

import json
from pathlib import Path

from tero2.providers.normalizers.claude import ClaudeNormalizer

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load(name: str) -> list[dict]:
    """Load a .jsonl fixture, skipping blank lines and // comments."""
    out = []
    for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        out.append(json.loads(s))
    return out


# ── system block ─────────────────────────────────────────────────────────────


def test_system_block_yields_status() -> None:
    """system block must produce kind='status' with tool count in content."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "system", "tools": ["bash", "read", "write"]},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "status"
    assert "3" in out[0].content


def test_system_block_empty_tools() -> None:
    """system block with empty tools list must still produce kind='status'."""
    n = ClaudeNormalizer()
    out = list(n.normalize({"type": "system", "tools": []}, role="builder"))
    assert out[0].kind == "status"
    assert "0" in out[0].content


# ── assistant text block ──────────────────────────────────────────────────────


def test_claude_text_block() -> None:
    """assistant message with one text block → one kind='text' event."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "hello"}]}},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "text"
    assert out[0].content == "hello"


def test_claude_text_block_role_preserved() -> None:
    """Role must be stored on the emitted event."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "x"}]}},
        role="scout",
    ))
    assert out[0].role == "scout"


# ── assistant thinking block ──────────────────────────────────────────────────


def test_claude_thinking_block() -> None:
    """assistant message with thinking block → one kind='thinking' event."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "pondering..."},
        ]}},
        role="architect",
    ))
    assert len(out) == 1
    assert out[0].kind == "thinking"
    assert out[0].content == "pondering..."


# ── assistant tool_use block ──────────────────────────────────────────────────


def test_claude_tool_use_block() -> None:
    """assistant message with tool_use block → one kind='tool_use' event."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant",
         "message": {"content": [
             {"type": "tool_use", "id": "toolu_1", "name": "Read",
              "input": {"path": "x"}},
         ]}},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "tool_use"
    assert out[0].tool_name == "Read"
    assert out[0].tool_id == "toolu_1"
    assert out[0].tool_args == {"path": "x"}


def test_claude_tool_use_toolu_id_prefix() -> None:
    """Claude IDs use 'toolu_' prefix — must be preserved verbatim."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant",
         "message": {"content": [
             {"type": "tool_use", "id": "toolu_01AbcDefGhi", "name": "bash",
              "input": {"command": "ls"}},
         ]}},
        role="builder",
    ))
    assert out[0].tool_id == "toolu_01AbcDefGhi"


# ── multi-block assistant message (one-raw-to-many-events) ───────────────────


def test_claude_multi_block_message() -> None:
    """One assistant message with [text, tool_use] → TWO events in order."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "thinking out loud"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        ]}},
        role="builder",
    ))
    assert [e.kind for e in out] == ["text", "tool_use"]
    assert out[0].content == "thinking out loud"
    assert out[1].tool_name == "Read"


def test_claude_three_block_message() -> None:
    """assistant message with [thinking, text, tool_use] → THREE events."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "plan"},
            {"type": "text", "text": "narration"},
            {"type": "tool_use", "id": "t2", "name": "bash", "input": {}},
        ]}},
        role="builder",
    ))
    assert [e.kind for e in out] == ["thinking", "text", "tool_use"]


# ── user tool_result: string content ─────────────────────────────────────────


def test_claude_tool_result_string() -> None:
    """user message with tool_result (string content) → kind='tool_result'."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "file contents"},
        ]}},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "tool_result"
    assert out[0].tool_id == "toolu_1"
    assert out[0].tool_output == "file contents"


# ── user tool_result: list content (MCP style) ───────────────────────────────


def test_claude_tool_result_list_joined() -> None:
    """user message with tool_result list content → joined with newlines."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "text", "text": "line1"},
                         {"type": "text", "text": "line2"}]},
        ]}},
        role="builder",
    ))
    assert out[0].tool_output == "line1\nline2"


def test_claude_tool_result_list_single_item() -> None:
    """Single-item list content must produce the item text without separator."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "text", "text": "only item"}]},
        ]}},
        role="builder",
    ))
    assert "only item" in out[0].tool_output


# ── result / turn_end ─────────────────────────────────────────────────────────


def test_claude_result_success_is_turn_end() -> None:
    """result with subtype='success' → kind='turn_end'."""
    n = ClaudeNormalizer()
    out = list(n.normalize({"type": "result", "subtype": "success"}, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "turn_end"


def test_claude_result_error_subtype_skipped() -> None:
    """result with subtype='error' (non-success) → no event emitted."""
    n = ClaudeNormalizer()
    out = list(n.normalize({"type": "result", "subtype": "error"}, role="builder"))
    assert out == []


# ── error block ───────────────────────────────────────────────────────────────


def test_claude_error_block() -> None:
    """error with nested error.message dict → kind='error', content=message."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "error", "error": {"type": "rate_limit", "message": "slow down"}},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "error"
    assert "slow down" in out[0].content


def test_claude_error_unknown_subtype() -> None:
    """error with non-dict 'error' value must still produce kind='error'."""
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "error", "error": "plain string error"},
        role="builder",
    ))
    assert out[0].kind == "error"


# ── malformed input ───────────────────────────────────────────────────────────


def test_claude_malformed_assistant_yields_error() -> None:
    """assistant dict missing 'message' key → exactly one kind='error' event."""
    n = ClaudeNormalizer()
    out = list(n.normalize({"type": "assistant"}, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "error"


def test_claude_non_dict_raw_yields_error() -> None:
    """Non-dict raw value → exactly one kind='error' event."""
    n = ClaudeNormalizer()
    out = list(n.normalize("not a dict", role="builder"))
    assert len(out) == 1
    assert out[0].kind == "error"


def test_claude_unknown_type_skipped() -> None:
    """Unknown outer type → empty iterable (no event, no exception)."""
    n = ClaudeNormalizer()
    out = list(n.normalize({"type": "heartbeat", "seq": 42}, role="builder"))
    assert out == []


# ── golden fixture round-trips ────────────────────────────────────────────────


def test_claude_golden_fixture_happy() -> None:
    """Fixture claude.jsonl must produce text, tool_use, tool_result, turn_end."""
    n = ClaudeNormalizer()
    events = []
    for raw in _load("claude.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    kinds = {e.kind for e in events}
    assert "text" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert "turn_end" in kinds


def test_claude_fixture_multi_block_line_yields_multiple_events() -> None:
    """The multi-block assistant line in the fixture must yield ≥2 events."""
    n = ClaudeNormalizer()
    for raw in _load("claude.jsonl"):
        evs = list(n.normalize(raw, role="builder"))
        if len(evs) >= 2:
            return  # found at least one multi-event line
    raise AssertionError("no raw line in claude.jsonl produced multiple events")


def test_claude_rate_limit_fixture() -> None:
    """Fixture claude_rate_limit.jsonl must produce at least one kind='error'."""
    n = ClaudeNormalizer()
    events = []
    for raw in _load("claude_rate_limit.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    assert any(e.kind == "error" for e in events)
