"""Halal tests for bugs 122–144 (Audit 5, 2026-04-21).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug 122  context: div_steps and escalation_history never persisted to AgentState
  Bug 123  telegram_input: fire-and-forget stderr drain task
  Bug 124  disk_layer: TOCTOU race in delta-based metrics write
  Bug 125  chain: provider_kind returns stale value after circuit-breaker skip
  Bug 126  normalizers: codex/opencode/zai silently drop unparseable input
  Bug 127  execute_phase: state mutated before checkpoint save — corruption on failure
  Bug 128  phases/context: heartbeat task reference not stored
  Bug 129  cli provider: subprocess leaks on early consumer break
  Bug 130  usage_tracker: accumulator updates outside lock
  Bug 131  notifier: TTS generation has no timeout
  Bug 132  harden_phase: disk.write_file unguarded — phase aborts on I/O error
  Bug 133  TUI app: query_one crashes if called before DOM ready
  Bug 134  stream_panel: unbounded dict growth for transient roles
  Bug 135  phases/context: ctx.reset() never called — escalation leaks between slices
  Bug 136  scout: disk.read_file exceptions not caught
  Bug 137  coach: duplicate section names merged with single newline
  Bug 138  config: str(x) on None in allowed_chat_ids
  Bug 139  shell: stdout/stderr transports not closed on exception path
  Bug 140  lock.py: bare except catches SystemExit during lock acquisition
  Bug 141  (docs only — requirements.md method name mismatches, no code fix needed)
  Bug 142  cli.py: cmd_run() creates Runner without stream_bus
  Bug 143  catalog: incomplete OSError handling in cache save
  Bug 144  telegram_input: stderr decode silently replaces errors
"""

from __future__ import annotations

import inspect

import pytest


# ── Bug 122: div_steps and escalation_history not persisted to AgentState ────


class TestBug122EscalationNotPersisted:
    """RunnerContext holds div_steps and escalation_history in memory-only fields.
    On crash recovery, diversification progress and escalation history are lost.
    Fix: add div_steps: int and escalation_history: list[str] to AgentState.
    """

    def test_agent_state_has_div_steps(self) -> None:
        import tero2.state as state_module

        fields = {
            f.name
            for f in (
                state_module.AgentState.__dataclass_fields__.values()  # type: ignore[attr-defined]
            )
        }
        assert "div_steps" in fields, (
            "Bug 122: AgentState is missing 'div_steps' field. "
            "div_steps is only tracked in RunnerContext (in-memory). "
            "On crash recovery, diversification progress is lost and the runner "
            "re-triggers Level 1 when Level 2 was already in progress. "
            "Fix: add 'div_steps: int = 0' to AgentState and persist in checkpoints."
        )

    def test_agent_state_has_escalation_history(self) -> None:
        import tero2.state as state_module

        fields = {
            f.name
            for f in (
                state_module.AgentState.__dataclass_fields__.values()  # type: ignore[attr-defined]
            )
        }
        assert "escalation_history" in fields, (
            "Bug 122: AgentState is missing 'escalation_history' field. "
            "escalation_history is only tracked in RunnerContext (in-memory). "
            "On crash recovery, escalation history is lost. "
            "Fix: add 'escalation_history: list[str] = field(default_factory=list)' "
            "to AgentState and persist in checkpoints."
        )


# ── Bug 123: telegram_input fire-and-forget stderr drain task ─────────────────


class TestBug123TelegramInputDrainTask:
    """asyncio.create_task(_drain_stderr()) result is discarded.
    If drain crashes, exception is silently swallowed. Subprocess stderr pipe
    buffer fills up, child blocks on write, runner hangs indefinitely.
    Fix: store task reference and add add_done_callback for exception logging.
    """

    def test_drain_stderr_task_is_stored(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._watch_runner)
        lines = source.splitlines()

        for line in lines:
            stripped = line.strip()
            if "create_task(_drain_stderr" in stripped:
                assert not stripped.startswith("asyncio.create_task"), (
                    "Bug 123: asyncio.create_task(_drain_stderr()) result is discarded "
                    "(bare statement). If drain crashes, exception is swallowed and "
                    "subprocess stderr pipe fills — child hangs on write. "
                    "Fix: assign to a variable and add add_done_callback."
                )
                return

        pytest.skip("_drain_stderr create_task not found in _watch_runner")

    def test_drain_stderr_task_has_done_callback(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._watch_runner)
        if "_drain_stderr" not in source:
            pytest.skip("_drain_stderr not found in _watch_runner")

        # Accept either an explicit add_done_callback or delegation to
        # TaskSupervisor.spawn — the supervisor attaches a done-callback
        # that logs uncaught exceptions from the supervised task.
        has_callback = (
            "add_done_callback" in source or "self._tasks.spawn" in source
        )
        assert has_callback, (
            "Bug 123: stderr drain task has no done_callback. "
            "Exceptions from _drain_stderr() are silently swallowed by asyncio. "
            "Fix: task.add_done_callback(lambda t: log.warning(...)) or route the "
            "task through TaskSupervisor.spawn()."
        )


