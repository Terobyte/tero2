# MVP2 — Strategist

> Specification for Claude Code implementation agent.
> Version: 1.0 | Status: Draft
> Prerequisite: MVP0 + MVP1 fully implemented.

## 1. What This MVP Achieves

**Problem:** You decompose tasks manually. The agent doesn't think strategically. No adaptation when things go wrong.

**After MVP2:** Give tero2 a high-level goal (e.g. "build a CLI tool for X") → Scout maps the codebase → Coach writes strategy → Architect decomposes into Tasks → Builder+Verifier execute → Coach adapts between Slices → stuck detection catches loops → 3-level escalation prevents infinite waste.

**This is the full SORA cycle.** After MVP2, the system can autonomously plan, build, verify, adapt, and escalate.

**Key additions over MVP1:**
- **Scout** — fast codebase recon → CONTEXT_MAP.md
- **Architect** — decomposes Slice into atomic Tasks with must-haves
- **Coach** — async strategic advisor, wakes by trigger, dies after writing
- **Trigger Detection** — when to wake the Coach
- **Context Assembly v2** — injects CONTEXT_MAP, CONTEXT_HINTS, STRATEGY
- **Stuck Detection** — structural: retry count, steps, tool hash repeat
- **Escalation** — 3 levels: diversification → backtrack+coach → human
- **Dispatcher v2** — full SORA state machine

---

## 2. Architecture: Full SORA

```
                         ┌─────────────┐
                         │  cli.py     │
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                    ┌────┤ runner.py   ├────────┐
                    │    │ (Dispatch.  │        │
                    │    │  v2 SORA)   │        │
                    │    └──┬──┬──┬────┘        │
                    │       │  │  │             │
              ┌─────▼──┐ ┌──▼──▼──▼──────┐  ┌──▼────────┐
              │triggers│ │   players/     │  │escalation │
              │.py     │ │               │  │.py        │
              └────────┘ │ scout.py      │  └───────────┘
                         │ architect.py  │
              ┌────────┐ │ builder.py    │  ┌───────────┐
              │stuck_  │ │ verifier.py   │  │coach_     │
              │detect. │ │               │  │runner.py  │
              │.py     │ └───────────────┘  └───────────┘
              └────────┘
                         ┌───────────────┐
                         │context_       │
                         │assembly.py v2 │
                         └───────────────┘
```

**Data flow — full SORA loop:**

```
1. Dispatcher reads STATE.json
   └── no active Slice → launch Scout

2. Scout (Kilo, cheap & fast)
   └── reads codebase → writes CONTEXT_MAP.md → dies

3. Dispatcher checks STRATEGY.md
   └── exists → pass to Architect
   └── missing → trigger Coach first

4. Coach (Codex, triggered)
   └── reads ROADMAP + CONTEXT_MAP + summaries
   └── writes STRATEGY.md + TASK_QUEUE.md + CONTEXT_HINTS.md → dies

5. Architect (Claude Opus/Sonnet)
   └── reads STRATEGY + CONTEXT_MAP
   └── writes S0X-PLAN.md with N Tasks → dies

6. For each Task:
   a. Builder (OpenCode/Z.AI) → code + T0X-SUMMARY.md
   b. Verifier (Kilo) → PASS/FAIL/ANOMALY
   c. FAIL → Reflexion → Builder retry
   d. ANOMALY → EVENT_JOURNAL → trigger Coach
   e. Stuck detected → Escalation (3 levels)

7. End of Slice → trigger Coach
   └── writes updated STRATEGY.md
   └── Dispatcher reads → next Slice or done

8. Milestone complete → squash merge → report
```

---

## 3. Updated Directory Structure

New files added to the MVP1 tree:

```
src/
├── ... (MVP0 + MVP1 modules)
├── players/
│   ├── __init__.py
│   ├── builder.py              # existing (MVP1)
│   ├── verifier.py             # existing (MVP1)
│   ├── scout.py                # NEW
│   └── architect.py            # NEW
├── coach_runner.py              # NEW — Coach execution wrapper
├── triggers.py                  # NEW — Coach trigger detection
├── stuck_detection.py           # NEW — structural loop detection
├── escalation.py                # NEW — 3-level escalation
├── context_assembly.py          # UPDATED — v2 with MAP/HINTS/STRATEGY
└── runner.py                    # UPDATED — Dispatcher v2 (SORA)
```

Prompt files added to `.sora/prompts/`:

```
.sora/prompts/
├── builder.md                   # existing (MVP1)
├── verifier.md                  # existing (MVP1)
├── reviewer.md                  # existing (MVP1)
├── scout.md                     # NEW
├── architect.md                 # NEW
└── coach.md                     # NEW
```

---

## 4. Updated Config

