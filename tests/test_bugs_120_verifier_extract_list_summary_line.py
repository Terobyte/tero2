"""Bug 120: ``_extract_list`` in verifier uses ``re.IGNORECASE``, so the
pytest summary line (``============== 2 failed in 0.5s ===============``)
matches the same pattern as a real failure line
(``FAILED tests/foo.py::test_bar - AssertionError``) and contaminates
``failed_tests`` with garbage like ``in 0.5s ===============``.

This garbage then flows into reflexion prompts as a "specific test name
that failed", which corrupts the LLM's understanding of what to fix.

The fix is to drop ``IGNORECASE``: pytest writes literal uppercase
``FAILED`` on per-test result lines, and only lowercase ``failed`` in
the summary line counting failures. Case-sensitive matching cleanly
separates the two.
"""

from __future__ import annotations


def test_pytest_summary_line_does_not_pollute_failed_tests():
    """The bug: pytest's lowercase ``N failed in Xs`` summary line is
    case-folded into the ``FAILED`` match under IGNORECASE, producing
    a bogus entry like ``in 0.5s ===============``."""
    from tero2.players.verifier import _extract_list

    output = (
        "FAILED tests/foo.py::test_bar - AssertionError: expected\n"
        "============== 2 failed in 0.5s ===============\n"
    )
    result = _extract_list(output, "FAILED")

    assert result == ["tests/foo.py::test_bar"], (
        "bug 120: pytest summary line 'N failed in Xs' was matched "
        "case-insensitively against FAILED and leaked into failed_tests. "
        f"expected ['tests/foo.py::test_bar'], got {result!r}"
    )


def test_realistic_pytest_output_extracts_only_test_ids():
    """Realistic pytest short-summary block with a summary line.

    Both real FAILED lines and the summary line ``2 failed in 0.5s``
    are present. Only the two test IDs must end up in ``failed_tests``.
    """
    from tero2.players.verifier import _extract_list

    output = (
        "=============================== FAILURES ================================\n"
        "______________________________ test_one ________________________________\n"
        "...stack...\n"
        "______________________________ test_two ________________________________\n"
        "...stack...\n"
        "=========================== short test summary info ====================\n"
        "FAILED tests/a.py::test_one - AssertionError: left != right\n"
        "FAILED tests/b.py::test_two - RuntimeError: boom\n"
        "=========================== 2 failed in 0.50s ==========================\n"
    )
    result = _extract_list(output, "FAILED")

    assert result == [
        "tests/a.py::test_one",
        "tests/b.py::test_two",
    ], (
        "bug 120: pytest summary line polluted failed_tests list. "
        f"got {result!r}"
    )


def test_lowercase_failed_in_prose_is_ignored():
    """Regression guard: arbitrary prose containing ``failed`` (e.g.
    error descriptions, log lines) must not be treated as a FAILED
    marker. Only the uppercase ``FAILED`` that pytest emits on result
    lines should count."""
    from tero2.players.verifier import _extract_list

    output = (
        "some log line: request failed with status 500\n"
        "another line: operation failed, retrying\n"
        "FAILED tests/real.py::test_thing - ConnectionError\n"
    )
    result = _extract_list(output, "FAILED")

    assert result == ["tests/real.py::test_thing"], (
        "bug 120: prose containing lowercase 'failed' leaked into the "
        f"failed_tests list. got {result!r}"
    )


def test_uppercase_only_match_preserves_bug_31_contract():
    """Regression: bug 31's existing contract (clean test IDs, no
    trailing error description, no '-' or numbers) still holds after
    dropping IGNORECASE."""
    from tero2.players.verifier import _extract_list

    output = "FAILED tests/bar.py::test_thing - ValueError: bad value 42"
    result = _extract_list(output, "FAILED")

    assert result == ["tests/bar.py::test_thing"]
    assert "-" not in result
    assert "42" not in result
    assert "ValueError:" not in result
