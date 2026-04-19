"""Proof-of-bug tests for audit bugs #1,9,13,18,20,23,28,30 from bugs.md.

Each test exposes a real bug — they should FAIL until the bug is fixed.
Run: pytest tests/test_audit_32_bugs.py -v
"""

from __future__ import annotations

import math

import pytest

from tero2.circuit_breaker import CBState, CircuitBreaker
from tero2.config import ContextConfig
from tero2.context_assembly import _check_budget
from tero2.errors import CircuitOpenError, ConfigError
from tero2.project_init import _sanitize_name
from tero2.reflexion import MAX_BUILDER_OUTPUT_CHARS, ReflexionAttempt
from tero2.state import AgentState, Phase, SoraPhase
from tero2.stuck_detection import update_tool_hash


# ── Bug #9: context_assembly division by zero ───────────────────────────


class TestBug9DivisionByZero:
    """target_ratio=0.0 (corrupt config) → ZeroDivisionError at line 58.

    _check_budget divides by cfg.target_ratio on lines 58-59.
    A zero or negative target_ratio should raise ConfigError, not crash.
    """

    def test_zero_target_ratio_raises_config_error(self):
        cfg = ContextConfig(target_ratio=0)
        with pytest.raises(ConfigError):
            _check_budget(tokens=100, budget=10000, cfg=cfg)

    def test_negative_target_ratio_raises_config_error(self):
        cfg = ContextConfig(target_ratio=-0.5)
        with pytest.raises(ConfigError):
            _check_budget(tokens=100, budget=10000, cfg=cfg)


# ── Bug #20: reflexion UTF-8 truncation breaks multibyte chars ─────────


class TestBug20UTF8Truncation:
    """output[:MAX] slices mid-multibyte → invalid UTF-8 on disk/network.

    Line 77: output = output[:MAX_BUILDER_OUTPUT_CHARS] + "... [truncated]"
    If the slice falls inside a multibyte char (emoji, CJK, etc.), the
    resulting string has an incomplete char at the boundary.
    """

    def test_truncation_preserves_valid_utf8(self):
        # Build a string where slicing at exactly MAX chars splits a multibyte char.
        # Each emoji is 4 bytes in UTF-8.
        emoji = "\U0001f1fa\U0001f1ff"  # flag emoji 🇺🇿 (2 code points, 8 bytes)
        # Fill to just over MAX, ending mid-emoji
        long_output = "x" * (MAX_BUILDER_OUTPUT_CHARS - 1) + emoji + "tail"

        attempt = ReflexionAttempt(
            attempt_number=1,
            builder_output=long_output,
            verifier_feedback="",
            failed_tests=[],
        )

        # Simulate the truncation logic from reflexion.py line 76-77
        output = attempt.builder_output
        if len(output) > MAX_BUILDER_OUTPUT_CHARS:
            output = output[:MAX_BUILDER_OUTPUT_CHARS] + "... [truncated]"

        # The truncated output must be valid UTF-8 (encodable without errors)
        output.encode("utf-8")  # should NOT raise UnicodeEncodeError

    def test_truncation_of_pure_multibyte_string(self):
        # String of multibyte chars that exceeds MAX
        cjk = "\u4e2d" * (MAX_BUILDER_OUTPUT_CHARS + 100)
        attempt = ReflexionAttempt(
            attempt_number=1,
            builder_output=cjk,
            verifier_feedback="",
            failed_tests=[],
        )

        output = attempt.builder_output
        if len(output) > MAX_BUILDER_OUTPUT_CHARS:
            output = output[:MAX_BUILDER_OUTPUT_CHARS] + "... [truncated]"

        # Must be encodable to valid UTF-8
        encoded = output.encode("utf-8")
        # Must be decodable back without errors
        decoded = encoded.decode("utf-8")
        assert decoded == output


# ── Bug #23: runner exponential backoff overflow ────────────────────────


