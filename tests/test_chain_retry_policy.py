"""Tests for ProviderChain retry policy edge cases.

Complements test_retry_and_circuit_breaker.py (which covers the main retry
loop, exponential backoff, and circuit-breaker state transitions).

This file focuses on:
- Non-recoverable errors short-circuit without retries
- Stream error-dict events are converted to ProviderError
- current_provider_index tracks the active provider through fallbacks
- run_prompt() delegates correctly to run()
- An empty provider list raises immediately
- Provider skipped silently when circuit is open (is_available=False)
- Yield-aware retry policy: errors before first yield retry; errors after hard-fail
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.errors import (
    CircuitOpenError,
    ProviderError,
    ProviderTimeoutError,
    RateLimitError,
)
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain


# ── helpers ───────────────────────────────────────────────────────────────────


class _OkProvider(BaseProvider):
    """Always succeeds with a single text chunk."""

    def __init__(self, name: str, reply: str = "ok") -> None:
        self._name = name
        self.calls = 0
        self._reply = reply

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        yield self._reply


class _AlwaysFailing(BaseProvider):
    """Always raises a RateLimitError (recoverable by default)."""

    def __init__(self, name: str, exc: Exception | None = None) -> None:
        self._name = name
        self.calls = 0
        self._exc = exc or RateLimitError(f"{name} 429")

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        raise self._exc
        yield  # make it a generator


class _YieldsErrorDict(BaseProvider):
    """Yields an error-type dict — chain must raise ProviderError on it."""

    def __init__(self, name: str, message: str = "stream broke") -> None:
        self._name = name
        self.calls = 0
        self._message = message

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        yield {"type": "error", "error": {"message": self._message}}


class _SequentialProvider(BaseProvider):
    """Yields items in the given sequence."""

    def __init__(self, name: str, items: list[Any]) -> None:
        self._name = name
        self._items = items
        self.calls = 0

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        for item in self._items:
            yield item


async def _collect(chain: ProviderChain, **kwargs: Any) -> list[Any]:
    result: list[Any] = []
    async for msg in chain.run(**kwargs):
        result.append(msg)
    return result


# ── non-recoverable errors ────────────────────────────────────────────────────


class TestNonRecoverableErrors:
    async def test_value_error_propagates_immediately(self) -> None:
        """ValueError is non-recoverable — chain must re-raise without retry."""
        prov = _AlwaysFailing("A", exc=ValueError("bad input"))
        prov_b = _OkProvider("B")
        chain = ProviderChain([prov, prov_b], rate_limit_max_retries=3, rate_limit_wait_s=0.0)

        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            with pytest.raises(ValueError, match="bad input"):
                await _collect(chain, prompt="x")

        assert prov.calls == 1, "non-recoverable error must not trigger retries"
        assert prov_b.calls == 0, "fallback must not be tried after non-recoverable error"

    async def test_runtime_error_propagates_immediately(self) -> None:
        """RuntimeError is not in the recoverable set — propagates straight up."""
        prov = _AlwaysFailing("A", exc=RuntimeError("crash"))
        chain = ProviderChain([prov], rate_limit_max_retries=5, rate_limit_wait_s=0.0)

        with pytest.raises(RuntimeError, match="crash"):
            await _collect(chain, prompt="x")

        assert prov.calls == 1

    async def test_provider_error_is_recoverable(self) -> None:
        """ProviderError IS recoverable — chain retries then falls back."""
        prov_a = _AlwaysFailing("A", exc=ProviderError("provider died"))
        prov_b = _OkProvider("B")

        chain = ProviderChain(
            [prov_a, prov_b], rate_limit_max_retries=0, rate_limit_wait_s=0.0
        )

        result = await _collect(chain, prompt="x")
        assert result == ["ok"]
        assert prov_b.calls == 1

    async def test_timeout_error_is_recoverable(self) -> None:
        """ProviderTimeoutError is recoverable — falls back to next provider."""
        prov_a = _AlwaysFailing("A", exc=ProviderTimeoutError("A", 30.0))
        prov_b = _OkProvider("B")

        chain = ProviderChain(
            [prov_a, prov_b], rate_limit_max_retries=0, rate_limit_wait_s=0.0
        )

        result = await _collect(chain, prompt="x")
        assert result == ["ok"]


# ── stream error-dict events ──────────────────────────────────────────────────


class TestStreamErrorDictEvents:
    async def test_error_dict_raises_rate_limit_after_exhaustion(self) -> None:
        """A yielded {"type": "error"} event is treated as ProviderError (recoverable).

        After all retries are exhausted the chain surfaces a RateLimitError,
        not the original ProviderError message — the error dict is an internal
        signal that triggers the retry loop, not a hard-fail.
        """
        prov = _YieldsErrorDict("A", message="rate limited by upstream")
        chain = ProviderChain([prov], rate_limit_max_retries=0, rate_limit_wait_s=0.0)

        # RateLimitError is a subclass of ProviderError, so catching ProviderError works
        with pytest.raises(RateLimitError, match="all providers in chain exhausted"):
            await _collect(chain, prompt="x")

    async def test_error_dict_with_data_message(self) -> None:
        """Error dict with nested error.data.message structure triggers retry/failover.

        The chain extracts the message from either error.message or
        error.data.message and raises ProviderError, which is recoverable,
        causing fallback to the next provider.
        """
        prov_a = _SequentialProvider(
            "A",
            [{"type": "error", "error": {"data": {"message": "nested error msg"}}}],
        )
        prov_b = _OkProvider("B")
        chain = ProviderChain(
            [prov_a, prov_b], rate_limit_max_retries=0, rate_limit_wait_s=0.0
        )

        result = await _collect(chain, prompt="x")
        # After error on A, chain falls back to B
        assert result == ["ok"]
        assert prov_a.calls == 1, "provider A must have been tried exactly once"
        assert prov_b.calls == 1, "provider B must have been used as the fallback"

    async def test_error_dict_triggers_fallback_after_retries(self) -> None:
        """Stream error events are treated as ProviderError → retries then fallback."""
        prov_a = _YieldsErrorDict("A", message="boom")
        prov_b = _OkProvider("B")

        chain = ProviderChain(
            [prov_a, prov_b], rate_limit_max_retries=1, rate_limit_wait_s=0.0
        )

        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            result = await _collect(chain, prompt="x")

        assert result == ["ok"]
        assert prov_a.calls == 2, "error dict treated as recoverable: 1 initial + 1 retry"

    async def test_non_error_dict_not_affected(self) -> None:
        """Non-error dicts are yielded normally."""
        prov = _SequentialProvider("A", [{"type": "text", "content": "hello"}])
        chain = ProviderChain([prov])
        result = await _collect(chain, prompt="x")
        assert result == [{"type": "text", "content": "hello"}]


# ── current_provider_index tracking ──────────────────────────────────────────


class TestCurrentProviderIndex:
    async def test_starts_at_zero(self) -> None:
        prov = _OkProvider("A")
        chain = ProviderChain([prov])
        assert chain.current_provider_index == 0

    async def test_index_zero_when_first_provider_succeeds(self) -> None:
        prov_a = _OkProvider("A")
        prov_b = _OkProvider("B")
        chain = ProviderChain([prov_a, prov_b])
        await _collect(chain, prompt="x")
        assert chain.current_provider_index == 0

    async def test_index_advances_on_fallback(self) -> None:
        """When A is exhausted and B succeeds, index must be 1."""
        prov_a = _AlwaysFailing("A")
        prov_b = _OkProvider("B")

        chain = ProviderChain(
            [prov_a, prov_b], rate_limit_max_retries=0, rate_limit_wait_s=0.0
        )

        await _collect(chain, prompt="x")
        assert chain.current_provider_index == 1

    async def test_index_advances_to_third_on_double_fallback(self) -> None:
        """A and B both exhausted — C succeeds, index must be 2."""
        prov_a = _AlwaysFailing("A")
        prov_b = _AlwaysFailing("B")
        prov_c = _OkProvider("C")

        chain = ProviderChain(
            [prov_a, prov_b, prov_c], rate_limit_max_retries=0, rate_limit_wait_s=0.0
        )

        await _collect(chain, prompt="x")
        assert chain.current_provider_index == 2

    async def test_index_when_circuit_skips_first(self) -> None:
        """When A's circuit is open it is skipped; B succeeds at index 1."""
        cb_registry = CircuitBreakerRegistry(failure_threshold=1)
        cb_registry.get("A").record_failure()  # open immediately

        prov_a = _OkProvider("A")  # would succeed but is skipped
        prov_b = _OkProvider("B")

        chain = ProviderChain([prov_a, prov_b], cb_registry=cb_registry)
        await _collect(chain, prompt="x")

        assert chain.current_provider_index == 1
        assert prov_a.calls == 0


