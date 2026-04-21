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
            lines.append("")

        return "\n".join(lines)

    @property
    def is_empty(self) -> bool:
        return len(self.attempts) == 0


def truncate_attempts(context: ReflexionContext) -> ReflexionContext:
    """Return a new ReflexionContext with builder_output truncated in each attempt."""
    return build_reflexion_context(context.attempts)


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
            output = output[:MAX_BUILDER_OUTPUT_CHARS] + "... [truncated]"
        truncated.append(
            ReflexionAttempt(
                attempt_number=a.attempt_number,
                builder_output=output,
                verifier_feedback=a.verifier_feedback,
                failed_tests=a.failed_tests,
            )
        )
    return ReflexionContext(attempts=truncated)


def add_attempt(
    context: ReflexionContext,
    builder_output: str,
    verifier_feedback: str,
    failed_tests: list[str] | None = None,
) -> ReflexionContext:
    """Add a failed attempt to the reflexion context.

    Args:
        context: Existing reflexion context (may be empty).
        builder_output: Raw output from the executor's failed run.
        verifier_feedback: Description of why it failed.
        failed_tests: Specific test names that failed.

    Returns:
        Updated ReflexionContext with the new attempt appended.
    """
    attempt = ReflexionAttempt(
        attempt_number=len(context.attempts) + 1,
        builder_output=builder_output,
        verifier_feedback=verifier_feedback,
        failed_tests=failed_tests or [],
    )
    context.attempts.append(attempt)
    return context
