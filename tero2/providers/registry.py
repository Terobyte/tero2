"""Provider registry and factory."""

from __future__ import annotations

import inspect
from typing import Any

from tero2.config import Config
from tero2.providers.base import BaseProvider

_REGISTRY: dict[str, type[BaseProvider]] = {}


def register(name: str, cls: type[BaseProvider]) -> None:
    _REGISTRY[name] = cls


def create_provider(
    name: str,
    config: Config | None = None,
    *,
    model_override: str = "",
    working_dir: str = "",
) -> BaseProvider:
    if name not in _REGISTRY:
        raise ValueError(f"unknown provider: {name}")
    cls = _REGISTRY[name]
    sig = inspect.signature(cls.__init__)
    params = list(sig.parameters.values())
    positional = [
        p
        for p in params
        if p.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    ]
    accepts_name_config = len(positional) >= 3
    if accepts_name_config:
        kw_params = {p.name: p for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY}
        extra_kw: dict[str, Any] = {}
        if "model_override" in kw_params:
            extra_kw["model_override"] = model_override
        if "working_dir" in kw_params:
            extra_kw["working_dir"] = working_dir
        return cls(name, config, **extra_kw)
    return cls()
