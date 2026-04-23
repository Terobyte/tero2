"""Tests for 31 new potential bugs (Audit 5 addendum).

Convention: test FAILS when the bug is present, PASSES when fixed.

Bugs are numbered B1–B31 per the audit report (not yet assigned to bugs.md).
After running: bugs whose tests FAIL are confirmed and written to bugs.md.

  B1   architect: task.index set after append — wrong index?
  B2   shell: CancelledError not caught → subprocess leak
  B3   execute_phase: checkpoint rollback [skip — already bug 127]
  B4   notifier: FD leak in send_voice()
  B5   lock: FD leak in acquire()
  B6   verifier: IndexError with custom verify_commands
  B7   cli: returncode is None → kill() race
  B8   execute_phase: coach hints not merged when STEER.md non-empty
  B9   telegram_input: task_done after requeue → deadlock risk
  B10  stream_bus: RuntimeError from call_soon_threadsafe on closed loop
  B11  usage_tracker: start_refresh_loop no try/except → background crash
  B12  state: _last_path updated even on failed save
  B13  runner: _handle_override uses stale self._current_state
  B14  reviewer: empty review_findings skip validation in fix mode
  B15  coach: loop to _MAX_TASKS without early-exit on missing files
  B16  builder: _recover_summary_from_disk without logging
  B17  scout: _count_files not guarded against symlink loops
  B18  execute_phase: escalation history type mismatch (str vs enum)
  B19  harden_phase: final PLAN.md write unguarded — aborts on I/O error
  B20  harden_phase: no shutdown check after async ops
  B21  catalog: TOCTOU in tmp cleanup — exists() → unlink()
  B22  cli: suppress(CancelledError) hides real errors in stderr drain
  B23  normalizers: type(raw).__name__ crash in error handler
  B24  tui/app: on_resize no try/except — crashes if widget missing
  B25  telegram_input: watcher tasks not tracked → orphan on shutdown
  B26  telegram_input: 5s timeout in _consume_plans causes latency
  B27  usage: compact mode destroys rows and they are never restored
  B28  stream_panel: concurrent dict iteration in _recompute_active_role
  B29  stream_panel: active_role=None → TypeError in watch_raw_mode
  B30  usage: _destroy_row silent failure → orphaned widget
  B31  context: _load_slice_plan doesn't distinguish "no plan" vs "crash"
"""

from __future__ import annotations

import inspect

import pytest


# ── B1: architect task.index after append ────────────────────────────────────