# ── empty provider list ───────────────────────────────────────────────────────


class TestEmptyProviderList:
    async def test_empty_list_raises_circuit_open_error(self) -> None:
        """No providers → all providers circuit-broken branch raises CircuitOpenError."""
        chain = ProviderChain([])
        with pytest.raises(CircuitOpenError, match="all providers circuit-broken"):
            await _collect(chain, prompt="x")


# ── run_prompt delegation ─────────────────────────────────────────────────────


class TestRunPromptDelegation:
    async def test_run_prompt_forwards_prompt_kwarg(self) -> None:
        """run_prompt(p) must call run(prompt=p) and yield the same events."""
        received_kwargs: list[dict] = []

        class _CapturingProvider(BaseProvider):
            @property
            def display_name(self) -> str:
                return "cap"

            async def run(self, **kwargs: Any):
                received_kwargs.append(dict(kwargs))
                yield "captured"

        chain = ProviderChain([_CapturingProvider()])
        result: list[Any] = []
        async for msg in chain.run_prompt("hello-prompt"):
            result.append(msg)

        assert result == ["captured"]
        assert received_kwargs == [{"prompt": "hello-prompt"}]

    async def test_run_prompt_collected_joins_parts(self) -> None:
        """run_prompt_collected returns all yielded strings joined by newlines."""
        prov = _SequentialProvider("A", ["line1", "line2", "line3"])
        chain = ProviderChain([prov])
        result = await chain.run_prompt_collected("q")
        assert result == "line1\nline2\nline3"

    async def test_run_prompt_collected_skips_empty_content(self) -> None:
        """Dicts with empty content/text fields produce no parts."""
        prov = _SequentialProvider("A", [{"content": ""}, {"text": ""}, "real"])
        chain = ProviderChain([prov])
        result = await chain.run_prompt_collected("q")
        assert result == "real"


