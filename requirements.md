# Live Agent Stream Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the triple-buffered `LogView` with a live `StreamBus` + normalizer pipeline so the TUI shows each agent's tool calls, text, and thinking as they happen — with a 7-role heartbeat sidebar, main panel, and compact status log.

**Architecture:** New `StreamBus` fan-out dispatcher (parallel to existing `EventDispatcher`, tuned for higher volume). Five per-provider normalizers convert raw provider output (`claude`, `codex`, `opencode`, `kilo` stream-JSON plus `zai` SDK objects) into a single `StreamEvent` schema. `CLIProvider.run` is rewritten to yield line-by-line instead of buffering. `BasePlayer._run_prompt` + `RunnerContext.run_agent` publish normalized events to the bus. TUI widgets subscribe: `RoleStreamPanel` (main), `HeartbeatSidebar` (7 mini-cells), `StatusLog` (4-line phase log). Retry/failover in `ProviderChain` becomes "retry only before first yield; hard-fail after".

**Tech Stack:** Python 3.11+, asyncio, Textual ≥1.0, Rich, pytest, pytest-asyncio, pytest-textual-snapshot.

**Spec:** `docs/superpowers/specs/2026-04-20-live-agent-stream-design.md` — authoritative reference. When in doubt, consult the spec's Decisions Log and Error Handling tables.

**Baseline:** current `main` HEAD at 2026-04-20. All file paths/line numbers in this plan are valid on main. The working copy has unstaged edits; implementation happens from `main`, conflicts resolved in favor of this plan.

---

## Prerequisites

Before starting Task 1:

- [x] **Create a worktree off main** (see superpowers:using-git-worktrees):

```bash
cd /Users/terobyte/Desktop/Projects/Active/tero2
git worktree add ../tero2-live-stream main
cd ../tero2-live-stream
```

- [x] **Verify baseline tests pass:**

```bash
uv run pytest tests/ -q
```
Expected: all existing tests pass. If they don't, investigate before proceeding — do not layer stream work on top of broken tests.

- [x] **Read the spec once, top to bottom:** `docs/superpowers/specs/2026-04-20-live-agent-stream-design.md`. The Decisions Log (spec §Decisions Log) encodes 10 choices that determine why each task below exists.

---

## File Structure Summary

| File | Status | Responsibility |
|------|--------|----------------|
| `tero2/stream_bus.py` | NEW | `StreamEvent` dataclass + `StreamBus` fan-out |
| `tero2/providers/normalizers/__init__.py` | NEW | `get_normalizer(provider_kind)` dispatcher |
| `tero2/providers/normalizers/base.py` | NEW | `StreamNormalizer` Protocol |
| `tero2/providers/normalizers/fallback.py` | NEW | `FallbackNormalizer` for unknown providers |
| `tero2/providers/normalizers/claude.py` | NEW | Claude stream-JSON parser |
| `tero2/providers/normalizers/codex.py` | NEW | Codex `--json` parser |
| `tero2/providers/normalizers/opencode.py` | NEW | OpenCode `--format json` parser |
| `tero2/providers/normalizers/kilo.py` | NEW | Kilo `--format json` parser |
| `tero2/providers/normalizers/zai.py` | NEW | Anthropic-SDK streaming adapter |
| `tero2/providers/base.py` | MOD | Add `kind` attribute |
| `tero2/providers/cli.py` | MOD | Remove triple buffering; yield line-by-line |
| `tero2/providers/chain.py` | MOD | `provider_kind` property + yield-aware retry policy |
| `tero2/providers/zai.py` | MOD | Set `self.kind = "zai"` |
| `tero2/players/base.py` | MOD | `__init__(..., stream_bus=None)`; `_run_prompt` normalizes + publishes |
| `tero2/players/{architect,scout,builder,coach,verifier,reviewer}.py` | MOD | Pass `stream_bus` through `super().__init__` |
| `tero2/phases/context.py` | MOD | `RunnerContext.stream_bus` field; `run_agent` normalizes + publishes |
| `tero2/phases/architect_phase.py` | MOD | Pass `stream_bus=ctx.stream_bus` to `ArchitectPlayer` |
| `tero2/phases/scout_phase.py` | MOD | same for `ScoutPlayer` |
| `tero2/phases/coach_phase.py` | MOD | same for `CoachPlayer` |
| `tero2/phases/harden_phase.py` | MOD | same for `ReviewerPlayer` |
| `tero2/phases/execute_phase.py` | MOD | same for `BuilderPlayer` + `VerifierPlayer` |
| `tero2/runner.py` | MOD | Create `StreamBus`, put in `RunnerContext` |
| `tero2/cli.py` | MOD | Pass `runner.stream_bus` to `DashboardApp` |
| `tero2/tui/app.py` | MOD | New compose, new hotkeys, pin/auto-switch, subscribe to bus |
| `tero2/tui/widgets/stream_panel.py` | NEW | `RoleStreamPanel` main content |
| `tero2/tui/widgets/heartbeat_sidebar.py` | NEW | `HeartbeatSidebar` 7 mini-cells |
| `tero2/tui/widgets/stream_event_formatter.py` | NEW | Pure `format(event, raw_mode) -> rich.Text` |
| `tero2/tui/widgets/status_log.py` | NEW | Compact 4-line `RichLog` for orchestration events |
| `tero2/tui/widgets/log_view.py` | UNCHANGED | No longer used in `app.py`; keep file for potential reuse. Removal = separate future PR |

Test files (all NEW):
- [x] `tests/test_stream_bus.py`
- [x] `tests/test_stream_event_formatter.py`
- [x] `tests/test_heartbeat_sidebar.py`
- [x] `tests/test_stream_panel.py`
- [x] `tests/test_status_log.py`
- [x] `tests/test_cli_provider_streaming.py`
- [x] `tests/test_chain_retry_policy.py`
- [x] `tests/test_player_stream_integration.py`
- [x] `tests/test_app_stream_wiring.py`
- [x] `tests/test_runner_context_stream.py`
- [x] `tests/test_e2e_stream_flow.py`
- [x] `tests/normalizers/__init__.py`
- [x] `tests/normalizers/test_claude.py`, `test_codex.py`, `test_opencode.py`, `test_kilo.py`, `test_zai.py`
- [x] `tests/normalizers/fixtures/` (raw provider output samples)

---

## Chunk 1: StreamBus Foundation (Spec Build-Order Step 1)

Goal of this chunk: introduce `StreamEvent` and `StreamBus` in isolation. No callers wired up. Delivers a runnable module with tests; nothing else is affected.

### Task 1: Create `StreamEvent` dataclass + factory

**Files:**
- [x] Create: `tero2/stream_bus.py`
- [x] Create: `tests/test_stream_bus.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_stream_bus.py
from datetime import datetime, timezone
import pytest

from tero2.stream_bus import StreamEvent, make_stream_event

def test_stream_event_defaults():
    ev = StreamEvent(
        role="builder",
        kind="text",
        timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert ev.role == "builder"
    assert ev.kind == "text"
    assert ev.content == ""
    assert ev.tool_name == ""
    assert ev.tool_args == {}
    assert ev.tool_output == ""
    assert ev.tool_id == ""
    assert ev.raw == {}

def test_make_stream_event_uses_utc_now(monkeypatch):
    fixed = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)

    class _FakeDT:
        @staticmethod
        def now(tz=None):
            assert tz is timezone.utc
            return fixed

    monkeypatch.setattr("tero2.stream_bus.datetime", _FakeDT)
    ev = make_stream_event("scout", "text", content="hi")
    assert ev.timestamp == fixed
    assert ev.role == "scout"
    assert ev.kind == "text"
    assert ev.content == "hi"

def test_make_stream_event_accepts_tool_fields():
    ev = make_stream_event(
        "builder",
        "tool_use",
        tool_name="Read",
        tool_args={"path": "x.py"},
        tool_id="toolu_abc",
        raw={"original": "dict"},
    )
    assert ev.tool_name == "Read"
    assert ev.tool_args == {"path": "x.py"}
    assert ev.tool_id == "toolu_abc"
    assert ev.raw == {"original": "dict"}
```

- [x] **Step 2: Implement `StreamEvent` + `make_stream_event`**

```python
# tero2/stream_bus.py
"""Live agent stream bus.

Parallel to ``tero2.events.EventDispatcher`` but tuned for higher volume:
per-subscriber ring buffer of 2000 events, sync publish, no priority logic.
Consumers: TUI widgets (``RoleStreamPanel``, ``HeartbeatSidebar``).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

StreamEventKind = Literal[
    "text",
    "tool_use",
    "tool_result",
    "thinking",
    "status",
    "error",
    "turn_end",
]

@dataclass
class StreamEvent:
    """Normalized stream event produced by a per-provider normalizer.

    Produced by ``tero2.providers.normalizers.*``; published via
    :class:`StreamBus`; consumed by TUI widgets.
    """

    role: str
    kind: StreamEventKind
    timestamp: datetime
    content: str = ""
    tool_name: str = ""
    tool_args: dict = field(default_factory=dict)
    tool_output: str = ""
    tool_id: str = ""
    raw: dict = field(default_factory=dict)

def make_stream_event(
    role: str,
    kind: StreamEventKind,
    *,
    timestamp: datetime | None = None,
    content: str = "",
    tool_name: str = "",
    tool_args: dict | None = None,
    tool_output: str = "",
    tool_id: str = "",
    raw: dict | None = None,
) -> StreamEvent:
    """Factory with UTC ``now`` default."""
    return StreamEvent(
        role=role,
        kind=kind,
        timestamp=timestamp or datetime.now(timezone.utc),
        content=content,
        tool_name=tool_name,
        tool_args=tool_args or {},
        tool_output=tool_output,
        tool_id=tool_id,
        raw=raw or {},
    )
```

- [x] **Run tests + commit**

```bash
uv run pytest tests/test_stream_bus.py -v
```
Expected: 3 passed.

```bash
git add tero2/stream_bus.py tests/test_stream_bus.py
git commit -m "add StreamEvent dataclass and factory"
```

---

### Task 2: `StreamBus.subscribe` / `publish` / `unsubscribe` happy path

**Files:**
- [x] Modify: `tero2/stream_bus.py`
- [x] Modify: `tests/test_stream_bus.py`

- [x] **Step 1: Append failing tests**

```python
# tests/test_stream_bus.py — appended
import asyncio

from tero2.stream_bus import StreamBus, make_stream_event

def _run(coro):
    return asyncio.run(coro)

def test_bus_subscribe_receives_events():
    async def scenario():
        bus = StreamBus()
        q = bus.subscribe()
        ev = make_stream_event("builder", "text", content="hello")
        bus.publish(ev)
        received = await asyncio.wait_for(q.get(), timeout=0.1)
        assert received is ev

    _run(scenario())

def test_bus_multiple_subscribers_all_receive():
    async def scenario():
        bus = StreamBus()
        q1 = bus.subscribe()
        q2 = bus.subscribe()
        ev = make_stream_event("scout", "status", content="start")
        bus.publish(ev)
        a = await asyncio.wait_for(q1.get(), timeout=0.1)
        b = await asyncio.wait_for(q2.get(), timeout=0.1)
        assert a is ev and b is ev

    _run(scenario())

def test_bus_unsubscribe_stops_delivery():
    async def scenario():
        bus = StreamBus()
        q = bus.subscribe()
        bus.unsubscribe(q)
        bus.publish(make_stream_event("x", "text"))
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(q.get(), timeout=0.05)

    _run(scenario())

def test_bus_unsubscribe_unknown_queue_is_silent():
    async def scenario():
        bus = StreamBus()
        bogus = asyncio.Queue()
        # Must not raise.
        bus.unsubscribe(bogus)

    _run(scenario())
```

