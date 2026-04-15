# MVP0 — Immortal Runner

> Specification for Claude Code implementation agent.
> Version: 1.1 | Status: Draft

## 1. What This MVP Achieves

**Problem:** tero v1 crashes silently, has no recovery, and burns budget on rate limits.

**After MVP0:** Run `tero2 run ~/project --plan plan.md` → leave → get Telegram notification "done" or "stuck, waiting for you." Agent survives crashes, rate limits, provider outages, and Mac sleep. Always resumes from last checkpoint.

**MVP0 is the foundation.** Every subsequent MVP builds on top of these modules. No roles, no strategy, no decomposition yet — just a single executor agent that refuses to die.

---

## 2. Project Setup

### 2.1 Directory Structure

```
tero2/
├── tero2/
│   ├── __init__.py
│   ├── constants.py
│   ├── errors.py
│   ├── config.py
│   ├── state.py
│   ├── lock.py
│   ├── disk_layer.py
│   ├── circuit_breaker.py
│   ├── notifier.py
│   ├── checkpoint.py
│   ├── runner.py
│   ├── cli.py
│   └── providers/
│       ├── __init__.py
│       ├── base.py
│       ├── subprocess_runner.py
│       ├── message_adapter.py
│       ├── chain.py
│       ├── claude_native.py
│       ├── codex.py
│       ├── opencode.py
│       ├── zai.py
│       ├── kilo.py
│       └── registry.py
├── tests/
│   ├── __init__.py
│   ├── test_constants.py
│   ├── test_config.py
│   ├── test_state.py
│   ├── test_lock.py
│   ├── test_disk_layer.py
│   ├── test_circuit_breaker.py
│   ├── test_notifier.py
│   ├── test_checkpoint.py
│   ├── test_runner.py
│   └── test_cli.py
├── daemon/
│   └── com.tero.agent.plist
├── pyproject.toml
├── lib/                          # existing design docs (read-only)
└── specs/                        # this file lives here
```

### 2.2 Python & Dependencies

- **Python:** 3.11+
- **Platform:** macOS (Darwin)
- **Package manager:** uv (preferred) or pip

### 2.3 pyproject.toml

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "tero2"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    # tomllib is stdlib in Python 3.11+ — no external dep needed (project requires >=3.11)
    "requests>=2.31",                       # Telegram Bot API
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "ruff>=0.4",
]
tts = [
    # Fish Audio TTS — only needed if voice notifications are enabled
    # Uses existing library/tts_fish_audio.py
]

[project.scripts]
tero2 = "tero2.cli:main"

[tool.ruff]
target-version = "py311"
line-length = 99

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

---

## 3. Architecture Overview

```
                        ┌──────────┐
                        │  cli.py  │   ← entry point: tero2 run / tero2 status
                        └────┬─────┘
                             │
                        ┌────▼─────┐
                  ┌─────┤runner.py ├─────────┐
                  │     └────┬─────┘         │
                  │          │               │
         ┌────────▼──┐  ┌───▼──────┐  ┌─────▼───────┐
         │notifier.py│  │checkpoint│  │  providers/  │
         └────┬──────┘  │  .py     │  │             │
              │         └────┬─────┘  │  chain.py   │
         ┌────▼──────┐  ┌───▼──────┐  │  + CB       │
         │ config.py │  │disk_layer│  │  registry   │
         └───────────┘  │  .py     │  │  claude     │
                        ├──────────┤  │  codex      │
                        │ state.py │  │  opencode   │
                        │ lock.py  │  │  zai        │
                        └──────────┘  └─────────────┘
```

**Data flow:**
1. `cli.py` parses args, loads config, calls `runner.run()`
2. `runner.py` reads state from disk, spawns a provider via `ProviderChain`, streams output
3. On success → `checkpoint.py` saves state → next task or done → `notifier.py` sends Telegram
4. On crash → `checkpoint.py` restores state → retry from last checkpoint
5. On rate limit → `chain.py` + `circuit_breaker.py` → fallback to next provider
6. On stuck/done → `notifier.py` → Telegram message (text, optionally voice)

---

## 4. Module Specifications

Modules are listed in **implementation order** (critical path). Each module specifies its complete public interface.

---

### 4.1 `tero2/constants.py`

**Purpose:** Named constants. No magic numbers anywhere else.

**Dependencies:** None.

**Port from v1:** Partial. Only constants needed for MVP0. Strip TUI/display/debug constants.

```python
"""Named constants for tero2. Import what you need."""

from __future__ import annotations

# ── Timeouts (seconds) ──────────────────────────────────────────
DEFAULT_PROVIDER_TIMEOUT_S: int = 900
DEFAULT_RUNNER_STEP_TIMEOUT_S: int = 600
DEFAULT_CHAIN_RETRY_WAIT_S: float = 60.0

HARD_TIMEOUT_S: int = 900          # 15 min — force kill + save state (asyncio.timeout)

# MVP2/MVP3: SOFT_TIMEOUT_S and IDLE_TIMEOUT_S are reserved for future stuck-detection.
# Do not add them here until the feature is implemented.

# ── Retry / attempt limits ──────────────────────────────────────
MAX_TASK_RETRIES: int = 3
MAX_STEPS_PER_TASK: int = 15

# ── Buffer / size limits ────────────────────────────────────────
MAX_TOOL_OUTPUT_CHARS: int = 8_000
STDOUT_READ_CHUNK_SIZE: int = 65_536
STREAM_READER_LIMIT: int = 16 * 1024 * 1024  # 16 MB

# Used by zai.py provider as the unknown-model fallback window.
DEFAULT_CONTEXT_LIMIT: int = 110_000

# MVP1: MAX_BUFFER_MSGS, LARGE_PROMPT_THRESHOLD_BYTES, DEFAULT_COMPACT_THRESHOLD
# are reserved for context management — not used in MVP0.

# ── Circuit Breaker ─────────────────────────────────────────────
CB_FAILURE_THRESHOLD: int = 3
CB_RECOVERY_TIMEOUT_S: int = 60

# ── Notifier ────────────────────────────────────────────────────
DEFAULT_HEARTBEAT_INTERVAL_S: int = 900  # 15 minutes

# ── Exit codes ──────────────────────────────────────────────────
EXIT_OK: int = 0
EXIT_AGENT_TIMEOUT: int = 124
EXIT_ALL_PROVIDERS_FAILED: int = 2
EXIT_LOCK_HELD: int = 3
EXIT_CONFIG_ERROR: int = 4
```

**Rules:**
- Every constant has a type annotation.
- No imports from other `tero2` modules.
- Group by domain with section comments.

---

### 4.2 `tero2/errors.py`

**Purpose:** Typed exception hierarchy. All application exceptions inherit from `Tero2Error`.

**Dependencies:** None (only stdlib).

**Port from v1:** Adapt. Rename base class `TeroError` → `Tero2Error`. Add new exceptions for MVP0.

