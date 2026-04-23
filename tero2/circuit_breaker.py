"""Circuit breaker for provider fault tolerance."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum

from tero2.constants import CB_FAILURE_THRESHOLD, CB_RECOVERY_TIMEOUT_S
from tero2.errors import CircuitOpenError


class CBState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    name: str
    failure_threshold: int = CB_FAILURE_THRESHOLD
    recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S
    state: CBState = CBState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0
    last_half_open_failure_time: float = 0.0
    _trial_in_progress: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def check(self) -> None:
        with self._lock:
            if self.state == CBState.CLOSED:
                return
            if self.state == CBState.OPEN:
                now = time.time()
                if now - self.last_failure_time >= self.recovery_timeout_s:
                    # Bug 256: with recovery_timeout_s=0, block re-probe after HALF_OPEN failure
                    if self.recovery_timeout_s == 0 and self.last_half_open_failure_time > 0:
                        raise CircuitOpenError(self.name)
                    self.state = CBState.HALF_OPEN
                    self._trial_in_progress = True
                    return
                raise CircuitOpenError(self.name)
            if self.state == CBState.HALF_OPEN:
                if self._trial_in_progress:
                    if (
                        self.recovery_timeout_s > 0
                        and time.time() - self.last_failure_time > self.recovery_timeout_s
                    ):
                        self._trial_in_progress = False
                    else:
                        raise CircuitOpenError(self.name)
                self._trial_in_progress = True
                self.last_failure_time = time.time()
                return

    def record_success(self) -> None:
        with self._lock:
            self.failure_count = 0
            self.state = CBState.CLOSED
            self._trial_in_progress = False
            self.last_half_open_failure_time = 0.0

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.time()
            if self.state == CBState.HALF_OPEN or self.failure_count >= self.failure_threshold:
                if self.state == CBState.HALF_OPEN:
                    self.last_half_open_failure_time = time.time()
                self.state = CBState.OPEN
                self._trial_in_progress = False

    @property
    def is_available(self) -> bool:
        try:
            self.check()
            return True
        except CircuitOpenError:
            return False


class CircuitBreakerRegistry:
    def __init__(
        self,
        failure_threshold: int = CB_FAILURE_THRESHOLD,
        recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S,
    ) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s

    def get(self, provider_name: str) -> CircuitBreaker:
        if provider_name not in self._breakers:
            self._breakers[provider_name] = CircuitBreaker(
                name=provider_name,
                failure_threshold=self._failure_threshold,
                recovery_timeout_s=self._recovery_timeout_s,
            )
        return self._breakers[provider_name]
