"""Bug 123 (sibling of 121): ``builder._recover_summary_from_disk``
iterates three candidate paths and catches ``(OSError,
FileNotFoundError)`` so the loop can skip missing/unreadable files.
It does NOT catch ``UnicodeDecodeError`` (a ``ValueError`` subclass,
not an ``OSError``), so a single candidate whose bytes are not valid
UTF-8 escapes the inner try, aborts the loop, and prevents the next
candidates from being tried.

In practice: agents that write a ``{task_id}-SUMMARY.md`` with
non-UTF-8 content (rare but possible — editor default encoding, paste
from clipboard) cause the whole recovery path to fail instead of
falling through to the synthesized-placeholder path in ``builder.run``.

Fix: add ``UnicodeDecodeError`` to the except tuple — same shape as
bug 121 in ``DiskLayer.read_file``. The loop skips the bad candidate,
tries the remaining ones, and falls through to ``""`` if none work.
"""

from __future__ import annotations

from pathlib import Path


def test_non_utf8_candidate_does_not_abort_recovery_loop(tmp_path: Path) -> None:
    """The bug: one non-UTF-8 candidate raises UnicodeDecodeError out of
    the loop. Contract: skip it and try the other candidates."""
    from tero2.players.builder import _recover_summary_from_disk

    # First candidate (T01-SUMMARY.md) has bad bytes.
    bad_path = tmp_path / "T01-SUMMARY.md"
    bad_path.write_bytes(b"\x93bad encoding\x94")

    # A later candidate (t01-SUMMARY.md after upper() = T01, lower() = t01)
    # is valid. Write both cases are tried per the candidates list in
    # builder; a lowercase variant wins when the original case probe fails.
    # We can't rely on that alone because "T01" IS the uppercase form
    # of "T01", so all three paths point at the same file. Instead,
    # verify the behavior by asserting the function doesn't raise.
    result = _recover_summary_from_disk("T01", str(tmp_path))

    # On broken code: UnicodeDecodeError escapes, the outer caller sees
    # it as a generic Exception. Fixed code: returns "".
    assert result == "", (
        "bug 123: UnicodeDecodeError must be caught in the same except "
        "tuple so a bad-encoded candidate doesn't abort the loop. "
        f"got {result!r}"
    )


def test_recovery_succeeds_when_utf8_candidate_exists(tmp_path: Path) -> None:
    """Regression guard: the healthy path (single valid UTF-8 candidate)
    still works."""
    from tero2.players.builder import _recover_summary_from_disk

    (tmp_path / "T02-SUMMARY.md").write_text(
        "# T02 summary\nAll work done.\n", encoding="utf-8"
    )
    result = _recover_summary_from_disk("T02", str(tmp_path))
    assert "All work done." in result


def test_recovery_returns_empty_when_no_candidates_exist(tmp_path: Path) -> None:
    """Regression guard: empty working_dir or no file returns ''."""
    from tero2.players.builder import _recover_summary_from_disk

    assert _recover_summary_from_disk("T03", str(tmp_path)) == ""
    assert _recover_summary_from_disk("T04", "") == ""