```toml
# ── New roles ───────────────────────────────────────────────────

[roles.scout]
provider = "kilo"
model = "kilo/xiaomi/mimo-v2-pro:free"
fallback = ["opencode"]
max_turns = 15
skip_if_files_lt = 20             # skip Scout for tiny projects

[roles.architect]
provider = "claude"
model = "opus"                    # or "sonnet" for cost savings
fallback = []                     # only Claude, no fallback
max_turns = 30

[roles.coach]
provider = "codex"
model = ""                        # from ~/.codex/config.toml
fallback = []
max_turns = 15

# ── Stuck Detection ─────────────────────────────────────────────

[stuck_detection]
max_steps_per_task = 15           # steps before forced fallback
max_retries = 3                   # retries before soft escalation
tool_repeat_threshold = 2         # same tool call N times = deadlock

# ── Escalation ──────────────────────────────────────────────────

[escalation]
diversification_temp_delta = 0.3  # temperature increase for Level 1
diversification_max_steps = 2     # steps before Level 2
backtrack_to_last_checkpoint = true

# ── Coach Triggers ──────────────────────────────────────────────

[coach_triggers]
on_end_of_slice = true
on_anomaly = true
on_budget_percent = 60            # trigger when budget >= 60% used
on_stuck = true
on_human_steer = true
```

**Updated `src/config.py` dataclasses:**

```python
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
class CoachTriggersConfig:
    on_end_of_slice: bool = True
    on_anomaly: bool = True
    on_budget_percent: int = 60
    on_stuck: bool = True
    on_human_steer: bool = True

@dataclass
class RoleConfig:
    """Updated — add skip_if_files_lt for Scout."""
    provider: str
    model: str = ""
    fallback: list[str] = field(default_factory=list)
    timeout_s: int = DEFAULT_PROVIDER_TIMEOUT_S
    max_turns: int = 30
    skip_if_files_lt: int = 0      # NEW — skip role for small projects

@dataclass
class Config:
    """Updated root config."""
    # ... existing fields ...
    stuck_detection: StuckDetectionConfig = field(default_factory=StuckDetectionConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
    coach_triggers: CoachTriggersConfig = field(default_factory=CoachTriggersConfig)
```

---

## 5. Module Specifications

---

### 5.1 `src/players/scout.py`

**Purpose:** Fast codebase reconnaissance. Reads project structure, key files, git log. Writes CONTEXT_MAP.md.

**Dependencies:** `src.providers.chain`, `src.persona`, `src.disk_layer`

**New module.**

```python
"""Scout player — fast codebase reconnaissance.

Runs before each Slice. Cheap model (Kilo), fast, read-only.
Produces CONTEXT_MAP.md — a compressed map of the codebase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.providers.chain import ProviderChain
from src.persona import PersonaRegistry
from src.disk_layer import DiskLayer

log = logging.getLogger(__name__)


@dataclass
class ScoutResult:
    """Result from Scout reconnaissance."""
    context_map: str            # CONTEXT_MAP.md content
    file_count: int             # number of files found
    success: bool
    error: str = ""


async def run_scout(
    scout_chain: ProviderChain,
    persona_registry: PersonaRegistry,
    working_dir: str,
    disk: DiskLayer,
    milestone_path: str = "milestones/M001",
) -> ScoutResult:
    """Run Scout to map the codebase.

    1. Load scout persona prompt
    2. Run scout agent — it reads the project structure
    3. Extract CONTEXT_MAP.md from output
    4. Write to .sora/milestones/M001/CONTEXT_MAP.md

    The scout agent is instructed to:
        a. List directory structure (1-2 levels)
        b. Read entry points
        c. Identify key modules
        d. Check git log --oneline -10
        e. Map dependencies
        f. Output as structured CONTEXT_MAP.md

    Returns:
        ScoutResult with the context map.
    """
    ...


def should_skip_scout(working_dir: str, skip_threshold: int) -> bool:
    """Check if Scout should be skipped for small projects.

    Returns True if the project has fewer files than skip_threshold.
    """
    ...
```

**Scout agent prompt construction:**

```
[System: Scout persona from prompts/scout.md]
[User: "Map this codebase. Write CONTEXT_MAP.md following the format in your instructions."]
```

The Scout persona prompt (from `lib/system-prompts.md`) contains the CONTEXT_MAP format spec. The scout agent produces it as output.

---

### 5.2 `src/players/architect.py`

**Purpose:** Decompose a Slice into atomic Tasks. Each Task fits in one context window.

**Dependencies:** `src.providers.chain`, `src.persona`, `src.context_assembly`, `src.disk_layer`

**New module.**