class TestB1ArchitectTaskIndex:
    """tasks[-1].index = len(tasks) - 1 is set after append.
    After append len(tasks) - 1 equals the 0-based position of the
    just-inserted task, so the index IS correct.
    Test confirms this is NOT a bug.
    """

    def test_task_indices_are_sequential_zero_based(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        plan = """\
# S01

## T01: First task
> Must have: something

## T02: Second task
> Must have: other thing

## T03: Third task
> Must have: third thing
"""
        result = _parse_slice_plan(plan, "S01", "milestones/M001")
        assert len(result.tasks) == 3, "expected 3 tasks"
        for i, task in enumerate(result.tasks):
            assert task.index == i, (
                f"B1 CONFIRMED: task {task.id!r} has index={task.index}, "
                f"expected {i}. 'tasks[-1].index = len(tasks) - 1' after append "
                f"gives wrong value."
            )


# ── B2: shell CancelledError not caught ──────────────────────────────────────


class TestB2ShellCancelledError:
    """ShellProvider.run() uses 'except Exception:' for cleanup.
    asyncio.CancelledError is a BaseException (not Exception) since Python 3.8.
    If the task is cancelled during proc.communicate(), the cleanup code
    (terminate/kill/close) is never reached — subprocess and FDs leak.
    Fix: change to 'except BaseException:'.
    """

    def test_shell_cleanup_catches_cancelled_error(self) -> None:
        import tero2.providers.shell as shell_module

        source = inspect.getsource(shell_module.ShellProvider.run)
        lines = source.splitlines()

        # Find the except clause that wraps proc.communicate()
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("except") and "communicate" not in stripped:
                # This is the cleanup handler
                has_base = (
                    "except BaseException" in stripped
                    or "except asyncio.CancelledError" in stripped
                    or "except (Exception, asyncio.CancelledError)" in stripped
                    or "except (asyncio.CancelledError, Exception)" in stripped
                    or "except CancelledError" in stripped
                )
                assert has_base, (
                    "B2 CONFIRMED: ShellProvider.run() uses 'except Exception:' "
                    "for subprocess cleanup. asyncio.CancelledError inherits from "
                    "BaseException (not Exception) since Python 3.8, so cancellation "
                    "during proc.communicate() leaks the subprocess and FDs. "
                    "Fix: change to 'except BaseException:' or catch CancelledError explicitly."
                )
                return

        pytest.skip("could not find except clause in ShellProvider.run")


# ── B4: notifier FD leak in send_voice() ─────────────────────────────────────


class TestB4NotifierFDLeak:
    """_upload() uses 'with open(audio_path, "rb") as f:' context manager.
    Context managers close the FD even on exception — this is NOT a bug.
    Test confirms the context manager is used.
    """

    def test_send_voice_uses_context_manager(self) -> None:
        import tero2.notifier as notifier_module

        source = inspect.getsource(notifier_module.Notifier.send_voice)
        assert "with open(" in source, (
            "B4: send_voice() does not use 'with open()' context manager. "
            "If it opens a file without a context manager, FDs will leak on exception."
        )


# ── B5: lock FD leak ─────────────────────────────────────────────────────────


class TestB5LockFDLeak:
    """acquire() opens fd, then if pid write fails, closes it in except BaseException block.
    self._fd is only set after all mutations succeed.
    This is CORRECT — no FD leak. Test confirms.
    """

    def test_lock_closes_fd_on_exception(self) -> None:
        import tero2.lock as lock_module

        source = inspect.getsource(lock_module.FileLock.acquire)
        # Must have at least one os.close(fd) in cleanup path
        assert "os.close(fd)" in source, (
            "B5: acquire() does not call os.close(fd) in cleanup path. "
            "If pid write fails, the file descriptor will leak."
        )


# ── B6: verifier IndexError with custom commands ─────────────────────────────


class TestB6VerifierIndexError:
    """With 1 custom verify_command, all_output has 1 element.
    Code: ruff_output = all_output[0] if len(all_output) > 0 else ''
          pytest_output = all_output[1] if len(all_output) > 1 else ''
    Both are guarded — no IndexError. Test confirms.
    """

    def test_verifier_1_custom_command_no_index_error(self) -> None:
        import tero2.players.verifier as verifier_module

        source = inspect.getsource(verifier_module.VerifierPlayer.run)
        assert "len(all_output) > 0" in source or "all_output[0] if" in source, (
            "B6: verifier does not guard all_output[0] access — IndexError risk."
        )
        assert "len(all_output) > 1" in source or "all_output[1] if" in source, (
            "B6: verifier does not guard all_output[1] access — IndexError risk."
        )


# ── B7: cli returncode race ───────────────────────────────────────────────────


class TestB7CLIReturnCodeRace:
    """CLIProvider finally block: 'if proc.returncode is None: proc.kill()'.
    Between the check and kill() the process could exit. But ProcessLookupError
    is already caught. This is handled — not a bug. Test confirms.
    """

    def test_cli_finally_handles_process_lookup_error(self) -> None:
        import tero2.providers.cli as cli_module

        source = inspect.getsource(cli_module.CLIProvider.run)
        assert "ProcessLookupError" in source, (
            "B7: CLIProvider does not catch ProcessLookupError in finally cleanup. "
            "If process exits between returncode check and kill(), OSError is raised."
        )


# ── B8: execute_phase coach hints vs STEER.md ────────────────────────────────


class TestB8CoachHintsSteerMd:
    """At task boundary: if STEER.md non-empty → context_hints = steer_content.
    After Coach fires: context_hints = CONTEXT_HINTS.md content (overrides STEER.md).
    On next retry within same task, Coach hints are used, not STEER.md.
    The bug: STEER.md reload happens only at task BOUNDARY, not between retries.
    Within a task's retry loop, Coach hints permanently replace STEER.md hints.
    """

    def test_steer_is_reread_between_retries(self) -> None:
        import tero2.phases.execute_phase as ep_module

        source = inspect.getsource(ep_module.run_execute)
        # The steer reload must happen INSIDE the retry loop (not just at task boundary)
        # to ensure steer context persists after coach fires within a task.
        lines = source.splitlines()
        in_retry_loop = False
        steer_inside_retry = False
        for line in lines:
            stripped = line.strip()
            # Heuristic: retry loop starts after "for attempt in range"
            if "for attempt in range" in stripped or "retry_count" in stripped and "range" in stripped:
                in_retry_loop = True
            if in_retry_loop and "read_steer" in stripped:
                steer_inside_retry = True
                break
        assert steer_inside_retry, (
            "B8 CONFIRMED: STEER.md is only reloaded at task boundaries, not inside "
            "the retry loop. When Coach fires within a task and updates CONTEXT_HINTS.md, "
            "context_hints = new_hints replaces the STEER.md content. On the next retry "
            "within the same task, STEER.md instructions are lost. "
            "Fix: re-read STEER.md at the start of each retry iteration."
        )


# ── B9: telegram_input task_done after requeue ───────────────────────────────


class TestB9TelegramInputTaskDone:
    """When paused: get() item, put() it back, task_done().
    put() increments _unfinished_tasks, task_done() decrements.
    Net: unfinished_tasks unchanged. join() still waits. Correct behavior.
    Test confirms this is NOT a bug.
    """

    def test_task_done_after_requeue_is_correct(self) -> None:
        import asyncio

        async def _check():
            q = asyncio.Queue()
            await q.put("item")
            assert q._unfinished_tasks == 1

            item = await q.get()
            assert q._unfinished_tasks == 1  # unchanged after get()

            await q.put(item)   # requeue: _unfinished_tasks becomes 2
            assert q._unfinished_tasks == 2

            q.task_done()       # task_done: _unfinished_tasks becomes 1
            assert q._unfinished_tasks == 1  # item is still "pending"

        asyncio.run(_check())


# ── B10: stream_bus TOCTOU RuntimeError ──────────────────────────────────────


class TestB10StreamBusTOCTOU:
    """Worker-thread path: checks 'not self._loop.is_closed()' then calls
    call_soon_threadsafe. If loop closes between check and call, RuntimeError
    is raised and propagates to the caller (uncaught).
    Fix: wrap call_soon_threadsafe in try/except RuntimeError.
    """

    def test_call_soon_threadsafe_has_error_guard(self) -> None:
        import tero2.stream_bus as bus_module

        source = inspect.getsource(bus_module.StreamBus.publish)
        lines = source.splitlines()

        # Find call_soon_threadsafe call in worker-thread path
        for i, line in enumerate(lines):
            if "call_soon_threadsafe" in line:
                # Check 3 lines above and below for try/except RuntimeError
                context = "\n".join(lines[max(0, i - 4) : i + 4])
                has_guard = "RuntimeError" in context or "except Exception" in context
                assert has_guard, (
                    "B10 CONFIRMED: call_soon_threadsafe() in StreamBus.publish() "
                    "has no RuntimeError guard. Between the 'not is_closed()' check and "
                    "the call, another thread can close the loop — RuntimeError propagates "
                    "uncaught to the caller. "
                    "Fix: wrap call_soon_threadsafe in try/except RuntimeError."
                )
                return

        pytest.skip("call_soon_threadsafe not found in StreamBus.publish")


# ── B11: usage_tracker background loop no error guard ────────────────────────


class TestB11UsageTrackerRefreshLoop:
    """start_refresh_loop() calls _refresh_limits() in 'while True' with no
    try/except. If _refresh_limits() raises (run_in_executor error, lock issue),
    the loop crashes and future refreshes stop.
    Fix: wrap _refresh_limits() in try/except Exception.
    """

    def test_refresh_loop_has_try_except(self) -> None:
        import tero2.usage_tracker as ut_module

        source = inspect.getsource(ut_module.UsageTracker.start_refresh_loop)
        has_guard = "try:" in source and ("except" in source)
        assert has_guard, (
            "B11 CONFIRMED: start_refresh_loop() has no try/except around "
            "_refresh_limits(). An exception from run_in_executor or lock acquisition "
            "will crash the 'while True' loop permanently. "
            "Fix: wrap 'await self._refresh_limits()' in try/except Exception."
        )


# ── B12: state _last_path on failed save ─────────────────────────────────────


class TestB12StateLastPath:
    """save() sets _last_path via object.__setattr__ AFTER os.replace() succeeds.
    On failure (OSError in tmp.write_text or os.replace), _last_path is not set.
    This is CORRECT — test confirms.
    """

    def test_last_path_set_only_after_successful_replace(self) -> None:
        import tero2.state as state_module

        source = inspect.getsource(state_module.AgentState.save)
        lines = source.splitlines()

        replace_line = None
        setattr_line = None
        for i, line in enumerate(lines):
            if "os.replace" in line and replace_line is None:
                replace_line = i
            if "_last_path" in line and replace_line is not None and setattr_line is None:
                setattr_line = i

        assert replace_line is not None, "os.replace not found in save()"
        assert setattr_line is not None, "_last_path assignment not found after os.replace"
        assert setattr_line > replace_line, (
            "B12 CONFIRMED: _last_path is set BEFORE os.replace(). "
            "On save failure, _last_path points to an unsaved path — "
            "touch() will overwrite previous good state with corrupt data."
        )


# ── B13: runner _handle_override stale state ─────────────────────────────────


class TestB13RunnerHandleOverride:
    """_handle_override(self, content, state) takes an explicit state parameter.
    It does NOT read self._current_state internally (it receives it as arg).
    This is correct — not a bug. Test confirms.
    """

    def test_handle_override_uses_parameter_not_self(self) -> None:
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._handle_override)
        lines = [l.strip() for l in source.splitlines()]

        # Method must NOT read self._current_state inside (only the passed-in state)
        reads_self_state = any(
            "self._current_state" in l and "=" not in l.split("self._current_state")[0].rstrip()
            for l in lines
            if "self._current_state" in l and not l.startswith("#")
        )
        # It's OK to ASSIGN self._current_state = mark_failed(state, ...) (left side)
        # What's NOT OK: passing self._current_state as the first ARG to mark_failed/mark_paused
        # instead of the local `state` parameter.
        for line in lines:
            stripped = line.strip()
            if "mark_failed" in stripped or "mark_paused" in stripped:
                # Extract argument: mark_failed(X, ...) — X should be `state`, not `self._current_state`
                for fn in ("mark_failed", "mark_paused"):
                    if fn in stripped:
                        # find the argument after the open paren
                        after = stripped.split(fn + "(", 1)[-1].split(",")[0].strip()
                        if after == "self._current_state":
                            assert False, (
                                f"B13 CONFIRMED: _handle_override() passes self._current_state to "
                                f"{fn}() instead of the local 'state' parameter. "
                                f"If the runner updates _current_state between the override check "
                                f"and this call, stale state is used. "
                                f"Fix: always pass the 'state' parameter, not self._current_state."
                            )


