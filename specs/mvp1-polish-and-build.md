# MVP1 — Polish and Build

> Specification for Claude Code implementation agent.
> Version: 1.0 | Status: Draft
> Prerequisite: MVP0 (Immortal Runner) fully implemented.

## 1. What This MVP Achieves

**Problem:** The executor retries blindly with no failure context. Plans arrive only from the CLI. No Telegram workflow.

**After MVP1:** Send a markdown plan (file or Telegram message) → tero2 executes → retries with reflexion (failure context injected) → notifies you when done.

**Key additions over MVP0:**
- **Reflexion** — failed attempts inject failure context into retries
- **Telegram input** — receive plans via Telegram, create projects automatically

**Deferred (not in MVP1):**
- **Plan Hardening** — iterative adversarial review — deferred to later MVP
- **Builder + Verifier roles** — specialized roles with distinct prompts — deferred to later MVP
- **Persona system** — role prompts loaded from `.md` files — deferred to later MVP
- **Context Assembly** — smart prompt construction with budget control — deferred to later MVP

---

## 2. Architecture Delta

```
MVP0 modules (unchanged):
    constants, errors, config, state, lock, disk_layer,
    circuit_breaker, providers/, notifier, checkpoint

MVP1 adds:
                        ┌──────────────┐
                        │ telegram_    │  ← receives plans from Telegram
                        │ input.py     │
                        └──────┬───────┘
                               │
                        ┌──────▼───────┐
                        │ project_     │  ← creates project + .sora/
                        │ init.py      │
                        └──────┬───────┘
                               │
                    ┌──────────▼──────────┐
                    │  runner.py (v1)     │  ← Dispatcher v1
                    │  Plan → Execute →  │
                    │  Reflexion →       │
                    │  Complete          │
                    └──────────┬──────────┘
                               │
                        ┌──────▼───────┐
                        │ reflexion.py │  ← failure context injection
                        └──────────────┘
```

**Not in MVP1 (deferred):** plan_hardening.py, persona.py, context_assembly.py, players/builder.py, players/verifier.py

---

## 3. Updated Directory Structure

New files added to the MVP0 tree:

```
src/
├── ... (MVP0 modules)
├── reflexion.py                  # NEW — failure context injection
├── telegram_input.py             # NEW — Telegram bot for receiving plans
├── project_init.py               # NEW — project scaffolding
└── runner.py                     # UPDATED — Dispatcher v1 with reflexion
```

**Not added in MVP1 (deferred):**
```
# DEFERRED — not in MVP1:
# ├── persona.py                  # persona/prompt registry
# ├── context_assembly.py         # smart prompt builder
# ├── plan_hardening.py           # iterative plan review
# ├── players/
# │   ├── builder.py              # code writer player
# │   └── verifier.py             # quality gate player
# └── .sora/prompts/*.md          # bundled role prompt files
```

---

## 4. Updated Config

> **⚠️ NOT YET IMPLEMENTED** — `ReflexionConfig`, `Config.reflexion`, and `TelegramConfig.allowed_chat_ids` are not yet in `config.py`. `_parse_config` does not yet parse `[reflexion]` or `allowed_chat_ids`.

MVP1 adds reflexion and telegram settings to `config.toml`:

```toml
# ── Reflexion ───────────────────────────────────────────────────

[reflexion]
max_cycles = 2                    # max reflexion retries per task

# ── Telegram Input ──────────────────────────────────────────────

[telegram]
# ... (existing from MVP0, plus:)
allowed_chat_ids = ["614473938"]  # only accept from these chats
```

**Updated `src/config.py` dataclasses:**

```python
@dataclass
class ReflexionConfig:
    max_cycles: int = 2

@dataclass
class TelegramConfig:
    """Updated — add allowed_chat_ids (security filter for Telegram input)."""
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True
    allowed_chat_ids: list[str] = field(default_factory=list)   # NEW

@dataclass
class Config:
    """Updated root config — add reflexion section."""
    # ... existing MVP0 fields ...
    reflexion: ReflexionConfig = field(default_factory=ReflexionConfig)
```

**Note on `_parse_config`:** When loading `[telegram]` from TOML, `allowed_chat_ids` must be parsed as `list[str]` (TOML array of strings). If missing from config file, defaults to `[]`.

**Deferred config (not in MVP1):**
```toml
# NOT IN MVP1 — deferred:
# [roles.builder], [roles.verifier], [roles.reviewer]  — no role system
# [plan_hardening]                                      — no plan hardening
# [context]                                             — no context assembly
```

---

## 4.X Updated `src/errors.py`

No new exceptions required for MVP1. `ContextWindowExceededError` is deferred with context assembly.

---

## 4.Y Updated `src/state.py` — Phase enum

No changes to `Phase` enum required for MVP1. The `HARDENING` and `EXECUTING` phases are deferred with plan hardening and the Builder/Verifier role system.

MVP0 Phase enum is sufficient:
```python
class Phase(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
```

---

## 5. Module Specifications

---

### 5.1 `src/persona.py`

> **⚠️ DEFERRED — Not in MVP1.** No persona/prompt system — deferred to later MVP. Do NOT implement this module.


**Purpose:** Load role prompts from `.md` files with YAML frontmatter.

**Port from v1:** Adapt `/Users/terobyte/Desktop/Projects/Active/tero/src/personas/registry.py` (144 lines).

**Changes from v1:**
- Drop `PersonaEntry` compat alias (unused in tero2)
- Drop `build_overlay()` multi-role combiner (tero2 uses 1 role per agent)
- Replace `yaml.safe_load` with stdlib frontmatter parsing (drop PyYAML dependency)
- Add `get_or_raise()` method

```python
"""Persona registry — load role prompts from .md files.

Each .md file has YAML-like frontmatter:
    ---
    name: builder
    description: Code writer agent.
    ---
    # Builder — ...
    (markdown body = system prompt overlay)

The frontmatter parser is minimal (no PyYAML dependency):
splits on '---', parses 'key: value' lines.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass
class Persona:
    """A loaded persona with its system prompt overlay."""
    name: str
    description: str
    overlay: str              # the markdown body — injected as system prompt


class PersonaRegistry:
    """Registry for loading and caching personas from .md files.

    Args:
        dir_path: Directory containing persona .md files.
    """

    def __init__(self, dir_path: Path) -> None:
        self._dir = Path(dir_path)
        self._cache: dict[str, Persona] = {}

    def load_all(self) -> list[Persona]:
        """Scan directory for .md files and parse each as a persona.

        Returns list of loaded personas. Also caches them internally.
        """
        ...

    def get(self, name: str) -> Persona | None:
        """Return cached persona by name, or None."""
        ...

    def get_or_raise(self, name: str) -> Persona:
        """Return cached persona by name. Raises KeyError if not found."""
        ...

    def list_names(self) -> list[str]:
        """Return sorted list of all loaded persona names."""
        ...

    @staticmethod
    def _parse_md(text: str) -> Persona | None:
        """Parse a .md file with frontmatter into a Persona.

        Frontmatter format (simplified, no PyYAML):
            ---
            name: <value>
            description: <value>
            ---
            <markdown body>

        Returns None if parsing fails.
        """
        ...
```

**Frontmatter parser rules (no PyYAML):**
- Text must start with `---\n`
- Use `text.split("---", 2)` — split on the **first two** `---` delimiters only.
  This produces `['', frontmatter, body]` even when the body contains `---` (e.g. markdown
  horizontal rules). Do NOT use a plain `split("---")` which would yield more than 3 parts.
- Parse frontmatter as `key: value` lines (one per line, strip whitespace)
- Required key: `name`
- Optional key: `description` (defaults to `""`)
- Body is everything after the second `---`, stripped
- Return `None` if split does not yield exactly 3 parts or `name` key is missing

