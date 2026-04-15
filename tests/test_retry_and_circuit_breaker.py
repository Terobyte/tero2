"""Tests for Steps 1-4 of MVP-1 phase: rate-limit retry, circuit breaker, Telegram start."""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tero2.circuit_breaker import CBState, CircuitBreaker, CircuitBreakerRegistry
from tero2.errors import (
    CircuitOpenError,
    RateLimitError,
)
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain


# ── helpers ──────────────────────────────────────────────────────────


class _OnceFailing(BaseProvider):
    """Raises RateLimitError for the first N calls, then yields one item."""

    def __init__(self, name: str, fail_times: int = 1):
        self._name = name
        self._calls = 0
        self._fail_times = fail_times

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self._calls += 1
        if self._calls <= self._fail_times:
            raise RateLimitError(f"{self._name} 429")
        yield f"{self._name}:ok"


class _AlwaysFailing(BaseProvider):
    def __init__(self, name: str, exc=None):
        self._name = name
        self._exc = exc or RateLimitError(f"{name} always 429")
        self._calls = 0

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self._calls += 1
        raise self._exc
        yield  # make this an async generator


class _AlwaysOk(BaseProvider):
    def __init__(self, name: str):
        self._name = name
        self._calls = 0

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self._calls += 1
        yield f"{self._name}:ok"


async def _collect(chain: ProviderChain, **kwargs: Any) -> list[Any]:
    result = []
    async for msg in chain.run(**kwargs):
        result.append(msg)
    return result


# ── Step 1: RateLimitError → retry with backoff + jitter → fallback ──


class TestStep1RateLimitRetryWithBackoff:
    async def test_retries_same_provider_before_success(self):
        """Provider fails twice then succeeds on 3rd attempt — no fallback needed."""
        prov_a = _OnceFailing("A", fail_times=2)
        prov_b = _AlwaysOk("B")

        sleep_calls: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_calls.append(t)

        chain = ProviderChain(
            [prov_a, prov_b],
            rate_limit_max_retries=3,
            rate_limit_wait_s=1.0,
        )

        with patch("tero2.providers.chain.asyncio.sleep", fake_sleep):
            with patch("tero2.providers.chain.random.uniform", return_value=0.0):
                result = await _collect(chain, prompt="x")

        assert result == ["A:ok"], "should succeed on A after retries, never touch B"
        assert prov_a._calls == 3, "A should be called 3 times (2 failures + 1 success)"
        assert prov_b._calls == 0, "B should never be called"
        assert len(sleep_calls) == 2, "sleep called before attempt 1 and attempt 2"

    async def test_backoff_grows_exponentially(self):
        """Each retry wait doubles: 1s, 2s, 4s, ..."""
        prov_a = _OnceFailing("A", fail_times=3)  # fail 3 times, succeed on 4th

        sleep_calls: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_calls.append(t)

        chain = ProviderChain(
            [prov_a],
            rate_limit_max_retries=4,
            rate_limit_wait_s=2.0,
        )

        with patch("tero2.providers.chain.asyncio.sleep", fake_sleep):
            with patch("tero2.providers.chain.random.uniform", return_value=0.0):
                result = await _collect(chain, prompt="x")

        assert result == ["A:ok"]
        # Attempt 0: no sleep
        # Attempt 1: 2.0 * 2^0 = 2.0s
        # Attempt 2: 2.0 * 2^1 = 4.0s
        # Attempt 3: 2.0 * 2^2 = 8.0s
        assert sleep_calls == pytest.approx([2.0, 4.0, 8.0])

    async def test_jitter_is_added(self):
        """Jitter (0–10% of base wait) is added to each sleep."""
        prov_a = _OnceFailing("A", fail_times=1)

        sleep_calls: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_calls.append(t)

        chain = ProviderChain(
            [prov_a],
            rate_limit_max_retries=2,
            rate_limit_wait_s=10.0,
        )

        with patch("tero2.providers.chain.asyncio.sleep", fake_sleep):
            with patch("tero2.providers.chain.random.uniform", return_value=0.5):
                await _collect(chain, prompt="x")

        # sleep = base (10.0 * 2^0 = 10.0) + jitter (0.5)
        assert sleep_calls[0] == pytest.approx(10.5)

    async def test_fallback_to_next_provider_after_all_retries_fail(self):
        """All retries for provider A exhausted → falls back to provider B."""
        prov_a = _AlwaysFailing("A")
        prov_b = _AlwaysOk("B")

        chain = ProviderChain(
            [prov_a, prov_b],
            rate_limit_max_retries=2,
            rate_limit_wait_s=0.0,
        )

        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            result = await _collect(chain, prompt="x")

        assert result == ["B:ok"]
        assert prov_a._calls == 3, "A tried 1 + 2 retries = 3 times"
        assert prov_b._calls == 1

    async def test_all_providers_exhausted_raises_ratelimiterror(self):
        """When all providers exhaust retries, RateLimitError is raised."""
        prov_a = _AlwaysFailing("A")
        prov_b = _AlwaysFailing("B")

        chain = ProviderChain(
            [prov_a, prov_b],
            rate_limit_max_retries=1,
            rate_limit_wait_s=0.0,
        )

        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            with pytest.raises(RateLimitError, match="all providers in chain exhausted"):
                await _collect(chain, prompt="x")

    async def test_wait_capped_at_300s(self):
        """Backoff is capped at 300s regardless of base_wait * 2^attempt."""
        prov_a = _OnceFailing("A", fail_times=3)

        sleep_calls: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_calls.append(t)

        chain = ProviderChain(
            [prov_a],
            rate_limit_max_retries=4,
            rate_limit_wait_s=200.0,  # 200 * 2^2 = 800 > 300
        )

        with patch("tero2.providers.chain.asyncio.sleep", fake_sleep):
            with patch("tero2.providers.chain.random.uniform", return_value=0.0):
                await _collect(chain, prompt="x")

        # attempt 1: 200*1=200, attempt 2: 200*2=400→capped 300, attempt 3: 200*4=800→capped 300
        assert sleep_calls[0] == pytest.approx(200.0)
        assert sleep_calls[1] == pytest.approx(300.0)
        assert sleep_calls[2] == pytest.approx(300.0)


