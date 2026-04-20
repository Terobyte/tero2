# Live Agent Stream — Design Spec

**Статус:** design, ready for implementation planning
**Дата:** 2026-04-20
**Автор:** Claude Code (brainstorming session с пользователем)

---

## Prerequisites / Baseline

Применяется к текущему `main` HEAD (commit `543bbfd` на момент написания). Все ссылки на файлы (`tero2/events.py`, `tero2/providers/cli.py`, `tero2/tui/app.py`, `tero2/players/base.py`, и т.д.) валидны на `main`.

Поверх `main` уже есть неcommit-нутые правки (см. `git status` — 20+ файлов M), но спек пишется **от main**. Если конфликты возникнут при реализации — разрешаются в пользу спеки.

---

## Summary

В tero2 сейчас непонятно, работает ли одна из 7 ролей SORA (`architect`, `scout`, `builder`, `coach`, `reviewer`, `verifier`, `executor`) или застряла. TUI показывает только coarse orchestration-события (`phase_change`, `step`, `done`), но **не содержимое** работы агента — какие tools вызваны, какие тексты сказаны, о чём «думает». Это происходит потому что `CLIProvider` трижды буферизует stdout: собирает все строки в список → yield'ит после завершения процесса → `ProviderChain` буферизует ещё раз → `run_prompt_collected` склеивает всё в одну строку. Stream агента есть (stream-json от CLI), но до TUI не доходит.

**Решение:** ломаем буферизацию, вводим **отдельный канал** `StreamBus` (параллельно существующему `EventDispatcher`) для stream-контента, добавляем **per-provider normalizer'ы** для 5 провайдеров (`claude`, `codex`, `opencode`, `kilo`, `zai`), переделываем TUI на **main-panel + sidebar-heartbeat** раскладку.

**Пользовательская боль (дословно):** «непонятно работает ли один из 7 ролей или нет. не видно прогресс» → «полностью вставить логи от работы агента в окно программы. какие tools какие мысли».

