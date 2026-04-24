"""Autonomous bug-loop iteration 5.

Three real bugs in unexplored areas:

1. HistoryLoseAllOnOneBadEntry
   Where:  tero2/history.py, ``load_history``.
   What:   ``load_history`` reads ~/.tero2/history.json and constructs
           ``HistoryEntry`` instances via ``[HistoryEntry(**e) for e in ...]``.
           A SINGLE malformed entry (extra field, missing field, wrong type)
           triggers a ``TypeError`` inside the list comprehension, which is
           caught by the outer ``except (..., TypeError, ...)``. Because the
           exception aborts the ENTIRE comprehension, ALL good entries in the
           list are also lost — ``load_history`` returns ``[]`` even when most
           entries were valid. Users with a single corrupt history entry
           effectively lose their entire project-run history for display.
   Fix:    Iterate entries individually and skip bad ones with a per-entry
           try/except, logging and dropping ONLY the malformed record.

2. ArchitectValidatePlanAllowsDuplicateTaskIds
   Where:  tero2/players/architect.py, ``validate_plan`` / ``_parse_slice_plan``.
   What:   ``validate_plan`` scans each ``## T0X:`` header and checks
           must-haves / description, but never verifies that task IDs are
           UNIQUE. A plan with two ``## T01: ...`` headers passes validation
           with zero errors, yet ``_parse_slice_plan`` produces two Task
           objects both with ``id="T01"``. Downstream ``execute_phase``
           creates both ``T01-SUMMARY.md`` files at the same path — the
           second overwrites the first, and crash recovery becomes
           ambiguous. Also, ``_extract_task_ids`` collapses to a set, so
           task-count vs id-count silently disagree.
   Fix:    In ``validate_plan``, track seen task IDs and append an error
           when a duplicate is encountered, e.g.
           ``errors.append(f"duplicate task id {tid}")``.

3. ReflexionBuildReflexionContextSharesFailedTestsList
   Where:  tero2/reflexion.py, ``build_reflexion_context``.
   What:   ``build_reflexion_context`` constructs new ``ReflexionAttempt``
           objects for each input attempt, truncating ``builder_output`` but
           re-using the original ``a.failed_tests`` list BY REFERENCE. A
           caller that mutates the returned context's attempts'
           ``failed_tests`` list (e.g. via ``.append``) silently mutates the
           caller-passed input as well. That breaks the function's implied
           contract of returning an immutable-snapshot copy and causes test
           failures that are hard to trace back to this shared state.
   Fix:    Pass ``failed_tests=list(a.failed_tests)`` in the
           ``ReflexionAttempt(...)`` constructor so each truncated attempt
           owns its own list.
"""

from __future__ import annotations

import importlib
import json
import os
import pathlib
import tempfile

import pytest


# ── Bug 1: history.load_history loses all entries on one bad entry ────────

class TestLoopIter5HistoryLoseAllOnOneBadEntry:
    """load_history returns [] when ONE entry has a bad shape — ALL valid entries lost."""

    def test_single_bad_entry_drops_every_valid_entry(self, tmp_path, monkeypatch):
        # Redirect HOME so history writes to a clean temp dir.
        monkeypatch.setenv("HOME", str(tmp_path))
        # Re-import the module so HISTORY_FILE picks up the new HOME.
        from tero2 import history as history_mod
        importlib.reload(history_mod)

        hist_path = tmp_path / ".tero2" / "history.json"
        hist_path.parent.mkdir(parents=True, exist_ok=True)

        valid_entry = {
            "path": "/tmp/good_project",
            "name": "good_project",
            "last_run": "2026-01-01T00:00:00+00:00",
            "last_plan": None,
            "run_count": 3,
        }
        # One valid entry + one malformed (extra unknown field).
        malformed_entry = {**valid_entry, "path": "/tmp/bad_project", "unknown_field_v2": "future"}

        data = {"version": 1, "entries": [valid_entry, malformed_entry]}
        hist_path.write_text(json.dumps(data), encoding="utf-8")

        entries = history_mod.load_history()
        # BUG: entries is [] — we lose the valid entry because of the malformed one.
        # Expected: at least the valid entry survives.
        assert len(entries) >= 1, (
            f"load_history should preserve valid entries even when other "
            f"entries are malformed, got {len(entries)} entries"
        )
        # The valid entry's path should be in the loaded result.
        assert any(e.path == "/tmp/good_project" for e in entries), (
            "valid entry was lost alongside the malformed one"
        )