```python
"""Exception hierarchy for tero2."""

from __future__ import annotations


class Tero2Error(Exception):
    """Base for all tero2 application errors."""


# ── Provider errors ─────────────────────────────────────────────

class ProviderError(Tero2Error):
    """Base for provider/LLM errors."""

class ProviderNotReadyError(ProviderError):
    """Provider failed readiness check."""

class ProviderTimeoutError(ProviderError):
    """Provider call exceeded time budget."""
    def __init__(self, provider: str, timeout_s: float) -> None:
        self.provider = provider
        self.timeout_s = timeout_s
        super().__init__(f"{provider} timed out after {timeout_s}s")

class RateLimitError(ProviderError):
    """All providers in chain exhausted after retries."""

class CircuitOpenError(ProviderError):
    """Provider circuit breaker is open (fast-fail)."""
    def __init__(self, provider: str) -> None:
        self.provider = provider
        super().__init__(f"Circuit open for {provider}, skipping")


# ── State / session errors ──────────────────────────────────────

class StateError(Tero2Error):
    """Base for state machine errors."""

class LockHeldError(StateError):
    """Another tero2 instance holds the lock."""
    def __init__(self, pid: int, lock_path: str) -> None:
        self.pid = pid
        self.lock_path = lock_path
        super().__init__(f"Lock held by PID {pid}: {lock_path}")

class StateTransitionError(StateError):
    """Invalid state transition."""
    def __init__(self, current: str, target: str) -> None:
        self.current = current
        self.target = target
        super().__init__(f"Invalid transition: {current} -> {target}")


# ── Config errors ───────────────────────────────────────────────

class ConfigError(Tero2Error):
    """Invalid or missing configuration."""


# ── Runner errors ───────────────────────────────────────────────

class RunnerError(Tero2Error):
    """Base for runner lifecycle errors."""

class TaskFailedError(RunnerError):
    """Task exhausted all retry attempts."""
    def __init__(self, task_id: str, attempts: int) -> None:
        self.task_id = task_id
        self.attempts = attempts
        super().__init__(f"Task {task_id} failed after {attempts} attempts")
```

---

### 4.3 `tero2/config.py`

**Purpose:** Load and merge TOML configuration. Single source of truth for all runtime settings.

**Dependencies:** `tero2.constants`

**New module.** Not ported from v1 (v1 uses YAML, tero2 uses TOML).

```python
"""Configuration loader for tero2.

Priority: project .sora/config.toml > global ~/.tero2/config.toml > defaults.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from tero2.constants import (
    DEFAULT_CHAIN_RETRY_WAIT_S,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    DEFAULT_PROVIDER_TIMEOUT_S,
    MAX_TASK_RETRIES,
    MAX_STEPS_PER_TASK,
    CB_FAILURE_THRESHOLD,
    CB_RECOVERY_TIMEOUT_S,
)


@dataclass
class RoleConfig:
    """Configuration for a single role→provider mapping.

    MVP0 uses only one role: "executor" (§4.11 CLI: [roles.executor]).
    RoleConfig is defined here because MVP1 will add builder/verifier/reviewer
    roles — defining it in MVP0 avoids an MVP1 migration for a trivial dataclass.
    Keep this minimal — do not add fields that MVP1 will have to remove.
    """
    provider: str
    model: str = ""
    fallback: list[str] = field(default_factory=list)
    timeout_s: int = DEFAULT_PROVIDER_TIMEOUT_S


@dataclass
class TelegramConfig:
    """Telegram notification settings."""
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True


@dataclass
class RetryConfig:
    """Retry and fault tolerance settings."""
    max_retries: int = MAX_TASK_RETRIES
    chain_retry_wait_s: float = DEFAULT_CHAIN_RETRY_WAIT_S  # base wait for backoff
    backoff_base: float = 2.0                               # exponential factor
    # Backoff formula: min(chain_retry_wait_s * backoff_base^attempt + jitter, 300)
    # jitter = random.uniform(0, chain_retry_wait_s * 0.1)
    # Example: attempt 0→60s, 1→120s+jitter, 2→240s+jitter (capped at 300s)
    max_steps_per_task: int = MAX_STEPS_PER_TASK
    cb_failure_threshold: int = CB_FAILURE_THRESHOLD
    cb_recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S


@dataclass
class Config:
    """Root configuration object."""
    projects_dir: str = "~/Desktop/Projects/Active"
    log_level: str = "INFO"
    roles: dict[str, RoleConfig] = field(default_factory=dict)
    telegram: TelegramConfig = field(default_factory=TelegramConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    providers: dict[str, dict] = field(default_factory=dict)


def load_config(project_path: Path) -> Config:
    """Load config with priority: project > global > defaults.

    Args:
        project_path: Path to the project root (contains .sora/)

    Returns:
        Merged Config object.
    """
    ...


def _load_toml(path: Path) -> dict:
    """Load a TOML file, return empty dict if missing."""
    ...


def _merge_dicts(base: dict, override: dict) -> dict:
    """Deep merge override into base. Override wins on conflicts."""
    ...


def _parse_config(raw: dict) -> Config:
    """Convert raw TOML dict into typed Config dataclass."""
    ...
```

**Config file locations:**
- Global: `~/.tero2/config.toml`
- Project: `<project_path>/.sora/config.toml`

**Minimal config.toml for MVP0:**

```toml
[general]
projects_dir = "~/Desktop/Projects/Active"
log_level = "INFO"

[retry]
max_retries = 3
chain_retry_wait_s = 60
backoff_base = 2.0
max_steps_per_task = 15

[telegram]
bot_token = ""
chat_id = "614473938"
heartbeat_interval_s = 900
voice_on_done = true
voice_on_stuck = true

# MVP0: single "executor" role using ProviderChain
[roles.executor]
provider = "opencode"
model = "z.ai/glm-5.1"
fallback = ["codex", "kilo"]

# Provider-specific configuration (optional overrides)
[providers.opencode]
command = "opencode"
default_model = "z.ai/glm-5.1"

[providers.codex]
command = "codex"
bypass_approvals = true
ephemeral = true

[providers.kilo]
command = "kilo"
default_model = "kilo/xiaomi/mimo-v2-pro:free"

[providers.claude]
command = "claude"
default_model = "sonnet"
```

---

### 4.4 `tero2/state.py`

**Purpose:** Data model for runtime state. Serializable to/from JSON.

**Dependencies:** None (only stdlib).

**New module.**

```python
"""Runtime state model for tero2."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


class Phase(str, Enum):
    """Execution phases for the runner state machine."""
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"          # waiting for human input
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class AgentState:
    """Serializable runtime state. Written to .sora/runtime/STATE.json."""
    phase: Phase = Phase.IDLE
    current_task: str = ""           # e.g., "task_001" or plan filename
    retry_count: int = 0
    steps_in_task: int = 0
    last_checkpoint: str = ""        # ISO timestamp of last successful checkpoint
    provider_index: int = 0          # current provider in chain (for resume after crash)
    started_at: str = ""             # ISO timestamp
    updated_at: str = ""             # ISO timestamp
    error_message: str = ""          # last error if failed/paused
    plan_file: str = ""              # path to the plan being executed

    def to_json(self) -> str:
        """Serialize to pretty-printed JSON string."""
        ...

    @classmethod
    def from_json(cls, data: str) -> AgentState:
        """Deserialize from JSON string. Tolerant of missing fields."""
        ...

    @classmethod
    def from_file(cls, path: Path) -> AgentState:
        """Load state from a JSON file. Returns default state if file missing."""
        ...

    def save(self, path: Path) -> None:
        """Atomically write state to file (write-tmp + rename)."""
        ...

    def touch(self) -> None:
        """Update the updated_at timestamp to now."""
        ...
```

