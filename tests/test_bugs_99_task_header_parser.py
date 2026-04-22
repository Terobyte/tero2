"""Bug 99: architect's plan parser rejects natural ``## Task T01: ...`` headers.

Before the fix, ``_TASK_RE`` and ``_TASK_SPLIT_RE`` required the header form
``## T01: ...`` verbatim. A valid plan like::

    ## Task T01: `reverse_string(s: str) -> str`
    - **Description:** ...
    - **Must-haves:**
      - [ ] ...

would be rejected with ``plan contains no tasks`` because the regex failed to
match ``T01`` with the word ``Task `` preceding it. This killed iter-2 of the
night loop despite harden_phase producing a perfectly-formed plan.

Halal negative tests pin both the new permissive behaviour and regression
protection for the original strict form.
"""

from __future__ import annotations

import textwrap

import pytest

from tero2.players.architect import (
    _count_tasks,
    _extract_task_ids,
    _parse_slice_plan,
    validate_plan,
)


# Exact reproduction of the harden-phase output that broke iter-2.
_NATURAL_PLAN = textwrap.dedent(
    """\
    # S01 Plan — Stringy Primitives

    ## Task T01: `reverse_string(s: str) -> str`
    - **Description:** Create `stringy/utils.py` with `reverse_string`.
    - **Must-haves:**
      - [ ] `stringy/utils.py` defines `def reverse_string(s: str) -> str`
    - **Files:** `stringy/utils.py`
    - **Depends on:** none

    ## Task T02: `is_palindrome(s: str) -> bool`
    - **Description:** Add `is_palindrome`.
    - **Must-haves:**
      - [ ] `stringy/utils.py` contains `def is_palindrome(s: str) -> bool`
    - **Files:** `stringy/utils.py`
    - **Depends on:** T01

    ## Task T03: `word_count(s: str) -> dict[str, int]`
    - **Description:** Add `word_count`.
    - **Must-haves:**
      - [ ] `stringy/utils.py` contains `def word_count`
    - **Files:** `stringy/utils.py`
    - **Depends on:** T02
    """
)

# Strict form used by legacy tests and strict prompts — must keep working.
_STRICT_PLAN = textwrap.dedent(
    """\
    # S01 Plan

    ## T01: Init module
    - **Must-haves:**
      - [ ] foo

    ## T02: Add loader
    - **Must-haves:**
      - [ ] bar
    """
)


class TestNaturalTaskHeaderFormat:
    """``## Task Txx: ...`` must be accepted."""

    def test_count_tasks_recognises_task_prefix(self) -> None:
        assert _count_tasks(_NATURAL_PLAN) == 3

    def test_extract_task_ids_recognises_task_prefix(self) -> None:
        assert _extract_task_ids(_NATURAL_PLAN) == {"T01", "T02", "T03"}

    def test_validate_plan_accepts_task_prefix(self) -> None:
        errors = validate_plan(_NATURAL_PLAN)
        assert errors == [], f"expected no validation errors, got: {errors}"

    def test_validate_plan_dependency_resolution_with_task_prefix(self) -> None:
        """T01→T02→T03 dependency chain must resolve even with Task-prefixed headers."""
        errors = validate_plan(_NATURAL_PLAN)
        unknown_errors = [e for e in errors if "unknown task" in e]
        assert unknown_errors == [], (
            "dependency refs T01/T02 must resolve when headers use Task prefix"
        )

    def test_parse_slice_plan_round_trip_matches_validator(self) -> None:
        """If validator counts N tasks, _parse_slice_plan must also produce N Task objects."""
        validator_count = _count_tasks(_NATURAL_PLAN)
        parsed = _parse_slice_plan(_NATURAL_PLAN, slice_id="S01")
        assert len(parsed.tasks) == validator_count


class TestStrictTaskHeaderFormat:
    """Regression — ``## Txx: ...`` (no prefix) must still work after the relaxation."""

    def test_count_tasks_strict_form(self) -> None:
        assert _count_tasks(_STRICT_PLAN) == 2

    def test_extract_task_ids_strict_form(self) -> None:
        assert _extract_task_ids(_STRICT_PLAN) == {"T01", "T02"}

    def test_validate_plan_accepts_strict_form(self) -> None:
        errors = validate_plan(_STRICT_PLAN)
        assert errors == [], f"expected no validation errors, got: {errors}"


class TestOtherPrefixVariants:
    """LLMs use many header styles. All should count as tasks once a Txx ID is present."""

    @pytest.mark.parametrize(
        "header",
        [
            "## T01: Do something",
            "## Task T01: Do something",
            "## Step T01: Do something",
            "## T01 — Do something",
            "##  T01:  Do something",  # extra whitespace
        ],
    )
    def test_header_variant_counts_as_task(self, header: str) -> None:
        plan = f"{header}\n- **Must-haves:**\n  - [ ] thing\n"
        assert _count_tasks(plan) == 1, f"{header!r} must be recognised as one task"

    @pytest.mark.parametrize(
        "header",
        [
            "## Introduction",
            "## Overview",
            "## T1: too few digits",  # T\d{2} requires two digits
            "## TX01: wrong letter",
        ],
    )
    def test_non_task_headers_ignored(self, header: str) -> None:
        plan = f"{header}\n- **Must-haves:**\n  - [ ] thing\n"
        assert _count_tasks(plan) == 0, f"{header!r} must NOT be counted as a task"


class TestValidationErrorOnEmptyPlan:
    """Defensive — truly empty plan must still fail validation with the original message."""

    def test_empty_plan_has_no_tasks_error(self) -> None:
        errors = validate_plan("# Just a title\n\nNo tasks at all.\n")
        assert "plan contains no tasks" in errors
