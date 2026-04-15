# MVP1+MVP2 Unified Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement MVP1 (reflexion, Telegram input, project init) and MVP2 (stuck detection, escalation) on top of the existing MVP0 baseline, producing a complete Dispatcher v2 with failure-aware retries, Telegram-driven plan submission, and 3-level escalation.

**Architecture:** MVP0 (Immortal Runner) is fully implemented. This plan adds 5 new modules — `reflexion.py`, `project_init.py`, `telegram_input.py` (MVP1), `stuck_detection.py`, `escalation.py` (MVP2) — wires them into the runner, and adds config/state/CLI updates. The runner's retry loop gains reflexion context injection (MVP1) and stuck detection + 3-level escalation (MVP2), working in tandem.

**Tech Stack:** Python 3.11+, asyncio, requests (existing dep), pytest, ruff

**Current state of codebase:**
- [x] MVP0: Fully implemented (runner, providers, state, checkpoint, disk_layer, notifier, CLI)
- [x] MVP1: **NOT implemented** — reflexion.py, telegram_input.py, project_init.py missing
- [x] MVP2: **Implemented but uncommitted** — stuck_detection.py, escalation.py, and runner.py MVP2 integration exist in the working tree (unstaged). `AgentState.tool_repeat_count` and `escalation_level` already in state.py. `StuckDetectionConfig`, `EscalationConfig`, and their Config fields + _parse_config already in config.py. Tasks 3+4 just verify correctness and commit.

**⚠️ Key invariants for executor:**
- [x] Do NOT re-add `tool_repeat_count`/`escalation_level` to `AgentState` — they're already there
- [x] Do NOT re-add `stuck_detection`/`escalation` to `Config` — they're already there
- [x] Do NOT create `test_stuck_detection.py` or `test_escalation.py` — use `tests/test_stuck_and_escalation.py`
- [x] `_run_agent()` returns `tuple[bool, str]` after Task 8 — update all callers

**Import path convention:** All specs use `src.` — implementation uses `tero2.` (see G1 in MVP1 spec).

---

## File Structure

### New Files (MVP1)
| File | Responsibility |
|------|---------------|
| `tero2/reflexion.py` | Failure context accumulation + prompt injection for retries |
| `tero2/project_init.py` | Project directory scaffolding + .sora/ init + git init |
| `tero2/telegram_input.py` | Telegram long-polling bot for receiving plans + commands |
| `tests/test_reflexion.py` | Reflexion unit tests |
| `tests/test_project_init.py` | Project init unit tests |
| `tests/test_telegram_input.py` | Telegram input unit tests |

### New Files (MVP2)
| File | Responsibility |
|------|---------------|
| `tero2/stuck_detection.py` | 3 structural stuck signals (retry, step limit, tool repeat) |
| `tero2/escalation.py` | 3-level escalation (diversification, backtrack, human) |
| `tests/test_stuck_and_escalation.py` | Stuck + escalation tests (already exists in working tree) |

### Modified Files
| File | Change |
|------|--------|
| `tero2/config.py` | Add `ReflexionConfig`, `allowed_chat_ids` to `TelegramConfig`, add `reflexion` field to `Config` (stuck_detection/escalation already exist) |
| `tero2/runner.py` | Integrate reflexion into retry loop; change `_run_agent()` to return `tuple[bool, str]` |
| `tero2/cli.py` | Add `telegram` subcommand |
| `tero2/providers/chain.py` | Add `run_prompt_collected()` method |

---

## Chunk 1: Config + Constants Foundation

### Task 1: Add all new config dataclasses

**Files:**
- [x] Modify: `tero2/config.py`
- [x] Test: `tests/test_config_mvp1.py`

> `tero2/constants.py` — no changes needed (no unused constants added).
> `tero2/state.py` — no changes needed (`tool_repeat_count` and `escalation_level` already present).

- [x] **Step 1: Write failing test for new config fields**

```python
# tests/test_config_mvp1.py
"""Tests for MVP1+MVP2 config additions."""

from tero2.config import (
    ReflexionConfig, StuckDetectionConfig, EscalationConfig,
    TelegramConfig, Config,
)


def test_reflexion_config_defaults():
    rc = ReflexionConfig()
    assert rc.max_cycles == 2


def test_stuck_detection_config_defaults():
    sd = StuckDetectionConfig()
    assert sd.max_steps_per_task == 15
    assert sd.max_retries == 3
    assert sd.tool_repeat_threshold == 2


def test_escalation_config_defaults():
    ec = EscalationConfig()
    assert ec.diversification_temp_delta == 0.3
    assert ec.diversification_max_steps == 2
    assert ec.backtrack_to_last_checkpoint is True


def test_telegram_config_allowed_chat_ids_default():
    tc = TelegramConfig()
    assert tc.allowed_chat_ids == []


def test_config_has_all_new_sections():
    cfg = Config()
    assert isinstance(cfg.reflexion, ReflexionConfig)
    assert isinstance(cfg.stuck_detection, StuckDetectionConfig)
    assert isinstance(cfg.escalation, EscalationConfig)
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_config_mvp1.py -v`
Expected: ImportError — new config classes don't exist yet

