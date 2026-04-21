"""Base provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    @abstractmethod
    async def run(self, **kwargs: Any) -> Any: ...

    @property
    def kind(self) -> str:
        """Canonical short name for normalizer dispatch (e.g. ``"claude"``).

        Distinct from :attr:`display_name` which may contain model tags.
        """
        return getattr(self, "_kind", "")

    @property
    def display_name(self) -> str:
        return self.__class__.__name__

    @property
    def command(self) -> str:
        return ""
