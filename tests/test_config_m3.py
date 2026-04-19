import pytest
from tero2.config import TelegramConfig, _parse_config


def test_telegram_config_enabled_default():
    cfg = TelegramConfig()
    assert cfg.enabled is False


def test_telegram_config_enabled_explicit():
    cfg = TelegramConfig(enabled=True, bot_token="tok")
    assert cfg.enabled is True


def test_parse_config_legacy_fallback():
    """If enabled absent but bot_token present -> enabled=True."""
    raw = {
        "telegram": {
            "bot_token": "tok:ABC",
            "chat_id": "123",
        }
    }
    config = _parse_config(raw)
    assert config.telegram is not None
    assert config.telegram.enabled is True


def test_parse_config_enabled_false_overrides_bot_token():
    """Explicit enabled=false beats non-empty bot_token."""
    raw = {
        "telegram": {
            "enabled": False,
            "bot_token": "tok:ABC",
        }
    }
    config = _parse_config(raw)
    assert config.telegram.enabled is False


def test_parse_config_no_telegram_section():
    config = _parse_config({})
    assert config.telegram is None or not getattr(config.telegram, "enabled", True)
