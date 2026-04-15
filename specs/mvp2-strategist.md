# MVP2 — Strategist

> Specification for Claude Code implementation agent.
> Version: 1.0 | Status: Draft
> Prerequisite: MVP0 + MVP1 fully implemented.

## 1. What This MVP Achieves

**Problem:** The executor gets no structural loop detection. Stuck tasks waste compute indefinitely. No escalation path.

**After MVP2:** Stuck detection catches loops → 3-level escalation prevents infinite waste → Dispatcher v2 state machine manages the lifecycle.

**Key additions over MVP1:**
- **Stuck Detection** — structural: retry count, steps, tool hash repeat
- **Escalation** — 3 levels: diversification → backtrack+coach → human
- **Dispatcher v2** — updated state machine

**Deferred (not in MVP2):**
- **Scout** — fast codebase recon → CONTEXT_MAP.md — deferred to later MVP
- **Architect** — decomposes Slice into atomic Tasks — deferred to later MVP
- **Coach** — async strategic advisor — deferred to later MVP
- **Trigger Detection** — when to wake Coach — deferred with Coach
- **Builder / Verifier roles** — specialized execution roles — deferred to later MVP
- **Context Assembly v2** — CONTEXT_MAP, CONTEXT_HINTS, STRATEGY injection — deferred to later MVP

---

## 2. Architecture: MVP2 (Stuck Detection + Escalation)

```
                         ┌─────────────┐
                         │  cli.py     │
                         └──────┬──────┘
                                │
                         ┌──────▼──────┐
                    ┌────┤ runner.py   ├────────┐
                    │    │ (Dispatch.  │        │
                    │    │  v2)        │        │
                    │    └──────┬──────┘        │
                    │           │               │
              ┌─────▼───────┐   │       ┌───────▼────┐
              │stuck_detect.│   │       │escalation  │
              │.py          │   │       │.py         │
              └─────────────┘   │       └────────────┘
                                │
                         ┌──────▼──────┐
                         │ executor    │
                         │ (MVP1, unch.)│
                         └─────────────┘
```

**Not in MVP2 (deferred):** triggers.py, players/scout.py, players/architect.py, coach_runner.py, context_assembly.py v2

**Future Vision (deferred beyond MVP2) — full SORA loop:**

> ⚠️ The following describes the full future SORA architecture. All components
> below (Scout, Coach, Architect, Builder, Verifier) are deferred to a later MVP.
> MVP2 scope is limited to stuck detection + escalation around the MVP1
> single-executor loop. Do NOT implement any of the steps below in MVP2.

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
├── stuck_detection.py           # NEW — structural loop detection
├── escalation.py                # NEW — 3-level escalation
└── runner.py                    # UPDATED — Dispatcher v2 (state machine)
```

**Not added in MVP2 (deferred):**
```
# DEFERRED — not in MVP2:
# ├── players/scout.py           # Scout role
# ├── players/architect.py       # Architect role
# ├── coach_runner.py             # Coach role
# ├── triggers.py                 # Coach trigger detection
# ├── context_assembly.py         # Context Assembly v2
# └── .sora/prompts/             # Bundled role prompt files
```

---

## 4. Updated Config

```toml
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
class Config:
    """Updated root config."""
    # ... existing fields ...
    stuck_detection: StuckDetectionConfig = field(default_factory=StuckDetectionConfig)
    escalation: EscalationConfig = field(default_factory=EscalationConfig)
```

**Deferred config (not in MVP2):**
```toml
# NOT IN MVP2 — deferred:
# [roles.scout], [roles.architect], [roles.coach]  — no role system
# [coach_triggers]                                  — deferred with Coach
```

**Implementation extras (not in this spec, present in code):**

The current implementation adds the following to `RetryConfig` and `constants.py` for ProviderChain rate-limit retry logic:
```python
# constants.py
RATE_LIMIT_WAIT_S: float = 5.0       # base wait for rate-limit retry
RATE_LIMIT_MAX_RETRIES: int = 3       # max retries per provider on rate limit