# ── open-circuit provider skipped without retries ─────────────────────────────


class TestCircuitOpenSkip:
    async def test_open_provider_consumes_no_retries(self) -> None:
        """A provider whose circuit is OPEN is skipped — no sleep() is called for it."""
        cb_registry = CircuitBreakerRegistry(failure_threshold=1)
        cb_registry.get("A").record_failure()  # open A

        prov_a = _OkProvider("A")
        prov_b = _OkProvider("B")

        sleep_calls: list[float] = []

        async def fake_sleep(t: float) -> None:
            sleep_calls.append(t)

        chain = ProviderChain(
            [prov_a, prov_b],
            cb_registry=cb_registry,
            rate_limit_max_retries=3,
            rate_limit_wait_s=1.0,
        )

        with patch("tero2.providers.chain.asyncio.sleep", fake_sleep):
            result = await _collect(chain, prompt="x")

        assert result == ["ok"]
        assert prov_a.calls == 0
        assert sleep_calls == [], "skipped provider must not trigger any sleep calls"

    async def test_all_circuits_open_raises_circuit_open_error(self) -> None:
        """When every provider's CB is open, chain raises CircuitOpenError."""
        cb_registry = CircuitBreakerRegistry(failure_threshold=1)
        cb_registry.get("A").record_failure()
        cb_registry.get("B").record_failure()

        prov_a = _OkProvider("A")
        prov_b = _OkProvider("B")

        chain = ProviderChain([prov_a, prov_b], cb_registry=cb_registry)
        with pytest.raises(CircuitOpenError, match="all providers circuit-broken"):
            await _collect(chain, prompt="x")


