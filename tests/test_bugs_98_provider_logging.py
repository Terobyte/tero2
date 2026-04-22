"""Bug 98: provider exceptions are swallowed in the failover path.

Before the fix, ``ProviderChain.run`` caught every provider exception, branched
on recoverable/yielded_anything/attempt, and eventually raised a generic
``RateLimitError("all providers in chain exhausted")``. The concrete ``exc``
value — the real reason a provider failed — was never logged. Debugging a
failed multi-provider run required attaching a debugger or adding prints.

These negative tests pin down the expected logging contract so a regression
that re-silences providers is caught immediately.
"""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.errors import ProviderError, ProviderTimeoutError, RateLimitError
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain

CHAIN_LOGGER = "tero2.providers.chain"


class _RaisingProvider(BaseProvider):
    """Always raises the given exception before yielding anything."""

    def __init__(self, name: str, exc: Exception) -> None:
        self._name = name
        self._exc = exc
        self.calls = 0

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        raise self._exc
        yield  # pragma: no cover — makes this an async generator


class _OkProvider(BaseProvider):
    def __init__(self, name: str) -> None:
        self._name = name
        self.calls = 0

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        yield "ok"


async def _drain(chain: ProviderChain) -> list[Any]:
    out: list[Any] = []
    async for msg in chain.run(prompt="x"):
        out.append(msg)
    return out


class TestPerAttemptWarning:
    async def test_retry_attempt_logs_warning_with_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Each failed attempt that will be retried must log a WARNING containing
        the provider name, attempt number, exception class, and exception text."""
        prov = _RaisingProvider("kilo", ProviderError("upstream 502 bad gateway"))
        ok = _OkProvider("opencode")
        chain = ProviderChain(
            [prov, ok],
            cb_registry=CircuitBreakerRegistry(),
            rate_limit_max_retries=2,
            rate_limit_wait_s=0.0,
        )

        caplog.set_level(logging.WARNING, logger=CHAIN_LOGGER)
        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            await _drain(chain)

        warnings = [
            r for r in caplog.records
            if r.name == CHAIN_LOGGER and r.levelno == logging.WARNING
        ]
        assert warnings, "expected at least one WARNING log from failover path"
        joined = "\n".join(r.getMessage() for r in warnings)
        assert "kilo" in joined
        assert "ProviderError" in joined
        assert "upstream 502 bad gateway" in joined
        assert "attempt" in joined.lower()

    async def test_warning_count_equals_retry_attempts(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """With max_retries=2, a provider that always fails emits 2 retry warnings
        before the final exhaustion error (the attempt that hits the cap logs ERROR, not WARNING)."""
        prov = _RaisingProvider("kilo", RateLimitError("429 rate limited"))
        chain = ProviderChain(
            [prov],
            cb_registry=CircuitBreakerRegistry(),
            rate_limit_max_retries=2,
            rate_limit_wait_s=0.0,
        )

        caplog.set_level(logging.WARNING, logger=CHAIN_LOGGER)
        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            with pytest.raises(RateLimitError):
                await _drain(chain)

        warnings = [
            r for r in caplog.records
            if r.name == CHAIN_LOGGER and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 2, (
            f"expected 2 retry warnings (attempts 1 and 2), got {len(warnings)}"
        )


class TestProviderExhaustionError:
    async def test_exhaustion_logs_error_with_exception(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When retries are exhausted for a provider, an ERROR log must include
        the provider name, total attempts, exception class, and exception text."""
        prov_kilo = _RaisingProvider("kilo", ProviderError("auth token expired"))
        prov_opencode = _RaisingProvider(
            "opencode", ProviderTimeoutError("opencode", 30.0)
        )
        chain = ProviderChain(
            [prov_kilo, prov_opencode],
            cb_registry=CircuitBreakerRegistry(),
            rate_limit_max_retries=1,
            rate_limit_wait_s=0.0,
        )

        caplog.set_level(logging.WARNING, logger=CHAIN_LOGGER)
        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            with pytest.raises(RateLimitError):
                await _drain(chain)

        errors = [
            r for r in caplog.records
            if r.name == CHAIN_LOGGER and r.levelno == logging.ERROR
        ]
        assert len(errors) >= 2, (
            "expected at least one ERROR per exhausted provider (kilo + opencode)"
        )
        joined = "\n".join(r.getMessage() for r in errors)
        assert "kilo" in joined
        assert "opencode" in joined
        assert "auth token expired" in joined
        assert "ProviderError" in joined
        assert "ProviderTimeoutError" in joined
        assert "exhausted" in joined.lower()


class TestNonRecoverableErrorLogging:
    async def test_non_recoverable_logs_error_before_reraise(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-recoverable exceptions (e.g. ValueError) must be logged as ERROR
        with the provider name and exception before the chain re-raises them."""
        prov = _RaisingProvider("kilo", ValueError("bad model id"))
        chain = ProviderChain(
            [prov],
            cb_registry=CircuitBreakerRegistry(),
            rate_limit_max_retries=5,
            rate_limit_wait_s=0.0,
        )

        caplog.set_level(logging.ERROR, logger=CHAIN_LOGGER)
        with pytest.raises(ValueError, match="bad model id"):
            await _drain(chain)

        errors = [
            r for r in caplog.records
            if r.name == CHAIN_LOGGER and r.levelno == logging.ERROR
        ]
        assert errors, "non-recoverable exception must be logged before re-raise"
        msg = errors[0].getMessage()
        assert "kilo" in msg
        assert "ValueError" in msg
        assert "bad model id" in msg
        assert "non-recoverable" in msg.lower()


class TestMidStreamFailureLogging:
    async def test_post_yield_error_logs_mid_stream(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A recoverable error that fires AFTER the first yield is a hard-fail.
        The log must flag it as mid-stream so the operator knows it wasn't retried."""

        class _PartialThenFail(BaseProvider):
            @property
            def display_name(self) -> str:
                return "kilo"

            async def run(self, **kwargs: Any):
                yield "partial chunk"
                raise RateLimitError("dropped mid-response")

        chain = ProviderChain(
            [_PartialThenFail()],
            cb_registry=CircuitBreakerRegistry(),
            rate_limit_max_retries=3,
            rate_limit_wait_s=0.0,
        )

        caplog.set_level(logging.ERROR, logger=CHAIN_LOGGER)
        with pytest.raises(RateLimitError, match="dropped mid-response"):
            await _drain(chain)

        errors = [
            r for r in caplog.records
            if r.name == CHAIN_LOGGER and r.levelno == logging.ERROR
        ]
        assert errors, "mid-stream failure must produce an ERROR log"
        msg = errors[0].getMessage()
        assert "kilo" in msg
        assert "mid-stream" in msg.lower()
        assert "dropped mid-response" in msg