```python
"""Architect player — decomposes Slice into Tasks.

Reads Scout's CONTEXT_MAP + Coach's STRATEGY.
Produces S0X-PLAN.md with N atomic Tasks, each with must-haves.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.providers.chain import ProviderChain
from src.persona import PersonaRegistry
from src.context_assembly import assemble_architect_prompt
from src.disk_layer import DiskLayer

log = logging.getLogger(__name__)


@dataclass
class ArchitectResult:
    """Result from Architect decomposition."""
    plan: str                   # S0X-PLAN.md content
    task_count: int             # number of tasks in the plan
    success: bool
    error: str = ""


async def run_architect(
    roadmap: str,
    strategy: str,
    context_map: str,
    architect_chain: ProviderChain,
    persona_registry: PersonaRegistry,
    disk: DiskLayer,
    slice_id: str = "S01",
    milestone_path: str = "milestones/M001",
) -> ArchitectResult:
    """Run Architect to decompose the Slice into Tasks.

    1. Assemble context: roadmap + strategy + context_map
    2. Run Architect agent
    3. Extract S0X-PLAN.md from output
    4. Validate: each Task has must-haves, task count <= 7
    5. Write plan to disk

    The Architect persona prompt instructs it to:
        a. Read STRATEGY, CONTEXT_MAP, ROADMAP
        b. Decompose into <= 7 Tasks
        c. Write must-haves as verifiable conditions
        d. Specify file paths and interfaces
        e. Declare inter-task dependencies

    Args:
        roadmap: The high-level goal (ROADMAP.md or hardened plan).
        strategy: Coach's STRATEGY.md (empty on first Slice).
        context_map: Scout's CONTEXT_MAP.md.
        architect_chain: ProviderChain for Architect role.
        persona_registry: For architect persona prompt.
        disk: DiskLayer for writing plan.
        slice_id: Current slice identifier (S01, S02, ...).
        milestone_path: Path under .sora/ for this milestone.

    Returns:
        ArchitectResult with the plan.
    """
    ...


def validate_plan(plan: str) -> list[str]:
    """Validate the Architect's plan.

    Checks:
        - Each Task has must-haves
        - Task count <= 7 (Architect rule)
        - Tasks have file paths specified
        - Dependencies reference valid task IDs

    Returns list of validation errors (empty = valid).
    """
    ...
```

---

### 5.3 `src/coach_runner.py`

**Purpose:** Execute the Coach role. The Coach wakes up, reads all disk state, writes strategy, dies.

**Dependencies:** `src.providers.chain`, `src.persona`, `src.disk_layer`

**New module.**

```python
"""Coach runner — async strategic advisor.

The Coach is NOT a long-running process. It:
    1. Wakes up (triggered by Dispatcher)
    2. Reads all state from disk (summaries, decisions, events, metrics)
    3. Thinks strategically
    4. Writes STRATEGY.md + TASK_QUEUE.md + RISK.md + CONTEXT_HINTS.md
    5. Dies (context freed, no accumulation)

Each invocation = fresh context (Iron Rule).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.providers.chain import ProviderChain
from src.persona import PersonaRegistry
from src.disk_layer import DiskLayer
from src.triggers import CoachTrigger

log = logging.getLogger(__name__)


@dataclass
class CoachResult:
    """Result from Coach invocation."""
    strategy: str              # STRATEGY.md content
    task_queue: str            # TASK_QUEUE.md content
    risk: str                  # RISK.md content
    context_hints: str         # CONTEXT_HINTS.md content
    success: bool
    error: str = ""


async def run_coach(
    trigger: CoachTrigger,
    coach_chain: ProviderChain,
    persona_registry: PersonaRegistry,
    disk: DiskLayer,
    milestone_path: str = "milestones/M001",
) -> CoachResult:
    """Invoke the Coach.

    1. Gather all context from disk:
        - ROADMAP.md
        - All T0X-SUMMARY.md for current Slice
        - DECISIONS.md
        - EVENT_JOURNAL.md
        - metrics.json
        - Previous CONTEXT_HINTS.md (what Coach wrote last time)
    2. Assemble into a Coach prompt
    3. Run Coach agent (Codex)
    4. Parse output into separate files
    5. Write: STRATEGY.md, TASK_QUEUE.md, RISK.md, CONTEXT_HINTS.md
    6. Append new decisions to DECISIONS.md

    Args:
        trigger: What triggered this Coach invocation.
        coach_chain: ProviderChain for Coach role.
        persona_registry: For coach persona prompt.
        disk: DiskLayer for reading/writing.
        milestone_path: Current milestone path in .sora/.

    Returns:
        CoachResult with all written files.
    """
    ...


def _gather_coach_context(
    disk: DiskLayer,
    milestone_path: str,
    slice_id: str,
) -> str:
    """Read all relevant files from disk and assemble Coach input.

    Reads:
        - milestones/{M}/ROADMAP.md
        - milestones/{M}/{S}/T*-SUMMARY.md (all summaries for current slice)
        - persistent/DECISIONS.md
        - persistent/EVENT_JOURNAL.md
        - reports/metrics.json
        - strategic/CONTEXT_HINTS.md (previous)
        - human/STEER.md (if exists)

    Returns assembled context string (target: 30-50K tokens).
    """
    ...


def _parse_coach_output(output: str) -> CoachResult:
    """Parse Coach output into separate files.

    The Coach persona prompt instructs it to output in sections:
        ## STRATEGY
        ...
        ## TASK_QUEUE
        ...
        ## RISK
        ...
        ## CONTEXT_HINTS
        ...

    Parse these sections into the CoachResult fields.
    """
    ...
```

**Coach context budget:**
- Target: 30-50K tokens (the largest context of any role)
- Includes ALL summaries for the current Slice
- Includes the full EVENT_JOURNAL (anomalies are critical input)
- Includes metrics.json for budget awareness

---

### 5.4 `src/triggers.py`

**Purpose:** Detect when the Coach should be invoked.

**Dependencies:** `src.state`, `src.disk_layer`, `src.config`

