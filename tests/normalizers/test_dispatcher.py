"""Tests for the normalizer dispatcher (tero2.providers.normalizers).

The dispatcher owns the provider registry: ``register()`` / ``get_normalizer()``.
These tests validate:
  - All five built-in providers are pre-registered at import time.
  - ``get_normalizer(known)`` returns an object that satisfies StreamNormalizer.
  - ``get_normalizer(unknown)`` returns FallbackNormalizer — never None.
  - ``FallbackNormalizer`` emits a ``kind="status"`` event for every raw input.
  - Custom ``register()`` calls are visible to subsequent ``get_normalizer()``
    calls and do not corrupt the registry for existing providers.
  - The same ``FallbackNormalizer`` singleton is returned for multiple unknown keys.
"""

from __future__ import annotations

import pytest

# Importing the package triggers the side-effect imports that populate the registry.
from tero2.providers.normalizers import get_normalizer, register
from tero2.providers.normalizers.fallback import FallbackNormalizer


# ── Registry pre-population ───────────────────────────────────────────────────


@pytest.mark.parametrize("kind", ["claude", "codex", "opencode", "kilo", "zai"])
def test_builtin_provider_is_registered(kind: str) -> None:
    """Every built-in provider must be retrievable by its kind string."""
    n = get_normalizer(kind)
    # Must not be the fallback — it should be the provider-specific normalizer.
    assert not isinstance(n, FallbackNormalizer), (
        f"get_normalizer('{kind}') returned FallbackNormalizer — "
        f"provider was not registered"
    )


@pytest.mark.parametrize("kind", ["claude", "codex", "opencode", "kilo", "zai"])
def test_builtin_normalizer_has_normalize_method(kind: str) -> None:
    """Every registered normalizer must expose a callable ``normalize`` method."""
    n = get_normalizer(kind)
    assert callable(getattr(n, "normalize", None)), (
        f"Normalizer for '{kind}' is missing a callable normalize() method"
    )


# ── Fallback for unknown kinds ────────────────────────────────────────────────


def test_unknown_kind_returns_fallback() -> None:
    """get_normalizer with an unregistered key must return FallbackNormalizer."""
    n = get_normalizer("nonexistent_provider_xyz")
    assert isinstance(n, FallbackNormalizer)


def test_unknown_kind_never_returns_none() -> None:
    """get_normalizer must never return None — even for garbage input."""
    assert get_normalizer("") is not None
    assert get_normalizer("???") is not None
    assert get_normalizer("CLAUDE") is not None  # case-sensitive: uppercase ≠ "claude"


def test_multiple_unknown_kinds_return_same_singleton() -> None:
    """All unknown keys must return the same FallbackNormalizer singleton."""
    a = get_normalizer("no_such_a")
    b = get_normalizer("no_such_b")
    assert a is b, "Expected the same FallbackNormalizer singleton for all unknown keys"


def test_case_sensitive_lookup() -> None:
    """Registry lookup is case-sensitive: 'Claude' ≠ 'claude'."""
    assert isinstance(get_normalizer("Claude"), FallbackNormalizer)
    assert isinstance(get_normalizer("CODEX"), FallbackNormalizer)


# ── FallbackNormalizer behaviour ─────────────────────────────────────────────


def test_fallback_yields_status_for_dict() -> None:
    """FallbackNormalizer must emit exactly one kind='status' event for a dict."""
    fb = get_normalizer("unknown")
    out = list(fb.normalize({"type": "future_event", "data": 42}, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "status"


def test_fallback_yields_status_for_non_dict() -> None:
    """FallbackNormalizer must emit one kind='status' event for non-dict input."""
    fb = get_normalizer("unknown")
    out = list(fb.normalize("plain string", role="builder"))
    assert len(out) == 1
    assert out[0].kind == "status"


def test_fallback_yields_status_for_none() -> None:
    """FallbackNormalizer must not crash on None — yields one kind='status'."""
    fb = get_normalizer("unknown")
    out = list(fb.normalize(None, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "status"


def test_fallback_content_contains_repr() -> None:
    """FallbackNormalizer status content must contain a repr of the raw value."""
    fb = get_normalizer("unknown")
    out = list(fb.normalize({"x": 1}, role="builder"))
    assert "raw:" in out[0].content


def test_fallback_role_preserved() -> None:
    """FallbackNormalizer must attach the given role to emitted events."""
    fb = get_normalizer("unknown")
    out = list(fb.normalize({}, role="verifier"))
    assert out[0].role == "verifier"


def test_fallback_long_repr_truncated() -> None:
    """FallbackNormalizer must truncate very long repr to ≤ ~200 chars in content."""
    fb = get_normalizer("unknown")
    big = {"data": "x" * 1000}
    out = list(fb.normalize(big, role="builder"))
    # Content must not balloon — the raw repr is truncated to ~200 chars.
    assert len(out[0].content) < 300


# ── Custom register / isolation ───────────────────────────────────────────────


class _MinimalNormalizer:
    """Minimal StreamNormalizer for testing the registry."""

    def normalize(self, raw, role, now=None):  # type: ignore[override]
        from tero2.stream_bus import make_stream_event
        yield make_stream_event(role, "status", content="minimal", raw={})


def test_custom_register_visible_to_get_normalizer() -> None:
    """register() with a new key must make it retrievable via get_normalizer()."""
    register("test_custom_provider", _MinimalNormalizer())
    n = get_normalizer("test_custom_provider")
    assert not isinstance(n, FallbackNormalizer)
    events = list(n.normalize({}, role="builder"))
    assert events[0].content == "minimal"


def test_custom_register_does_not_corrupt_existing_providers() -> None:
    """Registering a new key must not affect existing provider registrations."""
    register("test_isolation_check", _MinimalNormalizer())
    # All built-ins must still be resolvable correctly.
    for kind in ("claude", "codex", "opencode", "kilo", "zai"):
        n = get_normalizer(kind)
        assert not isinstance(n, FallbackNormalizer), (
            f"'{kind}' was corrupted after registering a new key"
        )


def test_register_overwrites_existing_key() -> None:
    """register() for an existing key must replace, not stack."""
    first_instance = _MinimalNormalizer()
    second_instance = _MinimalNormalizer()
    register("test_overwrite", first_instance)
    register("test_overwrite", second_instance)
    retrieved = get_normalizer("test_overwrite")
    assert retrieved is second_instance, (
        "Expected the second registered instance to overwrite the first"
    )


# ── StreamNormalizer protocol satisfaction ────────────────────────────────────


@pytest.mark.parametrize("kind", ["claude", "codex", "opencode", "kilo", "zai"])
def test_builtin_normalizer_satisfies_protocol(kind: str) -> None:
    """Each built-in normalizer must structurally satisfy the StreamNormalizer protocol."""
    n = get_normalizer(kind)
    # The Protocol is structural — runtime_checkable would need @runtime_checkable.
    # We verify the shape manually instead.
    normalize_fn = getattr(n, "normalize", None)
    assert callable(normalize_fn), f"{kind} normalizer missing normalize()"
    import inspect
    sig = inspect.signature(normalize_fn)
    params = list(sig.parameters)
    # Must accept at least `raw` and `role`
    assert len(params) >= 2, (
        f"{kind}.normalize() has fewer than 2 parameters: {params}"
    )
