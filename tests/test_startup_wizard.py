from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App

from tero2.tui.screens.project_pick import ProjectPickScreen
from tero2.tui.screens.startup_wizard import StartupWizard


@pytest.mark.asyncio
async def test_project_pick_shows_history(tmp_path):
    from tero2.history import HistoryEntry
    entries = [
        HistoryEntry(
            path=str(tmp_path / "proj1"),
            name="proj1",
            last_run="2026-04-18T10:00:00+00:00",
            last_plan="plan.md",
            run_count=3,
        )
    ]
    (tmp_path / "proj1").mkdir()

    with patch("tero2.tui.screens.project_pick.load_history", return_value=entries):
        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = ProjectPickScreen()
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.1)
            items = screen.query("ListView ListItem")
            assert len(items) >= 1


@pytest.mark.asyncio
async def test_project_pick_marks_missing_dir(tmp_path):
    from tero2.history import HistoryEntry
    entries = [
        HistoryEntry(
            path=str(tmp_path / "nonexistent"),
            name="nonexistent",
            last_run="2026-04-18T10:00:00+00:00",
            last_plan=None,
            run_count=1,
        )
    ]
    with patch("tero2.tui.screens.project_pick.load_history", return_value=entries):
        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = ProjectPickScreen()
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.1)
            warnings = screen.query(".entry-warning")
            assert len(warnings) >= 1


@pytest.mark.asyncio
async def test_project_pick_escape_returns_none(tmp_path):
    results = []
    with patch("tero2.tui.screens.project_pick.load_history", return_value=[]):
        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = ProjectPickScreen()
            await app.push_screen(screen, results.append)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert results == [None]


@pytest.mark.asyncio
async def test_startup_wizard_composes():
    with patch("tero2.tui.screens.project_pick.load_history", return_value=[]):
        app = App()
        async with app.run_test(headless=True) as pilot:
            wiz = StartupWizard()
            await app.push_screen(wiz, lambda x: None)
            await pilot.pause(0.1)
            # wizard shows ProjectPickScreen as first step
            assert len(app.screen_stack) >= 2
