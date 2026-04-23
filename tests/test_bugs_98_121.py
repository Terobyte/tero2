"""Halal tests for bugs 98–121 (Audit 4, 2026-04-21).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug 98   stream_bus: TOCTOU race in ring-buffer drop policy
  Bug 99   chain: circuit breaker not updated on non-recoverable errors
  Bug 100  telegram_input: queue drain race in stop()
  Bug 101  execute_phase: escalation level desync between context and state
  Bug 102  checkpoint: triple disk write in checkpoint methods
  Bug 103  execute_phase: metrics update failure crashes entire phase
  Bug 104  cli: AssertionError crash when proc.stdout is None
  Bug 105  shell: no cleanup on success path
  Bug 106  telegram_input: _watch_runner background task exception silently lost
  Bug 107  TUI heartbeat_sidebar: IndexError on empty content
  Bug 108  TUI settings: IndexError when chat_ids field is empty
  Bug 109  notifier: Telegram API 200 with ok:false treated as success
  Bug 110  history: PermissionError not caught in load_history()
  Bug 111  stream_bus: queues not drained on unsubscribe — memory leak
  Bug 112  stream_bus: overly broad exception masking
  Bug 113  stream_bus: silent event loss on queue overflow
  Bug 114  catalog: silent timezone assumption serves stale cache
  Bug 115  catalog: orphaned .tmp files in cache directory
  Bug 116  catalog: subprocess kill/wait race on timeout
  Bug 117  execute_phase: escalation history duplicates
  Bug 118  telegram_input: file download missing encoding
  Bug 119  chain: provider index not reset after all providers fail
  Bug 120  notifier: missing timeout on asyncio.to_thread calls
  Bug 121  TUI usage panel: resource leak on row removal
"""

from __future__ import annotations

import inspect

import pytest


# ── Bug 98: stream_bus TOCTOU race in ring-buffer drop policy ────────────────


class TestBug98StreamBusTOCTOU:
    """TOCTOU: check q.full() then get_nowait() then put_nowait() is racy.
    Fix: attempt put_nowait() first; only get_nowait() + retry on QueueFull.
    """

    def test_no_toctou_full_check(self) -> None:
        import tero2.stream_bus as bus_module

        source = inspect.getsource(bus_module.StreamBus._publish_impl)
        # Strip docstring (everything between first triple-quote and second)
        code_only = source
        if '"""' in source:
            end = source.index('"""', source.index('"""') + 3) + 3
            code_only = source[end:]

        assert "if q.full():" not in code_only, (
            "Bug 98: _publish_impl still uses 'if q.full():' before put_nowait(). "
            "TOCTOU race: between the full() check and get_nowait(), another "
            "consumer can drain the queue. "
            "Fix: try put_nowait() first; only get_nowait() + retry on QueueFull."
        )

    def test_catches_only_queue_errors_in_drop_path(self) -> None:
        import tero2.stream_bus as bus_module

        source = inspect.getsource(bus_module.StreamBus._publish_impl)
        assert "except (asyncio.QueueEmpty, Exception)" not in source, (
            "Bug 98/112: _publish_impl catches bare Exception alongside QueueEmpty. "
            "Use specific queue exceptions only."
        )


# ── Bug 99: chain circuit breaker not updated on non-recoverable errors ───────


class TestBug99ChainCircuitBreakerNonRecoverable:
    """Non-recoverable errors bypass cb.record_failure(). Broken provider
    is never fast-failed — wastes API credits on every call.
    Fix: call cb.record_failure() before re-raising.
    """

    def test_record_failure_before_non_recoverable_reraise(self) -> None:
        import tero2.providers.chain as chain_module

        source = inspect.getsource(chain_module.ProviderChain.run)
        lines = source.splitlines()

        found_non_recoverable = False
        has_record_failure_before_raise = False

        for i, line in enumerate(lines):
            if "not _is_recoverable_error" in line or "non-recoverable" in line.lower():
                found_non_recoverable = True
                for k in range(i, min(i + 8, len(lines))):
                    if "record_failure" in lines[k]:
                        has_record_failure_before_raise = True
                        break
                    if lines[k].strip() == "raise":
                        break
                break

        if not found_non_recoverable:
            pytest.skip("non-recoverable check not found in ProviderChain.run")

        assert has_record_failure_before_raise, (
            "Bug 99: non-recoverable errors in ProviderChain.run skip "
            "cb.record_failure(). The circuit breaker never opens for this "
            "provider — every call retries it, burning API credits. "
            "Fix: call cb.record_failure() before re-raising non-recoverable errors."
        )


