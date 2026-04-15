"""Tests for tero2.reflexion — failure context injection for retries."""

from __future__ import annotations

from tero2.reflexion import (
    ReflexionAttempt,
    ReflexionContext,
    add_attempt,
    build_reflexion_context,
)


class TestReflexionContext:
    def test_empty_context_produces_empty_prompt(self):
        ctx = ReflexionContext()
        assert ctx.is_empty
        assert ctx.to_prompt() == ""

    def test_single_attempt_format(self):
        ctx = ReflexionContext(
            attempts=[
                ReflexionAttempt(
                    attempt_number=1,
                    builder_output="wrote auth module",
                    verifier_feedback="test_token_expiry FAILED",
                    failed_tests=["test_token_expiry"],
                    must_haves_failed=["tokens expire after 1h"],
                )
            ]
        )
        prompt = ctx.to_prompt()
        assert "Previous Attempts" in prompt
        assert "Attempt 1" in prompt
        assert "wrote auth module" in prompt
        assert "test_token_expiry FAILED" in prompt
        assert "test_token_expiry" in prompt
        assert "tokens expire after 1h" in prompt

    def test_multiple_attempts(self):
        ctx = ReflexionContext(
            attempts=[
                ReflexionAttempt(
                    attempt_number=1,
                    builder_output="try 1",
                    verifier_feedback="fail 1",
                    failed_tests=[],
                    must_haves_failed=[],
                ),
                ReflexionAttempt(
                    attempt_number=2,
                    builder_output="try 2",
                    verifier_feedback="fail 2",
                    failed_tests=["test_a"],
                    must_haves_failed=["req_a"],
                ),
            ]
        )
        prompt = ctx.to_prompt()
        assert "Attempt 1" in prompt
        assert "Attempt 2" in prompt
        assert "try 1" in prompt
        assert "try 2" in prompt
        assert not ctx.is_empty


class TestAddAttempt:
    def test_add_to_empty_context(self):
        ctx = ReflexionContext()
        result = add_attempt(ctx, builder_output="did stuff", verifier_feedback="fail")
        assert len(result.attempts) == 1
        assert result.attempts[0].attempt_number == 1
        assert result.attempts[0].builder_output == "did stuff"
        assert result.attempts[0].verifier_feedback == "fail"

    def test_add_appends_and_increments_number(self):
        ctx = ReflexionContext(
            attempts=[
                ReflexionAttempt(
                    attempt_number=1,
                    builder_output="first",
                    verifier_feedback="bad",
                    failed_tests=[],
                    must_haves_failed=[],
                )
            ]
        )
        result = add_attempt(ctx, builder_output="second", verifier_feedback="still bad")
        assert len(result.attempts) == 2
        assert result.attempts[1].attempt_number == 2
        assert result.attempts[1].builder_output == "second"

    def test_add_with_optional_fields(self):
        ctx = ReflexionContext()
        result = add_attempt(
            ctx,
            builder_output="output",
            verifier_feedback="feedback",
            failed_tests=["test_x", "test_y"],
            must_haves_failed=["must_1"],
        )
        assert result.attempts[0].failed_tests == ["test_x", "test_y"]
        assert result.attempts[0].must_haves_failed == ["must_1"]


class TestBuildReflexionContext:
    def test_truncates_long_builder_output(self):
        long_output = "x" * 3000
        attempts = [
            ReflexionAttempt(
                attempt_number=1,
                builder_output=long_output,
                verifier_feedback="fail",
                failed_tests=[],
                must_haves_failed=[],
            )
        ]
        ctx = build_reflexion_context(attempts)
        assert len(ctx.attempts[0].builder_output) < len(long_output)
        assert "[truncated]" in ctx.attempts[0].builder_output

    def test_short_output_not_truncated(self):
        attempts = [
            ReflexionAttempt(
                attempt_number=1,
                builder_output="short",
                verifier_feedback="fail",
                failed_tests=[],
                must_haves_failed=[],
            )
        ]
        ctx = build_reflexion_context(attempts)
        assert ctx.attempts[0].builder_output == "short"
