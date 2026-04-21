"""Normalizer tests for the Kilo CLI provider (kilo run --format json).

Tests ``KiloNormalizer`` against the real raw shapes emitted by
``kilo run --format json``.  Kilo uses a ``"type"`` discriminator key (like
Claude/Codex) but with its own conventions:

  - Sequential numeric IDs prefixed ``kilo_NN`` for tool correlation.
  - ``"input"`` for tool arguments (same as Claude, unlike OpenCode ``"args"``).
  - ``"tool_use_id"`` for result correlation.
  - ``"content"`` may be a list of ``{"type":"text","text":"..."}`` blocks.
  - Unknown ``"type"`` values fall back to ``kind="text"`` (never raise).

Contracts validated here:
  Step 1 — text events normalise correctly
  Step 2 — status with 'text' key
  Step 3 — tool_use with kilo-style sequential numeric IDs
  Step 4 — tool_result with list content (Kilo wraps output in content list)
  Step 5 — kilo-specific unknown event type falls back to text
  Step 6 — error with 'error' key
  Step 7 — turn_end terminates the stream
  Step 8 — fixture round-trip: all fixture events normalise without raising
"""

from __future__ import annotations

import json
from pathlib import Path

from tero2.providers.normalizers.kilo import KiloNormalizer

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


# ── Step 1: text events ──────────────────────────────────────────────────────


class TestTextEvents:
    def test_text_kind(self) -> None:
        """Kilo text events must produce kind='text'."""
        n = KiloNormalizer()
        out = list(n.normalize({"type": "text", "text": "I'll inspect the project."}, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "text"

    def test_text_content_from_text_key(self) -> None:
        """Content must come from the 'text' field."""
        msg = "I'll inspect the project and propose changes."
        n = KiloNormalizer()
        out = list(n.normalize({"type": "text", "text": msg}, role="builder"))
        assert out[0].content == msg

    def test_text_role_preserved(self) -> None:
        """Role must be stored on the event."""
        n = KiloNormalizer()
        out = list(n.normalize({"type": "text", "text": "x"}, role="architect"))
        assert out[0].role == "architect"


# ── Step 2: status events ────────────────────────────────────────────────────


class TestStatusEvents:
    def test_status_kind(self) -> None:
        """Kilo status events must produce kind='status'."""
        n = KiloNormalizer()
        out = list(n.normalize({"type": "status", "text": "Initializing session..."}, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "status"

    def test_status_content_from_text_key(self) -> None:
        """Status content must come from the 'text' field."""
        n = KiloNormalizer()
        out = list(n.normalize({"type": "status", "text": "Initializing session..."}, role="builder"))
        assert out[0].content == "Initializing session..."


# ── Step 3: tool_use with kilo numeric IDs ───────────────────────────────────


class TestToolUseKilo:
    def test_tool_use_kind(self) -> None:
        """tool_use events must produce kind='tool_use'."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "kilo_01", "name": "bash", "input": {}},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "tool_use"

    def test_tool_use_kilo_id_preserved(self) -> None:
        """Kilo numeric IDs (kilo_NN) must be preserved as-is."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "kilo_01", "name": "bash", "input": {}},
            role="builder",
        ))
        assert out[0].tool_id == "kilo_01"

    def test_tool_use_bash_command(self) -> None:
        """Bash tool input must be carried in tool_args."""
        inp = {"command": "cat pyproject.toml"}
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "kilo_01", "name": "bash", "input": inp},
            role="builder",
        ))
        assert out[0].tool_args == inp

    def test_tool_use_glob_tool(self) -> None:
        """Glob tool must normalise with tool_name='glob'."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "tool_use", "id": "kilo_02", "name": "glob",
             "input": {"pattern": "tests/**/*.py"}},
            role="builder",
        ))
        assert out[0].tool_name == "glob"
        assert out[0].tool_args["pattern"] == "tests/**/*.py"


# ── Step 4: tool_result with list content ────────────────────────────────────


class TestToolResultListContent:
    def test_list_content_normalised(self) -> None:
        """Kilo wraps tool output in a content list — must be joined into a string."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {
                "type": "tool_result",
                "tool_use_id": "kilo_02",
                "content": [
                    {"type": "text", "text": "tests/test_bugs.py"},
                    {"type": "text", "text": "tests/test_runner_sora.py"},
                ],
            },
            role="builder",
        ))
        assert out[0].kind == "tool_result"
        assert "tests/test_bugs.py" in out[0].tool_output
        assert "tests/test_runner_sora.py" in out[0].tool_output

    def test_list_content_id_preserved(self) -> None:
        """tool_id must be set from tool_use_id even with list content."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "tool_result", "tool_use_id": "kilo_02",
             "content": [{"type": "text", "text": "item"}]},
            role="builder",
        ))
        assert out[0].tool_id == "kilo_02"

    def test_plain_string_content(self) -> None:
        """Plain string content must also work (fallback for string-only responses)."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "tool_result", "tool_use_id": "kilo_01",
             "content": "[project]\nname = \"tero2\""},
            role="builder",
        ))
        assert out[0].tool_output == "[project]\nname = \"tero2\""