# ── Bug 100: telegram_input queue drain race in stop() ────────────────────────


class TestBug100TelegramInputStopDrainRace:
    """stop() drains plan queue with get_nowait() while _consume_plans()
    may be between get() and task_done(). task_done() called twice → ValueError.
    Fix: don't drain in stop(); just set _running = False.
    """

    def test_stop_does_not_drain_queue(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot.stop)
        assert "get_nowait" not in source, (
            "Bug 100: stop() calls get_nowait() to drain the queue while "
            "_consume_plans() may be between get() and task_done(). "
            "This causes task_done() to be called twice → ValueError on join(). "
            "Fix: don't drain queue in stop(); just set _running = False and "
            "let _consume_plans finish naturally."
        )

    def test_stop_sets_running_false(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot.stop)
        assert "_running = False" in source, (
            "Bug 100: stop() must set _running = False to signal _consume_plans "
            "to exit cleanly."
        )


# ── Bug 101: execute_phase escalation level desync ────────────────────────────


class TestBug101EscalationLevelDesync:
    """ctx.escalation_level (enum) not synced from ctx.state.escalation_level
    (int) after execute_escalation() updates the state.
    Fix: ctx.escalation_level = EscalationLevel(ctx.state.escalation_level).
    """

    def test_escalation_level_synced_after_execute_escalation(self) -> None:
        import tero2.phases.execute_phase as ep_module

        source = inspect.getsource(ep_module.run_execute)
        lines = source.splitlines()

        found_execute_escalation = False
        synced_after = False

        for i, line in enumerate(lines):
            if "execute_escalation" in line and "await" in line:
                found_execute_escalation = True
            if found_execute_escalation:
                if "EscalationLevel(" in line and "ctx.escalation_level" in line:
                    synced_after = True
                    break

        if not found_execute_escalation:
            pytest.skip("execute_escalation call not found in run_execute")

        assert synced_after, (
            "Bug 101: after execute_escalation() updates ctx.state, "
            "ctx.escalation_level (enum) is never re-synced from "
            "ctx.state.escalation_level (int). Subsequent escalation "
            "decisions use the stale enum value. "
            "Fix: ctx.escalation_level = EscalationLevel(ctx.state.escalation_level) "
            "after the execute_escalation call."
        )


# ── Bug 102: checkpoint triple disk write ─────────────────────────────────────


class TestBug102CheckpointTripleWrite:
    """mark_completed/failed/paused/running call state.touch() then save(),
    which calls touch() again and write_state(). 3x I/O per checkpoint.
    Fix: remove redundant state.touch() from the mark_* methods.
    """

    def test_mark_completed_no_redundant_touch(self) -> None:
        import tero2.checkpoint as cp_module

        source = inspect.getsource(cp_module.CheckpointManager.mark_completed)
        assert "state.touch()" not in source, (
            "Bug 102: mark_completed calls state.touch() before save(). "
            "save() already writes state — this is a redundant touch+write. "
            "Fix: remove state.touch() from mark_completed."
        )

    def test_mark_failed_no_redundant_touch(self) -> None:
        import tero2.checkpoint as cp_module

        source = inspect.getsource(cp_module.CheckpointManager.mark_failed)
        assert "state.touch()" not in source, (
            "Bug 102: mark_failed calls state.touch() before save(). "
            "Fix: remove state.touch() from mark_failed."
        )

    def test_mark_paused_no_redundant_touch(self) -> None:
        import tero2.checkpoint as cp_module

        source = inspect.getsource(cp_module.CheckpointManager.mark_paused)
        assert "state.touch()" not in source, (
            "Bug 102: mark_paused calls state.touch() before save(). "
            "Fix: remove state.touch() from mark_paused."
        )

    def test_mark_running_no_redundant_touch(self) -> None:
        import tero2.checkpoint as cp_module

        source = inspect.getsource(cp_module.CheckpointManager.mark_running)
        assert "state.touch()" not in source, (
            "Bug 102: mark_running calls state.touch() before save(). "
            "Fix: remove state.touch() from mark_running."
        )


# ── Bug 103: execute_phase metrics update crashes phase ───────────────────────


