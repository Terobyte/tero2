"""Halal tests for bugs found in autonomous loop iter2 (2026-04-23).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug L4  coach: STEER.md only cleared when ALL four sections written
          (`wrote_any = all(...)`); should clear when ANY section was written.
  Bug L5  verifier: _run_command dispatches redirection commands (`>`, `<`,
          `>>`, wildcards) through subprocess instead of shell, so the
          redirection/glob is silently broken.
  Bug L6  coach: _gather_context breaks on first missing T0X-SUMMARY.md
          inside a slice, so skipped/interrupted tasks hide every later
          summary from the Coach's strategic context.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest


# ── Bug L4: Coach STEER.md cleared only when all four sections present ───────


class TestLoopIter2CoachSteerClear:
    """Coach writes strategic docs with a STEER.md directive.

    Where: tero2/players/coach.py:90 — `wrote_any = all([...])`.

    Scenario: LLM responds with STRATEGY + TASK_QUEUE + CONTEXT_HINTS but
    no RISK section (common when there is nothing risky to flag).  Because
    `wrote_any = all(...)` returns False when any section is empty, STEER.md
    is NOT cleared even though the directive was folded into the written
    strategy docs.  The comment and variable name indicate the intent is
    "clear when any section was applied", so this should be `any(...)`.

    Fix hint: change `all([strategy, task_queue, risk, context_hints])`
    to `any([strategy, task_queue, risk, context_hints])` on line 90 of
    tero2/players/coach.py.
    """

    def test_steer_cleared_when_three_of_four_sections_written(
        self, tmp_path: Path
    ) -> None:
        from tero2.disk_layer import DiskLayer
        from tero2.players.coach import CoachPlayer

        disk = DiskLayer(tmp_path)
        disk.init()
        disk.write_steer("please prioritise the auth module")

        # Minimal fake chain that returns a plausible three-section response.
        class _FakeChain:
            async def run_prompt_collected(self, prompt: str, **kwargs: Any) -> str:
                return (
                    "## STRATEGY\nFocus on auth first.\n\n"
                    "## TASK_QUEUE\n- T01 auth module\n\n"
                    # No RISK section -- nothing risky to flag.
                    "## CONTEXT_HINTS\nkeep tokens scoped.\n"
                )

        coach = CoachPlayer(_FakeChain(), disk)  # type: ignore[arg-type]

        # Run the coach.
        result = asyncio.run(coach.run())
        assert result.success
        # Strategy, task_queue and context_hints were written.
        assert (tmp_path / ".sora" / "strategic" / "STRATEGY.md").exists()
        assert (tmp_path / ".sora" / "strategic" / "TASK_QUEUE.md").exists()
        assert (tmp_path / ".sora" / "strategic" / "CONTEXT_HINTS.md").exists()

        # The STEER directive was folded into the above docs, so STEER.md
        # must be cleared.  With the `all(...)` bug it stays on disk and
        # the same directive keeps being re-applied every Coach pass.
        remaining_steer = (tmp_path / ".sora" / "human" / "STEER.md")
        assert not remaining_steer.exists() or remaining_steer.read_text() == "", (
            "Bug L4: STEER.md was not cleared even though three strategy "
            "sections were written and the directive was applied. "
            "tero2/players/coach.py:90 uses `all(...)` where `any(...)` is meant."
        )


# ── Bug L5: Verifier redirection dropped ──────────────────────────────────────


class TestLoopIter2VerifierShellOps:
    """Verifier _run_command fails to detect shell-only operators.

    Where: tero2/players/verifier.py:80 — `_SHELL_OPS` regex lists
    `&&|\\|\\||[|;]|\\bcd\\b` but omits `>` (redirection),
    `<` (stdin), and `*` / `?` (glob wildcards).

    Scenario: a project configures `[verifier] commands = ["pytest > tests.log"]`
    or uses a must-have like `ls tests/test_*.py`.  `_SHELL_OPS.search(cmd_str)`
    returns None, so `_run_command` falls through to shlex.split + subprocess
    without a shell, and the `>`/`*` characters are passed as literal
    argv entries.  The redirection never happens (no file written) and the
    glob never expands (command sees literal `*.py`).

    Fix hint: extend `_SHELL_OPS` to include redirection, glob, and any
    other characters that require a shell (e.g. `>`, `<`, `*`, `?`,
    `(`, `)`, backticks).  Or always route through `_run_shell`.
    """

    def test_redirection_operator_uses_shell(self, tmp_path: Path) -> None:
        from tero2.players.verifier import _run_command

        target = tmp_path / "out.txt"
        # `echo` with redirection is a plain cwd-independent command; it
        # should actually create the file when executed through a shell.
        rc, out, err = _run_command(
            f"echo hello > {target}",
            str(tmp_path),
        )

        assert rc == 0, f"Bug L5: echo failed entirely (rc={rc}, err={err!r})"
        assert target.exists(), (
            "Bug L5: verifier._run_command did not route the redirection "
            "through a shell — `>` was passed as a literal argv entry, so "
            "the expected output file was never created. "
            "tero2/players/verifier.py:80 `_SHELL_OPS` is missing `>`/`<`."
        )
        assert target.read_text().strip() == "hello"


# ── Bug L6: Coach._gather_context aborts on any task summary gap ───────────────


class TestLoopIter2CoachTaskSummaryGap:
    """Coach stops reading task summaries at the first gap in a slice.

    Where: tero2/players/coach.py:132-148 — the inner loop
    `for i in range(1, _MAX_TASKS + 1)` breaks on the first missing
    `T0X-SUMMARY.md`, so any later summaries in the same slice are
    silently ignored.

    Scenario: an operator skips T01 via the TUI 'k' binding (or the
    task is marked done without writing a summary) and then T02..T05
    complete normally with summaries on disk.  The Coach's strategic
    pass is invoked at end-of-slice.  Because T01-SUMMARY.md is
    missing, the inner loop breaks immediately and Coach never reads
    T02..T05.  The prompt sent to the LLM has no task context, so
    the resulting STRATEGY.md / RISK.md reflect a phantom empty slice.

    Fix hint: replace the inner `break` with `continue` so a single
    missing summary does not shadow subsequent ones.  (The dense-prefix
    assumption was wrong; tasks can be skipped, renamed, or interrupted.)
    """

    def test_missing_t01_does_not_hide_later_summaries(
        self, tmp_path: Path
    ) -> None:
        from tero2.disk_layer import DiskLayer
        from tero2.players.coach import CoachPlayer

        disk = DiskLayer(tmp_path)
        disk.init()
        # Create slice dir and populate T02..T04 summaries without T01.
        slice_dir = tmp_path / ".sora" / "milestones" / "M001" / "S01"
        slice_dir.mkdir(parents=True, exist_ok=True)
        (slice_dir / "T02-SUMMARY.md").write_text("completed T02 auth module")
        (slice_dir / "T03-SUMMARY.md").write_text("completed T03 refresh flow")
        (slice_dir / "T04-SUMMARY.md").write_text("completed T04 token revocation")

        class _FakeChain:
            async def run_prompt_collected(self, prompt: str, **kwargs: Any) -> str:
                return (
                    "## STRATEGY\nok\n## TASK_QUEUE\n-\n## RISK\n-\n"
                    "## CONTEXT_HINTS\n-\n"
                )

        coach = CoachPlayer(_FakeChain(), disk)  # type: ignore[arg-type]
        ctx = coach._gather_context("milestones/M001", "S01")

        # With the bug, the loop breaks on missing T01 and task_summaries
        # ends up empty despite T02..T04 being on disk.
        assert "T02 auth module" in ctx["task_summaries"], (
            "Bug L6: Coach._gather_context aborted at missing T01 and lost "
            "T02-SUMMARY.md from the prompt context. "
            "tero2/players/coach.py:137 uses `break` where `continue` is meant."
        )
        # Stronger: all three later summaries must appear.
        for tid in ("T02", "T03", "T04"):
            assert tid in ctx["task_summaries"], (
                f"Bug L6: Coach lost {tid}-SUMMARY.md due to early break "
                "on missing predecessor."
            )