**Behavior rules:**
- `save()` must be atomic: write to a `.tmp` file then `os.rename()`. This guarantees no partial writes on crash.
- `from_file()` returns default `AgentState()` if file doesn't exist or is corrupt — never crashes.
- All timestamps are ISO 8601 UTC.
- `Phase` is a string enum so it serializes naturally to JSON.

**Valid state transitions:**

```
IDLE      → RUNNING   (mark_started — new run)
RUNNING   → COMPLETED (mark_completed)
RUNNING   → FAILED    (mark_failed — all retries exhausted)
RUNNING   → PAUSED    (mark_paused — OVERRIDE.md contains PAUSE)
PAUSED    → RUNNING   (mark_running — PAUSE cleared)
PAUSED    → FAILED    (mark_failed — SIGTERM received while paused)
FAILED    → RUNNING   (mark_started — user re-runs after failure; resets retry_count)
COMPLETED → RUNNING   (mark_started — user re-runs a completed project; resets state)
```

Any other transition raises `StateTransitionError`. `CheckpointManager.mark_*` methods are the
only place that change phase — they validate the transition and raise `StateTransitionError`
before writing to disk if the current→target pair is not in the table above.

---

### 4.5 `tero2/lock.py`

**Purpose:** OS-level file lock to prevent concurrent tero2 instances on the same project.

**Dependencies:** `tero2.errors` (`LockHeldError`)

**New module.**

```python
"""OS-level file lock for single-writer guarantee.

Uses fcntl.flock (advisory lock on macOS) + PID written to the lock file
for stale lock detection after crashes.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path

from tero2.errors import LockHeldError


class FileLock:
    """Exclusive file lock with PID tracking.

    Usage:
        lock = FileLock(Path(".sora/runtime/auto.lock"))
        lock.acquire()    # raises LockHeldError if another process holds it
        try:
            ... do work ...
        finally:
            lock.release()

    Also usable as a context manager:
        with FileLock(path) as lock:
            ...
    """

    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire the lock. Raises LockHeldError if held by another live process."""
        ...

    def release(self) -> None:
        """Release the lock and remove the lock file."""
        ...

    def is_held(self) -> tuple[bool, int]:
        """Check if lock is held by a live process.

        Returns:
            (is_held, pid) — pid is 0 if not held.
        """
        ...

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
```

**Behavior rules:**
- Use `fcntl.flock(fd, LOCK_EX | LOCK_NB)` for non-blocking exclusive lock.
- Write `PID\n` to the lock file after acquiring.
- On `acquire()`: if `flock` fails with `EAGAIN`/`EACCES`, read PID from file, check if process is alive (`os.kill(pid, 0)`). If dead → remove stale lock and retry once. If alive → raise `LockHeldError`.
- `release()` removes the lock file. Silent if already released.
- Lock file path: `.sora/runtime/auto.lock`

---

### 4.6 `tero2/disk_layer.py`

**Purpose:** CRUD operations for all `.sora/` files. Single module that knows the directory structure.

**Dependencies:** `tero2.state`, `tero2.lock`

**New module.**

```python
"""Disk layer — CRUD for .sora/ directory structure.

All agents communicate through the filesystem. This module is the
only place that knows the .sora/ layout.
"""

from __future__ import annotations

from pathlib import Path

from tero2.state import AgentState


class DiskLayer:
    """File-based state management for a project.

    Args:
        project_path: Root of the target project (parent of .sora/).
    """

    def __init__(self, project_path: Path) -> None:
        self.project_path = project_path
        self.sora_dir = project_path / ".sora"

    # ── Initialization ──────────────────────────────────────────

    def init(self) -> None:
        """Create .sora/ directory structure if it doesn't exist.

        Creates:
            .sora/runtime/
            .sora/strategic/
            .sora/persistent/
            .sora/milestones/
            .sora/human/
            .sora/prompts/
            .sora/reports/
        """
        ...

    def is_initialized(self) -> bool:
        """Check if .sora/ exists and has the expected structure."""
        ...

    # ── State (runtime/) ────────────────────────────────────────

    def read_state(self) -> AgentState:
        """Read STATE.json from runtime/. Returns default if missing."""
        ...

    def write_state(self, state: AgentState) -> None:
        """Atomically write STATE.json to runtime/."""
        ...

    @property
    def lock_path(self) -> Path:
        """Path to auto.lock file."""
        return self.sora_dir / "runtime" / "auto.lock"

    # ── Persistent files ────────────────────────────────────────

    def read_file(self, relative_path: str) -> str:
        """Read any file under .sora/ by relative path. Returns "" if missing."""
        ...

    def write_file(self, relative_path: str, content: str) -> None:
        """Write any file under .sora/ by relative path. Creates dirs as needed."""
        ...

    def append_file(self, relative_path: str, content: str) -> None:
        """Append to a file under .sora/. Creates if missing."""
        ...

    # ── Reports / Metrics ───────────────────────────────────────

    def read_metrics(self) -> dict:
        """Read reports/metrics.json. Returns empty dict if missing."""
        ...

    def write_metrics(self, metrics: dict) -> None:
        """Write reports/metrics.json."""
        ...

    def append_activity(self, event: dict) -> None:
        """Append a JSON line to reports/activity.jsonl."""
        ...

    # ── Human steering ──────────────────────────────────────────

    def read_override(self) -> str:
        """Read human/OVERRIDE.md. Returns "" if missing."""
        ...

    def read_steer(self) -> str:
        """Read human/STEER.md. Returns "" if missing."""
        ...

    def clear_override(self) -> None:
        """Delete human/OVERRIDE.md after processing."""
        ...

    # ── Plan ────────────────────────────────────────────────────

    def read_plan(self, plan_file: str) -> str:
        """Read a plan file. Accepts absolute path or relative to project."""
        ...
```

**Behavior rules:**
- All write operations create parent directories automatically (`Path.mkdir(parents=True, exist_ok=True)`).
- All reads return empty string/dict for missing files — never raise.
- `write_state()` uses atomic write (temp file + rename).
- `append_activity()` opens file in append mode, writes one JSON line, closes immediately.
- Methods that read from `human/` are called by the runner at phase boundaries.

---

### 4.7 `tero2/providers/` (Ported from v1)

**Purpose:** CLI-based AI provider abstraction layer.

**Port from v1:** `/Users/terobyte/Desktop/Projects/Active/tero/src/providers/`

**What to port (11 files, ~2097 lines):**

