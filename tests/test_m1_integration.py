"""M1 integration: tero2 go paths work end-to-end."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_tero2_go_without_args_does_not_crash(tmp_path):
    """tero2 go without args → wizard path → sys.exit(0) on cancel (no TypeError)."""
    with patch("tero2.cli.run_startup_wizard", return_value=None) as mock_wiz:
        from tero2.cli import cmd_go
        import types
        args = types.SimpleNamespace(
            project_path=None, plan=None, config=None, idle_timeout=0, verbose=False
        )
        import sys
        with pytest.raises(SystemExit) as exc:
            cmd_go(args)
        assert exc.value.code == 0


def test_tero2_go_with_path_skips_wizard(tmp_path):
    """tero2 go <path> does NOT call run_startup_wizard."""
    with patch("tero2.cli.run_startup_wizard") as mock_wiz:
        with patch("tero2.tui.app.DashboardApp") as MockApp:
            MockApp.return_value.run = MagicMock()
            with patch("tero2.runner.Runner"), patch("tero2.events.EventDispatcher"), \
                 patch("tero2.cli.record_run"):
                from tero2.cli import cmd_go
                import types
                args = types.SimpleNamespace(
                    project_path=str(tmp_path),
                    plan=None, config=None, idle_timeout=0, verbose=False
                )
                cmd_go(args)
        mock_wiz.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_app_no_controls_panel():
    """DashboardApp must not reference #controls anywhere — would raise NoMatches."""
    from tero2.tui.app import DashboardApp
    from tero2.tui.widgets.stuck_hint import StuckHintWidget
    from textual.css.query import NoMatches

    runner = MagicMock()
    runner.config.roles = {}
    runner.run = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.subscribe.return_value = asyncio.Queue()
    cq = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=cq)

    async with app.run_test(headless=True) as pilot:
        with pytest.raises(NoMatches):
            app.query_one("#controls")
        app.query_one("#stuck-hint", StuckHintWidget)  # must exist
