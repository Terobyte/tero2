"""Normalizer tests for the Zai provider (Claude Agent SDK via api.z.ai).

Tests ``ZaiNormalizer`` against both dict inputs and duck-typed SDK objects.
Zai uses the Claude Agent SDK which streams SDK message objects.  The
``ZaiNormalizer`` handles both forms via duck-typing so the same logic runs
whether the pipeline passes plain dicts (from fixtures / intermediate stages)
or real SDK objects (from the live Zai connection).

Contracts validated here:
  Step 1  — text events from SDK messages normalise correctly
  Step 2  — thinking blocks from Zai (GLM-5.1 + extended thinking)
  Step 3  — tool_use events from SDK messages
  Step 4  — tool_result with plain string content
  Step 5  — tool_result with list content (SDK wraps in content list)
  Step 6  — status events from SDK pipeline
  Step 7  — error events with 'error' key (Zai API errors)
  Step 8  — turn_end terminates the stream
  Step 9  — raw dict preserved on every event
  Step 10 — fixture round-trip: all fixture events normalise without raising
  Step 11 — duck-typed SDK objects (not dicts) normalise correctly
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from tero2.providers.normalizers.zai import ZaiNormalizer

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


class _SdkMsg:
    """Minimal duck-typed SDK object — NOT a dict.

    Models the attribute-access pattern of real Anthropic SDK message objects
    (e.g. ``anthropic.types.TextBlock``, ``anthropic.types.ToolUseBlock``).
    ``ZaiNormalizer`` must handle these via ``getattr()`` in ``_get()``.
    """

    def __init__(self, **kwargs: object) -> None:
        for k, v in kwargs.items():
            object.__setattr__(self, k, v)


# ── Step 1: text events ──────────────────────────────────────────────────────


class TestTextEvents:
    def test_text_kind(self) -> None:
        """Zai SDK text messages must produce kind='text'."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "text", "text": "Processing your request via GLM-5.1."},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "text"

    def test_text_content_exact(self) -> None:
        """Content must match the 'text' field exactly."""
        msg = "Processing your request via GLM-5.1."
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "text", "text": msg}, role="builder"))
        assert out[0].content == msg

    def test_text_role_stored(self) -> None:
        """Role ('builder', 'scout', etc.) must be stored on the event."""
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "text", "text": "x"}, role="verifier"))
        assert out[0].role == "verifier"


# ── Step 2: thinking blocks ──────────────────────────────────────────────────


