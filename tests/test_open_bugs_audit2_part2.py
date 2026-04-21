"""Negative tests for remaining open bugs from bugs.md Audit 2.

Convention: test FAILS when bug is present, PASSES when fixed.

Additional bugs confirmed as already FIXED (should be removed from bugs.md):
  Bug 22  app: query_one wrapped in try/except NoMatches (lines 104-110)
  Bug 24  runner: slice loop uses correct `< max_slices - 1` condition (line 570)
  Bug 27  telegram: _MAX_FILE_SIZE check at line 300-303 already present
  Bug 32  plan_pick: _mtime has try/except OSError: return 0.0 (line 59)
  Bug 33  project_pick: action_manual_input already guards with query_one + NoMatches
  Bug 34  app: BINDINGS already contains ('n', 'new_project') and ('o', 'settings')
  Bug 36  project_pick: 'd' binding and action_delete_entry both present
  Bug 43  app: routing block wrapped in broad `except Exception` (line 153)
  Bug 47  runner: ctx.escalation_level IS reset to NONE when stuck.signal == NONE
          (line 333). Bug description is misleading — code works correctly.
  Bug 50  model_pick: on_list_view_selected guards with `0 <= idx < len(self._filtered)`

Bugs tested here:
  Bug 19  usage_tracker: record_step modifies shared dict without any lock
  Bug 48  runner: slice loop does not check shutdown_event before run_architect/run_execute
  Bug 52  cli provider: CancelledError from stderr_task swallowed → b"" returned
"""

from __future__ import annotations

import asyncio
import inspect
import threading

import pytest


# ── Bug 19: usage_tracker race condition on shared dict ───────────────────────


class TestBug19UsageTrackerRace:
    """record_step() modifies _providers dict without any threading lock.

    The compound check-then-set operation is not atomic:
      if provider not in self._providers:
          self._providers[provider] = {...}   ← another thread can interleave here
    Under concurrent calls, one thread can overwrite another's freshly-created
    entry, losing all accumulated tokens/cost/steps.

    Fix: guard _providers access with threading.Lock or asyncio.Lock.
    """

    def test_record_step_source_contains_a_lock(self) -> None:
        from tero2.usage_tracker import UsageTracker

        source = inspect.getsource(UsageTracker.record_step)
        has_lock = (
            "lock" in source.lower()
            or "Lock" in source
            or "acquire" in source
            or "with self._" in source
        )
        assert has_lock, (
            "Bug 19: UsageTracker.record_step has no lock protecting _providers. "
            "Concurrent calls can interleave between the 'not in' check and the "
            "dict assignment, silently losing increments. "
            "Fix: add threading.Lock or asyncio.Lock around _providers access."
        )


# ── Bug 48: runner slice loop skips shutdown check ────────────────────────────


class TestBug48SliceLoopShutdownCheck:
    """_execute_sora's extra-slice loop calls run_architect / run_execute without
    first checking shutdown_event.is_set().

    If SIGTERM arrives while one of these long-running calls is in progress,
    the runner can't exit until the current call finishes (potentially minutes).

    Fix: add `if shutdown_event and shutdown_event.is_set(): return` before
    each run_architect / run_execute invocation in the while loop.
    """

    def test_execute_sora_checks_shutdown_before_architect_in_slice_loop(self) -> None:
        from tero2.runner import Runner

        source = inspect.getsource(Runner._execute_sora)

        # We need the shutdown check to appear AFTER the while loop header
        # and BEFORE run_architect. The simplest proxy: count how many times
        # shutdown_event.is_set() appears in the entire method.
        # A proper fix adds at least one check inside the slice while loop.
        checks = source.count("shutdown_event.is_set()")

        # The slice loop needs its own check; the method already has one at the
        # top of the retry function — so a fixed implementation has >= 2.
        assert checks >= 2, (
            f"Bug 48: _execute_sora has only {checks} shutdown_event.is_set() check(s). "
            "The extra-slice while loop must also check shutdown_event before invoking "
            "run_architect() or run_execute() — long calls block graceful shutdown. "
            "Fix: add shutdown_event.is_set() guard at each phase boundary in the slice loop."
        )

    def test_execute_sora_slice_loop_respects_shutdown_mid_run(self) -> None:
        """The slice loop must exit cleanly when shutdown fires after a phase starts.

        Structural proxy: verify shutdown_event is referenced inside the while-loop
        body (not just at the top of the method before the loop).
        """
        from tero2.runner import Runner
        import ast, textwrap

        src = inspect.getsource(Runner._execute_sora)
        # Find the start of the extra_slices while loop
        loop_marker = "while extra_slices_done"
        loop_start = src.find(loop_marker)
        assert loop_start != -1, "Could not locate the extra-slice while loop in source."

        loop_body = src[loop_start:]
        has_shutdown_in_loop = "shutdown_event" in loop_body

        assert has_shutdown_in_loop, (
            "Bug 48: the extra-slice while loop body contains no reference to shutdown_event. "
            "A SIGTERM received during run_architect/run_execute is ignored until the call ends."
        )


