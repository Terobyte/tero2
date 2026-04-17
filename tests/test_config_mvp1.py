"""Tests for MVP1+MVP2 config additions."""

import pytest

from tero2.config import (
    Config,
    ContextConfig,
    EscalationConfig,
    PlanHardeningConfig,
    ReflexionConfig,
    RetryConfig,
    RoleConfig,
    StuckDetectionConfig,
    TelegramConfig,
    _merge_dicts,
    _parse_config,
    load_config,
)
from tero2.errors import ConfigError


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


# ── _parse_config: reflexion section ────────────────────────────────────────


def test_parse_config_reflexion_custom_cycles():
    """_parse_config reads [reflexion] max_cycles from raw TOML dict."""
    cfg = _parse_config({"reflexion": {"max_cycles": 5}})
    assert cfg.reflexion.max_cycles == 5


def test_parse_config_reflexion_missing_uses_default():
    """_parse_config uses ReflexionConfig default when [reflexion] is absent."""
    cfg = _parse_config({})
    assert cfg.reflexion.max_cycles == 2


# ── _parse_config: telegram.allowed_chat_ids ────────────────────────────────


def test_parse_config_telegram_allowed_chat_ids_str_list():
    """_parse_config reads allowed_chat_ids as list[str]."""
    raw = {"telegram": {"bot_token": "tok", "allowed_chat_ids": ["123", "456"]}}
    cfg = _parse_config(raw)
    assert cfg.telegram.allowed_chat_ids == ["123", "456"]


def test_parse_config_telegram_allowed_chat_ids_int_coerced_to_str():
    """_parse_config coerces integer chat IDs to str (TOML arrays may be int)."""
    raw = {"telegram": {"bot_token": "tok", "allowed_chat_ids": [614473938]}}
    cfg = _parse_config(raw)
    assert cfg.telegram.allowed_chat_ids == ["614473938"]


def test_parse_config_telegram_missing_section_empty_ids():
    """allowed_chat_ids defaults to [] when telegram section is absent."""
    cfg = _parse_config({})
    assert cfg.telegram.allowed_chat_ids == []


def test_parse_config_telegram_missing_allowed_chat_ids_key():
    """allowed_chat_ids defaults to [] when key is absent from [telegram]."""
    raw = {"telegram": {"bot_token": "tok", "chat_id": "999"}}
    cfg = _parse_config(raw)
    assert cfg.telegram.allowed_chat_ids == []


# ── _merge_dicts: deep merge ─────────────────────────────────────────────────


def test_merge_dicts_shallow_override():
    """_merge_dicts overrides a flat key."""
    result = _merge_dicts({"a": 1, "b": 2}, {"b": 99})
    assert result["a"] == 1
    assert result["b"] == 99


def test_merge_dicts_deep_nested():
    """_merge_dicts merges nested dicts without clobbering sibling keys."""
    base = {"section": {"x": 1, "y": 2}, "other": 3}
    override = {"section": {"y": 99}}
    result = _merge_dicts(base, override)
    assert result["section"]["x"] == 1
    assert result["section"]["y"] == 99
    assert result["other"] == 3


def test_merge_dicts_base_unchanged():
    """_merge_dicts does not mutate the base dict."""
    base = {"a": {"k": 1}}
    _merge_dicts(base, {"a": {"k": 2}})
    assert base["a"]["k"] == 1


# ── load_config: round-trip with override file ───────────────────────────────


def test_load_config_override_applies_reflexion(tmp_path):
    """load_config applies [reflexion] from an override TOML file."""
    override = tmp_path / "override.toml"
    override.write_text("[reflexion]\nmax_cycles = 9\n", encoding="utf-8")
    cfg = load_config(project_path=tmp_path, override_path=override)
    assert cfg.reflexion.max_cycles == 9


def test_load_config_override_applies_allowed_chat_ids(tmp_path):
    """load_config applies telegram.allowed_chat_ids from override TOML."""
    override = tmp_path / "override.toml"
    override.write_text(
        '[telegram]\nbot_token = "x"\nallowed_chat_ids = ["111", "222"]\n',
        encoding="utf-8",
    )
    cfg = load_config(project_path=tmp_path, override_path=override)
    assert cfg.telegram.allowed_chat_ids == ["111", "222"]