class TestThinkingBlocks:
    def test_thinking_kind(self) -> None:
        """Zai thinking blocks (via SDK extended thinking) must produce kind='thinking'."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "thinking", "thinking": "I should read the runner first."},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "thinking"

    def test_thinking_content(self) -> None:
        """Thinking content must come from the 'thinking' field."""
        thought = "The user wants me to analyze the runner. I should start by reading the file."
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "thinking", "thinking": thought}, role="builder"))
        assert out[0].content == thought


# ── Step 3: tool_use events ──────────────────────────────────────────────────


class TestToolUseEvents:
    def test_tool_use_kind(self) -> None:
        """SDK tool_use events must produce kind='tool_use'."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "zai_toolu_01", "name": "bash",
             "input": {"command": "python -c \"import tero2\""}},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "tool_use"

    def test_tool_use_id_zai_prefix(self) -> None:
        """Zai SDK IDs (zai_toolu_NN) must be preserved verbatim."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "zai_toolu_01", "name": "bash", "input": {}},
            role="builder",
        ))
        assert out[0].tool_id == "zai_toolu_01"

    def test_tool_use_bash_name(self) -> None:
        """tool_name must come from the 'name' field."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "zai_toolu_01", "name": "bash", "input": {}},
            role="builder",
        ))
        assert out[0].tool_name == "bash"

    def test_tool_use_read_tool(self) -> None:
        """read tool must normalise with tool_name='read'."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "zai_toolu_02", "name": "read",
             "input": {"file_path": "/repo/tero2/__init__.py"}},
            role="builder",
        ))
        assert out[0].tool_name == "read"
        assert out[0].tool_args["file_path"] == "/repo/tero2/__init__.py"


# ── Step 4: tool_result with plain string content ────────────────────────────


class TestToolResultString:
    def test_tool_result_kind(self) -> None:
        """tool_result must produce kind='tool_result'."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "tool_result", "tool_use_id": "zai_toolu_01", "content": "out"},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "tool_result"

    def test_tool_result_output(self) -> None:
        """tool_output must equal the 'content' string."""
        path = "/Users/terobyte/Desktop/Projects/Active/tero2/tero2/__init__.py"
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "tool_result", "tool_use_id": "zai_toolu_01", "content": path},
            role="builder",
        ))
        assert out[0].tool_output == path

    def test_tool_result_id(self) -> None:
        """tool_id must come from the 'tool_use_id' field."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "tool_result", "tool_use_id": "zai_toolu_01", "content": ""},
            role="builder",
        ))
        assert out[0].tool_id == "zai_toolu_01"


# ── Step 5: tool_result with list content ────────────────────────────────────


class TestToolResultListContent:
    def test_list_content_joined(self) -> None:
        """Zai SDK wraps file content in a list — must be joined to a string."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {
                "type": "tool_result",
                "tool_use_id": "zai_toolu_02",
                "content": [
                    {"type": "text", "text": "\"\"\"tero2 — autonomous agent orchestration.\"\"\"\n"}
                ],
            },
            role="builder",
        ))
        assert out[0].kind == "tool_result"
        assert "tero2" in out[0].tool_output

    def test_list_content_multiple_items(self) -> None:
        """Multiple list items must all appear in the joined output."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {
                "type": "tool_result",
                "tool_use_id": "zai_toolu_03",
                "content": [
                    {"type": "text", "text": "line one"},
                    {"type": "text", "text": "line two"},
                ],
            },
            role="builder",
        ))
        assert "line one" in out[0].tool_output
        assert "line two" in out[0].tool_output


# ── Step 6: status events ────────────────────────────────────────────────────


class TestStatusEvents:
    def test_status_kind(self) -> None:
        """Status events from Zai pipeline must produce kind='status'."""
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "status", "text": "Running tool: bash"}, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "status"

    def test_status_content(self) -> None:
        """Status content must come from the 'text' field."""
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "status", "text": "Running tool: bash"}, role="builder"))
        assert out[0].content == "Running tool: bash"


# ── Step 7: error with 'error' key (Zai API errors) ─────────────────────────


class TestErrorEvents:
    def test_error_kind(self) -> None:
        """Zai error events must produce kind='error'."""
        n = ZaiNormalizer()
        out = list(n.normalize(
            {"type": "error", "error": "GLM-5.1: upstream connection reset"},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "error"

    def test_error_content_from_error_key(self) -> None:
        """Content must come from 'error' key (Zai uses 'error' not 'text')."""
        msg = "GLM-5.1: upstream connection reset"
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "error", "error": msg}, role="builder"))
        assert out[0].content == msg


# ── Step 8: turn_end ─────────────────────────────────────────────────────────


class TestTurnEnd:
    def test_turn_end_kind(self) -> None:
        """turn_end emitted by Zai SDK wrapper must produce kind='turn_end'."""
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "turn_end"}, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "turn_end"

    def test_unknown_type_yields_nothing(self) -> None:
        """Unknown type values must yield nothing (no exception)."""
        n = ZaiNormalizer()
        out = list(n.normalize({"type": "heartbeat", "seq": 1}, role="builder"))
        assert out == []

    def test_none_raw_yields_nothing(self) -> None:
        """None input must yield nothing (no exception)."""
        n = ZaiNormalizer()
        out = list(n.normalize(None, role="builder"))
        assert out == []


# ── Step 9: raw dict preserved ───────────────────────────────────────────────


class TestRawPreserved:
    def test_raw_identity_preserved_for_dict(self) -> None:
        """For dict input, the 'raw' field must be the exact input dict object."""
        n = ZaiNormalizer()
        raw = {"type": "text", "text": "raw check"}
        out = list(n.normalize(raw, role="builder"))
        assert out[0].raw is raw

    def test_raw_is_dict_for_sdk_object(self) -> None:
        """For SDK object input, 'raw' on the event must be a dict (not the object)."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(type="text", text="sdk narration")
        out = list(n.normalize(sdk_obj, role="builder"))
        assert isinstance(out[0].raw, dict)


# ── Step 10: fixture round-trip ──────────────────────────────────────────────


