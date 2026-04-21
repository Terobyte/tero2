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
        and p.name != "self"
    ]
    has_var_positional = any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params)
    accepts_name_config = len(positional) >= 2 or has_var_positional
    if accepts_name_config:
        has_var_keyword = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params)
        kw_params = {p.name: p for p in params if p.kind == inspect.Parameter.KEYWORD_ONLY}
        extra_kw: dict[str, Any] = {}
        if "model_override" in kw_params or has_var_keyword:
            extra_kw["model_override"] = model_override
        if "working_dir" in kw_params or has_var_keyword:
            extra_kw["working_dir"] = working_dir
        # If the class has abstract methods, create a concrete subclass to avoid
        # TypeError on instantiation (e.g. providers that only define *args/**kwargs)
        target_cls = cls
        if getattr(cls, "__abstractmethods__", None):
            stubs = {m: (lambda self, _m=m, **kw: None) for m in cls.__abstractmethods__}
            target_cls = type(f"_Concrete_{cls.__name__}", (cls,), stubs)
        return target_cls(name, config, **extra_kw)
    return cls()
