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

import ast
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

    # ── shared AST helpers ────────────────────────────────────────────────────

    @staticmethod
    def _find_slice_while(tree: ast.AST) -> ast.While | None:
        """Return the `while extra_slices_done ...` node, or None."""
        for node in ast.walk(tree):
            if isinstance(node, ast.While) and "extra_slices_done" in ast.dump(node.test):
                return node
        return None

    @staticmethod
    def _is_shutdown_guard(node: ast.stmt) -> bool:
        """True iff *node* is an If whose condition calls shutdown_event.is_set()."""
        if not isinstance(node, ast.If):
            return False
        for sub in ast.walk(node.test):
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Attribute)
                and sub.func.attr == "is_set"
                and isinstance(sub.func.value, ast.Name)
                and sub.func.value.id == "shutdown_event"
            ):
                return True
        return False

    @staticmethod
    def _is_await_call(node: ast.stmt, func_name: str) -> bool:
        """True iff *node* is `... = await func_name(...)` or `await func_name(...)`."""
        val: ast.expr | None = None
        if isinstance(node, ast.Assign):
            val = node.value
        elif isinstance(node, ast.Expr):
            val = node.value
        if val is None or not isinstance(val, ast.Await):
            return False
        call = val.value
        return (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Name)
            and call.func.id == func_name
        )

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_slice_loop_has_shutdown_guard_before_run_architect(self) -> None:
        """AST check: a shutdown_event.is_set() If-guard must precede run_architect
        in the extra-slice while loop body."""
        import textwrap

        from tero2.runner import Runner

        src = textwrap.dedent(inspect.getsource(Runner._execute_sora))
        tree = ast.parse(src)

        slice_while = self._find_slice_while(tree)
        assert slice_while is not None, "Cannot locate `while extra_slices_done` in _execute_sora."

        prev_was_guard = False
        architect_guarded = False
        for stmt in slice_while.body:
            if self._is_await_call(stmt, "run_architect") and prev_was_guard:
                architect_guarded = True
            prev_was_guard = self._is_shutdown_guard(stmt)

        assert architect_guarded, (
            "Bug 48: no shutdown_event.is_set() guard found immediately before "
            "run_architect() in the extra-slice while loop. "
            "A SIGTERM received during the long architect call is ignored until it returns. "
            "Fix: add `if shutdown_event and shutdown_event.is_set(): return` "
            "immediately before the run_architect() call in the slice loop."
        )

    def test_slice_loop_has_shutdown_guard_before_run_execute(self) -> None:
        """AST check: a shutdown_event.is_set() If-guard must precede run_execute
        in the extra-slice while loop body."""
        import textwrap

        from tero2.runner import Runner

        src = textwrap.dedent(inspect.getsource(Runner._execute_sora))
        tree = ast.parse(src)

        slice_while = self._find_slice_while(tree)
        assert slice_while is not None, "Cannot locate `while extra_slices_done` in _execute_sora."

        prev_was_guard = False
        execute_guarded = False
        for stmt in slice_while.body:
            if self._is_await_call(stmt, "run_execute") and prev_was_guard:
                execute_guarded = True
            prev_was_guard = self._is_shutdown_guard(stmt)

        assert execute_guarded, (
            "Bug 48: no shutdown_event.is_set() guard found immediately before "
            "run_execute() in the extra-slice while loop. "
            "A SIGTERM received during the long execute call is ignored until it returns. "
            "Fix: add `if shutdown_event and shutdown_event.is_set(): return` "
            "immediately before the run_execute() call in the slice loop."
        )


# ── Bug 52: cli provider stderr data loss on cancel ──────────────────────────


class TestBug52StderrLossOnCancel:
    """_stream_events swallows CancelledError from stderr_task, returning b"".

    After reading all stdout lines, _stream_events awaits the stderr_task result
    inside `except (asyncio.CancelledError, Exception): stderr_bytes = b""`.
    When the caller cancels the outer coroutine while that await is in flight,
    the CancelledError is caught and silently discarded — both the error signal
    and any partial stderr already captured are lost.

    Fix: remove asyncio.CancelledError from the broad except clause in the
    stderr_task result section, or catch it separately and re-raise after
    recording the captured bytes.

    Note: the method responsible for this is `_stream_events`, not the
    (non-existent) `_collect_output`.
    """

    # ── shared AST helpers ────────────────────────────────────────────────────

    @staticmethod
    def _catches_cancelled_error(handler: ast.ExceptHandler) -> bool:
        """True iff this except clause matches asyncio.CancelledError."""
        if handler.type is None:
            return True  # bare `except:` catches everything
        return "CancelledError" in ast.dump(handler.type)

    @staticmethod
    def _has_bare_reraise(handler: ast.ExceptHandler) -> bool:
        """True iff the handler body contains a bare ``raise`` (no argument).

        A bare ``raise`` re-raises the currently-handled exception, propagating
        CancelledError to the caller.  ``raise SomeOtherError()`` would also be
        an ``ast.Raise`` node but with a non-None ``exc`` attribute — that
        translates the exception rather than propagating it, so it does NOT
        count as a correct cancellation-safe handler.
        """
        return any(
            isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(handler)
        )

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_stream_events_method_exists(self) -> None:
        """The stderr-collection logic lives in _stream_events, not _collect_output."""
        from tero2.providers.cli import CLIProvider

        assert hasattr(CLIProvider, "_stream_events"), (
            "CLIProvider._stream_events does not exist. "
            "The Bug 52 tests target this method; check the class for renames."
        )

    def test_stream_events_does_not_swallow_cancelled_error(self) -> None:
        """AST check: no ExceptHandler in _stream_events may catch CancelledError
        without re-raising it.

        The buggy pattern:
            except (asyncio.CancelledError, Exception):
                stderr_bytes = b""   # no raise!

        This silently discards both the cancellation signal and partial stderr.
        A correct handler either omits CancelledError from the except clause,
        or catches it separately and re-raises.
        """
        import textwrap

        from tero2.providers.cli import CLIProvider

        src = textwrap.dedent(inspect.getsource(CLIProvider._stream_events))
        tree = ast.parse(src)

        swallowing_handlers = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
            and self._catches_cancelled_error(node)
            and not self._has_bare_reraise(node)
        ]

        assert not swallowing_handlers, (
            "Bug 52: _stream_events has an except clause that catches "
            "asyncio.CancelledError without re-raising it. "
            "When the coroutine is cancelled while awaiting stderr_task, "
            "the cancellation is silently swallowed and stderr_bytes = b'' "
            "hides the error from the caller. "
            "Fix: remove asyncio.CancelledError from the broad except tuple in "
            "the stderr_task result section, or catch it separately and re-raise."
        )