**New module.**

```python
"""Coach trigger detection.

The Coach is invoked episodically, not continuously.
This module checks trigger conditions after each phase boundary.
"""

from __future__ import annotations

from enum import Enum

from src.state import AgentState
from src.config import CoachTriggersConfig
from src.disk_layer import DiskLayer


class CoachTrigger(str, Enum):
    """Reasons for invoking the Coach."""
    END_OF_SLICE = "end_of_slice"
    ANOMALY = "anomaly"
    BUDGET_THRESHOLD = "budget_threshold"
    STUCK = "stuck"
    HUMAN_STEER = "human_steer"
    FIRST_RUN = "first_run"        # no STRATEGY.md exists yet


def check_triggers(
    state: AgentState,
    disk: DiskLayer,
    config: CoachTriggersConfig,
    slice_just_completed: bool = False,
    stuck_detected: bool = False,
) -> CoachTrigger | None:
    """Check if any Coach trigger condition is met.

    Checked at every phase boundary (between tasks, between slices).

    Returns the highest-priority trigger, or None if no trigger.

    Priority:
        1. STUCK — most urgent
        2. ANOMALY — unexpected behavior needs strategic review
        3. HUMAN_STEER — user wants change
        4. BUDGET_THRESHOLD — money concerns
        5. END_OF_SLICE — routine strategic review
        6. FIRST_RUN — initialization
    """
    ...


def _check_first_run(disk: DiskLayer) -> bool:
    """True if STRATEGY.md doesn't exist yet (first Slice)."""
    return disk.read_file("strategic/STRATEGY.md") == ""


def _check_anomaly(disk: DiskLayer, last_check_timestamp: str) -> bool:
    """True if EVENT_JOURNAL.md has new entries since last check."""
    ...


def _check_budget(disk: DiskLayer, threshold_percent: int) -> bool:
    """True if budget usage >= threshold (from metrics.json)."""
    ...


def _check_human_steer(disk: DiskLayer) -> bool:
    """True if STEER.md exists and is non-empty."""
    return disk.read_steer() != ""
```

---

### 5.5 Updated `src/context_assembly.py` — v2

**Changes from MVP1:**
- Add `assemble_architect_prompt()` — includes STRATEGY + CONTEXT_MAP
- Add `assemble_coach_prompt()` — includes all summaries + journal + metrics
- Update `assemble_builder_prompt()` — inject CONTEXT_HINTS from Coach
- Add `assemble_scout_prompt()` — minimal, just the role prompt

```python
# ── New functions added to context_assembly.py ──────────────────

def assemble_scout_prompt(
    role_prompt: str,
) -> str:
    """Assemble prompt for Scout. Minimal — just the role prompt.

    Scout gets no extra context. It discovers context.
    """
    return role_prompt


def assemble_architect_prompt(
    role_prompt: str,
    roadmap: str,
    strategy: str,
    context_map: str,
    config: ContextConfig | None = None,
) -> str:
    """Assemble prompt for Architect.

    Includes: role + ROADMAP + STRATEGY + CONTEXT_MAP.
    Target: 15-25K tokens.
    """
    ...


def assemble_coach_prompt(
    role_prompt: str,
    roadmap: str,
    summaries: list[str],
    decisions: str,
    event_journal: str,
    metrics: str,
    previous_hints: str,
    steer: str = "",
    trigger_reason: str = "",
) -> str:
    """Assemble prompt for Coach.

    Largest context of any role (30-50K target).
    Includes everything the Coach needs for strategic review.
    """
    ...


def assemble_builder_prompt(
    role_prompt: str,
    task_plan: str,
    previous_summaries: list[str],
    reflexion_context: str = "",
    context_hints: str = "",        # NEW — from Coach
    config: ContextConfig | None = None,
) -> str:
    """Updated: now accepts context_hints from Coach.

    Injection order:
        1. role_prompt (never cut)
        2. task_plan (never cut)
        3. context_hints (from Coach, cut at COMPRESS)
        4. reflexion_context (cut at COMPRESS)
        5. previous_summaries (cut oldest first)
    """
    ...
```

---

### 5.6 `src/stuck_detection.py`

**Purpose:** Detect when the agent is stuck in a loop. Structural detection only (semantic detection is MVP3).

**Dependencies:** `src.state`, `src.config`

**New module.**

