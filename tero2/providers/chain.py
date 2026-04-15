"""Provider chain with circuit breaker integration."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.errors import (
    ProviderError,
    ProviderNotReadyError,
    ProviderTimeoutError,
    RateLimitError,
)
from tero2.providers.base import BaseProvider


def _is_recoverable_error(exc: BaseException) -> bool:
    return isinstance(exc, (RateLimitError, ProviderTimeoutError, ProviderNotReadyError, ProviderError))


class ProviderChain:
    def __init__(
        self,
        providers: list[BaseProvider],
        cb_registry: CircuitBreakerRegistry | None = None,
    ) -> None:
        self.providers = providers
        self.cb_registry = cb_registry or CircuitBreakerRegistry()
        self._current_provider_index: int = 0

    @property
    def current_provider_index(self) -> int:
        return self._current_provider_index

    async def run(self, **kwargs: Any) -> AsyncGenerator[Any, None]:
        for idx, provider in enumerate(self.providers):
            self._current_provider_index = idx
            cb = self.cb_registry.get(provider.display_name)
            if not cb.is_available:
                continue
            try:
                buffer = []
                async for msg in provider.run(**kwargs):
                    buffer.append(msg)
                cb.record_success()
                for msg in buffer:
                    yield msg
                return
            except Exception as exc:
                if not _is_recoverable_error(exc):
                    raise
                cb.record_failure()
                continue
        raise RateLimitError("all providers in chain exhausted")

    async def run_prompt(self, prompt: str) -> AsyncGenerator[Any, None]:
        async for msg in self.run(prompt=prompt):
            yield msg
