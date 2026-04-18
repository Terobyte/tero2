import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from textual.app import App

from tero2.tui.screens.settings import SettingsScreen


def _make_app():
    app = App()
    return app


@pytest.mark.asyncio
async def test_settings_screen_composes():
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=Path("/tmp/test_settings.toml"))
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        from textual.widgets import TabbedContent
        screen.query_one(TabbedContent)  # must exist


@pytest.mark.asyncio
async def test_settings_has_three_tabs():
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=Path("/tmp/test_settings.toml"))
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        from textual.widgets import Tab
        tabs = screen.query(Tab)
        assert len(tabs) >= 3


@pytest.mark.asyncio
async def test_settings_escape_dismisses(tmp_path):
    results = []
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=tmp_path / "config.toml")
        await app.push_screen(screen, results.append)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert len(results) == 1  # dismissed


@pytest.mark.asyncio
async def test_settings_save_writes_toml(tmp_path):
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=tmp_path / "config.toml")
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        await pilot.press("s")
        await pilot.pause(0.1)
        assert (tmp_path / "config.toml").exists()