class TestBug103MetricsUpdateNoCrash:
    """_update_task_metrics() called with no error handling. OSError on
    disk write kills the entire phase, losing all task progress.
    Fix: wrap in try/except OSError with log.warning.
    """

    def test_metrics_update_wrapped_in_try_except(self) -> None:
        import tero2.phases.execute_phase as ep_module

        source = inspect.getsource(ep_module.run_execute)
        lines = source.splitlines()

        found_metrics = False
        wrapped_in_try = False

        for i, line in enumerate(lines):
            if "_update_task_metrics" in line:
                found_metrics = True
                start = max(0, i - 5)
                context = "\n".join(lines[start : i + 5])
                if "try:" in context and ("OSError" in context or "Exception" in context):
                    wrapped_in_try = True
                break

        if not found_metrics:
            pytest.skip("_update_task_metrics call not found in run_execute")

        assert wrapped_in_try, (
            "Bug 103: _update_task_metrics() is called without error handling. "
            "An OSError (permission denied, disk full) propagates and kills the "
            "entire phase — all task progress is lost. "
            "Fix: wrap in try/except OSError with log.warning."
        )


# ── Bug 104: cli AssertionError when proc.stdout is None ─────────────────────


class TestBug104CLIAssertStdout:
    """assert proc.stdout is not None crashes in production with -O flag.
    Fix: replace with if proc.stdout is None: raise ProviderError(...).
    """

    def test_no_assert_for_stdout(self) -> None:
        import tero2.providers.cli as cli_module

        source = inspect.getsource(cli_module.CLIProvider.run)
        assert "assert proc.stdout" not in source, (
            "Bug 104: CLIProvider.run uses 'assert proc.stdout is not None' "
            "which raises AssertionError (not ProviderError) and is disabled "
            "with python -O. Fix: replace with "
            "'if proc.stdout is None: raise ProviderError(...)'."
        )

    def test_explicit_none_check_raises_provider_error(self) -> None:
        import tero2.providers.cli as cli_module

        source = inspect.getsource(cli_module.CLIProvider.run)
        has_explicit_check = (
            "proc.stdout is None" in source
            or "stdout is None" in source
        )
        assert has_explicit_check, (
            "Bug 104: CLIProvider.run must have an explicit None check for "
            "proc.stdout that raises ProviderError (not AssertionError)."
        )


# ── Bug 105: shell provider no cleanup on success path ───────────────────────


class TestBug105ShellSuccessCleanup:
    """Success path yields without explicit cleanup — potential resource leak
    if process outlives the generator.
    Fix: use communicate() (which waits for process exit) or add proc.wait().
    """

    def test_success_path_uses_communicate(self) -> None:
        import tero2.providers.shell as shell_module

        source = inspect.getsource(shell_module.ShellProvider.run)
        assert "communicate" in source, (
            "Bug 105: ShellProvider.run success path yields without waiting "
            "for the process to fully exit. Resources may leak. "
            "Fix: use proc.communicate() (which waits for exit before yield) "
            "or add await proc.wait() before yielding."
        )


# ── Bug 106: telegram_input watcher task reference lost ──────────────────────


class TestBug106TelegramInputWatcherTask:
    """asyncio.create_task() result never stored — if watcher crashes, the
    exception is swallowed by asyncio with no Telegram notification.
    Fix: store task and add add_done_callback for exception logging.
    """

    def test_watch_runner_task_reference_stored(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._launch_runner)

        # Accept any of these reference-preserving contracts:
        #   (a) explicit assignment of create_task result: `task = asyncio.create_task(...)`
        #   (b) delegation to TaskSupervisor.spawn(...)
        #   (c) registration into a set: `self._watcher_tasks.add(...)`
        # The spawn() call is checked against whole-source so multi-line
        # arguments do not defeat the pattern.
        has_spawn = "self._tasks.spawn" in source and "_watch_runner" in source
        has_watcher_set = "_watcher_tasks" in source and (
            "_watcher_tasks.add(" in source or "_watcher_tasks.append(" in source
        )
        has_assigned_create_task = False
        for line in source.splitlines():
            stripped = line.strip()
            if "create_task" in stripped and "_watch_runner" in stripped:
                if not stripped.startswith("asyncio.create_task"):
                    has_assigned_create_task = True
                break

        task_stored = has_spawn or has_watcher_set or has_assigned_create_task

        assert task_stored, (
            "Bug 106: _watch_runner task reference is not stored. Fire-and-forget "
            "create_task() loses the reference — the watcher cannot be cancelled "
            "and its exceptions are only logged at process exit. "
            "Fix: assign to a variable, register with a TaskSupervisor via "
            "self._tasks.spawn(...), or store in self._watcher_tasks."
        )

    def test_watch_runner_task_has_done_callback(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._launch_runner)
        # Done-callback is either registered explicitly (via add_done_callback)
        # or implicitly by spawning the task through TaskSupervisor — the
        # supervisor attaches its own done-callback that logs uncaught
        # exceptions and discards the task from the live set.
        has_callback = (
            "add_done_callback" in source or "self._tasks.spawn" in source
        )
        assert has_callback, (
            "Bug 106: _watch_runner task has no done_callback. "
            "Silent crashes remain invisible. "
            "Fix: task.add_done_callback(lambda t: log.error(...)) or route "
            "the task through TaskSupervisor.spawn()."
        )


