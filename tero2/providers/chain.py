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

    @property
    def provider_kind(self) -> str:
        """Canonical provider_kind of the currently-active provider.

        Used by ``BasePlayer._run_prompt`` and ``RunnerContext.run_agent``
        to dispatch to the correct stream normalizer. Failover updates
        ``_current_provider_index`` so this tracks live.
        """
        if not self.providers:
            return ""
        idx = min(self._current_provider_index, len(self.providers) - 1)
        return getattr(self.providers[idx], "kind", "")

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
            for attempt in range(self._rate_limit_max_retries + 1):
                if attempt > 0:
                    wait = min(
                        self._rate_limit_wait_s * (2.0 ** (attempt - 1)),
                        300.0,
                    )
                    jitter = random.uniform(0, self._rate_limit_wait_s * 0.1)
                    await asyncio.sleep(wait + jitter)

                # Track whether we have yielded any message to the caller.
                # Error before first yield → retry/failover (safe, nothing sent yet).
                # Error after first yield → hard-fail (can't retry a partial stream).
                yielded_anything = False
                try:
                    async with aclosing(provider.run(**kwargs)) as stream:
                        async for msg in stream:
                            # First message is an error-dict → treat as pre-yield
                            # stream failure so the retry/failover path applies.
                            if (
                                not yielded_anything
                                and isinstance(msg, dict)
                                and msg.get("type") == "error"
                            ):
                                error_data = msg.get("error") or {}
                                error_msg = (
                                    error_data.get("message", "")
                                    if isinstance(error_data, dict)
                                    else str(error_data)
                                ) or "stream error"
                                raise ProviderError(error_msg)
                            yielded_anything = True
                            yield msg
                    cb.record_success()
                    return
                except Exception as exc:
                    if not _is_recoverable_error(exc):
                        # Non-recoverable: hard-fail immediately, no retry.
                        cb.record_failure()
                        raise
                    if yielded_anything:
                        # Recoverable but partial stream already sent: hard-fail.
                        cb.record_failure()
                        raise
                    # Recoverable and nothing yielded yet: retry this provider.
                    if attempt >= self._rate_limit_max_retries:
                        # Exhausted retries for this provider — record failure,
                        # then try next provider in the outer loop.
                        cb.record_failure()
                        break

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
