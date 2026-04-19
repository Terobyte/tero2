import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_command_provider_importable():
    from tero2.tui.commands import Tero2CommandProvider
    assert Tero2CommandProvider is not None


def test_dashboard_has_commands_class_var():
    from tero2.tui.app import DashboardApp
    from tero2.tui.commands import Tero2CommandProvider
    assert hasattr(DashboardApp, "COMMANDS")
    assert Tero2CommandProvider in DashboardApp.COMMANDS


@pytest.mark.asyncio
async def test_command_palette_opens_with_ctrl_p():
    from tero2.tui.app import DashboardApp
    runner = MagicMock()
    runner.config.roles = {}
    runner.run = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.subscribe.return_value = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=asyncio.Queue())
    async with app.run_test(headless=True) as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause(0.2)
        from textual.command import CommandPalette
        assert any(isinstance(s, CommandPalette) for s in app.screen_stack)