# ── Bug 107: heartbeat_sidebar IndexError on empty content ───────────────────


class TestBug107HeartbeatSidebarEmptyContent:
    """event.content.splitlines()[0] can IndexError when content is empty
    but the truthiness guard is bypassed or not present at all.
    Fix: guard with: lines = content.splitlines(); m.last_line = lines[0] if lines else "".
    """

    def test_splitlines_index_guarded(self) -> None:
        import tero2.tui.widgets.heartbeat_sidebar as hs_module

        source = inspect.getsource(hs_module)
        lines_src = source.splitlines()

        for i, line in enumerate(lines_src):
            stripped = line.strip()
            # Flag only completely unguarded direct index access
            if "splitlines()[0]" in stripped and " if " not in stripped:
                pytest.fail(
                    f"Bug 107: heartbeat_sidebar.py line {i+1} accesses "
                    f"splitlines()[0] without any guard: {stripped}. "
                    "Fix: use 'lines[0] if lines else \"\"' pattern."
                )


# ── Bug 108: settings IndexError when chat_ids is empty ──────────────────────


class TestBug108SettingsChatIdsEmpty:
    """chat_ids[0] accessed without checking list is non-empty.
    Fix: chat_id = chat_ids[0] if chat_ids else "".
    """

    def test_chat_ids_access_guarded(self) -> None:
        import tero2.tui.screens.settings as settings_module

        source = inspect.getsource(settings_module)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            stripped = line.strip()
            if (
                "chat_ids[0]" in stripped
                and "if chat_ids" not in stripped
                and "if len" not in stripped
            ):
                pytest.fail(
                    f"Bug 108: settings.py line {i+1} accesses chat_ids[0] "
                    f"without empty-list guard: {stripped}. "
                    "Fix: chat_id = chat_ids[0] if chat_ids else ''"
                )


# ── Bug 109: notifier Telegram ok:false treated as success ───────────────────


class TestBug109NotifierOkFalse:
    """Telegram can return HTTP 200 with {"ok": false}. Checking only
    status_code == 200 causes the caller to think notification was delivered.
    Fix: also check resp.json().get("ok") is True.
    """

    def test_send_checks_ok_field(self) -> None:
        import tero2.notifier as notifier_module

        source = inspect.getsource(notifier_module.Notifier.send)
        has_ok_check = (
            '"ok"' in source
            or "'ok'" in source
        )
        assert has_ok_check, (
            "Bug 109: Notifier.send only checks resp.status_code == 200. "
            "Telegram returns HTTP 200 with {'ok': false} on errors like "
            "'chat not found' — caller thinks message was delivered. "
            "Fix: also check resp.json().get('ok') is True."
        )


# ── Bug 110: history PermissionError not caught ───────────────────────────────


