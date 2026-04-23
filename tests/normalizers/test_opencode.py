"""Normalizer tests for the OpenCode CLI provider (opencode run --format json).

Covers both wire formats emitted by opencode:

  **Real OpenCode 1.4.0 format** (``"type"`` discriminator + ``"part"`` envelope):
    - Captured from live ``opencode run --format json`` runs (see fixtures/captures/).
    - Uses ``type="step_start"|"tool_use"|"text"|"step_finish"`` and
      ``{"type":"error",...}`` for unknown-model failures.

  **Legacy / synthetic format** (``"event"`` discriminator):
    - Kept for backward compatibility; synthetic fixtures still exercise this path.
    - ``"event":"message"`` with ``"role"`` and ``"text"`` for assistant narration
    - ``"event":"tool_call"`` with ``"args"`` (not ``"input"``) for tool invocations
    - ``"event":"tool_result"`` with ``"result"`` (not ``"content"``) for tool output
    - ``"event":"end"`` for stream termination (not ``"done"`` or ``"result"``)
    - ``"event":"error"`` with ``"message"`` for errors

Contracts validated here:
  - Provider-specific event keys map to canonical StreamEvent kinds.
  - ``role="user"`` messages are silently skipped (no event emitted).
  - Tool correlation via ``"id"``/``"callID"`` key.
  - ``"args"``/``"state.input"`` dict is stored in tool_args unchanged.
  - Unknown event values → empty iterable, no exception.
  - Synthetic golden fixture covers message, tool_call, tool_result, error, end.
  - Real capture fixtures validate the production ``"type"``/``"part"`` wire format.
"""

from __future__ import annotations

import json
from pathlib import Path

from tero2.providers.normalizers.opencode import OpenCodeNormalizer

FIXTURE_DIR = Path(__file__).parent / "fixtures"
CAPTURES_DIR = FIXTURE_DIR / "captures"


def _load(name: str) -> list[dict]:
    """Load a .jsonl fixture, skipping blank lines and // comments."""
    out = []
    for line in (FIXTURE_DIR / name).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        out.append(json.loads(s))
    return out


# ── message events (event="message", role="assistant") ───────────────────────


def test_opencode_message_kind() -> None:
    """OpenCode assistant messages must produce kind='text'."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "message", "role": "assistant", "text": "Starting."},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "text"


def test_opencode_message_content_from_text_key() -> None:
    """Text content must come from the 'text' field."""
    n = OpenCodeNormalizer()
    msg = "Starting implementation of the requested changes."
    out = list(n.normalize(
        {"event": "message", "role": "assistant", "text": msg},
        role="builder",
    ))
    assert out[0].content == msg


def test_opencode_user_message_skipped() -> None:
    """event='message' with role='user' must yield nothing."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "message", "role": "user", "text": "user prompt"},
        role="builder",
    ))
    assert out == []


def test_opencode_message_role_on_event() -> None:
    """SORA role must be stored on the emitted event."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "message", "role": "assistant", "text": "x"},
        role="verifier",
    ))
    assert out[0].role == "verifier"


# ── tool_call events (OpenCode uses "args", not "input") ─────────────────────


def test_opencode_tool_call_kind() -> None:
    """OpenCode tool_call events must produce kind='tool_use'."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_call", "name": "bash", "args": {}, "id": "oc_001"},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "tool_use"


def test_opencode_tool_call_name() -> None:
    """tool_name must come from the 'name' field."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_call", "name": "bash", "args": {}, "id": "oc_001"},
        role="builder",
    ))
    assert out[0].tool_name == "bash"


def test_opencode_tool_call_id_oc_prefix() -> None:
    """OpenCode IDs use 'oc_tool_NNN' prefix — must be preserved verbatim."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_call", "name": "bash", "args": {}, "id": "oc_tool_001"},
        role="builder",
    ))
    assert out[0].tool_id == "oc_tool_001"


def test_opencode_tool_call_args_dict() -> None:
    """OpenCode uses 'args' (not 'input') — must land in tool_args unchanged."""
    args = {"command": "find . -name '*.py' -not -path './.venv/*'"}
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_call", "name": "bash", "args": args, "id": "oc_001"},
        role="builder",
    ))
    assert out[0].tool_args == args


def test_opencode_write_tool() -> None:
    """write tool must normalise with tool_name='write'."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_call", "name": "write",
         "args": {"path": "/repo/out.py", "content": "# generated\n"},
         "id": "oc_tool_002"},
        role="builder",
    ))
    assert out[0].tool_name == "write"
    assert out[0].tool_args["path"] == "/repo/out.py"


# ── tool_result events (OpenCode uses "result", not "content") ───────────────


def test_opencode_tool_result_kind() -> None:
    """OpenCode tool_result events must produce kind='tool_result'."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_result", "id": "oc_001", "result": "out"},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "tool_result"


def test_opencode_tool_result_output_from_result_key() -> None:
    """tool_output must come from 'result' key (OpenCode-specific, not 'content')."""
    text = "./tero2/runner.py\n./tero2/config.py\n./tero2/events.py"
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_result", "id": "oc_001", "result": text},
        role="builder",
    ))
    assert out[0].tool_output == text