# RetryConfig (config.py)
rate_limit_wait_s: float = RATE_LIMIT_WAIT_S
rate_limit_max_retries: int = RATE_LIMIT_MAX_RETRIES
```

These fields are passed to `ProviderChain` and control per-provider retry-with-backoff behavior on `RateLimitError`. They are an undocumented extension beyond this spec.

---

## 5. Module Specifications

---

### 5.1 `src/players/scout.py`

> **⚠️ DEFERRED — Not in MVP2.** No roles (Scout) — deferred to later MVP. Do NOT implement this module.


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

> **⚠️ DEFERRED — Not in MVP2.** No roles (Architect) — deferred to later MVP. Do NOT implement this module.


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

> **⚠️ DEFERRED — Not in MVP2.** No roles (Coach) — deferred to later MVP. Do NOT implement this module.


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

> **⚠️ DEFERRED — Not in MVP2.** No roles (Coach triggers) — deferred with Coach. Do NOT implement this module.


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

> **⚠️ DEFERRED — Not in MVP2.** No context assembly v2 — deferred to later MVP. Do NOT implement this module.


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
    - Inject "previous path was a dead end, try a different approach"
    - 2 more steps to recover

Level 2: Backtrack (automatic)
    - Reset stuck counters to 0 (steps_in_task, retry_count, tool_repeat_count, last_tool_hash)
    - Write stuck details to EVENT_JOURNAL
    - Resume (Coach is deferred in MVP2 — not triggered)

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
    stuck_result: StuckResult | None = None,
    escalation_history: list[EscalationLevel] | None = None,
) -> AgentState:
    """Execute the escalation action.

    Level 1: update state, inject prompt is handled by caller
    Level 2: reset stuck counters to 0, write EVENT_JOURNAL, resume
    Level 3: write STUCK_REPORT.md (needs stuck_result + escalation_history), notify Telegram, pause

    stuck_result and escalation_history are required for Level 3 (write_stuck_report).
    Pass None for lower levels — they are not used.

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
    ├── 2 steps allowed (diversification_max_steps)
    └── still stuck after 2 steps?
        │
        ▼
Level 2: Backtrack (Coach deferred in MVP2)
    ├── reset steps_in_task, retry_count, tool_repeat_count, last_tool_hash → 0
    ├── write stuck event to EVENT_JOURNAL
    ├── resume (no Coach in MVP2 — Coach triggered here in future MVP)
    └── still stuck after backtrack?
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

> **Implementation note:** Current code implements stuck detection + escalation inside a single `_execute_plan()` method rather than the `_execute_sora()` / `_run_slice()` / `_execute_with_escalation()` split described below. The behavior is equivalent for MVP2 scope. The split into separate methods is the target architecture for when Scout/Architect/Coach are implemented.
>
> **Other current divergences:**
> - `_build_chain(start_index)` is used instead of `_build_role_chain(role_name)` (see G16 in mvp1 spec — not yet migrated).
> - `reflexion.py` is not yet implemented — the import below is deferred; retry loop runs without failure context injection.
> - `Notifier` uses `async def send/notify` (fire-and-forget with `await`) rather than sync. This is acceptable.

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
# NOTE: PersonaRegistry is deferred — not imported in MVP2
from src.providers import create_provider
from src.providers.chain import ProviderChain

# NOTE: Scout, Architect, Builder, Verifier, Coach, plan_hardening,
#       context_assembly, and CoachTrigger are all deferred — not imported in MVP2.
from src.stuck_detection import check_stuck, StuckSignal, update_tool_hash
from src.escalation import (
    decide_escalation, execute_escalation, EscalationLevel, EscalationAction,
)
# NOTE: reflexion is deferred — reflexion.py not yet implemented.
# from src.reflexion import ReflexionContext, add_attempt
from src.errors import TaskFailedError, LockHeldError
from src.constants import MAX_TASK_RETRIES, HARD_TIMEOUT_S

log = logging.getLogger(__name__)

# NOTE: SoraPhase enum (HARDENING, SCOUT, COACH, ARCHITECT, REASSESS) is deferred.
# MVP2 uses the standard Phase enum from src.state (IDLE/RUNNING/PAUSED/COMPLETED/FAILED).


class Runner:
    """SORA Dispatcher v2 — stuck detection + escalation.

    Extends the MVP1 single-executor Runner with structural stuck detection
    and 3-level escalation.

    NOTE: Scout, Coach, Architect, Builder/Verifier, persona loading, and
    Plan Hardening are all deferred — not in MVP2. MVP2 wraps the MVP1
    single-executor loop with the stuck detection + escalation layer only.
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
        # NOTE: persona_registry is deferred — not instantiated in MVP2
        self.cb_registry = CircuitBreakerRegistry(
            failure_threshold=self.config.retry.cb_failure_threshold,
            recovery_timeout_s=self.config.retry.cb_recovery_timeout_s,
        )
        # Escalation tracking (instance-level — persists across _execute_with_escalation calls)
        self._escalation_level = EscalationLevel.NONE
        self._diversification_steps = 0
        self._escalation_history: list[EscalationLevel] = []

    async def run(self) -> None:
        """Main entry point. Same lock/init pattern as MVP0."""
        ...

    async def _execute_sora(self, state: AgentState) -> None:
        """MVP2 execution cycle — single executor with stuck detection + escalation.

        Reads plan, runs executor loop with stuck detection signals and escalation
        wrapping each attempt.

        NOTE: Scout/Coach/Architect/Builder/Verifier/Plan Hardening are all
        deferred — not in MVP2.
        """
        ...

    async def _run_slice(self, slice_id: str, state: AgentState) -> None:
        """Execute one Slice with stuck detection + escalation (MVP2 scope).

        MVP2 flow: single executor loop where each attempt is checked for
        stuck signals and routed through the 3-level escalation logic.

        NOTE: Scout, Coach, Architect, Builder/Verifier, and Plan Hardening
        are all deferred — not in MVP2. Those steps appear only in the
        Future Vision diagram.
        """
        ...

    # ── Phase methods (deferred) ─────────────────────────────────
    # Scout, Coach, Architect, and Builder/Verifier are deferred.
    # Do NOT implement _phase_scout, _phase_coach, _phase_architect,
    # or _phase_execute in MVP2.

    async def _execute_with_escalation(
        self,
        plan: str,
        state: AgentState,
    ) -> str:
        """Execute plan with stuck detection + escalation around the
        MVP1 single-executor loop.

        Inner loop per attempt:
            1. Check stuck signals → escalation decision
            2. Run executor (single call — no Builder/Verifier)
            3. On success → return output
            4. On failure → Reflexion → retry
            5. Stuck Level 1 → diversification inject
            6. Stuck Level 2 → backtrack to checkpoint
            7. Stuck Level 3 → write STUCK_REPORT.md, PAUSE runner

        Returns executor output on success.
        Raises RunnerError if all escalation levels exhausted.
        """
        ...

    # ── Helper methods ──────────────────────────────────────────

    def _build_role_chain(self, role_name: str) -> ProviderChain:
        """Build ProviderChain for a role from config.

        NOTE: Role chains (builder, verifier, scout, etc.) are deferred.
        In MVP2 only the single executor provider is used.
        """
        ...

    def _check_phase_boundary(self, state: AgentState) -> None:
        """Check for overrides at phase boundaries.

        Called between every attempt:
            - Read OVERRIDE.md → handle PAUSE/STOP
            - Read STEER.md → inject into next executor prompt

        NOTE: Coach triggers are deferred — not checked in MVP2.
        """
        ...
```

