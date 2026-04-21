"""Provider stream normalizer registry.

Each per-provider module (claude.py, codex.py, …) calls ``register()`` at
import time.  The dispatcher is wired in via side-effect imports at the bottom
of this file.

Protocol contract (see :class:`StreamNormalizer`):
- ``normalize(raw, role)`` → ``Iterable[StreamEvent]``
- One raw line may yield *zero or many* events (e.g. an assistant message with
  text + tool_use blocks yields two events).
- On parse failure: yield ONE ``StreamEvent(kind="error", …)``.
- Pure function — no I/O, no global state, no mutation of *raw*.
"""

from __future__ import annotations

from tero2.stream_bus import StreamEvent  # noqa: F401 – re-exported for tests
from tero2.providers.normalizers.base import StreamNormalizer
from tero2.providers.normalizers.fallback import FallbackNormalizer


# ── Registry ─────────────────────────────────────────────────────────────────

_FALLBACK = FallbackNormalizer()
_NORMALIZERS: dict[str, StreamNormalizer] = {}


def register(provider_kind: str, normalizer: StreamNormalizer) -> None:
    """Register *normalizer* under *provider_kind* (e.g. ``"claude"``)."""
    _NORMALIZERS[provider_kind] = normalizer


def get_normalizer(provider_kind: str) -> StreamNormalizer:
    """Return the normalizer for *provider_kind*, falling back to :class:`FallbackNormalizer`.

    NOT by display_name — display_name may be ``'ZAI (GLM-5.1)'``.
    Use the provider's internal kind key (e.g. ``'zai'``).
    Unknown kinds return the singleton :data:`_FALLBACK` rather than ``None``,
    so callers never need an ``if normalizer is None`` guard.
    """
    return _NORMALIZERS.get(provider_kind, _FALLBACK)


__all__ = ["StreamNormalizer", "FallbackNormalizer", "get_normalizer", "register"]


# ── Side-effect imports to populate the registry ─────────────────────────────

from tero2.providers.normalizers import claude     # noqa: F401,E402
from tero2.providers.normalizers import codex      # noqa: F401,E402
from tero2.providers.normalizers import opencode   # noqa: F401,E402
from tero2.providers.normalizers import kilo       # noqa: F401,E402
from tero2.providers.normalizers import zai        # noqa: F401,E402
