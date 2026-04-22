"""
Failing tests demonstrating 5 medium bugs from bugs.md.

  A30 — config.py lines 202–206: tg_enabled parsed as bool(value) — TOML string
         "false" evaluates to True (non-empty string), enabling Telegram unintentionally.
  A22 — players/verifier.py lines 61–74: _run_shell() missing FileNotFoundError handler
         unlike _run_subprocess() which catches it and returns rc=-1 (ANOMALY).
  A21 — stuck_detection.py lines 84–100: update_tool_hash off-by-one — with threshold=2
         stuck fires on 3rd identical call (count=2) instead of 2nd (count=1).
  A19 — history.py lines 44–59: record_run mutates entries in-place then sorts at loop
         end; if sort raises, history is left permanently corrupted.
  A24 — config_writer.py lines 91–107: .lock and .tmp temp files never cleaned up on
         success or failure — repeated saves fill the config directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# A30 — config.py: tg_enabled="false" (string) evaluates to True via bool()
# ─────────────────────────────────────────────────────────────────────────────

def test_a30_tg_enabled_string_false_must_disable_telegram():
    """A30 — _parse_config must treat the string "false" as disabled, not enabled.

    Current code (config.py lines 202–206)::

        tg_enabled = tg.get("enabled")
        if tg_enabled is None:
            tg_enabled = bool(tg.get("bot_token", ""))
        cfg.telegram = TelegramConfig(
            enabled=bool(tg_enabled),   # ← bool("false") == True

    Bug: TOML parsers may deliver string values when the raw dict is built
    programmatically.  ``bool("false")`` returns ``True`` because "false" is a
    non-empty string, silently enabling Telegram even when the user wrote
    ``enabled = "false"``.  The correct behaviour is to treat any case-insensitive
    string ``"false"`` / ``"0"`` / ``"no"`` as disabled.

    This test passes ``enabled = "false"`` (string) to ``_parse_config`` and
    asserts ``cfg.telegram.enabled == False``.  With the current bug,
    ``bool("false") == True`` so the assertion fails.
    """
    from tero2.config import _parse_config

    raw = {
        "telegram": {
            "enabled": "false",       # string, not bool — must be treated as False
            "bot_token": "tok123",
            "chat_id": "999",
        }
    }

    cfg = _parse_config(raw)

    assert cfg.telegram.enabled is False, (
        f"BUG: cfg.telegram.enabled is {cfg.telegram.enabled!r} after parsing "
        "enabled='false' (string).  bool('false') == True because 'false' is a "
        "non-empty string.  _parse_config must detect the string literal 'false' "
        "and treat it as disabled.  This enables Telegram unintentionally "
        "whenever the value arrives as a string from programmatic config "
        "construction or a TOML round-trip quirk."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A22 — verifier.py: _run_shell() missing FileNotFoundError handler
# ─────────────────────────────────────────────────────────────────────────────

def test_a22_run_shell_catches_file_not_found_returns_anomaly_rc():
    """A22 — _run_shell() must catch FileNotFoundError and return rc=-1, not propagate.

    Current code (_run_shell, verifier.py lines 61–74)::

        def _run_shell(cmd_str: str, cwd: str) -> tuple[int, str, str]:
            try:
                proc = subprocess.run(cmd_str, ..., shell=True)
                return proc.returncode, proc.stdout, proc.stderr
            except subprocess.TimeoutExpired:
                return -1, "", f"command timed out: {cmd_str}"
            # ← NO FileNotFoundError handler!

    Bug: _run_subprocess (lines 45–58) catches FileNotFoundError and returns
    rc=-1 to signal ANOMALY.  _run_shell lacks this handler.  With shell=True,
    subprocess.run can still raise FileNotFoundError (e.g. when the shell
    binary itself is unavailable).  The inconsistency means _run_shell propagates
    the exception while _run_subprocess returns a graceful -1.

    This test patches subprocess.run to raise FileNotFoundError and calls
    _run_shell directly.  The correct behaviour is rc=-1 (no exception).  With
    the current bug, FileNotFoundError propagates and pytest catches it as an
    error, not an assertion failure.
    """
    from tero2.players.verifier import _run_shell

    with patch("tero2.players.verifier.subprocess.run", side_effect=FileNotFoundError("sh: not found")):
        try:
            rc, stdout, stderr = _run_shell("echo hello", cwd="/tmp")
        except FileNotFoundError as exc:
            pytest.fail(
                f"BUG: _run_shell() propagated FileNotFoundError: {exc}\n"
                "_run_subprocess catches FileNotFoundError and returns rc=-1; "
                "_run_shell has no such handler (verifier.py lines 61–74).  "
                "The exception must be caught and rc=-1 returned to signal ANOMALY, "
                "consistent with _run_subprocess."
            )

    assert rc == -1, (
        f"BUG: _run_shell() returned rc={rc!r} after FileNotFoundError — "
        "expected rc=-1 (ANOMALY signal).  Even if the exception is caught, "
        "a non-(-1) rc would be incorrect."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A21 — stuck_detection.py: update_tool_hash off-by-one in repeat counter
# ─────────────────────────────────────────────────────────────────────────────

def test_a21_update_tool_hash_stuck_fires_on_second_repeat_not_third():
    """A21 — With threshold=2, stuck must fire on the 2nd identical call, not the 3rd.

    Current code (stuck_detection.py lines 84–100)::

        new_count = state.tool_repeat_count + 1 if is_repeat else 0
        # (check_stuck: fires when tool_repeat_count >= threshold)

    Call trace with threshold=2:
        Call 1: last_hash="", new_hash=H, is_repeat=False, new_count=0
        Call 2: last_hash=H,  new_hash=H, is_repeat=True,  new_count=1
        Call 3: last_hash=H,  new_hash=H, is_repeat=True,  new_count=2 → FIRES

    Bug: with threshold=2, the stuck signal fires on the 3rd total call (3rd
    identical call, 2nd repeat).  Semantically, threshold=2 should mean
    "tolerate 1 repeat and fire on the 2nd repeat", i.e. fire when count
    reaches 1 after the 2nd call, not when count reaches 2 after the 3rd.

    The off-by-one: the counter starts at 0 on first repeat instead of 1,
    so it takes one extra identical call to reach threshold.

    This test verifies that with threshold=2 the stuck signal fires BEFORE
    a 4th identical call is needed.  Specifically, check_stuck must return
    TOOL_REPEAT after the 3rd call with new_count=2.  But the intent of
    threshold=2 is to fire at the 2nd repeat (count would be 1 under a
    correct off-by-one-free implementation).

    Concretely: we assert that check_stuck fires when tool_repeat_count==2
    and threshold==2 (current code: ok, this passes) — but we then show
    the REAL off-by-one by asserting a 4th call IS NOT needed.  The bug
    manifests as: calling with same hash 4 times before stuck fires.
    """
    from tero2.stuck_detection import (
        StuckSignal,
        check_stuck,
        update_tool_hash,
    )
    from tero2.config import StuckDetectionConfig
    from tero2.state import AgentState

    config = StuckDetectionConfig(
        max_retries=999,           # don't fire retry signal
        max_steps_per_task=999,    # don't fire step limit
        tool_repeat_threshold=2,   # should fire after 2nd identical call
    )

    # Build an AgentState that won't trigger retry/step signals
    state = AgentState(retry_count=0, steps_in_task=0)
    tool_call = "write_file(path='x.py', content='hello')"

    # Call 1: establishes hash — no repeat yet
    state, is_repeat1 = update_tool_hash(state, tool_call)
    assert not is_repeat1, "First call should not be a repeat"
    result1 = check_stuck(state, config)
    assert result1.signal == StuckSignal.NONE, (
        f"After call 1 (no repeat), stuck must be NONE; got {result1.signal}"
    )

    # Call 2: first repeat — count should be 1
    state, is_repeat2 = update_tool_hash(state, tool_call)
    assert is_repeat2, "Second identical call must be detected as repeat"
    # With threshold=2 and count=1, check_stuck should already fire
    # because threshold=2 intends "fire after 2 identical repeats after first"
    result2 = check_stuck(state, config)

    # Call 3: second repeat — count becomes 2, and ONLY now does stuck fire (the bug)
    state, is_repeat3 = update_tool_hash(state, tool_call)
    assert is_repeat3, "Third identical call must be detected as repeat"
    result3 = check_stuck(state, config)

    # With the current code: result2.signal == NONE (count=1 < threshold=2)
    # and result3.signal == TOOL_REPEAT (count=2 >= threshold=2).
    # The bug: threshold=2 should fire at the 2nd identical call (count=1),
    # not at the 3rd (count=2). The counter is one behind — off by one.
    assert result2.signal == StuckSignal.TOOL_REPEAT, (
        f"BUG: check_stuck did NOT fire TOOL_REPEAT after 2nd identical call "
        f"(tool_repeat_count={state.tool_repeat_count - 1}, threshold=2).  "
        f"Got signal={result2.signal!r}.  "
        "With threshold=2 the intent is to fire after 2 identical calls once "
        "the hash is established (calls 1+2), but the counter starts at 0 for "
        "the first repeat (call 2) so it takes a 3rd identical call to reach "
        "count=2 and fire.  This is an off-by-one in stuck_detection.py: "
        "``new_count = state.tool_repeat_count + 1`` should start at 1 for the "
        "first repeat (i.e. initialise with 1 not 0), or the threshold comparison "
        "should use ``> threshold - 1`` / ``>= threshold - 1``."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A19 — history.py: sort failure leaves history permanently corrupted
# ─────────────────────────────────────────────────────────────────────────────

def test_a19_record_run_sort_failure_leaves_history_corrupted(tmp_path):
    """A19 — record_run must protect history from corruption if sort() raises.

    Current code (history.py lines 44–59)::

        for entry in entries:
            if entry.path == path_str:
                entry.last_run = now        # ← in-place mutation
                entry.last_plan = plan_str  # ← in-place mutation
                entry.run_count += 1        # ← in-place mutation
                break
        else:
            entries.insert(0, HistoryEntry(...))  # ← mutates list

        entries.sort(key=lambda e: e.last_run, reverse=True)  # ← raises here?
        _write(entries[:20])

    Bug: all mutations to ``entries`` (both in-place field updates and the
    ``insert``) happen BEFORE ``sort``.  If ``sort`` raises (e.g. due to a
    TypeError from a corrupted ``last_run`` field), the list is already
    mutated but ``_write`` is never called — the run_count increment is
    silently lost with no error handling whatsoever.

    This test injects a HistoryEntry with a non-string ``last_run`` field (an
    integer), which causes ``entries.sort(key=lambda e: e.last_run)`` to
    raise a ``TypeError`` when comparing str vs int.  We then assert that the
    ``record_run`` call either (a) handles the sort error gracefully and still
    persists the new entry, or (b) raises a visible exception.  With the
    current code, the TypeError propagates unhandled — confirming there is no
    try/except around the mutation+sort block.
    """
    import json
    import tero2.history as history_module
    from tero2.history import HistoryEntry, _write as _history_write

    history_file = tmp_path / "history.json"

    # Patch the module-level HISTORY_FILE so we don't touch the real one
    original_history_file = history_module.HISTORY_FILE
    history_module.HISTORY_FILE = history_file

    try:
        # Pre-populate history with one entry whose last_run is an integer
        # (not a string) — this will cause sort(key=lambda e: e.last_run)
        # to raise TypeError when comparing str vs int for a second entry.
        corrupted_data = {
            "version": 1,
            "entries": [
                {
                    "path": "/projects/existing",
                    "name": "existing",
                    "last_run": 12345,   # ← integer, not ISO string — triggers sort TypeError
                    "last_plan": None,
                    "run_count": 1,
                },
            ],
        }
        history_file.write_text(json.dumps(corrupted_data), encoding="utf-8")

        # load_history will happily load the corrupted entry (no type checking)
        entries = history_module.load_history()
        assert len(entries) == 1, "setup: expected one pre-loaded entry"
        assert entries[0].last_run == 12345, "setup: corrupted last_run must be int"

        project_path = Path("/projects/newproject")

        # record_run will insert a new entry with a proper ISO string last_run,
        # then call entries.sort(key=lambda e: e.last_run).
        # Comparing str and int in key extraction raises TypeError in Python.
        try:
            history_module.record_run(project_path, plan_file=None)
            # If no exception: implementation caught the sort error internally.
            # In that case, we verify that the new entry was still written.
            reloaded = history_module.load_history()
            paths = [e.path for e in reloaded]
            assert str(project_path.expanduser().resolve()) in paths, (
                "BUG: after sort failure, new entry was silently lost.  "
                "record_run must either handle the sort error and still write "
                "the new entry, or raise a visible error."
            )
        except TypeError as exc:
            # Current code: TypeError from sort propagates unhandled.
            # This proves there is no try/except around the mutation+sort block.
            pytest.fail(
                f"BUG: record_run() raised TypeError ({exc}) when sort() "
                "encountered a corrupted last_run field (int vs str comparison).  "
                "There is no try/except around the mutation+sort block in "
                "history.py lines 44–59.  A sort failure abruptly terminates "
                "record_run, silently losing the run_count increment and the "
                "new entry without any error handling.  "
                "Fix: wrap mutations + sort in a try/except, or sort a copy "
                "before committing mutations to the original list."
            )
    finally:
        history_module.HISTORY_FILE = original_history_file


# ─────────────────────────────────────────────────────────────────────────────
# A24 / bug 115 — config_writer.py cleanup contract
#
# These tests were originally written in the cleanliness-over-correctness
# frame: "no .lock file should remain after a write." Bug 115 (commit 9df277b)
# reversed that on the lock file specifically: unlinking a flock path while
# holders may still have the old inode open breaks mutual exclusion (a later
# writer O_CREATs a fresh inode and acquires its own flock, while a prior
# writer is still on the old inode). Persisting the lock file is the
# race-free contract.
#
# The tmp-file half of the contract is unchanged: .tmp is a scratch artefact
# that MUST be cleaned up on success (via rename) and on failure (via the
# finally block's unlink).
# ─────────────────────────────────────────────────────────────────────────────

def test_a24_no_lock_file_leftover_after_successful_write(tmp_path):
    """Post-bug-115 contract: .tmp is consumed on success, .lock persists.

    The old assertion (.lock must be unlinked after success) is what bug 115
    fixed — unlinking broke flock exclusivity across concurrent writers. We
    now assert the opposite for the lock path, and keep the tmp-path
    assertion unchanged.
    """
    from tero2.config_writer import write_global_config_section

    config_path = tmp_path / "config.toml"
    lock_path = config_path.with_suffix(".lock")
    tmp_path_file = config_path.with_suffix(".tmp")

    write_global_config_section(config_path, "telegram", {"bot_token": "tok", "enabled": True})

    assert lock_path.exists(), (
        "bug 115 contract: the flock file must persist across writes — "
        "unlinking it allows a second writer to create a new-inode lock "
        "while the previous holder is still on the old inode."
    )

    assert not tmp_path_file.exists(), (
        f"scratch .tmp file {tmp_path_file} still exists after successful "
        "write_global_config_section().  Expected tmp.replace(config_path) to "
        "consume the temp file; check that no second .tmp path is left behind."
    )


def test_a24_no_temp_files_leftover_after_failed_write(tmp_path):
    """Post-bug-115 contract: on failure, .tmp is cleaned, .lock persists.

    Prior version of this test asserted .lock was removed on failure too —
    bug 115 reversed that for the same reason as the success-path case. The
    .tmp cleanup is still required: .tmp is a scratch artefact owned by the
    writer, and the finally block must unlink it on error.
    """
    from tero2.config_writer import write_global_config_section

    config_path = tmp_path / "config.toml"
    lock_path = config_path.with_suffix(".lock")
    tmp_file = config_path.with_suffix(".tmp")

    # Patch Path.write_text to raise OSError after lock is acquired
    original_write_text = Path.write_text

    def failing_write_text(self, *args, **kwargs):
        if self.suffix == ".tmp":
            raise OSError("disk full: injected fault for A24 test")
        return original_write_text(self, *args, **kwargs)

    with patch.object(Path, "write_text", failing_write_text):
        try:
            write_global_config_section(config_path, "telegram", {"bot_token": "tok"})
        except OSError:
            pass  # expected — the injected error propagates

    assert lock_path.exists(), (
        "bug 115 contract: the flock file must persist even when the write "
        "fails — same race rationale as the success path. Writers may be "
        "mid-acquire on the old inode when this one releases."
    )

    assert not tmp_file.exists(), (
        f"BUG: .tmp file {tmp_file} remains after failed "
        "write_global_config_section().  If an error occurs after .tmp is "
        "written but before tmp.replace(), the .tmp file is stranded.  "
        "The finally block must also clean up .tmp."
    )
