"""
Failing tests demonstrating 6 critical bugs from bugs.md.

  A3  — architect.py lines 201–202: _recover_plan_from_disk inverted validation:
         `if validate_plan(content): continue` — when recovery returns a plan the
         caller clears errors without re-validating (lines 119–120), so any plan
         accepted by _recover_plan_from_disk is used unconditionally even if it is
         semantically wrong.
  A4  — scout.py: build_file_tree has no visited-dir guard; circular symlinks cause
         exponential entry multiplication, bounded only by max_depth.
  A5  — coach.py: for-else-break silently drops all slices after first cap-hit.
  A6  — builder.py: BuilderPlayer.run() calls the module-level
         _recover_summary_from_disk directly (line 86) instead of the static method
         BuilderPlayer._recover_summary_from_disk — the static method is dead code
         and its override cannot be relied upon.
  A9  — circuit_breaker.py: a HALF_OPEN trial with _trial_in_progress=True is
         never allowed a second probe even after another recovery_timeout elapses —
         the provider is permanently unavailable without explicit record_success /
         record_failure.
  A11 — config.py: max_slices / idle_timeout_s stored as str when provided as string.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.circuit_breaker import CBState, CircuitBreaker
from tero2.errors import CircuitOpenError


# ─────────────────────────────────────────────────────────────────────────────
# A3 — ArchitectPlayer: recovery clears errors without re-validation
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_architect_run_rejects_recovered_invalid_plan(tmp_path):
    """A3 — When _recover_plan_from_disk returns a plan, run() must re-validate it.

    Current code (architect.py lines 112–120)::

        recovered = self._recover_plan_from_disk(slice_id, milestone_path)
        if recovered is not None:
            log.warning(...)
            plan = recovered[1]
            errors = []          # ← clears errors WITHOUT re-validating!

    Bug: ``errors = []`` is set unconditionally after recovery — the recovered
    plan is never re-validated.  If ``_recover_plan_from_disk`` returns an
    invalid plan (e.g. by returning the file whose contents do not actually
    satisfy the validator), that plan is used as if it were valid.

    This test patches ``_recover_plan_from_disk`` to return a plan that
    FAILS ``validate_plan`` (no must-haves), then calls ``run()``.  With
    correct code, the recovered invalid plan should be rejected and
    ``ArchitectResult(success=False)`` returned.  With the current bug,
    ``errors = []`` clears the validation failure and ``run()`` returns
    ``success=True`` with the invalid plan.
    """
    from tero2.players.architect import ArchitectPlayer, validate_plan
    from tero2.providers.chain import ProviderChain
    from tero2.disk_layer import DiskLayer

    # LLM output: also invalid (no tasks at all)
    llm_output = "I have completed the task decomposition."
    assert validate_plan(llm_output) != [], (
        "Test setup: llm_output must fail validate_plan"
    )

    # The "recovered" plan also has a structural problem: T01 with no must-haves
    recovered_invalid_plan = (
        "## T01: Setup module\n\n"
        "Implement the core module.\n"
        "No acceptance criteria defined here.\n"
    )
    assert validate_plan(recovered_invalid_plan) != [], (
        f"Test setup: recovered_invalid_plan must fail validate_plan, "
        f"errors={validate_plan(recovered_invalid_plan)!r}"
    )

    chain = MagicMock(spec=ProviderChain)
    chain.run_prompt_collected = AsyncMock(return_value=llm_output)

    disk = MagicMock(spec=DiskLayer)
    disk.read_file.return_value = ""
    disk.write_file.return_value = None

    player = ArchitectPlayer(chain, disk, working_dir=str(tmp_path))

    # Patch _recover_plan_from_disk to return the invalid recovered plan
    with patch.object(
        player,
        "_recover_plan_from_disk",
        return_value=(str(tmp_path / "S01-PLAN.md"), recovered_invalid_plan),
    ):
        result = await player.run(slice_id="S01", milestone_path="milestones/M001")

    # CORRECT behaviour: recovered plan re-validated → still invalid → success=False
    # BUGGY behaviour:   errors = [] clears the failure → success=True (wrong!)
    assert not result.success, (
        f"BUG: ArchitectPlayer.run() returned success=True after recovery "
        f"produced an INVALID plan.  The code clears ``errors = []`` "
        f"(architect.py line 120) without re-calling validate_plan on the "
        f"recovered content.  An invalid recovered plan must still produce "
        f"ArchitectResult(success=False).\n"
        f"result={result!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# A4 — ScoutPlayer.build_file_tree: no visited-dir guard for circular symlinks
# ─────────────────────────────────────────────────────────────────────────────

def test_build_file_tree_visited_guard_prevents_exponential_expansion(tmp_path):
    """A4 — build_file_tree must handle circular symlinks with a visited-dir guard.

    Current code (_walk inside build_file_tree)::

        def _walk(path: str, prefix: str, depth: int) -> None:
            if depth > max_depth:
                return
            ...
            if is_dir:
                _walk(full, prefix + extension, depth + 1)

    Bug: there is no visited-directory set.  A circular symlink within the
    depth window causes exponential entry multiplication: at each depth level
    the symlink re-enters the root directory, expanding N subdirs × depth
    times.  With max_depth=6 and 3 subdirs each containing a loop back to
    root, the output is O(N^depth) entries.

    A correct implementation tracks visited real paths (via ``os.path.realpath``)
    and returns immediately on re-entry.  With a visited-dir guard the output
    for this tiny tree should be ≤ 15 lines regardless of depth.

    Without a guard, the current code produces ~79 lines for max_depth=6 —
    far exceeding the expected bounded output.
    """
    from tero2.players.scout import build_file_tree

    # Three subdirs, each containing a circular symlink back to root
    for i in range(3):
        d = tmp_path / f"dir{i}"
        d.mkdir()
        try:
            (d / "loop").symlink_to(tmp_path)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")

    output = build_file_tree(str(tmp_path), max_depth=6)
    line_count = len(output.splitlines())

    # With a visited-dir guard: O(1) bounded output (~4 dirs + root = ≤ 15 lines)
    # Without a guard: exponential (~79 lines for this case)
    assert line_count <= 15, (
        f"BUG: build_file_tree produced {line_count} lines for a tree with "
        "3 subdirs each containing a circular symlink back to root "
        "(max_depth=6).  Without a visited-directory guard, the function "
        "follows each symlink at every depth level, multiplying entries "
        "exponentially.  Expected ≤ 15 lines with a visited-dir guard; "
        f"got {line_count}.  This confirms the missing visited-dir guard (A4)."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A5 — CoachPlayer._gather_context: for-else-break drops slices after cap
# ─────────────────────────────────────────────────────────────────────────────

def test_gather_context_reads_all_slices_even_when_first_hits_cap(tmp_path):
    """A5 — _gather_context must read summaries from ALL slices, not stop at first.

    Current code (lines 121–133)::

        for sid in slice_dirs:
            for i in range(1, _MAX_TASKS + 1):
                ...
                if total_size + len(entry) > _SIZE_CAP:
                    break              # exits inner loop
                summaries.append(entry)
                ...
            else:
                continue              # inner loop completed normally → next sid
            break                     # inner break → outer break fires here

    Bug: the ``for-else-break`` pattern means whenever the inner loop
    exits via ``break`` (size cap reached on ANY task in ANY slice), the
    outer loop's ``else`` branch is NOT executed and the outer ``break``
    fires, abandoning ALL subsequent slices.

    This test sets up two slices where S01/T01 exceeds the 50,000-byte cap
    and S02/T01 has a small distinctive summary.  With correct code S02 is
    read (at least attempted up to the cap).  With the bug, S02 is silently
    dropped when S01's inner loop fires ``break``.
    """
    from tero2.players.coach import CoachPlayer
    from tero2.providers.chain import ProviderChain
    from tero2.disk_layer import DiskLayer

    # Build .sora milestone structure manually
    sora_dir = tmp_path / ".sora"
    milestone = sora_dir / "milestones" / "M001"
    s01 = milestone / "S01"
    s02 = milestone / "S02"
    s01.mkdir(parents=True)
    s02.mkdir(parents=True)

    # S01/T01 — exceeds _SIZE_CAP (50,000 bytes) alone
    (s01 / "T01-SUMMARY.md").write_text("x" * 60_000, encoding="utf-8")

    # S02/T01 — small distinctive content
    small_content = "S02 unique summary — must appear in gather_context output"
    (s02 / "T01-SUMMARY.md").write_text(small_content, encoding="utf-8")

    disk = DiskLayer(tmp_path)
    disk.init()

    def patched_read_file(rel_path: str) -> str:
        if "SUMMARY" in rel_path:
            full = disk.sora_dir / rel_path
            try:
                return full.read_text(encoding="utf-8")
            except OSError:
                return ""
        return ""

    disk.read_file = patched_read_file        # type: ignore[method-assign]
    disk.read_metrics = lambda: {}            # type: ignore[method-assign]
    disk.read_steer = lambda: ""              # type: ignore[method-assign]

    chain = MagicMock(spec=ProviderChain)
    player = CoachPlayer(chain, disk)

    context = player._gather_context("milestones/M001", "S01")
    task_summaries = context["task_summaries"]

    assert small_content in task_summaries, (
        f"BUG: _gather_context did not include S02/T01 summary after S01/T01 "
        f"exceeded the 50,000-byte size cap.  The for-else-break pattern causes "
        f"the outer slice loop to exit as soon as the inner task loop hits "
        f"``break``, silently dropping all subsequent slices (S02, S03, …).  "
        f"Fix: restructure the loop so each slice is processed independently.\n"
        f"task_summaries[:300] = {task_summaries[:300]!r}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# A6 — BuilderPlayer.run(): bypasses static method, calls module-level directly
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_builder_run_uses_static_method_for_disk_recovery(tmp_path):
    """A6 — BuilderPlayer.run() must call self._recover_summary_from_disk, not bypass it.

    Current code (builder.py line 86)::

        summary = _recover_summary_from_disk(task_id, self.working_dir)
        #          ^^^^^^^^^^^^^^^^^^^^^^^^^^^^ module-level function, NOT self._recover_*

    Bug: ``run()`` calls the module-level ``_recover_summary_from_disk``
    function directly instead of delegating to ``BuilderPlayer._recover_summary_from_disk``
    (the static method at lines 111–114).  The static method exists and
    re-exposes the module-level function, but since ``run()`` bypasses it,
    any subclass that overrides ``_recover_summary_from_disk`` to customise
    recovery behaviour will be silently ignored.

    More critically, bugs.md A6 documents that the static method itself
    has infinite-recursion risk (calling itself instead of the module-level
    function).  By bypassing the static method entirely, ``run()`` avoids
    that recursion only by accident — not by design.

    This test verifies that when the LLM returns an empty string (triggering
    the disk recovery path), ``BuilderPlayer._recover_summary_from_disk`` is
    the method actually called.  We patch the STATIC METHOD on the class and
    assert it is invoked.  If ``run()`` calls the module-level function
    directly, the patch is never reached and the test fails.
    """
    from tero2.players.builder import BuilderPlayer
    from tero2.providers.chain import ProviderChain
    from tero2.disk_layer import DiskLayer

    chain = MagicMock(spec=ProviderChain)
    # LLM returns empty → triggers _recover_summary_from_disk path
    chain.run_prompt_collected = AsyncMock(return_value="")

    disk = MagicMock(spec=DiskLayer)
    disk.write_file.return_value = None

    player = BuilderPlayer(chain, disk, working_dir=str(tmp_path))

    static_method_called: list[str] = []

    def mock_static_recover(task_id: str, working_dir: str) -> str:
        static_method_called.append(task_id)
        return "Summary content from patched static method."

    # Patch the STATIC METHOD on the class (not the module-level function)
    with patch.object(BuilderPlayer, "_recover_summary_from_disk", staticmethod(mock_static_recover)):
        await player.run(
            task_plan="Do the work.",
            task_id="T01",
            slice_id="S01",
            milestone_path="milestones/M001",
        )

    # BUG: run() calls the module-level _recover_summary_from_disk directly,
    # bypassing the static method.  The patch on the class is never invoked.
    assert static_method_called, (
        "BUG: BuilderPlayer.run() did not call BuilderPlayer._recover_summary_from_disk "
        "(the static method) when the LLM returned an empty string.  "
        "Instead it called the module-level _recover_summary_from_disk directly "
        "(builder.py line 86: `summary = _recover_summary_from_disk(task_id, self.working_dir)`).  "
        "This bypasses the class's own recovery hook, making the static method "
        "dead code and preventing subclasses from overriding disk recovery behaviour."
    )


# ─────────────────────────────────────────────────────────────────────────────
# A9 — CircuitBreaker: HALF_OPEN trial permanently blocks provider
# ─────────────────────────────────────────────────────────────────────────────

def test_half_open_allows_new_probe_after_second_recovery_timeout():
    """A9 — A new probe must be allowed when the recovery timeout elapses again
    while stuck in HALF_OPEN (trial abandoned without recording outcome).

    Current code (circuit_breaker.py lines 38–42)::

        if self.state == CBState.HALF_OPEN:
            if self._trial_in_progress:
                raise CircuitOpenError(self.name)   # every subsequent call blocked
            self._trial_in_progress = True
            return

    Bug: once ``_trial_in_progress`` is set to ``True`` (during OPEN→HALF_OPEN),
    the HALF_OPEN branch raises ``CircuitOpenError`` on every subsequent call
    regardless of how much time has passed.  If the trial is abandoned without
    calling ``record_success()`` or ``record_failure()``, the CB stays in
    HALF_OPEN permanently — provider is inaccessible forever.

    There is NO timeout check in the HALF_OPEN branch.  The recovery_timeout_s
    check exists ONLY in the OPEN branch.  A correct implementation must also
    allow a new probe in HALF_OPEN when the timeout has elapsed again (treating
    the previous abandoned trial as failed and starting fresh).

    This test:
    1. Transitions CB from OPEN → HALF_OPEN (first probe).
    2. Simulates an abandoned trial: recovery_timeout elapses again, but no
       record_success() or record_failure() is called.
    3. Asserts that a new probe is allowed (HALF_OPEN → HALF_OPEN reset, or
       back to OPEN and then HALF_OPEN).
    4. With the current bug, step 3 raises CircuitOpenError → test fails.
    """
    cb = CircuitBreaker(name="svc_a9", failure_threshold=1, recovery_timeout_s=1)
    cb.record_failure()
    assert cb.state == CBState.OPEN

    # Force recovery timeout elapsed → OPEN → HALF_OPEN
    cb.last_failure_time = 0.0
    cb.check()
    assert cb.state == CBState.HALF_OPEN
    assert cb._trial_in_progress, (
        "After OPEN→HALF_OPEN transition, _trial_in_progress must be True"
    )

    # Simulate abandoned trial: another recovery_timeout has elapsed.
    # Set last_failure_time to epoch so elapsed >> recovery_timeout_s (1 s).
    cb.last_failure_time = 0.0  # timeout has elapsed again

    # With correct code: the elapsed timeout triggers a new probe allowance.
    # With bug A9:       the HALF_OPEN guard raises immediately, ignoring timeout.
    try:
        cb.check()
        # Allowed through — CB correctly handled the abandoned trial.
    except CircuitOpenError:
        pytest.fail(
            "BUG: CircuitBreaker.check() raised CircuitOpenError in HALF_OPEN "
            "state even though the recovery_timeout_s has elapsed again.  "
            "Once _trial_in_progress is set to True (during OPEN→HALF_OPEN "
            "transition), the HALF_OPEN branch blocks ALL subsequent calls "
            "permanently — `if self._trial_in_progress: raise CircuitOpenError`.  "
            "There is no timeout check in the HALF_OPEN branch, so an abandoned "
            "trial (no record_success or record_failure called) leaves the "
            "provider permanently unavailable.  "
            "Fix: check elapsed time in HALF_OPEN and allow a new probe if "
            "recovery_timeout_s has elapsed since last_failure_time."
        )


# ─────────────────────────────────────────────────────────────────────────────
# A11 — config.py: max_slices / idle_timeout_s stored as str when given as str
# ─────────────────────────────────────────────────────────────────────────────

def test_parse_config_max_slices_must_be_int_when_given_as_string():
    """A11 — _parse_config must reject (or coerce) a string max_slices value.

    Current code (config.py lines 275–279)::

        sora = raw.get("sora", {})
        if "max_slices" in sora:
            cfg.max_slices = sora["max_slices"]     # ← no int() cast
        if "idle_timeout_s" in sora:
            cfg.idle_timeout_s = sora["idle_timeout_s"]  # ← no int() cast

    Bug: when the config dict contains string values for these fields (e.g.
    from programmatic construction or a malformed TOML-like source),
    ``max_slices`` and ``idle_timeout_s`` are stored as strings.  All
    downstream code that treats these as integers (``range(cfg.max_slices)``,
    ``cfg.max_slices > 0``, ``cfg.idle_timeout_s + 30``) raises ``TypeError``.

    This test passes ``max_slices = "10"`` and ``idle_timeout_s = "300"``
    and asserts the resulting ``Config`` fields are ``int`` values.  Under
    the current code they are stored verbatim as strings.
    """
    from tero2.config import _parse_config

    raw = {
        "sora": {
            "max_slices": "10",        # string, not int
            "idle_timeout_s": "300",   # string, not int
        }
    }

    cfg = _parse_config(raw)

    assert isinstance(cfg.max_slices, int), (
        f"BUG: cfg.max_slices is {type(cfg.max_slices).__name__!r} "
        f"(value={cfg.max_slices!r}) after parsing the string '10'.  "
        "_parse_config must validate/coerce sora.max_slices to int.  "
        "Storing a raw string breaks all downstream int operations such as "
        "`range(cfg.max_slices)` which raises TypeError."
    )
    assert cfg.max_slices == 10, (
        f"BUG: expected cfg.max_slices == 10, got {cfg.max_slices!r}"
    )

    assert isinstance(cfg.idle_timeout_s, int), (
        f"BUG: cfg.idle_timeout_s is {type(cfg.idle_timeout_s).__name__!r} "
        f"(value={cfg.idle_timeout_s!r}) after parsing the string '300'.  "
        "Must be coerced to int."
    )
    assert cfg.idle_timeout_s == 300, (
        f"BUG: expected cfg.idle_timeout_s == 300, got {cfg.idle_timeout_s!r}"
    )