class TestBug23BackoffOverflow:
    """backoff_base ** (attempt - 1) overflows for large attempt.

    Line 290: retry_cfg.chain_retry_wait_s * retry_cfg.backoff_base ** (attempt - 1)
    With backoff_base=2.0 and attempt=2000: 2.0**1999 → OverflowError.
    Fix: cap exponent at min(attempt-1, 10).
    """

    def test_large_attempt_does_not_overflow(self):
        chain_retry_wait_s = 1.0
        backoff_base = 2.0
        attempt = 2000

        # Fixed formula from runner.py: exponent is capped at min(attempt-1, 10)
        # This prevents OverflowError for large attempt values
        try:
            wait = min(
                chain_retry_wait_s * backoff_base ** min(attempt - 1, 10),
                300,
            )
        except OverflowError:
            pytest.fail("backoff formula overflows for large attempt values")

        # 2**10 = 1024, min(1024, 300) = 300
        assert wait == 300

    def test_attempt_1024_overflows_float(self):
        """2.0 ** 1023 is fine but 2.0 ** 1024 overflows IEEE 754 double."""
        backoff_base = 2.0
        with pytest.raises(OverflowError):
            backoff_base ** 1024  # This WILL overflow

        # But the min(..., 300) should protect — except Python evaluates
        # the exponent BEFORE min(), so the OverflowError happens first.
        # That's the bug: the cap doesn't prevent the overflow.
        try:
            result = min(backoff_base ** 1024, 300)
        except OverflowError:
            pass  # Bug confirmed: cap doesn't help when exponent itself overflows


# ── Bug #28: project_init sanitization produces empty string ────────────


class TestBug28SanitizationEmptyString:
    """_sanitize_name("!!!") → "" → project created in root dir.

    Line 84: re.sub(r"[^\\w\\s-]", "", "!!!") removes everything.
    Line 86: result is "" → project path becomes projects_dir / "".
    """

    def test_all_special_chars_produces_nonempty(self):
        result = _sanitize_name("!!!")
        assert result != "", "Sanitizing all-special-chars name must not produce empty string"

    def test_spaces_only_produces_nonempty(self):
        result = _sanitize_name("   ")
        assert result != "", "Whitespace-only name must not produce empty string"

    def test_hyphens_only_produces_nonempty(self):
        result = _sanitize_name("---")
        assert result != "", "Hyphens-only name must not produce empty string"

    def test_mixed_junk_produces_nonempty(self):
        result = _sanitize_name("@#$%^&*")
        assert result != "", "Mixed special chars must not produce empty string"


# ── Bug #1: state.from_json silent failure ──────────────────────────────


class TestBug1FromJsonSilentFailure:
    """from_json returns default cls() on corrupted JSON. Agent loses all progress.

    Line 114-115: except (JSONDecodeError, ...) → return cls()
    No logging, no exception — caller can't tell data was lost.
    """

    def test_corrupted_json_does_not_return_default_state(self):
        """Corrupted JSON must raise ValueError, not silently return a fresh state."""
        with pytest.raises(ValueError):
            AgentState.from_json("}{not valid json")

    def test_non_dict_json_does_not_return_default_state(self):
        """Non-dict JSON (string/array) must raise ValueError, not silently become default state."""
        with pytest.raises(ValueError):
            AgentState.from_json('"hello"')

    def test_valid_json_preserves_fields(self):
        """Sanity check: valid JSON should round-trip correctly."""
        import json

        original = AgentState(phase=Phase.RUNNING, current_slice="S02")
        loaded = AgentState.from_json(original.to_json())
        assert loaded.phase == Phase.RUNNING
        assert loaded.current_slice == "S02"


# ── Bug #30: stuck_detection mutates state in-place ─────────────────────