# ── Bug 124: disk_layer TOCTOU race in delta-based metrics write ──────────────


class TestBug124DiskLayerTOCTOU:
    """TOCTOU between read_metrics() and write_metrics(). The last_read baseline
    can be stale by the time write_metrics() acquires the lock.
    Fix: re-read current state under lock in write_metrics() before applying delta.
    """

    def test_write_metrics_rereads_under_lock(self) -> None:
        import tero2.disk_layer as disk_module

        source = inspect.getsource(disk_module.DiskLayer.write_metrics)
        lines = source.splitlines()

        found_lock = False
        has_reread_under_lock = False

        for i, line in enumerate(lines):
            if "self._metrics_lock" in line:
                found_lock = True
            if found_lock and ("_read_metrics_raw" in line or "read_metrics" in line):
                has_reread_under_lock = True
                break

        assert found_lock, (
            "Bug 124: write_metrics() must acquire _metrics_lock."
        )
        assert has_reread_under_lock, (
            "Bug 124: write_metrics() does not re-read current metrics under lock. "
            "TOCTOU: between read_metrics() and write_metrics(), another thread can "
            "modify the file and the baseline (last_read) becomes stale. "
            "Fix: call self._read_metrics_raw() under lock in write_metrics() "
            "to get the actual current state before applying delta."
        )

    def test_read_metrics_sets_last_read_inside_lock(self) -> None:
        import tero2.disk_layer as disk_module

        source = inspect.getsource(disk_module.DiskLayer.read_metrics)
        lines = source.splitlines()

        found_lock = False
        last_read_inside_lock = False

        for i, line in enumerate(lines):
            if "self._metrics_lock" in line:
                found_lock = True
            if found_lock and "last_read" in line and ("=" in line):
                last_read_inside_lock = True
                break

        assert found_lock, "Bug 124: read_metrics() must acquire _metrics_lock."
        assert last_read_inside_lock, (
            "Bug 124: read_metrics() sets last_read BEFORE or OUTSIDE the lock. "
            "Fix: set self._metrics_thread_local.last_read INSIDE the lock block "
            "so the baseline is consistent with what was actually read."
        )


# ── Bug 125: chain provider_kind stale after circuit-breaker skip ─────────────


class TestBug125ChainProviderKindStale:
    """_current_provider_index was set before the CB availability check.
    When index 0 fails and index 1's CB is open, provider_kind reports index 1's
    kind but work is done by index 2.
    Fix: only set _current_provider_index after CB availability check passes.
    """

    def test_provider_index_set_after_cb_check(self) -> None:
        import tero2.providers.chain as chain_module

        source = inspect.getsource(chain_module.ProviderChain.run)
        lines = source.splitlines()

        # Find the line with `if not cb.is_available:` (the CB skip guard).
        # The `continue` is on the next line.
        for i, line in enumerate(lines):
            if "is_available" in line and "not" in line and "if" in line:
                # Look 1-2 lines ahead for the continue
                lookahead = "\n".join(lines[i : i + 3])
                if "continue" not in lookahead:
                    continue
                # Found the CB skip guard. _current_provider_index must NOT be
                # set before this block in the same loop iteration.
                context_before = "\n".join(lines[max(0, i - 4) : i])
                assert "_current_provider_index" not in context_before, (
                    "Bug 125: _current_provider_index is set BEFORE the "
                    "is_available check in ProviderChain.run. When a provider's "
                    "circuit breaker is open, we skip it but the index was already "
                    "updated — provider_kind() reports the wrong provider. "
                    "Fix: set _current_provider_index only after the "
                    "is_available check passes."
                )
                return

        pytest.skip("is_available/continue check not found in ProviderChain.run")


# ── Bug 126: normalizers silently drop unparseable input ─────────────────────