| File | Lines | Changes from v1 |
|------|-------|-----------------|
| `base.py` | 68 | None. Copy as-is. |
| `subprocess_runner.py` | 150 | None. Copy as-is. |
| `message_adapter.py` | 309 | None. Copy as-is. |
| `chain.py` | 137 | Add CircuitBreaker integration (§4.8). |
| `claude_native.py` | 151 | None. Copy as-is. |
| `codex.py` | 525 | None. Copy as-is. |
| `opencode.py` | 246 | None. Copy as-is. |
| `zai.py` | 183 | Remove `src.config.get_context_window` dependency — inline full model table. |
| `kilo.py` | ~30 | **NEW thin wrapper** (not in v1). In v1, "kilo" is handled as an alias inside `__init__.py` with `command="kilo"`. For tero2, create a dedicated file mirroring `opencode.py` with `COMMAND = "kilo"`. Verify the binary name matches the installed executable. See stub below. |
| `registry.py` | 166 | None. Copy as-is. |
| `__init__.py` | 132 | None. Copy as-is. |

**Import path changes:**
- v1: `from src.constants import X` → tero2: `from tero2.constants import X`
- v1: `from src.errors import X` → tero2: `from tero2.errors import X`
- v1: `from src.config import get_context_window` → tero2: remove dependency (only in `zai.py`)

**`kilo.py` stub:**

```python
"""Kilo provider — thin wrapper mirroring opencode.py."""

from tero2.providers.opencode import OpenCodeProvider

COMMAND = "kilo"


class KiloProvider(OpenCodeProvider):
    """Kilo CLI provider. Mirrors OpenCodeProvider with COMMAND = "kilo".

    Kilo uses the same CLI interface as opencode. Override display_name
    so logs/metrics distinguish it from opencode.
    """

    @property
    def display_name(self) -> str:
        return "kilo"

    # _build_cmd uses self.command — override to return COMMAND.
    @property
    def command(self) -> str:
        return COMMAND
```

Verify that `kilo` binary is on PATH before using: `which kilo`.

**`registry.py` / `__init__.py` — kilo alias:**
In v1, `__init__.py` handles `"kilo"` as an alias: `if provider_type in ("kilo",): command = "kilo"`.
Since tero2 adds a dedicated `kilo.py`, update `__init__.py` to use the new `KiloProvider` class
rather than the alias path. Remove the alias condition to avoid double-registration.

**`zai.py` specific fix:**
Replace `from tero2.config import get_context_window` (which does not exist in tero2) with the full inline model table from v1.
Do **not** truncate to 2 models — the full 26-model table must be ported or other models silently
fall back to a wrong context limit. Copy the complete `_MODEL_CONTEXT_WINDOWS` dict from v1's
`src/config.py` into `zai.py` as a module-level constant:

```python
# Port the full table from v1 src/config.py _get_context_window().
# Truncating to a subset breaks context tracking for non-GLM models.
# Type: dict[str, int] — keys are substring patterns, values are token limits.
# The v1 source uses list[tuple[str, int]] internally; tero2 uses dict for clarity.
# Both support the same first-match-wins substring lookup via .items().
#
# ⚠ SUBSTRING MATCH WARNING: keys are matched via `key in model.lower()`.
# Use precise prefixes, not bare words. For example:
#   "codex-" (not "codex") — avoids matching "codex-mini-latest" with the wrong window
#   "gpt-4o" (not "gpt-4") — avoids gpt-4-turbo matching gpt-4o entry
#
# ⚠ DO NOT port v1 keys blindly. v1 matched "codex" via exact equality
# (lower == "codex"), so "codex" was safe there. Here it is a substring key —
# "codex" would match "codex-mini-latest" and assign the wrong context window.
# Use "codex-" (with trailing dash) or the full model name for codex entries.
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    # ... full 26-entry table from v1 ...
}

def _get_context_window(model: str) -> int:
    for key, window in _MODEL_CONTEXT_WINDOWS.items():
        if key in model.lower():
            return window
    return DEFAULT_CONTEXT_LIMIT
```

**Verification after port:**
- `ruff check tero2/providers/`
- All imports resolve
- `python -c "from tero2.providers import create_provider"` succeeds

---

### 4.8 `tero2/circuit_breaker.py`

**Purpose:** Prevent repeated calls to a dead provider. Fast-fail to fallback.

**Dependencies:** `tero2.constants`, `tero2.errors`

**New module.** Not in v1.

```python
"""Circuit breaker for provider fault tolerance.

Three states:
  CLOSED    — normal operation, calls go through
  OPEN      — provider blocked, fast-fail immediately
  HALF_OPEN — one probe call allowed to test recovery
"""

from __future__ import annotations

import time
from enum import Enum
from dataclasses import dataclass, field

from tero2.constants import CB_FAILURE_THRESHOLD, CB_RECOVERY_TIMEOUT_S
from tero2.errors import CircuitOpenError


class CBState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Per-provider circuit breaker.

    Usage:
        cb = CircuitBreaker(name="opencode")
        cb.check()            # raises CircuitOpenError if OPEN
        try:
            result = provider.run(...)
            cb.record_success()
        except Exception:
            cb.record_failure()
            raise
    """
    name: str
    failure_threshold: int = CB_FAILURE_THRESHOLD
    recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S
    state: CBState = CBState.CLOSED
    failure_count: int = 0
    last_failure_time: float = 0.0

    def check(self) -> None:
        """Check if calls are allowed. Raises CircuitOpenError if OPEN.

        If OPEN and recovery_timeout has elapsed, transitions to HALF_OPEN.
        """
        ...

    def record_success(self) -> None:
        """Record a successful call. Resets to CLOSED."""
        ...

    def record_failure(self) -> None:
        """Record a failed call. Transitions to OPEN after threshold."""
        ...

    @property
    def is_available(self) -> bool:
        """True if calls are allowed (CLOSED or HALF_OPEN)."""
        ...
```

**Integration with `chain.py`:**

Add a `CircuitBreakerRegistry` and modify `ProviderChain.run()`:

```python
class CircuitBreakerRegistry:
    """Manages circuit breakers for all providers."""

    def __init__(self, failure_threshold: int = CB_FAILURE_THRESHOLD,
                 recovery_timeout_s: int = CB_RECOVERY_TIMEOUT_S) -> None:
        self._breakers: dict[str, CircuitBreaker] = {}
        self._failure_threshold = failure_threshold
        self._recovery_timeout_s = recovery_timeout_s

    def get(self, provider_name: str) -> CircuitBreaker:
        """Get or create a circuit breaker for a provider."""
        if provider_name not in self._breakers:
            self._breakers[provider_name] = CircuitBreaker(
                name=provider_name,
                failure_threshold=self._failure_threshold,
                recovery_timeout_s=self._recovery_timeout_s,
            )
        return self._breakers[provider_name]
```

**New function added to `chain.py`:**