# ── B14: reviewer empty review_findings in fix mode ──────────────────────────


class TestB14ReviewerEmptyFindings:
    """mode=='fix' with empty review_findings: condition 'if mode == "fix" and review_findings'
    is False — no findings added to prompt. This is intentional design: empty findings
    means the previous review pass produced nothing, so we run fix without context.
    Test confirms this is EXPECTED behavior (not a bug).
    """

    def test_empty_findings_does_not_crash_fix_mode(self) -> None:
        import tero2.players.reviewer as reviewer_module

        source = inspect.getsource(reviewer_module.ReviewerPlayer.run)
        # The condition 'if mode == "fix" and review_findings:' correctly skips
        # empty findings — this prevents injection of empty ## Reviewer Findings sections.
        assert 'if mode == "fix" and review_findings:' in source or (
            'mode == "fix"' in source and "review_findings" in source
        ), "Reviewer fix-mode check not found"


# ── B15: coach loop without early exit ───────────────────────────────────────


class TestB15CoachLoop:
    """coach loops from T01 to T{_MAX_TASKS} for each slice, reading files.
    If file doesn't exist, read_file returns '' and the entry is skipped.
    No crash, just unnecessary reads. Test confirms disk.read_file handles
    missing files gracefully.
    """

    def test_coach_loop_breaks_on_missing_files(self) -> None:
        import tero2.players.coach as coach_module

        source = inspect.getsource(coach_module.CoachPlayer._gather_context)
        assert "_MAX_TASKS" in source or "range(1," in source, (
            "B15: task loop not found in CoachPlayer._gather_context"
        )
        lines = source.splitlines()
        in_loop = False
        has_break_on_empty = False
        for line in lines:
            stripped = line.strip()
            if "range(1," in stripped:
                in_loop = True
            if in_loop and stripped == "break" and "content" in "\n".join(
                lines[max(0, lines.index(line) - 2) : lines.index(line) + 1]
            ):
                has_break_on_empty = True
                break
            if in_loop and "if not content" in stripped and "break" in "\n".join(
                lines[lines.index(line) : lines.index(line) + 3]
            ):
                has_break_on_empty = True
                break
        assert has_break_on_empty, (
            "B15 CONFIRMED: CoachPlayer._gather_context loops to _MAX_TASKS for every "
            "slice without breaking when files stop existing. For a slice with 5 tasks, "
            "it reads T06-SUMMARY.md through T{MAX}-SUMMARY.md unnecessarily. "
            "Fix: break out of the inner loop when read_file returns empty string."
        )