class TestBug126NormalizersDropUnparseable:
    """codex.py, opencode.py, zai.py return early on non-dict input without
    yielding a StreamEvent(kind='error'). Silent data loss.
    Fix: yield StreamEvent(role=role, kind='error', ...) before returning.
    """

    def test_codex_yields_error_on_non_dict(self) -> None:
        import tero2.providers.normalizers.codex as codex_module
        from datetime import datetime, timezone

        normalizer = codex_module.CodexNormalizer()
        now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)

        events = list(normalizer.normalize("not a dict", "builder", now=now))
        assert len(events) > 0, (
            "Bug 126: codex normalizer returns without yielding when input is not a dict. "
            "Protocol requires a StreamEvent(kind='error') on parse failure. "
            "Fix: yield error event before returning."
        )
        assert any(e.kind == "error" for e in events), (
            "Bug 126: codex normalizer yielded events but none had kind='error'. "
            "Fix: yield StreamEvent(kind='error') on non-dict input."
        )

    def test_opencode_yields_error_on_non_dict(self) -> None:
        import tero2.providers.normalizers.opencode as opencode_module
        from datetime import datetime, timezone

        normalizer = opencode_module.OpenCodeNormalizer()
        now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)

        events = list(normalizer.normalize("not a dict", "builder", now=now))
        assert len(events) > 0, (
            "Bug 126: opencode normalizer silently drops non-dict input. "
            "Fix: yield StreamEvent(kind='error') on parse failure."
        )
        assert any(e.kind == "error" for e in events), (
            "Bug 126: opencode normalizer must yield an error event on non-dict input."
        )

    def test_zai_yields_error_on_non_dict(self) -> None:
        import tero2.providers.normalizers.zai as zai_module
        from datetime import datetime, timezone

        normalizer = zai_module.ZaiNormalizer()
        now = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)

        events = list(normalizer.normalize("not a dict", "builder", now=now))
        assert len(events) > 0, (
            "Bug 126: zai normalizer silently drops non-dict input. "
            "Fix: yield StreamEvent(kind='error') on parse failure."
        )
        assert any(e.kind == "error" for e in events), (
            "Bug 126: zai normalizer must yield an error event on non-dict input."
        )


# ── Bug 127: execute_phase state mutated before checkpoint save ───────────────


class TestBug127StateMutatedBeforeSave:
    """State is mutated (task_in_progress=True, current_task_index=N) before
    ctx.checkpoint.save(). If save fails (OSError), state is corrupted relative
    to disk — recovery skips the task thinking it's complete.
    Fix: snapshot state before mutation; rollback on save failure.
    """

    def test_checkpoint_save_on_failure_rollback_or_snapshot(self) -> None:
        import tero2.phases.execute_phase as ep_module

        source = inspect.getsource(ep_module.run_execute)
        lines = source.splitlines()

        found_mutation = False
        has_rollback_or_try = False

        for i, line in enumerate(lines):
            if "task_in_progress = True" in line:
                found_mutation = True
                # Look ahead for rollback/snapshot pattern
                context = "\n".join(lines[i : i + 15])
                if (
                    "try:" in context
                    and ("except" in context)
                    and (
                        "rollback" in context
                        or "snapshot" in context
                        or "task_in_progress = False" in context
                        or "copy" in context
                    )
                ):
                    has_rollback_or_try = True
                break

        if not found_mutation:
            pytest.skip("task_in_progress = True not found in run_execute")

        assert has_rollback_or_try, (
            "Bug 127: run_execute mutates ctx.state (task_in_progress=True, etc.) "
            "before calling ctx.checkpoint.save(). If save() raises OSError, "
            "in-memory state says task is in progress but disk disagrees — "
            "crash recovery skips the task thinking it's complete. "
            "Fix: snapshot state before mutation; rollback on save failure."
        )


# ── Bug 128: heartbeat task reference not stored ──────────────────────────────


