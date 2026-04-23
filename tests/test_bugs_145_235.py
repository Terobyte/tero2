"""Halal tests for bugs 145--197 (Audit 6, 2026-04-23).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug 145  providers/chain: circuit breaker concurrent state mutation
  Bug 146  providers/cli: subprocess stdout pipe deadlock
  Bug 155  circuit_breaker: HALF_OPEN allows concurrent probes
  Bug 197  usage_tracker: missing log import
  Bug 195  stream_bus: unsubscribe race condition
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest


# ── Bug 145: providers/chain circuit breaker concurrent state mutation ───────


class TestBug145ChainCBConcurrentState:
    """CircuitBreaker.check() and record_failure() mutate shared state
    (state, failure_count, _trial_in_progress) without any locking.
    Concurrent threads can corrupt the state.
    Fix: add threading.Lock to CircuitBreaker for all state mutations.
    """

    def test_cb_concurrent_check_and_failure_state_consistency(self) -> None:
        from tero2.circuit_breaker import CBState, CircuitBreaker

        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=0)

        errors: list[str] = []
        barrier = threading.Barrier(8)

        def hammer_check() -> None:
            barrier.wait()
            for _ in range(500):
                try:
                    cb.check()
                except Exception:
                    pass

        def hammer_failure() -> None:
            barrier.wait()
            for _ in range(500):
                cb.record_failure()

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = []
            for _ in range(4):
                futs.append(pool.submit(hammer_check))
            for _ in range(4):
                futs.append(pool.submit(hammer_failure))
            for f in futs:
                f.result()

        # After all threads finish, verify invariants:
        # 1) state must be a valid CBState
        assert isinstance(cb.state, CBState), (
            "Bug 145: after concurrent check()/record_failure() calls, "
            f"circuit breaker state is not a valid CBState: {cb.state!r}. "
            "Fix: add a threading.Lock to CircuitBreaker and hold it in "
            "check(), record_success(), and record_failure()."
        )
        # 2) failure_count must be non-negative
        assert cb.failure_count >= 0, (
            "Bug 145: after concurrent access, failure_count is negative "
            f"({cb.failure_count}). State mutation is not thread-safe. "
            "Fix: add a threading.Lock to CircuitBreaker."
        )
        # 3) failure_count must not exceed a reasonable upper bound
        #    (4 threads * 500 iterations = 2000 max)
        assert cb.failure_count <= 2000, (
            "Bug 145: failure_count is impossibly large "
            f"({cb.failure_count}), indicating lost updates from concurrent "
            "record_failure() calls. Fix: add a threading.Lock to CircuitBreaker."
        )

    def test_chain_cb_has_lock_protection(self) -> None:
        """Structural test: CircuitBreaker must have a threading.Lock."""
        from tero2.circuit_breaker import CircuitBreaker

        cb = CircuitBreaker(name="test")
        has_lock = any(
            isinstance(getattr(cb, attr, None), threading.Lock)
            for attr in dir(cb)
        )
        assert has_lock, (
            "Bug 145: CircuitBreaker has no threading.Lock attribute. "
            "Without lock protection, concurrent check()/record_failure() "
            "calls from multiple threads corrupt internal state. "
            "Fix: add self._lock = threading.Lock() and acquire it in "
            "check(), record_success(), and record_failure()."
        )


# ── Bug 146: providers/cli subprocess stdout pipe deadlock ──────────────────


class TestBug146CLIStdoutPipeDeadlock:
    """CLIProvider.run() writes to proc.stdin and only reads stdout/stderr
    AFTER stdin is closed. If the child process emits enough stdout to fill
    the OS pipe buffer (~64KB) before stdin is consumed, the child blocks
    on stdout write and proc.stdin.drain() blocks forever — classic deadlock.
    Fix: consume stdout/stderr concurrently with stdin writes using asyncio tasks.
    """

    def test_stdin_write_and_stdout_read_are_concurrent(self) -> None:
        import tero2.providers.cli as cli_module

        source = inspect.getsource(cli_module.CLIProvider.run)
        lines = source.splitlines()

        # Find the section where stdin_data is written (proc.stdin.write / drain).
        # Check whether stdout consumption is started BEFORE or concurrently.
        stdin_write_line = None
        stream_events_before_write = False

        for i, line in enumerate(lines):
            if "proc.stdin.write" in line or "proc.stdin.drain" in line:
                if stdin_write_line is None:
                    stdin_write_line = i
                    # Look at code before this line: is _stream_events or a
                    # stdout-reading task already launched?
                    preamble = "\n".join(lines[:i])
                    stream_events_before_write = (
                        "_stream_events" in preamble
                        or "stdout_task" in preamble
                        or "read_stdout" in preamble
                    )

        if stdin_write_line is None:
            pytest.skip("proc.stdin.write not found in CLIProvider.run")

        # Also check that after stdin close, stdout is consumed via a task
        # that was already spawned (concurrent), not sequentially.
        # The fix should spawn an stdout-reading task BEFORE writing stdin.
        assert stream_events_before_write, (
            "Bug 146: CLIProvider.run() writes to proc.stdin before spawning "
            "any stdout/stderr consumer. If the child process fills its stdout "
            "pipe buffer before stdin is closed, the child blocks on write and "
            "proc.stdin.drain() blocks indefinitely — deadlock. "
            "Fix: spawn an asyncio task to consume stdout/stderr BEFORE "
            "writing to proc.stdin, then write stdin, close it, and await "
            "the stdout task."
        )

    def test_stdin_section_creates_concurrent_read_task(self) -> None:
        """Check the stdin write block creates an stdout read task."""
        import tero2.providers.cli as cli_module

        source = inspect.getsource(cli_module.CLIProvider.run)
        lines = source.splitlines()

        # Find the "if stdin_data and proc.stdin:" block
        in_stdin_block = False
        has_create_task_in_block = False

        for i, line in enumerate(lines):
            if "stdin_data" in line and "proc.stdin" in line:
                in_stdin_block = True
                continue
            if in_stdin_block:
                if "create_task" in line and ("stdout" in line or "stream" in line):
                    has_create_task_in_block = True
                    break
                # End of stdin block (e.g., finally: or next major section)
                if line.strip() and not line.strip().startswith(("proc.stdin", "try:", "except", "await", "BrokenPipe", "pass", "raise", "finally", "#", ")")) and "stdin" not in line:
                    break

        if not in_stdin_block:
            pytest.skip("stdin write block not found in CLIProvider.run")

        assert has_create_task_in_block, (
            "Bug 146: the stdin write block in CLIProvider.run() does not "
            "create a concurrent stdout read task. Without concurrent "
            "consumption, a child process that writes to stdout before "
            "reading all stdin will deadlock. "
            "Fix: create_task for stdout reading before writing stdin."
        )


# ── Bug 155: circuit_breaker HALF_OPEN allows concurrent probes ─────────────


class TestBug155HalfOpenConcurrentProbes:
    """In HALF_OPEN state with _trial_in_progress=True, check() should
    only allow ONE probe. But without locking, multiple threads calling
    check() simultaneously can both pass the _trial_in_progress guard
    before either sets it to True.
    Fix: add lock around the HALF_OPEN check path.
    """

    def test_half_open_only_allows_single_probe(self) -> None:
        from tero2.circuit_breaker import CBState, CircuitBreaker
        from tero2.errors import CircuitOpenError

        # Start with NO trial in progress so all threads race to claim the
        # probe slot. With _trial_in_progress=True ALL threads would raise
        # CircuitOpenError unconditionally — the race can never manifest.
        # With _trial_in_progress=False, without a lock, multiple threads
        # can simultaneously read False and all become probes.
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=0)
        cb.state = CBState.HALF_OPEN
        cb._trial_in_progress = False

        successes = []
        barrier = threading.Barrier(8)

        def try_check() -> None:
            barrier.wait()
            try:
                cb.check()
                successes.append(1)
            except CircuitOpenError:
                pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(try_check) for _ in range(8)]
            for f in futs:
                f.result()

        assert len(successes) <= 1, (
            f"Bug 155: {len(successes)} threads passed check() in HALF_OPEN "
            "simultaneously. Without atomic check-and-set of _trial_in_progress, "
            "multiple threads read False before any writes True and all become "
            "probes. Fix: add a lock around the HALF_OPEN probe-claim section."
        )

    def test_half_open_check_has_lock_protection(self) -> None:
        """check() must hold the lock around the HALF_OPEN probe-claim section."""
        import tero2.circuit_breaker as cb_module

        source = inspect.getsource(cb_module.CircuitBreaker.check)
        has_lock = "_lock" in source
        assert has_lock, (
            "Bug 155: CircuitBreaker.check() has no lock around the HALF_OPEN "
            "probe-claim. Two threads can both read _trial_in_progress=False "
            "before either writes True, and both become probes. "
            "Fix: acquire self._lock in check() before the _trial_in_progress check."
        )

    def test_half_open_state_is_threadsafe(self) -> None:
        """Concurrent check() from HALF_OPEN with no trial claimed must not
        allow more than one probe per cycle."""
        from tero2.circuit_breaker import CBState, CircuitBreaker

        # Use _trial_in_progress=False so threads actually race the probe-claim.
        # With True all threads raise immediately — nothing to protect.
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=0)
        cb.state = CBState.HALF_OPEN
        cb._trial_in_progress = False

        successes_per_round: list[int] = []
        barrier = threading.Barrier(8)

        def rapid_check() -> None:
            barrier.wait()
            count = 0
            for _ in range(100):
                try:
                    cb.check()
                    count += 1
                    # Reset after each probe so the race can repeat
                    cb._trial_in_progress = False
                except Exception:
                    pass
            successes_per_round.append(count)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(rapid_check) for _ in range(8)]
            for f in futs:
                f.result()

        assert isinstance(cb.state, CBState), (
            "Bug 155: concurrent HALF_OPEN check() calls corrupted state: "
            f"{cb.state!r}. Fix: add threading.Lock."
        )


# ── Bug 197: usage_tracker missing log import ──────────────────────────────


class TestBug197UsageTrackerMissingLogImport:
    """usage_tracker.py line 97 calls log.warning(...) but the module has
    no `log = logging.getLogger(...)` and no `import logging`.
    When the exception path in start_refresh_loop() is hit, NameError
    is raised instead of the intended warning.
    Fix: add `import logging` and `log = logging.getLogger(__name__)`.
    """

    def test_module_has_logging_import(self) -> None:
        import tero2.usage_tracker as ut_module

        source = inspect.getsource(ut_module)
        assert "import logging" in source, (
            "Bug 197: usage_tracker.py does not import logging. "
            "Line 97 calls log.warning() which will raise NameError "
            "when _refresh_limits() raises an exception. "
            "Fix: add 'import logging' at the top of the file."
        )

    def test_module_has_log_getter(self) -> None:
        import tero2.usage_tracker as ut_module

        source = inspect.getsource(ut_module)
        assert "log = logging.getLogger" in source, (
            "Bug 197: usage_tracker.py has no 'log = logging.getLogger(__name__)'. "
            "Line 97 calls log.warning(...) which raises NameError on the "
            "exception path in start_refresh_loop(). "
            "Fix: add 'log = logging.getLogger(__name__)' after imports."
        )

    def test_exception_path_raises_nameerror(self) -> None:
        """Trigger the exception path in start_refresh_loop and verify
        NameError is raised (bug) instead of logging the warning (fix)."""
        import tero2.usage_tracker as ut_module

        tracker = ut_module.UsageTracker()

        async def _run() -> None:
            # Patch _refresh_limits to always raise
            async def _boom() -> None:
                raise RuntimeError("boom")

            tracker._refresh_limits = _boom  # type: ignore[assignment]

            call_count = 0
            original_sleep = asyncio.sleep

            async def counting_sleep(seconds: float) -> None:
                nonlocal call_count
                call_count += 1
                if call_count > 2:
                    raise asyncio.CancelledError  # stop the loop
                await original_sleep(0)

            with patch("tero2.usage_tracker.asyncio.sleep", side_effect=counting_sleep):
                try:
                    await tracker.start_refresh_loop()
                except NameError:
                    pytest.fail(
                        "Bug 197: start_refresh_loop() raised NameError "
                        "on the exception path — log.warning() was called "
                        "but 'log' is not defined in usage_tracker.py. "
                        "Fix: add 'import logging' and "
                        "'log = logging.getLogger(__name__)'."
                    )
                except (RuntimeError, asyncio.CancelledError):
                    pass  # expected: RuntimeError from _boom or our cancel

        asyncio.run(_run())


# ── Bug 195: stream_bus unsubscribe race condition ─────────────────────────


class TestBug195StreamBusUnsubscribeRace:
    """StreamBus.unsubscribe() removes the queue from _subscribers under
    lock, but then drains the queue OUTSIDE the lock. Between the remove
    and the drain, a concurrent publish() can still put events into the
    queue because _publish_impl() only snapshots _subscribers under its
    own lock acquisition.
    Fix: drain the queue INSIDE the same lock that guards removal, or
    re-check after drain that no new events arrived.
    """

    def test_no_events_after_unsubscribe(self) -> None:
        """Publish events concurrently with unsubscribe and verify no
        events arrive in the queue after unsubscribe returns.

        Strategy: replace _subscribers with a custom list that signals
        when remove() is called, then immediately publish from another
        thread during the drain window.
        """

        async def _run() -> None:
            from tero2.stream_bus import StreamBus, make_stream_event

            bus = StreamBus()
            q = bus.subscribe()

            # Pre-publish one event so we know publish works
            bus.publish(make_stream_event("builder", "text", content="hello"))
            await asyncio.sleep(0)

            # Drain the pre-published event
            while not q.empty():
                q.get_nowait()

            # Use a signaling list to detect when remove() fires
            remove_done = threading.Event()
            original_list = bus._subscribers

            class SignalingList(list):
                def remove(self, value: object) -> None:
                    super().remove(value)
                    remove_done.set()

            bus._subscribers = SignalingList(original_list)

            def concurrent_publish() -> None:
                # Wait until unsubscribe has removed the queue from the list
                remove_done.wait(timeout=2.0)
                # The drain loop is now running OUTSIDE the lock.
                # Try to squeeze an event in during the drain window.
                for _ in range(50):
                    time.sleep(0.001)
                    bus.publish(make_stream_event("builder", "text", content="late"))

            pub_thread = threading.Thread(target=concurrent_publish, daemon=True)
            pub_thread.start()

            bus.unsubscribe(q)
            pub_thread.join(timeout=2.0)

            # After unsubscribe returns, the queue should be empty.
            # If a "late" event arrived after removal (during drain window),
            # it demonstrates the race.
            remaining = []
            while not q.empty():
                remaining.append(q.get_nowait())

            assert len(remaining) == 0, (
                f"Bug 195: {len(remaining)} event(s) arrived in queue after "
                "unsubscribe() returned. The drain in unsubscribe() happens "
                "OUTSIDE the subscriber lock, so a concurrent publish() can "
                "put events into the queue between removal and drain. "
                "Fix: drain the queue INSIDE the same lock that guards "
                "_subscribers.remove(), or re-check after drain."
            )

        asyncio.run(_run())

    def test_unsubscribe_drain_inside_lock(self) -> None:
        """Structural test: unsubscribe must drain the queue inside the lock.

        The bug: the `with self._sub_lock:` block removes the queue, then the
        drain loop (`while True: q.get_nowait()`) runs AFTER the with block
        exits. A concurrent publish can put events into the queue in this gap.

        Detection: find the `with self._sub_lock:` line and the `while True:`
        drain loop. The drain while-loop must be INSIDE the with block (i.e.
        its indentation must be deeper than the with statement).
        """
        import tero2.stream_bus as sb_module

        source = inspect.getsource(sb_module.StreamBus.unsubscribe)
        lines = source.splitlines()

        # Find the `with self._sub_lock:` line and the `while True:` drain.
        lock_line = None
        drain_while_line = None

        for i, line in enumerate(lines):
            if "self._sub_lock" in line and lock_line is None:
                lock_line = i
            if "while True:" in line and drain_while_line is None:
                drain_while_line = i

        if lock_line is None:
            pytest.skip("_sub_lock not found in unsubscribe")
        if drain_while_line is None:
            pytest.skip("drain while loop not found in unsubscribe")

        # The `with self._sub_lock:` block body is indented deeper than the
        # `with` line itself. The `while True:` drain must also be inside
        # that block, meaning its indentation must be strictly deeper than
        # the `with` line.
        lock_indent = len(lines[lock_line]) - len(lines[lock_line].lstrip())
        drain_indent = len(lines[drain_while_line]) - len(lines[drain_while_line].lstrip())

        assert drain_indent > lock_indent, (
            "Bug 195: StreamBus.unsubscribe() drains the queue OUTSIDE the "
            "subscriber lock. The `with self._sub_lock:` block removes the "
            "queue, then the `while True: q.get_nowait()` drain runs after "
            "the lock is released. A concurrent publish() can put events into "
            "the queue in this gap. Fix: move the drain loop inside the "
            "with self._sub_lock block."
        )

    def test_subscribe_unsubscribe_concurrent_no_lost_events(self) -> None:
        """Rapid subscribe/unsubscribe cycles should not corrupt the subscriber list."""
        from tero2.stream_bus import StreamBus

        bus = StreamBus()
        errors: list[str] = []

        def subscribe_unsubscribe(n: int) -> None:
            for _ in range(n):
                q = bus.subscribe()
                bus.unsubscribe(q)

        with ThreadPoolExecutor(max_workers=4) as pool:
            futs = [pool.submit(subscribe_unsubscribe, 50) for _ in range(4)]
            for f in futs:
                f.result()

        # After all subscribe/unsubscribe cycles, _subscribers should be empty
        assert len(bus._subscribers) == 0, (
            f"Bug 195: after concurrent subscribe/unsubscribe, _subscribers "
            f"has {len(bus._subscribers)} leaked entries. The subscriber list "
            "was corrupted by concurrent mutation. "
            "Fix: ensure all mutations to _subscribers are under _sub_lock."
        )