# ── Bug 2: architect.validate_plan allows duplicate task IDs ──────────────

class TestLoopIter5ArchitectValidatePlanAllowsDuplicateTaskIds:
    """validate_plan accepts plans with duplicate task IDs (e.g. T01 twice)."""

    def test_duplicate_task_ids_should_fail_validation(self):
        from tero2.players.architect import validate_plan

        plan = (
            "## T01: First description of T01\n"
            "\n"
            "Some body text for T01.\n"
            "\n"
            "**Must-haves:**\n"
            "- item1\n"
            "\n"
            "## T01: Duplicate — SAME ID again!\n"
            "\n"
            "Body for the duplicate T01.\n"
            "\n"
            "**Must-haves:**\n"
            "- item2\n"
        )

        errors = validate_plan(plan)
        # BUG: errors is [] or only cosmetic errors — no mention of duplicate task IDs.
        duplicate_errors = [e for e in errors if "duplicate" in e.lower()]
        assert duplicate_errors, (
            f"validate_plan should flag duplicate task IDs, "
            f"got errors={errors!r}"
        )

    def test_unique_task_ids_pass_duplicate_check(self):
        """Sanity check: unique IDs still validate (no false positives)."""
        from tero2.players.architect import validate_plan

        plan = (
            "## T01: First\n\nBody for T01.\n\n**Must-haves:**\n- item1\n\n"
            "## T02: Second\n\nBody for T02.\n\n**Must-haves:**\n- item2\n"
        )

        errors = validate_plan(plan)
        duplicate_errors = [e for e in errors if "duplicate" in e.lower()]
        assert not duplicate_errors, (
            f"unique IDs should not trigger duplicate error, got {duplicate_errors!r}"
        )


# ── Bug 3: reflexion.build_reflexion_context shares failed_tests reference ─

class TestLoopIter5ReflexionBuildReflexionContextSharesFailedTestsList:
    """build_reflexion_context re-uses the original failed_tests list by reference."""

    def test_mutation_of_returned_context_does_not_affect_input(self):
        from tero2.reflexion import ReflexionAttempt, build_reflexion_context

        original_failed_tests = ["test_a", "test_b"]
        attempt = ReflexionAttempt(
            attempt_number=1,
            builder_output="short output",
            verifier_feedback="feedback",
            failed_tests=original_failed_tests,
        )

        new_ctx = build_reflexion_context([attempt])

        # Mutate the NEW context's failed_tests list.
        new_ctx.attempts[0].failed_tests.append("test_c_added_later")

        # BUG: original_failed_tests is also mutated because the list is shared.
        # Expected: build_reflexion_context should produce independent copies.
        assert "test_c_added_later" not in original_failed_tests, (
            f"mutating the truncated context's failed_tests should not bleed "
            f"into the original — got {original_failed_tests!r}"
        )

    def test_input_mutation_does_not_affect_returned_context(self):
        """Mirror test: mutating the input list should not change the returned snapshot."""
        from tero2.reflexion import ReflexionAttempt, build_reflexion_context

        original_failed_tests = ["test_a"]
        attempt = ReflexionAttempt(
            attempt_number=1,
            builder_output="short",
            verifier_feedback="feedback",
            failed_tests=original_failed_tests,
        )

        new_ctx = build_reflexion_context([attempt])
        original_failed_tests.append("late_addition")

        assert "late_addition" not in new_ctx.attempts[0].failed_tests, (
            f"mutating the original list after build_reflexion_context returned "
            f"should not affect the snapshot, got {new_ctx.attempts[0].failed_tests!r}"
        )