**Dispatcher v2 execution (detailed pseudocode):**

```
_execute_sora(state):
    # plan_file = state.plan_file (set by project_init or CLI --plan arg)
    # NOTE: no persona_registry.load_all() — persona system is deferred
    plan = disk.read_plan(plan_file)

    state.phase = Phase.RUNNING
    disk.write_state(state)
    notifier.notify("starting execution (MVP2: stuck detection + escalation)", PROGRESS)

    # Single Slice (multi-Slice is deferred)
    slice_id = "S01"
    await _run_slice(slice_id, state)

    # Milestone complete
    checkpoint.mark_completed(state)
    notifier.notify("milestone complete", DONE)


_run_slice(slice_id, state):
    # MVP2: wraps single executor with stuck detection + escalation.
    # Scout / Coach / Architect / Plan Hardening are deferred.
    _check_phase_boundary(state)
    output = await _execute_with_escalation(plan=disk.read_plan(plan_file), state=state)

    disk.write_file(f"milestones/M001/{slice_id}/RESULT.md", output)
    notifier.notify(f"Slice {slice_id} complete", PROGRESS)


_execute_with_escalation(plan, state):
    # MVP2 adds stuck detection + escalation around the MVP1 executor loop.
    # No Builder/Verifier/persona — single executor call per attempt.
    # Escalation state is on self (instance), not local — persists correctly.

    for attempt in range(config.retry.max_retries):
        # ── Attempt-boundary stuck check (RETRY_EXHAUSTED, STEP_LIMIT) ──
        inject_prompt = ""
        if attempt > 0:
            stuck = check_stuck(state, config.stuck_detection)
            if stuck.signal != StuckSignal.NONE:
                action = decide_escalation(
                    stuck, self._escalation_level,
                    self._diversification_steps, config.escalation
                )
                if action.level != EscalationLevel.NONE:
                    self._escalation_history.append(action.level)
                state = await execute_escalation(
                    action, state, disk, notifier, checkpoint,
                    stuck_result=stuck,
                    escalation_history=self._escalation_history,
                )
                if action.should_pause:
                    return  # Level 3: PAUSE, waiting for human
                self._escalation_level = action.level
                if action.level == EscalationLevel.DIVERSIFICATION:
                    self._diversification_steps += 1
                elif action.level == EscalationLevel.BACKTRACK_COACH:
                    self._diversification_steps = 0
                inject_prompt = action.inject_prompt

        # ── Override / PAUSE check at phase boundary ──
        _check_phase_boundary(state)

        effective_plan = plan
        if inject_prompt:
            effective_plan = f"## Notice\n{inject_prompt}\n\n---\n\n{plan}"

        # ── Inner message loop — per-step tool hash tracking + TOOL_REPEAT detection ──
        success = False
        async for message in chain.run_prompt(effective_plan):
            msg_type = message.type  # "tool_result" or "turn_end"

            if msg_type == "tool_result":
                # update_tool_hash MUST be called here (not at attempt boundary)
                # — this is the only place TOOL_REPEAT can be detected
                state, _ = update_tool_hash(state, str(message))
                # NOTE: checkpoint.increment_step must NOT raise RuntimeError
                # when max_steps_per_task is exceeded. The STEP_LIMIT signal from
                # check_stuck (called immediately below) handles this gracefully.
                # Just increment and let stuck detection decide what to do.
                state = checkpoint.increment_step(state)

                # Mid-attempt stuck check for TOOL_REPEAT
                mid_stuck = check_stuck(state, config.stuck_detection)
                if mid_stuck.signal == StuckSignal.TOOL_REPEAT:
                    # Break inner loop — outer loop will handle escalation next attempt
                    notifier.notify("tool repeat detected — aborting attempt", STUCK)
                    break

            elif msg_type == "turn_end":
                success = True
                break

        if success:
            checkpoint.mark_completed(state)
            notifier.notify("done", DONE)
            return

        # Failure → increment retry counter for next attempt's stuck check
        state = checkpoint.increment_retry(state)
        notifier.notify(f"attempt {attempt + 1} failed, retrying", PROGRESS)

    checkpoint.mark_failed(state, "all retries exhausted")
    notifier.notify(f"failed after {config.retry.max_retries} attempts", ERROR)
```