class TestBug110HistoryPermissionError:
    """load_history() catches FileNotFoundError, JSONDecodeError, TypeError
    but not PermissionError/OSError. Wrong file permissions crash any operation.
    Fix: add OSError to the exception tuple.
    """

    def test_load_history_catches_oserror(self) -> None:
        import tero2.history as history_module

        source = inspect.getsource(history_module.load_history)
        has_oserror = "OSError" in source or "PermissionError" in source
        assert has_oserror, (
            "Bug 110: load_history() does not catch OSError/PermissionError. "
            "If the history file has wrong permissions, any tero2 operation "
            "crashes with an unhandled exception. "
            "Fix: add OSError to the except tuple."
        )

    def test_load_history_returns_empty_on_permission_error(
        self, tmp_path: "Path", monkeypatch: "pytest.MonkeyPatch"
    ) -> None:
        import os
        from pathlib import Path
        import tero2.history as history_module

        hist_file = tmp_path / "history.json"
        hist_file.write_text("[]", encoding="utf-8")
        hist_file.chmod(0o000)

        monkeypatch.setattr(history_module, "HISTORY_FILE", hist_file)

        try:
            result = history_module.load_history()
            assert isinstance(result, list), (
                "Bug 110: load_history must return a list even on PermissionError"
            )
        except (PermissionError, OSError):
            pytest.fail(
                "Bug 110: load_history raises PermissionError instead of "
                "catching it and returning a default value. "
                "Fix: add OSError to the except tuple."
            )
        finally:
            hist_file.chmod(0o644)


# ── Bug 111: stream_bus unsubscribe memory leak ───────────────────────────────


class TestBug111StreamBusUnsubscribeDrain:
    """unsubscribe() removes queue from _subscribers but doesn't drain it.
    Queue + StreamEvent objects retained. Long-running app leaks memory.
    Fix: drain queue before removing.
    """

    def test_unsubscribe_drains_queue(self) -> None:
        import tero2.stream_bus as bus_module

        source = inspect.getsource(bus_module.StreamBus.unsubscribe)
        has_drain = (
            "get_nowait" in source
            or "empty()" in source
            or "drain" in source
        )
        assert has_drain, (
            "Bug 111: StreamBus.unsubscribe() removes the queue from "
            "_subscribers without draining its pending events. "
            "Queue and StreamEvent objects remain in memory — memory leak "
            "in long-running apps with many subscribe/unsubscribe cycles. "
            "Fix: drain the queue before removing it."
        )


# ── Bug 112: stream_bus overly broad exception masking ────────────────────────


class TestBug112StreamBusBroadException:
    """Catches bare Exception alongside QueueEmpty/QueueFull. Masks
    AttributeError, TypeError from malformed subscriber objects.
    Fix: catch only (asyncio.QueueEmpty, asyncio.QueueFull).
    """

    def test_publish_impl_no_bare_exception_catch(self) -> None:
        import tero2.stream_bus as bus_module

        source = inspect.getsource(bus_module.StreamBus._publish_impl)
        assert "except (asyncio.QueueEmpty, Exception)" not in source, (
            "Bug 112: _publish_impl catches (asyncio.QueueEmpty, Exception) — "
            "bare Exception masks AttributeError/TypeError from malformed "
            "subscriber objects, making them very hard to debug. "
            "Fix: catch only (asyncio.QueueEmpty, asyncio.QueueFull)."
        )
        assert "except (asyncio.QueueFull, Exception)" not in source, (
            "Bug 112: _publish_impl catches (asyncio.QueueFull, Exception) — "
            "overly broad. Fix: catch only specific queue exceptions."
        )


# ── Bug 113: stream_bus silent event loss ─────────────────────────────────────


class TestBug113StreamBusSilentEventLoss:
    """If put_nowait() fails even after drop attempt, exception caught silently.
    No logging or counter — operators can't detect persistent overflow.
    Fix: add dropped-event counter or log.debug.
    """

    def test_publish_impl_logs_or_counts_dropped_events(self) -> None:
        import tero2.stream_bus as bus_module

        source = inspect.getsource(bus_module.StreamBus._publish_impl)
        has_observability = (
            "log." in source
            or "_dropped" in source
            or "dropped" in source
        )
        assert has_observability, (
            "Bug 113: _publish_impl silently swallows put_nowait() failures. "
            "No logging or counter — operators have no visibility into "
            "persistent queue overflow. "
            "Fix: add log.debug or a dropped-event counter when put fails."
        )


# ── Bug 114: catalog stale timezone assumption ────────────────────────────────