class TestBug128HeartbeatTaskNotStored:
    """heartbeat_task is a local variable in run_agent(). On shutdown/cancellation
    from outside, no reference exists to cancel it cleanly.
    Fix: store as self._heartbeat_task and cancel in cleanup path.
    """

    def test_heartbeat_task_stored_as_instance_attribute(self) -> None:
        import tero2.phases.context as ctx_module

        source = inspect.getsource(ctx_module.RunnerContext.run_agent)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            if "create_task" in line and "_heartbeat_loop" in line:
                assert "self._heartbeat_task" in line, (
                    f"Bug 128: heartbeat create_task at line {i+1} is stored as a "
                    "local variable only. There is no way to cancel the heartbeat "
                    "from outside run_agent() (e.g. on Runner shutdown). "
                    "Fix: assign to 'self._heartbeat_task = asyncio.create_task(...)' "
                    "and cancel in the cleanup path."
                )
                return

        pytest.skip("heartbeat create_task not found in run_agent")

    def test_heartbeat_task_cancellable_from_outside(self) -> None:
        import tero2.phases.context as ctx_module

        source = inspect.getsource(ctx_module.RunnerContext)
        has_instance_attr = (
            "self._heartbeat_task" in source
            and ".cancel()" in source
        )
        assert has_instance_attr, (
            "Bug 128: RunnerContext has no self._heartbeat_task instance attribute. "
            "Without storing the task reference, a Runner shutdown cannot cancel "
            "the heartbeat loop while run_agent() is executing. "
            "Fix: store as self._heartbeat_task; cancel in a shutdown/cleanup method."
        )


# ── Bug 129: CLI provider subprocess leaks on early consumer break ────────────


class TestBug129CLISubprocessLeakOnBreak:
    """If consumer breaks early, finally block calls await proc.wait() without
    timeout. Zombie processes can block this indefinitely.
    Fix: add asyncio.wait_for(proc.wait(), timeout=0.5).
    """

    def test_proc_wait_has_timeout_in_finally(self) -> None:
        import tero2.providers.cli as cli_module

        source = inspect.getsource(cli_module.CLIProvider.run)
        lines = source.splitlines()

        found_finally = False
        found_wait = False
        has_timeout = False

        for i, line in enumerate(lines):
            if "finally:" in line:
                found_finally = True
            if found_finally and "proc.wait()" in line:
                found_wait = True
                context = "\n".join(lines[max(0, i - 3) : i + 2])
                if "wait_for" in context or "timeout" in context:
                    has_timeout = True
                break

        if not found_finally or not found_wait:
            pytest.skip("proc.wait() in finally block not found in CLIProvider.run")

        assert has_timeout, (
            "Bug 129: CLIProvider.run calls await proc.wait() in the finally "
            "block without a timeout. If the subprocess is a zombie, this blocks "
            "forever and leaks resources. "
            "Fix: await asyncio.wait_for(proc.wait(), timeout=0.5)."
        )


# ── Bug 130: usage_tracker accumulator updates outside lock ──────────────────


class TestBug130UsageTrackerAccumulatorOutsideLock:
    """_total_tokens += tokens and _total_cost += cost happen before
    acquiring _providers_lock. Concurrent calls can corrupt accumulators.
    Fix: move ALL accumulator updates inside the lock.
    """

    def test_total_tokens_updated_inside_lock(self) -> None:
        import tero2.usage_tracker as ut_module

        source = inspect.getsource(ut_module.UsageTracker.record_step)
        lines = source.splitlines()

        lock_line = None
        tokens_line = None

        for i, line in enumerate(lines):
            if "_providers_lock" in line and lock_line is None:
                lock_line = i
            if "_total_tokens" in line and "+=" in line and tokens_line is None:
                tokens_line = i

        if tokens_line is None:
            pytest.skip("_total_tokens += not found in record_step")

        assert lock_line is not None and tokens_line > lock_line, (
            "Bug 130: _total_tokens += tokens is executed BEFORE acquiring "
            "_providers_lock in record_step(). Concurrent calls can corrupt "
            "the accumulator via a non-atomic read-modify-write. "
            "Fix: move _total_tokens += and _total_cost += inside the lock block."
        )

    def test_total_cost_updated_inside_lock(self) -> None:
        import tero2.usage_tracker as ut_module

        source = inspect.getsource(ut_module.UsageTracker.record_step)
        lines = source.splitlines()

        lock_line = None
        cost_line = None

        for i, line in enumerate(lines):
            if "_providers_lock" in line and lock_line is None:
                lock_line = i
            if "_total_cost" in line and "+=" in line and cost_line is None:
                cost_line = i

        if cost_line is None:
            pytest.skip("_total_cost += not found in record_step")

        assert lock_line is not None and cost_line > lock_line, (
            "Bug 130: _total_cost += cost is executed BEFORE acquiring "
            "_providers_lock in record_step(). Fix: move inside the lock."
        )


# ── Bug 131: notifier TTS generation has no timeout ─────────────────────────