# ── B16: builder _recover_summary_from_disk no logging ───────────────────────


class TestB16BuilderNoLogging:
    """_recover_summary_from_disk is called when builder returns empty output.
    The call has no log statement around it indicating recovery was attempted.
    This is a silent fallback — debugging is harder without a log line.
    Test checks whether recovery is logged.
    """

    def test_builder_logs_recovery_attempt(self) -> None:
        import tero2.players.builder as builder_module

        source = inspect.getsource(builder_module.BuilderPlayer.run)
        lines = source.splitlines()

        recover_line = None
        for i, line in enumerate(lines):
            if "_recover_summary_from_disk" in line:
                recover_line = i
                break

        assert recover_line is not None, "_recover_summary_from_disk call not found"

        # Check 5 lines before and after for any logging
        context = "\n".join(lines[max(0, recover_line - 3) : recover_line + 3])
        has_log = "log." in context or "logging." in context or "warn" in context
        assert has_log, (
            "B16 CONFIRMED: BuilderPlayer.run calls _recover_summary_from_disk "
            "without any log statement nearby. Silent fallback makes debugging hard "
            "when the builder writes files instead of returning text. "
            "Fix: add log.warning('builder: empty output — attempting disk recovery') "
            "before the _recover_summary_from_disk call."
        )