```python
def _is_recoverable_error(exc: BaseException) -> bool:
    """Return True if the exception is transient — chain should try next provider.

    Recoverable: rate limits, timeouts, provider not ready.
    Non-recoverable: config errors, programming errors, unexpected failures.
    """
    from tero2.errors import RateLimitError, ProviderTimeoutError, ProviderNotReadyError
    return isinstance(exc, (RateLimitError, ProviderTimeoutError, ProviderNotReadyError))
```

**New method added to `ProviderChain`:**

```python
async def run_prompt(self, prompt: str) -> AsyncGenerator[Any, None]:
    """Simple entry point: run a single prompt string.

    Wraps run() with prompt= kwarg. Used by runner._run_agent().
    MVP1 will replace this with context-assembled kwargs.
    """
    async for msg in self.run(prompt=prompt):
        yield msg
```

**Modified `chain.py` run loop (pseudocode):**

```python
# In ProviderChain.run() — an async generator:
#
# Retry architecture (two levels, intentional):
#   Level 1 (chain.py inner): iterates providers in order, CB skips dead ones.
#                              max_retries controls per-provider retry within chain.
#   Level 2 (runner.py outer): retries the entire chain call on failure.
#                              MAX_TASK_RETRIES controls full-chain retries.
# Total max attempts = MAX_TASK_RETRIES × (len(providers) with CB open skips).
# This is intentional: chain exhaustion → runner retries from fresh state.

for provider in self.providers:
    cb = self.cb_registry.get(provider.display_name)
    if not cb.is_available:
        continue  # skip OPEN providers — CB fast-fail

    try:
        # Buffer messages so we can yield them after confirming success.
        # If the provider errors mid-stream, we discard buffered output
        # and move to the next provider in the chain.
        buffer: list = []
        async for msg in provider.run(**kwargs):
            buffer.append(msg)
        cb.record_success()
        for msg in buffer:
            yield msg
        return  # chain succeeded — stop here

    except Exception as exc:
        cb.record_failure()
        buffer.clear()
        if not _is_recoverable_error(exc):
            raise  # non-recoverable (e.g. config error) → propagate immediately
        # Recoverable (rate limit, timeout) → try next provider
        continue

# All providers failed/skipped
raise RateLimitError("all providers in chain exhausted")
```

---

### 4.9 `tero2/notifier.py`

**Purpose:** Send notifications to Telegram (text and voice).

**Dependencies:** `tero2.config` (`TelegramConfig`), `requests`

**New module.**

```python
"""Telegram notification sender.

Supports text messages and voice messages (via Fish Audio TTS).
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

from tero2.config import TelegramConfig

log = logging.getLogger(__name__)

TTS_SCRIPT = Path("/Users/terobyte/Desktop/Projects/Active/scripts/library/tts_fish_audio.py")


class NotifyLevel(str, Enum):
    HEARTBEAT = "heartbeat"    # "working, task 3/7"
    PROGRESS = "progress"      # "slice 2 done, starting slice 3"
    STUCK = "stuck"            # "stuck on task 3, waiting for you"
    DONE = "done"              # "done, 47/47 tests green"
    ERROR = "error"            # "provider claude unavailable, fallback to codex"


class Notifier:
    """Telegram notification sender.

    Args:
        config: TelegramConfig with bot_token and chat_id.
    """

    def __init__(self, config: TelegramConfig) -> None:
        self.config = config
        self._enabled = bool(config.bot_token and config.chat_id)

    def send(self, text: str, level: NotifyLevel = NotifyLevel.PROGRESS) -> bool:
        """Send a text message to Telegram.

        Args:
            text: Message text (supports Markdown).
            level: Notification level (affects voice behavior).

        Returns:
            True if sent successfully, False otherwise.
            Never raises — notifications are best-effort.
        """
        ...

    def send_voice(self, text: str) -> bool:
        """Generate TTS audio and send as voice message.

        Uses Fish Audio TTS (JLM4.7 voice) via existing library script.

        Returns:
            True if sent successfully, False otherwise.
            Never raises.
        """
        ...

    def notify(self, text: str, level: NotifyLevel = NotifyLevel.PROGRESS) -> None:
        """Smart notification: text always, voice for DONE/STUCK if configured.

        This is the primary method to use. It decides text vs voice
        based on level and config.
        """
        ...

    @property
    def enabled(self) -> bool:
        """True if Telegram is configured."""
        return self._enabled
```

**Behavior rules:**
- **Never raise exceptions.** Notification failure must not crash the runner. Log and return False.
- `notify()` always sends text. Adds voice for `DONE` (if `voice_on_done`) and `STUCK` (if `voice_on_stuck`).
- Voice uses `tts_fish_audio_simple()` from existing library. Import dynamically to avoid hard dependency.
- Telegram Bot API: `POST https://api.telegram.org/bot{token}/sendMessage` for text, `sendVoice` for audio.
- Use `requests` library (sync is fine — notifications are fire-and-forget).

**Voice integration:**
```python
def _generate_tts(self, text: str) -> Path | None:
    """Generate TTS audio file. Returns path or None on failure."""
    try:
        import sys
        sys.path.insert(0, str(TTS_SCRIPT.parent.parent))
        from library.tts_fish_audio import tts_fish_audio_simple
        return Path(tts_fish_audio_simple(text))
    except Exception:
        log.warning("TTS generation failed", exc_info=True)
        return None
```

---

### 4.10 `tero2/checkpoint.py`

**Purpose:** Save and restore runner state at well-defined points. Crash recovery starts from last checkpoint.

**Dependencies:** `tero2.disk_layer`, `tero2.state`

**New module.**

```python
"""Checkpoint management for crash recovery.

A checkpoint is saved after every successful step. On crash, the runner
resumes from the last checkpoint instead of restarting from scratch.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from tero2.disk_layer import DiskLayer
from tero2.state import AgentState, Phase


class CheckpointManager:
    """Save and restore checkpoints via DiskLayer.

    Args:
        disk: DiskLayer for the current project.
    """

    def __init__(self, disk: DiskLayer) -> None:
        self.disk = disk

    def save(self, state: AgentState) -> None:
        """Save a checkpoint. Updates timestamp and writes to disk.

        Called after every successful step (task completion, phase transition).
        """
        ...

    def restore(self) -> AgentState:
        """Restore the last checkpoint from disk.

        Returns default AgentState if no checkpoint exists.
        """
        ...

    def mark_started(self, plan_file: str) -> AgentState:
        """Create initial state for a new run.

        Sets phase=RUNNING, records plan_file and start time.
        """
        ...

    def mark_completed(self, state: AgentState) -> AgentState:
        """Mark the run as completed."""
        ...

    def mark_failed(self, state: AgentState, error: str) -> AgentState:
        """Mark the run as failed with error message."""
        ...

    def mark_paused(self, state: AgentState, reason: str) -> AgentState:
        """Mark the run as paused (waiting for human input)."""
        ...

    def mark_running(self, state: AgentState) -> AgentState:
        """Resume from PAUSED to RUNNING. Clears error_message."""
        ...

    def increment_retry(self, state: AgentState) -> AgentState:
        """Increment retry count and reset steps_in_task. Returns updated state."""
        ...

    def increment_step(self, state: AgentState) -> AgentState:
        """Increment steps_in_task counter.

        IMPORTANT: Call this only on tool call completions or agent turns —
        NOT on every streamed message. Providers emit dozens/hundreds of
        streaming chunks per turn. Calling on every chunk would hit
        MAX_STEPS_PER_TASK (15) in seconds. The caller must filter
        message types and only call this for tool_result or turn_end events.
        """
        ...
```

