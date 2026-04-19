import sys
from unittest.mock import MagicMock, patch
import pytest


def test_cmd_telegram_exits_when_disabled():
    """tero2 telegram refuses to start when enabled=False."""
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock(spec=Config)
    cfg.telegram = TelegramConfig(enabled=False, bot_token="tok:ABC")

    with patch("tero2.config.load_config", return_value=cfg):
        from tero2.cli import cmd_telegram
        args = MagicMock()
        args.project = None
        args.verbose = False
        with pytest.raises(SystemExit) as exc:
            cmd_telegram(args)
        assert exc.value.code == 1


def test_cmd_telegram_exits_when_no_token():
    """tero2 telegram refuses when enabled=True but no token."""
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock(spec=Config)
    cfg.telegram = TelegramConfig(enabled=True, bot_token="")

    with patch("tero2.config.load_config", return_value=cfg):
        from tero2.cli import cmd_telegram
        args = MagicMock()
        args.project = None
        args.verbose = False
        with pytest.raises(SystemExit) as exc:
            cmd_telegram(args)
        assert exc.value.code == 1


def test_cmd_telegram_proceeds_when_enabled_with_token():
    """tero2 telegram proceeds when enabled=True and token set."""
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock(spec=Config)
    cfg.telegram = TelegramConfig(enabled=True, bot_token="tok:ABC", allowed_chat_ids=["123"])

    with patch("tero2.config.load_config", return_value=cfg), \
         patch("tero2.telegram_input.TelegramInputBot") as MockBot, \
         patch("tero2.cli.asyncio.run") as mock_run:
        from tero2.cli import cmd_telegram
        args = MagicMock()
        args.project = None
        args.verbose = False
        cmd_telegram(args)
        MockBot.assert_called_once_with(cfg)
        assert mock_run.called  # bot.start() was scheduled
