import pytest
from textual.app import App
from textual.widgets import Label

from tero2.providers.catalog import ModelEntry
from tero2.tui.screens.model_pick import ModelPickScreen


_FAKE_MODELS = [
    ModelEntry(id="opus", label="Claude Opus"),
    ModelEntry(id="sonnet", label="Claude Sonnet"),
    ModelEntry(id="haiku", label="Claude Haiku"),
]


@pytest.mark.asyncio
async def test_model_pick_shows_entries():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        assert len(items) == 3


@pytest.mark.asyncio
async def test_model_pick_filter_reduces_list():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        await pilot.click("#model-search")
        for ch in "opus":
            await pilot.press(ch)
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        assert len(items) == 1


@pytest.mark.asyncio
async def test_model_pick_escape_returns_none():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert results == [None]


@pytest.mark.asyncio
async def test_model_pick_enter_returns_entry():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(results) == 1
        assert results[0] is not None
        assert isinstance(results[0], ModelEntry)