class TestFixtureRoundTrip:
    def test_all_fixture_events_normalise(self) -> None:
        """Every line in fixtures/zai.jsonl must normalise without raising."""
        n = ZaiNormalizer()
        for raw in _load("zai.jsonl"):
            events = list(n.normalize(raw, role="builder"))
            for ev in events:
                assert ev.kind in {
                    "text", "tool_use", "tool_result", "thinking",
                    "status", "error", "turn_end",
                }

    def test_fixture_contains_thinking_event(self) -> None:
        """Zai fixture must include a thinking event (extended thinking via GLM-5.1)."""
        n = ZaiNormalizer()
        thinking_events = []
        for raw in _load("zai.jsonl"):
            for ev in n.normalize(raw, role="builder"):
                if ev.kind == "thinking":
                    thinking_events.append(ev)
        assert thinking_events, "fixture missing thinking event"

    def test_fixture_has_list_content_tool_result(self) -> None:
        """Fixture must include a tool_result with list content (SDK-style wrap)."""
        raws = _load("zai.jsonl")
        list_results = [
            r for r in raws
            if r.get("type") == "tool_result" and isinstance(r.get("content"), list)
        ]
        assert list_results, "fixture missing tool_result with list content"

    def test_fixture_error_uses_error_key(self) -> None:
        """Zai fixture error must use the 'error' key (not 'text') for API errors."""
        raws = _load("zai.jsonl")
        error_raws = [r for r in raws if r.get("type") == "error"]
        assert error_raws, "fixture missing error event"
        zai_errors = [r for r in error_raws if "error" in r]
        assert zai_errors, "fixture error event missing 'error' key (expected Zai API error shape)"

    def test_fixture_covers_main_kinds(self) -> None:
        """Golden fixture must produce text, thinking, tool_use, tool_result, status, error, turn_end."""
        n = ZaiNormalizer()
        kinds = set()
        for raw in _load("zai.jsonl"):
            for ev in n.normalize(raw, role="builder"):
                kinds.add(ev.kind)
        assert "text" in kinds
        assert "thinking" in kinds
        assert "tool_use" in kinds
        assert "tool_result" in kinds
        assert "status" in kinds
        assert "error" in kinds
        assert "turn_end" in kinds

    def test_fixture_turn_end_is_last(self) -> None:
        """The last raw line in zai.jsonl must be turn_end."""
        raws = _load("zai.jsonl")
        assert raws[-1].get("type") == "turn_end"


# ── Step 11: duck-typed SDK objects ──────────────────────────────────────────


class TestSdkObjectDuckTyping:
    """Tests that ZaiNormalizer works with non-dict SDK objects via getattr()."""

    def test_sdk_text_object_produces_text_event(self) -> None:
        """SDK text object (not a dict) must produce kind='text'."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(type="text", text="SDK narration from GLM-5.1")
        out = list(n.normalize(sdk_obj, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "text"
        assert out[0].content == "SDK narration from GLM-5.1"

    def test_sdk_thinking_object(self) -> None:
        """SDK thinking object must produce kind='thinking' with correct content."""
        n = ZaiNormalizer()
        thought = "Should I read tero2/runner.py first?"
        sdk_obj = _SdkMsg(type="thinking", thinking=thought)
        out = list(n.normalize(sdk_obj, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "thinking"
        assert out[0].content == thought

    def test_sdk_tool_use_object(self) -> None:
        """SDK tool_use object must produce kind='tool_use' with correct fields."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(
            type="tool_use",
            id="zai_toolu_01",
            name="bash",
            input={"command": "ls -la"},
        )
        out = list(n.normalize(sdk_obj, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "tool_use"
        assert out[0].tool_id == "zai_toolu_01"
        assert out[0].tool_name == "bash"
        assert out[0].tool_args == {"command": "ls -la"}

    def test_sdk_tool_result_object_string_content(self) -> None:
        """SDK tool_result object with string content must produce kind='tool_result'."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(
            type="tool_result",
            tool_use_id="zai_toolu_01",
            content="tero2/__init__.py",
        )
        out = list(n.normalize(sdk_obj, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "tool_result"
        assert out[0].tool_id == "zai_toolu_01"
        assert out[0].tool_output == "tero2/__init__.py"

    def test_sdk_error_object(self) -> None:
        """SDK error object must produce kind='error' reading from 'error' attribute."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(type="error", error="connection reset by peer")
        out = list(n.normalize(sdk_obj, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "error"
        assert out[0].content == "connection reset by peer"

    def test_sdk_turn_end_object(self) -> None:
        """SDK turn_end object must produce kind='turn_end'."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(type="turn_end")
        out = list(n.normalize(sdk_obj, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "turn_end"

    def test_sdk_object_raw_field_is_dict(self) -> None:
        """StreamEvent.raw must be a dict even when input is an SDK object."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(type="text", text="hello")
        out = list(n.normalize(sdk_obj, role="builder"))
        assert isinstance(out[0].raw, dict), "raw must be dict, not SDK object"
        assert out[0].raw.get("type") == "text"
        assert out[0].raw.get("text") == "hello"

    def test_sdk_role_stored_on_event(self) -> None:
        """SORA role must be stored on events produced from SDK objects."""
        n = ZaiNormalizer()
        sdk_obj = _SdkMsg(type="text", text="x")
        out = list(n.normalize(sdk_obj, role="scout"))
        assert out[0].role == "scout"
