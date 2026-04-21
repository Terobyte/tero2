"""Provider chain with circuit breaker integration."""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.constants import RATE_LIMIT_MAX_RETRIES, RATE_LIMIT_WAIT_S
from tero2.errors import (
    CircuitOpenError,
    ProviderError,
    ProviderNotReadyError,
    ProviderTimeoutError,
    RateLimitError,
)
from tero2.providers.base import BaseProvider


def _is_recoverable_error(exc: BaseException) -> bool:
    return isinstance(
        exc, (ProviderError, RateLimitError, ProviderTimeoutError, ProviderNotReadyError)
    )


# Context window sizes for known models (same table as zai.py).
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "glm": 128_000,
    "deepseek": 128_000,
    "qwen": 128_000,
    "mimo": 128_000,
    "claude": 200_000,
    "sonnet": 200_000,
    "opus": 200_000,
    "haiku": 200_000,
    "gpt-4": 128_000,
    "gemini": 1_000_000,
}


def get_model_context_limit(model: str) -> int:
    """Return context window size for a model string. Default: 128_000."""
    model_lower = model.lower()
    for key, limit in _MODEL_CONTEXT_WINDOWS.items():
        if key in model_lower:
            return limit
    return 128_000


class ProviderChain:
    def __init__(
        self,
        providers: list[BaseProvider],
        cb_registry: CircuitBreakerRegistry | None = None,
        rate_limit_max_retries: int = RATE_LIMIT_MAX_RETRIES,
        rate_limit_wait_s: float = RATE_LIMIT_WAIT_S,
    ) -> None:
        self.providers = providers
        self.cb_registry = cb_registry or CircuitBreakerRegistry()
        self._rate_limit_max_retries = rate_limit_max_retries
        self._rate_limit_wait_s = rate_limit_wait_s
        self._current_provider_index: int = 0

    @property
    def current_provider_index(self) -> int:
        return self._current_provider_index

    async def run(self, **kwargs: Any) -> AsyncGenerator[Any, None]:
        any_attempted = False
        for idx, provider in enumerate(self.providers):
            cb = self.cb_registry.get(provider.display_name)
            if not cb.is_available:
                continue
            any_attempted = True
            self._current_provider_index = idx

            # Per-provider retry loop: attempt 0 = initial call,
            # attempts 1..rate_limit_max_retries = backoff retries.
            # Circuit breaker records exactly ONE failure when ALL retries are
            # exhausted, not per-attempt — so retries are transparent to the CB.
            for attempt in range(self._rate_limit_max_retries + 1):
                if attempt > 0:
                    wait = min(
                        self._rate_limit_wait_s * (2.0 ** (attempt - 1)),
                        300.0,
                    )
                    jitter = random.uniform(0, self._rate_limit_wait_s * 0.1)
                    await asyncio.sleep(wait + jitter)

                try:
                    messages: list[Any] = []
                    async with aclosing(provider.run(**kwargs)) as stream:
                        async for msg in stream:
                            if isinstance(msg, dict) and msg.get("type") == "error":
                                error_data = msg.get("error", {})
                                error_msg = (
                                    error_data.get("message", "")
                                    or (error_data.get("data") or {}).get("message", "")
                                    or str(error_data)
                                )
                                raise ProviderError(error_msg or "stream error event")
                            messages.append(msg)
                    cb.record_success()
                    for msg in messages:
                        yield msg
                    return
                except Exception as exc:
                    if not _is_recoverable_error(exc):
                        raise
                    # Inner loop continues to next attempt.
            else:
                # All retries for this provider exhausted — count as one CB failure.
                cb.record_failure()

        if not any_attempted:
            raise CircuitOpenError("all providers circuit-broken")
        raise RateLimitError("all providers in chain exhausted")

    async def run_prompt(self, prompt: str) -> AsyncGenerator[Any, None]:
        async for msg in self.run(prompt=prompt):
            yield msg

    async def run_prompt_collected(self, prompt: str) -> str:
        """Send a single assembled prompt and return the full response as a string.

        Used by plan_hardening, builder, verifier -- all of which assemble
        a single prompt document via assemble_context() and expect a string back.

        Internally calls run_prompt() (AsyncGenerator) and collects all text content.
        """
        parts: list[str] = []
        async for msg in self.run_prompt(prompt):
            if isinstance(msg, str):
                parts.append(msg)
            elif isinstance(msg, dict):
                content = msg.get("content", "") or msg.get("text", "")
                if content:
                    parts.append(str(content))
            else:
                # Object with .content or .text attribute
                text = getattr(msg, "content", None) or getattr(msg, "text", None)
                if text:
                    parts.append(str(text))
        return "\n".join(parts)
