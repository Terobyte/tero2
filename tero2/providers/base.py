"""Base provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseProvider(ABC):
    @abstractmethod
    async def run(self, **kwargs: Any) -> Any: ...

    @property
    def display_name(self) -> str:
        return self.__class__.__name__

    @property
    def command(self) -> str:
        return ""
