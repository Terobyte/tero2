"""Normalizer tests for the Codex CLI provider (codex exec --json format).

Tests ``CodexNormalizer`` against the real raw shapes emitted by
``codex exec --json``.  Codex uses different type tokens than Claude:
  - ``"text"`` with a ``"content"`` key (not ``"text"``)
  - ``"tool"`` for tool invocations (not ``"tool_use"``)
  - ``"tool_output"`` for results (not ``"tool_result"``)
  - ``"done"`` for stream end (not ``"result"`` or ``"turn_end"``)
  - ``"error"`` with a ``"message"`` key (not ``"error.message"`` nested)

Contracts validated here:
  - Provider-specific type tokens map to canonical StreamEvent kinds.
  - Codex ``"tool"`` / ``"tool_output"`` correlation via ``"id"`` key.
  - ``"cmd"`` inside ``input`` is preserved in tool_args unchanged.
  - Unknown Codex types (future additions) → empty iterable, no exception.
  - Golden fixture round-trip covers text, tool, tool_output, done, error.
"""

from __future__ import annotations

import json
from pathlib import Path

from tero2.providers.normalizers.codex import CodexNormalizer

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


# ── text events (Codex uses "content" key, not "text") ───────────────────────


def test_codex_text_kind() -> None:
    """Codex text events (type='text') must produce kind='text'."""
    n = CodexNormalizer()
    out = list(n.normalize({"type": "text", "content": "hello"}, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "text"


def test_codex_text_content_from_content_key() -> None:
    """Content must come from the Codex 'content' key (not 'text')."""
    n = CodexNormalizer()
    msg = "I'll help you implement the requested feature."
    out = list(n.normalize({"type": "text", "content": msg}, role="builder"))
    assert out[0].content == msg


def test_codex_text_role_preserved() -> None:
    """Role must be stored on the event."""
    n = CodexNormalizer()
    out = list(n.normalize({"type": "text", "content": "x"}, role="scout"))
    assert out[0].role == "scout"


# ── tool events (Codex uses type="tool", not "tool_use") ─────────────────────


def test_codex_tool_kind() -> None:
    """Codex tool invocations (type='tool') must produce kind='tool_use'."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool", "name": "bash", "id": "call_Abc123", "input": {}},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "tool_use"


def test_codex_tool_name() -> None:
    """tool_name must come from the 'name' field."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool", "name": "bash", "id": "call_Abc123", "input": {}},
        role="builder",
    ))
    assert out[0].tool_name == "bash"


def test_codex_tool_id_call_prefix() -> None:
    """Codex IDs use 'call_' prefix — tool_id must preserve it verbatim."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool", "name": "bash", "id": "call_Abc123Xyz", "input": {}},
        role="builder",
    ))
    assert out[0].tool_id == "call_Abc123Xyz"


def test_codex_tool_input_with_cmd_key() -> None:
    """Codex may use 'cmd' inside 'input' — tool_args must carry it unchanged."""
    inp = {"cmd": "git log --oneline -5"}
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool", "name": "bash", "id": "call_X", "input": inp},
        role="builder",
    ))
    assert out[0].tool_args == inp
    assert out[0].tool_args["cmd"] == "git log --oneline -5"


def test_codex_apply_patch_tool() -> None:
    """apply_patch tool must normalise with tool_name='apply_patch'."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool", "name": "apply_patch", "id": "call_Def456",
         "input": {"patch": "--- a/foo.py\n+++ b/foo.py\n"}},
        role="builder",
    ))
    assert out[0].tool_name == "apply_patch"
    assert out[0].tool_id == "call_Def456"


# ── tool_output events (Codex uses type="tool_output", not "tool_result") ────


def test_codex_tool_output_kind() -> None:
    """Codex tool results (type='tool_output') must produce kind='tool_result'."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool_output", "id": "call_Abc123", "output": "ok"},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "tool_result"


def test_codex_tool_output_content() -> None:
    """tool_output from 'output' key must land in tool_output field."""
    text = "a1b2c3d fix: resolve race condition\ne4f5g6h add: unit tests"
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool_output", "id": "call_Abc123", "output": text},
        role="builder",
    ))
    assert out[0].tool_output == text


def test_codex_tool_output_id_correlation() -> None:
    """tool_id on the result must match the originating tool 'id'."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "tool_output", "id": "call_Def456Uvw", "output": "Applied."},
        role="builder",
    ))
    assert out[0].tool_id == "call_Def456Uvw"


# ── done event (Codex stream terminator) ────────────────────────────────────