- [x] **Step 2: Add `StreamBus` class**

```python
# tero2/stream_bus.py — append below make_stream_event
class StreamBus:
    """Fan-out dispatcher for agent stream content.

    Sync ``publish()`` — safe to call in tight loops from an asyncio task.
    Per-subscriber queue; when full, oldest event is dropped (ring buffer).
    """

    def __init__(self, max_queue_size: int = 2000) -> None:
        self._subscribers: list[asyncio.Queue[StreamEvent]] = []
        self._max = max_queue_size
        self._loop: asyncio.AbstractEventLoop | None = None

    def subscribe(self) -> asyncio.Queue[StreamEvent]:
        q: asyncio.Queue[StreamEvent] = asyncio.Queue(maxsize=self._max)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[StreamEvent]) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, event: StreamEvent) -> None:
        try:
            current_loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop is None:
            self._loop = current_loop
        if current_loop is not self._loop:
            self._loop.call_soon_threadsafe(self._publish_impl, event)
            return
        self._publish_impl(event)

    def _publish_impl(self, event: StreamEvent) -> None:
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except Exception:
                # One bad subscriber must not poison siblings.
                pass
```

- [x] **Run tests + commit**

```bash
uv run pytest tests/test_stream_bus.py -v
```
Expected: all 7 pass.

```bash
git add tero2/stream_bus.py tests/test_stream_bus.py
git commit -m "add StreamBus subscribe/publish"
```

---

### Task 3: Ring-buffer drop-oldest + dead subscriber tolerance

**Files:**
- [x] Modify: `tests/test_stream_bus.py`

- [x] **Step 1: Append tests**

```python
# tests/test_stream_bus.py — appended
def test_bus_ring_buffer_drops_oldest_when_full():
    async def scenario():
        bus = StreamBus(max_queue_size=3)
        q = bus.subscribe()
        events = [make_stream_event("r", "text", content=str(i)) for i in range(5)]
        for ev in events:
            bus.publish(ev)
        received = []
        for _ in range(3):
            received.append(await asyncio.wait_for(q.get(), timeout=0.05))
        assert [e.content for e in received] == ["2", "3", "4"]

    _run(scenario())

def test_bus_survives_subscriber_with_broken_queue():
    async def scenario():
        bus = StreamBus(max_queue_size=3)
        good = bus.subscribe()

        class _BadQueue:
            def full(self):
                return False

            def put_nowait(self, item):
                raise RuntimeError("boom")

            def get_nowait(self):
                raise asyncio.QueueEmpty()

        bus._subscribers.append(_BadQueue())
        ev = make_stream_event("r", "text", content="x")
        bus.publish(ev)  # must not raise
        received = await asyncio.wait_for(good.get(), timeout=0.05)
        assert received is ev

    _run(scenario())
```

- [x] **Run tests + commit**

```bash
uv run pytest tests/test_stream_bus.py -v
```
Expected: all pass.

```bash
git add tests/test_stream_bus.py
git commit -m "pin StreamBus ring-buffer and fault-tolerance behavior"
```

---

## Chunk 2: Normalizers (Spec Build-Order Step 2)

Goal: five provider-specific normalizers converting raw output → `StreamEvent` list, plus a Protocol and a `FallbackNormalizer`. Normalizers are pure functions with golden-fixture tests.

### Task 4: Normalizer `Protocol`, `FallbackNormalizer`, and dispatcher

**Files:**
- [x] Create: `tero2/providers/normalizers/__init__.py`
- [x] Create: `tero2/providers/normalizers/base.py`
- [x] Create: `tero2/providers/normalizers/fallback.py`
- [x] Create: `tests/normalizers/__init__.py`
- [x] Create: `tests/normalizers/test_dispatcher.py`

- [x] **Step 1: Write failing tests**

```python
# tests/normalizers/test_dispatcher.py
from tero2.providers.normalizers import get_normalizer
from tero2.providers.normalizers.fallback import FallbackNormalizer

def test_dispatcher_returns_fallback_for_unknown_kind():
    norm = get_normalizer("unknown-provider")
    assert isinstance(norm, FallbackNormalizer)

def test_fallback_emits_single_status_event():
    norm = FallbackNormalizer()
    events = list(norm.normalize({"type": "foo", "x": 1}, role="builder"))
    assert len(events) == 1
    ev = events[0]
    assert ev.kind == "status"
    assert "foo" in ev.content or "raw:" in ev.content
    assert ev.raw == {"type": "foo", "x": 1}
    assert ev.role == "builder"

def test_fallback_handles_non_dict_raw():
    norm = FallbackNormalizer()
    events = list(norm.normalize("some text", role="scout"))
    assert len(events) == 1
    assert events[0].kind == "status"
    assert events[0].role == "scout"
```

- [x] **Step 2: Implement Protocol and fallback**

```python
# tero2/providers/normalizers/base.py
"""Normalizer Protocol.

Per-provider normalizers are pure functions: one ``raw`` input (dict for
CLI providers, SDK Message objects for zai) -> zero or more ``StreamEvent``.
No I/O, no global state. On parse failure yield exactly one
``StreamEvent(kind="error", content=..., raw=...)``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, Iterable, Protocol

from tero2.stream_bus import StreamEvent

class StreamNormalizer(Protocol):
    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = ...,
    ) -> Iterable[StreamEvent]:
        ...
```

```python
# tero2/providers/normalizers/fallback.py
"""FallbackNormalizer for unknown/unregistered provider kinds."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.stream_bus import StreamEvent

class FallbackNormalizer:
    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        preview = repr(raw)
        if len(preview) > 200:
            preview = preview[:200] + "…"
        raw_dict = raw if isinstance(raw, dict) else {"repr": preview}
        yield StreamEvent(
            role=role,
            kind="status",
            timestamp=now(),
            content=f"raw: {preview}",
            raw=raw_dict,
        )
```

```python
# tero2/providers/normalizers/__init__.py
"""Per-provider stream normalizers keyed by provider_kind."""

from __future__ import annotations

from tero2.providers.normalizers.base import StreamNormalizer
from tero2.providers.normalizers.fallback import FallbackNormalizer

_FALLBACK = FallbackNormalizer()
_NORMALIZERS: dict[str, StreamNormalizer] = {}

def register(kind: str, normalizer: StreamNormalizer) -> None:
    _NORMALIZERS[kind] = normalizer

def get_normalizer(provider_kind: str) -> StreamNormalizer:
    """Lookup normalizer by provider_kind (e.g. ``"claude"`` / ``"zai"``).

    NOT by ``display_name``. Unknown kinds get :class:`FallbackNormalizer`.
    """
    return _NORMALIZERS.get(provider_kind, _FALLBACK)

__all__ = ["StreamNormalizer", "FallbackNormalizer", "get_normalizer", "register"]
```

```python
# tests/normalizers/__init__.py
# (empty — marker file)
```

- [x] **Run tests + commit**

```bash
uv run pytest tests/normalizers/test_dispatcher.py -v
```

```bash
git add tero2/providers/normalizers tests/normalizers/__init__.py tests/normalizers/test_dispatcher.py
git commit -m "add normalizer protocol, fallback, and dispatcher"
```

---

### Task 5: Collect provider fixtures (HUMAN one-off)

**Files:**
- [x] Create: `tests/normalizers/fixtures/claude.jsonl`
- [x] Create: `tests/normalizers/fixtures/claude_rate_limit.jsonl`
- [x] Create: `tests/normalizers/fixtures/codex.jsonl`
- [x] Create: `tests/normalizers/fixtures/codex_tool_error.jsonl`
- [x] Create: `tests/normalizers/fixtures/opencode.jsonl`
- [x] Create: `tests/normalizers/fixtures/opencode_unknown_model.jsonl`
- [x] Create: `tests/normalizers/fixtures/kilo.jsonl`
- [x] Create: `tests/normalizers/fixtures/zai.jsonl`

- [x] **Step 1: Run each CLI** against `"read README.md and summarize in one sentence"` in a sandbox containing a tiny README, capture full raw stdout to `.jsonl`. Use the project's existing CLI invocations as a reference (`grep -n 'create_subprocess_exec' tero2/providers/cli.py`).

- [x] **Step 2: Capture negative fixtures** — invalid API key for rate_limit, read nonexistent file for tool_error, bad model name for opencode_unknown_model.

- [x] **Step 3: Manual review** — confirm each `.jsonl` has valid JSON per line, no secrets leaked. Collectively covers: text, tool_use, tool_result, thinking, turn_end, error. Note capture date in `tests/normalizers/fixtures/README.md`.

- [x] **Step 4: Commit**

```bash
git add tests/normalizers/fixtures/
git commit -m "add provider stream fixtures for normalizer tests"
```

---

### Task 6: `ClaudeNormalizer`

**Files:**
- [x] Create: `tero2/providers/normalizers/claude.py`
- [x] Create: `tests/normalizers/test_claude.py`

Claude stream-JSON mapping:
- [x] `{"type":"system","tools":[...]}` → one `kind="status"` with `content="init: N tools"`
- [x] `{"type":"assistant","message":{"content":[...blocks...]}}`:
  - `text` block → `kind="text", content=text`
  - `tool_use` block → `kind="tool_use", tool_name, tool_args, tool_id`
  - `thinking` block → `kind="thinking", content=thinking`
- [x] `{"type":"user","message":{"content":[{type:"tool_result","tool_use_id","content":...}]}}` → `kind="tool_result", tool_id, tool_output=<stringified>`
- [x] `{"type":"result","subtype":"success"}` → `kind="turn_end"`
- [x] `{"type":"error","error":{"message":"..."}}` → `kind="error", content=message`
- [x] Anything else → empty iterable

- [x] **Step 1: Failing tests** — one per block type + multi-block assistant + malformed (missing `message`) → single error event + 2 golden-fixture tests (happy + rate_limit).

