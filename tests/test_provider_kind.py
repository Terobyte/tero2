"""Failing tests for Task 13: BaseProvider.kind attribute.

These tests verify that each provider exposes a canonical ``kind`` string
used for normalizer dispatch.  They FAIL until Task 13 (Step 2) implements
the ``kind`` property on ``BaseProvider`` and sets ``_kind`` in each
``__init__``.
"""

from __future__ import annotations

from tero2.config import Config
from tero2.providers.registry import create_provider


def test_claude_provider_kind() -> None:
    p = create_provider("claude", Config())
    assert p.kind == "claude"


def test_zai_provider_kind() -> None:
    p = create_provider("zai", Config())
    assert p.kind == "zai"


def test_base_provider_default_kind_is_empty() -> None:
    from tero2.providers.base import BaseProvider

    class _Dummy(BaseProvider):
        async def run(self, **kwargs):  # type: ignore[override]
            if False:
                yield None

    assert _Dummy().kind == ""