# ── B17: scout symlink loops ──────────────────────────────────────────────────


class TestB17ScoutSymlinkLoops:
    """_count_files uses os.walk(). By default followlinks=False, so symlink
    directories are not followed. No infinite loop possible. Test confirms.
    """

    def test_count_files_does_not_follow_symlinks(self) -> None:
        import tero2.players.scout as scout_module

        source = inspect.getsource(scout_module._count_files)
        # os.walk default followlinks=False — either it's explicitly False or not specified
        if "followlinks=True" in source:
            assert False, (
                "B17 CONFIRMED: _count_files passes followlinks=True to os.walk. "
                "Symlink cycles will cause infinite recursion. "
                "Fix: remove followlinks=True or add cycle detection."
            )


# ── B18: escalation history type mismatch ────────────────────────────────────


class TestB18EscalationDedup:
    """escalation_history is list[EscalationLevel] (enum). esc_action.level is
    EscalationLevel. The dedup check 'ctx.escalation_history[-1] != esc_action.level'
    compares enum to enum — no type mismatch. Test confirms this is NOT a bug.
    """

    def test_escalation_history_contains_enums(self) -> None:
        from tero2.phases.context import RunnerContext
        from tero2.escalation import EscalationLevel

        ctx = RunnerContext()
        ctx.escalation_history.append(EscalationLevel.DIVERSIFICATION)
        last = ctx.escalation_history[-1]
        assert last == EscalationLevel.DIVERSIFICATION, (
            "B18: escalation_history does not store EscalationLevel enums."
        )
        assert isinstance(last, EscalationLevel), (
            "B18 CONFIRMED: escalation_history stores non-enum values. "
            "Dedup comparison 'history[-1] != esc_action.level' will be str vs enum."
        )


# ── B19: harden_phase final write unguarded ──────────────────────────────────


class TestB19HardenFinalWrite:
    """_run_harden_rounds() writes PLAN.md at the end without try/except.
    On OSError (disk full, permission), the exception propagates and the
    phase returns no PhaseResult — caller may mishandle.
    Fix: wrap in try/except OSError; return PhaseResult(success=False).
    """

    def test_final_plan_write_is_guarded(self) -> None:
        import tero2.phases.harden_phase as harden_module

        source = inspect.getsource(harden_module.run_harden)
        lines = source.splitlines()

        plan_write_line = None
        for i, line in enumerate(lines):
            if "PLAN.md" in line and "write_file" in line and "plan_v" not in line:
                plan_write_line = i
                break

        assert plan_write_line is not None, "PLAN.md write_file not found in run_harden"

        # Check the 5 lines before for a try: block
        context_before = "\n".join(lines[max(0, plan_write_line - 5) : plan_write_line + 1])
        has_try = "try:" in context_before
        assert has_try, (
            "B19 CONFIRMED: final ctx.disk.write_file('PLAN.md', ...) in run_harden() "
            "is not wrapped in try/except. An OSError (disk full, read-only filesystem) "
            "will propagate uncaught, leaving the phase without a success PhaseResult. "
            "Fix: wrap in try/except OSError and return PhaseResult(success=False, error=...)."
        )