def test_load_config_no_override_returns_defaults(tmp_path):
    """load_config with no config files returns default Config values."""
    cfg = load_config(project_path=tmp_path)
    # In isolation (no .sora/config.toml or ~/.tero2/config.toml present),
    # defaults must hold. We check reflexion since it's a safe constant.
    assert isinstance(cfg.reflexion, ReflexionConfig)
    assert isinstance(cfg.retry, RetryConfig)


# ── PlanHardeningConfig defaults ────────────────────────────────────────────


def test_plan_hardening_config_defaults():
    """PlanHardeningConfig() has correct documented defaults."""
    ph = PlanHardeningConfig()
    assert ph.max_rounds == 5
    assert ph.stop_on_cosmetic_only is True
    assert ph.debug is False


# ── ContextConfig defaults ───────────────────────────────────────────────────


def test_context_config_defaults():
    """ContextConfig() has correct documented defaults."""
    cc = ContextConfig()
    assert cc.target_ratio == 0.70
    assert cc.warning_ratio == 0.80
    assert cc.hard_fail_ratio == 0.95
    assert cc.skip_scout_if_files_lt == 20


# ── Config contains new sections ─────────────────────────────────────────────


def test_config_has_plan_hardening_and_context_sections():
    """Config() exposes plan_hardening and context as typed sub-configs."""
    cfg = Config()
    assert isinstance(cfg.plan_hardening, PlanHardeningConfig)
    assert isinstance(cfg.context, ContextConfig)


def test_config_max_slices_default():
    """Config.max_slices defaults to 50."""
    cfg = Config()
    assert cfg.max_slices == 50


def test_config_idle_timeout_default():
    """Config.idle_timeout_s defaults to 0 (never)."""
    cfg = Config()
    assert cfg.idle_timeout_s == 0


# ── _parse_config: plan_hardening section ───────────────────────────────────


def test_parse_config_plan_hardening_custom():
    """_parse_config reads [plan_hardening] fields from raw dict."""
    raw = {"plan_hardening": {"max_rounds": 3, "stop_on_cosmetic_only": False, "debug": True}}
    cfg = _parse_config(raw)
    assert cfg.plan_hardening.max_rounds == 3
    assert cfg.plan_hardening.stop_on_cosmetic_only is False
    assert cfg.plan_hardening.debug is True


def test_parse_config_plan_hardening_missing_uses_defaults():
    """_parse_config returns PlanHardeningConfig defaults when section absent."""
    cfg = _parse_config({})
    assert cfg.plan_hardening.max_rounds == 5
    assert cfg.plan_hardening.stop_on_cosmetic_only is True
    assert cfg.plan_hardening.debug is False


# ── _parse_config: context section ──────────────────────────────────────────


def test_parse_config_context_custom():
    """_parse_config reads [context] ratios from raw dict."""
    raw = {"context": {"target_ratio": 0.60, "warning_ratio": 0.75, "hard_fail_ratio": 0.90, "skip_scout_if_files_lt": 10}}
    cfg = _parse_config(raw)
    assert cfg.context.target_ratio == 0.60
    assert cfg.context.warning_ratio == 0.75
    assert cfg.context.hard_fail_ratio == 0.90
    assert cfg.context.skip_scout_if_files_lt == 10


def test_parse_config_context_missing_uses_defaults():
    """_parse_config returns ContextConfig defaults when [context] absent."""
    cfg = _parse_config({})
    assert cfg.context.target_ratio == 0.70
    assert cfg.context.skip_scout_if_files_lt == 20


# ── _parse_config: [sora] section ───────────────────────────────────────────


def test_parse_config_sora_max_slices():
    """_parse_config reads sora.max_slices."""
    cfg = _parse_config({"sora": {"max_slices": 100}})
    assert cfg.max_slices == 100


