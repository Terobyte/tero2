from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.app import App

from tero2.tui.screens.providers_pick import ProvidersPickScreen


@pytest.mark.asyncio
async def test_providers_pick_shows_roles():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ProvidersPickScreen(project_path=Path("/tmp/test-proj"))
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        # builder, architect, scout, verifier, coach = 5 default roles
        assert len(items) >= 4


@pytest.mark.asyncio
async def test_providers_pick_save_writes_config(tmp_path):
    results = []
    (tmp_path / ".sora").mkdir()
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ProvidersPickScreen(project_path=tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        await pilot.press("s")
        await pilot.pause(0.1)
        assert (tmp_path / ".sora" / "config.toml").exists()


@pytest.mark.asyncio
async def test_providers_pick_sora_invariant_blocks_save(tmp_path):
    """If builder present but architect/verifier missing, save must fail."""
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ProvidersPickScreen(project_path=tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        # Remove architect/verifier from screen state
        screen._roles = {"builder": ("claude", "sonnet")}
        await pilot.press("s")
        await pilot.pause(0.1)
        # Save should show error, not write file
        assert not (tmp_path / ".sora" / "config.toml").exists()