class TestBug114CatalogStaleTimezone:
    """Cached entries without timezone treated as UTC. If cache was written
    by a different timezone system, age calculation is wrong — stale entries
    served as fresh.
    Fix: treat missing timezone as invalid cache; return None.
    """

    def test_no_naive_utc_assumption(self) -> None:
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module)
        has_assumption = (
            "replace(tzinfo=timezone.utc)" in source
            and "tzinfo is None" in source
        )
        assert not has_assumption, (
            "Bug 114: catalog.py assumes naive datetime == UTC by calling "
            "fetched_at.replace(tzinfo=timezone.utc). If cache was written "
            "on a system with a different timezone, age calculation is wrong. "
            "Fix: treat tzinfo=None as invalid/stale cache and return None."
        )


# ── Bug 115: catalog orphaned .tmp files ──────────────────────────────────────


class TestBug115CatalogOrphanedTmpFiles:
    """Atomic write creates .tmp then renames. If process crashes between
    write and rename, orphaned .tmp files accumulate forever.
    Fix: glob and delete *.tmp on module init.
    """

    def test_module_cleans_tmp_on_init(self) -> None:
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module)
        has_cleanup = (
            "*.tmp" in source
            and ("unlink" in source or "remove" in source or "delete" in source)
        )
        assert has_cleanup, (
            "Bug 115: catalog.py creates .tmp files for atomic writes but "
            "never cleans them up if the process crashes mid-write. "
            "Orphaned .tmp files accumulate in the cache directory. "
            "Fix: glob *.tmp files and delete them on module initialization."
        )


# ── Bug 116: catalog subprocess kill/wait race ────────────────────────────────


class TestBug116CatalogKillWaitRace:
    """Between proc.returncode is None check and proc.kill(), process may
    exit naturally. proc.wait() after kill may misbehave.
    Fix: wrap kill+wait in try/except (ProcessLookupError, OSError).
    """

    def test_wait_after_kill_has_exception_handler(self) -> None:
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module)
        lines = source.splitlines()

        found_kill = False
        kill_idx = None
        wait_idx = None

        for i, line in enumerate(lines):
            if "proc.kill()" in line and kill_idx is None:
                found_kill = True
                kill_idx = i
            if found_kill and "proc.wait()" in line and wait_idx is None:
                wait_idx = i
                break

        if not found_kill or wait_idx is None:
            pytest.skip("proc.kill()/proc.wait() sequence not found in catalog module")

        # Check that wait() is inside a try block that DIRECTLY wraps it
        # (not just the kill() try block above it)
        segment = "\n".join(lines[kill_idx : wait_idx + 2])
        # The try: surrounding proc.wait() must appear AFTER proc.kill()
        has_try_around_wait = (
            segment.count("try:") >= 2  # one for kill, one for wait
            or (
                "try:" in "\n".join(lines[wait_idx - 3 : wait_idx + 1])
                and ("OSError" in "\n".join(lines[wait_idx : wait_idx + 5])
                     or "ProcessLookupError" in "\n".join(lines[wait_idx : wait_idx + 5]))
            )
        )
        assert has_try_around_wait, (
            "Bug 116: catalog.py calls proc.wait() after proc.kill() without "
            "a separate try/except wrapping proc.wait() itself. "
            "If the process exits between kill() and wait(), OSError may raise. "
            "Fix: wrap proc.kill()+proc.wait() together in "
            "try/except (ProcessLookupError, OSError)."
        )


# ── Bug 117: execute_phase escalation history duplicates ─────────────────────


class TestBug117EscalationHistoryDuplicates:
    """Same escalation level appended on each trigger. After two DIVERSIFICATION
    escalations, history = [DIVERSIFICATION, DIVERSIFICATION].
    Fix: check history[-1] != esc_action.level before appending.
    """

    def test_escalation_history_dedup_check(self) -> None:
        import tero2.phases.execute_phase as ep_module

        source = inspect.getsource(ep_module.run_execute)
        lines = source.splitlines()

        found_append = False
        has_dedup = False

        for i, line in enumerate(lines):
            if "escalation_history.append" in line:
                found_append = True
                start = max(0, i - 5)
                context = "\n".join(lines[start : i + 2])
                if (
                    "escalation_history[-1]" in context
                    or "not in escalation_history" in context
                ):
                    has_dedup = True
                break

        if not found_append:
            pytest.skip("escalation_history.append not found in run_execute")

        assert has_dedup, (
            "Bug 117: escalation_history.append has no deduplication guard. "
            "Two tasks at the same DIVERSIFICATION level produce "
            "[DIVERSIFICATION, DIVERSIFICATION] — misleading stuck reports. "
            "Fix: check escalation_history[-1] != esc_action.level before appending."
        )