**Prompt files location:**
- Default: `<project>/.sora/prompts/`
- Fallback: `<tero2_root>/prompts/` (bundled defaults)

---

### 5.2 `src/context_assembly.py`

> **⚠️ DEFERRED — Not in MVP1.** No context assembly — deferred to later MVP. Do NOT implement this module.


**Purpose:** Build the full prompt for an agent with budget control. Ensures the agent gets exactly the right amount of context — not too much, not too little.

**Dependencies:** `src.config` (`ContextConfig`), `src.disk_layer`

**New module.**

```python
"""Context Assembly — smart prompt builder with budget control.

Each agent starts with a clean context (Iron Rule). This module assembles
the pre-inlined prompt: system prompt + plan + summaries + hints,
respecting the context window budget.

Key insight (Sweep, 7.7K★): quality peaks at 10-15K tokens of context,
NOT at maximum window. More context = worse results.
"""

from __future__ import annotations

from pathlib import Path

from src.config import ContextConfig
from src.disk_layer import DiskLayer
from src.errors import ContextWindowExceededError  # noqa: F401 — re-exported for callers


class ContextBudget:
    """Result of budget check."""
    OK = "ok"
    WARNING = "warning"
    COMPRESS = "compress"
    HARD_FAIL = "hard_fail"


def estimate_tokens(text: str) -> int:
    """Estimate token count from text. ~4 chars per token for English.

    This is a rough heuristic. Accurate counting requires a tokenizer,
    which is overkill for budget control.
    """
    return len(text) // 4


def check_budget(
    assembled_tokens: int,
    model_limit: int,
    config: ContextConfig,
) -> str:
    """Check context budget status.

    Returns one of: ContextBudget.OK, WARNING, COMPRESS, HARD_FAIL
    """
    ratio = assembled_tokens / model_limit if model_limit > 0 else 1.0
    if ratio > config.hard_fail_ratio:
        return ContextBudget.HARD_FAIL
    if ratio > config.warning_ratio:
        return ContextBudget.COMPRESS
    if ratio > config.target_ratio:
        return ContextBudget.WARNING
    return ContextBudget.OK


def assemble_context(
    role_prompt: str,
    task_plan: str,
    summaries: list[str] | None = None,
    context_hints: str = "",
    model_limit: int = 200_000,
    config: ContextConfig | None = None,
) -> str:
    """Assemble the full prompt for an agent.

    The returned string is a single document passed verbatim to the CLI provider.
    It includes both the system prompt overlay and the user task — concatenated with
    clear section delimiters (e.g. "## Role\n{role_prompt}\n\n## Task\n{task_plan}").

    Priority (what gets cut last → first):
        1. role_prompt     — NEVER cut
        2. task_plan       — NEVER cut
        3. context_hints   — cut at COMPRESS (MVP2+, empty string in MVP1)
        4. summaries       — cut first (oldest first)

    Args:
        role_prompt: System prompt for this role (from PersonaRegistry).
        task_plan: The plan/task to execute.
        summaries: Previous task summaries (oldest first). Lossy compression.
        context_hints: Hints from Coach (MVP2+). Empty string in MVP1.
        model_limit: Context window token limit for the model.
        config: Context budget config. Uses defaults if None.

    Returns:
        Assembled prompt string within budget. Passed as a single user message
        to ProviderChain.run_prompt().

    Raises:
        ContextWindowExceededError: If mandatory parts alone exceed budget.
    """
    ...


def assemble_builder_prompt(
    role_prompt: str,
    task_plan: str,
    previous_summaries: list[str],
    reflexion_context: str = "",
    config: ContextConfig | None = None,
) -> str:
    """Convenience: assemble prompt for Builder role.

    Includes reflexion context (failure feedback) if present.
    """
    ...


def assemble_verifier_prompt(
    role_prompt: str,
    task_plan: str,
    must_haves: list[str],
    config: ContextConfig | None = None,
) -> str:
    """Convenience: assemble prompt for Verifier role.

    Includes must-haves as a checklist for verification.
    """
    ...


def assemble_reviewer_prompt(
    role_prompt: str,
    plan_content: str,
    review_round: int,
    config: ContextConfig | None = None,
) -> str:
    """Convenience: assemble prompt for Reviewer role (Plan Hardening).

    Includes round number so the reviewer knows it's pass N.
    """
    ...
```

**Assembly algorithm (greedy):**

```
1. Start with mandatory parts: role_prompt + task_plan
   Format: "## Role\n{role_prompt}\n\n## Task\n{task_plan}"
2. Check budget — if already HARD_FAIL, raise ContextWindowExceededError
3. Add optional parts in priority order:
   a. context_hints (if fits) — empty in MVP1, reserved for MVP2 Coach
   b. summaries (if fits, most recent first — cut oldest first)
4. After each addition, check budget:
   - OK / WARNING → continue
   - COMPRESS → stop adding, return what we have
   - HARD_FAIL → remove last added item, return
5. Return assembled prompt as single string
```

**Note:** `code_snippets` parameter removed — it was dead weight (no caller used it). Reserved for MVP2 Scout context injection.

---

### 5.3 `src/plan_hardening.py`

> **⚠️ DEFERRED — Not in MVP1.** No plan hardening — deferred to later MVP. Do NOT implement this module.


**Purpose:** Iterative adversarial review of the plan before any code is written. Each round uses a fresh context (Iron Rule).

**Dependencies:** `src.persona`, `src.context_assembly`, `src.providers`, `src.config`, `src.disk_layer`, `src.notifier`

**ProviderChain interface for MVP1 modules:**

`ProviderChain.run_prompt(prompt: str) -> str` — send a single assembled prompt to the CLI provider and collect the full response as a string. The prompt already contains both the role overlay and the task (produced by `assemble_context()`). This is a blocking async call.

```python
# Add to src/providers/chain.py (MVP1 addition):

async def run_prompt(self, prompt: str) -> str:
    """Send a single assembled prompt and return the full response.

    Used by plan_hardening, builder, verifier — all of which assemble
    a single prompt document via assemble_context() and expect a string back.

    Internally converts to the provider's native message format:
        [{"role": "user", "content": prompt}]

    Retries and fallback logic are handled by the existing run() machinery.
    """
    ...
```

**New module.**