# ── yield-aware retry policy ──────────────────────────────────────────────────


class _ScriptedProvider(BaseProvider):
    """Yields items from a script; optionally raises at a given index.

    ``raise_at`` is the loop iteration at which the error fires:
    - raise_at=0: raises before yielding any item (pre-yield).
    - raise_at=N (N > 0): yields N items, then raises.
    - raise_at >= len(script): yields all items, then raises after the loop.
    """

    def __init__(
        self,
        name: str,
        script: list[Any],
        raise_at: int | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._name = name
        self._script = script
        self._raise_at = raise_at
        self._raise_exc = raise_exc or RateLimitError("scripted")
        self.calls = 0

    @property
    def display_name(self) -> str:
        return self._name

    async def run(self, **kwargs: Any):
        self.calls += 1
        for i, item in enumerate(self._script):
            if self._raise_at is not None and i == self._raise_at:
                raise self._raise_exc
            yield item
        # Raise after the loop when raise_at targets a position beyond the script.
        if self._raise_at is not None and self._raise_at >= len(self._script):
            raise self._raise_exc


def _scripted_chain(*providers: BaseProvider, retries: int = 2) -> ProviderChain:
    return ProviderChain(
        list(providers),
        cb_registry=CircuitBreakerRegistry(),
        rate_limit_max_retries=retries,
        rate_limit_wait_s=0.0,
    )


class TestYieldAwareRetryPolicy:
    async def test_retry_before_first_yield_succeeds(self) -> None:
        """Error before any yield is pre-yield → retry/failover allowed."""
        failing = _ScriptedProvider(
            "a",
            [{"type": "text", "text": "x"}],
            raise_at=0,
            raise_exc=RateLimitError("rl"),
        )
        good = _ScriptedProvider("b", [{"type": "text", "text": "ok"}])
        chain = _scripted_chain(failing, good, retries=1)

        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            out = await _collect(chain, prompt="hi")

        assert out == [{"type": "text", "text": "ok"}]
        assert good.calls == 1

    async def test_error_after_first_yield_is_hard_fail(self) -> None:
        """Recoverable error AFTER first yield must hard-fail — no retry, no fallback."""
        mid_fail = _ScriptedProvider(
            "a",
            [{"type": "text", "text": "partial"}],
            raise_at=1,
            raise_exc=RateLimitError("mid"),
        )
        good = _ScriptedProvider("b", [{"type": "text", "text": "ok"}])
        chain = _scripted_chain(mid_fail, good, retries=3)

        with pytest.raises(RateLimitError):
            await _collect(chain, prompt="hi")

        assert good.calls == 0, "fallback must not be tried after post-yield error"

    async def test_first_msg_error_dict_triggers_retry(self) -> None:
        """An error-dict as the very first message is treated as pre-yield → retry/failover."""
        error_first = _ScriptedProvider(
            "a", [{"type": "error", "error": {"message": "rate limit"}}]
        )
        good = _ScriptedProvider("b", [{"type": "text", "text": "ok"}])
        chain = _scripted_chain(error_first, good, retries=1)

        with patch("tero2.providers.chain.asyncio.sleep", AsyncMock()):
            out = await _collect(chain, prompt="hi")

        assert out == [{"type": "text", "text": "ok"}]
        assert good.calls == 1

    async def test_mid_stream_error_dict_passes_through(self) -> None:
        """An error-dict that arrives AFTER normal messages is yielded as-is (normalizer handles)."""
        mixed = _ScriptedProvider(
            "a",
            [
                {"type": "text", "text": "hello"},
                {"type": "error", "error": {"message": "oops"}},
            ],
        )
        chain = _scripted_chain(mixed, retries=0)
        out = await _collect(chain, prompt="hi")

        assert out == [
            {"type": "text", "text": "hello"},
            {"type": "error", "error": {"message": "oops"}},
        ]
