# TUI Redesign M1 — MVP Wizard Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** `tero2 go` without args opens a startup wizard (project pick → plan pick) → launches DashboardApp. `tero2 go <path>` continues to work. ControlsPanel replaced with StuckHintWidget.

**Architecture:** New modules: `tero2/history.py`, `tero2/tui/widgets/stuck_hint.py`, `tero2/tui/screens/startup_wizard.py`, `tero2/tui/screens/project_pick.py`, `tero2/tui/screens/plan_pick.py`. Modified: `tero2/cli.py`, `tero2/tui/app.py`, `tero2/tui/styles.tcss`, `tero2/constants.py`, `tero2/players/scout.py`. Deleted: `tero2/tui/widgets/controls.py`.

**Tech Stack:** Python 3.11+, Textual ≥1.0, pytest, pytest-textual-snapshot

---

## Chunk 1: Foundation (constants, history, stuck widget)

### Task 1: Move _SKIP_DIRS to constants.py

**Files:**
- [x] Modify: `tero2/constants.py`
- [x] Modify: `tero2/players/scout.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_constants.py
from tero2.constants import PROJECT_SCAN_SKIP_DIRS

def test_skip_dirs_is_frozenset():
    assert isinstance(PROJECT_SCAN_SKIP_DIRS, frozenset)

def test_skip_dirs_contains_expected():
    for d in (".git", ".venv", "node_modules", "__pycache__", "dist"):
        assert d in PROJECT_SCAN_SKIP_DIRS
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_constants.py -v
```
Expected: `ImportError: cannot import name 'PROJECT_SCAN_SKIP_DIRS'`

- [x] **Step 3: Add PROJECT_SCAN_SKIP_DIRS to constants.py**

Append to the end of `tero2/constants.py`:
```python
PROJECT_SCAN_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "dist",
})
```

- [x] **Step 4: Update scout.py to import from constants**

In `tero2/players/scout.py`, find `_SKIP_DIRS` (lines ~177-188) and replace the local definition with:
```python
from tero2.constants import PROJECT_SCAN_SKIP_DIRS as _SKIP_DIRS
```
Remove the old `_SKIP_DIRS = {...}` block entirely.

- [x] **Step 5: Run tests to verify pass**

```
pytest tests/test_constants.py tests/test_players.py -v
```
Expected: all PASS

- [x] **Step 6: Commit**

```bash
git add tero2/constants.py tero2/players/scout.py tests/test_constants.py
git commit -m "move _SKIP_DIRS to constants.PROJECT_SCAN_SKIP_DIRS"
```

---

### Task 2: tero2/history.py — project run history

**Files:**
- [x] Create: `tero2/history.py`
- [x] Create: `tests/test_history.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_history.py
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tero2.history import HistoryEntry, load_history, record_run, trim_history


def test_load_history_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr("tero2.history.HISTORY_FILE", tmp_path / "history.json")
    result = load_history()
    assert result == []


def test_record_run_creates_entry(tmp_path, monkeypatch):
    monkeypatch.setattr("tero2.history.HISTORY_FILE", tmp_path / "history.json")
    project = tmp_path / "myproject"
    project.mkdir()
    record_run(project, plan_file=Path("plan.md"))
    entries = load_history()
    assert len(entries) == 1
    assert entries[0].name == "myproject"
    assert entries[0].last_plan == "plan.md"
    assert entries[0].run_count == 1


def test_record_run_increments_count(tmp_path, monkeypatch):
    monkeypatch.setattr("tero2.history.HISTORY_FILE", tmp_path / "history.json")
    project = tmp_path / "proj"
    project.mkdir()
    record_run(project, plan_file=None)
    record_run(project, plan_file=None)
    entries = load_history()
    assert entries[0].run_count == 2


def test_record_run_atomic_write(tmp_path, monkeypatch):
    """Verify .tmp file is not left behind after write."""
    monkeypatch.setattr("tero2.history.HISTORY_FILE", tmp_path / "history.json")
    project = tmp_path / "p"
    project.mkdir()
    record_run(project, plan_file=None)
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_trim_history_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr("tero2.history.HISTORY_FILE", tmp_path / "history.json")
    for i in range(25):
        p = tmp_path / f"proj{i}"
        p.mkdir()
        record_run(p, plan_file=None)
    entries = load_history()
    assert len(entries) == 20  # default max_entries
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_history.py -v
```
Expected: `ModuleNotFoundError: No module named 'tero2.history'`

- [x] **Step 3: Implement tero2/history.py**

