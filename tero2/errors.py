"""Exception hierarchy for tero2."""

from __future__ import annotations


class Tero2Error(Exception):
    """Base for all tero2 application errors."""


class ProviderError(Tero2Error):
    """Base for provider/LLM errors."""


class ProviderNotReadyError(ProviderError):
    """Provider failed readiness check."""


class ProviderTimeoutError(ProviderError):
    """Provider call exceeded time budget."""

    def __init__(self, provider: str, timeout_s: float) -> None:
        self.provider = provider
        self.timeout_s = timeout_s
        super().__init__(f"{provider} timed out after {timeout_s}s")


class RateLimitError(ProviderError):
    """All providers in chain exhausted after retries."""


class CircuitOpenError(ProviderError):
    """Provider circuit breaker is open (fast-fail)."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"Circuit open for {provider}, skipping")


class StateError(Tero2Error):
    """Base for state machine errors."""


class LockHeldError(StateError):
    """Another tero2 instance holds the lock."""

    def __init__(self, pid: int, lock_path: str) -> None:
        self.pid = pid
        self.lock_path = lock_path
        super().__init__(f"Lock held by PID {pid}: {lock_path}")


class StateTransitionError(StateError):
    """Invalid state transition."""

    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid transition: {current} -> {target}")


class ConfigError(Tero2Error):
    """Invalid or missing configuration."""


class RunnerError(Tero2Error):
    """Base for runner lifecycle errors."""


class TaskFailedError(RunnerError):
    """Task exhausted all retry attempts."""

    def __init__(self, task_id: str, attempts: int) -> None:
        self.task_id = task_id
        self.attempts = attempts
        super().__init__(f"Task {task_id} failed after {attempts} attempts")