```python
"""Stuck detection — structural loop detection.

Three signals (all deterministic, no LLM):
    1. retry_count >= threshold → probably stuck
    2. steps_in_task >= threshold → task taking too long
    3. last_tool_hash repeated N times → deadlock (same action repeated)

These counters are NOT accessible to the LLM — only the Dispatcher reads them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum

from src.state import AgentState
from src.config import StuckDetectionConfig


class StuckSignal(str, Enum):
    """Type of stuck signal detected."""
    NONE = "none"
    RETRY_EXHAUSTED = "retry_exhausted"
    STEP_LIMIT = "step_limit"
    TOOL_REPEAT = "tool_repeat"


@dataclass
class StuckResult:
    """Result of stuck detection check."""
    signal: StuckSignal
    details: str                # human-readable explanation
    severity: int               # 0=none, 1=warning, 2=escalate


def check_stuck(
    state: AgentState,
    config: StuckDetectionConfig,
) -> StuckResult:
    """Check if the agent is stuck.

    Checked after every step in the execution loop.

    Returns StuckResult with signal and severity.
    Severity 0 = no problem, 1 = warning (try diversification), 2 = escalate.
    """
    ...


def compute_tool_hash(tool_call: str) -> str:
    """Compute a hash of a tool call for repeat detection.

    Uses first 16 chars of SHA-256 of the tool call string.
    """
    return hashlib.sha256(tool_call.encode()).hexdigest()[:16]


def update_tool_hash(state: AgentState, tool_call: str) -> tuple[AgentState, bool]:
    """Update the tool hash in state and check for repeat.

    Args:
        state: Current agent state.
        tool_call: String representation of the current tool call.

    Returns:
        (updated_state, is_repeat) — is_repeat is True if hash matches previous.
    """
    ...
```

---

### 5.7 `src/escalation.py`

**Purpose:** 3-level response to stuck situations. Automated recovery before bothering the human.

**Dependencies:** `src.state`, `src.disk_layer`, `src.notifier`, `src.checkpoint`, `src.coach_runner`, `src.config`

**New module.**

```python
"""Escalation — 3-level response to stuck agents.

Level 1: Diversification (automatic)
    - Increase temperature
    - Inject "previous path was a dead end, try a different approach"
    - 2 more steps to recover

Level 2: Backtrack + Coach (automatic)
    - Rollback to last checkpoint
    - Write stuck details to EVENT_JOURNAL
    - Trigger Coach for strategic advice
    - Resume with Coach's new STRATEGY

Level 3: Human escalation
    - Write STUCK_REPORT.md to .sora/human/
    - Send Telegram notification (text + voice)
    - PAUSE execution
    - Wait for STEER.md or OVERRIDE.md
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from datetime import datetime, timezone

from src.state import AgentState, Phase
from src.disk_layer import DiskLayer
from src.notifier import Notifier, NotifyLevel
from src.checkpoint import CheckpointManager
from src.config import EscalationConfig
from src.stuck_detection import StuckResult, StuckSignal

log = logging.getLogger(__name__)


class EscalationLevel(int, Enum):
    NONE = 0
    DIVERSIFICATION = 1
    BACKTRACK_COACH = 2
    HUMAN = 3


@dataclass
class EscalationAction:
    """Action to take in response to stuck detection."""
    level: EscalationLevel
    inject_prompt: str = ""     # text to inject into next agent call (Level 1)
    should_backtrack: bool = False
    should_trigger_coach: bool = False
    should_pause: bool = False


def decide_escalation(
    stuck_result: StuckResult,
    current_level: EscalationLevel,
    diversification_steps_taken: int,
    config: EscalationConfig,
) -> EscalationAction:
    """Decide what escalation action to take.

    Progression:
        No stuck → Level 0 (do nothing)
        First stuck signal → Level 1 (diversification)
        Level 1 didn't help (N steps) → Level 2 (backtrack + coach)
        Level 2 didn't help (coach ran, still stuck) → Level 3 (human)

    Args:
        stuck_result: Current stuck detection result.
        current_level: Current escalation level (tracks progression).
        diversification_steps_taken: Steps since diversification started.
        config: Escalation config.

    Returns:
        EscalationAction describing what to do.
    """
    ...


async def execute_escalation(
    action: EscalationAction,
    state: AgentState,
    disk: DiskLayer,
    notifier: Notifier,
    checkpoint: CheckpointManager,
) -> AgentState:
    """Execute the escalation action.

    Level 1: update state, inject prompt is handled by caller
    Level 2: rollback state, write EVENT_JOURNAL, return (Coach triggered by caller)
    Level 3: write STUCK_REPORT.md, notify Telegram, pause

    Returns updated AgentState.
    """
    ...


def write_stuck_report(
    disk: DiskLayer,
    state: AgentState,
    stuck_result: StuckResult,
    escalation_history: list[EscalationLevel],
) -> None:
    """Write STUCK_REPORT.md for human review.

    Format:
        # Stuck Report — {timestamp}
        **Task:** {task_id}
        **Signal:** {retry_exhausted | step_limit | tool_repeat}
        **Steps:** {steps_in_task}
        **Last actions:** {from state}
        **What was tried:** Level 1 diversification, Level 2 backtrack+coach
        **Needed:** STEER.md / OVERRIDE.md / HUMAN_CONTEXT.md
    """
    ...
```

**Escalation state machine:**

```
Normal operation (Level 0)
    │
    ▼ stuck detected
Level 1: Diversification
    ├── inject "try different approach" into next prompt
    ├── 2 steps allowed
    └── still stuck after 2 steps?
        │
        ▼
Level 2: Backtrack + Coach
    ├── rollback to last checkpoint
    ├── write to EVENT_JOURNAL
    ├── trigger Coach → Coach writes new STRATEGY
    ├── resume with new strategy
    └── still stuck after Coach intervention?
        │
        ▼
Level 3: Human
    ├── write STUCK_REPORT.md
    ├── Telegram: text + voice "stuck, waiting for you"
    ├── PAUSE execution
    └── wait for human input (STEER.md / OVERRIDE.md)
```

