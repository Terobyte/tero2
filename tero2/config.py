"""Configuration loader for tero2.

Priority: project .sora/config.toml > global ~/.tero2/config.toml > defaults.
"""

from __future__ import annotations

import fcntl
import logging
import os
import threading
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

# Guards load_config() so concurrent threads don't interleave TOML parsing.
_load_lock = threading.Lock()


# ─────────────────────────────────────────────────────────────────────────────
# Coercion helpers used by __post_init__ validators.
# ─────────────────────────────────────────────────────────────────────────────


def _coerce_bool(v) -> bool:
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "no", "off", "")
    return bool(v)


def _coerce_int(value, cls: str, name: str) -> int:
    if isinstance(value, bool):
        # bool is a subclass of int, but callers rarely want True/False as 1/0 here.
        # Preserve the numeric value but flag nothing — bool coerces cleanly.
        return int(value)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{cls}.{name} must be int, got {value!r}") from exc


def _coerce_float(value, cls: str, name: str) -> float:
    if isinstance(value, bool):
        return float(value)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{cls}.{name} must be float, got {value!r}") from exc


def _coerce_str_list(value, cls: str, name: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        # A single string means a one-element list.
        return [value]
    try:
        iterator = iter(value)
    except TypeError as exc:
        raise ConfigError(f"{cls}.{name} must be a list, got {value!r}") from exc
    return [str(x) for x in iterator if x is not None]


# ─────────────────────────────────────────────────────────────────────────────
# Config dataclasses — each enforces its own schema at construction time.
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class StuckDetectionConfig:
    max_steps_per_task: int = 15
    max_retries: int = 3
    tool_repeat_threshold: int = 2

    def __post_init__(self) -> None:
        cls = "StuckDetectionConfig"
        self.max_steps_per_task = _coerce_int(self.max_steps_per_task, cls, "max_steps_per_task")
        self.max_retries = _coerce_int(self.max_retries, cls, "max_retries")
        # Allow 0 for tool_repeat_threshold (semantically "disabled");
        # runtime checks interpret the value. Same for max_steps_per_task and
        # max_retries — callers may disable limits by passing sentinel values.
        self.tool_repeat_threshold = _coerce_int(
            self.tool_repeat_threshold, cls, "tool_repeat_threshold"
        )


@dataclass
class EscalationConfig:
    diversification_temp_delta: float = 0.3
    diversification_max_steps: int = 2
    backtrack_to_last_checkpoint: bool = True

    def __post_init__(self) -> None:
        cls = "EscalationConfig"
        self.diversification_temp_delta = _coerce_float(
            self.diversification_temp_delta, cls, "diversification_temp_delta"
        )
        self.diversification_max_steps = _coerce_int(
            self.diversification_max_steps, cls, "diversification_max_steps"
        )
        self.backtrack_to_last_checkpoint = _coerce_bool(self.backtrack_to_last_checkpoint)


@dataclass
class PlanHardeningConfig:
    max_rounds: int = 5
    stop_on_cosmetic_only: bool = True
    debug: bool = False

    def __post_init__(self) -> None:
        cls = "PlanHardeningConfig"
        self.max_rounds = _coerce_int(self.max_rounds, cls, "max_rounds")
        self.stop_on_cosmetic_only = _coerce_bool(self.stop_on_cosmetic_only)
        self.debug = _coerce_bool(self.debug)


@dataclass
class ContextConfig:
    target_ratio: float = 0.70
    warning_ratio: float = 0.80
    hard_fail_ratio: float = 0.95
    skip_scout_if_files_lt: int = 20

    def __post_init__(self) -> None:
        cls = "ContextConfig"
        self.target_ratio = _coerce_float(self.target_ratio, cls, "target_ratio")
        self.warning_ratio = _coerce_float(self.warning_ratio, cls, "warning_ratio")
        self.hard_fail_ratio = _coerce_float(self.hard_fail_ratio, cls, "hard_fail_ratio")
        self.skip_scout_if_files_lt = _coerce_int(
            self.skip_scout_if_files_lt, cls, "skip_scout_if_files_lt"
        )
        # Ratios are validated at usage (see _check_budget) so that tests that
        # probe runtime behaviour with zero/negative ratios still work.


@dataclass
class RoleConfig:
    provider: str
    model: str = ""
    fallback: list[str] = field(default_factory=list)
    timeout_s: int = DEFAULT_PROVIDER_TIMEOUT_S
    context_window: int = 128000

    def __post_init__(self) -> None:
        cls = "RoleConfig"
        self.provider = str(self.provider)
        self.model = str(self.model)
        self.fallback = _coerce_str_list(self.fallback, cls, "fallback")
        self.timeout_s = _coerce_int(self.timeout_s, cls, "timeout_s")
        self.context_window = _coerce_int(self.context_window, cls, "context_window")
        # Note: prior _parse_config() clamped timeout_s and context_window to
        # [1, 86400] and [1, 1_000_000] respectively; that clamp is applied at
        # parse time in _parse_config() rather than here so that tests that
        # construct RoleConfig with sentinel values (e.g. context_window=-1)
        # continue to exercise the downstream assembler paths.


@dataclass
class ReflexionConfig:
    max_cycles: int = 2

    def __post_init__(self) -> None:
        cls = "ReflexionConfig"
        self.max_cycles = _coerce_int(self.max_cycles, cls, "max_cycles")


@dataclass
class TelegramConfig:
    enabled: bool = False   # explicit opt-in; legacy fallback: non-empty bot_token -> True
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True
    allowed_chat_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        cls = "TelegramConfig"
        self.enabled = _coerce_bool(self.enabled)
        self.bot_token = str(self.bot_token)
        self.chat_id = str(self.chat_id)
        self.heartbeat_interval_s = _coerce_int(
            self.heartbeat_interval_s, cls, "heartbeat_interval_s"
        )
        self.voice_on_done = _coerce_bool(self.voice_on_done)
        self.voice_on_stuck = _coerce_bool(self.voice_on_stuck)
        self.allowed_chat_ids = _coerce_str_list(
            self.allowed_chat_ids, cls, "allowed_chat_ids"
        )
        # heartbeat_interval_s=0 is a legitimate "disable heartbeat" value —
        # runtime loop short-circuits on it.


@dataclass
class VerifierConfig:
    commands: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        cls = "VerifierConfig"
        self.commands = _coerce_str_list(self.commands, cls, "commands")


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

    def __post_init__(self) -> None:
        cls = "RetryConfig"
        self.max_retries = _coerce_int(self.max_retries, cls, "max_retries")
        self.chain_retry_wait_s = _coerce_float(
            self.chain_retry_wait_s, cls, "chain_retry_wait_s"
        )
        self.backoff_base = _coerce_float(self.backoff_base, cls, "backoff_base")
        self.max_steps_per_task = _coerce_int(
            self.max_steps_per_task, cls, "max_steps_per_task"
        )
        self.cb_failure_threshold = _coerce_int(
            self.cb_failure_threshold, cls, "cb_failure_threshold"
        )
        self.cb_recovery_timeout_s = _coerce_int(
            self.cb_recovery_timeout_s, cls, "cb_recovery_timeout_s"
        )
        self.rate_limit_wait_s = _coerce_float(
            self.rate_limit_wait_s, cls, "rate_limit_wait_s"
        )
        self.rate_limit_max_retries = _coerce_int(
            self.rate_limit_max_retries, cls, "rate_limit_max_retries"
        )
        # Preserve the prior _parse_config clamp of max_retries to >= 1. Other
        # range checks live in the callers (runtime) so that tests that probe
        # edge values can still construct a RetryConfig.
        if self.max_retries < 1:
            self.max_retries = 1


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

    def __post_init__(self) -> None:
        cls = "Config"
        self.projects_dir = str(self.projects_dir)
        self.log_level = str(self.log_level)
        self.max_slices = _coerce_int(self.max_slices, cls, "max_slices")
        self.idle_timeout_s = _coerce_int(self.idle_timeout_s, cls, "idle_timeout_s")
        if not isinstance(self.roles, dict):
            raise ConfigError(
                f"{cls}.roles must be a dict, got {type(self.roles).__name__}"
            )
        if not isinstance(self.providers, dict):
            raise ConfigError(
                f"{cls}.providers must be a dict, got {type(self.providers).__name__}"
            )


def load_config(project_path: Path, override_path: Path | None = None) -> Config:
    global_path = Path.home() / ".tero2" / "config.toml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = global_path.with_suffix(".lock")
    lock_fd: int | None = None
    try:
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_SH)
        except OSError:
            pass
    except OSError:
        lock_fd = None
    try:
        with _load_lock:
            project_path_config = project_path / ".sora" / "config.toml"
            global_raw = _load_toml(global_path)
            project_raw = _load_toml(project_path_config)
            merged = _merge_dicts(global_raw, project_raw)
            if override_path is not None:
                override_raw = _load_toml(override_path)
                merged = _merge_dicts(merged, override_raw)
            return _parse_config(merged)
    finally:
        if lock_fd is not None:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                os.close(lock_fd)
            except OSError:
                pass