class TestBug131NotifierTTSNoTimeout:
    """send_voice() calls asyncio.to_thread(self._generate_tts, text) without
    wait_for timeout. TTS subprocess may hang indefinitely, stalling event loop.
    Fix: wrap in asyncio.wait_for(..., timeout=30).
    """

    def test_send_voice_generate_tts_has_timeout(self) -> None:
        import tero2.notifier as notifier_module

        source = inspect.getsource(notifier_module.Notifier.send_voice)
        lines = source.splitlines()

        found_generate_tts = False

        for i, line in enumerate(lines):
            if "_generate_tts" in line and "to_thread" in line:
                found_generate_tts = True
                context = "\n".join(lines[max(0, i - 2) : i + 3])
                assert "wait_for" in context, (
                    "Bug 131: send_voice() wraps _generate_tts in asyncio.to_thread "
                    "without asyncio.wait_for timeout. TTS subprocess (Fish Audio) "
                    "may hang indefinitely, stalling the entire event loop. "
                    "Fix: await asyncio.wait_for(asyncio.to_thread(self._generate_tts, text), "
                    "timeout=30)."
                )
                return

        if not found_generate_tts:
            pytest.skip("_generate_tts to_thread not found in send_voice")


# ── Bug 132: harden_phase disk.write_file unguarded ──────────────────────────


class TestBug132HardenPhaseWriteUnguarded:
    """Two disk.write_file() calls in run_harden have no try/except.
    Disk full or permission error aborts entire harden phase.
    Fix: wrap intermediate writes in try/except OSError.
    """

    def test_intermediate_write_wrapped_in_try_except(self) -> None:
        import tero2.phases.harden_phase as hp_module

        source = inspect.getsource(hp_module.run_harden)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            if "write_file" in line and "plan_v" in line:
                start = max(0, i - 5)
                context = "\n".join(lines[start : i + 3])
                assert "try:" in context or "OSError" in context, (
                    f"Bug 132: harden_phase.py line {i+1} calls write_file for "
                    "intermediate plan version without try/except. Disk full or "
                    "permission denied aborts the entire harden phase, losing progress. "
                    "Fix: wrap intermediate write in try/except OSError with log.warning."
                )
                return

        pytest.skip("write_file(plan_v...) not found in run_harden")

    def test_final_write_exists(self) -> None:
        import tero2.phases.harden_phase as hp_module

        source = inspect.getsource(hp_module.run_harden)
        assert "write_file" in source and "PLAN.md" in source, (
            "Bug 132: run_harden must write the final PLAN.md."
        )


# ── Bug 133: TUI app query_one crashes before DOM ready ──────────────────────


class TestBug133TUIAppQueryOneCrash:
    """action_toggle_raw(), action_clear_stream(), action_unpin() call query_one()
    without guards. Before on_mount() completes, raises NoMatches.
    Fix: wrap query_one() calls in try/except NoMatches.
    """

    def test_action_toggle_raw_guarded(self) -> None:
        import tero2.tui.app as app_module

        source = inspect.getsource(app_module.DashboardApp.action_toggle_raw)
        has_guard = "NoMatches" in source or "try:" in source
        assert has_guard, (
            "Bug 133: action_toggle_raw() calls query_one() without guarding against "
            "NoMatches. If triggered before on_mount() completes or during screen "
            "transitions, crashes with NoMatches. "
            "Fix: wrap query_one() in try/except NoMatches."
        )

    def test_action_clear_stream_guarded(self) -> None:
        import tero2.tui.app as app_module

        source = inspect.getsource(app_module.DashboardApp.action_clear_stream)
        has_guard = "NoMatches" in source or "try:" in source
        assert has_guard, (
            "Bug 133: action_clear_stream() calls query_one() without NoMatches guard."
        )

    def test_action_unpin_guarded(self) -> None:
        import tero2.tui.app as app_module

        source = inspect.getsource(app_module.DashboardApp.action_unpin)
        has_guard = "NoMatches" in source or "try:" in source
        assert has_guard, (
            "Bug 133: action_unpin() calls query_one() without NoMatches guard."
        )


# ── Bug 134: stream_panel unbounded dict growth ───────────────────────────────


