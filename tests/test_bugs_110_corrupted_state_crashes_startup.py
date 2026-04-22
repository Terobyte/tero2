"""Bug 110: corrupted STATE.json crashed the runner on startup.

``AgentState.from_file`` caught ``OSError`` and ``UnicodeDecodeError`` and
degraded to a fresh state, but ``AgentState.from_json`` — called with the
file's content — raises ``ValueError`` on either bad JSON or a non-dict
top level, and that exception was never caught at the ``from_file``
layer. A corrupted or hand-edited ``STATE.json`` therefore propagated a
``ValueError`` through ``Runner.run()`` and killed the process before it
even reached the idle loop.

Losing state is bad, but a fresh state with a loud warning is strictly
better than a crash loop: the next save overwrites the bad file, and the
operator can resume from whatever checkpoint the rest of the disk still
carries. ``from_file`` now catches ``ValueError`` the same way it catches
``OSError`` — log at error level and return ``cls()``.

Halal pair: each test produces a corruption shape that previously
triggered the unhandled ``ValueError``. Without the fix they would all
raise; with the fix they all return a fresh ``AgentState``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tero2.state import AgentState, Phase, SoraPhase


class TestCorruptedJsonDoesNotCrash:
    """Several corruption shapes, each returning a fresh default state."""

    def test_invalid_json_syntax(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = tmp_path / "STATE.json"
        p.write_text("{not valid json at all", encoding="utf-8")

        with caplog.at_level(logging.ERROR, logger="tero2.state"):
            state = AgentState.from_file(p)

        assert state.phase == Phase.IDLE, (
            "invalid JSON must degrade to fresh default state"
        )
        errors = [
            r.getMessage() for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert any("corrupted" in m.lower() for m in errors), (
            f"corruption must produce a loud error log, got: {errors!r}"
        )

    def test_list_at_top_level(self, tmp_path: Path) -> None:
        """from_json raises ValueError when top-level is not a dict — that's
        the exact shape that escaped from_file's except clause."""
        p = tmp_path / "STATE.json"
        p.write_text('["this", "is", "a", "list"]', encoding="utf-8")

        state = AgentState.from_file(p)
        assert state.phase == Phase.IDLE

    def test_scalar_at_top_level(self, tmp_path: Path) -> None:
        p = tmp_path / "STATE.json"
        p.write_text("42", encoding="utf-8")

        state = AgentState.from_file(p)
        assert state.phase == Phase.IDLE

    def test_null_at_top_level(self, tmp_path: Path) -> None:
        p = tmp_path / "STATE.json"
        p.write_text("null", encoding="utf-8")

        state = AgentState.from_file(p)
        assert state.phase == Phase.IDLE

    def test_empty_file(self, tmp_path: Path) -> None:
        p = tmp_path / "STATE.json"
        p.write_text("", encoding="utf-8")

        state = AgentState.from_file(p)
        assert state.phase == Phase.IDLE


class TestValidStateStillLoadsUnchanged:
    """Regression: the fix must not affect the happy path."""

    def test_valid_state_round_trips(self, tmp_path: Path) -> None:
        p = tmp_path / "STATE.json"
        original = AgentState(
            phase=Phase.RUNNING, sora_phase=SoraPhase.EXECUTE, retry_count=3
        )
        original.save(p)

        loaded = AgentState.from_file(p)
        assert loaded.phase == Phase.RUNNING
        assert loaded.sora_phase == SoraPhase.EXECUTE
        assert loaded.retry_count == 3


class TestMissingFileStillReturnsFresh:
    """The previous behaviour for a completely missing file must be
    preserved — it was already handled via OSError and must remain
    a WARNING rather than an ERROR."""

    def test_missing_file_returns_fresh_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        p = tmp_path / "does_not_exist.json"

        with caplog.at_level(logging.WARNING, logger="tero2.state"):
            state = AgentState.from_file(p)

        assert state.phase == Phase.IDLE
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("cannot read" in m for m in warnings), (
            f"missing file stays at WARNING level, got: {warnings!r}"
        )
        # And conversely — should NOT log at ERROR level for missing.
        errors = [
            r.getMessage() for r in caplog.records if r.levelno == logging.ERROR
        ]
        assert not any("corrupted" in m.lower() for m in errors), (
            f"missing file must not be tagged as corruption, got: {errors!r}"
        )