**Behavior rules:**
- Every `mark_*` and `increment_*` method calls `self.disk.write_state()` internally.
- `save()` sets `last_checkpoint` to current UTC ISO timestamp.
- All methods return the updated state (for chaining).
- `restore()` never raises — returns default state if disk is empty.
- `mark_running()` is the inverse of `mark_paused()` — used when PAUSE directive is cleared.

---

### 4.11 `tero2/runner.py`

**Purpose:** Main execution loop. Spawns providers, watches them, handles crashes/timeouts/retries.

**Dependencies:** `tero2.config`, `tero2.disk_layer`, `tero2.checkpoint`, `tero2.notifier`, `tero2.lock`, `tero2.state`, `tero2.providers`, `tero2.circuit_breaker`, `tero2.errors`, `tero2.constants` + stdlib `asyncio`, `signal`

**New module.** This is the heart of MVP0.

```python
"""Runner — main execution loop for tero2.

Lifecycle:
    1. Acquire lock
    2. Load/restore state
    3. Read plan
    4. Build ProviderChain for executor role
    5. Stream agent execution
    6. On success → checkpoint → notify
    7. On error → retry/fallback/escalate
    8. Release lock
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import suppress
from pathlib import Path

from tero2.config import Config, load_config
from tero2.disk_layer import DiskLayer
from tero2.lock import FileLock
from tero2.checkpoint import CheckpointManager
from tero2.notifier import Notifier, NotifyLevel
from tero2.state import AgentState, Phase
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.providers import create_provider
from tero2.providers.chain import ProviderChain
from tero2.errors import (
    LockHeldError,
    RateLimitError,
    TaskFailedError,
    ConfigError,
)
from tero2.constants import (
    MAX_TASK_RETRIES,
    HARD_TIMEOUT_S,
)

log = logging.getLogger(__name__)


class Runner:
    """Main tero2 execution engine.

    Args:
        project_path: Path to the project root.
        plan_file: Path to the plan file (.md).
        config: Optional pre-loaded Config. If None, loads from disk.
    """

    def __init__(
        self,
        project_path: Path,
        plan_file: Path,            # always an absolute Path (cmd_run resolves it)
        config: Config | None = None,
    ) -> None:
        self.project_path = project_path
        self.plan_file = plan_file
        self.config = config or load_config(project_path)
        self.disk = DiskLayer(project_path)
        self.checkpoint = CheckpointManager(self.disk)
        self.notifier = Notifier(self.config.telegram)
        self.lock = FileLock(self.disk.lock_path)
        self.cb_registry = CircuitBreakerRegistry(
            failure_threshold=self.config.retry.cb_failure_threshold,
            recovery_timeout_s=self.config.retry.cb_recovery_timeout_s,
        )

    async def run(self) -> None:
        """Execute the plan. Main entry point.

        Acquires lock, runs the agent loop, handles all errors,
        releases lock on exit.
        """
        ...

    async def _execute_plan(self, state: AgentState) -> None:
        """Core execution loop.

        1. Read plan from disk
        2. Build ProviderChain
        3. Stream agent output
        4. Handle completion/failure
        """
        ...

    def _build_chain(self, start_index: int = 0) -> ProviderChain:
        """Build a ProviderChain from config's executor role.

        Reads roles.executor from config:
          provider = primary provider name
          fallback = list of fallback provider names
          model = model override

        Creates provider instances and wraps in ProviderChain
        with CircuitBreaker integration.

        Args:
            start_index: Resume from this provider offset (from state.provider_index).
                         Skips providers that were already exhausted in a prior run.
                         Pass 0 (default) to start from the beginning of the chain.

        Implementation:
            role = self.config.roles.get("executor")
            all_names = [role.provider] + role.fallback   # e.g. ["opencode", "codex", "kilo"]
            names = all_names[start_index:]               # slice — skip exhausted providers
            providers = [create_provider(n, self.config) for n in names]
            return ProviderChain(providers, cb_registry=self.cb_registry)

        Note: start_index is used at chain-build time (slicing the provider list),
        NOT inside ProviderChain.run() itself. ProviderChain always iterates
        self.providers from index 0 — it has no notion of "skip first N".
        """
        ...

    async def _run_agent(
        self,
        chain: ProviderChain,
        plan_content: str,
        state: AgentState,
    ) -> bool:
        """Run one attempt of the agent.

        Streams output from the ProviderChain.
        Runs heartbeat task concurrently (fires every heartbeat_interval_s).
        Monitors for timeouts (HARD_TIMEOUT_S).
        Returns True on success, False on failure.
        """
        ...

    async def _heartbeat_loop(self, state_ref: list[AgentState]) -> None:
        """Send periodic HEARTBEAT notifications while running.

        Receives state via mutable list so _run_agent can update it in-place.
        Runs as a background task during _run_agent. Cancelled when
        streaming completes or times out.

        Fires every config.telegram.heartbeat_interval_s seconds.
        """
        ...

    async def _check_override(self) -> str | None:
        """Check for human OVERRIDE.md. Returns content or None.

        Uses asyncio.to_thread() — file I/O must not block the event loop.
        """
        ...

    async def _override_contains_pause(self) -> bool:
        """Return True if OVERRIDE.md currently contains 'PAUSE' directive.

        Uses asyncio.to_thread() — file I/O must not block the event loop.
        """
        ...

    def _handle_override(self, content: str, state: AgentState) -> None:
        """Process OVERRIDE commands: PAUSE, STOP, etc."""
        ...
```

**Runner execution flow (pseudocode):**

