"""Dynamic + static model catalog for all supported providers."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".tero2" / "cache"
_CACHE_TTL_S = 3600  # 1 hour


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str


DEFAULT_PROVIDERS: list[str] = [
    "claude", "codex", "opencode", "kilo", "zai", "gemma"
]

STATIC_CATALOG: dict[str, list[ModelEntry]] = {
    "claude": [
        ModelEntry(id="sonnet", label="Claude Sonnet"),
        ModelEntry(id="opus", label="Claude Opus"),
        ModelEntry(id="haiku", label="Claude Haiku"),
    ],
    "codex": [
        ModelEntry(id="", label="gpt-codex (medium, default)"),
        ModelEntry(id="gpt-5.4", label="gpt-5.4 (high reasoning)"),
    ],
    "zai": [
        ModelEntry(id="glm-5.1", label="GLM-5.1 (native)"),
    ],
    "gemma": [],   # in development
    "opencode": [],  # dynamic only
    "kilo": [],      # dynamic only
}

_DYNAMIC_PROVIDERS = {"opencode", "kilo"}


def _humanize(model_id: str) -> str:
    label = model_id
    for prefix in ("openrouter/", "anthropic/", "google/", "meta-llama/"):
        label = label.removeprefix(prefix)
    return label.capitalize()


async def fetch_cli_models(
    cli_name: str,
    provider_filter: str | None = None,
    free_only: bool = False,
    refresh: bool = False,
) -> list[ModelEntry]:
    # Uses create_subprocess_exec (not shell=True) — no injection risk.
    cmd = [cli_name, "models"]
    if provider_filter:
        cmd.append(provider_filter)
    if refresh:
        cmd.append("--refresh")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(f"{cli_name} models exited {proc.returncode}")
        entries = []
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if free_only and ":free" not in line:
                continue
            entries.append(ModelEntry(id=line, label=_humanize(line)))
        return entries
    except (FileNotFoundError, asyncio.TimeoutError, RuntimeError) as e:
        log.warning("fetch_cli_models(%s) failed: %s — using static fallback", cli_name, e)
        return STATIC_CATALOG.get(cli_name, [])


def _cache_path(cli: str) -> Path:
    return _CACHE_DIR / f"{cli}_models.json"


def _load_cache(cli: str) -> list[ModelEntry] | None:
    p = _cache_path(cli)
    try:
        raw = json.loads(p.read_text())
        fetched_at = datetime.fromisoformat(raw["fetched_at"])
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age > _CACHE_TTL_S:
            return None
        return [ModelEntry(**e) for e in raw["entries"]]
    except (FileNotFoundError, KeyError, json.JSONDecodeError, ValueError):
        return None


def _save_cache(cli: str, entries: list[ModelEntry]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(cli)
        data = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "entries": [{"id": e.id, "label": e.label} for e in entries],
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(p)
    except OSError as e:
        log.warning("cache write failed for %s: %s", cli, e)


async def get_models(cli: str, free_only: bool = False) -> list[ModelEntry]:
    if cli not in _DYNAMIC_PROVIDERS:
        return STATIC_CATALOG.get(cli, [])
    cached = _load_cache(cli)
    if cached is not None:
        if free_only:
            return [m for m in cached if ":free" in m.id]
        return cached
    entries = await fetch_cli_models(cli, free_only=free_only)
    _save_cache(cli, entries)
    return entries
