"""Named constants for tero2. Import what you need."""

from __future__ import annotations

# ── Timeouts (seconds) ──────────────────────────────────────────
DEFAULT_PROVIDER_TIMEOUT_S: int = 900
DEFAULT_RUNNER_STEP_TIMEOUT_S: int = 600
DEFAULT_CHAIN_RETRY_WAIT_S: float = 60.0

HARD_TIMEOUT_S: int = 900  # 15 min — force kill + save state (asyncio.timeout)

# ── Retry / attempt limits ──────────────────────────────────────
MAX_TASK_RETRIES: int = 3
MAX_STEPS_PER_TASK: int = 15

# ── Buffer / size limits ────────────────────────────────────────
MAX_TOOL_OUTPUT_CHARS: int = 8_000
STDOUT_READ_CHUNK_SIZE: int = 65_536
STREAM_READER_LIMIT: int = 16 * 1024 * 1024  # 16 MB

# Used by zai.py provider as the unknown-model fallback window.
DEFAULT_CONTEXT_LIMIT: int = 110_000

# ── Circuit Breaker ─────────────────────────────────────────────
CB_FAILURE_THRESHOLD: int = 3
CB_RECOVERY_TIMEOUT_S: int = 60

# ── Per-provider rate-limit retry ───────────────────────────────
RATE_LIMIT_WAIT_S: float = 5.0   # base wait before first retry (seconds)
RATE_LIMIT_MAX_RETRIES: int = 3  # max retries per provider before falling back

# ── Notifier ────────────────────────────────────────────────────
DEFAULT_HEARTBEAT_INTERVAL_S: int = 900  # 15 minutes

# ── Exit codes ──────────────────────────────────────────────────
EXIT_OK: int = 0
EXIT_AGENT_TIMEOUT: int = 124
EXIT_ALL_PROVIDERS_FAILED: int = 2
EXIT_LOCK_HELD: int = 3
EXIT_CONFIG_ERROR: int = 4

# ── File-tree scanning ───────────────────────────────────────
# Shared skip-set used by ScoutPlayer._build_file_tree and PlanPickScreen.
# Add entries here once; both consumers pick them up automatically.
PROJECT_SCAN_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        "dist",
    }
)