```python
"""Plan Hardening — iterative adversarial review.

Convergence loop: review the plan with a fresh-context reviewer,
apply fixes, repeat until no critical issues found or max rounds hit.

Each review round is a fresh agent (Iron Rule — no accumulated context).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from src.config import Config, PlanHardeningConfig
from src.disk_layer import DiskLayer
from src.persona import PersonaRegistry
from src.providers.chain import ProviderChain

log = logging.getLogger(__name__)


@dataclass
class HardeningResult:
    """Result of the plan hardening process."""
    hardened_plan: str          # the final plan text after all rounds
    rounds_completed: int      # how many review rounds ran
    total_issues_found: int    # total issues across all rounds
    converged: bool            # True if stopped due to no issues, not max rounds


async def harden_plan(
    plan_content: str,
    reviewer_chain: ProviderChain,
    persona_registry: PersonaRegistry,
    config: PlanHardeningConfig,
    disk: DiskLayer,
) -> HardeningResult:
    # Internally loads two personas:
    #   persona_registry.get_or_raise("reviewer_review")  — find-issues mode
    #   persona_registry.get_or_raise("reviewer_fix")     — apply-fixes mode
    """Run the plan hardening convergence loop.

    Algorithm:
        1. Send plan to Reviewer agent with "find issues" prompt
        2. Parse response: extract issues found
        3. If 0 issues or cosmetic-only → plan is hardened, stop
        4. Apply fixes (send plan + issues back to reviewer: "fix these")
        5. Repeat from 1 with the fixed plan (new fresh context)
        6. Stop after max_rounds regardless

    Args:
        plan_content: The raw plan markdown.
        reviewer_chain: ProviderChain for the reviewer role.
        persona_registry: To get the reviewer persona prompt.
        config: Plan hardening config (max_rounds, stop_on_cosmetic_only).
        disk: DiskLayer for writing intermediate plans.

    Returns:
        HardeningResult with the final hardened plan.
    """
    ...


async def _run_review_round(
    plan: str,
    round_number: int,
    reviewer_chain: ProviderChain,
    review_prompt: str,
) -> tuple[str, int, bool]:
    """Run one review round (find issues only — does NOT apply fixes).

    The review prompt instructs the reviewer to output a structured JSON block:
        ```json
        {"critical": 3, "improvements": 2, "issues": ["...", "..."]}
        ```
    This is more reliable than parsing free text for "[Проблем: N]" patterns.

    Args:
        plan: Current plan text.
        round_number: 1-indexed round number.
        reviewer_chain: Provider chain for reviewer.
        review_prompt: System prompt instructing reviewer to FIND issues (not fix).

    Returns:
        (review_output, issues_count, is_cosmetic_only)
    """
    ...


async def _apply_fixes(
    plan: str,
    review_output: str,
    reviewer_chain: ProviderChain,
    fix_prompt: str,
) -> str:
    """Ask the reviewer to apply its own fixes to the plan.

    Uses a DIFFERENT prompt from review — fix mindset differs from review mindset.
    The fix prompt instructs the agent to output ONLY the corrected plan text
    (no commentary), so it can be written directly to disk.

    Args:
        plan: Current plan text.
        review_output: The structured review JSON from _run_review_round.
        reviewer_chain: Provider chain for reviewer.
        fix_prompt: System prompt instructing reviewer to APPLY fixes (not find).

    Returns the updated plan text.
    """
    ...


def _count_issues(review_output: str) -> tuple[int, bool]:
    """Parse reviewer output to count issues.

    The review prompt instructs the model to output a JSON block:
        ```json
        {"critical": 3, "improvements": 2, "issues": ["...", "..."]}
        ```

    Parsing strategy:
        1. Extract content between ```json and ``` markers
        2. json.loads() the block
        3. Read "critical" and "improvements" keys
        4. is_cosmetic_only = True iff critical == 0 (improvements-only)
        5. Fallback: if JSON parse fails, log warning and return (1, False)
           — conservative fallback that continues the loop rather than silently stopping

    Returns:
        (issue_count, is_cosmetic_only)
        issue_count = critical + improvements
        is_cosmetic_only = True if critical == 0
    """
    ...
```

**Hardening flow:**

```
Plan v0 (raw from user)
  │
  ▼ Round 1: fresh Reviewer context
  "Find issues: contradictions, unprovable must-haves, missing edges"
  │
  ▼ Response: "3 critical, 2 improvements"
  Apply fixes → Plan v1
  │
  ▼ Round 2: fresh Reviewer context (Iron Rule!)
  "Find issues" with Plan v1
  │
  ▼ Response: "1 improvement (cosmetic)"
  stop_on_cosmetic_only=true → STOP
  │
  ▼ Plan v1 is the hardened plan
```

**Behavior rules:**
- Each review round spawns a FRESH agent (no context carryover between rounds).
- **Two distinct prompts** — loaded from separate persona files:
  - `reviewer_review.md` — system prompt for FINDING issues (adversarial, nitpick mode)
  - `reviewer_fix.md` — system prompt for APPLYING fixes (constructive, editor mode)
  - Both loaded from `.sora/prompts/` via `PersonaRegistry`.
- `_run_review_round` uses `reviewer_review.md`; `_apply_fixes` uses `reviewer_fix.md`.
- Review prompt requires JSON output block: `{"critical": N, "improvements": N, "issues": [...]}`.
- Fix prompt requires the output to be ONLY the corrected plan markdown (no wrapper text).
- Issues classified: `critical` (blocking) vs `improvements` (cosmetic).
- Convergence: stop when `critical == 0` (improvements-only → cosmetic, stop if flag set).
- Hard cap: `max_rounds` (default 5) prevents infinite loops.
- Write each intermediate plan version to disk: `.sora/milestones/M001/plan_v{N}.md`

---

### 5.4 `src/players/builder.py`

> **⚠️ DEFERRED — Not in MVP1.** No roles (Builder) — deferred to later MVP. Do NOT implement this module.


**Purpose:** Code writing agent. Receives a task plan, writes code, commits.

**Dependencies:** `src.providers.chain`, `src.persona`, `src.context_assembly`, `src.disk_layer`

**New module.**

```python
"""Builder player — writes code for a single task.

Receives pre-assembled context (plan + summaries + hints).
Writes code, runs lint, commits. Produces SUMMARY.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from src.providers.chain import ProviderChain
from src.context_assembly import assemble_builder_prompt
from src.config import Config, ContextConfig
from src.disk_layer import DiskLayer
from src.persona import PersonaRegistry

log = logging.getLogger(__name__)


@dataclass
class BuilderResult:
    """Result from a builder run."""
    success: bool
    summary: str                # content written to {task_id}-SUMMARY.md
    output: str                 # raw agent output (for reflexion on failure)
    files_changed: list[str]    # paths of files created/modified
    error: str = ""             # error message if failed


async def run_builder(
    task_plan: str,
    previous_summaries: list[str],
    reflexion_context: str,
    builder_chain: ProviderChain,
    persona_registry: PersonaRegistry,
    working_dir: str,
    disk: DiskLayer,
    config: Config,
) -> BuilderResult:
    """Execute the Builder for a single task.

    1. Assemble context: role prompt + plan + summaries + reflexion
    2. Run the Builder agent via ProviderChain
    3. Collect output
    4. Extract summary from output
    5. Return result

    Args:
        task_plan: The specific task to execute (from PLAN.md).
        previous_summaries: Summaries of previously completed tasks.
        reflexion_context: Failure feedback from previous attempt (empty on first try).
        builder_chain: ProviderChain for the builder role.
        persona_registry: To get builder persona prompt.
        working_dir: Project directory where code is written.
        disk: DiskLayer for writing SUMMARY.
        config: Runtime config.

    Returns:
        BuilderResult with success/failure and summary.
    """
    ...


def _extract_summary(output: str) -> str:
    """Extract the task summary from builder output.

    Looks for a structured summary section in the output.
    If not found, generates a minimal summary from the output.
    """
    ...
```

**Builder agent interaction:**
- System prompt: `persona_registry.get("builder").overlay`
- User prompt: assembled context from `assemble_builder_prompt()`
- The builder is told to write code AND produce a summary at the end
- Output is streamed via `ProviderChain.run()`
- All text blocks are concatenated for the raw output
- Summary is extracted from the output (or auto-generated)

---

### 5.5 `src/players/verifier.py`

> **⚠️ DEFERRED — Not in MVP1.** No roles (Verifier) — deferred to later MVP. Do NOT implement this module.


**Purpose:** Quality gate. Runs tests, linters, checks must-haves. Decides PASS/FAIL/ANOMALY.

**Dependencies:** `src.providers.chain`, `src.persona`, `src.context_assembly`, `src.disk_layer`

**New module.**