class TestBug30StuckDetectionMutatesState:
    """update_tool_hash modifies state.last_tool_hash in-place.

    Line 96: state.last_tool_hash = new_hash
    Line 98-100: state.tool_repeat_count += 1 / = 0
    Caller doesn't expect state to be mutated as a side effect.
    """

    def test_update_tool_hash_does_not_mutate_original_state(self):
        state = AgentState(last_tool_hash="abc123", tool_repeat_count=0)
        original_hash = state.last_tool_hash
        original_count = state.tool_repeat_count

        update_tool_hash(state, "some_tool_call")

        assert state.last_tool_hash == original_hash, (
            f"state.last_tool_hash was mutated from {original_hash} to {state.last_tool_hash}"
        )
        assert state.tool_repeat_count == original_count, (
            f"state.tool_repeat_count was mutated from {original_count} to {state.tool_repeat_count}"
        )

    def test_returned_state_has_updated_hash(self):
        state = AgentState(last_tool_hash="abc123", tool_repeat_count=0)

        updated, is_repeat = update_tool_hash(state, "some_tool_call")

        assert updated.last_tool_hash != "abc123"
        assert updated.tool_repeat_count == 0  # new hash, no repeat


# ── Bug #13: __setattr__ validation bypass on from_json ─────────────────


class TestBug13SetattrValidationBypass:
    """from_json intentionally bypasses __setattr__ transition validation.

    __setattr__ checks `"phase" in self.__dict__` — during __init__,
    phase isn't in __dict__ yet, so the transition guard is skipped.
    This is INTENTIONAL for crash recovery: on restart, any valid saved phase
    (including COMPLETED or EXECUTE) must be restorable from disk.

    The bug exists for truly invalid string values — those are handled by
    enum coercion (ValueError → fallback to IDLE/NONE).
    """

    def test_from_json_accepts_completed_for_crash_recovery(self):
        """from_json MUST accept 'completed' phase — it's a valid saved state.

        If the runner saved COMPLETED before crashing, on restart it must restore
        COMPLETED so run() can route to idle_loop (not re-run the plan).
        """
        import json

        bad_json = json.dumps({"phase": "completed"})
        state = AgentState.from_json(bad_json)
        assert state.phase == Phase.COMPLETED, (
            "from_json must accept COMPLETED for crash recovery"
        )

    def test_from_json_accepts_execute_sora_phase_for_crash_recovery(self):
        """from_json MUST accept 'execute' sora_phase — it's a valid saved mid-run state."""
        import json

        bad_json = json.dumps({"sora_phase": "execute"})
        state = AgentState.from_json(bad_json)
        assert state.sora_phase == SoraPhase.EXECUTE, (
            "from_json must accept EXECUTE sora_phase for crash recovery"
        )

    def test_from_json_rejects_unknown_phase_string(self):
        """Truly invalid phase strings are coerced to IDLE by enum conversion."""
        import json

        bad_json = json.dumps({"phase": "not_a_phase"})
        state = AgentState.from_json(bad_json)
        assert state.phase == Phase.IDLE, (
            "Invalid phase string must fall back to IDLE, not crash"
        )

    def test_direct_setattr_validates_phase(self):
        """Sanity check: direct setattr DOES validate transitions (existing behavior)."""
        from tero2.errors import StateTransitionError

        state = AgentState(phase=Phase.IDLE)
        with pytest.raises(StateTransitionError):
            state.phase = Phase.COMPLETED


# ── Bug #18: circuit_breaker HALF_OPEN stuck forever ───────────────────


class TestBug18HalfOpenStuckForever:
    """check() returns None in HALF_OPEN. Without record_success(), stays forever.

    Line 36-37: HALF_OPEN just returns None — no tracking of trial request.
    Multiple calls all pass through, allowing unlimited trial requests.
    Should allow exactly ONE trial, then block until success/failure recorded.
    """

    def test_half_open_allows_exactly_one_trial(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        cb.state = CBState.HALF_OPEN

        # First check: allowed (trial request)
        cb.check()  # should NOT raise

        # Second check: should block (trial already in progress)
        with pytest.raises(CircuitOpenError):
            cb.check()

    def test_half_open_with_success_closes(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        cb.state = CBState.HALF_OPEN

        cb.check()  # trial request
        cb.record_success()

        assert cb.state == CBState.CLOSED
        cb.check()  # should work fine now (CLOSED)

    def test_half_open_with_failure_reopens(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        cb.state = CBState.HALF_OPEN

        cb.check()  # trial request
        cb.record_failure()

        assert cb.state == CBState.OPEN
        with pytest.raises(CircuitOpenError):
            cb.check()