# ── Bug 52: cli provider stderr data loss on cancel ──────────────────────────


class TestBug52StderrLossOnCancel:
    """_collect_output swallows CancelledError from stderr_task, returning b"".

    When the caller cancels the outer coroutine while `await stderr_task` is in
    flight, the except clause at the end of _collect_output catches CancelledError
    and silently returns b"" — discarding any partial stderr already captured.

    Fix: re-raise CancelledError after recording captured bytes, or shield
    stderr_task so cancellation doesn't propagate through it.
    """

    def test_collect_output_reraises_cancelled_error_after_recording_stderr(self) -> None:
        from tero2.providers.cli import CLIProvider

        source = inspect.getsource(CLIProvider._collect_output)

        # The bug: CancelledError is listed in the except tuple, swallowing it.
        # A fix would either not catch CancelledError or re-raise it.
        has_cancel_in_bare_except = (
            "CancelledError, Exception" in source or
            "asyncio.CancelledError" in source and "except" in source
        )
        # Check if CancelledError is re-raised after being caught
        reraises = "raise" in source

        if has_cancel_in_bare_except and not reraises:
            pytest.fail(
                "Bug 52: _collect_output catches asyncio.CancelledError and returns b'' "
                "without re-raising. Any partial stderr captured before cancellation is lost. "
                "Fix: separate CancelledError from the except clause and re-raise it, "
                "or save captured stderr_bytes before re-raising."
            )

    def test_cancelled_error_not_in_broad_swallow_clause(self) -> None:
        """The except clause must not silently swallow CancelledError.

        When _collect_output is cancelled while awaiting stderr_task, the
        CancelledError must propagate to the caller (or be re-raised after
        recording partial stderr). The current code catches it together with
        all other exceptions and returns b"", losing both the error signal
        and any partial stderr data.

        Fix: remove asyncio.CancelledError from the except tuple in the
        stderr_task result section, or catch it separately and re-raise.
        """
        from tero2.providers.cli import CLIProvider

        source = inspect.getsource(CLIProvider._collect_output)

        # Find the final stderr_task result section (after normal stdout loop).
        # Bug pattern: "except (asyncio.CancelledError, Exception): stderr_bytes = b''"
        # which silently swallows the cancellation.
        cancel_in_swallow = (
            "CancelledError, Exception" in source
            or "CancelledError," in source
        )
        reraises_cancel = "raise" in source

        assert not (cancel_in_swallow and not reraises_cancel), (
            "Bug 52: _collect_output catches asyncio.CancelledError in a broad except "
            "without re-raising. When the caller cancels the coroutine, CancelledError "
            "is swallowed and stderr_bytes = b'' hides the cancellation from the caller. "
            "Fix: don't catch CancelledError in the stderr result section, or re-raise it "
            "after capturing partial stderr bytes."
        )
