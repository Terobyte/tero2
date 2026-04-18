"""Regression tests: post-ControlsPanel migration DOM structure.

Acceptance criteria (after ControlsPanel → Header + Footer migration):
  - #controls is not present in the DOM
  - #stuck-hint StuckHintWidget is present and hidden by default
  - Textual Header and Footer are mounted
  - A 'stuck' event causes StuckHintWidget.display to flip True
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.widgets import Footer, Header

from tero2.tui.app import DashboardApp
from tero2.tui.widgets.stuck_hint import StuckHintWidget


def _make_app():
    """Return (app, event_queue) with a MagicMock dispatcher.

    dispatcher.subscribe.return_value is set to a real asyncio.Queue so
    on_mount stores the pre-created queue in self._event_queue — the same
    object the test can push events to after launch.
    """
    runner = MagicMock()
    runner.config.roles = {"builder": MagicMock()}
    runner.run = AsyncMock()
    runner.project_path = None
    dispatcher = MagicMock()
    event_queue: asyncio.Queue = asyncio.Queue()
    dispatcher.subscribe.return_value = event_queue
    cq: asyncio.Queue = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=cq)
    return app, event_queue


@pytest.mark.asyncio
async def test_no_controls_panel_in_dom() -> None:
    """ControlsPanel must not exist — NoMatches would crash old code."""
    from textual.css.query import NoMatches

    app, _ = _make_app()
    async with app.run_test(headless=True) as pilot:
        with pytest.raises(NoMatches):
            app.query_one("#controls")


@pytest.mark.asyncio
async def test_stuck_hint_exists_and_hidden() -> None:
    """StuckHintWidget is in the DOM and display=False on mount."""
    app, _ = _make_app()
    async with app.run_test(headless=True) as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is False


@pytest.mark.asyncio
async def test_header_and_footer_exist() -> None:
    """Textual Header and Footer are mounted after ControlsPanel removal."""
    app, _ = _make_app()
    async with app.run_test(headless=True) as pilot:
        app.query_one(Header)   # raises NoMatches if absent
        app.query_one(Footer)   # raises NoMatches if absent


@pytest.mark.asyncio
async def test_stuck_hint_shown_after_stuck_event() -> None:
    """Emit a 'stuck' event via the subscribed queue → _consume_events flips display."""
    from tero2.events import make_event

    app, event_queue = _make_app()
    async with app.run_test(headless=True) as pilot:
        # role is str (not None) — matches make_event signature
        event = make_event("stuck", role="", data={})
        await event_queue.put(event)
        await pilot.pause(0.2)
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is True, "StuckHintWidget.display should be True after 'stuck' event"