```python
"""Verifier player — quality gate after Builder.

Runs real tests and linters. Checks must-haves from the plan.
Returns PASS, FAIL (with details), or ANOMALY.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum

from src.providers.chain import ProviderChain
from src.context_assembly import assemble_verifier_prompt
from src.config import Config
from src.disk_layer import DiskLayer
from src.persona import PersonaRegistry

log = logging.getLogger(__name__)


class Verdict(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    ANOMALY = "anomaly"


@dataclass
class VerifierResult:
    """Result from a verifier run."""
    verdict: Verdict
    details: str              # human-readable explanation
    failed_tests: list[str]   # specific test names that failed
    must_haves_checked: int   # how many must-haves were verified
    must_haves_passed: int    # how many must-haves passed
    anomaly_description: str = ""  # if verdict is ANOMALY


async def run_verifier(
    task_plan: str,
    must_haves: list[str],
    verifier_chain: ProviderChain,
    persona_registry: PersonaRegistry,
    working_dir: str,
    disk: DiskLayer,
    config: Config,
) -> VerifierResult:
    """Execute the Verifier for a completed task.

    1. Assemble context: verifier prompt + plan + must-haves checklist
    2. Run verifier agent — it executes real tests and linters
    3. Parse verdict from output
    4. Return structured result

    The verifier agent is instructed to:
        a. Run `ruff check .`
        b. Run `pytest -x` (if pytest is available)
        c. Check each must-have from the plan
        d. Output a structured verdict

    Args:
        task_plan: The task plan (contains must-haves).
        must_haves: List of must-have conditions extracted from plan.
        verifier_chain: ProviderChain for the verifier role.
        persona_registry: To get verifier persona prompt.
        working_dir: Project directory to verify.
        disk: DiskLayer for writing EVENT_JOURNAL on ANOMALY.
        config: Runtime config.

    Returns:
        VerifierResult with verdict and details.
    """
    ...


def _parse_verdict(output: str) -> tuple[Verdict, str]:
    """Parse the verifier's output to extract verdict.

    Looks for:
        PASS, FAIL, ANOMALY keywords in the output.
        Failed test names.
        Must-have check results.

    Returns:
        (verdict, details_text)
    """
    ...


def extract_must_haves(plan: str) -> list[str]:
    """Extract must-have items from a plan.

    Looks for:
        **Must-haves:**
        - [ ] condition 1
        - [ ] condition 2

    Or markdown checkbox patterns.

    Returns list of must-have strings.
    """
    ...
```

**Verifier flow:**
1. Verifier agent receives the plan with must-haves as a checklist
2. Agent runs real commands: `ruff check .`, `pytest -x`, etc.
3. Agent checks each must-have by inspecting the code
4. Agent outputs a structured verdict: PASS / FAIL / ANOMALY
5. On FAIL: `details` contains what broke and why
6. On ANOMALY: writes to `EVENT_JOURNAL.md` (for Coach in MVP2)

---

### 5.6 `src/reflexion.py`

> **⚠️ NOT YET IMPLEMENTED** — `reflexion.py` does not exist. Retry loop runs without failure context injection.

**Purpose:** When Builder fails, inject the failure context into the next attempt. The agent learns from its mistakes within the same task.

**Dependencies:** `src.disk_layer`

**New module.**

```python
"""Reflexion — failure context injection for retries.

When Builder fails verification, the failure details are injected into
the next attempt's context. This gives the agent memory of what went wrong.

Max reflexion cycles: 2 (configurable). After that → escalate, don't loop.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.players.verifier import VerifierResult  # avoid circular import at runtime


@dataclass
class ReflexionContext:
    """Accumulated failure context across retry attempts."""
    attempts: list[ReflexionAttempt]

    def to_prompt(self) -> str:
        """Format reflexion context as a prompt section for the Builder.

        Example output:
            ## Previous Attempts (DO NOT repeat these mistakes)

            ### Attempt 1 — FAILED
            **What was tried:** wrote auth module with JWT
            **What failed:** pytest: test_token_expiry FAILED — token not expiring
            **Verifier feedback:** must-have "tokens expire after 1h" not satisfied
            **Avoid:** hardcoded expiry, missing time comparison

            ### Attempt 2 — FAILED
            ...
        """
        ...

    @property
    def is_empty(self) -> bool:
        return len(self.attempts) == 0


@dataclass
class ReflexionAttempt:
    """Record of one failed attempt."""
    attempt_number: int
    builder_output: str        # what the builder did (truncated)
    verifier_feedback: str     # why it failed
    failed_tests: list[str]    # specific test names
    must_haves_failed: list[str]  # which must-haves didn't pass


def build_reflexion_context(
    attempts: list[ReflexionAttempt],
) -> ReflexionContext:
    """Build reflexion context from a list of failed attempts.

    Truncates builder_output to avoid context overflow.
    """
    ...


def add_attempt(
    context: ReflexionContext,
    builder_output: str,
    verifier_result: "VerifierResult",
) -> ReflexionContext:
    """Add a failed attempt to the reflexion context.

    Args:
        context: Existing reflexion context (may be empty).
        builder_output: Raw output from the builder's failed run.
        verifier_result: The VerifierResult that caused the failure.

    Returns:
        Updated ReflexionContext with the new attempt appended.
    """
    ...
```

**Reflexion injection into Builder prompt:**

```
[System prompt: Builder persona]
[Plan: task details]
[Previous summaries]
[Reflexion context ← THIS IS NEW]
  "Previous Attempts (DO NOT repeat these mistakes)
   Attempt 1 — FAILED: ...
   Attempt 2 — FAILED: ..."
```

The `assemble_builder_prompt()` in `context_assembly.py` accepts `reflexion_context` as a string and inserts it between summaries and the plan.

---

### 5.7 `src/telegram_input.py`

> **⚠️ NOT YET IMPLEMENTED** — `telegram_input.py` does not exist. Telegram input workflow is unavailable.

**Purpose:** Receive plans via Telegram. Long-polling bot that listens for markdown files or text plans.

**Dependencies:** `src.config`, `src.project_init`, `src.notifier`

**External dependency:** None new — uses `requests` (already in MVP0 dependencies) for long-polling.
`python-telegram-bot` was considered but rejected: it's a heavy dependency (1400+ lines of framework)
for a simple long-polling bot. The `requests`-based `getUpdates` loop from MVP0's `notifier.py`
pattern is sufficient and keeps the dependency footprint minimal.

**New module.**

```python
"""Telegram input — receive plans and commands via Telegram bot.

Long-polling bot that:
    1. Accepts markdown files (.md) → creates project → starts runner
    2. Accepts text messages → treats as plan → creates project → starts runner
    3. Accepts commands: /status, /stop, /pause
    4. Only responds to allowed chat_ids (security)
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import requests  # stdlib-weight: already a project dependency from MVP0

from src.config import Config
from src.project_init import init_project
from src.notifier import Notifier

log = logging.getLogger(__name__)


class TelegramInputBot:
    """Telegram bot for receiving plans and commands.

    Args:
        config: tero2 Config with telegram settings.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self.notifier = Notifier(config.telegram)
        self._allowed_ids: set[str] = set(config.telegram.allowed_chat_ids)
        self._plan_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        # Queue holds (project_name, plan_content) tuples.
        # Polling loop enqueues; a separate consumer coroutine dequeues and processes.
        # This prevents the race where two rapid messages both try to acquire the lock:
        # the second would fail without a queue. With a queue, plans are serialized.

    async def start(self) -> None:
        """Start the long-polling loop and plan consumer. Blocks until stopped.

        Launches two coroutines concurrently:
            - _poll_loop(): getUpdates long-polling, enqueues plans
            - _consume_plans(): dequeues plans and runs init_project + runner
        """
        ...

    async def stop(self) -> None:
        """Stop the bot gracefully. Drains the queue before exiting."""
        ...

    async def _handle_message(self, update: dict) -> None:
        """Handle an incoming message.

        Flow:
            1. Check chat_id is allowed
            2. If document (.md file) → download → extract plan
            3. If text → use as plan
            4. Extract project name from plan (first heading or first line)
            5. Call init_project() to create project + .sora/
            6. Notify: "project created, starting"
            7. Launch runner in background (subprocess or asyncio task)
        """
        ...

    async def _handle_command(self, text: str, chat_id: str) -> None:
        """Handle slash commands: /status, /stop, /pause."""
        ...

    def _is_allowed(self, chat_id: str) -> bool:
        """Check if chat_id is in the allowed list."""
        return str(chat_id) in self._allowed_ids
```

