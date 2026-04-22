"""Bug 118: ``UsageTracker.record_step`` mutates ``_total_tokens`` and
``_total_cost`` outside any lock.

The relevant lines in ``tero2/usage_tracker.py``::

    def record_step(self, provider, tokens, cost, is_estimated):
        self._total_tokens += tokens         # unguarded
        self._total_cost += cost             # unguarded

        with self._providers_lock:
            ... _providers dict mutations ...

The inline comment in the current file claims "Thread-safe via GIL for
simple int/float arithmetic", but that is wrong. ``x += y`` compiles to
``LOAD_ATTR, LOAD_FAST, BINARY_ADD, STORE_ATTR`` — four bytecodes, and
the CPython interpreter can switch threads between any of them. Two
concurrent ``record_step`` calls can each read the same ``_total_tokens``,
both compute ``x+new``, and both store back — a classic lost-update race.

This is not hypothetical: the usage tracker is called from multiple
places (the Runner's heartbeat loop, the TUI's refresh, per-step
tracking from providers). The whole point of ``_providers_lock`` is
mutual exclusion for record_step; scalars should be inside the same
critical section.

Fix: move the scalar increments inside the existing ``_providers_lock``.

Tests are split into:
  1. A structural guard that inspects the source and asserts the scalar
     increments are *textually inside* the lock block. Structural because
     an observable race-condition test with Python's GIL is flaky in
     isolation; the structural assertion pins the race-free invariant.
  2. A deterministic concurrent test that drives many threads through
     record_step and asserts the final totals match what we put in.
     On broken code this is flaky (passes most of the time); on the fix
     it is guaranteed. Keep it anyway — it catches any regression that
     would re-introduce the race in a stronger form.

Test-first per feedback_tdd_order.md.
"""

from __future__ import annotations

import inspect
import threading

import pytest

from tero2.usage_tracker import UsageTracker


class TestStructuralLockCoversScalars:
    """Static guard: the scalar increments must be inside ``_providers_lock``."""

    def test_total_tokens_increment_inside_lock_block(self) -> None:
        source = inspect.getsource(UsageTracker.record_step)
        lock_idx = source.find("with self._providers_lock")
        assert lock_idx != -1, (
            "record_step should use self._providers_lock — invariant broken"
        )
        total_tokens_idx = source.find("self._total_tokens +=")
        assert total_tokens_idx != -1, "scalar increment must still exist"
        assert total_tokens_idx > lock_idx, (
            "bug 118: self._total_tokens += tokens is outside the "
            "_providers_lock critical section. Move it inside so the "
            "scalar totals get the same mutual exclusion as the per-"
            "provider dict."
        )

    def test_total_cost_increment_inside_lock_block(self) -> None:
        source = inspect.getsource(UsageTracker.record_step)
        lock_idx = source.find("with self._providers_lock")
        cost_idx = source.find("self._total_cost +=")
        assert cost_idx > lock_idx, (
            "bug 118: self._total_cost += cost is outside the lock — "
            "same fix as _total_tokens"
        )


class TestConcurrentRecordStepPreservesTotals:
    """Deterministic behavioural guard: 10k increments across 10 threads
    must land 10k of each in the final totals.

    On the un-fixed code this is flaky-red (sometimes misses a few updates
    to the lost-update race). On the fix it is fully deterministic."""

    def test_totals_match_sum_of_inputs(self) -> None:
        tracker = UsageTracker()
        per_thread = 1000
        threads_n = 10
        token_per_call = 7
        cost_per_call = 0.013

        def worker() -> None:
            for _ in range(per_thread):
                tracker.record_step(
                    provider="p",
                    tokens=token_per_call,
                    cost=cost_per_call,
                    is_estimated=False,
                )

        threads = [threading.Thread(target=worker) for _ in range(threads_n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        expected_tokens = per_thread * threads_n * token_per_call
        expected_cost = per_thread * threads_n * cost_per_call

        summary = tracker.session_summary()
        assert summary["total_tokens"] == expected_tokens, (
            f"lost updates: got {summary['total_tokens']}, expected "
            f"{expected_tokens}. {per_thread * threads_n} record_step calls "
            "interleaved across threads dropped increments on the un-locked "
            "scalar += path."
        )
        # Float sum can have tiny rounding. Use isclose with tight tolerance
        # proportional to the expected magnitude.
        assert abs(summary["total_cost"] - expected_cost) < 1e-6 * expected_cost, (
            f"lost updates on _total_cost: got {summary['total_cost']}, "
            f"expected {expected_cost}"
        )