---

### 5.8 Updated `src/runner.py` — Dispatcher v2 (SORA)

**Purpose:** Full SORA state machine. Orchestrates all roles through the complete cycle.

**This is the most complex module.** It replaces MVP1's Dispatcher v1 with the full SORA loop.

```python
"""Runner v2 — Dispatcher with full SORA cycle.

State machine:
    INIT → SCOUT → COACH(first) → ARCHITECT → EXECUTE → COACH(end) → next Slice

    Within EXECUTE:
        For each Task: Builder → Verifier → [Reflexion] → next Task
        Stuck detection + escalation at every step
        Coach triggered on anomaly/stuck/budget

Hierarchy:
    Milestone (optional, for large projects)
      └── Slice (vertical feature, 1-7 Tasks)
           └── Task (atomic, 1 context window)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import AsyncIterator

from src.config import Config, load_config
from src.disk_layer import DiskLayer
from src.lock import FileLock
from src.checkpoint import CheckpointManager
from src.notifier import Notifier, NotifyLevel
from src.state import AgentState, Phase
from src.circuit_breaker import CircuitBreakerRegistry
from src.persona import PersonaRegistry
from src.providers import create_provider
from src.providers.chain import ProviderChain

from src.players.scout import run_scout, should_skip_scout
from src.players.architect import run_architect
from src.players.builder import run_builder
from src.players.verifier import run_verifier, extract_must_haves, Verdict
from src.coach_runner import run_coach
from src.triggers import check_triggers, CoachTrigger
from src.stuck_detection import check_stuck, StuckSignal, update_tool_hash
from src.escalation import (
    decide_escalation, execute_escalation, EscalationLevel, EscalationAction,
)
from src.plan_hardening import harden_plan
from src.reflexion import ReflexionContext, add_attempt
from src.context_assembly import assemble_builder_prompt
from src.errors import TaskFailedError, LockHeldError
from src.constants import MAX_TASK_RETRIES, HARD_TIMEOUT_S

log = logging.getLogger(__name__)


class SoraPhase(str, Enum):
    """Phases in the SORA execution cycle."""
    INIT = "init"
    HARDENING = "hardening"
    SCOUT = "scout"
    COACH = "coach"
    ARCHITECT = "architect"
    EXECUTE = "execute"
    COMPLETE = "complete"
    REASSESS = "reassess"


class Runner:
    """SORA Dispatcher — full autonomous execution engine.

    Orchestrates: Scout → Coach → Architect → (Builder→Verifier)×N → Coach → repeat
    """

    def __init__(
        self,
        project_path: Path,
        plan_file: str,
        config: Config | None = None,
    ) -> None:
        self.project_path = project_path
        self.plan_file = plan_file
        self.config = config or load_config(project_path)
        self.disk = DiskLayer(project_path)
        self.checkpoint = CheckpointManager(self.disk)
        self.notifier = Notifier(self.config.telegram)
        self.lock = FileLock(self.disk.lock_path)
        self.persona_registry = PersonaRegistry(self.disk.sora_dir / "prompts")
        self.cb_registry = CircuitBreakerRegistry(
            failure_threshold=self.config.retry.cb_failure_threshold,
            recovery_timeout_s=self.config.retry.cb_recovery_timeout_s,
        )
        # Escalation tracking
        self._escalation_level = EscalationLevel.NONE
        self._diversification_steps = 0

    async def run(self) -> None:
        """Main entry point. Same lock/init pattern as MVP0."""
        ...

    async def _execute_sora(self, state: AgentState) -> None:
        """Full SORA execution cycle.

        Manages Slices. Each Slice goes through:
            Scout → (Coach) → Architect → Execute → Coach(end-of-slice)
        """
        ...

    async def _run_slice(self, slice_id: str, state: AgentState) -> None:
        """Execute one Slice through the SORA pipeline.

        1. Scout (if not skipped)
        2. Coach (if triggered — first run, anomaly, etc.)
        3. Architect (decompose into Tasks)
        4. Plan Hardening on the Architect's plan
        5. Execute Tasks (Builder + Verifier loop)
        6. Coach (end-of-slice review)
        """
        ...

    # ── Phase methods ───────────────────────────────────────────

    async def _phase_scout(self, slice_id: str) -> str:
        """Run Scout. Returns CONTEXT_MAP content."""
        ...

    async def _phase_coach(self, trigger: CoachTrigger, slice_id: str) -> None:
        """Run Coach. Writes strategic files to disk."""
        ...

    async def _phase_architect(self, slice_id: str) -> list[tuple[str, str]]:
        """Run Architect. Returns list of (task_id, task_plan) tuples."""
        ...

    async def _phase_execute(
        self,
        tasks: list[tuple[str, str]],
        slice_id: str,
        state: AgentState,
    ) -> list[str]:
        """Execute all Tasks in a Slice. Returns list of summaries."""
        ...

    async def _execute_task_with_escalation(
        self,
        task_plan: str,
        task_id: str,
        previous_summaries: list[str],
        slice_id: str,
        state: AgentState,
    ) -> str:
        """Execute a single Task with stuck detection and escalation.

        Inner loop:
            1. Check stuck → escalation decision
            2. Builder run (with escalation inject if Level 1)
            3. Verifier check
            4. PASS → return summary
            5. FAIL → Reflexion → retry
            6. ANOMALY → trigger Coach
            7. Stuck → escalate (Level 1 → 2 → 3)

        Returns task summary on success.
        Raises TaskFailedError if all escalation levels exhausted.
        """
        ...

    # ── Helper methods ──────────────────────────────────────────

    def _build_role_chain(self, role_name: str) -> ProviderChain:
        """Build ProviderChain for a role from config."""
        ...

    def _check_phase_boundary(self, state: AgentState) -> None:
        """Check for overrides and triggers at phase boundaries.

        Called between every phase transition:
            - Read OVERRIDE.md → handle PAUSE/STOP
            - Read STEER.md → pass to Coach on next trigger
            - Check Coach triggers
        """
        ...
```