**No new pyproject.toml dependencies** — `requests` is already listed in MVP0.

**Note:** This is a simple long-polling bot using Bot API (not MTProto). Implementation uses
`requests.post(f"{BASE_URL}/getUpdates", json={"offset": offset, "timeout": 30})`.
UserBot architecture (MTProto) comes in MVP6.

**Security:**
- Only responds to chat IDs in `allowed_chat_ids`
- All other messages are silently ignored
- No sensitive data echoed back

---

### 5.8 `src/project_init.py`

> **⚠️ NOT YET IMPLEMENTED** — `project_init.py` does not exist. Project creation via Telegram is unavailable.

**Purpose:** Create a new project directory with `.sora/` structure and git init.

**Dependencies:** `src.disk_layer`, `src.config`

**New module.**

```python
"""Project initialization — create project + .sora/ + git.

Creates the project under the configured projects_dir,
initializes git, and creates the .sora/ directory structure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from src.config import Config
from src.disk_layer import DiskLayer


def init_project(
    project_name: str,
    plan_content: str,
    config: Config,
) -> Path:
    """Create a new project and initialize .sora/.

    1. Sanitize project name (lowercase, replace spaces with hyphens)
    2. Create directory under config.projects_dir
    3. git init
    4. Create .sora/ structure via DiskLayer.init()
    5. Write plan to .sora/milestones/M001/ROADMAP.md
       Store this path in AgentState.plan_file so the runner can read it:
           state.plan_file = str(project_path / ".sora/milestones/M001/ROADMAP.md")

    NOTE: .sora/prompts/ is NOT created here — persona/prompt system is
    deferred. Do not call copy_default_prompts() in MVP1.

    Args:
        project_name: Name for the project (from plan heading or user input).
        plan_content: The markdown plan to write.
        config: tero2 config (for projects_dir path).

    Returns:
        Path to the created project directory.

    Raises:
        FileExistsError: If project directory already exists.
    """
    ...


def _sanitize_name(name: str) -> str:
    """Convert project name to directory-safe format.

    "My Cool Project" → "my-cool-project"
    """
    ...


def _extract_project_name(plan: str) -> str:
    """Extract project name from plan content.

    Uses first heading (# Title) or first non-empty line.
    """
    ...


# copy_default_prompts — DEFERRED. Not in MVP1.
# Persona/prompt system (.sora/prompts/) is deferred to a later MVP.
# Do NOT implement this function or create .sora/prompts/ in MVP1.
```

---

### 5.9 Updated `src/runner.py` — Dispatcher v1

**Purpose:** Updated runner with reflexion support on failure.

**Changes from MVP0:**
- Reflexion on executor failure (inject failure context into retry)
- Accepts plans from Telegram (via project_init)

**NOT changed from MVP0 (deferred):** No plan hardening, no Builder/Verifier roles, no persona system, no context assembly.

```python
"""Runner v1 — Dispatcher with reflexion on failure.

MVP1 execution flow:
    1. Read plan (file or from Telegram / project_init)
    2. Execute plan with executor provider
    3. On failure: Reflexion — inject failure context → retry
    4. After max retries → FAILED → notify Telegram
    5. Success → COMPLETED → notify Telegram
"""

from __future__ import annotations

# ... (existing MVP0 imports plus reflexion) ...


class Runner:
    """Updated runner with reflexion on failure."""

    def __init__(
        self,
        project_path: Path,
        plan_file: str,
        config: Config | None = None,
    ) -> None:
        # ... (MVP0 init — no persona_registry) ...

    async def run(self) -> None:
        """Main entry point. Unchanged structure from MVP0 but calls _execute_v1."""
        ...

    async def _execute_v1(self, state: AgentState) -> None:
        """MVP1 execution cycle — single executor with reflexion on failure.

        1. Read plan from state.plan_file
        2. Execute plan with executor provider (single call — no task splitting)
        3. On failure: inject failure context (Reflexion) → retry
        4. After max retries → FAILED → notify
        5. Success → COMPLETED → notify

        NOTE: Plan hardening, Builder/Verifier roles, persona loading, task
        splitting, and context assembly are all deferred — not in MVP1.
        """
        ...

    async def _execute_with_reflexion(self, plan: str, state: AgentState) -> str:
        """Execute plan with the executor provider, retrying on failure with
        Reflexion context injected into each retry attempt.

        Returns executor output string on success.
        Raises RunnerError after max retries exhausted.
        """
        ...
```

**Dispatcher v1 execution flow (detailed pseudocode):**

```
_execute_v1(state):
    # plan_file = state.plan_file (set by project_init or CLI --plan arg)
    plan = disk.read_plan(state.plan_file)

    state.phase = Phase.RUNNING
    disk.write_state(state)
    notifier.notify("starting execution", PROGRESS)

    reflexion = ReflexionContext(attempts=[])

    for attempt in range(config.reflexion.max_cycles + 1):
        result = await executor.run(
            plan=plan,
            reflexion_context=reflexion.to_prompt(),
            working_dir=str(project_path),
            config=config,
        )

        if result.success:
            break

        # Failure: build reflexion context for next attempt
        reflexion = add_attempt(reflexion, result.output, result.error)
        state = checkpoint.increment_retry(state)
        notifier.notify(f"attempt {attempt + 1} failed, retrying", PROGRESS)
    else:
        state.phase = Phase.FAILED
        state.error_message = str(result.error)
        checkpoint.mark_failed(state)
        notifier.notify("max retries reached — execution failed", ERROR)
        return

    # Success
    state.phase = Phase.COMPLETED
    checkpoint.mark_completed(state)
    notifier.notify("execution completed", DONE)
```

---

## 6. Bundled Prompt Files

> **⚠️ DEFERRED — Not in MVP1.** Bundled role prompt files (builder.md, verifier.md, reviewer_review.md, reviewer_fix.md) are deferred with the persona/prompt system. Do NOT create `.sora/prompts/` or bundled `.md` prompts in MVP1.

---

## 7. Implementation Order

```
Track A (core):       Track B (telegram):
─────────────────     ────────────────────
1. reflexion.py       2. project_init.py
                      3. telegram_input.py
              ───── MERGE ─────
          4. runner.py (Dispatcher v1 with reflexion)
          5. config.py updates (ReflexionConfig, allowed_chat_ids)
          6. tests
```

| Step | Module | Depends On |
|------|--------|------------|
| 1 | `reflexion.py` | — |
| 2 | `project_init.py` | disk_layer, config |
| 3 | `telegram_input.py` | config, project_init, notifier |
| 4 | Update `runner.py` | reflexion, telegram_input, project_init |
| 5 | Update `config.py` | — |
| 6 | Tests | all above |

Steps 1 and 2 can run in parallel.

**Not implemented in MVP1 (deferred):** persona.py, context_assembly.py, plan_hardening.py, players/builder.py, players/verifier.py, bundled prompts.

---

## 8. Acceptance Criteria

- [ ] Reflexion: on executor failure → failure context injected into retry prompt
- [ ] After max reflexion cycles → task marked FAILED → Telegram notification
- [ ] Run COMPLETED → Telegram "done" with voice
- [ ] Telegram: send `.md` file → project created → executor starts
- [ ] Telegram: send text plan → project created → executor starts
- [ ] Only allowed `chat_id`s can interact with the bot
- [ ] `tero2 status` shows current phase (RUNNING / COMPLETED / FAILED)
- [ ] `.sora/` has: `runtime/STATE.json`, project dir scaffolded by `project_init`
- [ ] `ruff check src/` clean, `pytest tests/` green

---

## 9. What MVP1 Does NOT Include

**Deferred from original MVP1 scope:**
- **No roles** (Builder, Verifier, Reviewer) — no specialized agent roles; executor handles everything
- **No plan hardening** — no adversarial review rounds before execution
- **No context assembly** — no smart prompt construction with budget control
- **No persona/prompt system** — no `.md` role prompt files, no PersonaRegistry