# ── Bug 118: telegram_input file download missing encoding ───────────────────


class TestBug118TelegramInputFileEncoding:
    """resp.text used without encoding. Files in non-UTF-8 encodings produce
    mojibake — requests falls back to ISO-8859-1.
    Fix: use resp.content.decode("utf-8", errors="replace").
    """

    def test_file_download_uses_content_decode(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._download_file)
        assert "resp.text" not in source, (
            "Bug 118: _download_file uses resp.text without specifying encoding. "
            "The requests library falls back to ISO-8859-1 for text/*, "
            "producing mojibake for UTF-8 or Windows-1251 files. "
            "Fix: use resp.content.decode('utf-8', errors='replace')."
        )

    def test_file_download_specifies_encoding(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._download_file)
        has_encoding = "decode(" in source or "encoding=" in source
        assert has_encoding, (
            "Bug 118: _download_file must explicitly specify encoding when "
            "reading file content. Use resp.content.decode('utf-8', errors='replace')."
        )


# ── Bug 119: chain provider index not reset after exhaustion ─────────────────


class TestBug119ChainProviderIndexReset:
    """After all providers fail, _current_provider_index stays at last failed.
    provider_kind() returns stale name, confusing stream normalizer dispatch.
    Fix: reset _current_provider_index = 0 before raising exhaustion error.
    """

    def test_provider_index_reset_before_exhaustion_raise(self) -> None:
        import tero2.providers.chain as chain_module

        source = inspect.getsource(chain_module.ProviderChain.run)
        lines = source.splitlines()

        found_exhaustion = False
        has_reset = False

        for i, line in enumerate(lines):
            if "any_attempted" in line and "not" in line:
                found_exhaustion = True
            if found_exhaustion:
                if "_current_provider_index = 0" in line:
                    has_reset = True
                    break
                if "raise" in line and "ProviderError" in line:
                    break

        if not found_exhaustion:
            pytest.skip("exhaustion check not found in ProviderChain.run")

        assert has_reset, (
            "Bug 119: ProviderChain.run does not reset _current_provider_index "
            "before raising the exhaustion ProviderError. After all providers fail, "
            "provider_kind() returns the stale last-failed provider name, "
            "confusing stream normalizer dispatch. "
            "Fix: set self._current_provider_index = 0 before raising."
        )


# ── Bug 120: notifier missing timeout on asyncio.to_thread ───────────────────


class TestBug120NotifierToThreadTimeout:
    """asyncio.to_thread has no timeout. If thread pool saturated, call blocks
    indefinitely, stalling event loop. requests timeout covers HTTP only.
    Fix: wrap in asyncio.wait_for(..., timeout=15).
    """

    def test_send_uses_wait_for(self) -> None:
        import tero2.notifier as notifier_module

        source = inspect.getsource(notifier_module.Notifier.send)
        assert "wait_for" in source, (
            "Bug 120: Notifier.send wraps requests calls in asyncio.to_thread "
            "without asyncio.wait_for timeout. If the thread pool is saturated, "
            "the call blocks forever, stalling the event loop. The requests "
            "timeout only covers HTTP, not thread queueing. "
            "Fix: wrap asyncio.to_thread(...) in asyncio.wait_for(..., timeout=15)."
        )


# ── Bug 121: TUI usage panel resource leak on row removal ────────────────────


class TestBug121UsagePanelRowDestroy:
    """row.remove() unmounts widget but ProgressBar timers may keep firing.
    Should use row.destroy() for full cleanup.
    Fix: call row.destroy() instead of row.remove().
    """

    def test_row_removal_uses_destroy(self) -> None:
        import tero2.tui.widgets.usage as usage_module

        source = inspect.getsource(usage_module)
        assert "row.remove()" not in source, (
            "Bug 121: usage.py calls row.remove() to remove provider rows. "
            "remove() only unmounts the widget — ProgressBar internal timers "
            "may keep firing, leaking resources. "
            "Fix: use row.destroy() instead of row.remove() for full cleanup."
        )

    def test_row_cleanup_uses_destroy(self) -> None:
        import tero2.tui.widgets.usage as usage_module

        source = inspect.getsource(usage_module)
        assert "destroy()" in source, (
            "Bug 121: usage.py must call destroy() (not remove()) on provider "
            "rows to ensure ProgressBar timers are fully cleaned up."
        )
