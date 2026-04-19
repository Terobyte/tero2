"""M3 integration: settings, config enabled, wizard step 3, atomic write."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_settings_screen_importable():
    from tero2.tui.screens.settings import SettingsScreen
    assert SettingsScreen is not None


def test_providers_pick_importable():
    from tero2.tui.screens.providers_pick import ProvidersPickScreen
    assert ProvidersPickScreen is not None


def test_telegram_config_enabled_field():
    from tero2.config import TelegramConfig
    cfg = TelegramConfig()
    assert hasattr(cfg, "enabled")
    assert cfg.enabled is False


def test_legacy_telegram_fallback():
    from tero2.config import _parse_config
    raw = {"telegram": {"bot_token": "tok:XYZ"}}
    config = _parse_config(raw)
    assert config.telegram.enabled is True


def test_cmd_telegram_respects_enabled(tmp_path):
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock()
    cfg.telegram = TelegramConfig(enabled=False, bot_token="tok:ABC")
    with patch("tero2.config.load_config", return_value=cfg):
        from tero2.cli import cmd_telegram
        import types
        args = types.SimpleNamespace(project=None, verbose=False)
        import sys
        with pytest.raises(SystemExit) as exc:
            cmd_telegram(args)
        assert exc.value.code == 1


def test_atomic_write_no_tmp_file(tmp_path):
    from tero2.config_writer import write_global_config_section
    target = tmp_path / "config.toml"
    write_global_config_section(target, "telegram", {"enabled": True})
    assert not list(tmp_path.glob("*.tmp"))
    assert target.exists()


def test_sora_invariant_in_providers_pick(tmp_path):
    from tero2.tui.screens.providers_pick import ProvidersPickScreen
    screen = ProvidersPickScreen.__new__(ProvidersPickScreen)
    screen._roles = {"builder": ("claude", "sonnet")}  # missing architect+verifier
    assert not screen._validate_sora()

    screen._roles = {
        "builder": ("claude", "sonnet"),
        "architect": ("claude", "opus"),
        "verifier": ("claude", "sonnet"),
    }
    assert screen._validate_sora()


def test_all_m3_imports():
    from tero2.config_writer import write_global_config_section  # noqa
    from tero2.tui.screens.settings import SettingsScreen  # noqa
    from tero2.tui.screens.providers_pick import ProvidersPickScreen  # noqa