```python
# tests/normalizers/test_claude.py
from pathlib import Path
import json

from tero2.providers.normalizers.claude import ClaudeNormalizer

FIXTURE_DIR = Path(__file__).parent / "fixtures"

def _load(name):
    out = []
    for line in (FIXTURE_DIR / name).read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or s.startswith("//"):
            continue
        out.append(json.loads(s))
    return out

def test_claude_text_block():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "hello"}]}},
        role="builder"))
    assert len(out) == 1
    assert out[0].kind == "text"
    assert out[0].content == "hello"

def test_claude_tool_use_block():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant",
         "message": {"content": [
             {"type": "tool_use", "id": "toolu_1", "name": "Read",
              "input": {"path": "x"}},
         ]}},
        role="builder"))
    assert out[0].kind == "tool_use"
    assert out[0].tool_name == "Read"
    assert out[0].tool_id == "toolu_1"
    assert out[0].tool_args == {"path": "x"}

def test_claude_thinking_block():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "pondering..."},
        ]}},
        role="architect"))
    assert out[0].kind == "thinking"
    assert out[0].content == "pondering..."

def test_claude_tool_result_string():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_1",
             "content": "file contents"},
        ]}},
        role="builder"))
    assert out[0].kind == "tool_result"
    assert out[0].tool_id == "toolu_1"
    assert out[0].tool_output == "file contents"

def test_claude_tool_result_list_joined():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t",
             "content": [{"type": "text", "text": "line1"},
                         {"type": "text", "text": "line2"}]},
        ]}},
        role="builder"))
    assert out[0].tool_output == "line1\nline2"

def test_claude_multi_block_message():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "thinking out loud"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
        ]}},
        role="builder"))
    assert [e.kind for e in out] == ["text", "tool_use"]

def test_claude_result_success_is_turn_end():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "result", "subtype": "success"}, role="builder"))
    assert out[0].kind == "turn_end"

def test_claude_error_block():
    n = ClaudeNormalizer()
    out = list(n.normalize(
        {"type": "error", "error": {"type": "rate_limit", "message": "slow"}},
        role="builder"))
    assert out[0].kind == "error"
    assert "slow" in out[0].content

def test_claude_malformed_assistant_yields_error():
    n = ClaudeNormalizer()
    out = list(n.normalize({"type": "assistant"}, role="builder"))
    assert len(out) == 1
    assert out[0].kind == "error"

def test_claude_golden_fixture_happy():
    n = ClaudeNormalizer()
    events = []
    for raw in _load("claude.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    kinds = {e.kind for e in events}
    assert "text" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert "turn_end" in kinds

def test_claude_rate_limit_fixture():
    n = ClaudeNormalizer()
    events = []
    for raw in _load("claude_rate_limit.jsonl"):
        events.extend(n.normalize(raw, role="builder"))
    assert any(e.kind == "error" for e in events)
```

- [x] **Step 2: Implement**

```python
# tero2/providers/normalizers/claude.py
"""Normalizer for Claude CLI stream-JSON output."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from tero2.providers.normalizers import register
from tero2.stream_bus import StreamEvent

class ClaudeNormalizer:
    def normalize(
        self,
        raw: Any,
        role: str,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> Iterable[StreamEvent]:
        if not isinstance(raw, dict):
            yield self._err(role, now(), f"non-dict raw: {type(raw).__name__}", raw)
            return

        kind = raw.get("type")
        try:
            if kind == "system":
                tools = raw.get("tools") or []
                yield StreamEvent(role=role, kind="status", timestamp=now(),
                                  content=f"init: {len(tools)} tools", raw=raw)
            elif kind == "assistant":
                yield from self._assistant(raw, role, now())
            elif kind == "user":
                yield from self._user(raw, role, now())
            elif kind == "result" and raw.get("subtype") == "success":
                yield StreamEvent(role=role, kind="turn_end",
                                  timestamp=now(), raw=raw)
            elif kind == "error":
                err = raw.get("error") or {}
                msg = err.get("message") if isinstance(err, dict) else str(err)
                yield StreamEvent(role=role, kind="error", timestamp=now(),
                                  content=msg or "unknown error", raw=raw)
        except Exception as exc:
            yield self._err(role, now(), f"parse: {exc}", raw)

    def _assistant(self, raw, role, ts):
        msg = raw.get("message")
        if not isinstance(msg, dict):
            raise ValueError("assistant.message missing")
        for block in msg.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                yield StreamEvent(role=role, kind="text", timestamp=ts,
                                  content=block.get("text") or "", raw=block)
            elif btype == "tool_use":
                yield StreamEvent(
                    role=role, kind="tool_use", timestamp=ts,
                    tool_name=block.get("name") or "",
                    tool_args=block.get("input") or {},
                    tool_id=block.get("id") or "",
                    raw=block,
                )
            elif btype == "thinking":
                yield StreamEvent(role=role, kind="thinking", timestamp=ts,
                                  content=block.get("thinking") or "", raw=block)

    def _user(self, raw, role, ts):
        msg = raw.get("message") or {}
        for block in msg.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            content = block.get("content")
            if isinstance(content, list):
                parts = []
                for sub in content:
                    if isinstance(sub, dict) and sub.get("type") == "text":
                        parts.append(sub.get("text") or "")
                    else:
                        parts.append(str(sub))
                output = "\n".join(parts)
            else:
                output = str(content) if content is not None else ""
            yield StreamEvent(
                role=role, kind="tool_result", timestamp=ts,
                tool_id=block.get("tool_use_id") or "",
                tool_output=output,
                raw=block,
            )

    def _err(self, role, ts, msg, raw):
        raw_dict = raw if isinstance(raw, dict) else {"repr": repr(raw)[:200]}
        return StreamEvent(role=role, kind="error", timestamp=ts,
                           content=msg, raw=raw_dict)

register("claude", ClaudeNormalizer())
```

- [x] **Step 3: Register via side-effect import** — add to `tero2/providers/normalizers/__init__.py`:

```python
from tero2.providers.normalizers import claude  # noqa: F401,E402
```

- [x] **Step 4: Run tests and commit**

```bash
uv run pytest tests/normalizers/test_claude.py -v
git add tero2/providers/normalizers/claude.py tero2/providers/normalizers/__init__.py tests/normalizers/test_claude.py
git commit -m "add claude stream normalizer"
```

---

### Task 7: `CodexNormalizer`

**Files:**
- [x] Create: `tero2/providers/normalizers/codex.py`
- [x] Create: `tests/normalizers/test_codex.py`

Inspect `tests/normalizers/fixtures/codex.jsonl` to confirm shape. Expected types: `text`, `tool`, `tool_output`, `done`, `error`.

Mapping:
- [x] `{"type":"text","content":"..."}` → `kind="text"`
- [x] `{"type":"tool","name":"...","input":{...},"id":"..."}` → `kind="tool_use"`
- [x] `{"type":"tool_output","id":"...","output":"..."}` → `kind="tool_result"`
- [x] `{"type":"done"}` → `kind="turn_end"`
- [x] `{"type":"error","message":"..."}` → `kind="error"`

Follow same pattern as Task 6:
- [x] Tests first (kind-per-kind + golden + tool_error fixture)
- [x] Run → fail
- [x] Implement, `register("codex", CodexNormalizer())`, side-effect import
- [x] Re-run → pass
- [x] Commit: `"add codex stream normalizer"`

If fixture shape disagrees with the mapping, update the mapping and note actual shape in a one-line comment at the top of `codex.py`.

---

### Task 8: `OpenCodeNormalizer`

**Files:** `tero2/providers/normalizers/opencode.py`, `tests/normalizers/test_opencode.py`

OpenCode `--format json` typically uses `event` key:
- [ ] `{"event":"message","role":"assistant","text":"..."}` → `kind="text"`
- [ ] `{"event":"tool_call","name":"...","args":{...},"id":"..."}` → `kind="tool_use"`
- [ ] `{"event":"tool_result","id":"...","result":"..."}` → `kind="tool_result"`
- [ ] `{"event":"end"}` → `kind="turn_end"`
- [ ] `{"event":"error","message":"..."}` → `kind="error"`

- [ ] Same TDD pattern. Plus one test using `opencode_unknown_model.jsonl` asserting at least one error event.
- [ ] Commit: `"add opencode stream normalizer"`

---

### Task 9: `KiloNormalizer`

**Files:** `tero2/providers/normalizers/kilo.py`, `tests/normalizers/test_kilo.py`

Inspect `kilo.jsonl` fixture to finalize mapping (similar mechanics to OpenCode).

- [ ] Same TDD pattern.
- [ ] Commit: `"add kilo stream normalizer"`

---

### Task 10: `ZaiNormalizer`

**Files:** `tero2/providers/normalizers/zai.py`, `tests/normalizers/test_zai.py`

`ZaiProvider` uses `claude_agent_sdk.query` which yields SDK `Message` objects — NOT dicts. Use duck-typing (class name + attribute access) rather than `isinstance` against SDK types (which may fail to import when `SDK_AVAILABLE=False`):

```python
# Pseudocode:
class_name = type(raw).__name__  # "AssistantMessage" / "UserMessage" / "ResultMessage" / "SystemMessage"
# raw.content is a list; each block has type(block).__name__ in
# {"TextBlock", "ToolUseBlock", "ToolResultBlock", "ThinkingBlock"}
# Attribute names on blocks: .text, .name, .input, .id, .tool_use_id, .content, .thinking
```

- [ ] Tests + impl + `register("zai", ZaiNormalizer())` + side-effect import.
- [ ] Commit: `"add zai SDK-stream normalizer"`

After Task 10:

```bash
uv run pytest tests/normalizers/ -v
```
Expected: all green.

---

## Chunk 3: `CLIProvider` streaming refactor (Spec Build-Order Step 3)

### Task 11: Streaming test with gate-based sync (failing)

**Files:** `tests/test_cli_provider_streaming.py`

Invariant: `CLIProvider.run` yields parsed messages while the subprocess is still running, not after `proc.wait()`. Sleep-based assertions are flaky — use an `asyncio.Event` gate controlling when the fake `proc.wait()` returns.

- [ ] **Step 1: Write failing test**

```python
# tests/test_cli_provider_streaming.py
"""Invariant: CLIProvider.run yields each parsed line while the subprocess
is still running, not after proc.wait() returns."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from tero2.providers.cli import CLIProvider
from tero2.config import Config

class _FakeStdout:
    def __init__(self, lines):
        self._items = list(lines)

    def __aiter__(self):
        return self

    async def __anext__(self):
        while self._items:
            item = self._items.pop(0)
            if isinstance(item, asyncio.Event):
                await item.wait()
                continue
            return item
        raise StopAsyncIteration

class _FakeStderr:
    async def read(self):
        return b""

class _FakeProc:
    def __init__(self, stdout_stream, exit_gate):
        self.stdout = stdout_stream
        self.stderr = _FakeStderr()
        self.stdin = None
        self._exit_gate = exit_gate
        self.returncode = 0

    async def wait(self):
        await self._exit_gate.wait()
        self.returncode = 0
        return 0

@pytest.mark.asyncio
async def test_cli_provider_yields_before_proc_exit():
    exit_gate = asyncio.Event()
    stdout = _FakeStdout([
        b'{"type":"text","text":"one"}\n',
        b'{"type":"text","text":"two"}\n',
    ])
    proc = _FakeProc(stdout, exit_gate)

    async def _fake_spawn(*a, **kw):
        return proc

    with patch("asyncio.create_subprocess_exec", _fake_spawn):
        provider = CLIProvider(name="fake", command="fake", config=Config())
        gen = provider.run(prompt="hi")
        first = await asyncio.wait_for(gen.__anext__(), timeout=0.5)
        second = await asyncio.wait_for(gen.__anext__(), timeout=0.5)
        # Gate still closed — buffering version would deadlock here.
        assert first["text"] == "one"
        assert second["text"] == "two"

        exit_gate.set()
        turn_end = await asyncio.wait_for(gen.__anext__(), timeout=0.5)
        assert turn_end.get("type") == "turn_end"
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
```

Note: `CLIProvider.__init__` signature varies — check `grep -n 'def __init__' tero2/providers/cli.py` and adjust accordingly. Key is a minimal instance that runs `.run()`.

- [ ] **Step 2: Do NOT commit yet** — Task 12 ships test + refactor in one commit.

---

### Task 12: Remove triple buffering in `CLIProvider.run`

**Files:** `tero2/providers/cli.py:192-230`, uncommitted `tests/test_cli_provider_streaming.py` from Task 11

