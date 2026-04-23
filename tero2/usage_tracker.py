"""Usage tracker for tero2 — wraps `caut usage --json` and accumulates session data."""

from __future__ import annotations

import asyncio
import json
import logging
import random
import subprocess
import threading
from typing import Any

log = logging.getLogger(__name__)


_REFRESH_INTERVAL_S = 300
_STARTUP_OFFSET_MAX_S = 30


def _validate_limits(data: Any) -> dict[str, float]:
    """Validate that *data* is a dict of {str: float}.

    Returns the validated dict on success, empty dict on failure.
    """
    if not isinstance(data, dict):
        return {}
    validated: dict[str, float] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            return {}
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            validated[k] = float(v)
        else:
            return {}
    return validated


class UsageTracker:
    """Tracks per-session token/cost usage and caches provider limit data from caut."""

    def __init__(self) -> None:
        # cached limits from `caut usage --json`
        self._limits: dict[str, float] = {}
        self._limits_lock = asyncio.Lock()

        # per-session accumulator
        self._total_tokens: int = 0
        self._total_cost: float = 0.0
        self._providers: dict[str, dict[str, Any]] = {}
        self._session_lock = asyncio.Lock()
        self._providers_lock = threading.Lock()  # guards _providers in record_step

    # ── limit fetching ─────────────────────────────────────────────────

    def fetch_limits(self) -> dict[str, float]:
        """Call `caut usage --json` and return validated provider limit fractions.

        Returns empty dict if caut is not installed or output is invalid.
        """
        try:
            result = subprocess.run(
                ["caut", "usage", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                return {}
            data = json.loads(result.stdout)
            return _validate_limits(data)
        except FileNotFoundError:
            # caut not installed
            return {}
        except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError):
            return {}

    async def _refresh_limits(self) -> None:
        """Fetch limits and update cached value (thread-safe via asyncio lock)."""
        limits = await asyncio.get_running_loop().run_in_executor(None, self.fetch_limits)
        async with self._limits_lock:
            self._limits = limits

    async def get_limits(self) -> dict[str, float]:
        """Return cached limits (safe for concurrent TUI reads)."""
        async with self._limits_lock:
            return dict(self._limits)

    async def start_refresh_loop(self) -> None:
        """Background loop: random 0-30s startup offset, then refresh every 300s.

        Designed to be awaited as an asyncio task:
            asyncio.create_task(tracker.start_refresh_loop())
        """
        offset = random.uniform(0, _STARTUP_OFFSET_MAX_S)
        await asyncio.sleep(offset)
        while True:
            try:
                await self._refresh_limits()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Blanket: this is the top-level background refresh loop. Any
                # failure (OSError, subprocess, malformed json, 3rd-party
                # library bug) must not kill the refresh task. log.exception
                # so the failure is debuggable.
                log.exception("refresh_limits failed")
            await asyncio.sleep(_REFRESH_INTERVAL_S)

    # ── session accumulation ───────────────────────────────────────────

    def reset_session(self) -> None:
        """Clear all session totals and provider data."""
        with self._providers_lock:
            self._total_tokens = 0
            self._total_cost = 0.0
            self._providers = {}

    def save(self, path) -> None:
        """Persist current session data to a JSON file."""
        import pathlib
        with self._providers_lock:
            data = {
                "total_tokens": self._total_tokens,
                "total_cost": self._total_cost,
                "providers": {k: dict(v) for k, v in self._providers.items()},
            }
        pathlib.Path(path).write_text(json.dumps(data), encoding="utf-8")

    def load(self, path) -> None:
        """Load session data from a JSON file (replaces current session)."""
        import pathlib
        try:
            data = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            # Disease 1: UnicodeDecodeError is a ValueError, not an OSError.
            # Treat an undecodable session file the same as missing/corrupt —
            # the accumulator starts from a clean slate.
            return
        with self._providers_lock:
            self._total_tokens = int(data.get("total_tokens", 0))
            self._total_cost = float(data.get("total_cost", 0.0))
            self._providers = {k: dict(v) for k, v in data.get("providers", {}).items()}

    def record_step(
        self,
        provider: str,
        tokens: int,
        cost: float,
        is_estimated: bool,
    ) -> None:
        """Accumulate token and cost data for one agent step.

        Bug 118: ``x += y`` on a Python attribute compiles to multiple
        bytecodes (LOAD, ADD, STORE). The GIL makes each bytecode atomic
        but does NOT make the read-modify-write sequence atomic, so two
        threads racing can each load the same value and both store back
        — a classic lost-update. Keep the scalar increments inside the
        same lock that guards the per-provider dict.
        """
        if tokens < 0 or cost < 0:
            raise ValueError(f"record_step: negative values rejected (tokens={tokens}, cost={cost})")
        with self._providers_lock:
            self._total_tokens += tokens
            # Use += for the scalar (keeps bug 118 structural guard happy),
            # then round for bug 299 float-precision accumulation error.
            self._total_cost += cost
            self._total_cost = round(self._total_cost, 10)

            if provider not in self._providers:
                self._providers[provider] = {
                    "tokens": 0,
                    "cost": 0.0,
                    "steps": 0,
                    "is_estimated": is_estimated,
                }

            entry = self._providers[provider]
            entry["tokens"] += tokens
            entry["cost"] = round(entry["cost"] + cost, 10)
            entry["steps"] += 1
            # if any step is estimated, mark provider as estimated
            if is_estimated:
                entry["is_estimated"] = True

    def session_summary(self) -> dict[str, Any]:
        """Return totals and per-provider breakdown for the current session.

        Bug 122: bug 118 closed the write-side race on the scalar totals,
        but left ``session_summary`` iterating ``_providers`` without the
        lock. TUI reads run concurrently with worker-thread record_step
        calls that may insert fresh provider keys, and "dictionary
        changed size during iteration" can surface. Read under the same
        lock that guards writes.
        """
        with self._providers_lock:
            return {
                "total_tokens": self._total_tokens,
                "total_cost": self._total_cost,
                "providers": {k: dict(v) for k, v in self._providers.items()},
            }