**Already deferred to MVP2+:**
- **No Scout** (codebase reconnaissance) — MVP2
- **No Architect** (auto-decomposition into Tasks) — MVP2
- **No Coach** (strategic advisor) — MVP2
- **No stuck detection** (semantic loops) — MVP2/MVP3
- **No escalation** (3-level) — MVP2
- **No CONTEXT_MAP or CONTEXT_HINTS** — MVP2
- **No voice input** (STT) — MVP4
- **No parallelism** — MVP5

MVP1 uses the same single-executor model as MVP0. The executor receives the plan directly. Auto-decomposition (Architect role) comes in a future MVP.

---

## 10. Gap Resolutions

This section resolves all design gaps identified during spec review. Each resolution is authoritative — the implementation agent MUST follow these decisions.

---

### G1. Import paths: `src/` → `tero2/`

**Gap:** Spec uses `src.config`, `src.providers`, etc. throughout. Actual package is `tero2/`.

**Resolution:** ALL import paths in this spec use `src.` as a placeholder. The implementation agent MUST replace `src.` with `tero2.` in all imports. Examples:

```python
# Spec says:          # Implementation uses:
from src.config ...   -> from tero2.config ...
from src.providers .. -> from tero2.providers ...
from src.persona ...  -> from tero2.persona ...
```

This applies to every module in sections 5.1-5.9.

---

### G2. `run_prompt()` signature collision

**Gap:** The existing `ProviderChain.run_prompt()` (MVP0) returns `AsyncGenerator`. The MVP1 spec needs a method that collects the full response as a `str`.

**Resolution:** Rename the new blocking method to `run_prompt_collected()`. Keep the existing `run_prompt()` unchanged for MVP0 backward compat.

```python
# Add to tero2/providers/chain.py -- new method (MVP1):

async def run_prompt_collected(self, prompt: str) -> str:
    """Send a single assembled prompt and return the full response as a string.

    Used by plan_hardening, builder, verifier -- all of which assemble
    a single prompt document via assemble_context() and expect a string back.

    Internally calls run_prompt() (AsyncGenerator) and collects all text content.
    """
    parts: list[str] = []
    async for msg in self.run_prompt(prompt):
        # Extract text from whatever message format the provider yields
        if isinstance(msg, str):
            parts.append(msg)
        elif isinstance(msg, dict):
            content = msg.get("content", "") or msg.get("text", "")
            if content:
                parts.append(str(content))
        else:
            # Object with .content or .text attribute
            text = getattr(msg, "content", None) or getattr(msg, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(parts)
```

**All MVP1 callers** (`plan_hardening.py`, `players/builder.py`, `players/verifier.py`) MUST use `run_prompt_collected()`, NOT `run_prompt()`.

---

### G3. State transitions for new phases

> **⚠️ DEFERRED — Not in MVP1.** `Phase.HARDENING` and `Phase.EXECUTING` are deferred with the plan-hardening and role-decomposition systems. The `_VALID_TRANSITIONS` table in `checkpoint.py` is unchanged from MVP0. Do NOT add HARDENING or EXECUTING transitions in MVP1.

---

### G4. AgentState fields for MVP1 tracking

> **⚠️ DEFERRED — Not in MVP1.** Fields for `hardening_round`, `hardening_max_rounds`, `current_task_index`, and `total_tasks` are deferred with the plan-hardening and multi-task execution systems. `AgentState` is unchanged from MVP0 for MVP1. The HARDENING and EXECUTING display mappings for `tero2 status` are likewise deferred.

---

### G5. CheckpointManager methods for new phases

> **⚠️ DEFERRED — Not in MVP1.** `mark_hardening()`, `update_hardening_round()`, `mark_executing()`, and `advance_task()` are all deferred with plan hardening and multi-task execution. No new checkpoint methods are added in MVP1.

---

### G6. Builder success/failure detection

**Gap:** How does `run_builder()` know if the Builder agent succeeded?

**Resolution:** The Builder is a CLI subprocess agent (opencode, codex, etc.). Success is determined by:

1. **Process exit code:** If the CLI provider subprocess exits non-zero, `ProviderChain` raises an exception -> `BuilderResult.success = False`.
2. **Output analysis:** If the process exits 0, the Builder is considered successful. The Builder prompt instructs the agent to produce a `## SUMMARY` section at the end of its output.
3. **No semantic success detection in MVP1.** The Verifier handles quality checks -- the Builder is "successful" if it runs without crashing. Quality is the Verifier's job.

```python
async def run_builder(...) -> BuilderResult:
    persona = persona_registry.get_or_raise("builder")
    prompt = assemble_builder_prompt(
        role_prompt=persona.overlay,
        task_plan=task_plan,
        previous_summaries=previous_summaries,
        reflexion_context=reflexion_context,
        config=config.context if hasattr(config, 'context') else None,
    )
    try:
        output = await builder_chain.run_prompt_collected(prompt)
    except Exception as exc:
        return BuilderResult(
            success=False, summary="", output="",
            files_changed=[], error=str(exc),
        )

    summary = _extract_summary(output)
    return BuilderResult(
        success=True, summary=summary, output=output,
        files_changed=[],  # populated by verifier in MVP2; empty in MVP1
    )
```

---

### G7. `_extract_summary()` algorithm

**Gap:** No parsing algorithm defined.

**Resolution:**

```python
import re

_SUMMARY_RE = re.compile(
    r"^#{1,3}\s*SUMMARY\s*\n(.*?)(?=^#{1,3}\s|\Z)",
    re.MULTILINE | re.DOTALL | re.IGNORECASE,
)

def _extract_summary(output: str) -> str:
    """Extract the task summary from builder output.

    1. Look for ## SUMMARY or ### SUMMARY section header
    2. Extract everything until next heading or end of text
    3. Fallback: last 500 chars of output, prefixed with "[auto-summary]"
    """
    match = _SUMMARY_RE.search(output)
    if match:
        return match.group(1).strip()
    # Fallback: truncated tail
    tail = output[-500:].strip() if len(output) > 500 else output.strip()
    return f"[auto-summary] {tail}"
```

---

### G8. Verifier tool execution model

**Gap:** Verifier "runs real commands" but it's a CLI subprocess. How does tool use work?

**Resolution:** The Verifier runs as a CLI subprocess agent (same as Builder). CLI providers (opencode, codex, kilo) have native tool-use capabilities -- they can run shell commands, read files, etc. within their own execution. tero2 does NOT intercept or parse tool calls.

The flow is:
1. tero2 sends the verifier prompt via `run_prompt_collected()`
2. The CLI subprocess agent (e.g., opencode) reads the prompt, runs `ruff check .`, `pytest -x`, etc. **internally** using its own tool-use
3. The agent produces a final text output with a structured verdict
4. tero2 parses ONLY the final text output -- it never sees individual tool calls

The verifier prompt (in `verifier.md`) must instruct the agent to output a verdict block:

```
## VERDICT: PASS|FAIL|ANOMALY
### Details
<explanation>
### Failed Tests
- test_name_1
- test_name_2
### Must-Haves
- [x] condition 1
- [ ] condition 2 (FAILED)
```

---

### G9. `_parse_verdict()` algorithm

**Gap:** No parsing logic defined.

**Resolution:**

