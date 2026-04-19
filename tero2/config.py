"""Configuration loader for tero2.

Priority: project .sora/config.toml > global ~/.tero2/config.toml > defaults.
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tero2.constants import (
    CB_FAILURE_THRESHOLD,
    CB_RECOVERY_TIMEOUT_S,
    DEFAULT_CHAIN_RETRY_WAIT_S,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    DEFAULT_PROVIDER_TIMEOUT_S,
    MAX_STEPS_PER_TASK,
    MAX_TASK_RETRIES,
    RATE_LIMIT_MAX_RETRIES,
    RATE_LIMIT_WAIT_S,
)
from tero2.errors import ConfigError

log = logging.getLogger(__name__)


@dataclass
class StuckDetectionConfig:
    max_steps_per_task: int = 15
    max_retries: int = 3
    tool_repeat_threshold: int = 2


@dataclass
class EscalationConfig:
    diversification_temp_delta: float = 0.3
    diversification_max_steps: int = 2
    backtrack_to_last_checkpoint: bool = True


@dataclass
class PlanHardeningConfig:
    max_rounds: int = 5
    stop_on_cosmetic_only: bool = True
    debug: bool = False


@dataclass
class ContextConfig:
    target_ratio: float = 0.70
    warning_ratio: float = 0.80
    hard_fail_ratio: float = 0.95
    skip_scout_if_files_lt: int = 20


@dataclass
class RoleConfig:
    provider: str
    model: str = ""
    fallback: list[str] = field(default_factory=list)
    timeout_s: int = DEFAULT_PROVIDER_TIMEOUT_S
    context_window: int = 128000


@dataclass
class ReflexionConfig:
    max_cycles: int = 2


@dataclass
class TelegramConfig:
    enabled: bool = False   # explicit opt-in; legacy fallback: non-empty bot_token -> True
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True
    allowed_chat_ids: list[str] = field(default_factory=list)


@dataclass
class VerifierConfig:
    commands: list[str] = field(default_factory=list)


@dataclass
class RetryConfig:
    max_retries: int = MAX_TASK_RETRIES
    chain_retry_wait_s: float = DEFAULT_CHAIN_RETRY_WAIT_S
    backoff_base: float = 2.0
    max_steps_per_task: int = MAX_STEPS_PER_TASK
    cb_failure_threshold: int = CB_FAILURE_THRESHOLD
    cb_recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S
    rate_limit_wait_s: float = RATE_LIMIT_WAIT_S
    rate_limit_max_retries: int = RATE_LIMIT_MAX_RETRIES


@dataclass
class Config:
    projects_dir: str = "~/Desktop/Projects/Active"
    log_level: str = "INFO"
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    providers: dict[str, dict] = field(default_factory=dict)
    stuck_detection: StuckDetectionConfig = field(default_factory=StuckDetectionConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    reflexion: ReflexionConfig = field(default_factory=ReflexionConfig)
    plan_hardening: PlanHardeningConfig = field(default_factory=PlanHardeningConfig)
    context: ContextConfig = field(default_factory=ContextConfig)
    verifier: VerifierConfig = field(default_factory=VerifierConfig)
    max_slices: int = 50
    idle_timeout_s: int = 0


def load_config(project_path: Path, override_path: Path | None = None) -> Config:
    global_path = Path.home() / ".tero2" / "config.toml"
    project_path_config = project_path / ".sora" / "config.toml"
    global_raw = _load_toml(global_path)
    project_raw = _load_toml(project_path_config)
    merged = _merge_dicts(global_raw, project_raw)
    if override_path is not None:
        override_raw = _load_toml(override_path)
        merged = _merge_dicts(merged, override_raw)
    return _parse_config(merged)


def _load_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, FileNotFoundError, tomllib.TOMLDecodeError):
        return {}


def _merge_dicts(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _merge_dicts(result[key], val)
        else:
            result[key] = val
    return result


def _parse_config(raw: dict) -> Config:
    cfg = Config()
    general = raw.get("general", {})
    if "projects_dir" in general:
        cfg.projects_dir = general["projects_dir"]
    if "log_level" in general:
        cfg.log_level = general["log_level"]

    for name, role_data in raw.get("roles", {}).items():
        if not role_data.get("provider"):
            raise ConfigError(f"role '{name}' missing required 'provider' field")
        cfg.roles[name] = RoleConfig(
            provider=role_data.get("provider", ""),
            model=role_data.get("model", ""),
            fallback=role_data.get("fallback", []),
            timeout_s=role_data.get("timeout_s", DEFAULT_PROVIDER_TIMEOUT_S),
            context_window=role_data.get("context_window", 128000),
        )

    if "executor" not in cfg.roles:
        cfg.roles["executor"] = RoleConfig(provider="opencode")

    # SORA validation: builder role requires architect and verifier
    if "builder" in cfg.roles:
        missing = [r for r in ("architect", "verifier") if r not in cfg.roles]
        if missing:
            raise ConfigError(
                f"roles.builder requires roles.architect and roles.verifier; "
                f"missing: {', '.join(missing)}"
            )

    # Early validation for optional scout/coach roles
    from tero2.providers.catalog import DEFAULT_PROVIDERS  # noqa: PLC0415

    for optional_role in ("scout", "coach"):
        role_cfg = cfg.roles.get(optional_role)
        if role_cfg is not None:
            if role_cfg.provider not in DEFAULT_PROVIDERS:
                log.warning(
                    "roles.%s references unknown provider %r "
                    "(known: %s) — chain build will fail at runtime",
                    optional_role,
                    role_cfg.provider,
                    ", ".join(DEFAULT_PROVIDERS),
                )
        elif "builder" in cfg.roles:
            log.info("roles.%s not configured — %s phase will be skipped", optional_role, optional_role)

    tg = raw.get("telegram", {})
    if tg:
        # Legacy fallback: if 'enabled' absent but bot_token present, treat as enabled
        tg_enabled = tg.get("enabled")
        if tg_enabled is None:
            tg_enabled = bool(tg.get("bot_token", ""))
        cfg.telegram = TelegramConfig(
            enabled=bool(tg_enabled),
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            heartbeat_interval_s=tg.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S),
            voice_on_done=tg.get("voice_on_done", True),
            voice_on_stuck=tg.get("voice_on_stuck", True),
            allowed_chat_ids=[str(x) for x in tg.get("allowed_chat_ids", [])],
        )

    retry = raw.get("retry", {})
    if retry:
        cfg.retry = RetryConfig(
            max_retries=retry.get("max_retries", MAX_TASK_RETRIES),
            chain_retry_wait_s=retry.get("chain_retry_wait_s", DEFAULT_CHAIN_RETRY_WAIT_S),
            backoff_base=retry.get("backoff_base", 2.0),
            max_steps_per_task=retry.get("max_steps_per_task", MAX_STEPS_PER_TASK),
            cb_failure_threshold=retry.get("cb_failure_threshold", CB_FAILURE_THRESHOLD),
            cb_recovery_timeout_s=retry.get("cb_recovery_timeout_s", CB_RECOVERY_TIMEOUT_S),
            rate_limit_wait_s=retry.get("rate_limit_wait_s", RATE_LIMIT_WAIT_S),
            rate_limit_max_retries=retry.get("rate_limit_max_retries", RATE_LIMIT_MAX_RETRIES),
        )

    cfg.providers = raw.get("providers", {})

    sd = raw.get("stuck_detection", {})
    if sd:
        cfg.stuck_detection = StuckDetectionConfig(
            max_steps_per_task=sd.get("max_steps_per_task", 15),
            max_retries=sd.get("max_retries", 3),
            tool_repeat_threshold=sd.get("tool_repeat_threshold", 2),
        )

    esc = raw.get("escalation", {})
    if esc:
        cfg.escalation = EscalationConfig(
            diversification_temp_delta=esc.get("diversification_temp_delta", 0.3),
            diversification_max_steps=esc.get("diversification_max_steps", 2),
            backtrack_to_last_checkpoint=esc.get("backtrack_to_last_checkpoint", True),
        )

    ref = raw.get("reflexion", {})
    if ref:
        cfg.reflexion = ReflexionConfig(
            max_cycles=ref.get("max_cycles", 2),
        )

    ph = raw.get("plan_hardening", {})
    if ph:
        cfg.plan_hardening = PlanHardeningConfig(
            max_rounds=ph.get("max_rounds", 5),
            stop_on_cosmetic_only=ph.get("stop_on_cosmetic_only", True),
            debug=ph.get("debug", False),
        )

    ctx = raw.get("context", {})
    if ctx:
        cfg.context = ContextConfig(
            target_ratio=ctx.get("target_ratio", 0.70),
            warning_ratio=ctx.get("warning_ratio", 0.80),
            hard_fail_ratio=ctx.get("hard_fail_ratio", 0.95),
            skip_scout_if_files_lt=ctx.get("skip_scout_if_files_lt", 20),
        )

    ver = raw.get("verifier", {})
    if ver:
        cfg.verifier = VerifierConfig(
            commands=list(ver.get("commands", [])),
        )

    sora = raw.get("sora", {})
    if "max_slices" in sora:
        cfg.max_slices = sora["max_slices"]
    if "idle_timeout_s" in sora:
        cfg.idle_timeout_s = sora["idle_timeout_s"]

    return cfg
