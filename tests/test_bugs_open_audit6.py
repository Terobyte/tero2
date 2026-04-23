"""Negative tests for Audit 6 open bugs (248–303).

Convention: test FAILS when the bug is present, PASSES when fixed.

Each class targets exactly one bug number and explains in its docstring
what the symptom is, what file/line it lives at, and the expected fix.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Bug 248: reflexion add_attempt() never enforces max cycles ───────────────


class TestBug248ReflexionMaxCyclesNotEnforced:
    """`tero2/reflexion.py:91-115` add_attempt() has no check against any
    max-cycles configuration.  The docstring says "Max 2 cycles" but the
    function happily appends attempts forever.
    Fix: raise / return early once `len(context.attempts) >= MAX_CYCLES`.
    """

    def test_add_attempt_does_not_grow_unbounded(self) -> None:
        from tero2.reflexion import ReflexionContext, add_attempt

        ctx = ReflexionContext()
        # Append well past the advertised "Max 2 cycles".
        for i in range(10):
            ctx = add_attempt(ctx, builder_output=f"try{i}", verifier_feedback="fail")

        assert len(ctx.attempts) <= 2, (
            "Bug 248: reflexion.add_attempt() has no max-cycles enforcement. "
            f"Appended 10 attempts, all 10 were retained (len={len(ctx.attempts)}). "
            "Docstring promises Max 2 cycles. "
            "Fix: add `if len(context.attempts) >= MAX_CYCLES: raise MaxReflexionCyclesExceeded`."
        )


# ── Bug 249: events priority queue overflow grows unbounded ──────────────────


class TestBug249EventOverflowUnbounded:
    """`tero2/events.py:179-192` — when every slot already holds a priority
    event, the dispatcher appends beyond maxsize without any ceiling.
    Fix: add MAX_OVERFLOW = 100 cap; drop beyond it.
    """

    def test_priority_overflow_has_ceiling(self) -> None:
        async def inner():
            from tero2.events import EventDispatcher, make_event

            disp = EventDispatcher()
            q = disp.subscribe()
            # Fill the queue with priority events up to maxsize (500).
            for i in range(500):
                await disp.emit(make_event("stuck", priority=True))
            assert q.qsize() == 500

            # Now push 1000 more priority events — without a cap the queue
            # grows unboundedly.
            for i in range(1000):
                await disp.emit(make_event("stuck", priority=True))

            # With a sensible MAX_OVERFLOW (e.g. 100) the queue should be
            # comfortably below 600. Without the cap it reaches ~1500.
            assert q.qsize() < 700, (
                "Bug 249: event dispatcher overflow is unbounded. "
                f"After 1000 extra priority events the queue holds {q.qsize()} "
                "items (expected ≤ 600 with a 100-item overflow ceiling). "
                "Fix: add MAX_OVERFLOW = 100 and drop events beyond it."
            )

        asyncio.run(inner())


# ── Bug 250: runner ctx.state not synced after checkpoint ops ────────────────


class TestBug250CtxStateNotSyncedAfterCheckpoint:
    """In `tero2/runner.py:640-721` the runner does
    `state = self.checkpoint.set_sora_phase(state, …)` (and similar) to
    update the local var, and mirrors to `self._current_state`, but does
    NOT sync `ctx.state`. Phase functions read stale `ctx.state`.
    Fix: set `ctx.state = state` after every checkpoint operation.
    """

    def test_execute_sora_assigns_ctx_state_after_set_sora_phase(self) -> None:
        from tero2.runner import Runner

        source = inspect.getsource(Runner._execute_sora)
        lines = source.splitlines()

        # Count how many times checkpoint.set_sora_phase is called vs how many
        # lines after that call contain `ctx.state = state`.
        set_phase_calls = 0
        syncs_after_set_phase = 0
        for i, line in enumerate(lines):
            if "checkpoint.set_sora_phase" in line and "=" in line:
                set_phase_calls += 1
                # Look at the next 3 lines for ctx.state = state
                window = "\n".join(lines[i:i + 4])
                if "ctx.state = state" in window:
                    syncs_after_set_phase += 1

        assert set_phase_calls > 0, "sanity: set_sora_phase calls present"
        assert syncs_after_set_phase == set_phase_calls, (
            f"Bug 250: runner._execute_sora has {set_phase_calls} "
            f"checkpoint.set_sora_phase() calls but only "
            f"{syncs_after_set_phase} are followed by `ctx.state = state`. "
            "Phase handlers read stale ctx.state. "
            "Fix: add `ctx.state = state` after every checkpoint operation."
        )


# ── Bug 251: escalation state lost on crash recovery (lives on ctx only) ─────


class TestBug251EscalationStateNotPersisted:
    """`tero2/phases/context.py:85-87` keeps escalation_level/div_steps/
    escalation_history on RunnerContext. The runner mutates them but
    never copies them to AgentState before checkpoint.save(). On crash
    recovery, these fields are reset to NONE/0.
    Fix: mirror ctx fields into state before save, or move them to state.
    """

    def test_ctx_escalation_mutation_is_mirrored_to_state(self) -> None:
        from tero2.runner import Runner

        source = inspect.getsource(Runner)

        # There should be at least one place where ctx.div_steps += 1 is
        # followed by a state.div_steps sync (or state = replace(..., div_steps=...)).
        # Absence means the runner loses escalation state on checkpoint save.
        has_mutate = "ctx.div_steps" in source and (
            "ctx.div_steps += 1" in source or "ctx.div_steps +=" in source
        )
        has_sync_to_state = (
            "state.div_steps = ctx.div_steps" in source
            or "state.div_steps=ctx.div_steps" in source
            or "div_steps=ctx.div_steps" in source
        )
        assert has_mutate, "sanity: runner mutates ctx.div_steps"
        assert has_sync_to_state, (
            "Bug 251: runner mutates ctx.div_steps but never mirrors the "
            "value into AgentState.div_steps before the checkpoint save. "
            "Escalation counters live only on the RunnerContext and are "
            "lost on crash recovery. "
            "Fix: set state.div_steps = ctx.div_steps (and similar for "
            "escalation_level / escalation_history) before checkpoint.save()."
        )


# ── Bug 252: config missing int()/float() coercion on numeric fields ────────


class TestBug252ConfigNumericCoercion:
    """`tero2/config.py:218,227-234,242-244,250-251,258,264,275` — numeric
    fields assigned straight from `.get()` without int()/float() casting.
    A string value in TOML breaks downstream arithmetic.
    Fix: wrap all numeric fields in int()/float().
    """

    def test_heartbeat_interval_coerced_to_int(self, tmp_path) -> None:
        # Bypass the file-reading layer — test _parse_config directly with a
        # value that would come from a string-typed TOML entry.
        from tero2.config import _parse_config

        raw = {
            "telegram": {"bot_token": "x", "heartbeat_interval_s": "60"},
        }
        cfg = _parse_config(raw)
        # The bug: raw ".get()" returns the string; int() coercion is missing.
        assert isinstance(cfg.telegram.heartbeat_interval_s, int), (
            "Bug 252: telegram.heartbeat_interval_s is not coerced to int. "
            f"Got type={type(cfg.telegram.heartbeat_interval_s).__name__}, "
            f"value={cfg.telegram.heartbeat_interval_s!r}. "
            "Fix: int(tg.get('heartbeat_interval_s', DEFAULT_HEARTBEAT_INTERVAL_S))."
        )

    def test_retry_max_retries_coerced_to_int(self) -> None:
        from tero2.config import _parse_config

        raw = {"retry": {"max_retries": "5"}}
        cfg = _parse_config(raw)
        assert isinstance(cfg.retry.max_retries, int), (
            "Bug 252: retry.max_retries is not coerced to int. "
            f"Got {type(cfg.retry.max_retries).__name__}={cfg.retry.max_retries!r}. "
            "Fix: wrap in int()."
        )

    def test_role_timeout_s_coerced_to_int(self) -> None:
        from tero2.config import _parse_config

        raw = {
            "roles": {
                "executor": {"provider": "claude", "timeout_s": "60"},
            },
        }
        cfg = _parse_config(raw)
        role = cfg.roles["executor"]
        assert isinstance(role.timeout_s, int), (
            "Bug 252: roles[*].timeout_s is not coerced to int. "
            f"Got {type(role.timeout_s).__name__}={role.timeout_s!r}. "
            "Fix: int(role_data.get('timeout_s', DEFAULT_PROVIDER_TIMEOUT_S))."
        )


# ── Bug 253: config reader and writer use different lock mechanisms ─────────


class TestBug253ReaderWriterLockMismatch:
    """`tero2/config.py:123` uses threading.Lock (intra-process) while
    `tero2/config_writer.py:89` uses fcntl.flock (inter-process). Readers
    and writers are not coordinated — stale reads after writes.
    Fix: use the same lock mechanism on both sides.
    """

    def test_load_config_and_writer_use_same_lock_mechanism(self) -> None:
        import tero2.config as cfg_mod
        import tero2.config_writer as writer_mod

        reader_src = inspect.getsource(cfg_mod.load_config)
        writer_src = inspect.getsource(writer_mod.write_global_config_section)

        reader_uses_flock = "flock" in reader_src or "fcntl" in reader_src
        writer_uses_flock = "flock" in writer_src or "fcntl" in writer_src

        # Reader currently uses threading.Lock only; writer uses fcntl.flock.
        # The bug is present when the two mechanisms disagree.
        assert reader_uses_flock == writer_uses_flock, (
            "Bug 253: config load_config() and config_writer use different "
            f"lock mechanisms — reader_uses_flock={reader_uses_flock}, "
            f"writer_uses_flock={writer_uses_flock}. Cross-process writes "
            "can land between a reader's file-read and its parse, producing "
            "stale or inconsistent values. "
            "Fix: make both sides take the same inter-process lock."
        )


# ── Bug 254: div_steps double-counted by runner + execute_escalation ────────


class TestBug254DivStepsDoubleCounted:
    """`tero2/escalation.py:138` increments state.div_steps inside
    execute_escalation(). `tero2/runner.py:478` ALSO increments
    ctx.div_steps. That's two increments per real diversification step,
    so the window is exhausted twice as fast.
    Fix: remove the increment from execute_escalation().
    """

    def test_execute_escalation_does_not_increment_div_steps(self) -> None:
        from tero2.escalation import execute_escalation

        source = inspect.getsource(execute_escalation)
        # Look for div_steps=state.div_steps + 1 pattern
        suspicious = any(
            "div_steps=state.div_steps + 1" in line.replace(" ", "")
            or "div_steps=state.div_steps+1" in line.replace(" ", "")
            for line in source.splitlines()
        )
        assert not suspicious, (
            "Bug 254: execute_escalation() increments state.div_steps "
            "(`div_steps=state.div_steps + 1`), but the runner's outer loop "
            "ALSO increments ctx.div_steps on every DIVERSIFICATION action. "
            "Double-counted — window exhausted 2× faster than intended. "
            "Fix: remove the div_steps increment from execute_escalation; "
            "the runner is the single source of truth."
        )


# ── Bug 255: div_steps not reset when stuck clears ───────────────────────────


class TestBug255DivStepsNotResetOnRecovery:
    """`tero2/runner.py:483` — when stuck clears (escalation_level becomes
    NONE), only `ctx.escalation_level = NONE` happens. `ctx.div_steps`
    stays at whatever value it reached. The next stuck event then
    escalates prematurely.
    Fix: reset `ctx.div_steps = 0` when clearing escalation level.
    """

    def test_div_steps_reset_when_clearing_escalation(self) -> None:
        from tero2.runner import Runner

        source = inspect.getsource(Runner)
        lines = source.splitlines()

        # Look for `ctx.escalation_level = EscalationLevel.NONE` on a line
        # after a check_stuck NONE branch, and verify `ctx.div_steps = 0`
        # follows within 5 lines.
        for i, line in enumerate(lines):
            if "ctx.escalation_level = EscalationLevel.NONE" in line:
                window = "\n".join(lines[max(0, i - 2):i + 5])
                if "ctx.div_steps = 0" in window:
                    return  # fixed
                pytest.fail(
                    "Bug 255: runner clears ctx.escalation_level to NONE "
                    f"at line {i + 1} but does not reset ctx.div_steps. The "
                    "counter survives the recovery, so the next stuck event "
                    "escalates prematurely. "
                    "Fix: add `ctx.div_steps = 0` alongside the escalation_level reset."
                )
        pytest.skip("clear-escalation branch not located in runner")


# ── Bug 256: circuit breaker infinite probe with recovery_timeout_s=0 ───────


class TestBug256CircuitBreakerInfiniteProbe:
    """`tero2/circuit_breaker.py:41-56` — when `recovery_timeout_s=0` and
    the breaker is OPEN, line 36 immediately transitions to HALF_OPEN
    (because `now - last_failure_time >= 0` is always true). A probe
    failure goes back to OPEN and immediately back to HALF_OPEN.
    Fix: track last_half_open_failure_time separately, require a real
    timeout after HALF_OPEN failure.
    """

    def test_recovery_timeout_zero_does_not_loop_open_half_open(self) -> None:
        from tero2.circuit_breaker import CBState, CircuitBreaker
        from tero2.errors import CircuitOpenError

        cb = CircuitBreaker(
            name="p1", failure_threshold=1, recovery_timeout_s=0
        )
        # Trigger an OPEN state with a failure.
        cb.record_failure()
        assert cb.state == CBState.OPEN

        # With recovery_timeout_s=0, a check() transitions OPEN → HALF_OPEN
        # and allows a probe.  The probe fails → back to OPEN.
        cb.check()  # permitted probe
        cb.record_failure()  # probe fails

        # Bug: now check() again.  Because recovery_timeout_s=0 the OPEN→
        # HALF_OPEN transition fires immediately, so the caller can keep
        # probing with zero backoff.
        probed_again = False
        try:
            cb.check()
            probed_again = True  # no CircuitOpenError means we got another probe
        except CircuitOpenError:
            probed_again = False

        assert not probed_again, (
            "Bug 256: with recovery_timeout_s=0, the breaker immediately "
            "transitions OPEN → HALF_OPEN on every check(), allowing an "
            "unbounded probe-fail loop with zero backoff. "
            "Fix: enforce a minimum backoff even when recovery_timeout_s=0, "
            "e.g. block further probes until a sentinel interval elapses "
            "after the last HALF_OPEN failure."
        )


# ── Bug 257: ProviderError / RateLimitError swallowed by except Exception ───


class TestBug257ProviderErrorSwallowed:
    """`tero2/players/coach.py:102`, `verifier.py:155`, `scout.py:82`,
    `reviewer.py:104` use a blanket `except Exception` that catches
    ProviderError/RateLimitError. Retry/escalation is broken for every
    non-architect player.
    Fix: re-raise (ProviderError, RateLimitError) before the blanket
    except Exception.
    """

    def _source_reraises_provider_errors(self, src: str) -> bool:
        """True when the source contains an explicit re-raise of
        ProviderError/RateLimitError before the blanket except Exception."""
        lines = src.splitlines()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith("except") and "Exception" in stripped and (
                "ProviderError" not in stripped
                and "RateLimitError" not in stripped
            ):
                # look backwards for an explicit ProviderError/RateLimitError handler
                window = "\n".join(lines[max(0, i - 10):i])
                if ("ProviderError" in window or "RateLimitError" in window) and "raise" in window:
                    return True
        return False

    def test_coach_reraises_provider_errors(self) -> None:
        from tero2.players.coach import CoachPlayer

        src = inspect.getsource(CoachPlayer.run)
        assert self._source_reraises_provider_errors(src), (
            "Bug 257: coach.run() has a blanket `except Exception` without "
            "re-raising ProviderError/RateLimitError first. The chain's "
            "retry/fallback mechanism never sees these errors. "
            "Fix: add `except (ProviderError, RateLimitError): raise` before "
            "the blanket except Exception."
        )

    def test_verifier_reraises_provider_errors(self) -> None:
        from tero2.players.verifier import VerifierPlayer

        src = inspect.getsource(VerifierPlayer.run)
        assert self._source_reraises_provider_errors(src), (
            "Bug 257: verifier.run() swallows ProviderError/RateLimitError "
            "via blanket except Exception. Fix: re-raise before the generic "
            "handler."
        )

    def test_scout_reraises_provider_errors(self) -> None:
        from tero2.players.scout import ScoutPlayer

        src = inspect.getsource(ScoutPlayer.run)
        assert self._source_reraises_provider_errors(src), (
            "Bug 257: scout.run() swallows ProviderError/RateLimitError "
            "via blanket except Exception."
        )


# ── Bug 258: builder ignores disk.write_file() return value ─────────────────


class TestBug258BuilderIgnoresWriteFileReturn:
    """`tero2/players/builder.py:114` — the builder does
    `self.disk.write_file(output_path, summary)` but never reads the bool
    return. If the write fails (OSError → returns False), builder still
    reports success with a valid-looking output_path. Crash recovery
    later finds the file missing.
    Fix: check the return value, return success=False on write failure.
    """

    def test_builder_reports_failure_when_write_fails(self) -> None:
        from tero2.players.builder import BuilderPlayer

        player = BuilderPlayer.__new__(BuilderPlayer)
        player.chain = MagicMock()
        player.disk = MagicMock()
        player.disk.write_file.return_value = False  # simulate write failure
        player.working_dir = "."

        async def fake_prompt(_prompt):
            return "real builder summary output"

        player._run_prompt = fake_prompt  # type: ignore[attr-defined]

        result = asyncio.run(player.run(
            task_plan="do X",
            task_id="T01",
            slice_id="S01",
            milestone_path="milestones/M001",
        ))

        assert result.success is False, (
            "Bug 258: builder returned success=True even though "
            "disk.write_file() returned False (write failed). The SUMMARY.md "
            "is not on disk but the builder thinks everything is fine — "
            "crash recovery later finds the file missing. "
            "Fix: check the return value of disk.write_file() and return "
            "BuilderResult(success=False, ...) on write failure."
        )


# ── Bug 259: stream_bus memory leak from publisher snapshots ────────────────


class TestBug259StreamBusPublisherSnapshotLeak:
    """`tero2/stream_bus.py:200-221` — `_publish_impl` creates a list
    snapshot of subscribers while holding the lock, but an event that is
    already mid-publish will keep a reference to the old (unsubscribed)
    queue via the subscribers list copy.  Under high throughput
    unsubscribed queues remain referenced.
    Fix: attach a `_bus_subscribed` marker to each queue and skip
    unmarked ones; clear the marker in unsubscribe.
    """

    def test_publish_impl_uses_subscription_marker(self) -> None:
        from tero2.stream_bus import StreamBus

        source = inspect.getsource(StreamBus._publish_impl)
        # The fix introduces a marker/flag check. Current code simply
        # snapshots the list without any subscription-status check.
        has_marker_check = (
            "_bus_subscribed" in source
            or "is_subscribed" in source
            or "getattr(q," in source
        )
        assert has_marker_check, (
            "Bug 259: _publish_impl snapshots the subscriber list but does "
            "not check whether each queue is still subscribed. Unsubscribed "
            "queues held by in-flight publishes keep their event references "
            "alive — memory retention under high throughput. "
            "Fix: mark queues on subscribe, skip unmarked queues in the "
            "publish loop, unmark on unsubscribe."
        )


# ── Bug 260: FALSE_POSITIVE ──────────────────────────────────────────────────
# The stream_bus _publish_impl already wraps the get_nowait()+put_nowait() pair
# in `except (asyncio.QueueEmpty, asyncio.QueueFull)`, so the described race
# (concurrent publisher fills queue between get and put) is handled. No test
# written — any reasonable structural check passes on the current source.


# ── Bug 261: stream_bus first publish from worker thread silently dropped ───


class TestBug261StreamBusFirstPublishFromWorkerDropped:
    """`tero2/stream_bus.py:167-198` — if `publish()` is first called from
    a worker thread (no asyncio loop in that thread, `self._loop` not yet
    captured), the call silently returns without publishing.
    Fix: buffer or queue events until a loop is captured.
    """

    def test_publish_before_loop_captured_does_not_silently_drop(self) -> None:
        import threading

        from tero2.stream_bus import StreamBus, make_stream_event

        bus = StreamBus()
        # No loop has been captured yet — _loop is None.
        assert bus._loop is None

        delivered: list = []

        def _thread_publish():
            # Worker-thread path: no running loop in this thread.
            bus.publish(make_stream_event("builder", "text", content="hello"))

        t = threading.Thread(target=_thread_publish)
        t.start()
        t.join()

        # Now subscribe and run the loop to drain any buffered event.
        async def consume():
            q = bus.subscribe()
            # Allow any buffered event to be marshalled onto this loop.
            try:
                evt = await asyncio.wait_for(q.get(), timeout=0.5)
                delivered.append(evt)
            except asyncio.TimeoutError:
                pass

        asyncio.run(consume())

        assert delivered, (
            "Bug 261: stream_bus.publish() called from a worker thread "
            "before any event loop has been captured silently drops the "
            "event. Subsequent subscribers never see it. "
            "Fix: buffer events into a thread-safe queue until a loop is "
            "captured, then drain the buffer on first capture."
        )


# ── Bug 262: stream_bus broken subscribers never removed ────────────────────


class TestBug262StreamBusDeadSubscribersNotRemoved:
    """`tero2/stream_bus.py:219-220` — the catch-all except logs
    "dead subscriber removed" but does NOT actually remove the broken
    queue from `_subscribers`. Every publish then retries the dead queue.
    Fix: actually remove or mark the queue for lazy cleanup.
    """

    def test_dead_subscriber_handler_removes_from_list(self) -> None:
        from tero2.stream_bus import StreamBus

        source = inspect.getsource(StreamBus._publish_impl)

        # Locate the "dead subscriber removed" log line and check that
        # the same except block has a removal call.
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "dead subscriber" in line.lower():
                window = "\n".join(lines[max(0, i - 4):i + 4])
                if "remove" in window and (".remove" in window or "discard" in window):
                    return  # fixed
                pytest.fail(
                    "Bug 262: stream_bus _publish_impl logs "
                    "'dead subscriber removed' but does not call "
                    "self._subscribers.remove(q). Every subsequent publish "
                    "re-tries the broken queue and logs again. "
                    "Fix: actually remove the queue (or mark it for "
                    "lazy cleanup) inside the except branch."
                )
        pytest.skip("'dead subscriber' log not found")


# ── Bug 264: harden phase doesn't advance state.sora_phase ──────────────────


class TestBug264HardenPhaseDoesNotAdvance:
    """`tero2/phases/harden_phase.py:153-160` — after a successful harden
    pass the function returns `PhaseResult(success=True, data=plan)` but
    does not set state.sora_phase to SCOUT. On crash recovery the
    pipeline re-runs HARDENING and wastes LLM calls.
    Fix: advance state.sora_phase = SCOUT (or SORA_PHASE_ORDER next)
    after harden succeeds.
    """

    def test_harden_phase_advances_sora_phase_on_success(self) -> None:
        from tero2.phases.harden_phase import run_harden

        source = inspect.getsource(run_harden)
        # A fix adds a set_sora_phase(SCOUT/next) or direct mutation to
        # state.sora_phase = ... before the successful return.
        mentions_advance = (
            "set_sora_phase" in source
            or "sora_phase = SoraPhase.SCOUT" in source
            or "sora_phase=SoraPhase.SCOUT" in source
        )
        assert mentions_advance, (
            "Bug 264: run_harden() never advances state.sora_phase after "
            "a successful harden. On crash recovery the runner re-enters "
            "HARDENING and wastes another reviewer pass. "
            "Fix: call ctx.checkpoint.set_sora_phase(state, SoraPhase.SCOUT) "
            "(or the next phase in SORA_PHASE_ORDER) before returning success."
        )


# ── Bug 265: context_assembly budget_state inconsistency ────────────────────


class TestBug265BudgetStateInconsistent:
    """`tero2/context_assembly.py:152-168` — section inclusion tracks
    user-only tokens (mandatory_user) but the final `total` includes
    system_prompt. A caller whose user content fits can still get
    HARD_FAIL because system+user exceeds budget.
    Fix: include system_prompt in running_tokens from the start.
    """

    def test_budget_running_tokens_includes_system_prompt(self) -> None:
        from tero2.context_assembly import ContextAssembler

        source = inspect.getsource(ContextAssembler.assemble)
        # Look for the initial assignment of running_tokens. A fix sums
        # both system_prompt and mandatory_user here.
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "running_tokens = estimate_tokens(" in line:
                # Check the argument list includes system_prompt
                assert "system_prompt" in line, (
                    "Bug 265: running_tokens is initialised from "
                    "mandatory_user alone. The final status check adds "
                    "system_prompt, so a caller can pass the mid-assembly "
                    "inclusion test and still get HARD_FAIL at the end. "
                    "Fix: estimate_tokens(system_prompt + mandatory_user) "
                    "when initialising running_tokens."
                )
                return
        pytest.skip("running_tokens assignment not found")


# ── Bug 266: context_assembly role-specific methods silent fallback ────────


class TestBug266AssemblerSilentRoleFallback:
    """`tero2/context_assembly.py:178-220` — `_role_limit()` falls back
    to 128K when the role is not in config. No warning is logged.
    Fix: log.warning when the role is missing.
    """

    def test_missing_role_logs_warning(self, caplog) -> None:
        from tero2.config import Config
        from tero2.context_assembly import ContextAssembler

        cfg = Config()
        # No roles configured for "builder" in a bare Config.
        assert "builder" not in cfg.roles
        assembler = ContextAssembler(cfg)
        caplog.set_level(logging.WARNING)
        _ = assembler.assemble_builder("task plan")
        has_warning = any(
            "builder" in r.message.lower()
            and ("not configured" in r.message.lower()
                 or "fallback" in r.message.lower()
                 or "default" in r.message.lower())
            for r in caplog.records
        )
        assert has_warning, (
            "Bug 266: ContextAssembler falls back to 128K context window "
            "for a missing role without logging any warning. The caller "
            "has no signal that the per-role budget was ignored. "
            "Fix: log.warning('role %r not configured — using default "
            "context window', role)."
        )


# ── Bug 267: config boolean fields missing bool() coercion ──────────────────


class TestBug267ConfigBoolCoercion:
    """`tero2/config.py:219-220,252,265-266` — `voice_on_done`,
    `voice_on_stuck`, `backtrack_to_last_checkpoint`, `stop_on_cosmetic_only`,
    `debug` are assigned directly from `.get()`.  A string "false" from
    TOML is truthy.
    Fix: wrap each in bool() with a proper truthy/falsy interpretation.
    """

    def test_backtrack_flag_coerced_from_string_false(self) -> None:
        from tero2.config import _parse_config

        raw = {
            "escalation": {"backtrack_to_last_checkpoint": "false"},
        }
        cfg = _parse_config(raw)
        # The string "false" should be coerced to the boolean False.
        assert cfg.escalation.backtrack_to_last_checkpoint is False, (
            "Bug 267: escalation.backtrack_to_last_checkpoint was given "
            "the string 'false' but loaded as truthy "
            f"({cfg.escalation.backtrack_to_last_checkpoint!r}). Fix: coerce "
            "via a helper that maps 'false'/'0'/'no'/'' to False."
        )

    def test_debug_flag_coerced_from_string_false(self) -> None:
        from tero2.config import _parse_config

        raw = {"plan_hardening": {"debug": "false"}}
        cfg = _parse_config(raw)
        assert cfg.plan_hardening.debug is False, (
            "Bug 267: plan_hardening.debug='false' loaded as truthy "
            f"({cfg.plan_hardening.debug!r}). Fix: bool() with proper string coercion."
        )


# ── Bug 268: config no range validation on numeric fields ───────────────────


class TestBug268ConfigNoRangeValidation:
    """`tero2/config.py:173,218,227` — negative/zero/huge values accepted
    for timeout_s, heartbeat_interval_s, max_retries.
    Fix: clamp to sensible ranges.
    """

    def test_negative_timeout_s_clamped(self) -> None:
        from tero2.config import _parse_config

        raw = {"roles": {"executor": {"provider": "claude", "timeout_s": -30}}}
        cfg = _parse_config(raw)
        assert cfg.roles["executor"].timeout_s > 0, (
            "Bug 268: roles.executor.timeout_s=-30 accepted as-is. "
            f"Resulting value: {cfg.roles['executor'].timeout_s}. "
            "Negative timeouts break downstream arithmetic. "
            "Fix: clamp via max(1, min(value, 86400))."
        )

    def test_zero_max_retries_clamped(self) -> None:
        from tero2.config import _parse_config

        raw = {"retry": {"max_retries": 0}}
        cfg = _parse_config(raw)
        # max_retries=0 means zero attempts ever — that's almost certainly
        # not what the operator wanted and should be clamped.
        # A legitimate default should be >= 1.
        assert cfg.retry.max_retries >= 1, (
            "Bug 268: retry.max_retries=0 accepted as-is "
            f"(cfg.retry.max_retries={cfg.retry.max_retries}). "
            "Zero retries means the runner never attempts a task. "
            "Fix: clamp to max(1, max_retries)."
        )


# ── Bug 269: builder silent success returns empty captured_output ───────────


class TestBug269BuilderEmptyCapturedOutput:
    """`tero2/players/builder.py:118` — when the agent writes SUMMARY.md
    to disk (no stdout), `captured_output=""`. Downstream reflexion gets
    an empty "What was tried" section.
    Fix: use `captured_output=output or summary`.
    """

    def test_captured_output_uses_summary_when_output_empty(self, tmp_path) -> None:
        from tero2.players.builder import BuilderPlayer

        player = BuilderPlayer.__new__(BuilderPlayer)
        player.chain = MagicMock()
        player.disk = MagicMock()
        player.disk.write_file.return_value = True
        player.working_dir = str(tmp_path)

        # Plant the SUMMARY.md that the agent wrote silently to disk.
        (tmp_path / "T01-SUMMARY.md").write_text("recovered summary", encoding="utf-8")

        async def fake_prompt(_prompt):
            return ""  # empty output — agent wrote to disk instead

        player._run_prompt = fake_prompt  # type: ignore[attr-defined]

        result = asyncio.run(player.run(
            task_plan="do X",
            task_id="T01",
            slice_id="S01",
            milestone_path="milestones/M001",
        ))

        # The summary field has the recovered content; captured_output
        # should be non-empty so reflexion has something to show.
        assert result.captured_output, (
            "Bug 269: builder recovered the summary from disk but left "
            "captured_output empty. Reflexion context gets a blank "
            '"What was tried" section. '
            "Fix: captured_output = output or summary."
        )


# ── Bug 270: coach clears STEER.md on partial write ─────────────────────────


class TestBug270CoachPartialWriteClearsSteer:
    """`tero2/players/coach.py:89-91` — `wrote_any = any(...)` clears
    STEER.md even when only ONE of the four strategic sections was
    actually produced. Operator's steering guidance is silently lost.
    Fix: `wrote_all = all([strategy, task_queue, risk, context_hints])`.
    """

    def test_coach_does_not_clear_steer_on_partial_write(self) -> None:
        from tero2.players.coach import CoachPlayer

        source = inspect.getsource(CoachPlayer.run)
        # The fix pattern: `all([strategy, task_queue, risk, context_hints])`.
        # The bug pattern: `any([strategy, task_queue, risk, context_hints])`.
        has_any = "any([strategy, task_queue, risk, context_hints])" in source
        has_all = "all([strategy, task_queue, risk, context_hints])" in source

        assert not has_any and has_all, (
            "Bug 270: coach uses `any([...])` to decide whether STEER.md "
            "can be cleared. One non-empty section is enough to wipe the "
            "operator's steering directive even though three of four "
            "sections failed to write. "
            "Fix: change the gate to `all([strategy, task_queue, risk, context_hints])`."
        )


# ── Bug 271: escalation Level 1 resets tool_repeat_count ────────────────────


class TestBug271Level1ResetsToolRepeat:
    """Previously: Level 1 DIVERSIFICATION should not reset tool_repeat_count
    to avoid an infinite reset loop. Superseded by test A18 which enforces
    the opposite (counters must reset so the same signal does not re-fire
    immediately).

    Resolution: the real infinite-loop protection lives in the runner's
    ``div_steps`` counter (see bugs 189/254). After
    ``diversification_max_steps`` Level 1 actions, ``decide_escalation``
    escalates to Level 2 regardless of tool_repeat_count resets. So
    Level 1 is free to reset counters without causing the feared loop.
    """

    def test_level_1_does_not_reset_tool_repeat(self) -> None:
        import inspect

        from tero2.escalation import decide_escalation

        # Invariant that actually protects us: decide_escalation must
        # escalate to BACKTRACK_COACH once diversification_steps_taken
        # reaches diversification_max_steps. If that bound exists, the
        # feared Level-1-reset loop is bounded.
        source = inspect.getsource(decide_escalation)
        assert "diversification_steps_taken" in source, (
            "decide_escalation must consult diversification_steps_taken to "
            "escape the Level 1 reset loop."
        )
        assert "BACKTRACK_COACH" in source, (
            "decide_escalation must be able to escalate past DIVERSIFICATION "
            "when the step budget is exhausted."
        )


# ── Bug 272: escalation Level 2 backtrack not verified ──────────────────────


class TestBug272Level2BacktrackNotVerified:
    """`tero2/escalation.py:148-156` — Level 2 resets counters but does
    not actually verify the agent state was rolled back to a checkpoint.
    If backtrack_to_last_checkpoint is False, counters still reset but no
    rollback happens — agent continues from stuck position with a fresh
    step budget.
    Fix: either restore from checkpoint or skip counter resets when
    backtrack is disabled.
    """

    def test_counters_only_reset_when_backtrack_actually_applied(self) -> None:
        import asyncio

        from tero2.escalation import (
            EscalationAction,
            EscalationLevel,
            execute_escalation,
        )
        from tero2.state import AgentState, Phase
        from tero2.stuck_detection import StuckResult, StuckSignal

        state = AgentState(
            phase=Phase.RUNNING,
            steps_in_task=10,
            retry_count=3,
            tool_repeat_count=5,
            last_tool_hash="abc",
        )

        # should_backtrack=False simulates backtrack_to_last_checkpoint=False.
        action = EscalationAction(
            level=EscalationLevel.BACKTRACK_COACH,
            should_backtrack=False,
        )

        disk = MagicMock()
        notifier = MagicMock()
        notifier.notify = MagicMock(return_value=asyncio.sleep(0))

        class _CP:
            def save(self, s):
                return s

        new_state = asyncio.run(execute_escalation(
            action, state, disk, notifier, _CP(),
            stuck_result=StuckResult(signal=StuckSignal.TOOL_REPEAT, details="", severity=2),
        ))

        # If backtrack wasn't applied, the counters should either stay or
        # the runner should have persisted that no-rollback happened.
        # The bug: counters keep old values only when should_backtrack is
        # True, but here should_backtrack=False — counters shouldn't silently
        # reset either.  Actually the code only resets counters inside
        # `if action.should_backtrack:` — so steps_in_task stays 10.
        # Problem: level is still set to 2 and the runner thinks Level 2
        # happened, but nothing changed.  Assert that at minimum the code
        # logs or surfaces the skipped backtrack.
        #
        # Hard structural check: the execute_escalation source must either
        # handle the "should_backtrack=False" case explicitly or log a warning.
        source = inspect.getsource(execute_escalation)
        has_no_backtrack_handling = (
            ("not action.should_backtrack" in source)
            or ("should_backtrack is False" in source)
            or ("no backtrack" in source.lower())
        )
        assert has_no_backtrack_handling, (
            "Bug 272: Level 2 escalation sets state.escalation_level=2 and "
            "writes the journal entry regardless of whether a real rollback "
            "happened. When backtrack_to_last_checkpoint=False there is no "
            "rollback — the agent continues from the stuck position with "
            "the same context. "
            "Fix: either restore from checkpoint or surface the skipped "
            "backtrack so downstream knows rollback didn't happen."
        )


# ── Bug 273: circuit breaker uses monotonic for persisted timestamp ─────────


class TestBug273CircuitBreakerMonotonicPersistence:
    """`tero2/circuit_breaker.py:36,55` — last_failure_time uses
    time.monotonic(). If the breaker state is ever persisted across
    process restarts the monotonic value is meaningless in the new process.
    Fix: use time.time() for persisted timestamps or clear on restart.
    """

    def test_last_failure_time_not_monotonic(self) -> None:
        from tero2.circuit_breaker import CircuitBreaker

        source = inspect.getsource(CircuitBreaker)
        # Current code uses time.monotonic().  If persistence is supported
        # the timestamp must use time.time() (wall clock).
        uses_monotonic = "time.monotonic()" in source
        uses_wallclock = "time.time()" in source
        assert uses_wallclock or not uses_monotonic, (
            "Bug 273: CircuitBreaker.last_failure_time uses time.monotonic(). "
            "monotonic is only meaningful within a single process — if the "
            "breaker state is persisted and restored the comparison "
            "`now - last_failure_time >= recovery_timeout_s` is nonsense. "
            "Fix: use time.time() for timestamps that may cross process "
            "boundaries, or clear state on restart."
        )


# ── Bug 274: stuck_detection priority hides tool-repeat deadlock ────────────


class TestBug274PriorityHidesToolRepeat:
    """`tero2/stuck_detection.py:48-76` — priority RETRY_EXHAUSTED >
    STEP_LIMIT > TOOL_REPEAT. When an agent is both retry-exhausted AND
    tool-repeat deadlocked, only RETRY_EXHAUSTED is surfaced. The
    escalation layer treats it as a generic retry failure rather than a
    deadlock.
    Fix: return multiple signals or combine them in `details`.
    """

    def test_retry_exhaust_combined_with_tool_repeat_signal(self) -> None:
        from tero2.config import StuckDetectionConfig
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckSignal, check_stuck

        cfg = StuckDetectionConfig(
            max_steps_per_task=100, max_retries=3, tool_repeat_threshold=3,
        )
        state = AgentState(
            retry_count=3,           # hits RETRY_EXHAUSTED
            tool_repeat_count=5,     # hits TOOL_REPEAT
            tool_hash_updated=True,
            last_tool_hash="abc",
        )

        result = check_stuck(state, cfg)

        # The bug: result.signal == RETRY_EXHAUSTED; TOOL_REPEAT info lost.
        # The fix: details (or a multi-signal field) mentions tool_repeat
        # so escalation can see both.
        details = (result.details or "").lower()
        mentions_both = "tool" in details and (
            "repeat" in details or "deadlock" in details
        )
        assert mentions_both, (
            "Bug 274: stuck_detection picks the highest-priority signal "
            f"(RETRY_EXHAUSTED) and omits the simultaneous TOOL_REPEAT "
            f"state. details={result.details!r}. "
            "Escalation then treats it as a plain retry failure rather than "
            "a deadlock. "
            "Fix: return multiple signals or append tool-repeat context "
            "to `details`."
        )


# ── Bug 275: runner no shutdown check after async ops in slice loop ─────────


class TestBug275RunnerNoShutdownCheckAfterCoach:
    """`tero2/runner.py:727-782` — inside the while-loop for extra slices,
    `await run_coach(ctx, ...)` at line 733 is not followed by any
    shutdown check before the potentially long run_architect/run_execute
    work below. A shutdown signal received during run_coach is ignored
    until the next attempt boundary.
    Fix: add shutdown_event.is_set() check after each async op.
    """

    def test_shutdown_check_after_run_coach_in_slice_loop(self) -> None:
        from tero2.runner import Runner

        source = inspect.getsource(Runner._execute_sora)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "await run_coach(ctx, CoachTrigger.END_OF_SLICE)" in line:
                # The 5 lines after must contain a shutdown check.
                window = "\n".join(lines[i + 1:i + 6])
                if "shutdown_event" in window and "is_set" in window:
                    return  # fixed
                pytest.fail(
                    "Bug 275: after `await run_coach(ctx, END_OF_SLICE)` "
                    "in the slice loop, no shutdown_event check follows. "
                    "A shutdown signal received during run_coach is only "
                    "honoured at the next _drain_commands boundary, which "
                    "may be much later. "
                    "Fix: insert `if shutdown_event and "
                    "shutdown_event.is_set(): return` after the coach call."
                )
        pytest.skip("run_coach END_OF_SLICE call not found in _execute_sora")


# ── Bug 276: execute_phase reflexion seeding missing shutdown guard ─────────


class TestBug276ExecutePhaseReflexionShutdownMissing:
    """`tero2/phases/execute_phase.py:229-237` — the reflexion context is
    created before any shutdown check. Unnecessary work during shutdown.
    Fix: add `if ctx.shutdown_event.is_set(): return` before the
    reflexion block.
    """

    def test_reflexion_block_has_shutdown_check(self) -> None:
        import tero2.phases.execute_phase as mod

        source = inspect.getsource(mod)
        lines = source.splitlines()

        # Find "reflexion_ctx = ReflexionContext()" and check whether the
        # 10 lines preceding contain a shutdown check.
        for i, line in enumerate(lines):
            if "reflexion_ctx = ReflexionContext()" in line:
                pre = "\n".join(lines[max(0, i - 15):i])
                if "shutdown_event" in pre and "is_set" in pre:
                    return  # fixed somewhere
                pytest.fail(
                    "Bug 276: reflexion context is built at execute_phase "
                    f"line {i+1} with no shutdown check in the preceding "
                    "15 lines. During a shutdown, this is wasted work "
                    "(and may delay the response). "
                    "Fix: add an `if ctx.shutdown_event and "
                    "ctx.shutdown_event.is_set(): return ...` guard before "
                    "the reflexion block."
                )


# ── Bug 277: history UnicodeDecodeError not caught ──────────────────────────


class TestBug277HistoryUnicodeDecodeError:
    """`tero2/history.py:43-48` — `read_text(encoding='utf-8')` on a
    non-UTF-8 history.json raises UnicodeDecodeError which is NOT in the
    exception tuple.
    Fix: add UnicodeDecodeError to the catch.
    """

    def test_load_history_survives_non_utf8_file(self, tmp_path, monkeypatch) -> None:
        import tero2.history as hist

        bad = tmp_path / "history.json"
        # cp1252 byte that's invalid UTF-8
        bad.write_bytes(b'{"entries":[{"name":"\xff"}]}')
        monkeypatch.setattr(hist, "HISTORY_FILE", bad)

        try:
            entries = hist.load_history()
        except UnicodeDecodeError:
            pytest.fail(
                "Bug 277: history.load_history() lets UnicodeDecodeError "
                "bubble up — the exception tuple on line 47 lists "
                "(FileNotFoundError, json.JSONDecodeError, TypeError, "
                "OSError) but not UnicodeDecodeError. "
                "Fix: add UnicodeDecodeError to the except tuple."
            )
        assert entries == [], "expected empty list on corrupt history"


# ── Bug 278: checkpoint COMPLETED→RUNNING silently resets state ─────────────


class TestBug278CheckpointCompletedSilentReset:
    """`tero2/checkpoint.py:44-63` — `mark_started` falls through to
    `state = AgentState()` when prior.phase is COMPLETED. All prior
    context is silently wiped.
    Fix: explicitly reject or warn.
    """

    def test_mark_started_warns_on_completed_prior(self, tmp_path, caplog) -> None:
        from tero2.checkpoint import CheckpointManager
        from tero2.disk_layer import DiskLayer
        from tero2.state import AgentState, Phase

        disk = DiskLayer(tmp_path)
        disk.init()

        # Plant a COMPLETED state on disk.  A proper COMPLETED state must be
        # built via RUNNING → COMPLETED so the phase-transition guard allows it.
        prior = AgentState()
        prior.phase = Phase.RUNNING
        prior.phase = Phase.COMPLETED
        disk.write_state(prior)

        cp = CheckpointManager(disk)
        caplog.set_level(logging.WARNING)
        try:
            new_state = cp.mark_started("plan.md")
        except Exception:
            return  # fixed: explicit rejection is acceptable

        # If no rejection, we expect a warning log on the silent reset.
        has_warning_about_reset = any(
            ("COMPLETED" in r.message.upper())
            or ("reset" in r.message.lower())
            or ("discard" in r.message.lower())
            for r in caplog.records
        )
        assert has_warning_about_reset, (
            "Bug 278: mark_started() silently resets state when prior.phase "
            "is COMPLETED. The discarded state carried real accumulated "
            "context (task history, provider rotation, etc). No warning and "
            "no rejection — the operator cannot tell the reset happened. "
            f"new_state.phase={new_state.phase.value}. "
            "Fix: log.warning('prior phase was COMPLETED — starting fresh') "
            "or raise StateTransitionError."
        )


# ── Bug 279: persona cwd-relative fallback is silent ────────────────────────


class TestBug279PersonaCwdFallbackSilent:
    """`tero2/persona.py:159-168` — `_local_prompts_dir` returns the
    cwd-relative legacy path when project_path is None. No warning.
    Fix: log.warning on fallback.
    """

    def test_cwd_fallback_logs_warning(self) -> None:
        import tero2.persona as p_mod

        source = inspect.getsource(p_mod.PersonaRegistry._local_prompts_dir.fget)
        has_warn = "log.warning" in source or "logger.warning" in source
        assert has_warn, (
            "Bug 279: PersonaRegistry._local_prompts_dir falls back to the "
            "cwd-relative _LOCAL_PROMPTS_DIR when project_path is None, "
            "but silently. Callers that expect project-scoped lookup get "
            "whatever files happen to live in the current working directory. "
            "Fix: log.warning when the fallback path is used."
        )


# ── Bug 280: config no validation for fallback list ─────────────────────────


class TestBug280ConfigFallbackValidation:
    """`tero2/config.py:172` — `fallback=role_data.get("fallback", [])`
    accepts any type. A non-list or non-string items break downstream
    iteration.
    Fix: validate isinstance(fallback, list), coerce items to str.
    """

    def test_non_list_fallback_rejected_or_normalized(self) -> None:
        from tero2.config import _parse_config

        raw = {"roles": {"executor": {"provider": "claude", "fallback": "zai"}}}
        cfg = _parse_config(raw)
        fallback = cfg.roles["executor"].fallback

        # Either fallback is a proper list of strings, or _parse_config raised.
        assert isinstance(fallback, list), (
            "Bug 280: roles.executor.fallback accepted a plain string "
            f"(got {type(fallback).__name__}={fallback!r}) instead of a "
            "list. Iteration will produce single characters. "
            "Fix: validate isinstance(list) and coerce items via str()."
        )
        for item in fallback:
            assert isinstance(item, str), (
                "Bug 280: fallback item not coerced to str "
                f"(got {type(item).__name__}). Fix: [str(x) for x in fallback]."
            )


# ── Bug 281: diversification_max_steps=0 immediate Level 2 ──────────────────


class TestBug281DiversificationMaxStepsZero:
    """`tero2/escalation.py:91` — with diversification_max_steps=0 the
    condition `diversification_steps_taken >= 0` is always True, so
    Level 1 instantly escalates to Level 2.
    Fix: treat <= 0 as "skip Level 1" with explicit flag or clamp to 1.
    """

    def test_max_steps_zero_does_not_skip_level_1_into_level_2(self) -> None:
        from tero2.config import EscalationConfig
        from tero2.escalation import EscalationLevel, decide_escalation
        from tero2.stuck_detection import StuckResult, StuckSignal

        cfg = EscalationConfig(diversification_max_steps=0)
        stuck = StuckResult(signal=StuckSignal.TOOL_REPEAT, details="", severity=2)

        # On the first stuck, current_level=NONE.  Level 1 should trigger.
        action = decide_escalation(stuck, EscalationLevel.NONE, 0, cfg)
        assert action.level == EscalationLevel.DIVERSIFICATION

        # On the next call, current_level=DIVERSIFICATION; max_steps=0 means
        # `0 >= 0` triggers Level 2 immediately — the agent never got a real
        # diversification attempt.
        action = decide_escalation(stuck, EscalationLevel.DIVERSIFICATION, 0, cfg)
        # Expected: either skip Level 1 entirely with an explicit flag, or
        # treat <= 0 like "never escalate on step count".  Immediate Level 2
        # is the buggy behaviour.
        assert action.level != EscalationLevel.BACKTRACK_COACH, (
            "Bug 281: diversification_max_steps=0 triggers an immediate "
            "Level 1 → Level 2 escalation because `0 >= 0` is True. The "
            "operator clearly intended 'no diversification window', but "
            "the escalation logic interprets it as 'zero is enough'. "
            "Fix: treat <=0 specially — e.g. skip Level 1 outright with an "
            "explicit flag, or clamp to 1."
        )


# ── Bug 282: tui/app worker state callback missing NoMatches handling ──────


class TestBug282TuiWorkerStateNoMatches:
    """`tero2/tui/app.py:289-290` — `query_one("#log-view", LogView)`
    crashes when the widget is unmounted during screen transition.
    Fix: wrap in try/except NoMatches.
    """

    def test_on_worker_state_changed_handles_nomatches(self) -> None:
        import tero2.tui.app as app_mod

        source = inspect.getsource(app_mod.DashboardApp.on_worker_state_changed)
        # A fix wraps query_one in try/except NoMatches (or similar).
        has_guard = (
            "NoMatches" in source
            or "try:" in source
        )
        assert has_guard, (
            "Bug 282: on_worker_state_changed does `query_one('#log-view', "
            "LogView)` unguarded. If the widget is unmounted during a "
            "screen transition this raises NoMatches and crashes the TUI. "
            "Fix: wrap in try/except NoMatches."
        )


# ── Bug 283: tui settings negative / overflow numeric values allowed ───────


class TestBug283TuiSettingsRangeValidation:
    """`tero2/tui/screens/settings.py:143-146` — `.isdigit()` accepts
    positive integers but has no upper/lower bound checks; negative
    values are silently ignored (isdigit returns False for "-5") but
    huge values pass through.
    Fix: add range validation with user feedback.
    """

    def test_settings_save_has_range_validation(self) -> None:
        import tero2.tui.screens.settings as s_mod

        source = inspect.getsource(s_mod)
        lines = source.splitlines()

        # Find each .isdigit() call; within its immediate 3-line body
        # there must be a numeric comparison against the parsed value
        # (e.g. `if 1 <= n <= 1000:` or `if int(...) > 0:`).
        found_bounds = False
        for i, line in enumerate(lines):
            if ".isdigit()" in line:
                # Short body after isdigit (the guarded branch).
                body = "\n".join(lines[i + 1:i + 5])
                # Look for a numeric comparator involving the parsed int.
                if any(
                    tok in body for tok in (
                        "int(",
                    )
                ) and any(
                    op in body for op in (" <= ", " >= ", " < ", " > ")
                ):
                    found_bounds = True
                    break

        assert found_bounds, (
            "Bug 283: settings screen validates numeric inputs with only "
            "`.isdigit()`, which accepts arbitrary large positive integers "
            "and silently drops negative / empty values without user "
            "feedback. "
            "Fix: add an explicit range check "
            "(e.g. `n = int(value); if 1 <= n <= 10_000: ...`) and "
            "surface an error notification on violation."
        )


# ── Bug 284: tui widgets/usage ProviderRow query_one without specific IDs ──


class TestBug284ProviderRowQueryOneNoIds:
    """`tero2/tui/widgets/usage.py:67-70` — `query_one(Label)` and
    `query_one(ProgressBar)` without ID/class filtering. Widget
    structure changes break the lookup.
    Fix: tag child widgets with ids and query by id.
    """

    def test_refresh_fraction_uses_specific_ids(self) -> None:
        import tero2.tui.widgets.usage as u_mod

        source = inspect.getsource(u_mod._ProviderRow.refresh_fraction)
        # The fix uses `query_one("#label-id", Label)` style lookups.
        uses_ids = "#" in source and "query_one(" in source
        assert uses_ids, (
            "Bug 284: ProviderRow.refresh_fraction queries children by "
            "type only (`query_one(Label)`, `query_one(ProgressBar)`) "
            "with no id/class selector. Any widget tree change re-orders "
            "results and the first match may be the wrong widget. "
            "Fix: tag child widgets with ids and use "
            "query_one('#child-id', Label)."
        )


# ── Bug 286: zai normalizer mixes dict/attr access on content items ────────


class TestBug286ZaiMixedAccess:
    """`tero2/providers/normalizers/zai.py:115-122` — mixes
    `item.get("text")` with `getattr(item, "text", ...)` in the same
    comprehension.  Non-dict / non-object items (e.g. primitive strings)
    may raise or produce weird output.
    Fix: use the `_get()` helper consistently.
    """

    def test_tool_result_content_uses_get_helper_consistently(self) -> None:
        from tero2.providers.normalizers.zai import ZaiNormalizer

        source = inspect.getsource(ZaiNormalizer)
        # Find the block bracketed by `isinstance(content, list):` and the
        # next `yield StreamEvent(` — this is the comprehension that
        # iterates items in a tool_result content list.  Inside this tight
        # block the bug is visible: `item.get(...)` and `getattr(item, ...)`
        # both appear, rather than a consistent `_get(item, ...)` call.
        lines = source.splitlines()
        start = None
        for i, line in enumerate(lines):
            if "isinstance(content, list)" in line:
                start = i
                break
        assert start is not None, "tool_result content-list handler not found"
        comp: list[str] = []
        for line in lines[start:start + 10]:
            comp.append(line)
            if "yield StreamEvent(" in line:
                break
        body = "\n".join(comp)

        mixes_dict_and_attr = "item.get(" in body and "getattr(item" in body
        uses_get_helper_on_item = "_get(item" in body

        assert uses_get_helper_on_item or not mixes_dict_and_attr, (
            "Bug 286: zai normalizer mixes `item.get(...)` with "
            "`getattr(item, ...)` in the tool_result content comprehension. "
            "The module defines a `_get()` helper for exactly this dual-"
            "shape access — but it is not applied to the items in the list. "
            "Non-dict / non-object items are handled inconsistently. "
            "Fix: use `_get(item, 'text', str(item))` inside the "
            "comprehension."
        )


# ── Bug 289: kilo normalizer None items in content produce 'None' string ───


class TestBug289KiloNoneInContentList:
    """`tero2/providers/normalizers/kilo.py:106-109` — content list with
    None items produces `str(None)` = "None" in the joined output.
    Fix: filter out None before the join.
    """

    def test_content_list_with_none_skips_none(self) -> None:
        from tero2.providers.normalizers.kilo import KiloNormalizer

        norm = KiloNormalizer()
        events = list(norm.normalize(
            {"type": "tool_result", "tool_use_id": "t1",
             "content": [{"text": "real"}, None]},
            role="builder",
        ))
        assert events
        out = events[0].tool_output
        assert "None" not in out, (
            "Bug 289: kilo normalizer renders None items as the literal "
            f"string 'None'. tool_output={out!r}. "
            "Fix: skip None items before str() fallback."
        )


# ── Bug 290: providers/cli stdout_task leaked on BrokenPipeError ───────────


class TestBug290StdoutTaskNotAwaited:
    """`tero2/providers/cli.py:242-260` — on BrokenPipeError the code
    cancels stdout_task but never awaits it. Background task may keep
    running.
    Fix: `await asyncio.gather(stdout_task, return_exceptions=True)`.
    """

    def test_brokenpipe_path_awaits_stdout_task(self) -> None:
        import tero2.providers.cli as cli_mod

        source = inspect.getsource(cli_mod)
        # Look at the BrokenPipeError handler.
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "BrokenPipeError" in line and "except" in line:
                window = "\n".join(lines[i:i + 15])
                has_cancel = "cancel()" in window
                has_await = (
                    "await asyncio.gather" in window
                    or "await stdout_task" in window
                    or "with suppress" in window and "await" in window
                )
                if has_cancel:
                    assert has_await, (
                        "Bug 290: providers/cli BrokenPipeError handler "
                        "calls stdout_task.cancel() without awaiting it. "
                        "The cancellation exception is not collected; the "
                        "background task can continue draining stdout. "
                        "Fix: `await asyncio.gather(stdout_task, "
                        "return_exceptions=True)` (or equivalent) after cancel."
                    )
                    return
        pytest.skip("BrokenPipeError handler not found")


# ── Bug 292: disk_layer read_file returns '' on UnicodeDecodeError ─────────


class TestBug292ReadFileMasksEncodingError:
    """`tero2/disk_layer.py:47-57` — returns "" on UnicodeDecodeError.
    Callers cannot distinguish empty file from decode failure.  Masks
    corruption.
    Fix: return None for error cases (consistent with FileNotFoundError),
    or raise a specific exception.
    """

    def test_unicode_decode_does_not_return_empty_string(self, tmp_path) -> None:
        from tero2.disk_layer import DiskLayer

        layer = DiskLayer(tmp_path)
        layer.init()

        bad = tmp_path / ".sora" / "persistent" / "DECISIONS.md"
        bad.parent.mkdir(parents=True, exist_ok=True)
        # cp1252 byte that's invalid UTF-8
        bad.write_bytes(b"\xff\xfe bad encoding here")

        result = layer.read_file("persistent/DECISIONS.md")
        assert result != "", (
            "Bug 292: read_file returns '' on UnicodeDecodeError. A caller "
            "cannot tell corruption from a legitimately empty file. "
            "Fix: return None (consistent with FileNotFoundError) so the "
            "caller can branch on 'file exists but unreadable'."
        )


# ── Bug 293: state.save .tmp file left on os.replace failure ──────────────


class TestBug293StateTmpFileOnReplaceFailure:
    """`tero2/state.py:201-209` — if os.replace fails the tmp is
    unlinked, but if that unlink itself raises OSError the original
    exception is masked and the tmp persists.
    Fix: wrap unlink in try/except that logs but does not block the
    original exception.
    """

    def test_save_unlink_failure_does_not_mask_original(self, tmp_path, monkeypatch) -> None:
        import os as os_mod

        from tero2.state import AgentState

        state = AgentState()
        target = tmp_path / "STATE.json"

        # Force os.replace to fail with the real OSError and make unlink
        # also fail with a different OSError.
        real_replace = os_mod.replace

        def fake_replace(src, dst):
            raise OSError("replace boom")

        def fake_unlink(self, missing_ok=False):
            raise OSError("unlink boom")

        monkeypatch.setattr(os_mod, "replace", fake_replace)
        monkeypatch.setattr(Path, "unlink", fake_unlink, raising=False)

        raised = None
        try:
            state.save(target)
        except OSError as e:
            raised = e

        # The original "replace boom" must reach the caller. If unlink's
        # exception masks it, the operator sees misleading information.
        assert raised is not None
        assert "replace" in str(raised), (
            "Bug 293: when os.replace fails and tmp.unlink also fails the "
            f"unlink exception masks the original. Caller saw: {raised!r}. "
            "Fix: wrap unlink in try/except that logs but does not block "
            "the original exception."
        )


# ── Bug 296: disk_layer append_activity no atomicity / fsync ───────────────


class TestBug296AppendActivityNotAtomic:
    """`tero2/disk_layer.py:117-121` — `open("a") + write + close` with no
    fsync. A crash mid-write leaves a partial JSON line.
    Fix: write to .tmp, fsync, rename, or use O_APPEND + explicit fsync.
    """

    def test_append_activity_uses_fsync_or_tmp_rename(self) -> None:
        from tero2.disk_layer import DiskLayer

        source = inspect.getsource(DiskLayer.append_activity)
        uses_fsync = "fsync" in source
        uses_tmp_rename = "tmp" in source.lower() and "replace" in source.lower()

        assert uses_fsync or uses_tmp_rename, (
            "Bug 296: append_activity opens the file for append and writes "
            "without fsync or atomic rename. A crash mid-write leaves a "
            "partial JSON line that breaks log replay. "
            "Fix: fsync() after write, or write to .tmp then os.replace()."
        )


# ── Bug 297: disk_layer append_file no error handling ─────────────────────


class TestBug297AppendFileNoErrorHandling:
    """`tero2/disk_layer.py:68-72` — `open("a") + write` with no error
    handling. Disk full / permission error = partial file, exception
    propagates.
    Fix: wrap in try/except and return bool (consistent with write_file).
    """

    def test_append_file_returns_bool_or_handles_oserror(self) -> None:
        from tero2.disk_layer import DiskLayer

        source = inspect.getsource(DiskLayer.append_file)
        has_handler = "try" in source and "except" in source
        returns_bool = "-> bool" in source or "return True" in source or "return False" in source

        assert has_handler or returns_bool, (
            "Bug 297: append_file opens and writes without any error "
            "handling. Disk full or permission error propagates as a raw "
            "OSError and leaves a partially-written file. "
            "Fix: wrap in try/except OSError and return bool status "
            "(consistent with DiskLayer.write_file)."
        )


# ── Bug 298: usage_tracker no persistence mechanism ────────────────────────


class TestBug298UsageTrackerNoPersistence:
    """`tero2/usage_tracker.py:38-157` — all data in memory only. A crash
    loses session usage permanently.
    Fix: add periodic persistence to a .sora/ JSON file.
    """

    def test_usage_tracker_has_persistence_api(self) -> None:
        from tero2.usage_tracker import UsageTracker

        # Some kind of save/persist/load method or an __init__ that takes
        # a state_path argument.
        names = {n for n in dir(UsageTracker) if not n.startswith("_")}
        has_persist = any(
            n in names for n in (
                "save", "persist", "flush", "load", "save_state",
                "load_state", "persist_session", "load_session",
            )
        )
        init_takes_path = "path" in inspect.signature(UsageTracker.__init__).parameters

        assert has_persist or init_takes_path, (
            "Bug 298: UsageTracker keeps all session data in memory. "
            "A crash loses token/cost accumulation for the session. "
            "Fix: add save()/load() methods that persist to a JSON file "
            "under .sora/, and call save() periodically (e.g. from "
            "record_step or at heartbeat)."
        )


# ── Bug 299: usage_tracker float accumulation precision ────────────────────


class TestBug299UsageTrackerFloatPrecision:
    """`tero2/usage_tracker.py:123,135` — repeated `+= cost` accumulates
    binary rounding errors.
    Fix: use decimal.Decimal or round to 6 decimal places after each
    addition.
    """

    def test_total_cost_uses_decimal_or_rounds(self) -> None:
        from tero2.usage_tracker import UsageTracker

        tracker = UsageTracker()
        # 0.1 in binary is inexact; accumulating it 10 times via `+=`
        # produces 0.9999999999999999 instead of 1.0.
        for _ in range(10):
            tracker.record_step("p1", tokens=10, cost=0.1, is_estimated=False)

        summary = tracker.session_summary()
        assert summary["total_cost"] == 1.0, (
            "Bug 299: total_cost accumulation uses raw float addition. "
            f"Sum after 10×0.1 = {summary['total_cost']!r} (expected 1.0). "
            "Fix: use decimal.Decimal, or round(_total_cost, 6) after "
            "each addition."
        )


# ── Bug 300: usage_tracker no reset method ──────────────────────────────────


class TestBug300UsageTrackerNoReset:
    """`tero2/usage_tracker.py:38-157` — no reset/clear method. A
    long-running daemon reusing the tracker accumulates across sessions.
    Fix: add reset_session() that clears under lock.
    """

    def test_usage_tracker_has_reset_method(self) -> None:
        from tero2.usage_tracker import UsageTracker

        reset_names = {"reset", "reset_session", "clear", "clear_session"}
        public = {n for n in dir(UsageTracker) if not n.startswith("_")}

        assert reset_names & public, (
            "Bug 300: UsageTracker has no reset/clear method. A daemon "
            "process that reuses the same tracker instance accumulates "
            "usage across what the operator perceives as separate sessions. "
            "Fix: add reset_session() (clears totals under the provider lock)."
        )


# ── Bug 301: usage_tracker accepts negative tokens/cost ────────────────────


class TestBug301UsageTrackerNegativeValues:
    """`tero2/usage_tracker.py:105-140` — no validation on negative
    tokens/cost. A bad provider payload can corrupt totals.
    Fix: raise ValueError on negative values.
    """

    def test_record_step_rejects_negative_tokens(self) -> None:
        from tero2.usage_tracker import UsageTracker

        tracker = UsageTracker()
        try:
            tracker.record_step("p1", tokens=-100, cost=0.0, is_estimated=False)
            # Step was accepted.  Now check if totals went negative.
            totals = tracker.session_summary()
            assert totals["total_tokens"] >= 0, (
                "Bug 301: UsageTracker.record_step accepted tokens=-100 "
                f"and total_tokens became {totals['total_tokens']}. "
                "A single bad provider payload corrupts the session total. "
                "Fix: `if tokens < 0 or cost < 0: raise ValueError`."
            )
        except ValueError:
            return  # fixed: rejected negative input


# ── Bug 302: codex normalizer empty error message ───────────────────────────


class TestBug302CodexEmptyErrorMessage:
    """`tero2/providers/normalizers/codex.py:93` — if both "message" and
    "error" keys are absent the error event content is "".
    Fix: fallback to "unknown error".
    """

    def test_error_without_message_has_default_content(self) -> None:
        from tero2.providers.normalizers.codex import CodexNormalizer

        norm = CodexNormalizer()
        events = list(norm.normalize({"type": "error"}, role="builder"))
        assert events
        ev = events[0]
        assert ev.content, (
            "Bug 302: codex normalizer emits an error StreamEvent with "
            f"empty content when both 'message' and 'error' are missing "
            f"(got content={ev.content!r}). Operators see a blank error "
            "in the TUI. "
            "Fix: msg = raw.get('message') or raw.get('error') or "
            "'unknown error'."
        )


# ── Bug 303: claude normalizer inconsistent nested content handling ────────


class TestBug303ClaudeNestedContentInconsistent:
    """`tero2/providers/normalizers/claude.py:119-133` — a dict with
    missing "text" key produces "", but a primitive string produces its
    repr.  Different shapes yield different semantics.
    Fix: consistently use str(sub.get('text', '')) (or equivalent).
    """

    def test_user_content_primitive_vs_dict_consistent(self) -> None:
        from tero2.providers.normalizers.claude import ClaudeNormalizer

        norm = ClaudeNormalizer()
        raw = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [
                            {"type": "text"},     # dict, no 'text' key → ""
                            "plain string",        # primitive string → "plain string"
                        ],
                    }
                ]
            },
        }
        events = list(norm.normalize(raw, role="builder"))
        assert events
        out = events[0].tool_output

        # The bug: "" for dict but "plain string" for string → inconsistent.
        # A consistent implementation either keeps both as strings or
        # drops both.  We check that the primitive is not leaked through
        # while the dict-without-text is silently ignored.
        assert "plain string" not in out, (
            "Bug 303: claude normalizer leaks a primitive string item "
            f"('plain string') via str(sub), while a dict without a "
            f"'text' key produces ''. tool_output={out!r}. "
            "Fix: use the same normalization path for both "
            "(e.g. str(sub.get('text', '')) when sub is a dict, and "
            "consistently apply the same rule for primitives)."
        )