def test_codex_done_is_turn_end() -> None:
    """Codex 'done' event must produce kind='turn_end'."""
    n = CodexNormalizer()
    out = list(n.normalize({"type": "done"}, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "turn_end"


# ── error events ──────────────────────────────────────────────────────────────


def test_codex_error_kind() -> None:
    """Codex error events must produce kind='error'."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "error", "message": "sandbox: command not permitted"},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "error"


def test_codex_error_content_from_message_key() -> None:
    """Error content must come from the 'message' key (Codex-specific)."""
    n = CodexNormalizer()
    out = list(n.normalize(
        {"type": "error", "message": "sandbox: command not permitted"},
        role="builder",
    ))
    assert out[0].content == "sandbox: command not permitted"


# ── unknown type → empty iterable ────────────────────────────────────────────


def test_codex_unknown_type_yields_nothing() -> None:
    """Unrecognised Codex event types must yield nothing (no exception)."""
    n = CodexNormalizer()
    out = list(n.normalize({"type": "progress", "pct": 50}, role="builder"))
    assert out == []


def test_codex_non_dict_raw_yields_error() -> None:
    """Non-dict raw must yield an error event (bug 126: silent data loss)."""
    n = CodexNormalizer()
    out = list(n.normalize("not a dict", role="builder"))
    assert len(out) == 1
    assert out[0].kind == "error"


# ── golden fixture round-trip ─────────────────────────────────────────────────


def test_codex_golden_fixture_happy() -> None:
    """Fixture codex.jsonl must produce text, tool_use, tool_result, turn_end, error."""
    n = CodexNormalizer()
    events = []
    for raw in _load("codex.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    kinds = {e.kind for e in events}
    assert "text" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert "turn_end" in kinds
    assert "error" in kinds


def test_codex_fixture_tool_id_correlation() -> None:
    """Each tool_result in the fixture must have a tool_id matching a prior tool_use."""
    n = CodexNormalizer()
    tool_use_ids: set[str] = set()
    result_ids: set[str] = set()
    for raw in _load("codex.jsonl"):
        for ev in n.normalize(raw, role="builder"):
            if ev.kind == "tool_use":
                tool_use_ids.add(ev.tool_id)
            elif ev.kind == "tool_result":
                result_ids.add(ev.tool_id)
    assert result_ids, "no tool_result events in fixture"
    assert result_ids.issubset(tool_use_ids), (
        f"tool_result IDs {result_ids - tool_use_ids} have no matching tool_use"
    )


def test_codex_fixture_done_is_last() -> None:
    """The last raw line in codex.jsonl must be the 'done' event."""
    raws = _load("codex.jsonl")
    assert raws[-1].get("type") == "done"


# ── negative-path fixture: tool errors surfaced as tool_output ────────────────


def test_codex_tool_error_fixture_produces_tool_results() -> None:
    """Failed shell commands in codex_tool_error.jsonl must still produce tool_result events.

    Codex surfaces tool errors as tool_output lines whose 'output' contains
    stderr text.  They do NOT use type='error'.  The normalizer must emit
    kind='tool_result' regardless of whether the shell command succeeded.
    """
    n = CodexNormalizer()
    events = []
    for raw in _load("codex_tool_error.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    result_events = [e for e in events if e.kind == "tool_result"]
    assert len(result_events) >= 2, (
        "fixture has two failed tool calls — expected at least 2 tool_result events"
    )


def test_codex_tool_error_fixture_preserves_stderr_text() -> None:
    """tool_result events from failed runs must carry the stderr text verbatim."""
    n = CodexNormalizer()
    result_events = []
    for raw in _load("codex_tool_error.jsonl"):
        for ev in n.normalize(raw, role="builder"):
            if ev.kind == "tool_result":
                result_events.append(ev)
    assert result_events, "no tool_result events in fixture"
    stderr_text = "No such file or directory"
    assert all(stderr_text in (ev.tool_output or "") for ev in result_events), (
        "expected all tool_result events to contain the shell error text"
    )


def test_codex_tool_error_fixture_id_correlation() -> None:
    """Every tool_result in the error fixture must have a tool_id matching a prior tool_use."""
    n = CodexNormalizer()
    tool_use_ids: set[str] = set()
    result_ids: set[str] = set()
    for raw in _load("codex_tool_error.jsonl"):
        for ev in n.normalize(raw, role="builder"):
            if ev.kind == "tool_use":
                tool_use_ids.add(ev.tool_id)
            elif ev.kind == "tool_result":
                result_ids.add(ev.tool_id)
    assert result_ids.issubset(tool_use_ids), (
        f"tool_result IDs {result_ids - tool_use_ids} have no matching tool_use"
    )