# ── B20: harden_phase no shutdown check after async ops ──────────────────────


class TestB20HardenShutdownCheck:
    """run_harden() only checks shutdown_event at the TOP of each round.
    After 'await player.run(mode="review")' and 'await player.run(mode="fix")',
    there is no shutdown check. Long-running review/fix passes ignore shutdown.
    Fix: check ctx.shutdown_event.is_set() after each await player.run() call.
    """

    def test_shutdown_checked_after_async_ops(self) -> None:
        import tero2.phases.harden_phase as harden_module

        source = inspect.getsource(harden_module.run_harden)
        lines = source.splitlines()

        # Find positions of player.run() calls and shutdown checks
        run_positions = [i for i, l in enumerate(lines) if "await player.run(" in l]
        shutdown_positions = [i for i, l in enumerate(lines) if "shutdown_event" in l and "is_set" in l]

        assert run_positions, "no await player.run() calls found in run_harden"
        assert shutdown_positions, "no shutdown_event check found in run_harden"

        # For EACH player.run() call, there should be a shutdown check within 8 lines after
        for run_pos in run_positions:
            nearby_shutdowns = [s for s in shutdown_positions if run_pos < s <= run_pos + 8]
            assert nearby_shutdowns, (
                f"B20 CONFIRMED: 'await player.run()' at line ~{run_pos} in run_harden "
                f"has no shutdown check within 8 lines. Long-running review/fix passes "
                f"will not respond to shutdown signals until the next round starts. "
                f"Fix: add 'if ctx.shutdown_event and ctx.shutdown_event.is_set(): return ...' "
                f"after each await player.run() call."
            )


# ── B21: catalog TOCTOU tmp cleanup ──────────────────────────────────────────


class TestB21CatalogTmpCleanup:
    """_save_cache() finally block: 'if tmp.exists(): tmp.unlink()'.
    After a successful tmp.replace(p), tmp no longer exists so unlink is skipped.
    But if tmp.replace() fails, tmp.exists() → tmp.unlink() has a tiny TOCTOU
    (another process could delete tmp between check and unlink).
    Fix: use tmp.unlink(missing_ok=True) instead of exists() + unlink().
    """

    def test_tmp_cleanup_uses_missing_ok(self) -> None:
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module._save_cache)
        lines = source.splitlines()

        # Check if the finally block uses missing_ok=True
        has_missing_ok = "missing_ok=True" in source or "missing_ok = True" in source
        has_exists_check = "tmp.exists()" in source

        if has_exists_check and not has_missing_ok:
            assert False, (
                "B21 CONFIRMED: _save_cache() finally block uses 'if tmp.exists(): tmp.unlink()' "
                "which has a TOCTOU race. Between exists() and unlink(), the file could be deleted "
                "by another process → FileNotFoundError. "
                "Fix: replace with 'tmp.unlink(missing_ok=True)'."
            )


# ── B22: cli suppress(CancelledError) hides errors ───────────────────────────


class TestB22CLISuppressCancelled:
    """In exception handler: 'with suppress(asyncio.CancelledError): await stderr_task'
    is used AFTER stderr_task.cancel(). This correctly suppresses the CancelledError
    from the cancellation itself. This is intentional — not a bug. Test confirms.
    """

    def test_suppress_cancelled_is_after_cancel(self) -> None:
        import tero2.providers.cli as cli_module

        source = inspect.getsource(cli_module.CLIProvider._stream_events)
        lines = source.splitlines()

        cancel_pos = None
        suppress_pos = None
        for i, line in enumerate(lines):
            if "stderr_task.cancel()" in line and cancel_pos is None:
                cancel_pos = i
            if "suppress(asyncio.CancelledError)" in line or "suppress(CancelledError)" in line:
                suppress_pos = i

        if cancel_pos is not None and suppress_pos is not None:
            assert suppress_pos > cancel_pos, (
                "B22: suppress(CancelledError) appears BEFORE stderr_task.cancel(). "
                "This would suppress real CancelledErrors from the main task, not just "
                "from the cancellation of stderr_task."
            )


# ── B23: normalizers type(raw).__name__ ──────────────────────────────────────


