# Telegram Dispatcher Stage 1 — Reactive Dispatcher Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a conversational dispatcher layer on top of the existing Telegram bot so that when the runner escalates to Level 3 the user can reply from Telegram with free text or `/`-commands and the bot translates that into tool calls that mutate project state (retry, swap_agent, skip, status, errors, abort) — unblocking the runner via an `asyncio.Event` or falling back to the legacy `STEER.md` flow on a 30-min timeout.

**Architecture:** New `tero2/dispatcher/` package with a `Brain` abstraction (text → `ToolCall | text`), a pluggable brain implementation backed by **Moonshot Kimi K2.6** via **OpenRouter** (model id `moonshotai/kimi-k2.6.6`, 262k context, OpenAI-compatible chat/completions API with `tools` + `tool_choice`), a 6-tool registry that mutates the project (config.toml, EVENT_JOURNAL, OVERRIDE.md) behind a `DispatcherWaiter` that the runner blocks on during Level 3 escalations. Non-plan Telegram text is routed through the dispatcher state machine; plan messages keep the legacy path.

**Tech Stack:** Python 3.12, asyncio, `httpx` (OpenRouter calls), existing `tero2.telegram_input` bot, existing `tero2.runner`, existing `tero2.escalation`, `pytest` + `pytest-asyncio`.

**Spec reference:** `docs/superpowers/specs/2026-04-20-telegram-dispatcher-design.md` §1.1–§1.11. This plan overrides the spec on **two points**: the Stage 1 brain is **Kimi K2.6 via OpenRouter** (`moonshotai/kimi-k2.6.6`), not Gemini; `google-generativeai` is NOT added as a dependency.

---

## File Structure

```
tero2/dispatcher/                       # NEW package
├── __init__.py
├── brain.py                            # Brain ABC + BrainReply + ToolCall + ToolContext + ToolResult
├── brains/
│   ├── __init__.py                     # make_brain(cfg) factory
│   ├── kimi.py                         # KimiBrain — OpenRouter, OpenAI-compatible tool calling
│   ├── openrouter.py                   # Stage 2 stub (NotImplementedError)
│   └── claude_code.py                  # Stage 2 stub
├── tools.py                            # 6 tools + dispatch table + MODEL_ALLOWLIST
├── rate_limit.py                       # TokenBucket
├── state_machine.py                    # ChatState, ChatMode enum, PendingEscalation, history mgmt
├── commands.py                         # parse_command() — /retry /swap /skip /status /errors /abort
└── waiter.py                           # DispatcherWaiter + EscalationCoordinator Protocol

tero2/telegram_input.py                 # MODIFIED — non-plan text → dispatcher; voice handling in Stage 2
tero2/escalation.py                     # MODIFIED — Level 3 registers w/ coordinator, blocks on waiter
tero2/runner.py                         # MODIFIED — takes escalation_coordinator, non-blocking wait_task
tero2/config.py                         # MODIFIED — + DispatcherConfig, Config.dispatcher

tests/dispatcher/                       # NEW test package
├── __init__.py
├── test_brain_kimi.py                  # mocked httpx → tool-call routing, fallback text
├── test_commands.py                    # parse_command() across verbs + malformed
├── test_config.py                      # Config.dispatcher wired
├── test_rate_limit.py                  # TokenBucket under concurrent load
├── test_state_machine.py               # transitions, persistence, history trim
├── test_tools.py                       # each tool over tmp project fs
├── test_waiter.py                      # resolve / timeout / double-resolve / race
├── test_escalation_integration.py      # Level 3 → /retry → waiter resolves
└── test_dispatcher_swap.py             # /swap → config.toml updated before waiter resolves

pyproject.toml                          # MODIFIED — add httpx (if not present); NO google-generativeai
```

---

## Decision Deltas vs Spec

