"""Tests for ProviderChain.provider_kind property."""

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain


class _Fake(BaseProvider):
    def __init__(self, name):
        self._name = name
        self._kind = name

    @property
    def display_name(self):
        return f"{self._name} display"

    @property
    def kind(self):
        return self._kind

    async def run(self, **kw):
        yield {"type": "text", "text": "ok"}


def test_provider_kind_reflects_current():
    a = _Fake("claude")
    b = _Fake("zai")
    chain = ProviderChain([a, b], cb_registry=CircuitBreakerRegistry())
    assert chain.provider_kind == "claude"
    chain._current_provider_index = 1
    assert chain.provider_kind == "zai"


def test_provider_kind_empty_when_no_providers():
    chain = ProviderChain([], cb_registry=CircuitBreakerRegistry())
    assert chain.provider_kind == ""
