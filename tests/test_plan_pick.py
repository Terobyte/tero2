"""Tests for PlanPickScreen — plan file selection modal (requirements.md Task 6)."""

from pathlib import Path

import pytest
from textual.app import App

from tero2.tui.screens.plan_pick import PlanPickScreen


@pytest.mark.asyncio
async def test_plan_pick_lists_md_files(tmp_path):
    (tmp_path / "plan.md").write_text("# plan")
    (tmp_path / "notes.md").write_text("# notes")
    (tmp_path / "other.txt").write_text("text")

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        assert len(items) >= 2


@pytest.mark.asyncio
async def test_plan_pick_skips_git_dir(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "plan.md").write_text("# hidden")
    (tmp_path / "real.md").write_text("# real")

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        # Inspect the cached file list stored on the screen instance.
        paths = [str(p) for p in screen._files]
        assert not any(".git" in p for p in paths)
        assert any("real.md" in p for p in paths)


@pytest.mark.asyncio
async def test_plan_pick_skips_sora_dir(tmp_path):
    sora = tmp_path / ".sora"
    sora.mkdir()
    (sora / "internal.md").write_text("# internal")
    (tmp_path / "plan.md").write_text("# plan")

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        paths = [str(p) for p in screen._files]
        assert not any(".sora" in p for p in paths)


@pytest.mark.asyncio
async def test_plan_pick_idle_mode_on_press_i(tmp_path):
    (tmp_path / "plan.md").write_text("# plan")
    results = []

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.press("i")
        await pilot.pause(0.1)
        assert results == [None]


@pytest.mark.asyncio
async def test_plan_pick_empty_dir_auto_idle(tmp_path):
    results = []

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.2)
        # auto-dismissed with None when no .md files
        assert results == [None]