def test_opencode_tool_result_id_correlation() -> None:
    """tool_id on the result must match the originating tool_call id."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "tool_result", "id": "oc_tool_002", "result": "File written."},
        role="builder",
    ))
    assert out[0].tool_id == "oc_tool_002"


# ── end event (OpenCode stream terminator) ────────────────────────────────────


def test_opencode_end_is_turn_end() -> None:
    """OpenCode 'end' event must produce kind='turn_end'."""
    n = OpenCodeNormalizer()
    out = list(n.normalize({"event": "end"}, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "turn_end"


# ── error events ──────────────────────────────────────────────────────────────


def test_opencode_error_kind() -> None:
    """OpenCode error events must produce kind='error'."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "error", "message": "model returned empty response"},
        role="builder",
    ))
    assert len(out) == 1
    assert out[0].kind == "error"


def test_opencode_error_content_from_message_key() -> None:
    """Error content must come from the 'message' key."""
    n = OpenCodeNormalizer()
    out = list(n.normalize(
        {"event": "error", "message": "model returned empty response"},
        role="builder",
    ))
    assert out[0].content == "model returned empty response"


# ── unknown event → empty iterable ────────────────────────────────────────────


def test_opencode_unknown_event_yields_nothing() -> None:
    """Unrecognised OpenCode event values must yield nothing (no exception)."""
    n = OpenCodeNormalizer()
    out = list(n.normalize({"event": "heartbeat", "seq": 1}, role="builder"))
    assert out == []


def test_opencode_type_key_without_part_yields_nothing() -> None:
    """A 'type=text' dict without the 'part' envelope must yield nothing.

    Real OpenCode 1.4.0 text events carry content inside a 'part' object.
    A bare {'type':'text','text':'...'} (no 'part' key) does not match
    the real format and must be silently skipped.
    """
    n = OpenCodeNormalizer()
    out = list(n.normalize({"type": "text", "text": "wrong format"}, role="builder"))
    assert out == []


def test_opencode_non_dict_raw_yields_error() -> None:
    """Non-dict raw must yield an error event (bug 126: silent data loss)."""
    n = OpenCodeNormalizer()
    out = list(n.normalize("not a dict", role="builder"))
    assert len(out) == 1
    assert out[0].kind == "error"


# ── golden fixture round-trip ─────────────────────────────────────────────────


def test_opencode_golden_fixture_happy() -> None:
    """Fixture opencode.jsonl must produce text, tool_use, tool_result, turn_end, error."""
    n = OpenCodeNormalizer()
    events = []
    for raw in _load("opencode.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    kinds = {e.kind for e in events}
    assert "text" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert "turn_end" in kinds
    assert "error" in kinds


def test_opencode_fixture_user_messages_skipped() -> None:
    """Any user-role message lines in the fixture must produce no events."""
    n = OpenCodeNormalizer()
    for raw in _load("opencode.jsonl"):
        if raw.get("event") == "message" and raw.get("role") == "user":
            out = list(n.normalize(raw, role="builder"))
            assert out == [], f"user message unexpectedly produced events: {out}"


def test_opencode_fixture_end_is_last() -> None:
    """The last raw line in opencode.jsonl must be the 'end' event."""
    raws = _load("opencode.jsonl")
    assert raws[-1].get("event") == "end"


def test_opencode_unknown_model_fixture_yields_error() -> None:
    """opencode_unknown_model.jsonl (bad model name) must produce at least one kind='error' event."""
    n = OpenCodeNormalizer()
    events = []
    for raw in _load("opencode_unknown_model.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    assert any(e.kind == "error" for e in events), (
        "expected at least one error event from unknown-model fixture"
    )


# ── real OpenCode 1.4.0 capture fixtures (type/part wire format) ──────────────


def _load_capture(name: str) -> list[dict]:
    """Load a .jsonl from fixtures/captures/, skipping blank lines and // comments."""
    out = []
    for line in (CAPTURES_DIR / name).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        out.append(json.loads(s))
    return out


def test_opencode_real_capture_produces_text_tool_use_turn_end() -> None:
    """Real opencode 1.4.0 capture must produce text, tool_use, and turn_end events.

    The capture (fixtures/captures/opencode.jsonl) uses the real wire format:
    type=step_start/tool_use/text/step_finish.  Verifies that the normalizer
    handles these type/part envelopes rather than silently dropping them.
    """
    n = OpenCodeNormalizer()
    events = []
    for raw in _load_capture("opencode.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    kinds = {e.kind for e in events}
    assert "text" in kinds, f"expected text event, got kinds={kinds}"
    assert "tool_use" in kinds, f"expected tool_use event, got kinds={kinds}"
    assert "turn_end" in kinds, f"expected turn_end event (from step_finish reason=stop), got kinds={kinds}"


def test_opencode_real_unknown_model_capture_yields_error() -> None:
    """Real captures/opencode_unknown_model.jsonl must produce kind='error'.

    The real capture uses {'type':'error','error':{'data':{'message':'...'}}}
    (not the synthetic {'event':'error','message':'...'} shape).
    Validates the production unknown-model failure path.
    """
    n = OpenCodeNormalizer()
    events = []
    for raw in _load_capture("opencode_unknown_model.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    assert any(e.kind == "error" for e in events), (
        "expected at least one error event from real unknown-model capture"
    )
    error_events = [e for e in events if e.kind == "error"]
    assert "not found" in error_events[0].content.lower() or "mimo" in error_events[0].content.lower(), (
        f"error content should mention the missing model, got: {error_events[0].content!r}"
    )
