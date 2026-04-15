"""Tests for MVP1+MVP2 config additions."""

from tero2.config import (
    Config,
    EscalationConfig,
    ReflexionConfig,
    StuckDetectionConfig,
    TelegramConfig,
)


def test_reflexion_config_defaults():
    rc = ReflexionConfig()
    assert rc.max_cycles == 2


def test_stuck_detection_config_defaults():
    sd = StuckDetectionConfig()
    assert sd.max_steps_per_task == 15
    assert sd.max_retries == 3
    assert sd.tool_repeat_threshold == 2


def test_escalation_config_defaults():
    ec = EscalationConfig()
    assert ec.diversification_temp_delta == 0.3
    assert ec.diversification_max_steps == 2
    assert ec.backtrack_to_last_checkpoint is True


def test_telegram_config_allowed_chat_ids_default():
    tc = TelegramConfig()
    assert tc.allowed_chat_ids == []


def test_config_has_all_new_sections():
    cfg = Config()
    assert isinstance(cfg.reflexion, ReflexionConfig)
    assert isinstance(cfg.stuck_detection, StuckDetectionConfig)
    assert isinstance(cfg.escalation, EscalationConfig)