class TestBug134StreamPanelUnboundedDicts:
    """_buffers and _last_seen dicts accumulate entries for every role.
    Individual deques have maxlen=500 but the dicts grow forever.
    Fix: LRU eviction or timestamp-based cleanup for stale roles.
    """

    def test_push_stream_event_has_eviction_logic(self) -> None:
        import tero2.tui.widgets.stream_panel as sp_module

        source = inspect.getsource(sp_module.RoleStreamPanel.push_stream_event)
        # Must have dict-level eviction, not just deque maxlen.
        # Look for deletion of roles from the dicts, or len() check on dicts.
        has_eviction = (
            "evict" in source
            or "lru" in source.lower()
            or ("len(self._buffers)" in source and (">" in source or ">=" in source))
            or ("del self._buffers" in source)
            or ("del self._last_seen" in source)
            or "_evict_stale_roles" in source
        )
        assert has_eviction, (
            "Bug 134: RoleStreamPanel.push_stream_event() grows _buffers and "
            "_last_seen dicts without bound. A long-running app with transient "
            "roles (one per agent run) accumulates entries forever — memory leak. "
            "Fix: evict stale roles from _buffers and _last_seen when the dict "
            "exceeds a threshold, or use LRU eviction."
        )

    def test_buffers_dict_bounded(self) -> None:
        import tero2.tui.widgets.stream_panel as sp_module

        source = inspect.getsource(sp_module.RoleStreamPanel)
        # Check for dict-level bounding, NOT deque maxlen (which exists already).
        has_max_roles = (
            "_MAX_ROLES" in source
            or "MAX_ROLES" in source
            or "_max_roles" in source
            or "max_roles" in source
            or "evict" in source
            or "_evict_stale_roles" in source
        )
        assert has_max_roles, (
            "Bug 134: RoleStreamPanel has no upper bound on the number of tracked roles. "
            "The deque per role has maxlen=500, but the _buffers and _last_seen dicts "
            "themselves grow without limit. "
            "Fix: define a _MAX_ROLES constant and evict the oldest entry when exceeded."
        )


# ── Bug 135: ctx.reset() not called between slices ───────────────────────────


class TestBug135CtxResetNotCalledBetweenSlices:
    """RunnerContext.reset() is never called in the SORA slice loop.
    Escalation raised to DIVERSIFICATION in slice 1 persists into slice 2.
    Fix: call ctx.reset() at the start of each new slice.
    """

    def test_ctx_reset_called_in_slice_loop(self) -> None:
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._execute_sora)
        lines = source.splitlines()

        found_next_slice = False
        has_reset_in_slice_loop = False

        for i, line in enumerate(lines):
            if "next_slice" in line or "current_slice" in line:
                found_next_slice = True
            if found_next_slice and "ctx.reset()" in line:
                has_reset_in_slice_loop = True
                break

        if not found_next_slice:
            pytest.skip("slice loop not found in _execute_sora")

        assert has_reset_in_slice_loop, (
            "Bug 135: ctx.reset() is never called in the SORA slice loop. "
            "If escalation is raised to DIVERSIFICATION during slice 1, "
            "ctx.escalation_level remains DIVERSIFICATION for slice 2 — "
            "causing immediate diversification at the start of the next slice. "
            "Fix: call ctx.reset() when transitioning to each new slice in "
            "Runner._execute_sora."
        )


# ── Bug 136: scout disk.read_file exceptions not caught ──────────────────────


class TestBug136ScoutReadFileUncaught:
    """_read_project_md() calls self.disk.read_file() with no try/except.
    The run() broad catch doesn't apply here — method is not 'non-fatal'
    as documented but crashes the whole run.
    Fix: wrap in try/except OSError, return empty string on failure.
    """

    def test_read_project_md_catches_exceptions(self) -> None:
        import tero2.players.scout as scout_module

        source = inspect.getsource(scout_module.ScoutPlayer._read_project_md)
        has_handler = "try:" in source and ("OSError" in source or "Exception" in source)
        assert has_handler, (
            "Bug 136: ScoutPlayer._read_project_md() calls disk.read_file() "
            "without a try/except. A PermissionError or OSError propagates and "
            "crashes the entire scout run, despite docstring saying 'non-fatal'. "
            "Fix: wrap in try/except OSError and return empty string on failure."
        )

    def test_read_project_md_returns_empty_on_oserror(self, tmp_path) -> None:
        from unittest.mock import MagicMock

        import tero2.players.scout as scout_module

        disk = MagicMock()
        disk.read_file.side_effect = PermissionError("no access")

        scout = scout_module.ScoutPlayer.__new__(scout_module.ScoutPlayer)
        scout.disk = disk

        try:
            result = scout._read_project_md()
            assert isinstance(result, str), (
                "Bug 136: _read_project_md must return a string even on OSError"
            )
        except (PermissionError, OSError):
            pytest.fail(
                "Bug 136: _read_project_md raised PermissionError instead of "
                "catching it and returning ''. Fix: add try/except OSError."
            )