Replace the `lines: list[str] = []; lines.append(...); ... for raw_line in lines: yield parsed` pattern with yield-inside-loop:

```python
try:
    async for line in proc.stdout:
        stripped = line.decode(errors="replace").strip()
        if not stripped:
            continue
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                yield parsed
            else:
                yield {"type": "text", "text": stripped}
        except json.JSONDecodeError:
            yield {"type": "text", "text": stripped}
except Exception:
    if stderr_task is not None:
        stderr_task.cancel()
        from contextlib import suppress
        with suppress(asyncio.CancelledError):
            await stderr_task
    raise

stderr_bytes = b""
if stderr_task is not None:
    try:
        stderr_bytes = stderr_task.result() if stderr_task.done() else await stderr_task
    except (asyncio.CancelledError, Exception):
        stderr_bytes = b""
await proc.wait()

if proc.returncode != 0:
    err_msg = stderr_bytes.decode(errors="replace").strip()
    log.error("%s exited %d: %s", self._name, proc.returncode, err_msg)
    raise ProviderError(f"{self._name} exited {proc.returncode}: {err_msg}")

yield {"type": "turn_end", "text": ""}
```

- [ ] **Step 1: Apply the refactor**
- [ ] **Step 2:** `uv run pytest tests/test_cli_provider_streaming.py -v --timeout=3` → pass
- [ ] **Run tests + commit**

```bash
git add tero2/providers/cli.py tests/test_cli_provider_streaming.py
git commit -m "stream CLIProvider output line-by-line instead of buffering"
```

---

## Chunk 4: `ProviderChain` + `BasePlayer` (Spec Build-Order Step 4)

### Task 13: `BaseProvider.kind` attribute

**Files:** `tero2/providers/base.py`, `tero2/providers/cli.py`, `tero2/providers/zai.py`, `tero2/providers/shell.py`, `tests/test_provider_kind.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_provider_kind.py
from tero2.config import Config
from tero2.providers.registry import create_provider

def test_claude_provider_kind():
    p = create_provider("claude", Config())
    assert p.kind == "claude"

def test_zai_provider_kind():
    p = create_provider("zai", Config())
    assert p.kind == "zai"

def test_base_provider_default_kind_is_empty():
    from tero2.providers.base import BaseProvider

    class _Dummy(BaseProvider):
        async def run(self, **kwargs):
            if False:
                yield None

    assert _Dummy().kind == ""
```

- [ ] **Step 2:** Add to `BaseProvider`:

```python
    @property
    def kind(self) -> str:
        """Canonical short name for normalizer dispatch (e.g. ``"claude"``).

        Distinct from :attr:`display_name` which may contain model tags.
        """
        return getattr(self, "_kind", "")
```

Set `self._kind = name` in `CLIProvider.__init__`, `self._kind = "zai"` in `ZaiProvider.__init__`, `self._kind = "shell"` in `ShellProvider.__init__`.

- [ ] **Run tests + commit**

```bash
uv run pytest tests/test_provider_kind.py -v
uv run pytest tests/ -q
```

```bash
git add tero2/providers/base.py tero2/providers/cli.py tero2/providers/zai.py tero2/providers/shell.py tests/test_provider_kind.py
git commit -m "add provider kind attribute for normalizer dispatch"
```

---

### Task 14: `ProviderChain.provider_kind` property

**Files:** `tero2/providers/chain.py`, `tests/test_chain_provider_kind.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_chain_provider_kind.py
from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain

class _Fake(BaseProvider):
    def __init__(self, name):
        self._name = name
        self._kind = name

    @property
    def display_name(self):
        return f"{self._name} display"

    @property
    def kind(self):
        return self._kind

    async def run(self, **kw):
        yield {"type": "text", "text": "ok"}

def test_provider_kind_reflects_current():
    a = _Fake("claude")
    b = _Fake("zai")
    chain = ProviderChain([a, b], cb_registry=CircuitBreakerRegistry())
    assert chain.provider_kind == "claude"
    chain._current_provider_index = 1
    assert chain.provider_kind == "zai"

def test_provider_kind_empty_when_no_providers():
    chain = ProviderChain([], cb_registry=CircuitBreakerRegistry())
    assert chain.provider_kind == ""
```

- [ ] **Step 2:** Add to `ProviderChain`:

```python
    @property
    def provider_kind(self) -> str:
        """Canonical provider_kind of the currently-active provider.

        Used by ``BasePlayer._run_prompt`` and ``RunnerContext.run_agent``
        to dispatch to the correct stream normalizer. Failover updates
        ``_current_provider_index`` so this tracks live.
        """
        if not self.providers:
            return ""
        idx = min(self._current_provider_index, len(self.providers) - 1)
        return getattr(self.providers[idx], "kind", "")
```

- [ ] **Step 3: Commit**

```bash
git add tero2/providers/chain.py tests/test_chain_provider_kind.py
git commit -m "add ProviderChain.provider_kind property"
```

---

### Task 15: Yield-aware retry/failover policy in `ProviderChain.run`

**Files:** `tero2/providers/chain.py`, `tests/test_chain_retry_policy.py`