- [x] **Step 3: Add `ReflexionConfig` dataclass + `allowed_chat_ids` to `TelegramConfig` + wire into `Config`**

`StuckDetectionConfig`, `EscalationConfig`, and their fields on `Config` are already implemented. Only add what's missing:

```python
# Add to tero2/config.py, after EscalationConfig:
@dataclass
class ReflexionConfig:
    max_cycles: int = 2

# Add allowed_chat_ids to TelegramConfig:
@dataclass
class TelegramConfig:
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True
    allowed_chat_ids: list[str] = field(default_factory=list)

# Add reflexion field to Config class:
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
    reflexion: ReflexionConfig = field(default_factory=ReflexionConfig)  # ← new

# Add to _parse_config() after the escalation block:
    ref = raw.get("reflexion", {})
    if ref:
        cfg.reflexion = ReflexionConfig(
            max_cycles=ref.get("max_cycles", 2),
        )

# Also update TelegramConfig construction in _parse_config() to include allowed_chat_ids:
    if tg:
        cfg.telegram = TelegramConfig(
            bot_token=tg.get("bot_token", ""),
            chat_id=tg.get("chat_id", ""),
            heartbeat_interval_s=tg.get("heartbeat_interval_s", DEFAULT_HEARTBEAT_INTERVAL_S),
            voice_on_done=tg.get("voice_on_done", True),
            voice_on_stuck=tg.get("voice_on_stuck", True),
            allowed_chat_ids=tg.get("allowed_chat_ids", []),
        )
```

Note: `AgentState.tool_repeat_count` and `AgentState.escalation_level` already exist in `tero2/state.py` — **do not re-add them**.

- [x] **Step 4: Run test to verify it passes**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_config_mvp1.py -v`
Expected: All 5 tests PASS

- [x] **Step 5: Run full test suite for regressions**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/ -v`
Expected: All existing tests PASS

- [x] **Step 6: Commit**

```bash
git add tero2/config.py tests/test_config_mvp1.py
git commit -m "add reflexion config and allowed_chat_ids to telegram config"
```

---

### Task 2: Add `run_prompt_collected()` to ProviderChain

**Files:**
- [x] Modify: `tero2/providers/chain.py`
- [x] Test: `tests/test_chain_collected.py`

- [x] **Step 1: Write failing test**

```python
# tests/test_chain_collected.py
import asyncio
from unittest.mock import MagicMock
from tero2.providers.chain import ProviderChain


def test_run_prompt_collected_concatenates_text():
    provider = MagicMock()
    provider.display_name = "fake"

    async def fake_run(**kwargs):
        yield "Hello "
        yield "World"

    # ProviderChain.run_prompt() calls self.run(prompt=...) which calls provider.run(**kwargs)
    # Do NOT mock provider.run_prompt — ProviderChain never calls that method.
    provider.run = fake_run
    chain = ProviderChain([provider])

    result = asyncio.run(chain.run_prompt_collected("test"))
    assert "Hello" in result
    assert "World" in result
```