# ── Bug 137: coach duplicate section names single newline ─────────────────────


class TestBug137CoachDuplicateSectionSingleNewline:
    """When duplicate section names found, content concatenated with '\\n'.
    Previous content may not end with newline — run-together text.
    Fix: use '\\n\\n' separator for clearer section separation.
    """

    def test_duplicate_section_uses_double_newline(self) -> None:
        import tero2.players.coach as coach_module

        source = inspect.getsource(coach_module)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            if "section_name in result" in line:
                for k in range(i, min(i + 5, len(lines))):
                    if "result[section_name]" in lines[k] and '+ "\\n" +' in lines[k]:
                        stripped = lines[k].strip()
                        if '\\n\\n' not in stripped and '+ "\\n" +' in stripped:
                            pytest.fail(
                                f"Bug 137: coach.py line {k+1} merges duplicate "
                                f"sections with single '\\n': {stripped}. "
                                "Previous content may not end with newline — "
                                "text runs together. "
                                "Fix: use '\\n\\n' separator."
                            )
                return

        pytest.skip("duplicate section merge not found in coach module")


# ── Bug 138: config None in allowed_chat_ids ──────────────────────────────────


class TestBug138ConfigNoneInChatIds:
    """str(None) produces 'None' string, accepted as valid chat ID.
    Fix: filter out None values before str() conversion.
    """

    def test_allowed_chat_ids_filters_none(self) -> None:
        import tero2.config as config_module

        source = inspect.getsource(config_module)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            if "allowed_chat_ids" in line and "str(x)" in line:
                stripped = line.strip()
                has_none_filter = (
                    "if x is not None" in stripped
                    or "x is not None" in stripped
                    or "filter" in stripped
                )
                assert has_none_filter, (
                    f"Bug 138: config.py line {i+1} converts allowed_chat_ids elements "
                    f"with str(x) without filtering None: {stripped}. "
                    "str(None) produces 'None' which is accepted as a valid chat ID, "
                    "allowing any sender through if 'None' appears in a message. "
                    "Fix: add 'if x is not None' guard in the list comprehension."
                )
                return

        pytest.skip("allowed_chat_ids str(x) comprehension not found in config")

    def test_none_excluded_from_parsed_chat_ids(self) -> None:
        import tero2.config as config_module

        raw = {
            "telegram": {
                "bot_token": "tok",
                "chat_id": "123",
                "allowed_chat_ids": [456, None, 789],
            }
        }
        cfg = config_module._parse_config(raw)
        assert "None" not in cfg.telegram.allowed_chat_ids, (
            "Bug 138: _parse_config includes 'None' (string) in "
            "allowed_chat_ids when None appears in the source list. "
            "Fix: filter out None values before str() conversion."
        )


# ── Bug 139: shell stdout/stderr transports not properly closed ───────────────


class TestBug139ShellTransportsNotClosed:
    """Exception path uses transport.close() (private API) instead of
    proc.stdout.close() / proc.stderr.close() in a finally block.
    Fix: use proc.stdout.close() / proc.stderr.close() in finally.
    """

    def test_exception_path_closes_streams_directly(self) -> None:
        import tero2.providers.shell as shell_module

        source = inspect.getsource(shell_module.ShellProvider.run)
        lines = source.splitlines()

        found_exception_path = False
        uses_transport_private = False
        uses_stream_close = False

        for i, line in enumerate(lines):
            if "except Exception:" in line:
                found_exception_path = True
            if found_exception_path and "_transport" in line:
                uses_transport_private = True
            if found_exception_path and (
                "proc.stdout.close()" in line or "proc.stderr.close()" in line
            ):
                uses_stream_close = True

        if not found_exception_path:
            pytest.skip("except Exception path not found in ShellProvider.run")

        assert not uses_transport_private or uses_stream_close, (
            "Bug 139: ShellProvider.run exception path accesses private _transport "
            "attribute to close streams. This is a private API that may not "
            "immediately release file descriptors. "
            "Fix: use proc.stdout.close() / proc.stderr.close() in a finally block."
        )


# ── Bug 140: lock.py bare except ─────────────────────────────────────────────


