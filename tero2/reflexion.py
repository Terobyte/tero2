"""Reflexion — failure context injection for retries.

When the executor fails, the failure details are injected into
the next attempt's context. This gives the agent memory of what went wrong.

Max reflexion cycles: 2 (configurable). After that -> escalate, don't loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_BUILDER_OUTPUT_CHARS = 2000


@dataclass
class ReflexionAttempt:
    """Record of one failed attempt."""

    attempt_number: int
    builder_output: str  # what the builder did (truncated)
    verifier_feedback: str  # why it failed
    failed_tests: list[str]  # specific test names
    must_haves_failed: list[str]  # which must-haves didn't pass


@dataclass
class ReflexionContext:
    """Accumulated failure context across retry attempts."""

    attempts: list[ReflexionAttempt] = field(default_factory=list)

    def to_prompt(self) -> str:
        """Format reflexion context as a prompt section for the executor.

        Example output:
            ## Previous Attempts (DO NOT repeat these mistakes)

            ### Attempt 1 -- FAILED
            **What was tried:** wrote auth module with JWT
            **What failed:** pytest: test_token_expiry FAILED -- token not expiring
            **Verifier feedback:** must-have "tokens expire after 1h" not satisfied
            **Avoid:** hardcoded expiry, missing time comparison
        """
        if self.is_empty:
            return ""

        lines = ["## Previous Attempts (DO NOT repeat these mistakes)\n"]
        for attempt in self.attempts:
            lines.append(f"### Attempt {attempt.attempt_number} -- FAILED")
            lines.append(f"**What was tried:** {attempt.builder_output}")
            lines.append(f"**What failed:** {attempt.verifier_feedback}")
            if attempt.failed_tests:
                lines.append(f"**Failed tests:** {', '.join(attempt.failed_tests)}")
            if attempt.must_haves_failed:
                lines.append(f"**Must-haves not met:** {'; '.join(attempt.must_haves_failed)}")
            lines.append("")

        return "\n".join(lines)

    @property
    def is_empty(self) -> bool:
        return len(self.attempts) == 0


def build_reflexion_context(
    attempts: list[ReflexionAttempt],
) -> ReflexionContext:
    """Build reflexion context from a list of failed attempts.

    Truncates builder_output to avoid context overflow.
    """
    truncated = []
    for a in attempts:
        output = a.builder_output
        if len(output) > MAX_BUILDER_OUTPUT_CHARS:
            output = output.encode("utf-8")[:MAX_BUILDER_OUTPUT_CHARS].decode("utf-8", errors="ignore") + "... [truncated]"
        truncated.append(
            ReflexionAttempt(
                attempt_number=a.attempt_number,
                builder_output=output,
                verifier_feedback=a.verifier_feedback,
                failed_tests=a.failed_tests,
                must_haves_failed=a.must_haves_failed,
            )
        )
    return ReflexionContext(attempts=truncated)


def add_attempt(
    context: ReflexionContext,
    builder_output: str,
    verifier_feedback: str,
    failed_tests: list[str] | None = None,
    must_haves_failed: list[str] | None = None,
) -> ReflexionContext:
    """Add a failed attempt to the reflexion context.

    Args:
        context: Existing reflexion context (may be empty).
        builder_output: Raw output from the executor's failed run.
        verifier_feedback: Description of why it failed.
        failed_tests: Specific test names that failed.
        must_haves_failed: Must-have conditions that weren't met.

    Returns:
        Updated ReflexionContext with the new attempt appended.
    """
    attempt = ReflexionAttempt(
        attempt_number=len(context.attempts) + 1,
        builder_output=builder_output,
        verifier_feedback=verifier_feedback,
        failed_tests=failed_tests or [],
        must_haves_failed=must_haves_failed or [],
    )
    context.attempts.append(attempt)
    return context
