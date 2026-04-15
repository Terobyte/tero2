"""Configuration loader for tero2.

Priority: project .sora/config.toml > global ~/.tero2/config.toml > defaults.
"""

from __future__ import annotations

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
)


@dataclass
class RoleConfig:
    provider: str
    model: str = ""
    fallback: list[str] = field(default_factory=list)
    timeout_s: int = DEFAULT_PROVIDER_TIMEOUT_S


@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True


@dataclass
class RetryConfig:
    max_retries: int = MAX_TASK_RETRIES
    chain_retry_wait_s: float = DEFAULT_CHAIN_RETRY_WAIT_S
    backoff_base: float = 2.0
    max_steps_per_task: int = MAX_STEPS_PER_TASK
    cb_failure_threshold: int = CB_FAILURE_THRESHOLD
    cb_recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S


@dataclass
class Config:
    projects_dir: str = "~/Desktop/Projects/Active"
    log_level: str = "INFO"
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    providers: dict[str, dict] = field(default_factory=dict)


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
        cfg.roles[name] = RoleConfig(
            provider=role_data.get("provider", ""),
            model=role_data.get("model", ""),
            fallback=role_data.get("fallback", []),
            timeout_s=role_data.get("timeout_s", DEFAULT_PROVIDER_TIMEOUT_S),
        )

    if "executor" not in cfg.roles:
        cfg.roles["executor"] = RoleConfig(provider="opencode")

    tg = raw.get("telegram", {})
    if tg:
        cfg.telegram = TelegramConfig(
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            heartbeat_interval_s=tg.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S),
            voice_on_done=tg.get("voice_on_done", True),
            voice_on_stuck=tg.get("voice_on_stuck", True),
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
        )

    cfg.providers = raw.get("providers", {})
    return cfg