class TestBug140LockBareExcept:
    """bare except: catches all exceptions including SystemExit/KeyboardInterrupt
    during PID write. Non-idiomatic and masks debugging issues.
    Fix: change to except BaseException: or except OSError:.
    """

    def test_no_bare_except_in_acquire(self) -> None:
        import tero2.lock as lock_module

        source = inspect.getsource(lock_module.FileLock.acquire)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "except:":
                pytest.fail(
                    f"Bug 140: lock.py FileLock.acquire() line {i+1} uses bare "
                    "'except:' which catches SystemExit and KeyboardInterrupt. "
                    "While the exception is re-raised, bare except is non-idiomatic "
                    "and masks issues during debugging. "
                    "Fix: change to 'except BaseException:' (explicit) or "
                    "'except OSError:' (only expected errors from os.write/os.lseek)."
                )


# ── Bug 142: cmd_run creates Runner without stream_bus ───────────────────────


class TestBug142CmdRunNoStreamBus:
    """cmd_run() creates Runner without stream_bus, dispatcher, or command_queue.
    Headless mode produces no events — differs silently from cmd_go() which
    provides all three. Should be documented or use a StreamBus for logging.
    Fix: document the discrepancy explicitly OR pass a StreamBus for headless logging.
    """

    def test_cmd_run_documents_headless_mode(self) -> None:
        import tero2.cli as cli_module

        source = inspect.getsource(cli_module.cmd_run)
        has_documentation = (
            "headless" in source.lower()
            or "stream_bus" in source
            or "no event" in source.lower()
            or "without stream" in source.lower()
        )
        assert has_documentation, (
            "Bug 142: cmd_run() creates Runner(project_path, plan_file, config=config) "
            "without stream_bus, dispatcher, or command_queue. This differs from "
            "cmd_go() which provides all three. The discrepancy is not documented. "
            "Fix: either pass stream_bus=StreamBus() for headless event logging, "
            "or add a comment documenting that headless mode intentionally has no events."
        )


# ── Bug 143: catalog cache save missing try/finally for tmp cleanup ───────────


class TestBug143CatalogCacheSaveMissingFinally:
    """_save_cache() catches OSError but doesn't clean up the .tmp file on failure.
    If write succeeds but rename fails, orphaned .tmp remains.
    Fix: add try/finally; unlink tmp on failure.
    """

    def test_save_cache_has_finally_for_tmp_cleanup(self) -> None:
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module._save_cache)
        has_cleanup = (
            "finally:" in source
            and ("unlink" in source or "remove" in source)
        )
        assert has_cleanup, (
            "Bug 143: catalog._save_cache() creates a .tmp file for atomic write "
            "but has no try/finally to clean it up. If tmp.replace(p) fails "
            "(different filesystem, permissions), the .tmp file is orphaned forever. "
            "Fix: add try/finally: tmp.unlink(missing_ok=True) after the write."
        )


# ── Bug 144: telegram_input stderr decode silent replacement ─────────────────


class TestBug144TelegramInputStderrDecode:
    """stderr_bytes.decode(errors='replace') silently replaces all decoding errors.
    Corrupted error messages give no indication that replacement occurred.
    Fix: log when replacement characters detected.
    """

    def test_stderr_decode_logs_replacement(self) -> None:
        import tero2.telegram_input as ti_module

        source = inspect.getsource(ti_module.TelegramInputBot._watch_runner)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            if "decode" in line and "replace" in line and "stderr" in line.lower():
                # Look ahead for a specific log about replacement/non-UTF-8 bytes.
                context = "\n".join(lines[i : i + 8])
                # Must find a log call that SPECIFICALLY mentions replacement
                # characters or non-UTF-8 bytes. A generic log.error(msg) that
                # happens to follow the decode does NOT count.
                has_specific_log = (
                    "\\ufffd" in context
                    or "non-utf" in context.lower()
                    or "replacement" in context.lower()
                    or "non_utf" in context.lower()
                )
                assert has_specific_log, (
                    f"Bug 144: telegram_input.py _watch_runner line {i+1} decodes "
                    "stderr with errors='replace' but does not log when replacement "
                    "characters (U+FFFD \\ufffd) are introduced. Corrupted error "
                    "messages are silently accepted, making debugging harder. "
                    "Fix: after decode, check '\\ufffd' in stderr_text and "
                    "log.warning a message about non-UTF-8 bytes."
                )
                return

        pytest.skip("stderr decode(errors='replace') not found in _watch_runner")
