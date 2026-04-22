"""Usage tracker for tero2 — wraps `caut usage --json` and accumulates session data."""

from __future__ import annotations

import asyncio
import json
import random
import subprocess
import threading
from typing import Any


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
            await self._refresh_limits()
            await asyncio.sleep(_REFRESH_INTERVAL_S)

    # ── session accumulation ───────────────────────────────────────────

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
        with self._providers_lock:
            self._total_tokens += tokens
            self._total_cost += cost

            if provider not in self._providers:
                self._providers[provider] = {
                    "tokens": 0,
                    "cost": 0.0,
                    "steps": 0,
                    "is_estimated": is_estimated,
                }

            entry = self._providers[provider]
            entry["tokens"] += tokens
            entry["cost"] += cost
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