# ── Step 5: kilo-specific unknown event type ─────────────────────────────────


class TestKiloSpecificUnknown:
    def test_kilo_internal_checkpoint_falls_back_to_text(self) -> None:
        """'kilo_internal_checkpoint' is Kilo-specific — must fall back to text."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "kilo_internal_checkpoint", "text": "checkpoint saved"},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "text"

    def test_unknown_kilo_type_text_preserved(self) -> None:
        """Text content from unknown Kilo types must land in content."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "kilo_internal_checkpoint", "text": "checkpoint saved"},
            role="builder",
        ))
        assert out[0].content == "checkpoint saved"

    def test_kilo_session_event_falls_back(self) -> None:
        """Any unrecognised Kilo event must not raise and yield one text event."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "kilo_session_start", "session_id": "abc123"},
            role="builder",
        ))
        assert out[0].kind == "text"

    def test_non_dict_raw_yields_nothing(self) -> None:
        """Non-dict raw value must yield nothing (no exception)."""
        n = KiloNormalizer()
        out = list(n.normalize("not a dict", role="builder"))
        assert out == []


# ── Step 6: error with 'error' key ───────────────────────────────────────────


class TestErrorEvents:
    def test_error_kind(self) -> None:
        """Error events must produce kind='error'."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "error", "error": "tool execution timeout after 30s"},
            role="builder",
        ))
        assert len(out) == 1
        assert out[0].kind == "error"

    def test_error_content_from_error_key(self) -> None:
        """Content must come from 'error' key when 'text' is absent."""
        n = KiloNormalizer()
        out = list(n.normalize(
            {"type": "error", "error": "tool execution timeout after 30s"},
            role="builder",
        ))
        assert out[0].content == "tool execution timeout after 30s"


# ── Step 7: turn_end ─────────────────────────────────────────────────────────


class TestTurnEnd:
    def test_turn_end_kind(self) -> None:
        """turn_end must produce kind='turn_end'."""
        n = KiloNormalizer()
        out = list(n.normalize({"type": "turn_end"}, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "turn_end"


# ── Step 8: fixture round-trip ───────────────────────────────────────────────


class TestFixtureRoundTrip:
    def test_all_fixture_events_normalise(self) -> None:
        """Every line in fixtures/kilo.jsonl must normalise without raising."""
        n = KiloNormalizer()
        for raw in _load("kilo.jsonl"):
            events = list(n.normalize(raw, role="builder"))
            for ev in events:
                assert ev.kind in {
                    "text", "tool_use", "tool_result", "thinking",
                    "status", "error", "turn_end",
                }

    def test_fixture_has_glob_tool(self) -> None:
        """Fixture must include a glob tool_use (Kilo-specific tool)."""
        n = KiloNormalizer()
        raws = _load("kilo.jsonl")
        glob_events = []
        for raw in raws:
            for ev in n.normalize(raw, role="builder"):
                if ev.kind == "tool_use" and ev.tool_name == "glob":
                    glob_events.append(ev)
        assert glob_events, "fixture missing glob tool_use"

    def test_fixture_has_list_content_result(self) -> None:
        """Fixture must include at least one tool_result with list content."""
        raws = _load("kilo.jsonl")
        list_results = [
            r for r in raws
            if r.get("type") == "tool_result" and isinstance(r.get("content"), list)
        ]
        assert list_results, "fixture missing tool_result with list content"

    def test_fixture_unknown_type_falls_back(self) -> None:
        """Kilo-specific unknown event in fixture must normalise as text."""
        n = KiloNormalizer()
        raws = _load("kilo.jsonl")
        unknown = [r for r in raws if r.get("type") == "kilo_internal_checkpoint"]
        assert unknown, "fixture missing kilo_internal_checkpoint event"
        out = list(n.normalize(unknown[0], role="builder"))
        assert out[0].kind == "text"

    def test_fixture_covers_all_main_kinds(self) -> None:
        """Golden fixture must produce text, status, tool_use, tool_result, error, turn_end."""
        n = KiloNormalizer()
        kinds = set()
        for raw in _load("kilo.jsonl"):
            for ev in n.normalize(raw, role="builder"):
                kinds.add(ev.kind)
        assert "text" in kinds
        assert "status" in kinds
        assert "tool_use" in kinds
        assert "tool_result" in kinds
        assert "error" in kinds
        assert "turn_end" in kinds

    def test_fixture_turn_end_is_last(self) -> None:
        """The last raw line in kilo.jsonl must be the turn_end event."""
        raws = _load("kilo.jsonl")
        assert raws[-1].get("type") == "turn_end"