**Dispatcher v2 execution (detailed pseudocode):**

```
_execute_sora(state):
    persona_registry.load_all()
    plan = disk.read_plan(plan_file)

    # Phase: Hardening (same as MVP1)
    hardened_plan = await _phase_hardening(plan)
    disk.write_file("milestones/M001/ROADMAP.md", hardened_plan)

    # Slice loop (MVP2 starts with single Slice, multi-Slice is natural extension)
    slice_id = "S01"
    await _run_slice(slice_id, state)

    # Milestone complete
    checkpoint.mark_completed(state)
    notifier.notify("milestone complete", DONE)


_run_slice(slice_id, state):
    milestone_path = "milestones/M001"

    # 1. Scout
    _check_phase_boundary(state)
    scout_config = config.roles.get("scout")
    if not should_skip_scout(working_dir, scout_config.skip_if_files_lt):
        context_map = await _phase_scout(slice_id)
    else:
        context_map = ""

    # 2. Coach (first run or triggered)
    trigger = check_triggers(state, disk, config.coach_triggers)
    if trigger:
        await _phase_coach(trigger, slice_id)

    # 3. Architect
    _check_phase_boundary(state)
    tasks = await _phase_architect(slice_id)

    # 4. Plan Hardening on Architect's plan
    # (optional — configurable whether to harden Architect output)

    # 5. Execute Tasks
    _check_phase_boundary(state)
    summaries = await _phase_execute(tasks, slice_id, state)

    # 6. End-of-Slice Coach
    if config.coach_triggers.on_end_of_slice:
        await _phase_coach(CoachTrigger.END_OF_SLICE, slice_id)

    notifier.notify(f"Slice {slice_id} complete ({len(tasks)} tasks)", PROGRESS)


_execute_task_with_escalation(task_plan, task_id, summaries, slice_id, state):
    builder_chain = _build_role_chain("builder")
    verifier_chain = _build_role_chain("verifier")
    reflexion = ReflexionContext(attempts=[])
    must_haves = extract_must_haves(task_plan)
    context_hints = disk.read_file("strategic/CONTEXT_HINTS.md")
    escalation_level = EscalationLevel.NONE
    div_steps = 0

    for cycle in range(MAX_TASK_RETRIES + 1):
        # Stuck check
        stuck = check_stuck(state, config.stuck_detection)
        if stuck.signal != StuckSignal.NONE:
            action = decide_escalation(stuck, escalation_level, div_steps, config.escalation)
            state = await execute_escalation(action, state, disk, notifier, checkpoint)

            if action.should_pause:
                return  # Level 3: paused, waiting for human

            if action.should_trigger_coach:
                await _phase_coach(CoachTrigger.STUCK, slice_id)
                context_hints = disk.read_file("strategic/CONTEXT_HINTS.md")

            escalation_level = action.level
            inject = action.inject_prompt
        else:
            inject = ""

        # Override check
        _check_phase_boundary(state)

        # Builder
        builder_result = await run_builder(
            task_plan, summaries,
            reflexion.to_prompt() + inject,
            context_hints=context_hints,
            builder_chain=builder_chain,
            persona_registry=persona_registry,
            working_dir=str(project_path),
            disk=disk, config=config,
        )

        if not builder_result.success:
            state = checkpoint.increment_retry(state)
            continue

        # Verifier
        verifier_result = await run_verifier(
            task_plan, must_haves, verifier_chain,
            persona_registry, str(project_path), disk, config,
        )

        if verifier_result.verdict == Verdict.PASS:
            disk.write_file(
                f"milestones/M001/{slice_id}/{task_id}-SUMMARY.md",
                builder_result.summary
            )
            return builder_result.summary

        if verifier_result.verdict == Verdict.ANOMALY:
            disk.append_file("persistent/EVENT_JOURNAL.md",
                            verifier_result.anomaly_description)
            # Anomaly triggers Coach
            trigger = check_triggers(state, disk, config.coach_triggers,
                                    stuck_detected=False)
            if trigger:
                await _phase_coach(trigger, slice_id)

        # FAIL → Reflexion
        reflexion = add_attempt(reflexion, builder_result.output, verifier_result)
        state = checkpoint.increment_retry(state)

    raise TaskFailedError(task_id, MAX_TASK_RETRIES + 1)
```