def _load_toml(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return {}
    except UnicodeDecodeError as exc:
        raise ConfigError(f"Cannot decode {path} as UTF-8: {exc}") from exc
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"TOML syntax error in {path}: {exc}") from exc


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
        cfg.projects_dir = str(general["projects_dir"])
    if "log_level" in general:
        cfg.log_level = str(general["log_level"])

    for name, role_data in raw.get("roles", {}).items():
        provider = str(role_data.get("provider", "")).strip()
        if not provider:
            raise ConfigError(f"role '{name}' missing required 'provider' field")
        # RoleConfig.__post_init__ handles int/str coercion; this layer applies
        # the TOML-sourced clamp matching pre-existing _parse_config behaviour.
        role = RoleConfig(
            provider=provider,
            model=role_data.get("model", ""),
            fallback=role_data.get("fallback", []),
            timeout_s=role_data.get("timeout_s", DEFAULT_PROVIDER_TIMEOUT_S),
            context_window=role_data.get("context_window", 128000),
        )
        role.timeout_s = max(1, min(role.timeout_s, 86400))
        role.context_window = max(1, min(role.context_window, 1_000_000))
        cfg.roles[name] = role

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

    # Validate required roles have known providers
    _optional_roles = {"scout", "coach"}
    for name, role_cfg in cfg.roles.items():
        if name not in _optional_roles and role_cfg.provider not in DEFAULT_PROVIDERS:
            raise ConfigError(
                f"roles.{name} references unknown provider {role_cfg.provider!r} "
                f"(known: {', '.join(sorted(DEFAULT_PROVIDERS))})"
            )

    tg = raw.get("telegram", {})
    if tg:
        # Legacy fallback: if 'enabled' absent but bot_token present, treat as enabled.
        # TelegramConfig.__post_init__ handles the bool/int/str coercion.
        tg_enabled = tg.get("enabled")
        if tg_enabled is None:
            tg_enabled = bool(tg.get("bot_token", ""))
        cfg.telegram = TelegramConfig(
            enabled=tg_enabled,
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            heartbeat_interval_s=tg.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S),
            voice_on_done=tg.get("voice_on_done", True),
            voice_on_stuck=tg.get("voice_on_stuck", True),
            allowed_chat_ids=tg.get("allowed_chat_ids", []),
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
            commands=ver.get("commands", []),
        )

    sora = raw.get("sora", {})
    if "max_slices" in sora:
        try:
            cfg.max_slices = int(sora["max_slices"])
        except (ValueError, TypeError) as e:
            raise ConfigError(f"sora.max_slices must be an integer: {sora['max_slices']!r}") from e
    if "idle_timeout_s" in sora:
        try:
            cfg.idle_timeout_s = int(sora["idle_timeout_s"])
        except (ValueError, TypeError) as e:
            raise ConfigError(f"sora.idle_timeout_s must be an integer: {sora['idle_timeout_s']!r}") from e

    return cfg