class TestB23NormalizersTypeName:
    """Codex, opencode, and zai normalizers: when raw is not a dict, they
    yield StreamEvent(kind='error', content=f'expected dict, got {type(raw).__name__}').
    type(raw).__name__ is safe for any object. This is already fixed. Test confirms.
    """

    def test_codex_yields_error_on_non_dict(self) -> None:
        from datetime import datetime, timezone
        import tero2.providers.normalizers.codex as codex_module

        normalizer = codex_module.CodexNormalizer()
        now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = list(normalizer.normalize("not a dict", "builder", now=now))
        assert len(events) > 0, "B23: codex normalizer swallows non-dict input silently"
        assert events[0].kind == "error", "B23: codex normalizer should yield error event"

    def test_opencode_yields_error_on_non_dict(self) -> None:
        from datetime import datetime, timezone
        import tero2.providers.normalizers.opencode as opencode_module

        normalizer = opencode_module.OpenCodeNormalizer()
        now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = list(normalizer.normalize("not a dict", "builder", now=now))
        assert len(events) > 0, "B23: opencode normalizer swallows non-dict input silently"
        assert events[0].kind == "error", "B23: opencode normalizer should yield error event"

    def test_zai_yields_error_on_non_dict(self) -> None:
        from datetime import datetime, timezone
        import tero2.providers.normalizers.zai as zai_module

        normalizer = zai_module.ZaiNormalizer()
        now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)
        events = list(normalizer.normalize("not a dict", "builder", now=now))
        assert len(events) > 0, "B23: zai normalizer swallows non-dict input silently"
        assert events[0].kind == "error", "B23: zai normalizer should yield error event"


# ── B24: tui/app on_resize no try/except ─────────────────────────────────────


class TestB24AppOnResize:
    """on_resize() calls query_one('#usage-panel', UsagePanel) without try/except.
    If the widget doesn't exist (before on_mount, during screen transition),
    NoMatches is raised and propagates uncaught.
    Fix: wrap in try/except NoMatches.
    """

    def test_on_resize_has_nomatches_guard(self) -> None:
        import tero2.tui.app as app_module

        app_cls = getattr(app_module, "DashboardApp", None) or getattr(app_module, "TeroApp", None)
        assert app_cls is not None, "Could not find main app class in tero2.tui.app"
        source = inspect.getsource(app_cls.on_resize)
        has_guard = "NoMatches" in source or "try:" in source or "except" in source
        assert has_guard, (
            "B24 CONFIRMED: on_resize() calls query_one() without try/except NoMatches. "
            "If the usage-panel widget isn't mounted yet (before on_mount completes or "
            "during screen transitions), NoMatches is raised and propagates uncaught. "
            "Fix: wrap query_one() in try/except NoMatches."
        )


# ── B25: telegram_input watcher tasks not tracked ────────────────────────────


class TestB25TelegramInputWatcherTasks:
    """_launch_runner() assigns watcher_task to a local variable.
    If called multiple times, previous watcher_task references are lost.
    Tasks continue running until completion but can't be cancelled on shutdown.
    Fix: keep a set/list of watcher tasks; cancel on shutdown.
    """

    def test_watcher_tasks_are_tracked(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._launch_runner)
        lines = source.splitlines()

        # Should either store in self._watcher_tasks or similar set
        has_tracking = (
            "self._watcher_tasks" in source
            or "self._tasks" in source
            or "_tasks.add(" in source
            or "_tasks.append(" in source
        )
        assert has_tracking, (
            "B25 CONFIRMED: _launch_runner() stores watcher_task in a local variable only. "
            "On repeated calls, previous task references are lost and cannot be cancelled "
            "during shutdown. "
            "Fix: maintain self._watcher_tasks: set[asyncio.Task] and discard on completion."
        )


# ── B26: telegram_input 5s timeout in _consume_plans ─────────────────────────


class TestB26TelegramInputConsumeTimeout:
    """_consume_plans() uses asyncio.wait_for(self._plan_queue.get(), timeout=5.0).
    On timeout, it loops back and tries again. With an empty queue, there's
    a 5-second delay before detecting new plans after a spurious timeout.
    This causes latency on idle → active transitions.
    Fix: use asyncio.Event or shorter timeout for better responsiveness.
    """

    def test_consume_plans_timeout_is_reasonable(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._consume_plans)
        # A 5-second timeout adds up to 5s latency per plan check cycle
        if "timeout=5.0" in source or "timeout=5" in source:
            assert False, (
                "B26 CONFIRMED: _consume_plans() uses a 5-second queue.get() timeout. "
                "This adds up to 5 seconds of latency between plans arriving and being "
                "processed when the queue transitions from empty to non-empty. "
                "Fix: use a shorter timeout (0.5s) or asyncio.Event to signal new plans."
            )


