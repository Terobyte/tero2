"""Bug 122: ``UsageTracker.session_summary()`` iterates ``self._providers``
outside the ``_providers_lock``. The TUI refresh path calls this from the
asyncio event loop, while agent threads call ``record_step`` (which now
correctly mutates under the lock, per bug 118). A concurrent insertion of
a new provider key while ``session_summary`` is iterating ``.items()`` can
raise ``RuntimeError: dictionary changed size during iteration``.

Bug 118 closed the write-side race on the scalar totals. This one closes
the read-side race on the provider dict — the read path must hold the
same lock that protects the write path.

Test strategy:
- Structural: inspect the source of ``session_summary`` and assert it
  enters ``_providers_lock`` (deterministic, no flakiness).
- Behavioural: race two threads — one iterating ``session_summary`` in
  a tight loop, one registering fresh provider keys via ``record_step``
  — and assert no exception surfaces across many iterations. On broken
  code this occasionally raises ``RuntimeError``; on fixed code it never
  does.
"""

from __future__ import annotations

import inspect
import threading
import time


def test_session_summary_enters_providers_lock_source():
    """Structural guard: session_summary must acquire the same lock that
    protects record_step. Protects the contract even if the behavioural
    test misses the race window on a lucky scheduling."""
    from tero2 import usage_tracker

    src = inspect.getsource(usage_tracker.UsageTracker.session_summary)
    assert "_providers_lock" in src, (
        "bug 122: session_summary must acquire _providers_lock before "
        "iterating _providers — the TUI reads this concurrently with "
        f"record_step. Source:\n{src}"
    )


def test_session_summary_concurrent_with_record_step():
    """Behavioural guard: race summary-reads against record_step-writes
    that insert fresh provider keys. On broken code this occasionally
    raises RuntimeError. On fixed code it stays clean across 2000+
    iterations."""
    from tero2.usage_tracker import UsageTracker

    tracker = UsageTracker()
    # Seed a few providers so reads have something to iterate.
    for i in range(3):
        tracker.record_step(f"p{i}", tokens=1, cost=0.1, is_estimated=False)

    stop_event = threading.Event()
    errors: list[BaseException] = []

    def reader():
        try:
            # Iterate many summary snapshots in a tight loop.
            while not stop_event.is_set():
                summary = tracker.session_summary()
                # Touch every provider to force the nested dict copy
                # (which is where "dictionary changed size during
                # iteration" surfaces on the broken path).
                for _k, v in summary["providers"].items():
                    _ = v.get("tokens")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    def writer():
        try:
            # Continuously register NEW provider keys — each record_step
            # with a fresh provider name grows self._providers.
            i = 100
            while not stop_event.is_set():
                tracker.record_step(
                    f"racing_{i}", tokens=1, cost=0.01, is_estimated=False
                )
                i += 1
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=reader), threading.Thread(target=writer)]
    for t in threads:
        t.start()
    time.sleep(0.5)  # let them race
    stop_event.set()
    for t in threads:
        t.join(timeout=5)

    assert not errors, (
        "bug 122: concurrent reader/writer raced on _providers dict — "
        f"got errors: {errors!r}"
    )


def test_session_summary_totals_still_correct():
    """Regression guard: acquiring the lock in session_summary must not
    change the returned data shape."""
    from tero2.usage_tracker import UsageTracker

    tracker = UsageTracker()
    tracker.record_step("claude", tokens=100, cost=0.5, is_estimated=False)
    tracker.record_step("gpt4", tokens=200, cost=1.0, is_estimated=True)

    summary = tracker.session_summary()
    assert summary["total_tokens"] == 300
    assert summary["total_cost"] == 1.5
    assert set(summary["providers"].keys()) == {"claude", "gpt4"}
    assert summary["providers"]["gpt4"]["is_estimated"] is True
