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
