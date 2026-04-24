"""Autonomous bug-loop iteration 3.

Three real bugs in unexplored areas:

1. CodexNestedErrorDict
   Where:  tero2/providers/normalizers/codex.py, ``normalize()`` error branch.
   What:   When Codex emits an error whose ``"error"`` key holds a nested
           ``{"message": "..."}`` dict (Claude-compatible shape), the normalizer
           sets ``StreamEvent.content`` to the dict instead of extracting the
           string. Downstream TUI code treats ``content`` as ``str`` — this
           leads to ``TypeError`` or shows ``"{'message': '...'}"`` to the user.
   Fix:    Unwrap ``err.get("message")`` when ``raw["error"]`` is a dict, same
           as the Claude normalizer does.

2. CatalogCacheVersionMismatch
   Where:  tero2/providers/catalog.py, ``_load_cache()``.
   What:   ``ModelEntry(**e)`` is called unconditionally; if the cached JSON
           was written by a newer/older version of tero2 with an extra field
           (e.g. ``"deprecated"``), ``ModelEntry`` raises ``TypeError`` which
           is NOT caught by the ``(FileNotFoundError, KeyError,
           json.JSONDecodeError, ValueError)`` handler. This bubbles up through
           ``get_models()`` and crashes the catalogue.
   Fix:    Either add ``TypeError`` to the caught exceptions, or strip unknown
           keys before calling ``ModelEntry(**e)``.

3. CheckpointMarkStartedFromRunning
   Where:  tero2/checkpoint.py, ``mark_started()``.
   What:   Crash recovery: when the on-disk state is ``Phase.RUNNING`` (e.g.
           the previous run was SIGKILLed), ``mark_started()`` tries to set
           ``state.phase = Phase.IDLE`` before calling ``_transition``. But
           ``AgentState.__setattr__`` validates every phase assignment against
           ``_PHASE_VALID_NEXT`` — and ``RUNNING → IDLE`` is NOT valid. The
           result: recovery path immediately raises ``StateTransitionError``,
           preventing any run from starting until the state file is hand-fixed.
   Fix:    Use ``object.__setattr__(state, "phase", Phase.IDLE)`` to bypass the
           transition guard for the crash-recovery adjustment (or use
           ``state.phase = Phase.FAILED`` first, since RUNNING → FAILED is
           valid, and then FAILED → RUNNING via ``_transition``).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tero2.checkpoint import CheckpointManager
from tero2.disk_layer import DiskLayer
from tero2.errors import StateTransitionError
from tero2.providers.normalizers.codex import CodexNormalizer
from tero2.state import AgentState, Phase


# ── Bug 1: Codex nested-error-dict ───────────────────────────────────────────


class TestLoopIter3CodexNestedErrorDict:
    """Codex error with nested dict leaks a dict into StreamEvent.content."""

    def test_codex_error_with_nested_dict_puts_dict_in_content(self) -> None:
        """Expected: content is the extracted string.  Actual: content is a dict."""
        n = CodexNormalizer()
        raw = {"type": "error", "error": {"message": "upstream failed"}}
        out = list(n.normalize(raw, role="builder"))
        assert len(out) == 1
        assert out[0].kind == "error"
        # REAL bug: content is the dict, not the message string.
        # This assertion captures the correct behaviour — currently fails.
        assert isinstance(out[0].content, str), (
            f"content must be str, got {type(out[0].content).__name__}: "
            f"{out[0].content!r}"
        )
        assert out[0].content == "upstream failed"


# ── Bug 2: Catalog cache version mismatch ────────────────────────────────────


class TestLoopIter3CatalogCacheVersionMismatch:
    """_load_cache raises TypeError when cache JSON has extra fields."""

    def test_cache_with_extra_field_does_not_crash(self, tmp_path: Path) -> None:
        """A cache file with an unknown field (schema evolution) must be treated
        as corrupt (return ``None``), not raise ``TypeError``."""
        import tero2.providers.catalog as cat

        # Redirect cache to a fresh temp dir so real user cache is untouched.
        original_cache_dir = cat._CACHE_DIR
        cat._CACHE_DIR = tmp_path
        try:
            p = tmp_path / "kilo_models.json"
            data = {
                "fetched_at": datetime.now(timezone.utc).isoformat(),
                "entries": [
                    {
                        "id": "gpt-4o",
                        "label": "GPT-4o",
                        # Hypothetical new field added in a later tero2 version
                        # (or in an in-progress branch on another machine).
                        "deprecated": False,
                    }
                ],
            }
            p.write_text(json.dumps(data), encoding="utf-8")

            # Expected: returns None (treat as corrupt, refetch).
            # Actual: raises TypeError from ModelEntry(**e).
            try:
                result = cat._load_cache("kilo")
            except TypeError as e:
                pytest.fail(
                    f"_load_cache must tolerate cache-version mismatch and "
                    f"return None; instead raised TypeError: {e}"
                )
            assert result is None, (
                f"_load_cache should return None for unknown-field cache; "
                f"got {result!r}"
            )
        finally:
            cat._CACHE_DIR = original_cache_dir


# ── Bug 3: Checkpoint mark_started from RUNNING ─────────────────────────────


class TestLoopIter3CheckpointMarkStartedFromRunning:
    """mark_started crashes when prior phase is RUNNING (crash recovery path)."""

    def test_mark_started_recovers_from_prior_running_phase(
        self, tmp_path: Path
    ) -> None:
        """Scenario: previous tero2 run was SIGKILLed while Phase.RUNNING. On
        restart, ``mark_started`` must recover without raising."""
        disk = DiskLayer(tmp_path)
        mgr = CheckpointManager(disk)

        # Simulate a crashed RUNNING state left on disk.
        prior = AgentState()
        prior.phase = Phase.RUNNING
        prior.retry_count = 5
        prior.current_task = "task-42"
        prior.steps_in_task = 7
        mgr.save(prior)

        # Expected: mark_started succeeds, new phase is RUNNING, context preserved.
        # Actual: StateTransitionError raised on the RUNNING→IDLE downgrade in
        # checkpoint.py (the intermediate step meant to route through _transition).
        try:
            restored = mgr.mark_started("plan.md")
        except StateTransitionError as e:
            pytest.fail(
                f"mark_started must recover from a prior RUNNING phase; "
                f"instead raised StateTransitionError: {e}"
            )
        assert restored.phase == Phase.RUNNING
        assert restored.retry_count == 5, (
            "crash-recovery must preserve retry_count; the bug trigger path "
            "also clobbers accumulated context on the way to crashing."
        )
        assert restored.current_task == "task-42"
