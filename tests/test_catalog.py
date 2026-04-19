import pytest
from tero2.providers.catalog import (
    DEFAULT_PROVIDERS,
    STATIC_CATALOG,
    ModelEntry,
    get_models,
)


def test_model_entry_is_frozen():
    entry = ModelEntry(id="claude-sonnet", label="Claude Sonnet")
    with pytest.raises(Exception):
        entry.id = "other"  # frozen dataclass


def test_default_providers_includes_all():
    for p in ("claude", "codex", "opencode", "kilo", "zai", "gemma"):
        assert p in DEFAULT_PROVIDERS


def test_static_catalog_claude_has_expected_models():
    models = STATIC_CATALOG["claude"]
    ids = [m.id for m in models]
    assert any("sonnet" in i for i in ids)
    assert any("opus" in i for i in ids)


def test_static_catalog_codex_has_reasoning_options():
    models = STATIC_CATALOG["codex"]
    ids = [m.id for m in models]
    assert "" in ids        # medium (default)
    assert "gpt-5.4" in ids  # high


def test_static_catalog_zai_has_glm():
    models = STATIC_CATALOG["zai"]
    ids = [m.id for m in models]
    assert "glm-5.1" in ids


def test_static_catalog_gemma_is_empty():
    assert STATIC_CATALOG["gemma"] == []


@pytest.mark.asyncio
async def test_get_models_returns_static_for_claude():
    models = await get_models("claude")
    assert len(models) >= 2
    assert all(isinstance(m, ModelEntry) for m in models)


@pytest.mark.asyncio
async def test_get_models_returns_static_for_zai():
    models = await get_models("zai")
    assert any(m.id == "glm-5.1" for m in models)