**Scope:**
- Новый модуль `tero2/stream_bus.py` (`StreamEvent` dataclass + `StreamBus` fan-out dispatcher)
- Новый пакет `tero2/providers/normalizers/` (5 normalizer-функций + golden fixtures)
- Рефактор `tero2/providers/cli.py` (убрать буфер, стримить yield'ами)
- Рефактор `tero2/providers/chain.py` (добавить `current_provider_name` property, ограничить retry-политику)
- Рефактор `tero2/players/base.py` (`_run_prompt` стрим-aware через normalizer → bus)
- Новые TUI-виджеты: `stream_panel.py`, `heartbeat_sidebar.py`, `stream_event_formatter.py`, `status_log.py`
- Изменение раскладки `tero2/tui/app.py` + обновление хоткеев
- Runner создаёт `StreamBus` и пробрасывает в players

**Out of scope (явно отложено):**
- Click-to-expand отдельных событий (MVP: global hotkey `v` для verbose/raw)
- Фильтр по kind'у (`/`)
- Scroll-lock / пауза автоскролла
- Поиск Ctrl+F в main panel
- Экспорт stream'а в файл
- Визуальный матчинг `tool_use ↔ tool_result` в одну карточку
- Telegram-подписка на StreamBus
- Responsive layout для узких терминалов (<100 cols)

---

## Decisions Log

Ключевые решения, принятые в брейнсторм-сессии (для audit trail):

| # | Вопрос                                         | Решение | Почему                                                   |
|---|-----------------------------------------------|---------|----------------------------------------------------------|
| 1 | Раскладка окна                                | **B**: main-panel + sidebar + usage-panel | SORA выполняет 1-2 роли одновременно (builder + async coach) → per-role grid избыточен, tabs требуют ручного переключения |
| 2 | Глубина контента в main-панели                | **B**: tools + args + truncated outputs + text + thinking-бадж | Минимум теряет debug-инфо; raw забивает экран. Toggle `v` в raw доступен |
| 3 | Транспорт от CLI до TUI                       | **B**: отдельная `StreamBus` рядом с `EventDispatcher` | Расширение EventDispatcher заспамило бы Telegram; прямая очередь ломает изоляцию Runner↔TUI |
| 4 | Содержимое sidebar + логика фокуса            | **B**: vital signs (tools, elapsed, model) + last line + auto-switch с pin 1-7 | Метрика `18 tools / 2m14s` даёт мгновенный ответ «работает или завис». Pin решает дерганье при двух активных ролях |
| 5 | Truncation: в normalizer или в formatter      | **Formatter**, normalizer хранит всё | Чтобы toggle `v` (raw) мог показать всё без новых запросов |
| 6 | Все 5 normalizer'ов в MVP                     | **Да** (claude, codex, opencode, kilo, zai) | Пользователь: «нужно сразу всех чтобы из коробки работало» |
| 7 | `tool_id` matching (tool_use↔tool_result)     | **В MVP** | Без него orphan-detection (скрытый hang) не работает |
| 8 | Retry policy после рефактора                  | **Retry работает только до первого yield**; после первого yield ошибка = hard fail | Stream нельзя «un-yield». Rate-limit в реальности приходит первой строкой |
| 9 | `StreamBus.publish()` sync vs async           | **Sync** | Безопасно вызывать в tight loop (`async for raw in chain`) без penalty |
| 10 | Ring-buffer size                             | **2000 событий per subscriber**, drop-oldest | EventDispatcher (500, priority-drop) рассчитан на орchestration events; stream — другой профиль volume |

---

## Problem Statement

### Текущая архитектура

```
Player.run()
  ↓
chain.run_prompt_collected(prompt)           [БУФЕР 3: склеивает в string]
  ↓
chain.run_prompt() → ProviderChain.run()     [БУФЕР 2: messages: list[Any]]
  ↓
provider.run() — CLIProvider                  [БУФЕР 1: lines: list[str]]
  ↓
async for line in proc.stdout:
    lines.append(line)                        ← собирает ВСЁ в память
# wait proc, check returncode
for raw_line in lines:
    yield parsed                              ← yields ПОСЛЕ process exit

EventDispatcher (отдельно):
Runner emits coarse events → TUI.LogView
```

**Следствие:** пользователь в TUI видит только:
- `phase_change` / `step` (роль сменилась, но что делает — неизвестно)
- `done` / `error` (финальный сигнал, постфактум)

**Не видит:**
- какие tools агент вызвал
- какие тексты сказал
- о чём думает
- зависла ли роль (нет heartbeat'а)

### Целевая архитектура

```
Player.run()
  ↓
_run_prompt(prompt) ────────────────────┐
  ↓                                     │ publish StreamEvent
chain.run_prompt() — async generator    │
  ↓                                     ▼
provider.run() — CLIProvider        StreamBus ──► TUI
  ↓                                  (fan-out:
async for line in proc.stdout:        main +
    yield parsed               ◄──    sidebar +
  (сразу по приходу)                    raw)
                                    
EventDispatcher (без изменений):
Runner emits coarse events → TUI.StatusLog + Telegram
```

---

## Architecture

### Component Map

```
tero2/
├── stream_bus.py                   [NEW] StreamEvent + StreamBus
├── events.py                       (existing, без изменений)
├── providers/
│   ├── base.py                     (existing)
│   ├── cli.py                      [MOD] убрать буфер
│   ├── chain.py                    [MOD] + current_provider_name, retry policy
│   ├── shell.py, zai.py, catalog.py, registry.py
│   └── normalizers/                [NEW package]
│       ├── __init__.py             [NEW] get_normalizer(provider_name) dispatcher
│       ├── base.py                 [NEW] StreamNormalizer Protocol
│       ├── claude.py               [NEW] parses stream-json
│       ├── codex.py                [NEW]
│       ├── opencode.py             [NEW]
│       ├── kilo.py                 [NEW]
│       └── zai.py                  [NEW] Anthropic API format
├── players/
│   ├── base.py                     [MOD] _run_prompt stream-aware
│   ├── architect.py, scout.py, builder.py, coach.py, reviewer.py, verifier.py
│   │                               (без изменений — используют BasePlayer)
├── runner.py                       [MOD] создаёт StreamBus, пробрасывает в players
├── cli.py                          [MOD] wires Runner → DashboardApp (передать bus)
└── tui/
    ├── app.py                      [MOD] new compose + hotkeys + pin/auto-switch
    ├── widgets/
    │   ├── stream_panel.py          [NEW] RoleStreamPanel (main content)
    │   ├── heartbeat_sidebar.py     [NEW] HeartbeatSidebar (7 mini-cells)
    │   ├── stream_event_formatter.py [NEW] format(StreamEvent) → rich.Text
    │   ├── status_log.py            [NEW] compact 4-line event log
    │   ├── log_view.py              [DEPRECATE] оставляем файл но не используем в app.py
    │   ├── pipeline.py, usage.py, stuck_hint.py (unchanged)

tests/
├── test_stream_bus.py               [NEW]
├── test_stream_event_formatter.py   [NEW]
├── test_heartbeat_sidebar.py        [NEW]
├── test_stream_panel.py             [NEW]
├── test_status_log.py               [NEW]
├── test_cli_provider_streaming.py   [NEW] timing-based
├── test_chain_retry_policy.py       [NEW]
├── test_player_stream_integration.py [NEW]
├── normalizers/                     [NEW]
│   ├── test_claude.py, test_codex.py, test_opencode.py, test_kilo.py, test_zai.py
│   └── fixtures/
│       ├── claude.jsonl, claude_rate_limit.jsonl
│       ├── codex.jsonl, codex_tool_error.jsonl
│       ├── opencode.jsonl, opencode_unknown_model.jsonl
│       ├── kilo.jsonl
│       └── zai.jsonl
└── test_e2e_stream_flow.py          [NEW] end-to-end smoke
```

### Data Model

```python
# tero2/stream_bus.py

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

@dataclass
class StreamEvent:
    """Normalized stream event from an agent CLI.

    Produced by per-provider normalizers, published via StreamBus,
    consumed by TUI widgets (RoleStreamPanel, HeartbeatSidebar).
    """
    role: str                       # "builder", "scout", ..., "" for system
    kind: Literal[
        "text",                     # agent's narration
        "tool_use",                 # tool invocation
        "tool_result",              # tool result
        "thinking",                 # chain-of-thought
        "status",                   # start/end/turn_boundary
        "error",                    # stream or parse error
    ]
    timestamp: datetime             # UTC
    content: str = ""               # for text/thinking/status/error
    tool_name: str = ""             # for tool_use/tool_result
    tool_args: dict = field(default_factory=dict)   # tool_use input
    tool_output: str = ""           # for tool_result (FULL, no truncation)
    tool_id: str = ""               # matching tool_use ↔ tool_result
    raw: dict = field(default_factory=dict)  # original provider dict


def make_stream_event(...) -> StreamEvent:
    """Factory with `datetime.now(timezone.utc)` default."""
```

### StreamBus

```python
# tero2/stream_bus.py

class StreamBus:
    """Fan-out dispatcher for agent stream content.

    Parallel to EventDispatcher but tuned for higher volume:
    - maxsize=2000 per subscriber
    - ring-buffer semantics (drop oldest on full)
    - no priority logic (all events equal)
    - publish() is SYNC (safe in tight loop)
    """

    def __init__(self, max_queue_size: int = 2000):
        self._subscribers: list[asyncio.Queue[StreamEvent]] = []
        self._max = max_queue_size

    def subscribe(self) -> asyncio.Queue[StreamEvent]:
        q = asyncio.Queue(maxsize=self._max)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, event: StreamEvent) -> None:
        for q in self._subscribers:
            if q.full():
                try:
                    q.get_nowait()          # drop oldest
                except asyncio.QueueEmpty:
                    pass
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
```

### Normalizer Contract

```python
# tero2/providers/normalizers/base.py

from typing import Protocol, Callable, Iterable
from datetime import datetime

class StreamNormalizer(Protocol):
    def normalize(
        self,
        raw: dict | str,
        role: str,
        now: Callable[[], datetime] = ...,
    ) -> Iterable[StreamEvent]:
        """Convert one raw provider output into zero or more StreamEvents.

        Rules:
        - One raw may produce multiple events (e.g. message with text + tool_use)
        - Empty iterable for irrelevant lines (metadata, heartbeats)
        - On parse failure: yield ONE StreamEvent(kind="error", content=<reason>, raw=<dict>)
        - Pure function — no I/O, no global state, no mutation
        """
```

**Per-provider normalizers:**

- `claude.py` — parses stream-json: `{"type":"assistant","message":{"content":[{type:text|tool_use|thinking,...}]}}`, `{"type":"user","message":{"content":[{type:tool_result,...}]}}`, system blocks
- `codex.py` — codex `--json` format
- `opencode.py` — opencode `--format json` format
- `kilo.py` — kilo `--format json` format
- `zai.py` — Anthropic API message format (already streaming in zai provider)

Dispatcher:

```python
# tero2/providers/normalizers/__init__.py

_NORMALIZERS: dict[str, StreamNormalizer] = {
    "claude": ClaudeNormalizer(),
    "codex": CodexNormalizer(),
    "opencode": OpenCodeNormalizer(),
    "kilo": KiloNormalizer(),
    "zai": ZaiNormalizer(),
}

_FALLBACK = FallbackNormalizer()  # emits kind="status" with content=str(raw)

def get_normalizer(provider_name: str) -> StreamNormalizer:
    return _NORMALIZERS.get(provider_name, _FALLBACK)
```

---

## Backend Changes

### `tero2/providers/cli.py` — break buffering

**Current (lines 189-230):**
```python
lines: list[str] = []
async for line in proc.stdout:
    lines.append(line.decode(errors="replace"))

# ... wait proc, check returncode ...

for raw_line in lines:
    yield parsed
```

**New:**
```python
async for line in proc.stdout:
    stripped = line.decode(errors="replace").strip()
    if not stripped:
        continue
    try:
        parsed = json.loads(stripped)
        yield parsed if isinstance(parsed, dict) else {"type":"text","text":stripped}
    except json.JSONDecodeError:
        yield {"type":"text","text":stripped}

# wait proc AFTER stream ends
rc = await proc.wait()
if rc != 0:
    raise ProviderError(...)
yield {"type":"turn_end","text":""}
```

**Key change:** yield happens inside the streaming loop, not after `proc.wait()`. stderr task remains.

### `tero2/providers/chain.py` — retry policy + current_provider_name

1. Add `@property current_provider_name(self) -> str: return self._providers[self._current_provider_index].display_name`

2. Retry logic:
    - Before first yield: keep current buffered behavior (collect → check for stream-level errors → yield all). Allows retry on early errors.
    - After first yield: pass-through. If error happens mid-stream, propagate without retry on same provider (failover to next provider in chain still works as before IF nothing yet yielded; otherwise hard fail).

Implementation sketch:
```python
async def run(self, **kwargs):
    for idx, provider in enumerate(self._providers):
        if not cb.is_available: continue
        self._current_provider_index = idx

        for attempt in range(self._rate_limit_max_retries + 1):
            # backoff sleep
            yielded_anything = False
            try:
                async for msg in provider.run(**kwargs):
                    # Check for stream-level error ONLY if nothing yielded yet
                    if not yielded_anything and isinstance(msg, dict) and msg.get("type") == "error":
                        raise ProviderError(...)
                    yielded_anything = True
                    yield msg
                cb.record_success()
                return
            except Exception as exc:
                if not _is_recoverable_error(exc):
                    raise
                if yielded_anything:
                    raise   # can't retry after stream started
                # else: continue inner loop (retry)
```

### `tero2/players/base.py` — stream-aware `_run_prompt`

```python
class BasePlayer(ABC):
    def __init__(self, chain, disk, *, working_dir="", stream_bus=None):
        self.chain = chain
        self.disk = disk
        self.working_dir = working_dir
        self._stream_bus = stream_bus   # optional; tests pass None

    async def _run_prompt(self, prompt: str) -> str:
        text_parts: list[str] = []
        async for raw in self.chain.run_prompt(prompt):
            provider_name = self.chain.current_provider_name
            normalizer = get_normalizer(provider_name)
            for event in normalizer.normalize(raw, self.role):
                if self._stream_bus is not None:
                    self._stream_bus.publish(event)
                if event.kind == "text":
                    text_parts.append(event.content)
        return "\n".join(text_parts)
```

### `tero2/runner.py` — create StreamBus

```python
class Runner:
    def __init__(self, ..., stream_bus: StreamBus | None = None):
        ...
        self._stream_bus = stream_bus or StreamBus()

    @property
    def stream_bus(self) -> StreamBus:
        return self._stream_bus

    def _build_players(self):
        return {
            "architect": ArchitectPlayer(chain, disk, stream_bus=self._stream_bus, ...),
            "builder":   BuilderPlayer(chain, disk, stream_bus=self._stream_bus, ...),
            "scout":     ScoutPlayer(chain, disk, stream_bus=self._stream_bus, ...),
            "coach":     CoachPlayer(chain, disk, stream_bus=self._stream_bus, ...),
            "verifier":  VerifierPlayer(chain, disk, stream_bus=self._stream_bus, ...),
            "reviewer":  ReviewerPlayer(chain, disk, stream_bus=self._stream_bus, ...),
            # executor — TBD, depends on current implementation
        }
```

### `tero2/cli.py` — wire bus into DashboardApp

```python
runner = Runner(...)
dispatcher = EventDispatcher()
command_queue = asyncio.Queue()
app = DashboardApp(
    runner=runner,
    dispatcher=dispatcher,
    command_queue=command_queue,
    stream_bus=runner.stream_bus,        # NEW
)
```

---

## TUI Changes

### Layout (`tero2/tui/app.py`)

```
Header
├── PipelinePanel (unchanged, narrow)
├── Horizontal "main-row":
│   ├── RoleStreamPanel         [NEW]   (3fr)
│   ├── HeartbeatSidebar        [NEW]   (width=26, fixed)
│   └── UsagePanel              (existing, 1fr, compact)
├── StatusLog                   [NEW]   (4 lines, compact, bottom)
├── StuckHintWidget             (existing, hidden by default)
└── Footer
```

`LogView` (существующий) больше не используется в `app.py`. Файл оставляем для возможного будущего reuse или удаления в отдельном commit'е — не трогаем в MVP чтобы минимизировать blast radius.

### Widgets

**`RoleStreamPanel`** (`tui/widgets/stream_panel.py`):
- Owns `active_role` reactive property (str). Default: auto-switch by priority.
- Owns `pinned_role` reactive property (str | None). When set, `active_role` doesn't auto-switch.
- Owns `raw_mode` reactive property (bool). Toggles via `v` hotkey.
- Internal `RoleBufferManager`: `dict[str, deque[StreamEvent]]` (maxlen=500 per role).
- Subscribes to `StreamBus`; on event: append to per-role buffer; refresh render if `event.role == self.active_role`.
- Render: iterate events of active role through `stream_event_formatter.format(event, raw_mode=self.raw_mode)`.
- Header line: `● <role> · <elapsed> [pinned: <role>]  v: raw-mode`

**`HeartbeatSidebar`** (`tui/widgets/heartbeat_sidebar.py`):
- Renders 7 mini-cells vertically.
- Per-role state aggregator (`RoleMetrics` dataclass: `status`, `elapsed_s`, `tool_count`, `last_line`, `provider`, `model`).
- Subscribes to `StreamBus` + `EventDispatcher` (for phase events → `done`/`running` status).
- Active-cell highlight: border `2px solid $accent` + background tint.
- Status dot: 🟢 running, 🟡 async, ⚪ idle, 🔴 error, ✓ done.
- On click: emit message for `DashboardApp` to pin that role.
- Role order (fixed): `scout, architect, builder, coach, verifier, reviewer, executor`. Hotkeys 1-7 map to this order.

**`StreamEventFormatter`** (`tui/widgets/stream_event_formatter.py`):
- Pure function: `format(event: StreamEvent, *, raw_mode: bool = False) -> rich.Text`.
- Truncation rules (when `raw_mode=False`):
  - `tool_output`: first 2 lines + `… +<N> bytes` suffix if >2 lines
  - `thinking`: collapse to `💭 thinking… (<N> chars)`
  - `text`: no truncation
- Colors per `kind`:
  - `text`: yellow
  - `tool_use`: green (tool name bold)
  - `tool_result`: dim white (output indented)
  - `thinking`: dim grey
  - `status`: cyan
  - `error`: red bold
- Role colors (same as `log_view.py`): scout=cyan, architect=blue, builder=green, coach=yellow, verifier=magenta, reviewer=purple, executor=white

**`StatusLog`** (`tui/widgets/status_log.py`):
- `RichLog(max_lines=4)` (or similar small fixed size).
- Subscribes to `EventDispatcher` (NOT `StreamBus`).
- Renders only: `phase_change`, `stuck`, `done`, `error`, `escalation`, `provider_switch`.
- Fills the role of current `LogView` for the orchestration events.

### Auto-switch + Pin Logic

**Active-role tracking** (in `RoleStreamPanel` or separate `ActiveRoleTracker`):
1. Every event updates `last_seen_at[role] = now()`.
2. "Active" = `now() - last_seen_at[role] < 5s`.
3. Priority order (hardcoded): `builder > verifier > architect > scout > reviewer > coach > executor`.
4. If no role is active → `active_role` = last seen role.
5. If `pinned_role` is set → ignore auto-switch, show pinned role.

**Pin hotkeys** (1-7):
- Only active when NOT in stuck mode. `DashboardApp.check_action` already gates 1-5 for stuck options; extend to 1-7 for non-stuck mode.
- Mapping (fixed by role order in sidebar): 1=scout, 2=architect, 3=builder, 4=coach, 5=verifier, 6=reviewer, 7=executor.
- `0` = unpin (resume auto-switch).

### Hotkeys (BINDINGS)

Add:
```python
("v", "toggle_raw", "Raw"),
("c", "clear_stream", "Очистить"),
("0", "unpin_role", "Unpin"),
# 1-7 shared with stuck_option_N — gated by check_action
("6", "stuck_option_6", "6"),   # only if ever needed; currently stuck has only 1-5
("7", "stuck_option_7", "7"),   # placeholder
```

In `check_action`:
- If stuck → 1-5 = stuck options (unchanged).
- If NOT stuck → 1-7 = pin role.

Current stuck options 1-5 и новые 6-7 для pin — через один набор bindings, но разный поведенческий путь в зависимости от `stuck_hint.display`.

### `DashboardApp` wiring

```python
def __init__(self, runner, dispatcher, command_queue, stream_bus):
    super().__init__()
    self._runner = runner
    self._dispatcher = dispatcher
    self._stream_bus = stream_bus
    self._command_queue = command_queue
    self._event_queue = None
    self._stream_queue = None
    self._runner_worker = None

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

def on_mount(self):
    self._event_queue = self._dispatcher.subscribe()
    self._stream_queue = self._stream_bus.subscribe()
    self._runner_worker = self.run_worker(self._run_runner(), exclusive=True)
    self.run_worker(self._consume_events(), exclusive=False)
    self.run_worker(self._consume_stream(), exclusive=False)

async def _consume_stream(self):
    """Drain StreamBus; route to stream_panel + heartbeat."""
    if self._stream_queue is None:
        return
    stream_panel = self.query_one("#stream-panel", RoleStreamPanel)
    heartbeat = self.query_one("#heartbeat", HeartbeatSidebar)
    while True:
        event = await self._stream_queue.get()
        stream_panel.on_stream_event(event)
        heartbeat.on_stream_event(event)
```

---

## Error Handling

| Failure                                     | Behavior                                                              |
|---------------------------------------------|-----------------------------------------------------------------------|
| Normalizer `JSONDecodeError`                | Normalizer yields `StreamEvent(kind="error", content="parse: <msg>", raw=...)` → renders red in main |
| Normalizer unknown event structure          | `FallbackNormalizer` → yields `kind="status", content="raw: <dict>"`  |
| Provider process crash mid-stream           | Exception in `CLIProvider.run()` → propagates through chain → caught by player → `PlayerResult(success=False)`. Events emitted before crash stay in bus (visible as history). |
| Provider rate-limit (early)                 | Caught by chain retry loop (only before first yield). Transparent.   |
| Provider rate-limit (mid-stream)            | Not retryable — hard fail. Player fails, upstream sees error.         |
| StreamBus subscriber dies (CancelledError)  | TUI `on_unmount` unsubscribes. Dead subscribers silently dropped in `publish()` |
| Orphan `tool_use` (no matching `tool_result`)| Not fatal. Can be detected by UI (tool_id without result) — shown as `tool_use · waiting…` badge in future v2. MVP: just shown as 2 separate lines. |

---

## Testing Strategy

### Unit (no TUI, no async subprocess)

- **`test_stream_bus.py`**: subscribe, unsubscribe, publish to multiple subscribers, ring-buffer drop-oldest at 2000, dead subscriber tolerance
- **`normalizers/test_*.py`** (5 files): golden-file tests. `normalize(line_from_fixture, role="builder")` → expected list of StreamEvents. Fixtures cover: text, tool_use, tool_result, thinking, errors, orphaned tool_use, multi-block assistant messages
- **`test_stream_event_formatter.py`**: pure function tests. Truncation rules, color mapping, raw-mode on/off
- **`test_chain_retry_policy.py`**: fake provider, assert retry before first yield works, retry after first yield raises

### Widget snapshot tests (Textual Pilot)

- **`test_heartbeat_sidebar.py`**: feed sequence of StreamEvents, assert per-role state rendered correctly (status dot, metrics, last line)
- **`test_stream_panel.py`**: feed events for role=builder, switch active to role=scout, assert only scout events shown
- **`test_status_log.py`**: feed Event dispatcher events, assert only high-signal kinds rendered

### Integration (fake subprocess)

- **`test_cli_provider_streaming.py`**: mock `asyncio.create_subprocess_exec` to yield scripted stdout with delays. Assert `yield` happens BEFORE `proc.wait()` completes (timing assertion using timestamps)
- **`test_player_stream_integration.py`**: fake chain yielding scripted dicts, fake bus collecting events. Assert bus receives normalized events AND player's returned string contains only text blocks

### End-to-end

- **`test_e2e_stream_flow.py`**: minimal Runner with fake CLI yielding scripted JSONL, real StreamBus + Dispatcher + DashboardApp (via Textual Pilot), assert main panel shows tool calls, sidebar shows running state for active role, status log shows phase_change

### Fixtures collection (one-off)

Before implementing normalizers: run each CLI on a simple scripted prompt (`"read README.md and summarize"`) with stdout capture. Save raw stdout to `tests/fixtures/stream_samples/<provider>.jsonl`. Review manually to ensure coverage of: text block, tool_use, tool_result, thinking (if supported), turn_end.

Negative fixtures (each): trigger rate-limit (bad api key), trigger tool error (ask to read nonexistent file), trigger unknown model (bad model name).

---

## Build Order

Each step = one commit, each commit leaves the system in working state.

### Step 1 — `StreamEvent` + `StreamBus` + unit tests
- Files: `tero2/stream_bus.py`, `tests/test_stream_bus.py`
- No callers yet. StreamBus exists in isolation.
- Verify: `pytest tests/test_stream_bus.py` green.

### Step 2 — Normalizers + golden fixtures
- Files: `tero2/providers/normalizers/{__init__.py,base.py,claude.py,codex.py,opencode.py,kilo.py,zai.py}`, fixtures in `tests/normalizers/fixtures/`, `tests/normalizers/test_*.py`
- Fixtures collected one-off (manual). Committed as part of this step.
- Normalizers not yet wired to anything.
- Verify: `pytest tests/normalizers/` green.

### Step 3 — `CLIProvider` streaming refactor
- Files: `tero2/providers/cli.py`, `tests/test_cli_provider_streaming.py`
- Break triple buffering. Yield line-by-line.
- Existing tests (`test_cli.py` if any) should still pass — same semantic output, just streamed.
- Verify: `pytest tests/` — full green.

### Step 4 — `ProviderChain.current_provider_name` + retry policy + `BasePlayer._run_prompt` stream-aware
- Files: `tero2/providers/chain.py`, `tero2/players/base.py`, `tests/test_chain_retry_policy.py`, `tests/test_player_stream_integration.py`
- `BasePlayer` accepts `stream_bus=None` optional arg. When None, skip publish.
- Existing player tests pass without changes (bus=None).
- Verify: `pytest tests/` — full green.

### Step 5 — TUI widgets (standalone, no app wiring)
- Files: `tero2/tui/widgets/{stream_panel.py,heartbeat_sidebar.py,stream_event_formatter.py,status_log.py}`, `tests/test_stream_panel.py`, `tests/test_heartbeat_sidebar.py`, `tests/test_stream_event_formatter.py`, `tests/test_status_log.py`
- Widgets exist but not used by `app.py` yet.
- Test via Textual Pilot — feed events, assert rendered state.
- Verify: `pytest tests/` — full green.

### Step 6 — `DashboardApp` wiring + hotkeys + auto-switch/pin logic
- Files: `tero2/tui/app.py` (compose, on_mount, _consume_stream, actions, check_action), `tests/test_app_stream_wiring.py`
- Accept `stream_bus` in ctor. Replace `LogView` in compose with new widgets. Add hotkeys `v`, `c`, `0`, extend 1-7 for role pin.
- Fallback: `LogView` остаётся в файлах (не удаляем), просто не используется в compose.
- Verify: TUI запускается, widgets видны (manual smoke), `pytest tests/` green.

### Step 7 — `Runner` creates StreamBus + `cli.py` wires to app
- Files: `tero2/runner.py`, `tero2/cli.py`, `tests/test_e2e_stream_flow.py`
- Runner instantiates StreamBus, passes to players.
- `cli.py` grabs `runner.stream_bus`, passes to DashboardApp.
- End-to-end smoke test: fake CLI → runner → UI shows tool calls in real-time.
- Verify: ручной smoke run, `pytest tests/test_e2e_stream_flow.py` green.

---

## Open Questions

(To be resolved during implementation or surfaced via spec-review loop)

1. **`LogView` deprecation path** — удалить в отдельном PR после shakedown или оставить надолго? MVP: не удаляем.
2. **Heartbeat sidebar на узком экране** — сейчас assume wide. В `on_resize` можно скрывать sidebar и переключаться на tabs-mode — но это out-of-scope v1.
3. **Executor role status** — сейчас `executor` в config есть, но player отдельный не виден в `players/`. Возможно нужен placeholder normalizer path или role вообще не streamed (ручная команда shell). Уточнить при реализации шага 7.
4. **Telegram subscription к StreamBus** — в теории Telegram может опционально получать highlights (текстовые блоки). Явно out-of-scope MVP, но структурой bus это позволяется через `subscribe()`.
5. **Fixtures refresh policy** — когда CLI обновит stream-json формат, fixtures устареют. Нужен ли скрипт для автообновления? MVP: manual, заносим в bugs.md если normalizer перестал работать.

---

## Rollout / Deployment

- **Единственный user — локальный pip install.** Нет remote rollout, нет feature flag.
- **Migration:** нет. Пользователь запускает `tero2 go` — получает новый UI. Откат — git revert.
- **Visible change at merge:** пользователь открывает TUI и видит новую раскладку с heartbeat-sidebar'ом и live-stream'ом.

---

## Success Criteria

- ✅ Пользователь открывает TUI, видит по каждой из 7 ролей: статус, elapsed, tool count, last line.
- ✅ Когда builder работает — в main-panel сыплются tool calls и text по мере их появления (не после завершения).
- ✅ Hotkey `1-7` — переключает фокус main-panel на pinned роль. `0` — auto-switch.
- ✅ Hotkey `v` — показывает полный thinking и необрезанный tool_output.
- ✅ При crash provider'а (rate-limit, invalid model) — ошибка видна в stream'е + EventDispatcher отправляет `error` event.
- ✅ Telegram по-прежнему получает только phase/stuck/done/error (не спамится tools).
- ✅ Все тесты зелёные. E2E smoke проходит.

---

*Авторство: brainstorming session 2026-04-20 между пользователем (Temirlan) и Claude Code.*
