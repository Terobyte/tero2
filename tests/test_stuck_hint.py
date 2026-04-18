"""Tests for StuckHintWidget.

Acceptance criteria:
  - Hidden by default (display=False)
  - Toggled visible via widget.display = True
  - Hint text contains "retry" and "switch" keywords
"""

from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from tero2.tui.widgets.stuck_hint import StuckHintWidget


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield StuckHintWidget(id="stuck-hint")


@pytest.mark.asyncio
async def test_stuck_hint_hidden_by_default():
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is False


@pytest.mark.asyncio
async def test_stuck_hint_shows_when_display_set():
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        widget.display = True
        assert widget.display is True


@pytest.mark.asyncio
async def test_stuck_hint_text_content():
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        rendered = widget.render()
        assert "retry" in str(rendered)
        assert "switch" in str(rendered)
