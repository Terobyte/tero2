"""Tests for DashboardApp command processing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from tero2.events import Command, EventDispatcher
from tero2.tui.app import DashboardApp
from tero2.tui.screens.role_swap import SwitchProviderMessage
from tero2.tui.screens.steer import SteerMessage


def _make_app() -> tuple[DashboardApp, asyncio.Queue[Command]]:
    """Return a DashboardApp with a mocked Runner and fresh command queue."""
    runner = MagicMock()
    runner.run = AsyncMock()
    runner.config.roles = {"scout": MagicMock(), "execute": MagicMock()}

    dispatcher = EventDispatcher()
    command_queue: asyncio.Queue[Command] = asyncio.Queue()

    app = DashboardApp(
        runner=runner,
        dispatcher=dispatcher,
        command_queue=command_queue,
    )
    return app, command_queue


@pytest.mark.asyncio
async def test_pause_sends_command() -> None:
    """action_pause() puts Command('pause') in command_queue."""
    app, q = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.press("p")
        await pilot.pause()

    assert not q.empty()
    cmd = q.get_nowait()
    assert cmd.kind == "pause"
    assert cmd.source == "tui"


@pytest.mark.asyncio
async def test_skip_sends_command() -> None:
    """action_skip() puts Command('skip_task') in command_queue."""
    app, q = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        await pilot.press("k")
        await pilot.pause()

    assert not q.empty()
    cmd = q.get_nowait()
    assert cmd.kind == "skip_task"
    assert cmd.source == "tui"


@pytest.mark.asyncio
async def test_stuck_option_clears_stuck_mode() -> None:
    """action_stuck_option_1() puts steer command and clears stuck_mode."""
    app, q = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        # manually set stuck_mode on pipeline and show stuck hint
        from tero2.tui.widgets.pipeline import PipelinePanel
        from tero2.tui.widgets.stuck_hint import StuckHintWidget

        pipeline = app.query_one("#pipeline", PipelinePanel)
        stuck_hint = app.query_one("#stuck-hint", StuckHintWidget)
        pipeline.stuck_mode = True
        stuck_hint.display = True

        await pilot.press("1")
        await pilot.pause()

        # stuck_mode should be cleared
        assert pipeline.stuck_mode is False
        assert stuck_hint.display is False

    assert not q.empty()
    cmd = q.get_nowait()
    assert cmd.kind == "steer"
    assert cmd.data == {"text": "stuck_option_1"}
    assert cmd.source == "tui"


@pytest.mark.asyncio
async def test_switch_provider_command() -> None:
    """on_switch_provider_message sends switch_provider command."""
    app, q = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        app.post_message(SwitchProviderMessage(role="scout", provider="opencode"))
        await pilot.pause()

    assert not q.empty()
    cmd = q.get_nowait()
    assert cmd.kind == "switch_provider"
    assert cmd.data == {"role": "scout", "provider": "opencode"}
    assert cmd.source == "tui"


@pytest.mark.asyncio
async def test_steer_command() -> None:
    """on_steer_message sends steer command."""
    app, q = _make_app()
    async with app.run_test(headless=True, size=(120, 30)) as pilot:
        app.post_message(SteerMessage(text="focus on tests"))
        await pilot.pause()

    assert not q.empty()
    cmd = q.get_nowait()
    assert cmd.kind == "steer"
    assert cmd.data == {"text": "focus on tests"}
    assert cmd.source == "tui"