- [x] **Step 2: Run test to verify it fails**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_chain_collected.py -v`
Expected: AttributeError — method doesn't exist

- [x] **Step 3: Implement run_prompt_collected**

```python
# Add to ProviderChain class in tero2/providers/chain.py:
async def run_prompt_collected(self, prompt: str) -> str:
    """Send prompt and return full response as a single string."""
    parts: list[str] = []
    async for msg in self.run_prompt(prompt):
        if isinstance(msg, str):
            parts.append(msg)
        elif isinstance(msg, dict):
            content = msg.get("content", "") or msg.get("text", "")
            if content:
                parts.append(str(content))
        else:
            text = getattr(msg, "content", None) or getattr(msg, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(parts)
```

- [x] **Step 4: Run test to verify it passes**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_chain_collected.py -v`
Expected: PASS

- [x] **Step 5: Commit**

```bash
git add tero2/providers/chain.py tests/test_chain_collected.py
git commit -m "add run_prompt_collected to ProviderChain"
```

---

## Chunk 2: MVP2 Core — Stuck Detection + Escalation

### Task 3: Verify and commit `stuck_detection.py`

**Files:**
- [ ] Verify: `tero2/stuck_detection.py` (already in working tree, unstaged)
- [ ] Test: `tests/test_stuck_and_escalation.py` (already exists — comprehensive, Parts 1+4)

> `tests/test_stuck_and_escalation.py` already exists with 35+ tests covering stuck detection, escalation, and runner integration. Do NOT create a new `test_stuck_detection.py` file — use the existing one.

- [ ] **Step 1: Read `tero2/stuck_detection.py` and verify against spec**

Key interface to verify:
- [ ] `StuckSignal` enum: NONE, RETRY_EXHAUSTED, STEP_LIMIT, TOOL_REPEAT
- [ ] `StuckResult` dataclass: signal, details, severity
- [ ] `check_stuck(state, config)` → StuckResult (priority: RETRY > STEP > TOOL)
- [ ] `compute_tool_hash(tool_call)` → 16-char hex (SHA-256[:16])
- [ ] `update_tool_hash(state, tool_call)` → (state, is_repeat)

- [ ] **Step 2: Run existing tests for stuck detection (Part 1)**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_stuck_and_escalation.py -k "TestStuckDetection" -v`
Expected: All PASS

- [ ] **Step 3: Commit**

```bash
git add tero2/stuck_detection.py
git commit -m "add stuck detection module with 3 structural signals"
```

---

### Task 4: Verify and commit `escalation.py`

**Files:**
- [ ] Verify: `tero2/escalation.py` (already in working tree, unstaged)
- [ ] Test: `tests/test_stuck_and_escalation.py` (already exists — comprehensive, Parts 2+3+4)

> Do NOT create a new `test_escalation.py`. All escalation tests are in `tests/test_stuck_and_escalation.py`.

- [ ] **Step 1: Read `tero2/escalation.py` and verify against spec**

Key interface to verify:
- [ ] `EscalationLevel` enum: NONE=0, DIVERSIFICATION=1, BACKTRACK_COACH=2, HUMAN=3
- [ ] `EscalationAction` dataclass: level, inject_prompt, should_backtrack, should_pause
- [ ] `decide_escalation(stuck_result, current_level, diversification_steps, config)` → EscalationAction
- [ ] `execute_escalation(action, state, disk, notifier, checkpoint, ...)` → AgentState
- [ ] `write_stuck_report(disk, state, stuck_result, escalation_history)` → None
- [ ] Level 1: inject diversification prompt
- [ ] Level 2: reset stuck counters, write EVENT_JOURNAL, resume
- [ ] Level 3: write STUCK_REPORT.md, Telegram notify, PAUSE

- [ ] **Step 2: Run existing escalation + runner integration tests**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_stuck_and_escalation.py -v`
Expected: All PASS

- [ ] **Step 3: Also commit runner.py (already has MVP2 integration)**

```bash
git add tero2/escalation.py tero2/runner.py
git commit -m "add 3-level escalation and wire into runner"
```

---

## Chunk 3: MVP1 Core — Reflexion + Project Init

### Task 5: Create `reflexion.py`

**Files:**
- [ ] Create: `tero2/reflexion.py`
- [ ] Test: `tests/test_reflexion.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_reflexion.py
from tero2.reflexion import (
    ReflexionContext, ReflexionAttempt,
    build_reflexion_context, add_attempt,
)


def test_empty_context():
    ctx = ReflexionContext()
    assert ctx.is_empty
    assert ctx.to_prompt() == ""


def test_attempt_fields():
    a = ReflexionAttempt(
        attempt_number=1,
        builder_output="wrote auth",
        failure_reason="test failed",
        failed_tests=["test_auth"],
    )
    assert a.attempt_number == 1


def test_to_prompt_contains_failure_info():
    ctx = ReflexionContext()
    ctx = add_attempt(ctx, "wrote JWT", "token not expiring", ["test_expiry"])
    prompt = ctx.to_prompt()
    assert "Attempt 1" in prompt
    assert "FAILED" in prompt
    assert "token not expiring" in prompt


def test_truncates_long_output():
    ctx = add_attempt(
        ReflexionContext(),
        builder_output="x" * 5000,
        failure_reason="too long",
    )
    assert len(ctx.to_prompt()) < 5000


def test_add_attempt_increments():
    ctx = ReflexionContext()
    ctx = add_attempt(ctx, "try 1", "fail 1")
    ctx = add_attempt(ctx, "try 2", "fail 2")
    assert len(ctx.attempts) == 2
    assert ctx.attempts[1].attempt_number == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_reflexion.py -v`
Expected: ImportError

- [ ] **Step 3: Implement reflexion.py**

```python
# tero2/reflexion.py
"""Reflexion — failure context injection for retries.

When the executor fails, failure details are injected into the next
attempt's context so it avoids repeating the same mistakes.
"""
from __future__ import annotations
from dataclasses import dataclass, field

_MAX_OUTPUT_CHARS = 1500


@dataclass
class ReflexionAttempt:
    attempt_number: int
    builder_output: str
    failure_reason: str
    failed_tests: list[str] = field(default_factory=list)


@dataclass
class ReflexionContext:
    attempts: list[ReflexionAttempt] = field(default_factory=list)

    def to_prompt(self) -> str:
        if not self.attempts:
            return ""
        lines = ["## Previous Attempts (DO NOT repeat these mistakes)\n"]
        for a in self.attempts:
            truncated = a.builder_output[:_MAX_OUTPUT_CHARS]
            if len(a.builder_output) > _MAX_OUTPUT_CHARS:
                truncated += "... [truncated]"
            lines.append(f"### Attempt {a.attempt_number} — FAILED")
            lines.append(f"**What was tried:** {truncated}")
            lines.append(f"**What failed:** {a.failure_reason}")
            if a.failed_tests:
                lines.append(f"**Failed tests:** {', '.join(a.failed_tests)}")
            lines.append(f"**Avoid:** repeating the same approach\n")
        return "\n".join(lines)

    @property
    def is_empty(self) -> bool:
        return len(self.attempts) == 0


def build_reflexion_context(attempts: list[ReflexionAttempt]) -> ReflexionContext:
    return ReflexionContext(attempts=list(attempts))


def add_attempt(
    context: ReflexionContext,
    builder_output: str,
    failure_reason: str,
    failed_tests: list[str] | None = None,
) -> ReflexionContext:
    num = len(context.attempts) + 1
    attempt = ReflexionAttempt(
        attempt_number=num,
        builder_output=builder_output,
        failure_reason=failure_reason,
        failed_tests=failed_tests or [],
    )
    return ReflexionContext(attempts=[*context.attempts, attempt])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_reflexion.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/reflexion.py tests/test_reflexion.py
git commit -m "add reflexion module for failure context injection"
```

---

### Task 6: Create `project_init.py`

**Files:**
- [ ] Create: `tero2/project_init.py`
- [ ] Test: `tests/test_project_init.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_project_init.py
from pathlib import Path
from tero2.project_init import init_project, _sanitize_name, _extract_project_name
from tero2.config import Config
import pytest


def test_sanitize_name():
    assert _sanitize_name("My Cool Project") == "my-cool-project"
    assert _sanitize_name("  Hello World  ") == "hello-world"
    assert _sanitize_name("already-clean") == "already-clean"


def test_extract_project_name_heading():
    assert _extract_project_name("# Build Auth\nContent") == "Build Auth"


def test_extract_project_name_first_line():
    assert _extract_project_name("Build Auth\nContent") == "Build Auth"


def test_init_project_creates_structure(tmp_path):
    config = Config()
    config.projects_dir = str(tmp_path)
    path = init_project("test-project", "# Test\nPlan content", config)

    assert path.exists()
    assert (path / ".sora" / "runtime").exists()
    assert (path / ".sora" / "milestones" / "M001" / "ROADMAP.md").exists()
    assert "Plan content" in (path / ".sora" / "milestones" / "M001" / "ROADMAP.md").read_text()


def test_init_project_raises_on_existing(tmp_path):
    config = Config()
    config.projects_dir = str(tmp_path)
    init_project("dup", "# Dup\nPlan", config)
    with pytest.raises(FileExistsError):
        init_project("dup", "# Dup\nPlan", config)


def test_init_project_git_init(tmp_path):
    config = Config()
    config.projects_dir = str(tmp_path)
    path = init_project("git-test", "# Test\nPlan", config)
    assert (path / ".git").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_project_init.py -v`
Expected: ImportError

- [ ] **Step 3: Implement project_init.py**

```python
# tero2/project_init.py
"""Project initialization — create project + .sora/ + git."""
from __future__ import annotations
import re
import subprocess
from pathlib import Path
from tero2.config import Config


def init_project(project_name: str, plan_content: str, config: Config) -> Path:
    safe_name = _sanitize_name(project_name)
    project_path = Path(config.projects_dir) / safe_name
    if project_path.exists():
        raise FileExistsError(f"project directory already exists: {project_path}")

    project_path.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=project_path, capture_output=True, check=True)

    sora = project_path / ".sora"
    for subdir in ["runtime", "strategic", "persistent", "human", "reports"]:
        (sora / subdir).mkdir(parents=True, exist_ok=True)

    milestone = sora / "milestones" / "M001"
    milestone.mkdir(parents=True, exist_ok=True)
    (milestone / "ROADMAP.md").write_text(plan_content)

    return project_path


def _sanitize_name(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9\s-]", "", name)
    name = re.sub(r"[\s_]+", "-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-")


def _extract_project_name(plan: str) -> str:
    for line in plan.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("#"):
            return line.lstrip("#").strip()
        return line
    return "unnamed-project"
```

- [ ] **Step 4: Ensure Config has `projects_dir` field**

Check `tero2/config.py` — add `projects_dir: str = ""` to `Config` if missing.

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_project_init.py -v`
Expected: All 6 tests PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/project_init.py tests/test_project_init.py
git commit -m "add project_init module for project scaffolding"
```

---

## Chunk 4: Telegram Input Module

### Task 7: Create `telegram_input.py`

**Files:**
- [ ] Create: `tero2/telegram_input.py`
- [ ] Test: `tests/test_telegram_input.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_telegram_input.py
import asyncio
from tero2.telegram_input import TelegramInputBot
from tero2.config import Config, TelegramConfig


def _make_config() -> Config:
    tc = TelegramConfig(
        bot_token="test-token",
        chat_id="614473938",
        allowed_chat_ids=["614473938"],
    )
    cfg = Config(telegram=tc)
    cfg.projects_dir = "/tmp/tero2-test"
    return cfg


def test_is_allowed_valid():
    bot = TelegramInputBot(_make_config())
    assert bot._is_allowed("614473938")


def test_is_allowed_invalid():
    bot = TelegramInputBot(_make_config())
    assert not bot._is_allowed("999999999")


def test_is_allowed_empty_rejects_all():
    cfg = _make_config()
    cfg.telegram.allowed_chat_ids = []
    bot = TelegramInputBot(cfg)
    assert not bot._is_allowed("614473938")


def test_handle_message_ignores_disallowed():
    bot = TelegramInputBot(_make_config())
    update = {"message": {"chat": {"id": 999}, "text": "# Plan\nDo stuff"}}
    asyncio.run(bot._handle_message(update))
    assert bot._plan_queue.empty()


def test_handle_message_enqueues_plan():
    bot = TelegramInputBot(_make_config())
    update = {"message": {"chat": {"id": 614473938}, "text": "# Build Auth\nJWT auth"}}
    asyncio.run(bot._handle_message(update))
    assert not bot._plan_queue.empty()
    name, content = asyncio.run(bot._plan_queue.get())
    assert "auth" in name.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_telegram_input.py -v`
Expected: ImportError

- [ ] **Step 3: Implement telegram_input.py**

```python
# tero2/telegram_input.py
"""Telegram long-polling bot for receiving plans + commands."""
from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path

import requests

from tero2.config import Config
from tero2.project_init import _extract_project_name, init_project

log = logging.getLogger(__name__)

_TELEGRAM_BASE = "https://api.telegram.org/bot{token}/{method}"
_POLL_TIMEOUT = 30


class TelegramInputBot:
    def __init__(self, config: Config) -> None:
        self.config = config
        self._plan_queue: asyncio.Queue[tuple[str, str]] = asyncio.Queue()
        self._offset: int = 0

    def _is_allowed(self, chat_id: str | int) -> bool:
        allowed = self.config.telegram.allowed_chat_ids
        if not allowed:
            return False
        return str(chat_id) in [str(a) for a in allowed]

    async def _handle_message(self, update: dict) -> None:
        message = update.get("message", {})
        chat_id = str(message.get("chat", {}).get("id", ""))
        text = message.get("text", "")
        if not text or not self._is_allowed(chat_id):
            return
        name = _extract_project_name(text)
        await self._plan_queue.put((name, text))
        log.info(f"plan enqueued: {name!r} from chat {chat_id}")

    async def _poll(self) -> list[dict]:
        url = _TELEGRAM_BASE.format(token=self.config.telegram.bot_token, method="getUpdates")
        params = {"offset": self._offset, "timeout": _POLL_TIMEOUT}
        try:
            resp = await asyncio.to_thread(
                requests.get, url, params=params, timeout=_POLL_TIMEOUT + 5
            )
            data = resp.json()
            updates = data.get("result", [])
            if updates:
                self._offset = updates[-1]["update_id"] + 1
            return updates
        except Exception as exc:
            log.warning(f"poll error: {exc}")
            return []

    async def _consume_plans(self) -> None:
        while True:
            name, content = await self._plan_queue.get()
            try:
                path = init_project(name, content, self.config)
                plan_file = path / ".sora" / "milestones" / "M001" / "ROADMAP.md"
                log.info(f"starting runner for {path}")
                subprocess.Popen(
                    ["tero2", "run", str(path), "--plan", str(plan_file)],
                    start_new_session=True,
                )
            except FileExistsError:
                log.warning(f"project {name!r} already exists — skipping")
            except Exception as exc:
                log.error(f"failed to launch project {name!r}: {exc}")
            finally:
                self._plan_queue.task_done()

    async def start(self) -> None:
        log.info("Telegram input bot started")
        consumer = asyncio.create_task(self._consume_plans())
        try:
            while True:
                updates = await self._poll()
                for update in updates:
                    await self._handle_message(update)
        finally:
            consumer.cancel()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_telegram_input.py -v`
Expected: All 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/telegram_input.py tests/test_telegram_input.py
git commit -m "add telegram_input module for receiving plans via Telegram"
```

---

## Chunk 5: Runner Integration + CLI

### Task 8: Integrate reflexion + stuck detection into runner.py

**Files:**
- [ ] Modify: `tero2/runner.py`
- [ ] Test: `tests/test_runner_reflexion.py`

This is the key integration. The runner's `_execute_plan()` method gets:
- [ ] **Reflexion** (MVP1): on failure, accumulate context, inject into next retry
- [ ] **Stuck detection** (MVP2): check signals after each attempt + mid-step tool repeat
- [ ] **Escalation** (MVP2): 3-level response to stuck signals

- [ ] **Step 1: Write failing test for runner reflexion injection**

```python
# tests/test_runner_reflexion.py
"""Tests for reflexion integration in runner retry loop."""
from pathlib import Path
from unittest.mock import patch

import pytest

from tero2.config import Config, RoleConfig, StuckDetectionConfig, TelegramConfig
from tero2.disk_layer import DiskLayer
from tero2.errors import RateLimitError
from tero2.notifier import NotifyLevel
from tero2.runner import Runner


class _CapturingChain:
    """Captures prompts sent to it, always fails."""
    current_provider_index = 0

    def __init__(self) -> None:
        self.plans: list[str] = []

    async def run_prompt(self, prompt: str):
        self.plans.append(prompt)
        raise RateLimitError("always fails")
        yield  # type: ignore[misc]


def _make_config(max_retries: int = 3) -> Config:
    cfg = Config()
    cfg.retry.max_retries = max_retries
    cfg.retry.chain_retry_wait_s = 0
    cfg.stuck_detection = StuckDetectionConfig(max_retries=999, max_steps_per_task=999)
    cfg.telegram = TelegramConfig()
    cfg.roles["executor"] = RoleConfig(provider="fake", timeout_s=5)
    return cfg


async def _noop_notify(text: str, level=NotifyLevel.PROGRESS) -> bool:
    return True


@pytest.mark.asyncio
async def test_reflexion_prompt_injected_on_second_attempt(tmp_path: Path) -> None:
    """After first failure, second attempt plan must include reflexion context."""
    project = tmp_path / "project"
    project.mkdir()
    DiskLayer(project).init()
    plan = project / "plan.md"
    plan.write_text("# Build auth\nImplement JWT.")

    capturing = _CapturingChain()
    runner = Runner(project, plan, config=_make_config(max_retries=2))
    runner.notifier.notify = _noop_notify  # type: ignore[method-assign]

    with patch.object(runner, "_build_chain", return_value=capturing):
        await runner.run()

    assert len(capturing.plans) >= 2
    second_prompt = capturing.plans[1]
    assert "Previous Attempts" in second_prompt or "FAILED" in second_prompt, (
        f"reflexion not injected in second attempt. Got: {second_prompt[:200]}"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_runner_reflexion.py -v`
Expected: FAIL — reflexion is not yet injected in runner

- [ ] **Step 3: Modify `_run_agent()` to return captured output**

**Critical:** `_run_agent()` currently returns `bool`. Reflexion needs `builder_output: str` from the agent's run. Change the signature to return `tuple[bool, str]`.

```python
# In tero2/runner.py, change _run_agent signature and body:
async def _run_agent(
    self,
    chain: ProviderChain,
    plan_content: str,
    state: AgentState,
) -> tuple[bool, str]:   # ← was: bool
    base_provider_index = state.provider_index
    state_ref = [state]
    output_parts: list[str] = []    # ← new: collect agent output
    heartbeat_task = asyncio.create_task(self._heartbeat_loop(state_ref))
    try:
        timeout = self.config.roles.get("executor", None)
        timeout_s = timeout.timeout_s if timeout else HARD_TIMEOUT_S
        async with asyncio.timeout(timeout_s):
            async for message in chain.run_prompt(plan_content):
                # Collect text output for reflexion
                if isinstance(message, str):
                    output_parts.append(message)
                elif isinstance(message, dict):
                    content = message.get("content") or message.get("text") or ""
                    if content:
                        output_parts.append(str(content))
                else:
                    text = getattr(message, "content", None) or getattr(message, "text", None)
                    if text:
                        output_parts.append(str(text))

                msg_type = getattr(message, "type", None) or (
                    message.get("type") if isinstance(message, dict) else None
                )
                if msg_type in ("tool_result", "turn_end"):
                    state.provider_index = base_provider_index + chain.current_provider_index
                    if msg_type == "tool_result":
                        state, _ = update_tool_hash(state, str(message))
                    state = self.checkpoint.increment_step(state)
                    state_ref[0] = state
                    self._current_state = state
                    if msg_type == "tool_result":
                        if state.steps_in_task >= self.checkpoint.max_steps_per_task:
                            log.warning("STEP_LIMIT reached — aborting attempt")
                            return False, "\n".join(output_parts)
                        mid_stuck = check_stuck(state, self.config.stuck_detection)
                        if mid_stuck.signal == StuckSignal.TOOL_REPEAT:
                            log.warning("TOOL_REPEAT detected — aborting attempt")
                            return False, "\n".join(output_parts)
        return True, "\n".join(output_parts)
    except TimeoutError:
        log.error("hard timeout reached")
        return False, "\n".join(output_parts)
    except RateLimitError:
        log.error("all providers exhausted")
        return False, "\n".join(output_parts)
    except Exception as exc:
        from tero2.providers.chain import _is_recoverable_error
        if not _is_recoverable_error(exc):
            raise
        log.error(f"agent error: {exc}")
        return False, "\n".join(output_parts)
    finally:
        heartbeat_task.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat_task
```

- [ ] **Step 4: Integrate reflexion into `_execute_plan()`**

```python
# In tero2/runner.py, add to imports:
from tero2.reflexion import ReflexionContext, add_attempt

# In _execute_plan(), before the retry loop:
reflexion = ReflexionContext()

# Change the _run_agent call:
success, agent_output = await self._run_agent(chain, effective_plan, state)

# After failure (before increment_retry), add:
if not success:
    reflexion = add_attempt(
        reflexion,
        builder_output=agent_output,
        failure_reason=f"attempt {attempt + 1} failed",
    )

# Before running executor, prepend reflexion context to plan:
#   (This goes AFTER the escalation inject_prompt logic, BEFORE building effective_plan from steer)
reflexion_prompt = reflexion.to_prompt()
if reflexion_prompt:
    effective_plan = f"{reflexion_prompt}\n\n---\n\n{effective_plan}"
```

Full ordering of effective_plan assembly (put this in place of existing effective_plan code):
```python
effective_plan = plan_content
reflexion_prompt = reflexion.to_prompt()
if reflexion_prompt:
    effective_plan = f"{reflexion_prompt}\n\n---\n\n{effective_plan}"
if inject_prompt:
    effective_plan = f"## Notice\n{inject_prompt}\n\n---\n\n{effective_plan}"
steer = await self._check_steer()
if steer:
    log.info("STEER.md present — prepending to plan")
    effective_plan = f"## Steering\n{steer}\n\n---\n\n{effective_plan}"
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_runner_reflexion.py -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/ -v`
Expected: All PASS

- [ ] **Step 7: Commit**

```bash
git add tero2/runner.py tero2/reflexion.py tests/test_runner_reflexion.py
git commit -m "integrate reflexion into runner retry loop"
```

---

### Task 9: Add `telegram` CLI subcommand

**Files:**
- [ ] Modify: `tero2/cli.py`

- [ ] **Step 1: Read current cli.py structure**

- [ ] **Step 2: Add telegram subcommand**

```python
def cmd_telegram(args) -> None:
    """Start the Telegram input bot (long-polling)."""
    config = load_config(Path(args.project or "."))
    if not config.telegram.bot_token:
        print("error: telegram.bot_token not configured")
        sys.exit(1)
    if not config.telegram.allowed_chat_ids:
        print("warning: telegram.allowed_chat_ids is empty")

    from tero2.telegram_input import TelegramInputBot
    bot = TelegramInputBot(config)
    asyncio.run(bot.start())
```

Register in argparse:
```python
sub_telegram = subparsers.add_parser("telegram", help="Start Telegram input bot")
sub_telegram.add_argument("--project", help="Project path", default=None)
sub_telegram.set_defaults(func=cmd_telegram)
```

- [ ] **Step 3: Lint check**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && ruff check tero2/cli.py`

- [ ] **Step 4: Commit**

```bash
git add tero2/cli.py
git commit -m "add telegram CLI subcommand"
```

---

## Chunk 6: Integration Tests + Final Verification

### Task 10: Integration tests

**Files:**
- [ ] Create: `tests/test_integration_mvp1_mvp2.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/test_integration_mvp1_mvp2.py
"""Integration tests for MVP1+MVP2 unified implementation."""
from pathlib import Path
from tero2.config import Config, ReflexionConfig, StuckDetectionConfig, EscalationConfig
from tero2.reflexion import ReflexionContext, add_attempt
from tero2.project_init import init_project, _extract_project_name
from tero2.stuck_detection import check_stuck, StuckSignal
from tero2.escalation import decide_escalation, EscalationLevel
from tero2.state import AgentState


def test_reflexion_plus_escalation_prompt():
    ctx = add_attempt(ReflexionContext(), "tried X", "X failed", ["test_x"])
    escalation_inject = "Dead end. Try different approach."
    plan = "# Build auth"
    effective = f"## Notice\n{escalation_inject}\n\n---\n\n{ctx.to_prompt()}\n\n---\n\n{plan}"
    assert "Dead end" in effective
    assert "Attempt 1" in effective
    assert "Build auth" in effective


def test_project_init_full_workflow(tmp_path):
    plan = "# Auth System\nBuild JWT auth."
    name = _extract_project_name(plan)
    config = Config()
    config.projects_dir = str(tmp_path)
    path = init_project(name, plan, config)
    assert (path / ".sora" / "milestones" / "M001" / "ROADMAP.md").is_file()


def test_stuck_then_escalate_then_reflexion():
    """Full cycle: stuck → escalate → reflexion inject."""
    state = AgentState(retry_count=3)
    config_sd = StuckDetectionConfig(max_retries=3)
    config_esc = EscalationConfig()

    stuck = check_stuck(state, config_sd)
    assert stuck.signal == StuckSignal.RETRY_EXHAUSTED

    action = decide_escalation(stuck, EscalationLevel.NONE, 0, config_esc)
    assert action.level == EscalationLevel.DIVERSIFICATION

    ctx = add_attempt(ReflexionContext(), "failed output", "test broke")
    assert "Attempt 1" in ctx.to_prompt()


def test_config_all_sections():
    cfg = Config(
        reflexion=ReflexionConfig(max_cycles=3),
        stuck_detection=StuckDetectionConfig(max_retries=5),
        escalation=EscalationConfig(diversification_max_steps=4),
    )
    assert cfg.reflexion.max_cycles == 3
    assert cfg.stuck_detection.max_retries == 5
    assert cfg.escalation.diversification_max_steps == 4
```

- [ ] **Step 2: Run integration tests**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/test_integration_mvp1_mvp2.py -v`
Expected: All PASS

- [ ] **Step 3: Run full test suite + lint**

Run: `cd /Users/terobyte/Desktop/Projects/Active/tero2 && python -m pytest tests/ -v && ruff check tero2/`
Expected: All green

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration_mvp1_mvp2.py
git commit -m "add MVP1+MVP2 integration tests"
```

---

### Task 11: Acceptance verification

**MVP1 acceptance:**
- [ ] Reflexion: on executor failure → failure context injected into retry prompt
- [ ] After max reflexion cycles → task marked FAILED → Telegram notification
- [ ] Telegram: send text plan → project created → executor starts
- [ ] Only allowed `chat_id`s can interact with the bot
- [ ] `tero2 status` shows current phase
- [ ] `.sora/` scaffolded by `project_init`

**MVP2 acceptance:**
- [ ] Stuck detection: 3+ retries on same task → signal raised
- [ ] Stuck detection: 15+ steps → signal raised
- [ ] Stuck detection: same tool repeated 2x → deadlock signal
- [ ] Escalation Level 1: diversification prompt injected
- [ ] Escalation Level 2: backtrack to checkpoint, resume
- [ ] Escalation Level 3: STUCK_REPORT.md written, Telegram voice, PAUSE
- [ ] OVERRIDE.md with "PAUSE" → runner pauses

**Final:**
- [ ] `ruff check tero2/` clean
- [ ] `pytest tests/` green

---

## Implementation Order Summary

```
Task 1:  Config + Constants + State updates          ← foundation (no deps)
Task 2:  ProviderChain.run_prompt_collected()         ← foundation (no deps)
  │
  ├── Task 3:  stuck_detection.py                     ← MVP2 (deps: state, config)
  ├── Task 4:  escalation.py                          ← MVP2 (deps: state, disk, notifier)
  ├── Task 5:  reflexion.py                           ← MVP1 (no deps)
  └── Task 6:  project_init.py                        ← MVP1 (deps: config)
       │
       └── Task 7:  telegram_input.py                 ← MVP1 (deps: project_init, notifier)
  │
  ├── Task 8:  Runner integration                     ← deps: Tasks 3-5
  └── Task 9:  CLI telegram command                   ← deps: Task 7
       │
       ├── Task 10: Integration tests                 ← deps: all
       └── Task 11: Acceptance verification           ← deps: all
```

**Parallelizable:** Tasks 1+2, Tasks 3+4+5+6, Tasks 8+9.

**Total: 11 tasks, ~55 steps.**