---

## 6. Bundled Prompt Files

> **⚠️ DEFERRED — Not in MVP2.** Bundled role prompt files (scout.md,
> architect.md, coach.md, builder.md, verifier.md) are deferred with the
> persona/prompt system and the role decomposition. Do NOT create `prompts/`
> or any bundled prompt `.md` files in MVP2.

---

## 7. Updated State Model

MVP2 adds stuck-detection and escalation fields to `AgentState`:

```python
@dataclass
class AgentState:
    """Updated for MVP2 — adds stuck detection and escalation tracking."""
    phase: Phase = Phase.IDLE
    # NOTE: sora_phase (scout/coach/architect/execute) is deferred — not in MVP2
    current_task: str = ""
    retry_count: int = 0
    steps_in_task: int = 0
    last_tool_hash: str = ""
    tool_repeat_count: int = 0        # NEW: consecutive same-hash count (stuck detection)
    last_checkpoint: str = ""
    provider_index: int = 0
    started_at: str = ""
    updated_at: str = ""
    error_message: str = ""
    plan_file: str = ""
    escalation_level: int = 0         # NEW: current escalation level (0-3)
    # NOTE: coach_invocations, tasks_completed, tasks_total are deferred
    #       with Coach invocation and multi-task slice tracking.
```

---

