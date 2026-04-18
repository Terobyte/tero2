import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_go_parser_allows_no_project_path():
    """project_path must be optional (nargs='?')."""
    from tero2.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["go"])
    assert args.project_path is None


def test_go_parser_still_accepts_path(tmp_path):
    from tero2.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["go", str(tmp_path)])
    assert args.project_path == str(tmp_path)


def test_cmd_go_records_history_on_launch(tmp_path, monkeypatch):
    """record_run is called after DashboardApp launches."""
    recorded = []

    monkeypatch.setattr("tero2.cli.record_run", lambda p, f: recorded.append((p, f)))

    with patch("tero2.tui.app.DashboardApp") as MockApp:
        MockApp.return_value.run = MagicMock()
        with patch("tero2.runner.Runner"), patch("tero2.events.EventDispatcher"):
            from tero2.cli import cmd_go
            args = MagicMock()
            args.project_path = str(tmp_path)
            args.plan = None
            args.config = None
            args.idle_timeout = 0
            args.verbose = False
            cmd_go(args)

    assert len(recorded) == 1
    assert recorded[0][0] == tmp_path


def test_cmd_go_calls_wizard_when_no_path(tmp_path):
    """When project_path is None, run_startup_wizard is called."""
    with patch("tero2.cli.run_startup_wizard") as mock_wizard:
        mock_wizard.return_value = None  # user cancelled
        from tero2.cli import cmd_go
        args = MagicMock()
        args.project_path = None
        args.plan = None
        args.config = None
        args.idle_timeout = 0
        args.verbose = False
        with pytest.raises(SystemExit) as exc:
            cmd_go(args)
        assert exc.value.code == 0
        mock_wizard.assert_called_once()