```
run():
    disk.init()                          # ensure .sora/ exists

    # Graceful shutdown via asyncio signal handler (NOT signal.signal).
    # signal.signal() is not safe in async context — it can interrupt asyncio's
    # event loop. loop.add_signal_handler() schedules a callback safely.
    #
    # IMPORTANT: Do NOT do any I/O (file writes, lock release) inside _on_signal.
    # loop.add_signal_handler callbacks run synchronously in the event loop thread.
    # Blocking I/O here can deadlock the loop. Instead, set a flag and let the
    # main async loop handle the actual cleanup.
    _shutdown_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _on_signal():
        log.info("shutdown signal received")
        _shutdown_event.set()
        # Checkpoint save and lock release happen in the finally block below —
        # NOT here. This callback only signals intent to shut down.

    loop.add_signal_handler(signal.SIGTERM, _on_signal)
    loop.add_signal_handler(signal.SIGINT, _on_signal)

    # lock.acquire() MUST be inside try so LockHeldError is caught below.
    try:
        lock.acquire()                   # single-writer guarantee
        state = checkpoint.restore()     # resume or fresh start
        self._current_state = state      # expose for signal handler

        if state.phase == COMPLETED:
            return                       # already done

        if state.phase in (IDLE, FAILED):
            # IDLE  → fresh run.
            # FAILED → user re-runs after failure; mark_started resets retry_count
            #          and transitions FAILED → RUNNING (see §4.4 state table).
            state = checkpoint.mark_started(plan_file)
            self._current_state = state

        notifier.notify("started", PROGRESS)
        await _execute_plan(state)

    except LockHeldError:
        print("another tero2 instance is running")
        sys.exit(EXIT_LOCK_HELD)
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        loop.remove_signal_handler(signal.SIGINT)
        lock.release()


_execute_plan(state):
    plan_content = disk.read_plan(plan_file)
    # Resume from the last known provider position (0 = fresh start).
    chain = _build_chain(start_index=state.provider_index)

    # range(MAX_TASK_RETRIES) gives exactly MAX_TASK_RETRIES=3 attempts (0, 1, 2).
    # Using +1 would silently add an extra attempt — avoid.
    for attempt in range(MAX_TASK_RETRIES):
        # Check for human override (async — file I/O must not block event loop)
        override = await _check_override()
        if override:
            _handle_override(override, state)
            if state.phase == PAUSED:
                notifier.notify("paused — remove PAUSE from OVERRIDE.md to resume", STUCK)
                # Stay alive and poll; do NOT exit (LaunchAgent restart is not reliable enough).
                # Check _shutdown_event so SIGTERM/SIGINT can break out of the pause loop.
                while await _override_contains_pause():
                    if _shutdown_event.is_set():
                        log.info("shutdown requested during PAUSE — exiting")
                        return
                    await asyncio.sleep(60)
                log.info("PAUSE cleared — resuming")
                state = checkpoint.mark_running(state)  # PAUSED → RUNNING
                self._current_state = state

        success = await _run_agent(chain, plan_content, state)

        if success:
            state = checkpoint.mark_completed(state)
            self._current_state = state
            notifier.notify("done", DONE)
            return

        state = checkpoint.increment_retry(state)
        self._current_state = state
        log.warning(f"attempt {attempt+1} failed, retrying...")

    # All retries exhausted
    state = checkpoint.mark_failed(state, "all retries exhausted")
    notifier.notify(f"failed after {MAX_TASK_RETRIES} attempts", ERROR)


_run_agent(chain, plan_content, state):
    # _heartbeat_loop needs current state to report progress.
    # Pass state as a mutable container (list) so the loop sees updates.
    state_ref = [state]
    # heartbeat_task is created inside the try block so the finally clause
    # always cancels and awaits it — guaranteeing no leaked tasks across retries.
    heartbeat_task = asyncio.create_task(_heartbeat_loop(state_ref))
    try:
        # asyncio.timeout() (Python 3.11+) works correctly with async generators.
        # asyncio.wait_for() only accepts coroutines/futures — do NOT use it here.
        #
        # Mac sleep note: asyncio.timeout() on macOS uses mach_continuous_time
        # (via time.monotonic()), which advances during sleep. The hard timeout
        # does NOT pause — it WILL fire after 900s of wall time including sleep.
        # Both outcomes are handled: provider process killed by OS → exception
        # caught below → retry; timeout fires first → TimeoutError → retry.
        # LaunchAgent restart + checkpoint resume is the recovery path.
        async with asyncio.timeout(HARD_TIMEOUT_S):
            # chain.run_prompt() is an async generator yielding message events.
            # increment_step is called ONLY on tool_result / turn_end events —
            # NOT on every streaming text chunk (would exhaust MAX_STEPS_PER_TASK in seconds).
            async for message in chain.run_prompt(plan_content):
                if message.type in ("tool_result", "turn_end"):
                    state = checkpoint.increment_step(state)
                    state_ref[0] = state
                    self._current_state = state

        return True  # completed without error

    except TimeoutError:
        log.error("hard timeout reached")
        return False
    except RateLimitError:
        log.error("all providers exhausted")
        return False
    except Exception as exc:
        # Use the same recoverability check as chain.py.
        # Recoverable (rate limit, timeout, provider not ready) → return False so
        # _execute_plan retries. Non-recoverable (config error, programming error,
        # unexpected failure) → re-raise immediately; retrying 3× wastes 3×60s
        # on a problem that won't self-heal and obscures the root cause.
        from tero2.providers.chain import _is_recoverable_error
        if not _is_recoverable_error(exc):
            raise
        log.error(f"agent error: {exc}")
        return False
    finally:
        # Always cancel+await so the heartbeat task is fully done before _run_agent
        # returns. This guarantees no leaked tasks when _execute_plan retries.
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task


_heartbeat_loop(state_ref: list[AgentState]):
    # Receives state via mutable list — allows _run_agent to update it in-place.
    interval = config.telegram.heartbeat_interval_s
    while True:
        await asyncio.sleep(interval)
        state = state_ref[0]
        notifier.notify(
            f"still working — step {state.steps_in_task}, retry {state.retry_count}",
            HEARTBEAT,
        )
```

**The runner builds its prompt like this (MVP0 — simple):**

```python
system_prompt = "You are a coding agent. Execute the plan below precisely."
user_prompt = plan_content  # the raw markdown plan
# Combined into a single string passed to chain.run_prompt():
full_prompt = f"{system_prompt}\n\n{user_prompt}"
```

`chain.run_prompt(full_prompt)` is the async generator used in `_run_agent`. It is defined in
`chain.py` and wraps the existing `run(**kwargs)` machinery with a simpler single-string interface.

> MVP1 replaces this with Context Assembly + Persona prompts.

**Circuit Breaker persistence note:**
CB state is in-memory for MVP0. After a crash + LaunchAgent restart, all breakers reset to CLOSED,
which means the runner will retry the same dead provider. This is acceptable for MVP0 because:
1. The retry backoff (60s wait between chain retries) gives providers time to recover.
2. The runner's 3-attempt limit still applies — it won't loop forever.
CB state persistence (to `.sora/runtime/cb_state.json`) is deferred to MVP2.

---

### 4.12 `tero2/cli.py`

**Purpose:** Command-line interface. Entry point for `tero2` command.

**Dependencies:** `tero2.runner`, `tero2.config`, `tero2.disk_layer`, `tero2.state`

**New module.**

```python
"""CLI entry point for tero2.

Commands:
    tero2 run <project_path> --plan <plan.md>   — run agent on project
    tero2 status <project_path>                  — show current state
    tero2 init <project_path>                    — initialize .sora/ structure
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path


def main() -> None:
    """Entry point. Parses args and dispatches to subcommands."""
    ...


def cmd_run(args: argparse.Namespace) -> None:
    """Run the agent on a project with a plan."""
    ...


def cmd_status(args: argparse.Namespace) -> None:
    """Print current runner state for a project."""
    ...


def cmd_init(args: argparse.Namespace) -> None:
    """Initialize .sora/ directory structure."""
    ...


def _build_parser() -> argparse.ArgumentParser:
    """Build the argument parser with subcommands."""
    ...
```