```python
"""Project run history — reads/writes ~/.tero2/history.json."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

HISTORY_FILE = Path.home() / ".tero2" / "history.json"
_VERSION = 1


@dataclass
class HistoryEntry:
    path: str
    name: str
    last_run: str  # ISO-8601 UTC
    last_plan: str | None
    run_count: int


def load_history() -> list[HistoryEntry]:
    try:
        raw = json.loads(HISTORY_FILE.read_text())
        return [HistoryEntry(**e) for e in raw.get("entries", [])]
    except (FileNotFoundError, json.JSONDecodeError, TypeError):
        return []


def record_run(project_path: Path, plan_file: Path | None) -> None:
    entries = load_history()
    path_str = str(project_path.expanduser().resolve())
    name = project_path.name
    now = datetime.now(timezone.utc).isoformat()
    plan_str = plan_file.name if plan_file else None

    for entry in entries:
        if entry.path == path_str:
            entry.last_run = now
            entry.last_plan = plan_str
            entry.run_count += 1
            break
    else:
        entries.insert(0, HistoryEntry(
            path=path_str,
            name=name,
            last_run=now,
            last_plan=plan_str,
            run_count=1,
        ))

    entries.sort(key=lambda e: e.last_run, reverse=True)
    _write(entries[:20])


def trim_history(max_entries: int = 20) -> None:
    entries = load_history()
    if len(entries) > max_entries:
        _write(entries[:max_entries])


def _write(entries: list[HistoryEntry]) -> None:
    HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": _VERSION, "entries": [asdict(e) for e in entries]}
    tmp = HISTORY_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    tmp.replace(HISTORY_FILE)
```

- [x] **Step 4: Run tests to verify pass**

```
pytest tests/test_history.py -v
```
Expected: all 5 PASS

- [x] **Step 5: Commit**

```bash
git add tero2/history.py tests/test_history.py
git commit -m "add history.py with HistoryEntry load/record/trim"
```

---

### Task 3: StuckHintWidget

**Files:**
- [x] Create: `tero2/tui/widgets/stuck_hint.py`
- [x] Create: `tests/test_stuck_hint.py`

- [x] **Step 1: Write the failing test**

```python
# tests/test_stuck_hint.py
import pytest
from textual.app import App, ComposeResult

from tero2.tui.widgets.stuck_hint import StuckHintWidget


class _HostApp(App):
    def compose(self) -> ComposeResult:
        yield StuckHintWidget(id="stuck-hint")


@pytest.mark.asyncio
async def test_stuck_hint_hidden_by_default():
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is False


@pytest.mark.asyncio
async def test_stuck_hint_shows_when_display_set():
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        widget.display = True
        assert widget.display is True


@pytest.mark.asyncio
async def test_stuck_hint_text_content():
    app = _HostApp()
    async with app.run_test() as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        rendered = widget.render()
        assert "retry" in str(rendered)
        assert "switch" in str(rendered)
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_stuck_hint.py -v
```
Expected: `ImportError: cannot import name 'StuckHintWidget'`

- [x] **Step 3: Implement StuckHintWidget**

**⚠️ Design note:** Use Textual's `display` reactive (hides widget + removes from layout). Do NOT mix CSS `display: none` with `self.visible` — they collide: `visible=True` only clears inline style, CSS rule still hides widget. One mechanism only.

```python
# tero2/tui/widgets/stuck_hint.py
"""Single-line hint widget shown during stuck state."""

from __future__ import annotations

from typing import ClassVar

from textual.widgets import Static


class StuckHintWidget(Static):
    """Shown above Footer when Runner is stuck. Hidden by default.

    Toggle via ``widget.display = True/False`` — Textual's built-in reactive
    that properly hides widget AND removes it from layout space.
    """

    DEFAULT_CSS: ClassVar[str] = """
    StuckHintWidget {
        height: 1;
        content-align: center middle;
        color: $warning;
    }
    """

    _HINT = "застряли — выбери: 1 retry  2 switch  3 skip  4 escalate  5 manual"

    def __init__(self, **kwargs) -> None:
        super().__init__(self._HINT, **kwargs)
        self.display = False
```

- [x] **Step 4: Run tests to verify pass**

```
pytest tests/test_stuck_hint.py -v
```
Expected: all PASS

- [x] **Step 5: Commit**

```bash
git add tero2/tui/widgets/stuck_hint.py tests/test_stuck_hint.py
git commit -m "add StuckHintWidget for stuck-state visibility"
```

---

## Chunk 2: Dashboard migration (app.py + styles)

### Task 4: Migrate app.py — ControlsPanel → StuckHintWidget

**Files:**
- [x] Modify: `tero2/tui/app.py` (all ControlsPanel references)
- [x] Delete: `tero2/tui/widgets/controls.py`
- [x] Modify: `tero2/tui/styles.tcss`
- [x] Modify: `tests/test_tui_commands.py` (update references)

**Exact changes needed in app.py:**