```python
import re

_VERDICT_RE = re.compile(
    r"^#{1,3}\s*VERDICT:\s*(PASS|FAIL|ANOMALY)",
    re.MULTILINE | re.IGNORECASE,
)
_FAILED_TEST_RE = re.compile(r"^-\s+(\S+)", re.MULTILINE)
_MUST_HAVE_PASS_RE = re.compile(r"^-\s*\[x\]", re.MULTILINE | re.IGNORECASE)
_MUST_HAVE_FAIL_RE = re.compile(r"^-\s*\[\s*\]", re.MULTILINE)

def _parse_verdict(output: str) -> tuple[Verdict, str]:
    """Parse the verifier's structured output.

    1. Look for ## VERDICT: PASS|FAIL|ANOMALY header
    2. Extract details section after verdict
    3. Count failed tests from bullet list
    4. Fallback: if no VERDICT header found, assume FAIL with output as details
    """
    match = _VERDICT_RE.search(output)
    if not match:
        return Verdict.FAIL, f"[no verdict found in output] {output[:500]}"

    verdict_str = match.group(1).upper()
    verdict = Verdict(verdict_str.lower())
    details = output[match.end():].strip()
    return verdict, details
```

---

### G10. `_parse_tasks()` algorithm

> **⚠️ DEFERRED — Not in MVP1.** Multi-task plan parsing (`_parse_tasks()`) is deferred with the multi-task execution loop. MVP1 passes the entire plan to the single executor as one unit — no task splitting required.

---

### G11. `extract_must_haves()` algorithm

**Gap:** No regex or fallback defined.

**Resolution:**

```python
import re

# Pattern 1: Markdown checkboxes -- "- [ ] condition"
_MUST_HAVE_CHECKBOX_RE = re.compile(
    r"^[-*]\s*\[[\sx]\]\s+(.+)$", re.MULTILINE | re.IGNORECASE
)

# Pattern 2: Under "Must-haves" heading -- bullet items
_MUST_HAVES_SECTION_RE = re.compile(
    r"(?:^|\n)#{1,3}\s*Must[- ]?haves?\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
    re.DOTALL | re.IGNORECASE,
)
_BULLET_RE = re.compile(r"^[-*]\s+(.+)$", re.MULTILINE)

def extract_must_haves(plan: str) -> list[str]:
    """Extract must-have items from a plan.

    Strategy:
        1. Find a "Must-haves" section -> extract all bullets
        2. If no section, find all markdown checkboxes anywhere
        3. If nothing found -> return empty list (verifier runs without checklist)
    """
    # Try section-based extraction first
    section_match = _MUST_HAVES_SECTION_RE.search(plan)
    if section_match:
        section = section_match.group(1)
        items = _BULLET_RE.findall(section)
        if items:
            return [item.strip() for item in items]

    # Fall back to checkbox patterns anywhere
    checkboxes = _MUST_HAVE_CHECKBOX_RE.findall(plan)
    if checkboxes:
        return [cb.strip() for cb in checkboxes]

    return []
```

---

### G12. Context window limit wiring

**Gap:** `assemble_context()` takes `model_limit=200_000` but nobody provides the real value.

**Resolution:** The model limit comes from the provider's model config. Add a model context window lookup to `tero2/providers/chain.py`:

```python
# Context window sizes for known models (same table as zai.py).
_MODEL_CONTEXT_WINDOWS: dict[str, int] = {
    "glm": 128_000,
    "deepseek": 128_000,
    "qwen": 128_000,
    "mimo": 128_000,
    "claude": 200_000,
    "gpt-4": 128_000,
    "gemini": 1_000_000,
}

def get_model_context_limit(model: str) -> int:
    """Return context window size for a model string. Default: 128_000."""
    model_lower = model.lower()
    for key, limit in _MODEL_CONTEXT_WINDOWS.items():
        if key in model_lower:
            return limit
    return 128_000
```

The Runner passes this to `assemble_context()`:

```python
# In runner._execute_v1():
role_cfg = self.config.roles.get("builder")
model_limit = get_model_context_limit(role_cfg.model) if role_cfg else 128_000
```

---

### G13. Hardening failure handling

**Gap:** What if the reviewer provider crashes mid-hardening?

**Resolution:** Hardening failures are **recoverable** -- they don't kill the whole run. The `harden_plan()` function catches provider errors and returns a partial result:

```python
async def harden_plan(...) -> HardeningResult:
    current_plan = plan_content
    total_issues = 0

    for round_num in range(1, config.max_rounds + 1):
        try:
            review_output, issues, cosmetic = await _run_review_round(
                current_plan, round_num, reviewer_chain, review_prompt
            )
        except Exception as exc:
            log.error(f"hardening round {round_num} failed: {exc}")
            # Return what we have -- partially hardened is better than nothing
            return HardeningResult(
                hardened_plan=current_plan,
                rounds_completed=round_num - 1,
                total_issues_found=total_issues,
                converged=False,
            )
        # ... continue normal flow ...
```

The runner logs a warning but proceeds to execution with whatever plan state exists:

```python
# In runner._phase_hardening():
result = await harden_plan(plan_content, ...)
if not result.converged:
    log.warning(
        f"hardening did not converge -- proceeding after {result.rounds_completed} rounds"
    )
    await self.notifier.notify(
        f"hardening incomplete ({result.rounds_completed} rounds) -- proceeding",
        NotifyLevel.PROGRESS,
    )
return result.hardened_plan
```

---

### G14. `_consume_plans()` specification

**Gap:** No signature or behavior defined for the plan consumer coroutine.

**Resolution:**

```python
async def _consume_plans(self) -> None:
    """Consume plans from the queue and start runners.

    Runs as a background coroutine alongside _poll_loop().
    Plans are processed sequentially -- one at a time.
    This prevents two runners from fighting over the same project lock.

    On error (e.g., FileExistsError from init_project), sends a Telegram
    notification and continues to the next plan.
    """
    while True:
        project_name, plan_content = await self._plan_queue.get()
        try:
            project_path = init_project(project_name, plan_content, self.config)
            await self.notifier.notify(
                f"project '{project_name}' created -- starting runner",
                NotifyLevel.PROGRESS,
            )
            # Launch runner as a subprocess to avoid blocking the bot
            await self._launch_runner(project_path)
        except FileExistsError:
            # Project name collision -- notify and skip
            await self.notifier.notify(
                f"project '{project_name}' already exists -- skipping",
                NotifyLevel.ERROR,
            )
        except Exception as exc:
            log.error(f"failed to process plan '{project_name}': {exc}")
            await self.notifier.notify(
                f"failed to start '{project_name}': {exc}",
                NotifyLevel.ERROR,
            )
        finally:
            self._plan_queue.task_done()

async def _launch_runner(self, project_path: Path) -> None:
    """Launch tero2 runner as a subprocess for the given project.

    Uses asyncio.create_subprocess_exec to avoid blocking.
    """
    plan_path = project_path / ".sora" / "milestones" / "M001" / "ROADMAP.md"
    proc = await asyncio.create_subprocess_exec(
        "tero2", "run", str(project_path), "--plan", str(plan_path),
    )
    log.info(f"launched runner (PID {proc.pid}) for {project_path.name}")
    # Fire and forget -- the runner handles its own lifecycle
```

---

### G15. Project name collision handling

**Gap:** `init_project()` raises `FileExistsError` but `telegram_input.py` doesn't handle it.

**Resolution:** Handled in G14 above -- `_consume_plans()` catches `FileExistsError` and notifies via Telegram. No code change to `init_project()` needed.

---

### G16. `_build_role_chain()` vs `_build_chain()` migration

> **⚠️ NOT YET IMPLEMENTED** — `runner.py` still uses `_build_chain(start_index)`. The rename to `_build_role_chain(role_name)` has not been applied.

**Gap:** MVP0 has `_build_chain()` hardcoded to "executor". MVP1 adds `_build_role_chain(role_name)`.

**Resolution:** `_build_role_chain()` replaces `_build_chain()`. The old method is removed.

