"""M2 integration: catalog, zai, model pick, command palette."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tero2.providers.catalog import ModelEntry, STATIC_CATALOG, get_models


@pytest.mark.asyncio
async def test_get_models_claude_static():
    models = await get_models("claude")
    assert len(models) >= 2
    assert all(isinstance(m, ModelEntry) for m in models)


@pytest.mark.asyncio
async def test_get_models_zai_has_glm():
    models = await get_models("zai")
    assert any(m.id == "glm-5.1" for m in models)


@pytest.mark.asyncio
async def test_get_models_uses_cache_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr("tero2.providers.catalog._CACHE_DIR", tmp_path)
    cache_file = tmp_path / "opencode_models.json"
    cache_file.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "entries": [{"id": "openrouter/anthropic/claude-opus", "label": "Claude Opus"}],
    }))

    with patch("tero2.providers.catalog.fetch_cli_models") as mock_fetch:
        models = await get_models("opencode")
        mock_fetch.assert_not_called()
        assert len(models) == 1


def test_zai_in_registry():
    from tero2.providers.registry import _REGISTRY
    assert "zai" in _REGISTRY


def test_all_imports_succeed():
    """Verify no import errors across M2 modules."""
    from tero2.providers.catalog import get_models, STATIC_CATALOG  # noqa
    from tero2.providers.zai import ZaiProvider  # noqa
    from tero2.tui.screens.model_pick import ModelPickScreen  # noqa
    from tero2.tui.commands import Tero2CommandProvider  # noqa