def test_parse_config_sora_idle_timeout():
    """_parse_config reads sora.idle_timeout_s."""
    cfg = _parse_config({"sora": {"idle_timeout_s": 300}})
    assert cfg.idle_timeout_s == 300


def test_parse_config_sora_missing_uses_defaults():
    """Missing [sora] section leaves max_slices=50, idle_timeout_s=0."""
    cfg = _parse_config({})
    assert cfg.max_slices == 50
    assert cfg.idle_timeout_s == 0


# ── _parse_config: RoleConfig.context_window ────────────────────────────────


def test_parse_config_role_context_window():
    """_parse_config reads roles.*.context_window."""
    raw = {"roles": {"executor": {"provider": "opencode", "context_window": 200000}}}
    cfg = _parse_config(raw)
    assert cfg.roles["executor"].context_window == 200000


def test_parse_config_role_context_window_default():
    """roles.*.context_window defaults to 128000 when not specified."""
    raw = {"roles": {"executor": {"provider": "opencode"}}}
    cfg = _parse_config(raw)
    assert cfg.roles["executor"].context_window == 128000


# ── SORA validation: builder requires architect + verifier ───────────────────


def test_sora_validation_builder_without_architect_or_verifier_raises():
    """_parse_config raises ConfigError when builder role lacks architect and verifier."""
    raw = {"roles": {"builder": {"provider": "opencode"}}}
    with pytest.raises(ConfigError, match="roles.builder requires"):
        _parse_config(raw)


def test_sora_validation_builder_without_architect_raises():
    """ConfigError is raised when architect is missing but verifier is present."""
    raw = {
        "roles": {
            "builder": {"provider": "opencode"},
            "verifier": {"provider": "opencode"},
        }
    }
    with pytest.raises(ConfigError, match="architect"):
        _parse_config(raw)


def test_sora_validation_builder_without_verifier_raises():
    """ConfigError is raised when verifier is missing but architect is present."""
    raw = {
        "roles": {
            "builder": {"provider": "opencode"},
            "architect": {"provider": "opencode"},
        }
    }
    with pytest.raises(ConfigError, match="verifier"):
        _parse_config(raw)


def test_sora_validation_builder_with_both_passes():
    """No ConfigError when builder, architect and verifier are all present."""
    raw = {
        "roles": {
            "builder": {"provider": "opencode"},
            "architect": {"provider": "opencode"},
            "verifier": {"provider": "opencode"},
        }
    }
    cfg = _parse_config(raw)
    assert "builder" in cfg.roles
    assert "architect" in cfg.roles
    assert "verifier" in cfg.roles


def test_sora_validation_no_builder_no_error():
    """No ConfigError when builder role is absent entirely."""
    raw = {"roles": {"executor": {"provider": "opencode"}}}
    cfg = _parse_config(raw)
    assert "builder" not in cfg.roles


def test_sora_validation_error_message_lists_missing_roles():
    """ConfigError message names which roles are missing."""
    raw = {"roles": {"builder": {"provider": "opencode"}}}
    with pytest.raises(ConfigError) as exc_info:
        _parse_config(raw)
    msg = str(exc_info.value)
    assert "architect" in msg
    assert "verifier" in msg


# ── load_config: plan_hardening override round-trip ─────────────────────────


def test_load_config_override_applies_plan_hardening(tmp_path):
    """load_config applies [plan_hardening] from override TOML file."""
    override = tmp_path / "override.toml"
    override.write_text("[plan_hardening]\nmax_rounds = 2\ndebug = true\n", encoding="utf-8")
    cfg = load_config(project_path=tmp_path, override_path=override)
    assert cfg.plan_hardening.max_rounds == 2
    assert cfg.plan_hardening.debug is True


def test_load_config_override_applies_context(tmp_path):
    """load_config applies [context] from override TOML file."""
    override = tmp_path / "override.toml"
    override.write_text("[context]\ntarget_ratio = 0.65\n", encoding="utf-8")
    cfg = load_config(project_path=tmp_path, override_path=override)
    assert cfg.context.target_ratio == 0.65