---

## 6. Bundled Prompt Files (new)

### `prompts/scout.md`

See `lib/system-prompts.md` → Scout section. Copy the full prompt.

### `prompts/architect.md`

See `lib/system-prompts.md` → Architect section. Copy the full prompt.

### `prompts/coach.md`

See `lib/system-prompts.md` → Coach section. Copy the full prompt.

---

## 7. Updated State Model

MVP2 adds Slice and phase tracking to `AgentState`:

```python
@dataclass
class AgentState:
    """Updated for MVP2 — adds slice tracking and SORA phases."""
    phase: Phase = Phase.IDLE
    sora_phase: str = ""              # NEW: current SORA phase (scout/coach/architect/execute)
    current_slice: str = ""           # NEW: e.g., "S01"
    current_task: str = ""
    retry_count: int = 0
    steps_in_task: int = 0
    last_tool_hash: str = ""
    tool_repeat_count: int = 0        # NEW: consecutive same-hash count
    last_checkpoint: str = ""
    provider_index: int = 0
    started_at: str = ""
    updated_at: str = ""
    error_message: str = ""
    plan_file: str = ""
    escalation_level: int = 0         # NEW: current escalation level (0-3)
    coach_invocations: int = 0        # NEW: how many times Coach was called
    tasks_completed: int = 0          # NEW: tasks done in current slice
    tasks_total: int = 0              # NEW: total tasks in current slice
```

---

## 8. Implementation Order

```
Track A (players):            Track B (intelligence):    Track C (control):
──────────────────            ─────────────────────      ─────────────────
1. players/scout.py           4. triggers.py             6. stuck_detection.py
2. players/architect.py       5. coach_runner.py         7. escalation.py
3. context_assembly.py v2
                           ───── MERGE ─────
                       8. runner.py (Dispatcher v2)
                       9. config.py updates
                       10. state.py updates
                       11. bundled prompt files (scout, architect, coach)
                       12. tests
```

| Step | Module | Depends On |
|------|--------|------------|
| 1 | `players/scout.py` | providers, persona, disk_layer |
| 2 | `players/architect.py` | providers, persona, context_assembly, disk_layer |
| 3 | `context_assembly.py` v2 | config (update with new assemble_* functions) |
| 4 | `triggers.py` | state, disk_layer, config |
| 5 | `coach_runner.py` | providers, persona, disk_layer, triggers |
| 6 | `stuck_detection.py` | state, config |
| 7 | `escalation.py` | state, disk_layer, notifier, checkpoint, config |
| 8 | Update `runner.py` | all above |
| 9 | Update `config.py` | — |
| 10 | Update `state.py` | — |
| 11 | Create `prompts/*.md` | — |
| 12 | Tests | all above |

Steps 1-3, 4-5, and 6-7 can run in parallel.

---

## 9. Acceptance Criteria

- [ ] "Build a CLI tool for X" → Scout maps codebase → CONTEXT_MAP.md written
- [ ] Scout skipped for projects with < 20 files (configurable)
- [ ] Coach invoked on first run → writes STRATEGY.md, TASK_QUEUE.md, CONTEXT_HINTS.md
- [ ] Architect produces S01-PLAN.md with <= 7 Tasks, each with must-haves
- [ ] Builder + Verifier execute all Tasks → summaries written
- [ ] Coach invoked at end-of-Slice → updated STRATEGY.md
- [ ] Coach invoked on ANOMALY from Verifier
- [ ] Stuck detection: 3+ retries on same task → signal raised
- [ ] Stuck detection: 15+ steps on a task → signal raised
- [ ] Stuck detection: same tool call repeated 2x → deadlock signal
- [ ] Escalation Level 1: diversification prompt injected, 2 extra steps
- [ ] Escalation Level 2: backtrack to checkpoint, Coach triggered, resume
- [ ] Escalation Level 3: STUCK_REPORT.md written, Telegram voice notification, runner PAUSEd
- [ ] After PAUSE: edit STEER.md → runner resumes with new direction
- [ ] `tero2 status` shows: SORA phase, slice, task N/M, escalation level
- [ ] OVERRIDE.md with "PAUSE" → runner pauses at next phase boundary
- [ ] Per-role cost tracking in metrics.json
- [ ] `ruff check src/` clean, `pytest tests/` green

---

## 10. What MVP2 Does NOT Include

- **No Debugger** (Frankenstein composite) — MVP3
- **No semantic loop detection** (cosine similarity) — MVP3
- **No voice input** (STT/Concierge) — MVP4
- **No parallelism** (worktree isolation) — MVP5
- **No multi-Milestone** — MVP2 runs a single Milestone with Slices

MVP2 runs one Milestone with potentially multiple Slices. Multi-Milestone orchestration is a natural extension but not part of this spec. The Slice loop is present but for MVP2, the Architect decomposes the hardened plan into one Slice. Multi-Slice emerges when the Coach reassesses and adds more Slices.