| Line | Old | New |
|------|-----|-----|
| 17 | `from tero2.tui.widgets.controls import ControlsPanel` | `from tero2.tui.widgets.stuck_hint import StuckHintWidget` |
| 8 | (existing imports) | add `from textual.widgets import Header, Footer` |
| 29 | `("s", "steer", "Стир"),` | `("s", "steer", "Указание"),` |
| 34-38 | stuck options with `""` labels | labels: `"1 retry"`, `"2 switch"`, `"3 skip"`, `"4 escalate"`, `"5 manual"` |
| after line 39 | — | add `("l", "change_plan", "Смена плана")`, `("n", "new_project", "Новый проект")`, `("o", "settings", "Настройки")` |
| 56-61 | `yield ControlsPanel(id="controls")` | `yield Header()` before PipelinePanel; `yield Footer()` and `yield StuckHintWidget(id="stuck-hint")` after main-row |
| 88 | `controls = self.query_one("#controls", ControlsPanel)` | `stuck_hint = self.query_one("#stuck-hint", StuckHintWidget)` |
| 107 | `controls.stuck_mode = True` | `stuck_hint.display = True` |
| 112 | `controls.stuck_mode = False` | `stuck_hint.display = False` |
| 148-150 | `controls = self.query_one(...)` / `controls.stuck_mode = False` | `stuck_hint = self.query_one("#stuck-hint", StuckHintWidget)` / `stuck_hint.display = False` |

- [x] **Step 1: Write the failing tests**