# ── Step 2: 3+ failures → circuit OPEN → fast-fail ───────────────────


class TestStep2CircuitBreakerOpensAndFastFails:
    def test_circuit_opens_after_failure_threshold(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        assert cb.state == CBState.CLOSED

        cb.record_failure()
        cb.record_failure()
        assert cb.state == CBState.CLOSED

        cb.record_failure()  # 3rd failure
        assert cb.state == CBState.OPEN

    def test_open_circuit_is_not_available(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        for _ in range(3):
            cb.record_failure()

        assert not cb.is_available
        with pytest.raises(CircuitOpenError):
            cb.check()

    async def test_chain_skips_open_circuit_fast_fails_to_fallback(self):
        """After 3 failures, circuit opens and chain skips provider (no retries)."""
        cb_registry = CircuitBreakerRegistry(failure_threshold=3)
        cb = cb_registry.get("A")
        for _ in range(3):
            cb.record_failure()  # open the circuit externally

        prov_a = _AlwaysOk("A")  # would succeed, but circuit is open
        prov_b = _AlwaysOk("B")

        chain = ProviderChain([prov_a, prov_b], cb_registry=cb_registry)
        result = await _collect(chain, prompt="x")

        assert result == ["B:ok"]
        assert prov_a._calls == 0, "A was skipped due to open circuit (fast-fail)"

    async def test_persistent_failures_open_circuit_then_fast_fail(self):
        """Each exhausted chain run = 1 CB failure; after threshold runs, circuit opens."""
        # failure_threshold=1: single exhausted run opens the circuit.
        cb_registry = CircuitBreakerRegistry(failure_threshold=1)
        prov_a = _AlwaysFailing("A")
        prov_b = _AlwaysOk("B")

        chain = ProviderChain(
            [prov_a, prov_b],
            cb_registry=cb_registry,
            rate_limit_max_retries=0,  # no retries — clean single-attempt failure
            rate_limit_wait_s=0.0,
        )

        result = await _collect(chain, prompt="first run")

        assert result == ["B:ok"]
        assert cb_registry.get("A").state == CBState.OPEN

        # Second run: A is skipped immediately (fast-fail), no retries attempted.
        prov_a2 = _AlwaysOk("A")  # would succeed now, but circuit is OPEN
        prov_b2 = _AlwaysOk("B")
        chain2 = ProviderChain([prov_a2, prov_b2], cb_registry=cb_registry)

        result2 = await _collect(chain2, prompt="second run")

        assert result2 == ["B:ok"]
        assert prov_a2._calls == 0, "A fast-failed on second run (circuit OPEN)"

    async def test_three_exhausted_runs_open_circuit(self):
        """Default threshold=3: circuit opens only after 3 fully exhausted runs."""
        cb_registry = CircuitBreakerRegistry(failure_threshold=3)
        prov_a = _AlwaysFailing("A")

        chain = ProviderChain(
            [prov_a],
            cb_registry=cb_registry,
            rate_limit_max_retries=0,
        )

        for run_num in range(3):
            with pytest.raises(RateLimitError):
                await _collect(chain, prompt="x")
            if run_num < 2:
                assert cb_registry.get("A").state == CBState.CLOSED, (
                    f"circuit should still be CLOSED after {run_num + 1} run(s)"
                )

        assert cb_registry.get("A").state == CBState.OPEN, (
            "circuit should be OPEN after 3 fully exhausted runs"
        )


# ── Step 3: HALF_OPEN recovery after 60s ─────────────────────────────


class TestStep3CircuitBreakerHalfOpenRecovery:
    def test_open_circuit_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        for _ in range(3):
            cb.record_failure()

        assert cb.state == CBState.OPEN

        # Simulate 60+ seconds passing
        cb.last_failure_time = time.monotonic() - 61
        cb.check()  # should transition to HALF_OPEN without raising
        assert cb.state == CBState.HALF_OPEN

    def test_half_open_is_available(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        cb.state = CBState.HALF_OPEN
        assert cb.is_available

    def test_half_open_success_closes_circuit(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        cb.state = CBState.HALF_OPEN
        cb.failure_count = 3

        cb.record_success()

        assert cb.state == CBState.CLOSED
        assert cb.failure_count == 0

    def test_half_open_failure_reopens_circuit(self):
        cb = CircuitBreaker(name="test", failure_threshold=3, recovery_timeout_s=60)
        cb.state = CBState.HALF_OPEN
        cb.failure_count = 3  # already at threshold

        cb.record_failure()

        assert cb.state == CBState.OPEN
        assert cb.failure_count == 4

    async def test_chain_probes_half_open_provider_and_closes_on_success(self):
        """HALF_OPEN provider gets one probe; on success circuit closes."""
        cb_registry = CircuitBreakerRegistry(failure_threshold=3, recovery_timeout_s=60)
        cb = cb_registry.get("A")
        for _ in range(3):
            cb.record_failure()
        # Simulate timeout expiry
        cb.last_failure_time = time.monotonic() - 61

        prov_a = _AlwaysOk("A")
        chain = ProviderChain([prov_a], cb_registry=cb_registry)
        result = await _collect(chain, prompt="probe")

        assert result == ["A:ok"]
        assert cb_registry.get("A").state == CBState.CLOSED

    async def test_chain_probes_half_open_provider_and_reopens_on_failure(self):
        """HALF_OPEN provider fails probe → circuit reopens."""
        cb_registry = CircuitBreakerRegistry(failure_threshold=3, recovery_timeout_s=60)
        cb = cb_registry.get("A")
        for _ in range(3):
            cb.record_failure()
        cb.last_failure_time = time.monotonic() - 61

        prov_a = _AlwaysFailing("A")
        prov_b = _AlwaysOk("B")
        chain = ProviderChain(
            [prov_a, prov_b],
            cb_registry=cb_registry,
            rate_limit_max_retries=0,  # no retries, single probe
        )

        result = await _collect(chain, prompt="probe")

        assert result == ["B:ok"]
        assert cb_registry.get("A").state == CBState.OPEN


# ── Step 4: Telegram "started" message ───────────────────────────────


class TestStep4TelegramStartedMessage:
    async def test_runner_sends_started_on_run(self, tmp_path):
        """Runner calls notifier.notify('started', ...) before executing the plan."""
        from unittest.mock import patch

        from tero2.config import Config, RoleConfig, TelegramConfig
        from tero2.runner import Runner

        project = tmp_path / "proj"
        project.mkdir()
        plan = project / "plan.md"
        plan.write_text("# Plan\n1. do stuff")

        config = Config()
        config.roles["executor"] = RoleConfig(provider="fake", timeout_s=5)
        config.telegram = TelegramConfig(bot_token="tok", chat_id="chat")

        notified: list[tuple] = []

        async def fake_notify(text: str, level=None) -> bool:
            notified.append((text, level))
            return True

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = fake_notify  # type: ignore[method-assign]

        # Patch _execute_plan so we only verify the "started" message
        async def fake_execute(state, shutdown_event=None) -> None:
            pass

        with patch.object(runner, "_execute_plan", fake_execute):
            await runner.run()

        texts = [t for t, _ in notified]
        assert "started" in texts, (
            f"runner.run() must send 'started' via notifier before execution. "
            f"Got: {texts}"
        )

    def test_runner_source_has_started_notify(self):
        """Static check: runner.run() source contains notifier.notify('started', ...)."""
        import inspect

        from tero2.runner import Runner

        source = inspect.getsource(Runner.run)
        assert '"started"' in source or "'started'" in source, (
            "Runner.run() must call notifier.notify with 'started' text"
        )