**CLI interface:**

```
tero2 run <project_path> --plan <plan_file>
    Run the agent. Reads plan, builds provider chain, executes.
    Options:
        --plan PATH      Path to the markdown plan file (required)
        --config PATH    Override config file path
        --verbose        Enable debug logging

tero2 status <project_path>
    Show current state: phase, task, retry count, last checkpoint.
    Reads .sora/runtime/STATE.json.

tero2 init <project_path>
    Create .sora/ directory structure.
    Safe to run multiple times (idempotent).
```

**`cmd_run` implementation outline:**

```python
def cmd_run(args):
    project_path = Path(args.project_path).expanduser().resolve()

    if not project_path.is_dir():
        print(f"error: {project_path} is not a directory")
        sys.exit(1)

    # Resolve plan_file to an absolute path so Runner and DiskLayer never
    # see relative paths. Try as-given first, then relative to project_path.
    plan_file = Path(args.plan).expanduser()
    if not plan_file.is_absolute():
        plan_file = (project_path / plan_file).resolve()
    else:
        plan_file = plan_file.resolve()

    if not plan_file.is_file():
        print(f"error: plan file not found: {plan_file}")
        sys.exit(1)

    # plan_file is a resolved Path — Runner.__init__ accepts Path, not str.
    runner = Runner(project_path, plan_file)
    asyncio.run(runner.run())
```

---

### 4.13 `daemon/com.tero.agent.plist`

**Purpose:** macOS LaunchAgent for auto-restart after crash/sleep.

**Not a Python module.** XML plist file.

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tero.agent</string>

    <key>ProgramArguments</key>
    <array>
        <string>/Users/terobyte/.local/bin/tero2</string>
        <string>run</string>
        <string>/Users/terobyte/Desktop/Projects/Active/current-project</string>
        <string>--plan</string>
        <string>plan.md</string>
    </array>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>/tmp/tero-agent.log</string>

    <key>StandardErrorPath</key>
    <string>/tmp/tero-agent.err</string>

    <key>WorkingDirectory</key>
    <string>/Users/terobyte/Desktop/Projects/Active</string>
</dict>
</plist>
```

**Installation:**
```bash
cp daemon/com.tero.agent.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tero.agent.plist
```

**Note:** The plist is a template. The `ProgramArguments` must be edited per project/plan before loading. This is a manual step in MVP0. MVP1's Telegram input automates this.

---

## 5. Implementation Order

### 5.1 Critical Path

```
Track A (state):            Track B (config):        Track C (providers):
─────────────────           ─────────────────        ────────────────────
1. constants.py             2. config.py             5. providers/ (port)
3. errors.py                9. notifier.py           6. circuit_breaker.py
4a. state.py                                         7. CB ↔ chain.py integration
4b. lock.py
8. disk_layer.py
10. checkpoint.py
                         ───── MERGE ─────
                    11. runner.py (needs A+B+C)
                    12. cli.py
                    13. daemon plist
                    14. tests
```

**Steps 1-3 and 5-6 and 2,9 can run in parallel.**

### 5.2 Recommended Build Sequence

| Step | Module | Estimated Complexity | Depends On |
|------|--------|---------------------|------------|
| 1 | `constants.py` | Simple | — |
| 2 | `errors.py` | Simple | — |
| 3 | `config.py` | Medium | constants |
| 4 | `state.py` | Simple | — |
| 5 | `lock.py` | Medium | errors |
| 6 | `disk_layer.py` | Medium | state, lock |
| 7 | Port `providers/` | Medium (copy+adapt) | constants, errors |
| 8 | `circuit_breaker.py` | Simple | constants, errors |
| 9 | Integrate CB into `chain.py` | Small | circuit_breaker, providers |
| 10 | `notifier.py` | Medium | config |
| 11 | `checkpoint.py` | Simple | disk_layer, state |
| 12 | `runner.py` | Complex | all above |
| 13 | `cli.py` | Simple | runner, config |
| 14 | `daemon/plist` | Trivial | cli |
| 15 | Tests | Medium | all above |

---

## 6. Acceptance Criteria

MVP0 is **done** when all of the following pass:

- [ ] `tero2 init ~/testproject` creates `.sora/` directory structure
- [ ] `tero2 run ~/testproject --plan plan.md` starts the agent and executes the plan
- [ ] Agent crash → auto-restart from last checkpoint (test: kill -9 the provider subprocess mid-run, verify resume)
- [ ] Rate limit (429) → retry with backoff + jitter → fallback to next provider in chain
- [ ] Provider dead for 3+ calls → CircuitBreaker opens → fast-fail to fallback
- [ ] CircuitBreaker recovery: after 60s, HALF_OPEN probe → if OK, back to CLOSED
- [ ] Telegram: "started" message on run start
- [ ] Telegram: heartbeat every 15 min (configurable) during execution
- [ ] Telegram: "done" message (with optional voice) on completion
- [ ] Telegram: "error" message on failure
- [ ] `tero2 status ~/testproject` prints current phase, task, retry count
- [ ] Lock prevents two `tero2 run` instances on the same project
- [ ] Stale lock (dead PID) is automatically cleaned up
- [ ] OVERRIDE.md with "PAUSE" → runner pauses → Telegram notification
- [ ] `ruff check tero2/` passes with zero errors
- [ ] `pytest tests/` passes with all tests green
- [ ] LaunchAgent survives Mac sleep (verify manually)

---

## 7. What MVP0 Does NOT Include

These are explicitly out of scope:

- **No roles** (Scout, Architect, Builder, Verifier, Coach) — MVP1/MVP2
- **No plan hardening** — MVP1
- **No context assembly** (smart prompt construction) — MVP1
- **No persona/prompt system** — MVP1
- **No Telegram input** (receiving commands) — MVP1
- **No stuck detection** (semantic loops) — MVP2/MVP3
- **No parallelism** — MVP5
- **No voice input** (STT) — MVP4
- **No decomposition** (Milestone → Slice → Task) — MVP2

MVP0's runner sends the raw plan as a prompt to a single provider chain. That's it. Simple, robust, unkillable.

---

## 8. Reference: tero v1 Source Locations

For porting providers, the source is:

```
/Users/terobyte/Desktop/Projects/Active/tero/src/
├── constants.py          — port subset of constants
├── errors.py             — port and rename base class
└── providers/
    ├── __init__.py       — copy as-is
    ├── base.py           — copy as-is
    ├── subprocess_runner.py — copy as-is
    ├── message_adapter.py   — copy as-is
    ├── chain.py          — copy + add CB integration
    ├── claude_native.py  — copy as-is
    ├── codex.py          — copy as-is
    ├── opencode.py       — copy as-is
    ├── zai.py            — copy + remove src.config dependency (port full model table)
    ├── kilo.py           — **NEW** (not in v1): thin wrapper mirroring opencode.py with COMMAND = "kilo" (see §4.7)
    └── registry.py       — copy as-is
```