**⚠️ Gotchas baked into these tests:**
- [x] `make_event` signature is `role: str = ""` — passing `role=None` raises TypeError. Use `role=""`.
- [x] `on_mount` subscribes once and stores the returned queue in `self._event_queue`. The background worker captures that reference when it starts. Overwriting `app._event_queue = asyncio.Queue()` from the test has no effect — the worker keeps the old reference. Instead: push to the SAME queue the app has (they're the same object as `dispatcher.subscribe.return_value`).

```python
# tests/test_app_migration.py
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from textual.widgets import Footer, Header

from tero2.tui.app import DashboardApp
from tero2.tui.widgets.stuck_hint import StuckHintWidget


def _make_app():
    runner = MagicMock()
    runner.config.roles = {"builder": MagicMock()}
    runner.run = AsyncMock()
    runner.project_path = None
    dispatcher = MagicMock()
    event_queue: asyncio.Queue = asyncio.Queue()
    dispatcher.subscribe.return_value = event_queue
    cq: asyncio.Queue = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=cq)
    return app, event_queue


@pytest.mark.asyncio
async def test_no_controls_panel_in_dom():
    """ControlsPanel must not exist — NoMatches would crash old code."""
    app, _ = _make_app()
    async with app.run_test(headless=True) as pilot:
        from textual.css.query import NoMatches
        with pytest.raises(NoMatches):
            app.query_one("#controls")


@pytest.mark.asyncio
async def test_stuck_hint_exists_and_hidden():
    app, _ = _make_app()
    async with app.run_test(headless=True) as pilot:
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is False


@pytest.mark.asyncio
async def test_header_and_footer_exist():
    app, _ = _make_app()
    async with app.run_test(headless=True) as pilot:
        app.query_one(Header)
        app.query_one(Footer)


@pytest.mark.asyncio
async def test_stuck_hint_shown_after_stuck_event():
    """Emit a 'stuck' event via the subscribed queue → _consume_events flips display."""
    from tero2.events import make_event
    app, event_queue = _make_app()
    async with app.run_test(headless=True) as pilot:
        # role is str (not None) — matches make_event signature
        event = make_event("stuck", role="", data={})
        await event_queue.put(event)
        await pilot.pause(0.2)
        widget = app.query_one("#stuck-hint", StuckHintWidget)
        assert widget.display is True
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_app_migration.py -v
```
Expected: `FAIL test_no_controls_panel_in_dom` (ControlsPanel still exists)

- [x] **Step 3: Apply all changes to app.py**

Edit `tero2/tui/app.py`:

- [x] Replace line 17 (ControlsPanel import):
```python
from textual.widgets import Footer, Header

from tero2.tui.widgets.stuck_hint import StuckHintWidget
```

- [x] Replace BINDINGS (lines 28-39):
```python
BINDINGS: ClassVar[list] = [
    ("r", "roles", "Роли"),
    ("s", "steer", "Указание"),
    ("p", "pause", "Пауза"),
    ("q", "quit", "Выход"),
    ("k", "skip", "Пропустить"),
    ("l", "change_plan", "Смена плана"),
    ("n", "new_project", "Новый"),
    ("o", "settings", "Настройки"),
    ("1", "stuck_option_1", "1 retry"),
    ("2", "stuck_option_2", "2 switch"),
    ("3", "stuck_option_3", "3 skip"),
    ("4", "stuck_option_4", "4 escalate"),
    ("5", "stuck_option_5", "5 manual"),
]
```

- [x] Replace compose() method:
```python
def compose(self) -> ComposeResult:
    yield Header()
    yield PipelinePanel(id="pipeline")
    with Horizontal(id="main-row"):
        yield LogView(id="log-view")
        yield UsagePanel(id="usage-panel")
    yield StuckHintWidget(id="stuck-hint")
    yield Footer()
```

- [x] In `_consume_events()`, replace line 88:
```python
stuck_hint = self.query_one("#stuck-hint", StuckHintWidget)
```
Replace lines 107 and 112:
```python
# line 107 (stuck event)
stuck_hint.display = True

# line 112 (done event)
stuck_hint.display = False
```

- [x] Replace `_clear_stuck_mode()`:
```python
def _clear_stuck_mode(self) -> None:
    pipeline = self.query_one("#pipeline", PipelinePanel)
    stuck_hint = self.query_one("#stuck-hint", StuckHintWidget)
    pipeline.stuck_mode = False
    stuck_hint.display = False
```

- [x] Add `check_action()` method after `_clear_stuck_mode`:
```python
def check_action(self, action: str, parameters: tuple) -> bool:
    stuck_actions = {
        "stuck_option_1", "stuck_option_2", "stuck_option_3",
        "stuck_option_4", "stuck_option_5",
    }
    if action in stuck_actions:
        try:
            hint = self.query_one("#stuck-hint", StuckHintWidget)
            return bool(hint.display)
        except Exception:
            return False
    return True
```

- [x] Add new action stubs after `action_skip`:

**⚠️ Guard against missing project_path:** `runner.project_path` can be `None` when the Runner was started via idle mode without a project (rare but possible). `PlanPickScreen.__init__` calls `.rglob("*.md")` on the path — passing `None` crashes with `AttributeError`. Guard before pushing the screen.

```python
def action_change_plan(self) -> None:
    project_path = getattr(self._runner, "project_path", None)
    if project_path is None:
        log_view = self.query_one("#log-view", LogView)
        log_view.push_message(
            "Смена плана недоступна: проект не задан.",
            style="yellow",
        )
        return

    from tero2.tui.screens.plan_pick import PlanPickScreen

    def _on_plan_selected(plan_file) -> None:
        if plan_file is not None:
            self._command_queue.put_nowait(
                Command("new_plan", data={"text": str(plan_file)}, source="tui")
            )

    self.push_screen(PlanPickScreen(project_path), _on_plan_selected)

def action_new_project(self) -> None:
    # M2: launch StartupWizard with callback to replace current runner
    log_view = self.query_one("#log-view", LogView)
    log_view.push_message("Смена проекта — будет в M2.", style="yellow")

def action_settings(self) -> None:
    # M3: open SettingsScreen
    log_view = self.query_one("#log-view", LogView)
    log_view.push_message("Настройки — будут в M3.", style="yellow")
```

- [x] **Step 4: Delete controls.py**

```bash
rm tero2/tui/widgets/controls.py
```

- [x] **Step 5: Update existing test_tui_commands.py**

In `tests/test_tui_commands.py`, find any references to `ControlsPanel` or `#controls` and replace with `StuckHintWidget` / `#stuck-hint` pattern.

- [x] **Step 6: Run all tests to verify pass**

```
pytest tests/test_app_migration.py tests/test_tui_commands.py tests/test_stuck_hint.py -v
```
Expected: all PASS

- [x] **Step 7: Commit**

```bash
git add tero2/tui/app.py tero2/tui/widgets/stuck_hint.py tests/test_app_migration.py tests/test_tui_commands.py
git rm tero2/tui/widgets/controls.py
git commit -m "replace ControlsPanel with StuckHintWidget, add Header/Footer"
```

---

### Task 5: styles.tcss — add styles for new widgets

**Files:**
- [x] Modify: `tero2/tui/styles.tcss`

- [x] **Step 1: Read current styles.tcss to understand structure**

Check `tero2/tui/styles.tcss` first to see existing selectors and layout.

- [x] **Step 2: Add styles for new screens**

**⚠️ Don't duplicate:** `StuckHintWidget` already declares `DEFAULT_CSS` in its class body (Task 3). Do NOT re-declare the same rules in `styles.tcss` — Textual would merge both and drift over time. Only styles that don't belong to a single widget go here.

Append to `tero2/tui/styles.tcss`:
```css
/* ProjectPickScreen / PlanPickScreen */
ProjectPickScreen,
PlanPickScreen {
    background: $surface;
    border: thick $primary;
    height: auto;
    max-height: 80%;
    width: 80%;
    margin: 2 4;
}

ProjectPickScreen ListView,
PlanPickScreen ListView {
    height: auto;
    max-height: 20;
}

ProjectPickScreen .path-label,
PlanPickScreen .path-label {
    color: $text-muted;
}

ProjectPickScreen .entry-warning {
    color: $warning;
}
```

- [x] **Step 3: Verify app still launches without CSS errors**

```
python -c "from tero2.tui.app import DashboardApp; print('OK')"
```
Expected: `OK`

- [x] **Step 4: Commit**

```bash
git add tero2/tui/styles.tcss
git commit -m "add styles for StuckHintWidget and wizard screens"
```

---

## Chunk 3: Wizard screens

### Task 6: PlanPickScreen

**Files:**
- [x] Create: `tero2/tui/screens/plan_pick.py`
- [x] Create: `tests/test_plan_pick.py`

Note: PlanPickScreen is needed BEFORE ProjectPickScreen because `action_change_plan` in app.py already references it.

- [x] **Step 1: Write the failing tests**

```python
# tests/test_plan_pick.py
from pathlib import Path

import pytest
from textual.app import App

from tero2.tui.screens.plan_pick import PlanPickScreen


@pytest.mark.asyncio
async def test_plan_pick_lists_md_files(tmp_path):
    (tmp_path / "plan.md").write_text("# plan")
    (tmp_path / "notes.md").write_text("# notes")
    (tmp_path / "other.txt").write_text("text")

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        assert len(items) >= 2


@pytest.mark.asyncio
async def test_plan_pick_skips_git_dir(tmp_path):
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "plan.md").write_text("# hidden")
    (tmp_path / "real.md").write_text("# real")

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        # Inspect the cached file list stored on the screen instance.
        paths = [str(p) for p in screen._files]
        assert not any(".git" in p for p in paths)
        assert any("real.md" in p for p in paths)


@pytest.mark.asyncio
async def test_plan_pick_skips_sora_dir(tmp_path):
    sora = tmp_path / ".sora"
    sora.mkdir()
    (sora / "internal.md").write_text("# internal")
    (tmp_path / "plan.md").write_text("# plan")

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        paths = [str(p) for p in screen._files]
        assert not any(".sora" in p for p in paths)


@pytest.mark.asyncio
async def test_plan_pick_idle_mode_on_press_i(tmp_path):
    (tmp_path / "plan.md").write_text("# plan")
    results = []

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.press("i")
        await pilot.pause(0.1)
        assert results == [None]


@pytest.mark.asyncio
async def test_plan_pick_empty_dir_auto_idle(tmp_path):
    results = []

    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = PlanPickScreen(tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.2)
        # auto-dismissed with None when no .md files
        assert results == [None]
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_plan_pick.py -v
```
Expected: `ImportError: cannot import name 'PlanPickScreen'`

- [x] **Step 3: Implement PlanPickScreen**

**⚠️ Design notes:**
- [x] Scan filesystem ONCE in `__init__` and cache as `self._files: list[Path]`. Don't re-`rglob()` on every compose/select — slow + index drift if files change mid-use.
- [x] Use public `event.list_view.index` attribute — `_index` is a private Textual internal and can break between versions.

```python
# tero2/tui/screens/plan_pick.py
"""PlanPickScreen — wizard step 2: pick a plan file."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Label, ListItem, ListView, Static

from tero2.constants import PROJECT_SCAN_SKIP_DIRS

_SKIP = PROJECT_SCAN_SKIP_DIRS | {".sora"}
_MAX_PLANS = 30


class PlanPickScreen(ModalScreen[Path | None]):
    """Pick a plan .md file from the project directory."""

    BINDINGS: ClassVar[list] = [
        Binding("i", "idle_mode", "Idle (без плана)"),
        Binding("b", "back", "Назад"),
        Binding("escape,q", "back", "Назад", show=False),
    ]

    def __init__(self, project_path: Path) -> None:
        super().__init__()
        self._project_path = project_path
        # Scan once; cache result. Index used in selection must match compose().
        self._files: list[Path] = self._scan_md_files()

    def _scan_md_files(self) -> list[Path]:
        files: list[Path] = []
        try:
            for p in self._project_path.rglob("*.md"):
                if any(part in _SKIP for part in p.parts):
                    continue
                files.append(p)
        except PermissionError:
            pass
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:_MAX_PLANS]

    def compose(self) -> ComposeResult:
        yield Static(
            f"tero2 — план для {self._project_path.name}",
            classes="screen-title",
        )
        if not self._files:
            yield Static("Нет .md файлов — запуск в idle-режиме…", classes="info-msg")
        else:
            items = [
                ListItem(
                    Label(p.name, classes="plan-name"),
                    Label(str(p.parent), classes="path-label"),
                )
                for p in self._files
            ]
            yield ListView(*items, id="plan-list")
        yield Footer()

    def on_mount(self) -> None:
        if not self._files:
            self.call_after_refresh(self._auto_idle)

    def _auto_idle(self) -> None:
        self.dismiss(None)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index  # public attr
        if idx is not None and 0 <= idx < len(self._files):
            self.dismiss(self._files[idx])

    def action_idle_mode(self) -> None:
        self.dismiss(None)

    def action_back(self) -> None:
        self.dismiss(None)
```

- [x] **Step 4: Run tests to verify pass**

```
pytest tests/test_plan_pick.py -v
```
Expected: all PASS

- [x] **Step 5: Commit**

```bash
git add tero2/tui/screens/plan_pick.py tests/test_plan_pick.py
git commit -m "add PlanPickScreen for wizard step 2"
```

---

### Task 7: ProjectPickScreen + StartupWizard

**Files:**
- [x] Create: `tero2/tui/screens/project_pick.py`
- [x] Create: `tero2/tui/screens/startup_wizard.py`
- [x] Create: `tests/test_startup_wizard.py`

- [x] **Step 1: Write the failing tests**

```python
# tests/test_startup_wizard.py
from pathlib import Path
from unittest.mock import patch

import pytest
from textual.app import App

from tero2.tui.screens.project_pick import ProjectPickScreen
from tero2.tui.screens.startup_wizard import StartupWizard


@pytest.mark.asyncio
async def test_project_pick_shows_history(tmp_path):
    from tero2.history import HistoryEntry
    entries = [
        HistoryEntry(
            path=str(tmp_path / "proj1"),
            name="proj1",
            last_run="2026-04-18T10:00:00+00:00",
            last_plan="plan.md",
            run_count=3,
        )
    ]
    (tmp_path / "proj1").mkdir()

    with patch("tero2.tui.screens.project_pick.load_history", return_value=entries):
        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = ProjectPickScreen()
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.1)
            items = screen.query("ListView ListItem")
            assert len(items) >= 1


@pytest.mark.asyncio
async def test_project_pick_marks_missing_dir(tmp_path):
    from tero2.history import HistoryEntry
    entries = [
        HistoryEntry(
            path=str(tmp_path / "nonexistent"),
            name="nonexistent",
            last_run="2026-04-18T10:00:00+00:00",
            last_plan=None,
            run_count=1,
        )
    ]
    with patch("tero2.tui.screens.project_pick.load_history", return_value=entries):
        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = ProjectPickScreen()
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.1)
            warnings = screen.query(".entry-warning")
            assert len(warnings) >= 1


@pytest.mark.asyncio
async def test_project_pick_escape_returns_none(tmp_path):
    results = []
    with patch("tero2.tui.screens.project_pick.load_history", return_value=[]):
        app = App()
        async with app.run_test(headless=True) as pilot:
            screen = ProjectPickScreen()
            await app.push_screen(screen, results.append)
            await pilot.press("escape")
            await pilot.pause(0.1)
            assert results == [None]


@pytest.mark.asyncio
async def test_startup_wizard_composes():
    with patch("tero2.tui.screens.project_pick.load_history", return_value=[]):
        app = App()
        async with app.run_test(headless=True) as pilot:
            wiz = StartupWizard()
            await app.push_screen(wiz, lambda x: None)
            await pilot.pause(0.1)
            # wizard shows ProjectPickScreen as first step
            assert len(app.screen_stack) >= 2
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_startup_wizard.py -v
```
Expected: `ImportError: cannot import name 'ProjectPickScreen'`

- [x] **Step 3: Implement ProjectPickScreen**

```python
# tero2/tui/screens/project_pick.py
"""ProjectPickScreen — wizard step 1: pick or enter project path."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from tero2.history import HistoryEntry, load_history


class ProjectPickScreen(ModalScreen[Path | None]):
    """Pick project from history or enter path manually."""

    BINDINGS: ClassVar[list] = [
        Binding("n", "manual_input", "Ввести путь"),
        Binding("escape,q", "cancel", "Выход", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[HistoryEntry] = load_history()
        self._manual_mode = False

    def compose(self) -> ComposeResult:
        yield Static("tero2 — выбор проекта", classes="screen-title")
        if not self._entries:
            yield Input(placeholder="Путь к проекту…", id="path-input")
            yield Label("Введите путь и нажмите Enter", classes="info-msg")
        else:
            items = self._build_items()
            yield ListView(*items, id="project-list")
        yield Footer()

    def _build_items(self) -> list[ListItem]:
        items = []
        for entry in self._entries:
            exists = Path(entry.path).is_dir()
            name_label = Label(entry.name)
            path_label = Label(entry.path, classes="path-label")
            if not exists:
                warn = Label("⚠ папка не найдена", classes="entry-warning")
                items.append(ListItem(name_label, path_label, warn))
            else:
                items.append(ListItem(name_label, path_label))
        return items

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index  # public attr, not _index
        if idx is None or not (0 <= idx < len(self._entries)):
            return
        entry = self._entries[idx]
        p = Path(entry.path)
        if p.is_dir():
            self.dismiss(p)
        else:
            self.notify("Папка не найдена — удалить из истории? (d)", severity="warning")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        p = Path(event.value).expanduser().resolve()
        if p.is_dir():
            self.dismiss(p)
        else:
            self.notify("Папка не найдена", severity="error")

    def action_manual_input(self) -> None:
        self.mount(Input(placeholder="Путь к проекту…", id="path-input"))

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [x] **Step 4: Implement StartupWizard**

```python
# tero2/tui/screens/startup_wizard.py
"""StartupWizard — multi-step wizard for no-args tero2 go."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Label

from tero2.tui.screens.plan_pick import PlanPickScreen
from tero2.tui.screens.project_pick import ProjectPickScreen


class StartupWizard(Screen[tuple[Path, Path | None] | None]):
    """Guides user through project + plan selection."""

    def compose(self) -> ComposeResult:
        yield Label("")  # placeholder, wizard uses push_screen

    def on_mount(self) -> None:
        self.app.push_screen(ProjectPickScreen(), self._on_project_picked)

    def _on_project_picked(self, project_path: Path | None) -> None:
        if project_path is None:
            self.dismiss(None)
            return
        self.app.push_screen(
            PlanPickScreen(project_path),
            lambda plan: self.dismiss((project_path, plan)),
        )
```

- [x] **Step 5: Run tests to verify pass**

```
pytest tests/test_startup_wizard.py -v
```
Expected: all PASS

- [x] **Step 6: Commit**

```bash
git add tero2/tui/screens/project_pick.py tero2/tui/screens/startup_wizard.py tests/test_startup_wizard.py
git commit -m "add ProjectPickScreen and StartupWizard"
```

---

## Chunk 4: CLI wiring + history recording

### Task 8: cli.py — optional project_path + wizard launch

**Files:**
- [x] Modify: `tero2/cli.py`
- [x] Create: `tests/test_cli_wizard.py`

- [x] **Step 1: Write the failing tests**

**⚠️ Note on parser naming:** the existing function in `tero2/cli.py:264` is `_build_parser()` (private). This plan makes it public as `build_parser` (rename) since we need a public testing surface. The underscore version is removed — no alias kept because it has no external callers in main (only tests from M1 use it).

```python
# tests/test_cli_wizard.py
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_go_parser_allows_no_project_path():
    """project_path must be optional (nargs='?')."""
    from tero2.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["go"])
    assert args.project_path is None


def test_go_parser_still_accepts_path(tmp_path):
    from tero2.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["go", str(tmp_path)])
    assert args.project_path == str(tmp_path)


def test_cmd_go_calls_wizard_when_no_path(tmp_path):
    """When project_path is None, run_startup_wizard is called."""
    with patch("tero2.cli.run_startup_wizard") as mock_wizard:
        mock_wizard.return_value = None  # user cancelled
        from tero2.cli import cmd_go
        args = MagicMock()
        args.project_path = None
        args.plan = None
        args.config = None
        args.idle_timeout = 0
        args.verbose = False
        with pytest.raises(SystemExit) as exc:
            cmd_go(args)
        assert exc.value.code == 0
        mock_wizard.assert_called_once()
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_cli_wizard.py::test_go_parser_allows_no_project_path -v
```
Expected: FAIL — `project_path` is required

- [x] **Step 3: Modify cli.py**

Rename `_build_parser()` → `build_parser()` at `tero2/cli.py:264` (public testing surface). Update any internal callers (e.g. in `main()`) to use the new name.

In `build_parser()`, change `project_path` from positional required to optional:
```python
# before:
go_parser.add_argument("project_path", help="path to project")

# after:
go_parser.add_argument("project_path", nargs="?", default=None, help="path to project (omit to open wizard)")
```

Add `run_startup_wizard()` function near the top of cli.py (before `cmd_go`):
```python
def run_startup_wizard() -> tuple | None:
    """Launch StartupWizard, return (project_path, plan_file) or None on cancel."""
    from tero2.tui.screens.startup_wizard import StartupWizard
    from textual.app import App

    result_holder = []

    class _WizardApp(App):
        def on_mount(self) -> None:
            self.push_screen(StartupWizard(), self._done)

        def _done(self, result) -> None:
            result_holder.append(result)
            self.exit()

    _WizardApp().run()
    return result_holder[0] if result_holder else None
```

Modify `cmd_go` to branch on `args.project_path is None`:
```python
def cmd_go(args) -> None:
    if args.project_path is None:
        result = run_startup_wizard()
        if result is None:
            sys.exit(0)
        project_path, plan_file = result
        plan_file = plan_file  # already a Path or None
    else:
        project_path = Path(args.project_path).expanduser().resolve()
        plan_file = _resolve_plan(args.plan, project_path) if args.plan else None
    # ... rest of existing cmd_go logic continues unchanged
```

- [x] **Step 4: Run tests to verify pass**

```
pytest tests/test_cli_wizard.py -v
```
Expected: all PASS

- [x] **Step 5: Verify regression — existing go path still works**

```
pytest tests/ -k "tui" -v
```
Expected: all pre-existing TUI tests still PASS

- [x] **Step 6: Commit**

```bash
git add tero2/cli.py tests/test_cli_wizard.py
git commit -m "make project_path optional in go subcommand, wire startup wizard"
```

---

### Task 9: record_run call in cmd_go

**Files:**
- [x] Modify: `tero2/cli.py`
- [x] Modify: `tests/test_cli_wizard.py`

- [x] **Step 1: Add test for history recording**

Add to `tests/test_cli_wizard.py`:
```python
def test_cmd_go_records_history_on_launch(tmp_path, monkeypatch):
    """record_run is called after DashboardApp launches."""
    recorded = []

    monkeypatch.setattr("tero2.cli.record_run", lambda p, f: recorded.append((p, f)))

    with patch("tero2.cli.DashboardApp") as MockApp:
        MockApp.return_value.run = MagicMock()
        with patch("tero2.cli.Runner"), patch("tero2.cli.EventDispatcher"):
            from tero2.cli import cmd_go
            args = MagicMock()
            args.project_path = str(tmp_path)
            args.plan = None
            args.config = None
            args.idle_timeout = 0
            args.verbose = False
            cmd_go(args)

    assert len(recorded) == 1
    assert recorded[0][0] == tmp_path
```

- [x] **Step 2: Run to verify failure**

```
pytest tests/test_cli_wizard.py::test_cmd_go_records_history_on_launch -v
```
Expected: FAIL — `record_run` not called yet

- [x] **Step 3: Add record_run call to cmd_go**

In `tero2/cli.py`, add import at top:
```python
from tero2.history import record_run
```

After `DashboardApp(...).run()` succeeds in `cmd_go`, add:
```python
record_run(project_path, plan_file)
```

- [x] **Step 4: Run tests to verify pass**

```
pytest tests/test_cli_wizard.py -v
```
Expected: all PASS

- [x] **Step 5: Commit**

```bash
git add tero2/cli.py tests/test_cli_wizard.py
git commit -m "record project run in history after DashboardApp launch"
```

---

## Chunk 5: Integration + full test suite

### Task 10: Integration smoke test + full suite

**Files:**
- [x] Create: `tests/test_m1_integration.py`

- [x] **Step 1: Write integration test**

```python
# tests/test_m1_integration.py
"""M1 integration: tero2 go paths work end-to-end."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def test_tero2_go_without_args_does_not_crash(tmp_path):
    """tero2 go without args → wizard path → sys.exit(0) on cancel (no TypeError)."""
    with patch("tero2.cli.run_startup_wizard", return_value=None) as mock_wiz:
        from tero2.cli import cmd_go
        import types
        args = types.SimpleNamespace(
            project_path=None, plan=None, config=None, idle_timeout=0, verbose=False
        )
        import sys
        with pytest.raises(SystemExit) as exc:
            cmd_go(args)
        assert exc.value.code == 0


def test_tero2_go_with_path_skips_wizard(tmp_path):
    """tero2 go <path> does NOT call run_startup_wizard."""
    with patch("tero2.cli.run_startup_wizard") as mock_wiz:
        with patch("tero2.cli.DashboardApp") as MockApp:
            MockApp.return_value.run = MagicMock()
            with patch("tero2.cli.Runner"), patch("tero2.cli.EventDispatcher"), \
                 patch("tero2.cli.record_run"):
                from tero2.cli import cmd_go
                import types
                args = types.SimpleNamespace(
                    project_path=str(tmp_path),
                    plan=None, config=None, idle_timeout=0, verbose=False
                )
                cmd_go(args)
        mock_wiz.assert_not_called()


@pytest.mark.asyncio
async def test_dashboard_app_no_controls_panel():
    """DashboardApp must not reference #controls anywhere — would raise NoMatches."""
    from tero2.tui.app import DashboardApp
    from tero2.tui.widgets.stuck_hint import StuckHintWidget
    from textual.css.query import NoMatches

    runner = MagicMock()
    runner.config.roles = {}
    runner.run = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.subscribe.return_value = asyncio.Queue()
    cq = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=cq)

    async with app.run_test(headless=True) as pilot:
        with pytest.raises(NoMatches):
            app.query_one("#controls")
        app.query_one("#stuck-hint", StuckHintWidget)  # must exist
```

- [x] **Step 2: Run integration tests**

```
pytest tests/test_m1_integration.py -v
```
Expected: all PASS

- [x] **Step 3: Run full test suite to verify no regressions**

```
pytest tests/ -v --tb=short
```
Expected: all pre-existing tests PASS, new tests PASS

- [x] **Step 4: Final M1 commit**

```bash
git add tests/test_m1_integration.py
git commit -m "m1 integration tests: wizard path, no controls panel, no regressions"
```

---

## Summary

After M1 completion:
- [x] `tero2 go` without args opens wizard → DashboardApp (no crash)
- [x] `tero2 go <path>` works as before (regression-free)
- [x] ControlsPanel deleted, StuckHintWidget shows during stuck state
- [x] Footer replaces ControlsPanel hotkey display
- [x] Stuck options `[1-5]` hidden from Footer until stuck state, with readable labels
- [x] `[s]` labeled "Указание" (was "Стир")
- [x] Project run history in `~/.tero2/history.json`
- [x] `[l]` changes plan (sends `new_plan` command — works in idle, warns in active)
- [x] `[n]` and `[o]` are stubs (M2/M3)