Rules (spec §Decisions Log #8):
- [ ] Recoverable errors BEFORE first yield → retry this provider, then fail over.
- [ ] Recoverable errors AFTER first yield → hard fail.
- [ ] Non-recoverable errors → hard fail always.
- [ ] First message of shape `{"type":"error",...}` → treat as pre-yield stream error, raise `ProviderError`.
- [ ] Mid-stream `{"type":"error"}` dict → pass through unchanged (normalizer handles).

- [ ] **Step 1: Failing tests**

```python
# tests/test_chain_retry_policy.py
import asyncio
import pytest

from tero2.circuit_breaker import CircuitBreakerRegistry
from tero2.errors import ProviderError, RateLimitError
from tero2.providers.base import BaseProvider
from tero2.providers.chain import ProviderChain

class _ScriptedProvider(BaseProvider):
    def __init__(self, name, script, raise_at=None, raise_exc=None):
        self._name = name
        self._kind = name
        self._script = script
        self._raise_at = raise_at
        self._raise_exc = raise_exc or RateLimitError("scripted")
        self.calls = 0

    @property
    def display_name(self):
        return self._name

    @property
    def kind(self):
        return self._kind

    async def run(self, **kw):
        self.calls += 1
        for i, item in enumerate(self._script):
            if self._raise_at is not None and i == self._raise_at:
                raise self._raise_exc
            yield item

def _chain(*providers, retries=2):
    return ProviderChain(
        list(providers),
        cb_registry=CircuitBreakerRegistry(),
        rate_limit_max_retries=retries,
        rate_limit_wait_s=0.0,
    )

async def _collect(chain):
    return [m async for m in chain.run(prompt="hi")]

def test_retry_before_first_yield_succeeds():
    async def scenario():
        failing = _ScriptedProvider(
            "a", [{"type": "text", "text": "x"}],
            raise_at=0, raise_exc=RateLimitError("rl"))
        good = _ScriptedProvider("b", [{"type": "text", "text": "ok"}])
        chain = _chain(failing, good, retries=1)
        out = await _collect(chain)
        assert out == [{"type": "text", "text": "ok"}]
        assert good.calls == 1

    asyncio.run(scenario())

def test_error_after_first_yield_is_hard_fail():
    async def scenario():
        mid_fail = _ScriptedProvider(
            "a",
            [{"type": "text", "text": "partial"}],
            raise_at=1,
            raise_exc=RateLimitError("mid"),
        )
        good = _ScriptedProvider("b", [{"type": "text", "text": "ok"}])
        chain = _chain(mid_fail, good, retries=3)
        with pytest.raises(RateLimitError):
            await _collect(chain)
        assert good.calls == 0

    asyncio.run(scenario())

def test_first_msg_error_triggers_retry():
    async def scenario():
        error_first = _ScriptedProvider(
            "a", [{"type": "error", "error": {"message": "rate limit"}}])
        good = _ScriptedProvider("b", [{"type": "text", "text": "ok"}])
        chain = _chain(error_first, good, retries=1)
        out = await _collect(chain)
        assert out == [{"type": "text", "text": "ok"}]
        assert good.calls == 1

    asyncio.run(scenario())

def test_mid_stream_error_message_passes_through():
    async def scenario():
        mixed = _ScriptedProvider(
            "a",
            [{"type": "text", "text": "hello"},
             {"type": "error", "error": {"message": "oops"}}])
        chain = _chain(mixed, retries=0)
        out = await _collect(chain)
        assert out == [
            {"type": "text", "text": "hello"},
            {"type": "error", "error": {"message": "oops"}},
        ]

    asyncio.run(scenario())

def test_non_recoverable_raises_without_retry():
    async def scenario():
        p = _ScriptedProvider(
            "a", [{"type": "text", "text": "x"}],
            raise_at=0, raise_exc=ValueError("coder error"))
        chain = _chain(p, retries=5)
        with pytest.raises(ValueError):
            await _collect(chain)
        assert p.calls == 1

    asyncio.run(scenario())
```

- [ ] **Step 2:** Run → expect 3+ failures against current chain.

- [ ] **Step 3:** Rewrite `ProviderChain.run`:

```python
async def run(self, **kwargs: Any) -> AsyncGenerator[Any, None]:
    for idx, provider in enumerate(self.providers):
        cb = self.cb_registry.get(provider.display_name)
        if not cb.is_available:
            continue
        self._current_provider_index = idx

        for attempt in range(self._rate_limit_max_retries + 1):
            if attempt > 0:
                await asyncio.sleep(
                    self._rate_limit_wait_s * (2 ** (attempt - 1))
                    + random.uniform(0, 0.25)
                )

            yielded_anything = False
            try:
                async for msg in provider.run(**kwargs):
                    if (not yielded_anything
                            and isinstance(msg, dict)
                            and msg.get("type") == "error"):
                        err = msg.get("error") or {}
                        text = (err.get("message")
                                if isinstance(err, dict) else str(err)) \
                               or "stream error"
                        raise ProviderError(text)
                    yielded_anything = True
                    yield msg
                cb.record_success()
                return
            except Exception as exc:
                if not _is_recoverable_error(exc):
                    cb.record_failure()
                    raise
                if yielded_anything:
                    cb.record_failure()
                    raise
                if attempt >= self._rate_limit_max_retries:
                    cb.record_failure()
                    break

    raise RateLimitError("all providers in chain exhausted")
```

- [ ] **Run tests + commit**

```bash
uv run pytest tests/test_chain_retry_policy.py -v
uv run pytest tests/ -q
```

If pre-existing chain tests fail, they may encode old mid-stream-retry behavior — consult spec §Decisions Log #8 before changing them (spec is authoritative).

```bash
git add tero2/providers/chain.py tests/test_chain_retry_policy.py
git commit -m "restrict chain retries/failover to pre-yield errors only"
```

---

### Task 16: Stream-aware `BasePlayer._run_prompt`

**Files:** `tero2/players/base.py`, `tests/test_player_stream_integration.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_player_stream_integration.py
import pytest
from datetime import datetime, timezone

from tero2.players.base import BasePlayer, PlayerResult
from tero2.stream_bus import StreamBus

class _FakeChain:
    def __init__(self, kind, script):
        self.kind = kind
        self._script = script
        self.providers = []
        self._current_provider_index = 0

    @property
    def provider_kind(self):
        return self.kind

    async def run_prompt(self, prompt):
        for item in self._script:
            yield item

class _MyPlayer(BasePlayer):
    role = "builder"

    async def run(self, **kwargs):
        captured = await self._run_prompt("hi")
        return PlayerResult(success=True, captured_output=captured)

@pytest.mark.asyncio
async def test_player_publishes_to_bus_and_returns_text_only():
    bus = StreamBus()
    q = bus.subscribe()
    chain = _FakeChain("claude", [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "hello"},
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"p": "x"}},
        ]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "file data"},
        ]}},
        {"type": "result", "subtype": "success"},
    ])
    player = _MyPlayer(chain, disk=None, stream_bus=bus)
    result = await player.run()

    received = []
    while not q.empty():
        received.append(q.get_nowait())
    kinds = [e.kind for e in received]
    assert "text" in kinds
    assert "tool_use" in kinds
    assert "tool_result" in kinds
    assert "turn_end" in kinds

    # Return string contains only kind="text" content — no tool_output.
    assert result.captured_output == "hello"
    assert "file data" not in result.captured_output

@pytest.mark.asyncio
async def test_player_without_bus_still_works():
    chain = _FakeChain("claude", [
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "hi"}]}},
    ])
    player = _MyPlayer(chain, disk=None, stream_bus=None)
    result = await player.run()
    assert result.captured_output == "hi"
```

- [ ] **Step 2:** Run → `TypeError: unexpected keyword argument 'stream_bus'`.

- [ ] **Step 3: Modify `BasePlayer`**

```python
# tero2/players/base.py
from datetime import datetime, timezone

from tero2.providers.normalizers import get_normalizer
from tero2.stream_bus import StreamBus, StreamEvent

class BasePlayer(ABC):
    role: str = ""

    def __init__(
        self,
        chain: ProviderChain,
        disk: "DiskLayer | None",
        *,
        working_dir: str = "",
        stream_bus: StreamBus | None = None,
    ) -> None:
        self.chain = chain
        self.disk = disk
        self.working_dir = working_dir
        self._stream_bus = stream_bus

    async def _run_prompt(self, prompt: str) -> str:
        text_parts: list[str] = []
        async for raw in self.chain.run_prompt(prompt):
            normalizer = get_normalizer(self.chain.provider_kind)
            try:
                events = list(normalizer.normalize(raw, self.role))
            except Exception as exc:
                events = [StreamEvent(
                    role=self.role,
                    kind="error",
                    timestamp=datetime.now(timezone.utc),
                    content=f"normalizer error: {exc}",
                    raw={"raw_repr": repr(raw)[:200]},
                )]
            for event in events:
                if self._stream_bus is not None:
                    self._stream_bus.publish(event)
                if event.kind == "text":
                    text_parts.append(event.content)
        return "\n".join(text_parts)
```

Check: `grep -n 'def run_prompt' tero2/providers/chain.py`. If `run_prompt` is a collector that returns a string, the streaming version may be named differently (e.g., `run_prompt_stream`). Use whichever is the async-generator streaming method; do NOT break the string-returning collector (legacy callers may depend on it).

- [ ] **Run tests + commit**

```bash
uv run pytest tests/test_player_stream_integration.py -v
uv run pytest tests/ -q
```

```bash
git add tero2/players/base.py tests/test_player_stream_integration.py
git commit -m "make BasePlayer._run_prompt normalize and publish to stream bus"
```

---

### Task 17: Cascade `stream_bus` kwarg through 6 player subclasses

**Files:** `tero2/players/{architect,scout,builder,coach,verifier,reviewer}.py`

Each subclass currently has:
```python
def __init__(self, chain, disk, *, working_dir=""):
    super().__init__(chain, disk, working_dir=working_dir)
```

Change each to:
```python
def __init__(self, chain, disk, *, working_dir="", stream_bus=None):
    super().__init__(chain, disk, working_dir=working_dir, stream_bus=stream_bus)
```

Use `TYPE_CHECKING` import to avoid circular deps:

```python
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from tero2.stream_bus import StreamBus
```

- [ ] **Step 1:** Apply the 6 identical edits.
- [ ] **Run tests + commit**

```bash
git add tero2/players/architect.py tero2/players/scout.py tero2/players/builder.py tero2/players/coach.py tero2/players/verifier.py tero2/players/reviewer.py
git commit -m "thread stream_bus kwarg through player subclasses"
```

---

## Chunk 5: TUI Widgets (Spec Build-Order Step 5)

### Task 18: `StreamEventFormatter`

**Files:** `tero2/tui/widgets/stream_event_formatter.py`, `tests/test_stream_event_formatter.py`

Truncation rules (when `raw_mode=False`):
- [ ] `tool_output`: first 2 lines + `    … +<N> bytes` suffix
- [ ] `thinking`: `💭 thinking… (<N> chars)`
- [ ] `text`: no truncation

Colors by kind: text=yellow, tool_use=green (name bold), tool_result=dim white, thinking=dim grey58, status=cyan, error=bold red, turn_end=dim cyan separator.

Role color prefix: scout=cyan, architect=blue, builder=green, coach=yellow, verifier=magenta, reviewer=purple, executor=white.

- [ ] **Step 1: Failing tests**

```python
# tests/test_stream_event_formatter.py
from datetime import datetime, timezone

from rich.text import Text

from tero2.stream_bus import StreamEvent
from tero2.tui.widgets.stream_event_formatter import format as fmt

def _ev(**kw):
    return StreamEvent(
        role=kw.pop("role", "builder"),
        kind=kw.pop("kind", "text"),
        timestamp=datetime(2026, 4, 20, tzinfo=timezone.utc),
        **kw,
    )

def test_text_basic():
    t = fmt(_ev(kind="text", content="hello world"))
    assert isinstance(t, Text)
    assert "hello world" in t.plain

def test_tool_use_shows_name_and_args():
    t = fmt(_ev(kind="tool_use", tool_name="Read",
                tool_args={"path": "x.py"}, tool_id="t1"))
    assert "Read" in t.plain
    assert "x.py" in t.plain

def test_tool_result_truncated():
    body = "\n".join(f"line{i}" for i in range(10))
    t = fmt(_ev(kind="tool_result", tool_id="t1", tool_output=body),
            raw_mode=False)
    assert "line0" in t.plain and "line1" in t.plain
    assert "line9" not in t.plain
    assert "+" in t.plain and "bytes" in t.plain

def test_tool_result_raw_mode_full():
    body = "\n".join(f"line{i}" for i in range(10))
    t = fmt(_ev(kind="tool_result", tool_id="t1", tool_output=body),
            raw_mode=True)
    for i in range(10):
        assert f"line{i}" in t.plain

def test_thinking_collapsed():
    t = fmt(_ev(kind="thinking", content="x" * 300), raw_mode=False)
    assert "thinking" in t.plain.lower()
    assert "300" in t.plain

def test_thinking_raw():
    t = fmt(_ev(kind="thinking", content="pondering deeply"), raw_mode=True)
    assert "pondering deeply" in t.plain

def test_status():
    t = fmt(_ev(kind="status", content="init: 12 tools"))
    assert "12 tools" in t.plain

def test_error():
    t = fmt(_ev(kind="error", content="boom"))
    assert "boom" in t.plain

def test_turn_end_never_raises():
    t = fmt(_ev(kind="turn_end"))
    assert isinstance(t, Text)
```

- [ ] **Step 2: Implement**

```python
# tero2/tui/widgets/stream_event_formatter.py
"""Pure formatter: StreamEvent -> rich.Text.

Truncation happens here (not in normalizers) so raw mode can show the full
content without re-fetching.
"""

from __future__ import annotations

from rich.text import Text

from tero2.stream_bus import StreamEvent

_ROLE_COLORS = {
    "scout": "cyan", "architect": "blue", "builder": "green",
    "coach": "yellow", "verifier": "magenta", "reviewer": "purple",
    "executor": "white", "": "white",
}
_KIND_STYLES = {
    "text": "yellow", "tool_use": "green", "tool_result": "dim white",
    "thinking": "dim grey58", "status": "cyan", "error": "bold red",
    "turn_end": "dim cyan",
}
_TOOL_RESULT_VISIBLE_LINES = 2
_THINKING_COLLAPSE_PREFIX = "💭 thinking… "

def format(event: StreamEvent, *, raw_mode: bool = False) -> Text:
    role_color = _ROLE_COLORS.get(event.role, "white")
    prefix = Text(f"[{event.role}] ", style=role_color) if event.role else Text("")
    style = _KIND_STYLES.get(event.kind, "white")

    if event.kind == "text":
        return prefix + Text(event.content, style=style)

    if event.kind == "tool_use":
        args = ", ".join(f"{k}={v!r}" for k, v in event.tool_args.items())
        if len(args) > 120:
            args = args[:120] + "…"
        return prefix + Text.assemble(
            (event.tool_name, "bold green"),
            Text(f"({args})", style="green"),
        )

    if event.kind == "tool_result":
        if raw_mode or not event.tool_output:
            body = event.tool_output
        else:
            lines = event.tool_output.splitlines()
            if len(lines) <= _TOOL_RESULT_VISIBLE_LINES:
                body = event.tool_output
            else:
                visible = "\n".join(lines[:_TOOL_RESULT_VISIBLE_LINES])
                leftover = len(event.tool_output.encode()) - len(visible.encode())
                body = f"{visible}\n    … +{leftover} bytes"
        return prefix + Text(body, style=style)

    if event.kind == "thinking":
        if raw_mode:
            return prefix + Text(event.content, style=style)
        return prefix + Text(
            f"{_THINKING_COLLAPSE_PREFIX}({len(event.content)} chars)",
            style=style,
        )

    if event.kind == "status":
        return prefix + Text(event.content, style=style)

    if event.kind == "error":
        return prefix + Text(f"ERROR: {event.content}", style=style)

    if event.kind == "turn_end":
        return Text("─ turn end ─", style=style)

    return prefix + Text(str(event.content), style="white")
```

- [ ] **Run tests + commit**

```bash
git add tero2/tui/widgets/stream_event_formatter.py tests/test_stream_event_formatter.py
git commit -m "add StreamEventFormatter with truncation and role colors"
```

---

### Task 19: `StatusLog` widget

**Files:** `tero2/tui/widgets/status_log.py`, `tests/test_status_log.py`

4-line `RichLog` subscribed to `EventDispatcher` (not StreamBus). Renders only: `phase_change`, `stuck`, `done`, `error`, `escalation`, `provider_switch`.

- [ ] **Step 1: Failing test** (via Textual Pilot)

```python
# tests/test_status_log.py
import pytest
from textual.app import App

from tero2.events import Event
from tero2.tui.widgets.status_log import StatusLog

class _Host(App):
    def compose(self):
        yield StatusLog(id="sl")

@pytest.mark.asyncio
async def test_shows_phase_change():
    app = _Host()
    async with app.run_test() as pilot:
        sl = app.query_one("#sl", StatusLog)
        sl.on_event(Event(kind="phase_change", data={"phase": "build"}))
        await pilot.pause()
        rendered = "\n".join(str(l) for l in sl.lines)
        assert "phase_change" in rendered or "build" in rendered

@pytest.mark.asyncio
async def test_ignores_noise():
    app = _Host()
    async with app.run_test() as pilot:
        sl = app.query_one("#sl", StatusLog)
        sl.on_event(Event(kind="step", data={"n": 1}))
        sl.on_event(Event(kind="heartbeat", data={}))
        await pilot.pause()
        assert not sl.lines or all(
            "step" not in str(l) and "heartbeat" not in str(l)
            for l in sl.lines
        )
```

Check `tero2/events.py` for exact `Event` dataclass shape (`grep -n 'class Event' tero2/events.py`) and adapt construction.

- [ ] **Step 2-3: Run, implement**

```python
# tero2/tui/widgets/status_log.py
"""Compact 4-line log for high-signal orchestration events.

Subscribes to EventDispatcher (NOT StreamBus).
"""

from __future__ import annotations

from textual.widgets import RichLog

from tero2.events import Event

_RENDERED_KINDS = {
    "phase_change", "stuck", "done", "error",
    "escalation", "provider_switch",
}

class StatusLog(RichLog):
    def __init__(self, **kwargs):
        super().__init__(max_lines=4, markup=False, highlight=False, **kwargs)

    def on_event(self, event: Event) -> None:
        if event.kind not in _RENDERED_KINDS:
            return
        data_repr = " ".join(f"{k}={v}" for k, v in (event.data or {}).items())
        self.write(f"[{event.kind}] {data_repr}")
```

- [ ] **Step 3-5: Run, commit**

```bash
git add tero2/tui/widgets/status_log.py tests/test_status_log.py
git commit -m "add StatusLog widget for orchestration events"
```

---

### Task 20: `HeartbeatSidebar` widget

**Files:** `tero2/tui/widgets/heartbeat_sidebar.py`, `tests/test_heartbeat_sidebar.py`

Fixed role display order (also used for hotkey indexing): `scout, architect, builder, coach, verifier, reviewer, executor`. Per-role `RoleMetrics`: status, elapsed_s, tool_count, last_line, provider, model, started_at.

- [ ] **Step 1: Failing tests**

```python
# tests/test_heartbeat_sidebar.py
import pytest
from datetime import datetime, timezone

from textual.app import App

from tero2.events import Event
from tero2.stream_bus import StreamEvent
from tero2.tui.widgets.heartbeat_sidebar import HeartbeatSidebar, SIDEBAR_ROLE_ORDER

class _Host(App):
    def compose(self):
        yield HeartbeatSidebar(id="hb")

def _sev(role, kind, **kw):
    return StreamEvent(role=role, kind=kind,
                       timestamp=datetime.now(timezone.utc), **kw)

@pytest.mark.asyncio
async def test_shows_all_seven_roles():
    app = _Host()
    async with app.run_test() as pilot:
        hb = app.query_one("#hb", HeartbeatSidebar)
        await pilot.pause()
        for role in SIDEBAR_ROLE_ORDER:
            assert role in hb.render_plain()

@pytest.mark.asyncio
async def test_tool_count_bumps():
    app = _Host()
    async with app.run_test() as pilot:
        hb = app.query_one("#hb", HeartbeatSidebar)
        hb.on_stream_event(_sev("builder", "tool_use", tool_name="Read"))
        hb.on_stream_event(_sev("builder", "tool_use", tool_name="Edit"))
        hb.on_stream_event(_sev("builder", "text", content="done"))
        await pilot.pause()
        assert hb.metrics["builder"].tool_count == 2
        assert hb.metrics["builder"].status == "running"

@pytest.mark.asyncio
async def test_done_transition():
    app = _Host()
    async with app.run_test() as pilot:
        hb = app.query_one("#hb", HeartbeatSidebar)
        hb.on_stream_event(_sev("scout", "text", content="scanning"))
        hb.on_phase_event(Event(kind="done", data={"role": "scout"}))
        await pilot.pause()
        assert hb.metrics["scout"].status == "done"

@pytest.mark.asyncio
async def test_error_transition():
    app = _Host()
    async with app.run_test() as pilot:
        hb = app.query_one("#hb", HeartbeatSidebar)
        hb.on_stream_event(_sev("builder", "error", content="rate limit"))
        await pilot.pause()
        assert hb.metrics["builder"].status == "error"
```

- [ ] **Step 2-3: Run, implement**

```python
# tero2/tui/widgets/heartbeat_sidebar.py
"""Sidebar showing live heartbeat for each of the 7 SORA roles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from textual.reactive import reactive
from textual.widgets import Static

from tero2.events import Event
from tero2.stream_bus import StreamEvent

SIDEBAR_ROLE_ORDER: tuple[str, ...] = (
    "scout", "architect", "builder",
    "coach", "verifier", "reviewer", "executor",
)

_STATUS_DOTS = {
    "idle": "⚪", "running": "🟢", "async": "🟡",
    "error": "🔴", "done": "✓",
}

@dataclass
class RoleMetrics:
    status: str = "idle"
    elapsed_s: float = 0.0
    tool_count: int = 0
    last_line: str = ""
    provider: str = ""
    model: str = ""
    started_at: datetime | None = None

class HeartbeatSidebar(Static):
    DEFAULT_CSS = """
    HeartbeatSidebar { width: 26; border: solid $panel; }
    """

    active_role: reactive[str] = reactive("")

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.metrics: dict[str, RoleMetrics] = {
            r: RoleMetrics() for r in SIDEBAR_ROLE_ORDER
        }

    def on_mount(self) -> None:
        self._refresh_render()
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        for m in self.metrics.values():
            if m.status == "running" and m.started_at is not None:
                m.elapsed_s = (now - m.started_at).total_seconds()
        self._refresh_render()

    def on_stream_event(self, ev: StreamEvent) -> None:
        if ev.role not in self.metrics:
            return
        m = self.metrics[ev.role]
        if m.started_at is None:
            m.started_at = ev.timestamp
        m.status = "error" if ev.kind == "error" else "running"
        if ev.kind == "tool_use":
            m.tool_count += 1
            m.last_line = f"→ {ev.tool_name}"
        elif ev.kind == "text" and ev.content:
            m.last_line = ev.content.splitlines()[0][:40]
        elif ev.kind == "tool_result":
            m.last_line = f"← {ev.tool_id}"
        elif ev.kind == "error":
            m.last_line = f"err: {ev.content[:40]}"
        self._refresh_render()

    def on_phase_event(self, ev: Event) -> None:
        role = (ev.data or {}).get("role") if ev.data else None
        if ev.kind == "done" and role in self.metrics:
            self.metrics[role].status = "done"
        elif ev.kind == "error" and role in self.metrics:
            self.metrics[role].status = "error"
        elif ev.kind == "phase_change" and role in self.metrics:
            if self.metrics[role].status == "idle":
                self.metrics[role].status = "running"
                self.metrics[role].started_at = datetime.now(timezone.utc)
        self._refresh_render()

    def render_plain(self) -> str:
        rows = []
        for role in SIDEBAR_ROLE_ORDER:
            m = self.metrics[role]
            dot = _STATUS_DOTS.get(m.status, "⚪")
            elapsed = f"{int(m.elapsed_s)}s" if m.elapsed_s else "-"
            row = f"{dot} {role:<10} {m.tool_count:>3}t {elapsed:>5}"
            if m.last_line:
                row += f"\n   {m.last_line[:22]}"
            rows.append(row)
        return "\n".join(rows)

    def _refresh_render(self) -> None:
        self.update(self.render_plain())
```

- [ ] **Step 3-5: Run, commit**

```bash
git add tero2/tui/widgets/heartbeat_sidebar.py tests/test_heartbeat_sidebar.py
git commit -m "add HeartbeatSidebar widget with per-role metrics"
```

---

### Task 21: `RoleStreamPanel` widget

**Files:** `tero2/tui/widgets/stream_panel.py`, `tests/test_stream_panel.py`

Reactives: `active_role`, `pinned_role`, `raw_mode`. Per-role deque(maxlen=500).

Auto-switch priority: `builder > verifier > architect > scout > reviewer > coach > executor` (spec §TUI Changes). Active window 5s. Pin overrides auto-switch.

- [ ] **Step 1: Failing tests**

```python
# tests/test_stream_panel.py
import pytest
from datetime import datetime, timezone

from textual.app import App

from tero2.stream_bus import StreamEvent
from tero2.tui.widgets.stream_panel import RoleStreamPanel

class _Host(App):
    def compose(self):
        yield RoleStreamPanel(id="sp")

def _sev(role, kind, content=""):
    return StreamEvent(role=role, kind=kind, content=content,
                       timestamp=datetime.now(timezone.utc))

@pytest.mark.asyncio
async def test_auto_switch_builder_over_scout():
    app = _Host()
    async with app.run_test() as pilot:
        sp = app.query_one("#sp", RoleStreamPanel)
        sp.on_stream_event(_sev("scout", "text", "scan"))
        sp.on_stream_event(_sev("builder", "text", "build"))
        await pilot.pause()
        assert sp.active_role == "builder"

@pytest.mark.asyncio
async def test_pin_overrides_auto_switch():
    app = _Host()
    async with app.run_test() as pilot:
        sp = app.query_one("#sp", RoleStreamPanel)
        sp.pinned_role = "scout"
        sp.on_stream_event(_sev("scout", "text", "A"))
        sp.on_stream_event(_sev("builder", "text", "B"))
        await pilot.pause()
        assert sp.active_role == "scout"

@pytest.mark.asyncio
async def test_renders_only_active_role():
    app = _Host()
    async with app.run_test() as pilot:
        sp = app.query_one("#sp", RoleStreamPanel)
        sp.pinned_role = "builder"
        sp.on_stream_event(_sev("scout", "text", "scout-only"))
        sp.on_stream_event(_sev("builder", "text", "builder-line"))
        await pilot.pause()
        rendered = sp._render_active_buffer_plain()
        assert "builder-line" in rendered
        assert "scout-only" not in rendered

@pytest.mark.asyncio
async def test_raw_mode_toggle():
    app = _Host()
    async with app.run_test() as pilot:
        sp = app.query_one("#sp", RoleStreamPanel)
        sp.pinned_role = "builder"
        long_body = "\n".join(f"L{i}" for i in range(20))
        sp.on_stream_event(StreamEvent(
            role="builder", kind="tool_result", tool_id="t1",
            tool_output=long_body,
            timestamp=datetime.now(timezone.utc),
        ))
        await pilot.pause()
        short = sp._render_active_buffer_plain()
        assert "L0" in short and "L19" not in short
        sp.raw_mode = True
        await pilot.pause()
        full = sp._render_active_buffer_plain()
        assert "L19" in full
```

- [ ] **Step 2-3: Run, implement**

```python
# tero2/tui/widgets/stream_panel.py
"""Main panel showing the live stream of the currently-active role."""

from __future__ import annotations

from collections import deque
from datetime import datetime, timezone

from textual.reactive import reactive
from textual.widgets import RichLog

from tero2.stream_bus import StreamEvent
from tero2.tui.widgets.stream_event_formatter import format as fmt_event

_PRIORITY = {
    "builder": 100, "verifier": 90, "architect": 80,
    "scout": 70, "reviewer": 60, "coach": 50, "executor": 40,
}
_ACTIVE_WINDOW_S = 5.0
_PER_ROLE_BUFFER = 500

class RoleStreamPanel(RichLog):
    active_role: reactive[str] = reactive("")
    pinned_role: reactive[str | None] = reactive(None)
    raw_mode: reactive[bool] = reactive(False)

    def __init__(self, **kwargs):
        super().__init__(markup=False, highlight=False, wrap=True, **kwargs)
        self._buffers: dict[str, deque[StreamEvent]] = {}
        self._last_seen: dict[str, datetime] = {}

    def on_stream_event(self, ev: StreamEvent) -> None:
        buf = self._buffers.setdefault(ev.role, deque(maxlen=_PER_ROLE_BUFFER))
        buf.append(ev)
        self._last_seen[ev.role] = ev.timestamp
        self._recompute_active_role()
        if ev.role == self.active_role:
            self.write(fmt_event(ev, raw_mode=self.raw_mode))

    def watch_active_role(self, old: str, new: str) -> None:
        self.clear()
        for ev in self._buffers.get(new, ()):
            self.write(fmt_event(ev, raw_mode=self.raw_mode))

    def watch_raw_mode(self, old: bool, new: bool) -> None:
        self.clear()
        for ev in self._buffers.get(self.active_role, ()):
            self.write(fmt_event(ev, raw_mode=new))

    def watch_pinned_role(self, old, new) -> None:
        self._recompute_active_role()

    def _recompute_active_role(self) -> None:
        if self.pinned_role:
            self.active_role = self.pinned_role
            return
        now = datetime.now(timezone.utc)
        candidates = [
            r for r, ts in self._last_seen.items()
            if (now - ts).total_seconds() < _ACTIVE_WINDOW_S
        ]
        if not candidates:
            if self._last_seen:
                self.active_role = max(self._last_seen, key=self._last_seen.get)
            return
        self.active_role = max(candidates, key=lambda r: _PRIORITY.get(r, 0))

    def _render_active_buffer_plain(self) -> str:
        parts = []
        for ev in self._buffers.get(self.active_role, ()):
            parts.append(fmt_event(ev, raw_mode=self.raw_mode).plain)
        return "\n".join(parts)
```

- [ ] **Step 3-5: Run, commit**

```bash
git add tero2/tui/widgets/stream_panel.py tests/test_stream_panel.py
git commit -m "add RoleStreamPanel with auto-switch, pin, and raw-mode"
```

---

## Chunk 6: `DashboardApp` wiring + hotkeys (Spec Build-Order Step 6)

### Task 22: New compose, bus subscription, cleanup

**Files:** `tero2/tui/app.py`, `tests/test_app_stream_wiring.py`

Add `stream_bus` parameter to `__init__`. Replace `compose` with horizontal layout: `PipelinePanel`, `RoleStreamPanel | HeartbeatSidebar | UsagePanel`, `StatusLog`, `StuckHintWidget`. Subscribe to both dispatcher and bus in `on_mount`. Unsubscribe in `on_unmount`.

- [ ] **Step 1: Failing tests**

```python
# tests/test_app_stream_wiring.py
import asyncio
import pytest
from datetime import datetime, timezone

from tero2.events import Event, EventDispatcher
from tero2.stream_bus import StreamBus, StreamEvent
from tero2.tui.app import DashboardApp

class _NoopRunner:
    async def run(self):
        await asyncio.sleep(0)

@pytest.mark.asyncio
async def test_stream_events_route_to_panel():
    dispatcher = EventDispatcher()
    bus = StreamBus()
    cq = asyncio.Queue()
    runner = _NoopRunner()
    app = DashboardApp(runner=runner, dispatcher=dispatcher,
                       command_queue=cq, stream_bus=bus)

    async with app.run_test() as pilot:
        bus.publish(StreamEvent(
            role="builder", kind="text", content="live text",
            timestamp=datetime.now(timezone.utc),
        ))
        await pilot.pause(0.05)
        panel = app.query_one("#stream-panel")
        assert panel.active_role == "builder"
        assert len(panel._buffers.get("builder", [])) == 1

@pytest.mark.asyncio
async def test_phase_events_route_to_status_log():
    dispatcher = EventDispatcher()
    bus = StreamBus()
    cq = asyncio.Queue()
    runner = _NoopRunner()
    app = DashboardApp(runner=runner, dispatcher=dispatcher,
                       command_queue=cq, stream_bus=bus)

    async with app.run_test() as pilot:
        dispatcher.publish(Event(kind="phase_change",
                                 data={"phase": "build", "role": "builder"}))
        await pilot.pause(0.05)
        status = app.query_one("#status-log")
        rendered = "\n".join(str(l) for l in status.lines)
        assert "phase_change" in rendered or "build" in rendered
```

Adjust `Event` / `EventDispatcher` construction to match `tero2/events.py`.

- [ ] **Step 2: Modify `DashboardApp`**

- [ ] Signature: add `stream_bus`.
- [ ] Replace `compose`:

```python
def compose(self):
    yield Header()
    yield PipelinePanel(id="pipeline")
    with Horizontal(id="main-row"):
        yield RoleStreamPanel(id="stream-panel")
        yield HeartbeatSidebar(id="heartbeat")
        yield UsagePanel(id="usage-panel")
    yield StatusLog(id="status-log")
    hint = StuckHintWidget(id="stuck-hint")
    hint.display = False
    yield hint
    yield Footer()
```

- [ ] Lifecycle:

```python
def on_mount(self) -> None:
    self._event_queue = self._dispatcher.subscribe()
    self._stream_queue = self._stream_bus.subscribe()
    self._runner_worker = self.run_worker(self._run_runner(), exclusive=True)
    self.run_worker(self._consume_events(), exclusive=False)
    self.run_worker(self._consume_stream(), exclusive=False)

def on_unmount(self) -> None:
    if getattr(self, "_event_queue", None) is not None:
        self._dispatcher.unsubscribe(self._event_queue)
    if getattr(self, "_stream_queue", None) is not None:
        self._stream_bus.unsubscribe(self._stream_queue)
```

- [ ] Workers:

```python
async def _consume_stream(self) -> None:
    panel = self.query_one("#stream-panel", RoleStreamPanel)
    heartbeat = self.query_one("#heartbeat", HeartbeatSidebar)
    while True:
        ev = await self._stream_queue.get()
        panel.on_stream_event(ev)
        heartbeat.on_stream_event(ev)

async def _consume_events(self) -> None:
    status_log = self.query_one("#status-log", StatusLog)
    heartbeat = self.query_one("#heartbeat", HeartbeatSidebar)
    while True:
        ev = await self._event_queue.get()
        status_log.on_event(ev)
        heartbeat.on_phase_event(ev)
```

- [ ] Remove `LogView` from `compose` only (keep file on disk).

- [ ] **Run tests + commit**

```bash
uv run pytest tests/test_app_stream_wiring.py -v
uv run pytest tests/ -q
```

```bash
git add tero2/tui/app.py tests/test_app_stream_wiring.py
git commit -m "wire StreamBus into DashboardApp; replace LogView in layout"
```

---

### Task 23: Hotkeys — `v` raw, `0` unpin, `1-7` pin (stuck fallback)

**Files:** `tero2/tui/app.py`

1-7 are always bound. In the action: if `#stuck-hint` is visible AND digit is 1-5, send stuck steer command; otherwise pin role at index N (`SIDEBAR_ROLE_ORDER[N-1]`).

- [ ] **Step 1: Append tests**

```python
# tests/test_app_stream_wiring.py — append
@pytest.mark.asyncio
async def test_hotkey_1_pins_scout():
    dispatcher = EventDispatcher()
    bus = StreamBus()
    cq = asyncio.Queue()
    runner = _NoopRunner()
    app = DashboardApp(runner=runner, dispatcher=dispatcher,
                       command_queue=cq, stream_bus=bus)
    async with app.run_test() as pilot:
        await pilot.press("1")
        await pilot.pause(0.05)
        panel = app.query_one("#stream-panel")
        assert panel.pinned_role == "scout"

@pytest.mark.asyncio
async def test_hotkey_0_unpins():
    dispatcher = EventDispatcher()
    bus = StreamBus()
    cq = asyncio.Queue()
    runner = _NoopRunner()
    app = DashboardApp(runner=runner, dispatcher=dispatcher,
                       command_queue=cq, stream_bus=bus)
    async with app.run_test() as pilot:
        panel = app.query_one("#stream-panel")
        panel.pinned_role = "builder"
        await pilot.press("0")
        await pilot.pause(0.05)
        assert panel.pinned_role is None

@pytest.mark.asyncio
async def test_hotkey_v_toggles_raw():
    dispatcher = EventDispatcher()
    bus = StreamBus()
    cq = asyncio.Queue()
    runner = _NoopRunner()
    app = DashboardApp(runner=runner, dispatcher=dispatcher,
                       command_queue=cq, stream_bus=bus)
    async with app.run_test() as pilot:
        panel = app.query_one("#stream-panel")
        assert panel.raw_mode is False
        await pilot.press("v")
        await pilot.pause(0.05)
        assert panel.raw_mode is True
```

- [ ] **Step 2: Update `BINDINGS` + actions**

```python
from tero2.tui.widgets.heartbeat_sidebar import SIDEBAR_ROLE_ORDER

BINDINGS = [
    *existing_bindings_unchanged,
    ("v", "toggle_raw", "Raw"),
    ("c", "clear_stream", "Clear"),
    ("0", "unpin", "Unpin"),
    ("1", "digit_1", "1"),
    ("2", "digit_2", "2"),
    ("3", "digit_3", "3"),
    ("4", "digit_4", "4"),
    ("5", "digit_5", "5"),
    ("6", "digit_6", "6"),
    ("7", "digit_7", "7"),
]

def _is_stuck_visible(self) -> bool:
    hint = self.query_one("#stuck-hint", StuckHintWidget)
    return bool(hint.display)

def _pin_index(self, idx: int) -> None:
    if 1 <= idx <= len(SIDEBAR_ROLE_ORDER):
        self.query_one("#stream-panel", RoleStreamPanel).pinned_role = (
            SIDEBAR_ROLE_ORDER[idx - 1]
        )

def _digit_action(self, idx: int) -> None:
    if self._is_stuck_visible() and 1 <= idx <= 5:
        from tero2.commands import Command
        self._command_queue.put_nowait(
            Command("steer", data={"text": f"stuck_option_{idx}"}, source="tui")
        )
        self._clear_stuck_mode()
    else:
        self._pin_index(idx)

def action_digit_1(self): self._digit_action(1)
def action_digit_2(self): self._digit_action(2)
def action_digit_3(self): self._digit_action(3)
def action_digit_4(self): self._digit_action(4)
def action_digit_5(self): self._digit_action(5)
def action_digit_6(self): self._digit_action(6)
def action_digit_7(self): self._digit_action(7)

def action_unpin(self) -> None:
    self.query_one("#stream-panel", RoleStreamPanel).pinned_role = None

def action_toggle_raw(self) -> None:
    panel = self.query_one("#stream-panel", RoleStreamPanel)
    panel.raw_mode = not panel.raw_mode

def action_clear_stream(self) -> None:
    self.query_one("#stream-panel", RoleStreamPanel).clear()
```

Remove now-obsolete `stuck_option_N` action methods. In `check_action`, drop the stuck-only gate for digits 1-5 — they're always available now; routing lives inside the action.

- [ ] **Run tests + commit**

```bash
uv run pytest tests/test_app_stream_wiring.py -v
uv run pytest tests/ -q
```

```bash
git add tero2/tui/app.py tests/test_app_stream_wiring.py
git commit -m "add stream hotkeys: v raw, 0 unpin, 1-7 pin with stuck fallback"
```

---

## Chunk 7: Runner + phases wiring + e2e (Spec Build-Order Step 7)

### Task 24: `RunnerContext.stream_bus` field

**Files:** `tero2/phases/context.py`, `tests/test_runner_context_stream.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_runner_context_stream.py
from tero2.phases.context import RunnerContext
from tero2.stream_bus import StreamBus

def test_default_stream_bus_is_none():
    ctx = RunnerContext()
    assert ctx.stream_bus is None

def test_accepts_stream_bus():
    bus = StreamBus()
    ctx = RunnerContext(stream_bus=bus)
    assert ctx.stream_bus is bus
```

- [ ] **Step 2-3:** Run → fail; add `stream_bus: StreamBus | None = None` field to the dataclass, import `StreamBus` at top.

- [ ] **Step 3-5: Run, commit**

```bash
git add tero2/phases/context.py tests/test_runner_context_stream.py
git commit -m "add stream_bus field to RunnerContext"
```

---

### Task 25: `RunnerContext.run_agent` publishes to bus

**Files:** `tero2/phases/context.py`, `tests/test_runner_context_stream.py`

Preserve ALL existing per-step stuck detection / step counting / heartbeat logic in `run_agent`. Only ADD normalization + publish per message. Fetch `get_normalizer(chain.provider_kind)` **per-message** (failover may change it). Do NOT guard with `isinstance(message, dict)` — `ZaiProvider` yields objects.

- [ ] **Step 1: Append failing tests**

```python
# tests/test_runner_context_stream.py — append
import asyncio
import pytest

from tero2.phases.context import RunnerContext
from tero2.stream_bus import StreamBus

class _FakeChain:
    def __init__(self, kind, script):
        self.kind = kind
        self._script = script
        self.providers = []
        self._current_provider_index = 0

    @property
    def provider_kind(self):
        return self.kind

    async def run_prompt(self, prompt):
        for item in self._script:
            yield item

@pytest.mark.asyncio
async def test_run_agent_publishes_to_bus():
    bus = StreamBus()
    q = bus.subscribe()
    ctx = RunnerContext(stream_bus=bus)
    chain = _FakeChain("claude", [
        {"type": "assistant",
         "message": {"content": [{"type": "text", "text": "hello"}]}},
        {"type": "result", "subtype": "success"},
    ])
    success, output = await ctx.run_agent(chain, "hi", role="executor")
    assert success
    assert "hello" in output
    kinds = []
    while not q.empty():
        kinds.append(q.get_nowait().kind)
    assert "text" in kinds
    assert "turn_end" in kinds

@pytest.mark.asyncio
async def test_run_agent_handles_non_dict_msg():
    bus = StreamBus()
    q = bus.subscribe()
    ctx = RunnerContext(stream_bus=bus)

    class _FakeMsg:
        text = "hi from zai"

    chain = _FakeChain("zai", [_FakeMsg()])
    success, output = await ctx.run_agent(chain, "hi", role="executor")
    assert not q.empty()  # at least one event published
```

- [ ] **Step 2: Refactor**

Add module-level helper:

```python
# top of tero2/phases/context.py
from datetime import datetime, timezone
from typing import Any

from tero2.providers.normalizers import get_normalizer
from tero2.stream_bus import StreamEvent

def _extract_text_from_message(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, dict):
        return message.get("text") or message.get("content") or ""
    return getattr(message, "text", None) or getattr(message, "content", None) or ""
```

Inside `RunnerContext.run_agent` — find the loop that currently iterates `chain.run_prompt(prompt_text)` and extracts text. Replace any `isinstance(message, dict)`-guarded text extraction with the helper, and ADD normalization+publish alongside the existing logic:

```python
async for message in chain.run_prompt(prompt_text):
    text_content = _extract_text_from_message(message)
    if text_content:
        captured_parts.append(text_content)

    if self.stream_bus is not None:
        normalizer = get_normalizer(chain.provider_kind)
        try:
            events = list(normalizer.normalize(message, role))
        except Exception as exc:
            events = [StreamEvent(
                role=role, kind="error",
                timestamp=datetime.now(timezone.utc),
                content=f"normalizer error: {exc}",
                raw={"raw_repr": repr(message)[:200]},
            )]
        for event in events:
            self.stream_bus.publish(event)

    # Preserve existing per-step stuck detection / heartbeat / step counting here.
```

- [ ] **Run tests + commit**

```bash
uv run pytest tests/test_runner_context_stream.py -v
uv run pytest tests/ -q
```

```bash
git add tero2/phases/context.py tests/test_runner_context_stream.py
git commit -m "publish executor-path stream events from RunnerContext.run_agent"
```

---

### Task 26: `Runner` owns `StreamBus`

**Files:** `tero2/runner.py`, `tests/test_runner_stream_bus.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_runner_stream_bus.py
from tero2.runner import Runner
from tero2.stream_bus import StreamBus

def test_runner_creates_stream_bus():
    runner = Runner()
    assert isinstance(runner.stream_bus, StreamBus)

def test_runner_context_receives_stream_bus():
    runner = Runner()
    ctx = runner._build_runner_context()
    assert ctx.stream_bus is runner.stream_bus
```

If `Runner.__init__` requires args on baseline, mirror the construction from an existing test (`grep -n 'Runner(' tests/test_runner_sora.py`).

- [ ] **Step 2-3:** Run → fail; add to `Runner`:

```python
from tero2.stream_bus import StreamBus

class Runner:
    def __init__(self, ..., stream_bus: StreamBus | None = None):
        ...  # existing init
        self._stream_bus = stream_bus or StreamBus()

    @property
    def stream_bus(self) -> StreamBus:
        return self._stream_bus

    def _build_runner_context(self) -> RunnerContext:
        ctx = RunnerContext(
            # ... all existing fields unchanged ...
            stream_bus=self._stream_bus,
        )
        return ctx
```

- [ ] **Step 3-5: Run, commit**

```bash
git add tero2/runner.py tests/test_runner_stream_bus.py
git commit -m "Runner owns StreamBus and threads it into RunnerContext"
```

---

### Task 27: 6 phase handlers pass `stream_bus=ctx.stream_bus`

**Files:** `tero2/phases/architect_phase.py:56`, `scout_phase.py:57`, `coach_phase.py:54`, `harden_phase.py:66`, `execute_phase.py:267`, `execute_phase.py:310`

Mechanical edit at each call site:
```python
player = XPlayer(chain, ctx.disk, working_dir=..., stream_bus=ctx.stream_bus)
```

- [ ] **Step 1:** Apply 6 edits.
- [ ] **Run tests + commit**

```bash
git add tero2/phases/architect_phase.py tero2/phases/scout_phase.py tero2/phases/coach_phase.py tero2/phases/harden_phase.py tero2/phases/execute_phase.py
git commit -m "pass RunnerContext.stream_bus to players from phase handlers"
```

---

### Task 28: `cli.py` passes `runner.stream_bus` to `DashboardApp`

**Files:** `tero2/cli.py`

- [ ] **Step 1:** Find `DashboardApp(...)` call site; add `stream_bus=runner.stream_bus`.
- [ ] **Run tests + commit**

```bash
git add tero2/cli.py
git commit -m "pass runner.stream_bus into DashboardApp from cli"
```

---

### Task 29: End-to-end smoke test + manual run

**Files:** `tests/test_e2e_stream_flow.py`

- [ ] **Step 1: Write test**

```python
# tests/test_e2e_stream_flow.py
"""Smoke: fake scripted runner publishes normalized events through the real
StreamBus; TUI routes them to stream panel, heartbeat, and status log."""

from __future__ import annotations

import asyncio
import pytest

from tero2.events import Event, EventDispatcher
from tero2.stream_bus import StreamBus
from tero2.tui.app import DashboardApp

SCRIPT = [
    {"type": "system", "subtype": "init", "tools": [{}, {}, {}]},
    {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "starting build"},
        {"type": "tool_use", "id": "t1", "name": "Read",
         "input": {"path": "main.py"}},
    ]}},
    {"type": "user", "message": {"content": [
        {"type": "tool_result", "tool_use_id": "t1", "content": "print('hi')"},
    ]}},
    {"type": "result", "subtype": "success"},
]

class _ScriptRunner:
    def __init__(self, bus, dispatcher):
        self.stream_bus = bus
        self._dispatcher = dispatcher

    async def run(self):
        from tero2.providers.normalizers import get_normalizer
        norm = get_normalizer("claude")
        self._dispatcher.publish(Event(
            kind="phase_change", data={"role": "builder", "phase": "build"}))
        for raw in SCRIPT:
            for ev in norm.normalize(raw, role="builder"):
                self.stream_bus.publish(ev)
            await asyncio.sleep(0)
        self._dispatcher.publish(Event(kind="done", data={"role": "builder"}))

@pytest.mark.asyncio
async def test_e2e_tool_calls_visible():
    bus = StreamBus()
    dispatcher = EventDispatcher()
    cq = asyncio.Queue()
    runner = _ScriptRunner(bus, dispatcher)
    app = DashboardApp(runner=runner, dispatcher=dispatcher,
                       command_queue=cq, stream_bus=bus)

    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        panel = app.query_one("#stream-panel")
        hb = app.query_one("#heartbeat")
        status = app.query_one("#status-log")

        buf = panel._buffers.get("builder", [])
        kinds = {e.kind for e in buf}
        assert "text" in kinds
        assert "tool_use" in kinds
        assert "tool_result" in kinds

        assert hb.metrics["builder"].tool_count >= 1
        assert hb.metrics["builder"].status in {"running", "done"}

        rendered = "\n".join(str(l) for l in status.lines)
        assert "phase_change" in rendered or "build" in rendered
```

- [ ] **Step 2:** Run → pass.

```bash
uv run pytest tests/test_e2e_stream_flow.py -v
```

- [ ] **Step 3: Manual smoke** — run the real program against a scratch project:

```bash
uv run tero2 go
```

Confirm:
- [ ] New TUI layout appears with 7 heartbeat cells.
- [ ] When an agent runs, main panel fills with text + tool calls in real time.
- [ ] `v` toggles raw mode (full tool_output + thinking).
- [ ] `1-7` pin roles; `0` unpins.
- [ ] `stuck` mode still works: pressing `1-5` when stuck hint visible sends steer command.
- [ ] Phase transitions appear in the compact status log.

- [ ] **Run tests + commit**

```bash
git add tests/test_e2e_stream_flow.py
git commit -m "add end-to-end smoke test for live stream flow"
```

---

## Closing

After Task 29:
- [ ] All tests green.
- [ ] Manual smoke verified in TUI.
- [ ] 7 spec Build-Order steps committed in order; each commit leaves the tree working.

Open Questions (spec §Open Questions):
- [ ] `LogView` stays on disk; separate cleanup PR after 2-3 weeks of shakedown.
- [ ] Heartbeat responsive layout: out of scope.
- [ ] Fixture refresh: manual when CLI output format changes; log under `bugs.md`.

Use superpowers:finishing-a-development-branch when ready to merge.

**Deferred to v2 (do NOT extend this plan):**
- [ ] Click-to-expand individual events
- [ ] Filter by kind (`/`)
- [ ] Scroll-lock / pause autoscroll
- [ ] Search (Ctrl+F) in main panel
- [ ] Export stream to file
- [ ] Visual `tool_use ↔ tool_result` correlation card
- [ ] Telegram subscription to StreamBus
- [ ] Responsive layout for terminals <100 cols