## 8. Implementation Order

> **NOTE:** Steps marked **[deferred]** are NOT in MVP2 — they belong to a
> future MVP that adds role decomposition (Scout/Coach/Architect/Builder/Verifier).
> MVP2 implements only the control-plane additions: stuck_detection, escalation,
> and Dispatcher v2.

```
Track C (control — MVP2 scope):
────────────────────────────────
1. stuck_detection.py
2. escalation.py
3. runner.py (Dispatcher v2)
4. config.py updates
5. state.py updates
6. tests

Deferred (future MVP):
──────────────────────
[deferred] players/scout.py
[deferred] players/architect.py
[deferred] context_assembly.py v2
[deferred] triggers.py
[deferred] coach_runner.py
[deferred] prompts/*.md
```

| Step | Module | Depends On |
|------|--------|------------|
| 1 | `stuck_detection.py` | state, config |
| 2 | `escalation.py` | state, disk_layer, notifier, checkpoint, config |
| 3 | Update `runner.py` | stuck_detection, escalation |
| 4 | Update `config.py` | — |
| 5 | Update `state.py` | — |
| 6 | Tests | all above |

Steps 1 and 2 can run in parallel.

---

## 9. Acceptance Criteria

**MVP2 (stuck detection + escalation — implement these):**
- [ ] Stuck detection: 3+ retries on same task → signal raised
- [ ] Stuck detection: 15+ steps on a task → signal raised
- [ ] Stuck detection: same tool call repeated 2x → deadlock signal
- [ ] Escalation Level 1: diversification prompt injected, 2 extra steps
- [ ] Escalation Level 2: backtrack to checkpoint, resume
- [ ] Escalation Level 3: STUCK_REPORT.md written, Telegram voice notification, runner PAUSEd
- [ ] After PAUSE: edit STEER.md → runner resumes with new direction
- [ ] OVERRIDE.md with "PAUSE" → runner pauses at next phase boundary
- [ ] `ruff check src/` clean, `pytest tests/` green

**Deferred (future MVP — do NOT implement in MVP2):**
- ~~Scout maps codebase → CONTEXT_MAP.md~~ — deferred
- ~~Coach invoked on first/end-of-slice/anomaly~~ — deferred
- ~~Architect produces S01-PLAN.md with Tasks~~ — deferred
- ~~Builder + Verifier execute Tasks~~ — deferred
- ~~Escalation Level 2 triggers Coach~~ — deferred (Coach is deferred; Level 2 backtracks + resumes without Coach)
- ~~`tero2 status` shows SORA phase, slice, task N/M~~ — deferred (no slices/tasks in MVP2)
- ~~Per-role cost tracking in metrics.json~~ — deferred (no roles in MVP2)

---

## 10. What MVP2 Does NOT Include

**Deferred from original MVP2 scope:**
- **No roles** (Scout, Architect, Builder, Verifier, Coach) — no specialized agent roles in MVP2; executor continues to handle everything
- **No Context Assembly v2** — CONTEXT_MAP, CONTEXT_HINTS, STRATEGY injection — deferred to later MVP

**Already deferred to MVP3+:**
- **No Debugger** (Frankenstein composite) — MVP3
- **No semantic loop detection** (cosine similarity) — MVP3
- **No voice input** (STT/Concierge) — MVP4
- **No parallelism** (worktree isolation) — MVP5
- **No multi-Milestone** — single Milestone scope

MVP2 focuses on structural stuck detection and escalation only. The executor model from MVP0/MVP1 is unchanged. Role decomposition (Scout/Architect/Builder/Verifier/Coach) comes in a future MVP.