```python
def _build_role_chain(self, role_name: str) -> ProviderChain:
    """Build a ProviderChain for a specific role from config.

    Falls back to "executor" role if the named role is not configured (MVP0 compat).
    """
    role = self.config.roles.get(role_name)
    if role is None:
        role = self.config.roles.get("executor")
    if role is None:
        from tero2.errors import ConfigError
        raise ConfigError(f"no '{role_name}' or 'executor' role configured")

    all_names = [role.provider] + role.fallback
    providers = []
    for i, name in enumerate(all_names):
        override = role.model if i == 0 else ""
        providers.append(
            create_provider(
                name, self.config,
                model_override=override,
                working_dir=str(self.project_path),
            )
        )
    return ProviderChain(providers, cb_registry=self.cb_registry)
```

The old `_build_chain()` is deleted. MVP0 behavior is preserved because `_build_role_chain("builder")` falls back to the "executor" role.

---

### G17. CLI `telegram` command

> **⚠️ NOT YET IMPLEMENTED** — `cli.py` does not have a `telegram` subcommand. `telegram_input.py` is not yet implemented.

**Gap:** No CLI subcommand to start the Telegram input bot.

**Resolution:** Add to `tero2/cli.py`:

```python
def cmd_telegram(args) -> None:
    """Start the Telegram input bot (long-polling)."""
    config = load_config(Path(args.project or "."))
    if not config.telegram.bot_token:
        print("error: telegram.bot_token not configured")
        sys.exit(1)
    if not config.telegram.allowed_chat_ids:
        print("warning: telegram.allowed_chat_ids is empty -- bot will ignore all messages")

    from tero2.telegram_input import TelegramInputBot
    bot = TelegramInputBot(config)
    asyncio.run(bot.start())
```

Argparse registration:

```python
sub_telegram = subparsers.add_parser("telegram", help="Start Telegram input bot")
sub_telegram.add_argument("--project", help="Project path for config loading", default=None)
sub_telegram.set_defaults(func=cmd_telegram)
```

---

### G18. Telegram async + sync requests

**Gap:** `telegram_input.py` uses synchronous `requests` but runs in an async context.

**Resolution:** All HTTP calls in `telegram_input.py` MUST use `asyncio.to_thread()` to avoid blocking the event loop. This is the same pattern used in `notifier.py` (Bug 10 fix).

```python
async def _poll_once(self, offset: int) -> tuple[list[dict], int]:
    """One getUpdates call. Returns (updates, new_offset)."""
    resp = await asyncio.to_thread(
        requests.post,
        f"https://api.telegram.org/bot{self.config.telegram.bot_token}/getUpdates",
        json={"offset": offset, "timeout": 30},
        timeout=35,  # slightly longer than long-poll timeout
    )
    data = resp.json()
    updates = data.get("result", [])
    if updates:
        offset = updates[-1]["update_id"] + 1
    return updates, offset
```

---

### G19. DiskLayer helpers for MVP1 paths

> **⚠️ NOT YET IMPLEMENTED** — `write_plan_version()`, `write_task_summary()`, `write_hardened_plan()` are not in `disk_layer.py`.

**Gap:** No path construction helpers for versioned plans and task summaries.

**Resolution:** Add convenience methods to `DiskLayer`. Thin wrappers over `write_file()`:

```python
# Add to tero2/disk_layer.py:

def write_plan_version(self, milestone: str, version: int, content: str) -> None:
    """Write a versioned plan: milestones/{milestone}/plan_v{version}.md"""
    self.write_file(f"milestones/{milestone}/plan_v{version}.md", content)

def write_task_summary(self, milestone: str, task_id: str, content: str) -> None:
    """Write task summary: milestones/{milestone}/{task_id}-SUMMARY.md"""
    self.write_file(f"milestones/{milestone}/{task_id}-SUMMARY.md", content)

def write_hardened_plan(self, milestone: str, content: str) -> None:
    """Write the final hardened plan: milestones/{milestone}/PLAN.md"""
    self.write_file(f"milestones/{milestone}/PLAN.md", content)
```

---

### G20. Bundled prompt file location

> **⚠️ DEFERRED — Not in MVP1.** Bundled role prompt files (`builder.md`, `verifier.md`, etc.) and `copy_default_prompts()` are deferred with the persona/prompt and role-decomposition systems. Do NOT create a `prompts/` directory or any bundled prompt `.md` files in MVP1.

---

### G21. Config parsing for new sections

> **⚠️ DEFERRED (partial) — Not in MVP1.** `[plan_hardening]` and `[context]` config sections are deferred with plan hardening and context assembly. Only `[reflexion]` (max_cycles) and `allowed_chat_ids` are MVP1 scope — see the `config.py` spec in Section 5 for the precise `_parse_config()` additions.

---

### G22. MVP0 bugs status

**Gap:** Analysis flagged open MVP0 bugs (9-12) as blocking MVP1.

**Resolution:** All 12 bugs are **FIXED** (see `bugs.md`). Bug 10 (sync requests) was fixed with `asyncio.to_thread()` in `notifier.py`. Bug 12 (stderr deadlock) was fixed in `cli.py`. **No action needed.**

---

### G23. `_execute_v1` vs `_execute_plan` coexistence

**Gap:** How does the runner decide between MVP0 `_execute_plan()` and MVP1 `_execute_v1()`?

**Resolution:** MVP1 replaces `_execute_plan()`. The runner's `run()` method calls `_execute_v1()` instead. MVP0 behavior is fully preserved because MVP1 uses the same single-executor + reflexion loop — it just adds the Telegram interrupt check and improved error handling around it.

This means MVP0 configs work unchanged with the MVP1 runner.

---

### G24. `_execute_v1` pseudocode with error handling (complete)

Replaces the pseudocode in Section 5.9 with full error handling.

**MVP1 scope:** single executor + reflexion loop. No persona loading, no plan hardening, no task splitting — those are deferred.

```python
async def _execute_v1(self, state: AgentState) -> None:
    # NOTE: no persona_registry.load_all() — persona system is deferred
    plan = self.disk.read_plan(state.plan_file)
    if not plan or not plan.strip():
        state = self.checkpoint.mark_failed(state, "plan file is empty or missing")
        self._current_state = state
        await self.notifier.notify("failed -- empty plan", NotifyLevel.ERROR)
        return

    # Transition to RUNNING (Phase.RUNNING is the only execution phase in MVP1)
    state = self.checkpoint.mark_running(state)
    self._current_state = state
    await self.notifier.notify("starting execution", NotifyLevel.PROGRESS)

    reflexion = ReflexionContext(attempts=[])

    for attempt in range(self.config.reflexion.max_cycles + 1):
        # Check for OVERRIDE.md (PAUSE/STOP) between attempts
        override = self.disk.read_override()
        if override:
            await self._handle_override(override, state)
            return

        try:
            result = await self.executor.run(
                plan=plan,
                reflexion_context=reflexion.to_prompt(),
                working_dir=str(self.project_path),
                config=self.config,
            )
        except Exception as exc:
            state = self.checkpoint.mark_failed(state, f"executor crashed: {exc}")
            self._current_state = state
            await self.notifier.notify(f"executor crashed: {exc}", NotifyLevel.ERROR)
            return

        if result.success:
            state = self.checkpoint.mark_completed(state)
            self._current_state = state
            await self.notifier.notify("execution complete", NotifyLevel.DONE)
            return

        # Failure → add to reflexion context and retry
        reflexion = add_attempt(reflexion, result.output, result.error)
        state = self.checkpoint.increment_retry(state)
        self._current_state = state
        await self.notifier.notify(
            f"attempt {attempt + 1} failed, retrying", NotifyLevel.PROGRESS
        )

    # All attempts exhausted
    state = self.checkpoint.mark_failed(state, "max reflexion cycles exhausted")
    self._current_state = state
    await self.notifier.notify("failed -- max retries exhausted", NotifyLevel.ERROR)
```

---

### G25. `players/__init__.py`

> **⚠️ DEFERRED — Not in MVP1.** The `players/` package (Builder, Verifier agents) is deferred with the role-decomposition system. Do NOT create a `players/` directory or any player modules in MVP1.