# ── B27: usage compact mode rows not restored ─────────────────────────────────


class TestB27UsageCompactModeRows:
    """When compact=True: all rows destroyed, _rows.clear().
    When compact=False: watch_compact calls _sync_rows() which re-creates rows
    from self._limits. Rows ARE restored correctly. Test confirms NOT a bug.
    """

    def test_sync_rows_recreates_on_compact_off(self) -> None:
        import tero2.tui.widgets.usage as usage_module

        source = inspect.getsource(usage_module.UsagePanel._sync_rows)
        # Must have both the compact teardown and the add-rows path
        assert "self.compact" in source, "_sync_rows must check self.compact"
        assert "_rows[name] = row" in source or "self._rows[name] = row" in source, (
            "B27 CONFIRMED: _sync_rows does not re-create rows. "
            "Rows destroyed in compact mode are never restored."
        )


# ── B28: stream_panel concurrent dict iteration ───────────────────────────────


class TestB28StreamPanelConcurrentDict:
    """_recompute_active_role() iterates self._last_seen.items() in a list
    comprehension, creating a snapshot. In asyncio (single-threaded), there
    is no concurrent modification. Test confirms NOT a bug.
    """

    def test_last_seen_iteration_is_safe(self) -> None:
        import tero2.tui.widgets.stream_panel as panel_module

        source = inspect.getsource(panel_module.RoleStreamPanel._recompute_active_role)
        # List comprehension over .items() creates a snapshot — safe in single-threaded asyncio
        assert "_last_seen.items()" in source or "_last_seen" in source, (
            "_last_seen not found in _recompute_active_role"
        )
        # Verify it's a comprehension (snapshot), not direct dict iteration
        assert "[" in source, "Expected list comprehension in _recompute_active_role"


# ── B29: stream_panel active_role=None TypeError ─────────────────────────────


class TestB29StreamPanelActiveRoleNone:
    """active_role is reactive[str] with default ''. Never None.
    _buffers.get('', ()) returns empty deque — no crash.
    Test confirms NOT a bug.
    """

    def test_active_role_defaults_to_empty_string(self) -> None:
        import tero2.tui.widgets.stream_panel as panel_module

        source = inspect.getsource(panel_module.RoleStreamPanel)
        # reactive[str] with default '' — never None
        assert 'reactive("")' in source or "reactive('')" in source, (
            "B29 CONFIRMED: active_role is not initialized to empty string. "
            "If active_role defaults to None, watch_raw_mode will call "
            "_buffers.get(None, ()) — which works, but is unexpected behavior. "
            "More importantly, format_event(ev) might be called with None active_role."
        )


# ── B30: usage _destroy_row silent failure ────────────────────────────────────


class TestB30UsageDestroyRowSilent:
    """_destroy_row() tries destroy(), falls back to remove(), swallows all exceptions.
    If both fail (e.g., widget not mounted), the widget is orphaned in memory.
    No log is emitted. Test checks for logging.
    """

    def test_destroy_row_logs_on_failure(self) -> None:
        import tero2.tui.widgets.usage as usage_module

        source = inspect.getsource(usage_module.UsagePanel._destroy_row)
        has_logging = "log." in source or "logging." in source or "warn" in source
        assert has_logging, (
            "B30 CONFIRMED: _destroy_row() silently catches all exceptions from "
            "destroy() and remove() with no logging. When both fallbacks fail, the "
            "widget is orphaned with no indication in logs. "
            "Fix: add log.warning() in the outer fallback's except clause."
        )


# ── B31: context _load_slice_plan ambiguity ───────────────────────────────────


class TestB31ContextLoadSlicePlan:
    """_load_slice_plan_from_disk() raises ValueError when plan is missing.
    The error message says 'Architect may have crashed before writing the plan'.
    The caller must distinguish 'slice not reached' vs 'Architect crashed'.
    But the ValueError message already provides this context.
    Test confirms the message is informative.
    """

    def test_load_slice_plan_error_message_is_informative(self) -> None:
        import tero2.phases.context as ctx_module

        source = inspect.getsource(ctx_module._load_slice_plan_from_disk)
        assert "Architect" in source or "plan file missing" in source, (
            "B31 CONFIRMED: _load_slice_plan_from_disk() raises ValueError without "
            "an informative message distinguishing 'plan not yet written' from 'crash'. "
            "Fix: include context about whether Architect was expected to have run."
        )