1. **Brain = Kimi K2.6 via OpenRouter** (not Gemini).
   - Model id: `moonshotai/kimi-k2.6` — 262,144 token context, OpenAI-compatible tool calling.
   - Pricing: $0.80 / 1M input, $3.50 / 1M output (as of 2026-04-21 on openrouter.ai).
   - API: `POST https://openrouter.ai/api/v1/chat/completions` (OpenAI-compatible JSON schema for `tools` + `tool_choice`).
   - Auth: `Authorization: Bearer $OPENROUTER_API_KEY`, header `HTTP-Referer: https://github.com/terobyte/tero2`, `X-Title: tero2`.
   - Client: `httpx.AsyncClient` (project already uses httpx; no new SDK dependency).
   - History translation: OpenAI style natively — no `user`↔`model` rewrite needed (the Gemini-specific bug #6 disappears).

2. **`google-generativeai` is NOT added.** Anywhere the spec imports it in §1.4, replace with `httpx` + the `KimiBrain` body below.

3. **Voice is Stage 2, not Stage 1** — unchanged from spec.

---

## Prereq Checks (run once before starting)

- [ ] `uv run pytest -q` passes on the current tree (baseline green).
- [ ] `OPENROUTER_API_KEY` env var is set in the shell where tero2 runs (`echo ${OPENROUTER_API_KEY:+set}` prints `set`).
- [ ] Working dir is tero2 root, branch is clean or on a feature branch.

---

## Task 1 — Config skeleton

**Files:**
- Modify: `tero2/config.py`
- Create: `tests/dispatcher/__init__.py` (empty)
- Create: `tests/dispatcher/test_config.py`

- [ ] **Step 1: Write failing test.**

```python
# tests/dispatcher/test_config.py
from tero2.config import Config, DispatcherConfig

def test_dispatcher_defaults():
    cfg = Config()
    assert isinstance(cfg.dispatcher, DispatcherConfig)
    assert cfg.dispatcher.enabled is False
    assert cfg.dispatcher.brain_provider == "kimi"
    assert cfg.dispatcher.brain_model == "moonshotai/kimi-k2.6"
    assert cfg.dispatcher.api_key_env == "OPENROUTER_API_KEY"
    assert cfg.dispatcher.escalation_timeout_s == 1800
    assert cfg.dispatcher.rate_limit_per_min == 20
    assert cfg.dispatcher.history_max == 40
```

- [ ] **Step 2: Run and verify FAIL** (`AttributeError: DispatcherConfig`).

  Run: `uv run pytest tests/dispatcher/test_config.py -v`

- [ ] **Step 3: Add `DispatcherConfig` and wire into `Config`.**

```python
# tero2/config.py — add near the other subconfigs
@dataclass
class DispatcherConfig:
    enabled: bool = False
    brain_provider: str = "kimi"              # "kimi" | "openrouter" | "llama_cpp"
    brain_model: str = "moonshotai/kimi-k2.6"
    api_key_env: str = "OPENROUTER_API_KEY"
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    escalation_timeout_s: int = 1800          # 30 min
    rate_limit_per_min: int = 20
    history_max: int = 40
    # Stage 2 fields (unused in Stage 1, declared now to keep schema stable)
    architect_enabled: bool = False
    claude_binary: str = "claude"
    output_flush_chars: int = 3000
    # Stage 3 fields (unused in Stage 1)
    gguf_path: str = ""
    mmproj_path: str = ""
    n_ctx: int = 4096
    n_threads: int = 8
    idle_unload_s: int = 3600

@dataclass
class Config:
    # …existing fields…
    dispatcher: DispatcherConfig = field(default_factory=DispatcherConfig)
```

Also extend the TOML loader so `[dispatcher]` section maps onto these fields.

- [ ] **Step 4: Run test — expect PASS.**

- [ ] **Step 5: Commit.**

```bash
git add tero2/config.py tests/dispatcher/
git commit -m "dispatcher: add DispatcherConfig scaffold"
```

---

## Task 2 — Brain abstractions

**Files:**
- Create: `tero2/dispatcher/__init__.py` (empty)
- Create: `tero2/dispatcher/brain.py`
- Create: `tero2/dispatcher/brains/__init__.py`

- [ ] **Step 1: Write `brain.py` with ABC + dataclasses.** Copy the types from spec §1.3 verbatim: `ToolCall(name, args)`, `BrainReply(text=None, tool_call=None)` with `__post_init__` guarding that at least one of `text`/`tool_call` is set, `ToolContext`, `ToolResult`, and `Brain` ABC with `async def interpret(user_text, tools, context, history) -> BrainReply` plus stubbed `async def transcribe(audio_path) -> str` that raises `NotImplementedError` in Stage 1.

- [ ] **Step 2: Write the factory scaffold** in `tero2/dispatcher/brains/__init__.py`:

```python
import os
from tero2.config import DispatcherConfig
from tero2.dispatcher.brain import Brain
from tero2.errors import ConfigError

def make_brain(cfg: DispatcherConfig) -> Brain:
    if cfg.brain_provider == "kimi":
        from tero2.dispatcher.brains.kimi import KimiBrain
        key = os.environ.get(cfg.api_key_env)
        if not key:
            raise ConfigError(f"env var {cfg.api_key_env} not set")
        return KimiBrain(api_key=key, model=cfg.brain_model, base_url=cfg.openrouter_base_url)
    if cfg.brain_provider == "openrouter":
        from tero2.dispatcher.brains.openrouter import OpenRouterBrain  # Stage 2
        raise ConfigError("openrouter brain ships in Stage 2")
    if cfg.brain_provider == "llama_cpp":
        raise ConfigError("llama_cpp brain ships in Stage 3")
    raise ConfigError(f"unknown brain_provider: {cfg.brain_provider}")
```

- [ ] **Step 3:** create empty stub files `tero2/dispatcher/brains/openrouter.py` and `claude_code.py` with a single `raise NotImplementedError("Stage 2")` module-level — keeps factory import paths real.

- [ ] **Step 4:** `uv run python -c "from tero2.dispatcher.brain import Brain, BrainReply, ToolCall; print('ok')"` → prints `ok`.

- [ ] **Step 5: Commit.**

```bash
git add tero2/dispatcher/
git commit -m "dispatcher: brain abc and factory scaffold"
```

---

## Task 3 — KimiBrain (OpenRouter)

**Files:**
- Create: `tero2/dispatcher/brains/kimi.py`
- Create: `tests/dispatcher/test_brain_kimi.py`

- [ ] **Step 1: Write test with mocked httpx** covering three paths:
  1. Response with `tool_calls` array → `BrainReply(tool_call=ToolCall(...))`.
  2. Response with text-only message → `BrainReply(text=...)`.
  3. Response with empty `content` AND no tool_call → `BrainReply(text="⚠️ Brain returned empty reply...")` (fallback).

```python
# tests/dispatcher/test_brain_kimi.py
import json
import pytest
import respx
import httpx
from tero2.dispatcher.brains.kimi import KimiBrain

TOOLS = [{"type":"function","function":{"name":"retry_phase","description":"x","parameters":{"type":"object","properties":{}}}}]

@respx.mock
@pytest.mark.asyncio
async def test_kimi_tool_call():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices":[{"message":{"role":"assistant","content":None,"tool_calls":[
                {"id":"1","type":"function","function":{"name":"retry_phase","arguments":"{}"}}
            ]}}]
        })
    )
    b = KimiBrain(api_key="k", model="moonshotai/kimi-k2.6")
    r = await b.interpret("retry please", TOOLS, {}, [])
    assert r.tool_call.name == "retry_phase"

@respx.mock
@pytest.mark.asyncio
async def test_kimi_text():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices":[{"message":{"role":"assistant","content":"which role?","tool_calls":None}}]
        })
    )
    b = KimiBrain(api_key="k", model="moonshotai/kimi-k2.6")
    r = await b.interpret("swap", TOOLS, {}, [])
    assert r.text == "which role?"
    assert r.tool_call is None

@respx.mock
@pytest.mark.asyncio
async def test_kimi_empty_fallback():
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(
        return_value=httpx.Response(200, json={
            "choices":[{"message":{"role":"assistant","content":"","tool_calls":None}}]
        })
    )
    b = KimiBrain(api_key="k", model="moonshotai/kimi-k2.6")
    r = await b.interpret("?", TOOLS, {}, [])
    assert r.text and "Brain returned empty" in r.text
```

(add `respx` to dev deps if absent; `pytest-asyncio` is already in use for async tests in tero2.)

- [ ] **Step 2: Run — FAIL** (module doesn't exist).

- [ ] **Step 3: Implement `KimiBrain`.**

```python
# tero2/dispatcher/brains/kimi.py
import json, logging
from pathlib import Path
from typing import Any
import httpx
from tero2.dispatcher.brain import Brain, BrainReply, ToolCall

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are the tero2 dispatcher. The user operates an autonomous coding agent "
    "that has escalated for human help.\n"
    "Translate user intent into ONE tool call when confident. If ambiguous, "
    "ask for clarification in plain text — do NOT guess.\n"
    "Max 2 sentences per text reply."
)

_EMPTY_FALLBACK = (
    "⚠️ Brain returned empty reply. "
    "Use: /retry /swap /skip /status /errors /abort"
)

class KimiBrain(Brain):
    def __init__(
        self,
        api_key: str,
        model: str = "moonshotai/kimi-k2.6",
        base_url: str = "https://openrouter.ai/api/v1",
        timeout: float = 15.0,
    ):
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    async def interpret(self, user_text, tools, context, history):
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT
                + "\n\nContext:\n" + json.dumps(context, indent=2)},
            *history[-20:],
            {"role": "user", "content": f"<|user_input>{user_text}<|/user_input>"},
        ]
        payload = {
            "model": self._model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
            "max_tokens": 512,
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "HTTP-Referer": "https://github.com/terobyte/tero2",
            "X-Title": "tero2",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload, headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPError as exc:
            log.warning("Kimi API error: %s", exc)
            return BrainReply(text=_EMPTY_FALLBACK)

        msg = (data.get("choices") or [{}])[0].get("message", {}) or {}
        tool_calls = msg.get("tool_calls") or []
        if tool_calls:
            tc = tool_calls[0]
            name = tc.get("function", {}).get("name", "")
            raw_args = tc.get("function", {}).get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                log.warning("Kimi tool_call args not JSON: %r", raw_args)
                return BrainReply(text=_EMPTY_FALLBACK)
            if not name:
                return BrainReply(text=_EMPTY_FALLBACK)
            return BrainReply(tool_call=ToolCall(name=name, args=args))

        text = (msg.get("content") or "").strip()
        if not text:
            return BrainReply(text=_EMPTY_FALLBACK)
        return BrainReply(text=text)

    async def transcribe(self, audio_path: Path) -> str:
        # Stage 2 ships voice — Deepgram client lives elsewhere.
        raise NotImplementedError("transcribe lives in Stage 2 (Deepgram)")
```

- [ ] **Step 4:** `uv run pytest tests/dispatcher/test_brain_kimi.py -v` → PASS.

- [ ] **Step 5: Commit.**

```bash
git add tero2/dispatcher/brains/kimi.py tests/dispatcher/test_brain_kimi.py pyproject.toml
git commit -m "dispatcher: kimi k2 brain over openrouter"
```

---

## Task 4 — Commands parser

**Files:**
- Create: `tero2/dispatcher/commands.py`
- Create: `tests/dispatcher/test_commands.py`

- [ ] **Step 1: Write tests** covering each verb (`/retry`, `/swap <role> <model>`, `/skip`, `/status`, `/errors`, `/abort`), unknown verbs, and malformed args. See spec §1.8 for parser expectations — `parse_command(text)` returns `ToolCall | None`.

- [ ] **Step 2: Run — FAIL.**

- [ ] **Step 3: Implement `parse_command`** per spec §1.8.

- [ ] **Step 4: PASS.**

- [ ] **Step 5:** `git commit -m "dispatcher: fast-path / commands"`

---

## Task 5 — Rate limit

**Files:**
- Create: `tero2/dispatcher/rate_limit.py`
- Create: `tests/dispatcher/test_rate_limit.py`

- [ ] **Step 1: Test — TokenBucket(capacity=10, refill_per_s=10/60).** 20 concurrent `take()` calls → exactly 10 return True.
- [ ] **Step 2: Implement** per spec §1.5 (the small file).
- [ ] **Step 3: PASS.**
- [ ] **Step 4:** `git commit -m "dispatcher: token bucket rate limit"`

---

## Task 6 — Waiter

**Files:**
- Create: `tero2/dispatcher/waiter.py`
- Create: `tests/dispatcher/test_waiter.py`

- [ ] **Step 1: Test** resolve / timeout / double-resolve (idempotent) / race where `resolve()` and timeout fire on same tick — expect the resolution, NOT `TIMEOUT_FALLBACK`.
- [ ] **Step 2: Implement `DispatcherWaiter`** and `EscalationCoordinator` Protocol per spec §1.6.
- [ ] **Step 3: PASS.**
- [ ] **Step 4:** `git commit -m "dispatcher: escalation waiter"`

---

## Task 7 — State machine

**Files:**
- Create: `tero2/dispatcher/state_machine.py`
- Create: `tests/dispatcher/test_state_machine.py`

- [ ] **Step 1: Tests** — `ChatState.from_dict(state.to_dict())` roundtrip; mode transitions `IDLE ↔ AWAITING_ESCALATION_ANSWER ↔ ARCHITECT` (architect not exercised yet — keep the enum value); history trim to `history_max`; `PendingEscalation` queue (second register does NOT overwrite active waiter).
- [ ] **Step 2: Implement** per spec §1.2.
- [ ] **Step 3: PASS.**
- [ ] **Step 4:** `git commit -m "dispatcher: per-chat state machine"`

---

## Task 8 — Tools

**Files:**
- Create: `tero2/dispatcher/tools.py`
- Create: `tests/dispatcher/test_tools.py`

- [ ] **Step 1: Tests over a tmp project** — each of `retry_phase`, `skip_phase`, `swap_agent`, `show_status`, `show_errors`, `abort_project`:
  - Verify config.toml/EVENT_JOURNAL/OVERRIDE.md mutations.
  - Negative: `swap_agent` with invalid role → error, no mutation.
  - Negative: `swap_agent` with model outside `MODEL_ALLOWLIST` → error, no mutation.
  - Negative: `swap_agent` with extra keys → rejected.
  - Negative: `abort_project` without `confirm=true` → prompt, no OVERRIDE.md write.
  - With `ctx.waiter=None`: tools succeed with `resolves_waiter=False`.
- [ ] **Step 2: Implement** per spec §1.5 (copy verbatim; tool schemas are OpenAI-style JSON and map directly to OpenRouter's `tools` field).
- [ ] **Step 3: PASS.**
- [ ] **Step 4:** `git commit -m "dispatcher: six tools with allowlisted swap"`

---

## Task 9 — Runner & escalation refactor

**Files:**
- Modify: `tero2/runner.py`
- Modify: `tero2/escalation.py`

- [ ] **Step 1: Existing escalation tests baseline green.**

  Run: `uv run pytest tests/test_escalation.py -v`

- [ ] **Step 2: Modify `Runner.__init__`** to accept `escalation_coordinator: EscalationCoordinator | None = None` and store it; keep the legacy STEER.md path when coordinator is None.

- [ ] **Step 3: Modify `escalation.py` Level 3** to call `coordinator.register_escalation(project, stuck_summary)` and `await coordinator.wait(timeout_s)`; on timeout-fallback sentinel, drop to legacy STEER.md flow.

- [ ] **Step 4: Add `_peek_command`** polling so a concurrent `/stop` cancels the wait task within 2s (spec §1.7).

- [ ] **Step 5:** `uv run pytest tests/test_escalation.py -v` → green. Add an integration test `tests/dispatcher/test_escalation_integration.py` with a stub coordinator that resolves after ~200ms; assert runner state mutations match.

- [ ] **Step 6: Commit.**

```bash
git add tero2/runner.py tero2/escalation.py tests/dispatcher/test_escalation_integration.py
git commit -m "dispatcher: runner/escalation wired to coordinator"
```

---

## Task 10 — Telegram integration

**Files:**
- Modify: `tero2/telegram_input.py`

- [ ] **Step 1: Add `_chat_states: dict[int, ChatState]`** and `_brain = make_brain(cfg.dispatcher)` at `__init__` (only when `cfg.dispatcher.enabled`).

- [ ] **Step 2: Implement `EscalationCoordinator` protocol methods** (`register_escalation`, `wait`, `resolve`) on `TelegramInputBot`, backed by `DispatcherWaiter`.

- [ ] **Step 3: Route non-plan text** — in `_handle_update`: if `state.mode == AWAITING_ESCALATION_ANSWER`, call `_dispatch_as_answer(text)`; if `IDLE`, keep legacy routing (`plan` submission, `/status`, etc).

- [ ] **Step 4: `_dispatch_as_answer`** — first try `parse_command(text)` (fast path); otherwise `await brain.interpret(...)` with the current tools, context, history. Rate-limit first via `TokenBucket`. Persist history turn. All user-facing sends go through `notifier.notify(level=...)`.

- [ ] **Step 5: Rehydrate on restart** — load persisted `ChatState` on boot; if mode == `AWAITING_ESCALATION_ANSWER`, send a restore message; accept `/abort` to clear.

- [ ] **Step 6: Manual E2E.** Run tero2 against a disposable project, force a Level 3 escalation, reply from phone with `/retry` and with `"retry please"` — both must resolve the waiter within 2s.

- [ ] **Step 7: Commit.**

```bash
git add tero2/telegram_input.py
git commit -m "dispatcher: telegram routing + escalation coordinator"
```

---

## Task 11 — Integration tests

**Files:**
- Create: `tests/dispatcher/test_dispatcher_swap.py` (separate from the earlier integration test)

- [ ] **Step 1: Test** that simulating a Level 3 escalation + a `swap_agent` tool call results in `.sora/config.toml` being updated **before** `waiter.resolve()` returns.

- [ ] **Step 2: Test** that `show_status` called 5× during an escalation does NOT bloat history (§1.11: "history length unchanged — only user turns appended").

- [ ] **Step 3: Test** double escalation: second `register_escalation` queues, does NOT overwrite `state.waiter`.

- [ ] **Step 4: Test** runner `/stop` during a 30-min wait cancels the waiter within 2s (§1.11 — bug #1 regression test).

- [ ] **Step 5: PASS all.** `uv run pytest tests/dispatcher/ -v`

- [ ] **Step 6:** `git commit -m "dispatcher: integration tests for swap and stop"`

---

## Task 12 — Manual E2E gate & ship

- [ ] **Step 1:** Set `dispatcher.enabled = true` in the test project's `.sora/config.toml`.

- [ ] **Step 2:** Run tero2 end-to-end: trigger a real stuck phase, reply from phone via free text ("swap scout to glm-4.6"), verify bot calls `swap_agent`, config.toml updates, runner resumes with new model.

- [ ] **Step 3:** Document the manual flow in `docs/superpowers/specs/manual-tests/dispatcher-stage1.md` (new file, short checklist).

- [ ] **Step 4: Tag release.**

```bash
git tag dispatcher-stage1
git push --tags
```

---

## Acceptance (Stage 1 done when)

- [ ] `uv run pytest tests/dispatcher/ -q` green — all unit + integration tests pass, no network.
- [ ] `uv run pytest -q` green on the full suite (no regressions in runner/escalation/telegram tests).
- [ ] Manual E2E: real escalation → phone reply → tool call → runner resumes, within 30-min timeout window.
- [ ] `OPENROUTER_API_KEY` absent → `ConfigError` on startup with clear message (not a silent broken bot).
- [ ] `dispatcher.enabled = false` (default) → zero behavior change from pre-dispatcher tero2.
