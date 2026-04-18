# TUI Redesign — Unified Plan (M1 + M2 + M3)

**Execution order:** M1 → M2 → M3. M2 requires M1 merged. M3 requires M1 + M2 merged.

Each milestone below is self-contained with its own Goal/Architecture/Tech Stack header.

---

# M1 — MVP Wizard

**Role map:**
| Task | Role | Why |
|------|------|-----|
| 1 | scout | refactor existing code |
| 2 | builder | new module |
| 3 | builder | new widget |
| 4 | builder | complex migration |
| 5 | architect | CSS/layout |
| 6 | builder | new screen |
| 7 | builder | new screens |
| 8 | builder | CLI changes |
| 9 | builder | small addition |
| 10 | verifier | integration tests |

**Goal:** `tero2 go` without args opens a startup wizard (project pick → plan pick) → launches DashboardApp. `tero2 go <path>` continues to work. ControlsPanel replaced with StuckHintWidget.

**Architecture:** New modules: `tero2/history.py`, `tero2/tui/widgets/stuck_hint.py`, `tero2/tui/screens/startup_wizard.py`, `tero2/tui/screens/project_pick.py`, `tero2/tui/screens/plan_pick.py`. Modified: `tero2/cli.py`, `tero2/tui/app.py`, `tero2/tui/styles.tcss`, `tero2/constants.py`, `tero2/players/scout.py`. Deleted: `tero2/tui/widgets/controls.py`.

**Tech Stack:** Python 3.11+, Textual ≥1.0, pytest, pytest-textual-snapshot

---

## Chunk 1: Foundation (constants, history, stuck widget)

### Task 1: Move _SKIP_DIRS to constants.py

**Files:**
- Modify: `tero2/constants.py`
- Modify: `tero2/players/scout.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_constants.py
from tero2.constants import PROJECT_SCAN_SKIP_DIRS

def test_skip_dirs_is_frozenset():
    assert isinstance(PROJECT_SCAN_SKIP_DIRS, frozenset)

def test_skip_dirs_contains_expected():
    for d in (".git", ".venv", "node_modules", "__pycache__", "dist"):
        assert d in PROJECT_SCAN_SKIP_DIRS
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_constants.py -v
```
Expected: `ImportError: cannot import name 'PROJECT_SCAN_SKIP_DIRS'`

- [ ] **Step 3: Add PROJECT_SCAN_SKIP_DIRS to constants.py**

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

- [ ] **Step 4: Update scout.py to import from constants**

In `tero2/players/scout.py`, find `_SKIP_DIRS` (lines ~177-188) and replace the local definition with:
```python
from tero2.constants import PROJECT_SCAN_SKIP_DIRS as _SKIP_DIRS
```
Remove the old `_SKIP_DIRS = {...}` block entirely.

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/test_constants.py tests/test_players.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/constants.py tero2/players/scout.py tests/test_constants.py
git commit -m "move _SKIP_DIRS to constants.PROJECT_SCAN_SKIP_DIRS"
```

---

### Task 2: tero2/history.py — project run history

**Files:**
- Create: `tero2/history.py`
- Create: `tests/test_history.py`

- [ ] **Step 1: Write the failing tests**

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

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_history.py -v
```
Expected: `ModuleNotFoundError: No module named 'tero2.history'`

- [ ] **Step 3: Implement tero2/history.py**

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

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_history.py -v
```
Expected: all 5 PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/history.py tests/test_history.py
git commit -m "add history.py with HistoryEntry load/record/trim"
```

---

### Task 3: StuckHintWidget

**Files:**
- Create: `tero2/tui/widgets/stuck_hint.py`
- Create: `tests/test_stuck_hint.py`

- [ ] **Step 1: Write the failing test**

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

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_stuck_hint.py -v
```
Expected: `ImportError: cannot import name 'StuckHintWidget'`

- [ ] **Step 3: Implement StuckHintWidget**

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

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_stuck_hint.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/tui/widgets/stuck_hint.py tests/test_stuck_hint.py
git commit -m "add StuckHintWidget for stuck-state visibility"
```

---

## Chunk 2: Dashboard migration (app.py + styles)

### Task 4: Migrate app.py — ControlsPanel → StuckHintWidget

**Files:**
- Modify: `tero2/tui/app.py` (all ControlsPanel references)
- Delete: `tero2/tui/widgets/controls.py`
- Modify: `tero2/tui/styles.tcss`
- Modify: `tests/test_tui_commands.py` (update references)

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

- [ ] **Step 1: Write the failing tests**

**⚠️ Gotchas baked into these tests:**
- `make_event` signature is `role: str = ""` — passing `role=None` raises TypeError. Use `role=""`.
- `on_mount` subscribes once and stores the returned queue in `self._event_queue`. The background worker captures that reference when it starts. Overwriting `app._event_queue = asyncio.Queue()` from the test has no effect — the worker keeps the old reference. Instead: push to the SAME queue the app has (they're the same object as `dispatcher.subscribe.return_value`).

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

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_app_migration.py -v
```
Expected: `FAIL test_no_controls_panel_in_dom` (ControlsPanel still exists)

- [ ] **Step 3: Apply all changes to app.py**

Edit `tero2/tui/app.py`:

1. Replace line 17 (ControlsPanel import):
```python
from textual.widgets import Footer, Header

from tero2.tui.widgets.stuck_hint import StuckHintWidget
```

2. Replace BINDINGS (lines 28-39):
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

3. Replace compose() method:
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

4. In `_consume_events()`, replace line 88:
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

5. Replace `_clear_stuck_mode()`:
```python
def _clear_stuck_mode(self) -> None:
    pipeline = self.query_one("#pipeline", PipelinePanel)
    stuck_hint = self.query_one("#stuck-hint", StuckHintWidget)
    pipeline.stuck_mode = False
    stuck_hint.display = False
```

6. Add `check_action()` method after `_clear_stuck_mode`:
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

7. Add new action stubs after `action_skip`:

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

- [ ] **Step 4: Delete controls.py**

```bash
rm tero2/tui/widgets/controls.py
```

- [ ] **Step 5: Update existing test_tui_commands.py**

In `tests/test_tui_commands.py`, find any references to `ControlsPanel` or `#controls` and replace with `StuckHintWidget` / `#stuck-hint` pattern.

- [ ] **Step 6: Run all tests to verify pass**

```
pytest tests/test_app_migration.py tests/test_tui_commands.py tests/test_stuck_hint.py -v
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add tero2/tui/app.py tero2/tui/widgets/stuck_hint.py tests/test_app_migration.py tests/test_tui_commands.py
git rm tero2/tui/widgets/controls.py
git commit -m "replace ControlsPanel with StuckHintWidget, add Header/Footer"
```

---

### Task 5: styles.tcss — add styles for new widgets

**Files:**
- Modify: `tero2/tui/styles.tcss`

- [ ] **Step 1: Read current styles.tcss to understand structure**

Check `tero2/tui/styles.tcss` first to see existing selectors and layout.

- [ ] **Step 2: Add styles for new screens**

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

- [ ] **Step 3: Verify app still launches without CSS errors**

```
python -c "from tero2.tui.app import DashboardApp; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add tero2/tui/styles.tcss
git commit -m "add styles for StuckHintWidget and wizard screens"
```

---

## Chunk 3: Wizard screens

### Task 6: PlanPickScreen

**Files:**
- Create: `tero2/tui/screens/plan_pick.py`
- Create: `tests/test_plan_pick.py`

Note: PlanPickScreen is needed BEFORE ProjectPickScreen because `action_change_plan` in app.py already references it.

- [ ] **Step 1: Write the failing tests**

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

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_plan_pick.py -v
```
Expected: `ImportError: cannot import name 'PlanPickScreen'`

- [ ] **Step 3: Implement PlanPickScreen**

**⚠️ Design notes:**
- Scan filesystem ONCE in `__init__` and cache as `self._files: list[Path]`. Don't re-`rglob()` on every compose/select — slow + index drift if files change mid-use.
- Use public `event.list_view.index` attribute — `_index` is a private Textual internal and can break between versions.

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

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_plan_pick.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/tui/screens/plan_pick.py tests/test_plan_pick.py
git commit -m "add PlanPickScreen for wizard step 2"
```

---

### Task 7: ProjectPickScreen + StartupWizard

**Files:**
- Create: `tero2/tui/screens/project_pick.py`
- Create: `tero2/tui/screens/startup_wizard.py`
- Create: `tests/test_startup_wizard.py`

- [ ] **Step 1: Write the failing tests**

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

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_startup_wizard.py -v
```
Expected: `ImportError: cannot import name 'ProjectPickScreen'`

- [ ] **Step 3: Implement ProjectPickScreen**

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

- [ ] **Step 4: Implement StartupWizard**

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

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/test_startup_wizard.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/tui/screens/project_pick.py tero2/tui/screens/startup_wizard.py tests/test_startup_wizard.py
git commit -m "add ProjectPickScreen and StartupWizard"
```

---

## Chunk 4: CLI wiring + history recording

### Task 8: cli.py — optional project_path + wizard launch

**Files:**
- Modify: `tero2/cli.py`
- Create: `tests/test_cli_wizard.py`

- [ ] **Step 1: Write the failing tests**

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

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_cli_wizard.py::test_go_parser_allows_no_project_path -v
```
Expected: FAIL — `project_path` is required

- [ ] **Step 3: Modify cli.py**

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

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_cli_wizard.py -v
```
Expected: all PASS

- [ ] **Step 5: Verify regression — existing go path still works**

```
pytest tests/ -k "tui" -v
```
Expected: all pre-existing TUI tests still PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/cli.py tests/test_cli_wizard.py
git commit -m "make project_path optional in go subcommand, wire startup wizard"
```

---

### Task 9: record_run call in cmd_go

**Files:**
- Modify: `tero2/cli.py`
- Modify: `tests/test_cli_wizard.py`

- [ ] **Step 1: Add test for history recording**

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

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_cli_wizard.py::test_cmd_go_records_history_on_launch -v
```
Expected: FAIL — `record_run` not called yet

- [ ] **Step 3: Add record_run call to cmd_go**

In `tero2/cli.py`, add import at top:
```python
from tero2.history import record_run
```

After `DashboardApp(...).run()` succeeds in `cmd_go`, add:
```python
record_run(project_path, plan_file)
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_cli_wizard.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/cli.py tests/test_cli_wizard.py
git commit -m "record project run in history after DashboardApp launch"
```

---

## Chunk 5: Integration + full test suite

### Task 10: Integration smoke test + full suite

**Files:**
- Create: `tests/test_m1_integration.py`

- [ ] **Step 1: Write integration test**

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

- [ ] **Step 2: Run integration tests**

```
pytest tests/test_m1_integration.py -v
```
Expected: all PASS

- [ ] **Step 3: Run M1 tests only**

```
pytest tests/test_constants.py tests/test_history.py tests/test_stuck_hint.py tests/test_app_migration.py tests/test_plan_pick.py tests/test_startup_wizard.py tests/test_cli_wizard.py tests/test_m1_integration.py -v --tb=short
```
Expected: all PASS

- [ ] **Step 4: Final M1 commit**

```bash
git add tests/test_m1_integration.py
git commit -m "m1 integration tests: wizard path, no controls panel, no regressions"
```

---

## Summary

After M1 completion:
- `tero2 go` without args opens wizard → DashboardApp (no crash)
- `tero2 go <path>` works as before (regression-free)
- ControlsPanel deleted, StuckHintWidget shows during stuck state
- Footer replaces ControlsPanel hotkey display
- Stuck options `[1-5]` hidden from Footer until stuck state, with readable labels
- `[s]` labeled "Указание" (was "Стир")
- Project run history in `~/.tero2/history.json`
- `[l]` changes plan (sends `new_plan` command — works in idle, warns in active)
- `[n]` and `[o]` are stubs (M2/M3)

## Human QA — M1

After automated tasks complete:
- [ ] Run full regression suite: `pytest tests/ -v --tb=short`
- [ ] Run `tero2 go` without args → verify wizard appears and is usable
- [ ] Run `tero2 go <path>` → verify direct launch still works
- [ ] During stuck state → verify StuckHintWidget shows and [1-5] keys work

# M2 — Model Catalog & Provider Picker

**Role map:**
| Task | Role | Why |
|------|------|-----|
| 1 | builder | new module (catalog) |
| 2 | scout | port from tero v1 |
| 3 | builder | new screen |
| 4 | builder | extend existing screen |
| 5 | builder | new feature |
| 6 | architect | CSS |
| 7 | verifier | integration tests |

**Goal:** Dynamic model catalog (opencode/kilo live fetch + static fallback), ModelPickScreen with fuzzy filter, RoleSwapScreen extended with model selection step, ZAI native provider port, Command Palette (Ctrl+P).

**Architecture:** New modules: `tero2/providers/catalog.py`, `tero2/providers/zai.py`, `tero2/tui/screens/model_pick.py`, `tero2/tui/commands.py`. Modified: `tero2/tui/screens/role_swap.py`, `tero2/providers/__init__.py`, `tero2/tui/app.py` (COMMANDS). Requires M1 to be merged first.

**Tech Stack:** Python 3.11+, asyncio, Textual ≥1.0, pytest, unittest.mock, claude-agent-sdk (for ZAI native path)

---

## Chunk 1: Provider catalog foundation

### Task 1: tero2/providers/catalog.py — static catalog + ModelEntry

**Files:**
- Create: `tero2/providers/catalog.py`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_catalog.py
import pytest
from tero2.providers.catalog import (
    DEFAULT_PROVIDERS,
    STATIC_CATALOG,
    ModelEntry,
    get_models,
)


def test_model_entry_is_frozen():
    entry = ModelEntry(id="claude-sonnet", label="Claude Sonnet")
    with pytest.raises(Exception):
        entry.id = "other"  # frozen dataclass


def test_default_providers_includes_all():
    for p in ("claude", "codex", "opencode", "kilo", "zai", "gemma"):
        assert p in DEFAULT_PROVIDERS


def test_static_catalog_claude_has_expected_models():
    models = STATIC_CATALOG["claude"]
    ids = [m.id for m in models]
    assert any("sonnet" in i for i in ids)
    assert any("opus" in i for i in ids)


def test_static_catalog_codex_has_reasoning_options():
    models = STATIC_CATALOG["codex"]
    ids = [m.id for m in models]
    assert "" in ids        # medium (default)
    assert "gpt-5.4" in ids  # high


def test_static_catalog_zai_has_glm():
    models = STATIC_CATALOG["zai"]
    ids = [m.id for m in models]
    assert "glm-5.1" in ids


def test_static_catalog_gemma_is_empty():
    assert STATIC_CATALOG["gemma"] == []


@pytest.mark.asyncio
async def test_get_models_returns_static_for_claude():
    models = await get_models("claude")
    assert len(models) >= 2
    assert all(isinstance(m, ModelEntry) for m in models)


@pytest.mark.asyncio
async def test_get_models_returns_static_for_zai():
    models = await get_models("zai")
    assert any(m.id == "glm-5.1" for m in models)
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_catalog.py -v
```
Expected: `ImportError: cannot import name 'DEFAULT_PROVIDERS'`

- [ ] **Step 3: Implement catalog.py**

Create `tero2/providers/catalog.py`:

```python
"""Dynamic + static model catalog for all supported providers."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_DIR = Path.home() / ".tero2" / "cache"
_CACHE_TTL_S = 3600  # 1 hour


@dataclass(frozen=True)
class ModelEntry:
    id: str
    label: str


DEFAULT_PROVIDERS: list[str] = [
    "claude", "codex", "opencode", "kilo", "zai", "gemma"
]

STATIC_CATALOG: dict[str, list[ModelEntry]] = {
    "claude": [
        ModelEntry(id="sonnet", label="Claude Sonnet"),
        ModelEntry(id="opus", label="Claude Opus"),
        ModelEntry(id="haiku", label="Claude Haiku"),
    ],
    "codex": [
        ModelEntry(id="", label="gpt-codex (medium, default)"),
        ModelEntry(id="gpt-5.4", label="gpt-5.4 (high reasoning)"),
    ],
    "zai": [
        ModelEntry(id="glm-5.1", label="GLM-5.1 (native)"),
    ],
    "gemma": [],   # in development
    "opencode": [],  # dynamic only
    "kilo": [],      # dynamic only
}

_DYNAMIC_PROVIDERS = {"opencode", "kilo"}


def _humanize(model_id: str) -> str:
    label = model_id
    for prefix in ("openrouter/", "anthropic/", "google/", "meta-llama/"):
        label = label.removeprefix(prefix)
    return label.capitalize()


async def fetch_cli_models(
    cli_name: str,
    provider_filter: str | None = None,
    free_only: bool = False,
    refresh: bool = False,
) -> list[ModelEntry]:
    # Uses create_subprocess_exec (not shell=True) — no injection risk.
    cmd = [cli_name, "models"]
    if provider_filter:
        cmd.append(provider_filter)
    if refresh:
        cmd.append("--refresh")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=15)
        if proc.returncode != 0:
            raise RuntimeError(f"{cli_name} models exited {proc.returncode}")
        entries = []
        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            if free_only and ":free" not in line:
                continue
            entries.append(ModelEntry(id=line, label=_humanize(line)))
        return entries
    except (FileNotFoundError, asyncio.TimeoutError, RuntimeError) as e:
        log.warning("fetch_cli_models(%s) failed: %s — using static fallback", cli_name, e)
        return STATIC_CATALOG.get(cli_name, [])


def _cache_path(cli: str) -> Path:
    return _CACHE_DIR / f"{cli}_models.json"


def _load_cache(cli: str) -> list[ModelEntry] | None:
    p = _cache_path(cli)
    try:
        raw = json.loads(p.read_text())
        fetched_at = datetime.fromisoformat(raw["fetched_at"])
        age = (datetime.now(timezone.utc) - fetched_at).total_seconds()
        if age > _CACHE_TTL_S:
            return None
        return [ModelEntry(**e) for e in raw["entries"]]
    except (FileNotFoundError, KeyError, json.JSONDecodeError, ValueError):
        return None


def _save_cache(cli: str, entries: list[ModelEntry]) -> None:
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        p = _cache_path(cli)
        data = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "entries": [{"id": e.id, "label": e.label} for e in entries],
        }
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.replace(p)
    except OSError as e:
        log.warning("cache write failed for %s: %s", cli, e)


async def get_models(cli: str, free_only: bool = False) -> list[ModelEntry]:
    if cli not in _DYNAMIC_PROVIDERS:
        return STATIC_CATALOG.get(cli, [])
    cached = _load_cache(cli)
    if cached is not None:
        if free_only:
            return [m for m in cached if ":free" in m.id]
        return cached
    entries = await fetch_cli_models(cli, free_only=free_only)
    _save_cache(cli, entries)
    return entries
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_catalog.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/providers/catalog.py tests/test_catalog.py
git commit -m "add providers/catalog.py with static catalog and async dynamic fetch"
```

---

### Task 2: tero2/providers/zai.py — port from tero

**Files:**
- Create: `tero2/providers/zai.py`
- Modify: `tero2/providers/__init__.py`
- Create: `tests/test_zai_provider.py`

- [ ] **Step 1: Read source file to port**

Read `/Users/terobyte/Desktop/Projects/Active/tero/src/providers/zai.py` to understand the implementation.

- [ ] **Step 2: Write failing tests**

```python
# tests/test_zai_provider.py
import pytest
from unittest.mock import patch


def test_zai_provider_importable():
    from tero2.providers.zai import ZaiProvider
    assert ZaiProvider is not None


def test_zai_provider_check_ready_without_key(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    with patch("tero2.providers.zai._read_settings_key", return_value=None):
        from tero2.providers.zai import ZaiProvider
        provider = ZaiProvider.__new__(ZaiProvider)
        ready, msg = provider.check_ready()
        assert not ready
        assert "ZAI_API_KEY" in msg or "key" in msg.lower()


def test_zai_registered_in_registry():
    from tero2.providers.registry import _REGISTRY
    assert "zai" in _REGISTRY
```

- [ ] **Step 3: Run to verify failure**

```
pytest tests/test_zai_provider.py -v
```
Expected: `ImportError: cannot import name 'ZaiProvider'`

- [ ] **Step 4: Port zai.py from tero**

Copy `/Users/terobyte/Desktop/Projects/Active/tero/src/providers/zai.py` to `tero2/providers/zai.py`.

Apply these import replacements (exact search-and-replace):
- `from src.config import` → `from tero2.config import`
- `from src.constants import` → `from tero2.constants import`
- `from src.providers.base import` → `from tero2.providers.base import`

Add `check_ready()` method if not present:
```python
import os

def _read_settings_key() -> str | None:
    p = Path.home() / ".claude-zai" / "settings.json"
    try:
        import json
        data = json.loads(p.read_text())
        return data.get("api_key") or data.get("apiKey")
    except (FileNotFoundError, json.JSONDecodeError):
        return None

# inside ZaiProvider class:
def check_ready(self) -> tuple[bool, str]:
    key = os.environ.get("ZAI_API_KEY") or _read_settings_key()
    if not key:
        return False, "ZAI_API_KEY not set and ~/.claude-zai/settings.json not found"
    return True, ""
```

- [ ] **Step 5: Register ZaiProvider in providers/__init__.py**

Add to `tero2/providers/__init__.py`:
```python
from tero2.providers.zai import ZaiProvider
register("zai", ZaiProvider)
```

- [ ] **Step 6: Run tests to verify pass**

```
pytest tests/test_zai_provider.py -v
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add tero2/providers/zai.py tero2/providers/__init__.py tests/test_zai_provider.py
git commit -m "port ZaiProvider from tero, register in provider registry"
```

---

## Chunk 2: ModelPickScreen

### Task 3: tero2/tui/screens/model_pick.py

**Files:**
- Create: `tero2/tui/screens/model_pick.py`
- Create: `tests/test_model_pick.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_model_pick.py
import pytest
from textual.app import App
from textual.widgets import Label

from tero2.providers.catalog import ModelEntry
from tero2.tui.screens.model_pick import ModelPickScreen


_FAKE_MODELS = [
    ModelEntry(id="opus", label="Claude Opus"),
    ModelEntry(id="sonnet", label="Claude Sonnet"),
    ModelEntry(id="haiku", label="Claude Haiku"),
]


@pytest.mark.asyncio
async def test_model_pick_shows_entries():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        assert len(items) == 3


@pytest.mark.asyncio
async def test_model_pick_filter_reduces_list():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        await pilot.click("#model-search")
        await pilot.type("opus")
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        assert len(items) == 1


@pytest.mark.asyncio
async def test_model_pick_escape_returns_none():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert results == [None]


@pytest.mark.asyncio
async def test_model_pick_enter_returns_entry():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ModelPickScreen(cli_name="claude", role_name="builder", entries=_FAKE_MODELS)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert len(results) == 1
        assert results[0] is not None
        assert isinstance(results[0], ModelEntry)
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_model_pick.py -v
```
Expected: `ImportError: cannot import name 'ModelPickScreen'`

- [ ] **Step 3: Implement ModelPickScreen**

```python
# tero2/tui/screens/model_pick.py
"""ModelPickScreen — CLI → model selection with fuzzy filter."""

from __future__ import annotations

from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from tero2.providers.catalog import ModelEntry


class ModelPickScreen(ModalScreen[ModelEntry | None]):
    """Pick a model from a list with live search filter."""

    BINDINGS: ClassVar[list] = [
        Binding("escape,q", "cancel", "Отмена", show=False),
        Binding("/", "focus_search", "Поиск", show=False),
    ]

    def __init__(
        self,
        cli_name: str,
        role_name: str,
        entries: list[ModelEntry],
    ) -> None:
        super().__init__()
        self._cli_name = cli_name
        self._role_name = role_name
        self._all_entries = entries
        self._filtered = list(entries)

    def compose(self) -> ComposeResult:
        yield Static(
            f"Выбор модели для {self._role_name} ({self._cli_name})",
            classes="screen-title",
        )
        yield Input(placeholder="Поиск модели…", id="model-search")
        lv_items = [
            ListItem(
                Label(e.label, classes="model-label"),
                Label(e.id, classes="model-id"),
            )
            for e in self._filtered
        ]
        yield ListView(*lv_items, id="model-list")
        yield Footer()

    def on_input_changed(self, event: Input.Changed) -> None:
        q = event.value.lower()
        self._filtered = (
            [e for e in self._all_entries if q in e.id.lower() or q in e.label.lower()]
            if q
            else list(self._all_entries)
        )
        self._rebuild_list()

    def _rebuild_list(self) -> None:
        lv = self.query_one("#model-list", ListView)
        lv.clear()
        for e in self._filtered:
            lv.append(
                ListItem(
                    Label(e.label, classes="model-label"),
                    Label(e.id, classes="model-id"),
                )
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Public `.index` attribute — see M1 Task 6 note on why not `_index`.
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._filtered):
            self.dismiss(self._filtered[idx])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_focus_search(self) -> None:
        self.query_one("#model-search", Input).focus()
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_model_pick.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/tui/screens/model_pick.py tests/test_model_pick.py
git commit -m "add ModelPickScreen with live fuzzy filter"
```

---

## Chunk 3: RoleSwapScreen extension + Command Palette

### Task 4: Extend RoleSwapScreen with model selection (step 3)

**Files:**
- Modify: `tero2/tui/screens/role_swap.py`
- Modify: `tero2/tui/app.py` (update SwitchProviderMessage handler)
- Create: `tests/test_role_swap_m2.py`

**Current state:** 2-step flow: step 1 = pick role, step 2 = pick provider from `_KNOWN_PROVIDERS = ["claude", "codex", "opencode", "kilo"]`.

**M2 changes:**
1. Replace `_KNOWN_PROVIDERS` with `catalog.DEFAULT_PROVIDERS`
2. Mark `gemma` entries as disabled (CSS class)
3. Add step 3: after picking provider, push `ModelPickScreen` to pick model
4. `SwitchProviderMessage` gains `model: str` field

- [ ] **Step 1: Write failing tests**

```python
# tests/test_role_swap_m2.py
import pytest
from textual.app import App
from textual.widgets import Label


@pytest.mark.asyncio
async def test_role_swap_zai_appears_in_providers():
    from tero2.tui.screens.role_swap import RoleSwapScreen
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = RoleSwapScreen(roles=["builder"])
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        await pilot.press("enter")  # select first role → step 2
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        all_text = " ".join(
            str(item.query_one(Label).renderable)
            for item in items
        )
        assert "zai" in all_text.lower()


@pytest.mark.asyncio
async def test_role_swap_gemma_has_disabled_class():
    from tero2.tui.screens.role_swap import RoleSwapScreen
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = RoleSwapScreen(roles=["builder"])
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        await pilot.press("enter")
        await pilot.pause(0.1)
        disabled = screen.query(".provider-disabled")
        assert len(disabled) >= 1


@pytest.mark.asyncio
async def test_role_swap_switch_message_has_model():
    from unittest.mock import AsyncMock, patch
    from tero2.providers.catalog import ModelEntry
    from tero2.tui.screens.role_swap import RoleSwapScreen, SwitchProviderMessage

    fake_models = [ModelEntry(id="sonnet", label="Sonnet")]

    messages = []

    class _TestApp(App):
        def on_switch_provider_message(self, msg: SwitchProviderMessage) -> None:
            messages.append(msg)

    with patch("tero2.tui.screens.role_swap.get_models", new=AsyncMock(return_value=fake_models)):
        app = _TestApp()
        async with app.run_test(headless=True) as pilot:
            screen = RoleSwapScreen(roles=["builder"])
            await app.push_screen(screen, lambda x: None)
            await pilot.pause(0.1)
            await pilot.press("enter")   # select role
            await pilot.pause(0.1)
            await pilot.press("enter")   # select first provider (claude)
            await pilot.pause(0.2)
            await pilot.press("enter")   # select first model (sonnet)
            await pilot.pause(0.2)

    assert len(messages) >= 1
    assert hasattr(messages[0], "model")
    assert messages[0].model == "sonnet"
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_role_swap_m2.py -v
```
Expected: FAIL — `zai` not in providers (hardcoded 4-item list)

- [ ] **Step 3: Update role_swap.py**

Add imports at top of `tero2/tui/screens/role_swap.py`:
```python
from tero2.providers.catalog import DEFAULT_PROVIDERS, ModelEntry, get_models
```

Remove `_KNOWN_PROVIDERS` constant.

Update `SwitchProviderMessage` to carry `model`:
```python
class SwitchProviderMessage(Message):
    def __init__(self, role: str, provider: str, model: str = "") -> None:
        super().__init__()
        self.role = role
        self.provider = provider
        self.model = model
```

In `_enter_step2()`, replace hardcoded list with `DEFAULT_PROVIDERS`:
```python
def _enter_step2(self) -> None:
    self._step = 2
    lv = self.query_one("#provider-list", ListView)
    lv.clear()
    self._providers_order = list(DEFAULT_PROVIDERS)
    for p in self._providers_order:
        if p == "gemma":
            lv.append(ListItem(Label(f"{p}  (in development)", classes="provider-disabled")))
        else:
            lv.append(ListItem(Label(p)))
```

Update `on_list_view_selected` — branch for step 2 now dispatches to async handler via `run_worker` (step handlers can't be async directly, since Textual calls them synchronously). Use public `event.list_view.index`.

```python
def on_list_view_selected(self, event: ListView.Selected) -> None:
    idx = event.list_view.index
    if idx is None:
        return
    if self._step == 1:
        # existing step-1 logic: pick role → _enter_step2()
        if 0 <= idx < len(self._roles):
            self._selected_role = self._roles[idx]
            self._enter_step2()
    elif self._step == 2:
        if 0 <= idx < len(self._providers_order):
            provider = self._providers_order[idx]
            # Kick off async provider → models → ModelPickScreen.
            self.run_worker(
                self._handle_provider_selected(provider),
                exclusive=True,
            )


async def _handle_provider_selected(self, provider: str) -> None:
    if provider == "gemma":
        self.notify("gemma — in development", severity="warning")
        return
    models = await get_models(provider)
    from tero2.tui.screens.model_pick import ModelPickScreen

    def _on_model(entry: ModelEntry | None) -> None:
        if entry is not None:
            self.app.post_message(
                SwitchProviderMessage(
                    role=self._selected_role,
                    provider=provider,
                    model=entry.id,
                )
            )
            self.dismiss(None)

    self.app.push_screen(
        ModelPickScreen(cli_name=provider, role_name=self._selected_role, entries=models),
        _on_model,
    )
```

- [ ] **Step 4: Update app.py on_switch_provider_message to include model**

In `tero2/tui/app.py`, update handler:
```python
def on_switch_provider_message(self, msg: SwitchProviderMessage) -> None:
    self._command_queue.put_nowait(
        Command(
            "switch_provider",
            data={"role": msg.role, "provider": msg.provider, "model": msg.model},
            source="tui",
        )
    )
```

- [ ] **Step 5: Run all role_swap tests**

```
pytest tests/test_role_swap_m2.py tests/ -k "role_swap" -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/tui/screens/role_swap.py tero2/tui/app.py tests/test_role_swap_m2.py
git commit -m "extend RoleSwapScreen with model selection, use catalog.DEFAULT_PROVIDERS"
```

---

### Task 5: Command Palette — tero2/tui/commands.py

**Files:**
- Create: `tero2/tui/commands.py`
- Modify: `tero2/tui/app.py` (add COMMANDS class var)
- Create: `tests/test_commands_palette.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_commands_palette.py
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def test_command_provider_importable():
    from tero2.tui.commands import Tero2CommandProvider
    assert Tero2CommandProvider is not None


def test_dashboard_has_commands_class_var():
    from tero2.tui.app import DashboardApp
    from tero2.tui.commands import Tero2CommandProvider
    assert hasattr(DashboardApp, "COMMANDS")
    assert Tero2CommandProvider in DashboardApp.COMMANDS


@pytest.mark.asyncio
async def test_command_palette_opens_with_ctrl_p():
    from tero2.tui.app import DashboardApp
    runner = MagicMock()
    runner.config.roles = {}
    runner.run = AsyncMock()
    dispatcher = MagicMock()
    dispatcher.subscribe.return_value = asyncio.Queue()
    app = DashboardApp(runner=runner, dispatcher=dispatcher, command_queue=asyncio.Queue())
    async with app.run_test(headless=True) as pilot:
        await pilot.press("ctrl+p")
        await pilot.pause(0.2)
        from textual.command import CommandPalette
        assert any(isinstance(s, CommandPalette) for s in app.screen_stack)
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_commands_palette.py -v
```
Expected: `ImportError: cannot import name 'Tero2CommandProvider'`

- [ ] **Step 3: Implement tero2/tui/commands.py**

```python
# tero2/tui/commands.py
"""Command Palette provider for DashboardApp (Ctrl+P)."""

from __future__ import annotations

from textual.command import Hit, Hits, Provider


class Tero2CommandProvider(Provider):
    """Provides tero2-specific commands for Textual Command Palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        commands = [
            ("Открыть новый проект", "new_project"),
            ("Сменить план", "change_plan"),
            ("Сменить провайдера роли", "roles"),
            ("Отправить указание агенту", "steer"),
            ("Настройки (глобальные)", "settings"),
            ("Пауза / возобновить", "pause"),
            ("Пропустить задачу", "skip"),
            ("Выход", "quit"),
        ]
        for label, action_name in commands:
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    lambda a=action_name: self.app.action(a),
                    help=f"tero2: {label}",
                )
```

- [ ] **Step 4: Add COMMANDS to DashboardApp in app.py**

In `tero2/tui/app.py`, add after BINDINGS:
```python
from tero2.tui.commands import Tero2CommandProvider

# inside DashboardApp:
COMMANDS: ClassVar[set] = {Tero2CommandProvider}
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/test_commands_palette.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/tui/commands.py tero2/tui/app.py tests/test_commands_palette.py
git commit -m "add Command Palette (Ctrl+P) with Tero2CommandProvider"
```

---

## Chunk 4: Styles + integration

### Task 6: styles.tcss — ModelPickScreen styles

**Files:**
- Modify: `tero2/tui/styles.tcss`

- [ ] **Step 1: Append new styles**

Add to `tero2/tui/styles.tcss`:
```css
/* ModelPickScreen */
ModelPickScreen {
    background: $surface;
    border: thick $primary;
    height: auto;
    max-height: 80%;
    width: 80%;
    margin: 2 4;
}

ModelPickScreen ListView {
    height: auto;
    max-height: 25;
}

ModelPickScreen .model-label {
    color: $text;
}

ModelPickScreen .model-id {
    color: $text-muted;
}

.provider-disabled {
    color: $text-muted;
    text-style: dim;
}
```

- [ ] **Step 2: Verify app still imports cleanly**

```
python -c "from tero2.tui.app import DashboardApp; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tero2/tui/styles.tcss
git commit -m "add ModelPickScreen and provider-disabled styles"
```

---

### Task 7: M2 Integration tests

**Files:**
- Create: `tests/test_m2_integration.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/test_m2_integration.py
"""M2 integration: catalog, zai, model pick, command palette."""
import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from tero2.providers.catalog import ModelEntry, STATIC_CATALOG, get_models


@pytest.mark.asyncio
async def test_get_models_claude_static():
    models = await get_models("claude")
    assert len(models) >= 2
    assert all(isinstance(m, ModelEntry) for m in models)


@pytest.mark.asyncio
async def test_get_models_zai_has_glm():
    models = await get_models("zai")
    assert any(m.id == "glm-5.1" for m in models)


@pytest.mark.asyncio
async def test_get_models_uses_cache_when_fresh(tmp_path, monkeypatch):
    monkeypatch.setattr("tero2.providers.catalog._CACHE_DIR", tmp_path)
    cache_file = tmp_path / "opencode_models.json"
    cache_file.write_text(json.dumps({
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "entries": [{"id": "openrouter/anthropic/claude-opus", "label": "Claude Opus"}],
    }))

    with patch("tero2.providers.catalog.fetch_cli_models") as mock_fetch:
        models = await get_models("opencode")
        mock_fetch.assert_not_called()
        assert len(models) == 1


def test_zai_in_registry():
    from tero2.providers.registry import _REGISTRY
    assert "zai" in _REGISTRY


def test_all_imports_succeed():
    """Verify no import errors across M2 modules."""
    from tero2.providers.catalog import get_models, STATIC_CATALOG  # noqa
    from tero2.providers.zai import ZaiProvider  # noqa
    from tero2.tui.screens.model_pick import ModelPickScreen  # noqa
    from tero2.tui.commands import Tero2CommandProvider  # noqa
```

- [ ] **Step 2: Run M2 integration tests**

```
pytest tests/test_m2_integration.py -v
```
Expected: all PASS

- [ ] **Step 3: Run M2 tests only**

```
pytest tests/test_catalog.py tests/test_zai_provider.py tests/test_model_pick.py tests/test_role_swap_m2.py tests/test_commands_palette.py tests/test_m2_integration.py -v --tb=short
```
Expected: all PASS

- [ ] **Step 4: Final M2 commit**

```bash
git add tests/test_m2_integration.py
git commit -m "m2 integration tests: catalog, zai, model pick, command palette"
```

---

## Summary

After M2 completion:
- Dynamic model catalog for opencode/kilo via async subprocess fetch with 1h TTL cache and static fallback
- Static catalog for claude/codex/zai; gemma shows as disabled with tooltip
- Native ZaiProvider registered — `check_ready()` reports missing key gracefully; zai/glm-5.1 via opencode also available
- ModelPickScreen with live fuzzy search handles 500+ models
- RoleSwapScreen extended to 3 steps: role → provider → model (with model in SwitchProviderMessage)
- Command Palette (Ctrl+P) with all DashboardApp actions
- All M1 tests continue passing

## Human QA — M2

After automated tasks complete:
- [ ] Run full regression suite: `pytest tests/ -v --tb=short`
- [ ] Open RoleSwap [r] → verify 3-step flow works (role → provider → model)
- [ ] Press Ctrl+P → verify Command Palette opens with all tero2 commands
- [ ] Verify gemma shows as "in development" in provider list

# M3 — Settings Screen & Project Wizard Step 3

**Role map:**
| Task | Role | Why |
|------|------|-----|
| 1 | builder | config changes |
| 2 | builder | CLI changes |
| 3 | builder | new utility |
| 4 | builder | new screen |
| 5 | builder | new screen + wizard update |
| 6 | architect | CSS |
| 7 | verifier | integration tests |

**Goal:** SettingsScreen with 3 tabs (Providers, Telegram, Behaviour), TelegramConfig.enabled field with legacy fallback, ProvidersPickScreen (wizard step 3 for new projects), SORA invariant validation, atomic config writes.

**Architecture:** New modules: `tero2/tui/screens/settings.py`, `tero2/tui/screens/providers_pick.py`. Modified: `tero2/config.py` (TelegramConfig.enabled + legacy fallback), `tero2/cli.py` (cmd_telegram guard), `tero2/tui/app.py` (action_settings wired), `tero2/tui/screens/startup_wizard.py` (add step 3). Requires M1 + M2 to be merged first.

**Tech Stack:** Python 3.11+, Textual ≥1.0, tomllib/tomli-w, pytest

---

## Chunk 1: Config changes

### Task 1: Add TelegramConfig.enabled + legacy fallback

**Files:**
- Modify: `tero2/config.py`
- Create: `tests/test_config_m3.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config_m3.py
import pytest
from tero2.config import TelegramConfig, _parse_config


def test_telegram_config_enabled_default():
    cfg = TelegramConfig()
    assert cfg.enabled is False


def test_telegram_config_enabled_explicit():
    cfg = TelegramConfig(enabled=True, bot_token="tok")
    assert cfg.enabled is True


def test_parse_config_legacy_fallback():
    """If enabled absent but bot_token present → enabled=True."""
    raw = {
        "telegram": {
            "bot_token": "tok:ABC",
            "chat_id": "123",
        }
    }
    config = _parse_config(raw)
    assert config.telegram is not None
    assert config.telegram.enabled is True


def test_parse_config_enabled_false_overrides_bot_token():
    """Explicit enabled=false beats non-empty bot_token."""
    raw = {
        "telegram": {
            "enabled": False,
            "bot_token": "tok:ABC",
        }
    }
    config = _parse_config(raw)
    assert config.telegram.enabled is False


def test_parse_config_no_telegram_section():
    config = _parse_config({})
    assert config.telegram is None or not getattr(config.telegram, "enabled", True)
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_config_m3.py -v
```
Expected: `TelegramConfig() has no field 'enabled'` or attribute error

- [ ] **Step 3: Update TelegramConfig in config.py**

In `tero2/config.py`, find `TelegramConfig` dataclass (around line 70-77) and add `enabled` field:
```python
@dataclass
class TelegramConfig:
    enabled: bool = False   # NEW — explicit opt-in
    bot_token: str = ""
    chat_id: str = ""
    heartbeat_interval_s: int = DEFAULT_HEARTBEAT_INTERVAL_S
    voice_on_done: bool = True
    voice_on_stuck: bool = True
    allowed_chat_ids: list[str] = field(default_factory=list)
```

In `_parse_config` (around line 168), find where `TelegramConfig` is constructed from raw TOML and add legacy fallback logic:
```python
# when parsing telegram section:
tg_raw = raw.get("telegram", {})
if tg_raw:
    # legacy fallback: if 'enabled' missing but bot_token present, treat as enabled
    if "enabled" not in tg_raw and tg_raw.get("bot_token"):
        tg_raw = {**tg_raw, "enabled": True}
    telegram = TelegramConfig(
        enabled=tg_raw.get("enabled", False),
        bot_token=tg_raw.get("bot_token", ""),
        # ... rest of existing fields
    )
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_config_m3.py -v
```
Expected: all PASS

- [ ] **Step 5: Run existing config tests to verify no regression**

```
pytest tests/test_config_mvp1.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/config.py tests/test_config_m3.py
git commit -m "add TelegramConfig.enabled with legacy fallback for missing field"
```

---

### Task 2: Update cmd_telegram guard in cli.py

**Files:**
- Modify: `tero2/cli.py`
- Create: `tests/test_cli_telegram_m3.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_cli_telegram_m3.py
import sys
from unittest.mock import MagicMock, patch
import pytest


def test_cmd_telegram_exits_when_disabled():
    """tero2 telegram refuses to start when enabled=False."""
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock(spec=Config)
    cfg.telegram = TelegramConfig(enabled=False, bot_token="tok:ABC")

    # NOTE: cmd_telegram uses `from tero2.config import load_config` INSIDE the
    # function body → patch the source module, not tero2.cli.
    with patch("tero2.config.load_config", return_value=cfg):
        from tero2.cli import cmd_telegram
        args = MagicMock()
        args.project = None
        args.verbose = False
        with pytest.raises(SystemExit) as exc:
            cmd_telegram(args)
        assert exc.value.code == 1


def test_cmd_telegram_exits_when_no_token():
    """tero2 telegram refuses when enabled=True but no token."""
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock(spec=Config)
    cfg.telegram = TelegramConfig(enabled=True, bot_token="")

    with patch("tero2.config.load_config", return_value=cfg):
        from tero2.cli import cmd_telegram
        args = MagicMock()
        args.project = None
        args.verbose = False
        with pytest.raises(SystemExit) as exc:
            cmd_telegram(args)
        assert exc.value.code == 1


def test_cmd_telegram_proceeds_when_enabled_with_token():
    """tero2 telegram proceeds when enabled=True and token set."""
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock(spec=Config)
    cfg.telegram = TelegramConfig(enabled=True, bot_token="tok:ABC", allowed_chat_ids=["123"])

    # TelegramInputBot is imported inside cmd_telegram too — patch source module.
    # asyncio.run is patched to avoid actually starting the bot's event loop.
    with patch("tero2.config.load_config", return_value=cfg), \
         patch("tero2.telegram_input.TelegramInputBot") as MockBot, \
         patch("tero2.cli.asyncio.run") as mock_run:
        from tero2.cli import cmd_telegram
        args = MagicMock()
        args.project = None
        args.verbose = False
        cmd_telegram(args)
        MockBot.assert_called_once_with(cfg)
        assert mock_run.called  # bot.start() was scheduled
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_cli_telegram_m3.py -v
```
Expected: FAIL — `cmd_telegram` doesn't check `enabled` yet

- [ ] **Step 3: Update cmd_telegram in cli.py**

Find `cmd_telegram` (around line 237-262) and replace the current guard:
```python
# OLD:
if not config.telegram or not config.telegram.bot_token:
    ...

# NEW:
if not config.telegram or not config.telegram.enabled:
    print("error: telegram disabled — enable via ~/.tero2/config.toml or SettingsScreen [o]")
    sys.exit(1)
if not config.telegram.bot_token:
    print("error: telegram bot_token not configured")
    sys.exit(1)
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_cli_telegram_m3.py -v
```
Expected: all PASS

- [ ] **Step 5: Run existing telegram tests**

```
pytest tests/test_telegram_input.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/cli.py tests/test_cli_telegram_m3.py
git commit -m "cmd_telegram checks enabled flag before starting bot"
```

---

## Chunk 2: SettingsScreen

### Task 3: Atomic TOML writer helper

**Files:**
- Create: `tero2/config_writer.py`
- Create: `tests/test_config_writer.py`

Note: Writing TOML requires a serializer. Check if `tomli_w` or `tomllib` (write side) is available. If not, use a simple manual TOML serializer for the subset of types used (str, bool, int, list[str]).

- [ ] **Step 1: Write failing tests**

```python
# tests/test_config_writer.py
from pathlib import Path
import pytest
from tero2.config_writer import write_global_config_section


def test_write_creates_file(tmp_path):
    target = tmp_path / "config.toml"
    write_global_config_section(target, "telegram", {"enabled": True, "bot_token": "tok"})
    assert target.exists()
    content = target.read_text()
    assert "[telegram]" in content
    assert "enabled = true" in content


def test_write_is_atomic(tmp_path):
    """No .tmp file left behind after write."""
    target = tmp_path / "config.toml"
    write_global_config_section(target, "roles.builder", {"provider": "claude", "model": "sonnet"})
    tmp_files = list(tmp_path.glob("*.tmp"))
    assert tmp_files == []


def test_write_preserves_other_sections(tmp_path):
    """Writing one section doesn't wipe other sections."""
    target = tmp_path / "config.toml"
    target.write_text('[sora]\nmax_slices = 10\n')
    write_global_config_section(target, "telegram", {"enabled": False})
    content = target.read_text()
    assert "[sora]" in content
    assert "max_slices" in content
    assert "[telegram]" in content


def test_write_nested_table_roundtrips(tmp_path):
    """Regression: nested sections must render as [a.b], not [b] — and re-reads
    cleanly after 5 consecutive writes (the ProvidersPickScreen case)."""
    import tomllib
    target = tmp_path / "config.toml"
    for role in ("builder", "architect", "scout", "verifier", "coach"):
        write_global_config_section(
            target, f"roles.{role}", {"provider": "claude", "model": "sonnet"}
        )
    parsed = tomllib.loads(target.read_text())
    assert set(parsed["roles"].keys()) == {
        "builder", "architect", "scout", "verifier", "coach"
    }
    assert parsed["roles"]["builder"]["provider"] == "claude"
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_config_writer.py -v
```
Expected: `ImportError: cannot import name 'write_global_config_section'`

- [ ] **Step 3: Implement config_writer.py**

```python
# tero2/config_writer.py
"""Atomic TOML section writer for global config."""

from __future__ import annotations

from pathlib import Path

try:
    import tomli_w as _tomli_w
    _HAS_TOMLI_W = True
except ImportError:
    _HAS_TOMLI_W = False

try:
    import tomllib as _tomllib
except ImportError:
    import tomli as _tomllib  # type: ignore[no-redef]


def _load_toml(path: Path) -> dict:
    try:
        return _tomllib.loads(path.read_text())
    except FileNotFoundError:
        return {}


def _serialize_toml(data: dict) -> str:
    if _HAS_TOMLI_W:
        return _tomli_w.dumps(data)
    return _simple_toml_dumps(data)


def _simple_toml_dumps(data: dict, prefix: str = "") -> str:
    """Fallback TOML writer.

    ⚠ CRITICAL: must pass the fully-qualified table name as `prefix` to
    recursive calls. Otherwise nested tables render as ``[b]`` instead of
    ``[a.b]`` — each ``write_global_config_section("roles.builder", …)`` call
    would then corrupt the file. Install tomli-w to avoid this path entirely.
    """
    lines: list[str] = []
    tables: list[tuple[str, dict]] = []
    for k, v in data.items():
        full_key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            tables.append((full_key, v))
        elif isinstance(v, bool):
            lines.append(f"{k} = {'true' if v else 'false'}")
        elif isinstance(v, str):
            lines.append(f'{k} = "{v}"')
        elif isinstance(v, list):
            items = ", ".join(f'"{i}"' for i in v)
            lines.append(f"{k} = [{items}]")
        else:
            lines.append(f"{k} = {v}")
    result = "\n".join(lines)
    for tname, tdata in tables:
        # NOTE: pass `prefix=tname` so the next level emits [a.b.c] correctly.
        result += f"\n\n[{tname}]\n" + _simple_toml_dumps(tdata, prefix=tname)
    return result


def write_global_config_section(config_path: Path, section: str, values: dict) -> None:
    """Atomically update one section in a TOML file, preserving all other sections."""
    existing = _load_toml(config_path)
    # Navigate/create nested section path
    parts = section.split(".")
    target = existing
    for part in parts[:-1]:
        target = target.setdefault(part, {})
    target[parts[-1]] = values

    content = _serialize_toml(existing)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = config_path.with_suffix(".tmp")
    tmp.write_text(content)
    tmp.replace(config_path)
```

- [ ] **Step 4: Run tests to verify pass**

```
pytest tests/test_config_writer.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add tero2/config_writer.py tests/test_config_writer.py
git commit -m "add atomic TOML section writer for settings persistence"
```

---

### Task 4: SettingsScreen — 3 tabs

**Files:**
- Create: `tero2/tui/screens/settings.py`
- Create: `tests/test_settings_screen.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_settings_screen.py
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from textual.app import App

from tero2.tui.screens.settings import SettingsScreen


def _make_app():
    app = App()
    return app


@pytest.mark.asyncio
async def test_settings_screen_composes():
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=Path("/tmp/test_settings.toml"))
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        from textual.widgets import TabbedContent
        app.query_one(TabbedContent)  # must exist


@pytest.mark.asyncio
async def test_settings_has_three_tabs():
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=Path("/tmp/test_settings.toml"))
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        from textual.widgets import Tab
        tabs = screen.query(Tab)
        assert len(tabs) >= 3


@pytest.mark.asyncio
async def test_settings_escape_dismisses(tmp_path):
    results = []
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=tmp_path / "config.toml")
        await app.push_screen(screen, results.append)
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert len(results) == 1  # dismissed


@pytest.mark.asyncio
async def test_settings_save_writes_toml(tmp_path):
    app = _make_app()
    async with app.run_test(headless=True) as pilot:
        screen = SettingsScreen(config_path=tmp_path / "config.toml")
        await app.push_screen(screen, lambda x: None)
        await pilot.pause(0.1)
        await pilot.press("s")
        await pilot.pause(0.1)
        assert (tmp_path / "config.toml").exists()
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_settings_screen.py -v
```
Expected: `ImportError: cannot import name 'SettingsScreen'`

- [ ] **Step 3: Implement SettingsScreen**

```python
# tero2/tui/screens/settings.py
"""SettingsScreen — global ~/.tero2/config.toml editor (3 tabs)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import (
    Checkbox,
    Footer,
    Input,
    Label,
    Static,
    Tab,
    TabbedContent,
    TabPane,
)

from tero2.config_writer import write_global_config_section

_GLOBAL_CONFIG = Path.home() / ".tero2" / "config.toml"


class SettingsScreen(ModalScreen[None]):
    """Global settings editor. Writes to ~/.tero2/config.toml on [s]."""

    BINDINGS: ClassVar[list] = [
        Binding("s", "save", "Сохранить"),
        Binding("b,escape,q", "cancel", "Закрыть", show=False),
    ]

    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self._config_path = config_path or _GLOBAL_CONFIG

    def compose(self) -> ComposeResult:
        yield Static(
            f"tero2 — настройки (глобальные {self._config_path})",
            classes="screen-title",
        )
        with TabbedContent():
            with TabPane("Провайдеры", id="tab-providers"):
                yield Label("Глобальные настройки провайдеров применяются к новым проектам.")
                yield Label("Используйте [r] в Dashboard для смены провайдера текущего запуска.")

            with TabPane("Telegram", id="tab-telegram"):
                yield Label("Telegram-бот:", classes="field-label")
                yield Checkbox("Включить", id="tg-enabled")
                yield Label("Bot token:", classes="field-label")
                yield Input(placeholder="bot:TOKEN", id="tg-token", password=True)
                yield Label("Разрешённые chat_id:", classes="field-label")
                yield Input(placeholder="614473938", id="tg-chat-ids")
                yield Checkbox("Голосовое уведомление при завершении", id="tg-voice", value=True)
                yield Static(
                    "Бот принимает .md-планы и текстовые сообщения.\n"
                    "НЕ чат с агентом — только входящий канал.",
                    classes="info-msg",
                )

            with TabPane("Поведение", id="tab-behaviour"):
                yield Label("Максимум слайсов:", classes="field-label")
                yield Input(placeholder="12", id="max-slices")
                yield Label("Idle timeout (сек, 0=выкл):", classes="field-label")
                yield Input(placeholder="0", id="idle-timeout")

        yield Footer()

    def action_save(self) -> None:
        try:
            self._do_save()
            self.notify("Настройки сохранены", severity="information")
        except Exception as e:
            self.notify(f"Ошибка сохранения: {e}", severity="error")

    def _do_save(self) -> None:
        # Telegram section
        try:
            enabled_cb = self.query_one("#tg-enabled", Checkbox)
            token_in = self.query_one("#tg-token", Input)
            chat_ids_in = self.query_one("#tg-chat-ids", Input)
            voice_cb = self.query_one("#tg-voice", Checkbox)
            write_global_config_section(self._config_path, "telegram", {
                "enabled": enabled_cb.value,
                "bot_token": token_in.value,
                "allowed_chat_ids": [
                    c.strip() for c in chat_ids_in.value.split(",") if c.strip()
                ],
                "voice_on_done": voice_cb.value,
            })
        except Exception:
            pass  # optional section

        # Behaviour section
        try:
            max_slices_in = self.query_one("#max-slices", Input)
            idle_in = self.query_one("#idle-timeout", Input)
            sora_data: dict = {}
            if max_slices_in.value.isdigit():
                sora_data["max_slices"] = int(max_slices_in.value)
            if idle_in.value.isdigit():
                sora_data["idle_timeout_s"] = int(idle_in.value)
            if sora_data:
                write_global_config_section(self._config_path, "sora", sora_data)
        except Exception:
            pass

    def action_cancel(self) -> None:
        self.dismiss(None)
```

- [ ] **Step 4: Wire action_settings in app.py**

In `tero2/tui/app.py`, replace the stub:
```python
def action_settings(self) -> None:
    from tero2.tui.screens.settings import SettingsScreen
    self.push_screen(SettingsScreen())
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/test_settings_screen.py -v
```
Expected: all PASS

- [ ] **Step 6: Commit**

```bash
git add tero2/tui/screens/settings.py tero2/tui/app.py tests/test_settings_screen.py
git commit -m "add SettingsScreen with 3 tabs, wire [o] action"
```

---

## Chunk 3: ProvidersPickScreen + Wizard step 3

### Task 5: ProvidersPickScreen (optional wizard step 3)

**Files:**
- Create: `tero2/tui/screens/providers_pick.py`
- Modify: `tero2/tui/screens/startup_wizard.py` (add step 3 conditional)
- Create: `tests/test_providers_pick.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_providers_pick.py
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from textual.app import App

from tero2.tui.screens.providers_pick import ProvidersPickScreen


@pytest.mark.asyncio
async def test_providers_pick_shows_roles():
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ProvidersPickScreen(project_path=Path("/tmp/test-proj"))
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        items = screen.query("ListView ListItem")
        # builder, architect, scout, verifier, coach = 5 default roles
        assert len(items) >= 4


@pytest.mark.asyncio
async def test_providers_pick_save_writes_config(tmp_path):
    results = []
    (tmp_path / ".sora").mkdir()
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ProvidersPickScreen(project_path=tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        await pilot.press("s")
        await pilot.pause(0.1)
        assert (tmp_path / ".sora" / "config.toml").exists()


@pytest.mark.asyncio
async def test_providers_pick_sora_invariant_blocks_save(tmp_path):
    """If builder present but architect/verifier missing, save must fail."""
    results = []
    app = App()
    async with app.run_test(headless=True) as pilot:
        screen = ProvidersPickScreen(project_path=tmp_path)
        await app.push_screen(screen, results.append)
        await pilot.pause(0.1)
        # Remove architect/verifier from screen state
        screen._roles = {"builder": ("claude", "sonnet")}
        await pilot.press("s")
        await pilot.pause(0.1)
        # Save should show error, not write file
        assert not (tmp_path / ".sora" / "config.toml").exists()
```

- [ ] **Step 2: Run to verify failure**

```
pytest tests/test_providers_pick.py -v
```
Expected: `ImportError: cannot import name 'ProvidersPickScreen'`

- [ ] **Step 3: Implement ProvidersPickScreen**

```python
# tero2/tui/screens/providers_pick.py
"""ProvidersPickScreen — wizard step 3 for new projects (no .sora/config.toml)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Footer, Label, ListItem, ListView, Static

from tero2.config_writer import write_global_config_section

_SORA_CONFIG_PATH = ".sora/config.toml"
_DEFAULT_ROLES: dict[str, tuple[str, str]] = {
    "builder": ("claude", "sonnet"),
    "architect": ("claude", "opus"),
    "scout": ("codex", ""),
    "verifier": ("claude", "sonnet"),
    "coach": ("claude", "opus"),
}
_ROLE_LABELS = {
    "builder": "Строитель",
    "architect": "Архитектор",
    "scout": "Разведчик",
    "verifier": "Проверяющий",
    "coach": "Коуч",
}
_SORA_REQUIRES = {"architect", "verifier"}


class ProvidersPickScreen(ModalScreen[bool]):
    """Configure providers for a new project. Returns True if saved."""

    BINDINGS: ClassVar[list] = [
        Binding("s", "save", "Сохранить и запустить"),
        Binding("b,escape", "back", "Назад", show=False),
    ]

    def __init__(self, project_path: Path) -> None:
        super().__init__()
        self._project_path = project_path
        self._roles: dict[str, tuple[str, str]] = dict(_DEFAULT_ROLES)

    def compose(self) -> ComposeResult:
        yield Static(
            f"tero2 — провайдеры для нового проекта {self._project_path.name}",
            classes="screen-title",
        )
        items = []
        for role_id, (provider, model) in self._roles.items():
            label = _ROLE_LABELS.get(role_id, role_id)
            model_display = model or "(по умолчанию)"
            items.append(
                ListItem(
                    Label(label, classes="role-name"),
                    Label(f"{provider}  ({model_display})", classes="provider-model"),
                )
            )
        yield ListView(*items, id="roles-list")
        yield Checkbox("Сохранить как глобальный default", id="save-global")
        yield Footer()

    def action_save(self) -> None:
        if not self._validate_sora():
            self.notify(
                "Ошибка: если есть builder, нужны architect и verifier",
                severity="error",
            )
            return
        try:
            self._write_project_config()
            if self.query_one("#save-global", Checkbox).value:
                self._write_global_config()
            self.dismiss(True)
        except OSError as e:
            self.notify(f"Ошибка записи: {e}", severity="error")

    def _validate_sora(self) -> bool:
        role_ids = set(self._roles.keys())
        if "builder" in role_ids and not _SORA_REQUIRES.issubset(role_ids):
            return False
        return True

    def _write_project_config(self) -> None:
        config_path = self._project_path / _SORA_CONFIG_PATH
        config_path.parent.mkdir(parents=True, exist_ok=True)
        for role_id, (provider, model) in self._roles.items():
            write_global_config_section(config_path, f"roles.{role_id}", {
                "provider": provider,
                "model": model,
            })

    def _write_global_config(self) -> None:
        global_path = Path.home() / ".tero2" / "config.toml"
        for role_id, (provider, model) in self._roles.items():
            write_global_config_section(global_path, f"roles.{role_id}", {
                "provider": provider,
                "model": model,
            })

    def action_back(self) -> None:
        self.dismiss(False)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        role_ids = list(self._roles.keys())
        idx = event.list_view.index  # public attribute; see M1 Task 6 note.
        if idx is None or not (0 <= idx < len(role_ids)):
            return
        role_id = role_ids[idx]
        provider, model = self._roles[role_id]
            # Open ModelPickScreen to change provider+model
            from tero2.providers.catalog import STATIC_CATALOG, DEFAULT_PROVIDERS, get_models
            from tero2.tui.screens.model_pick import ModelPickScreen
            # For simplicity: cycle through static providers (full async in M2 style)
            self.notify(f"Смена провайдера для {role_id} — нажмите [r] в Dashboard для runtime.")
```

- [ ] **Step 4: Add wizard step 3 to startup_wizard.py**

In `tero2/tui/screens/startup_wizard.py`, update `_on_project_picked` callback chain to conditionally show `ProvidersPickScreen`:
```python
def _on_plan_picked(self, project_path: Path, plan_file: Path | None) -> None:
    sora_config = project_path / ".sora" / "config.toml"
    if sora_config.exists():
        # project already configured → skip providers step
        self.dismiss((project_path, plan_file))
    else:
        from tero2.tui.screens.providers_pick import ProvidersPickScreen

        def _on_providers(saved: bool) -> None:
            self.dismiss((project_path, plan_file))

        self.app.push_screen(ProvidersPickScreen(project_path), _on_providers)
```

Update `_on_project_picked` to pass `project_path` into the plan-step callback:
```python
def _on_project_picked(self, project_path: Path | None) -> None:
    if project_path is None:
        self.dismiss(None)
        return
    self.app.push_screen(
        PlanPickScreen(project_path),
        lambda plan: self._on_plan_picked(project_path, plan),
    )
```

- [ ] **Step 5: Run tests to verify pass**

```
pytest tests/test_providers_pick.py -v
```
Expected: all PASS

- [ ] **Step 6: Run startup wizard tests to verify no regression**

```
pytest tests/test_startup_wizard.py -v
```
Expected: all PASS

- [ ] **Step 7: Commit**

```bash
git add tero2/tui/screens/providers_pick.py tero2/tui/screens/startup_wizard.py tests/test_providers_pick.py
git commit -m "add ProvidersPickScreen and wire as optional wizard step 3"
```

---

## Chunk 4: Styles + final integration

### Task 6: styles.tcss — Settings and ProvidersPickScreen styles

**Files:**
- Modify: `tero2/tui/styles.tcss`

- [ ] **Step 1: Append new styles**

Add to `tero2/tui/styles.tcss`:
```css
/* SettingsScreen */
SettingsScreen {
    background: $surface;
    border: thick $primary;
    height: auto;
    max-height: 90%;
    width: 85%;
    margin: 1 2;
}

SettingsScreen .field-label {
    color: $text-muted;
    margin-top: 1;
}

/* ProvidersPickScreen */
ProvidersPickScreen {
    background: $surface;
    border: thick $primary;
    height: auto;
    max-height: 80%;
    width: 80%;
    margin: 2 4;
}

ProvidersPickScreen .role-name {
    color: $text;
    width: 15;
}

ProvidersPickScreen .provider-model {
    color: $text-muted;
}
```

- [ ] **Step 2: Verify app still imports cleanly**

```
python -c "from tero2.tui.app import DashboardApp; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add tero2/tui/styles.tcss
git commit -m "add SettingsScreen and ProvidersPickScreen styles"
```

---

### Task 7: M3 Integration + full regression test

**Files:**
- Create: `tests/test_m3_integration.py`

- [ ] **Step 1: Write integration tests**

```python
# tests/test_m3_integration.py
"""M3 integration: settings, config enabled, wizard step 3, atomic write."""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def test_settings_screen_importable():
    from tero2.tui.screens.settings import SettingsScreen
    assert SettingsScreen is not None


def test_providers_pick_importable():
    from tero2.tui.screens.providers_pick import ProvidersPickScreen
    assert ProvidersPickScreen is not None


def test_telegram_config_enabled_field():
    from tero2.config import TelegramConfig
    cfg = TelegramConfig()
    assert hasattr(cfg, "enabled")
    assert cfg.enabled is False


def test_legacy_telegram_fallback():
    from tero2.config import _parse_config
    raw = {"telegram": {"bot_token": "tok:XYZ"}}
    config = _parse_config(raw)
    assert config.telegram.enabled is True


def test_cmd_telegram_respects_enabled(tmp_path):
    from tero2.config import TelegramConfig, Config
    cfg = MagicMock()
    cfg.telegram = TelegramConfig(enabled=False, bot_token="tok:ABC")
    # Patch source module — cmd_telegram does `from tero2.config import load_config` inside.
    with patch("tero2.config.load_config", return_value=cfg):
        from tero2.cli import cmd_telegram
        import types
        args = types.SimpleNamespace(project=None, verbose=False)
        import sys
        with pytest.raises(SystemExit) as exc:
            cmd_telegram(args)
        assert exc.value.code == 1


def test_atomic_write_no_tmp_file(tmp_path):
    from tero2.config_writer import write_global_config_section
    target = tmp_path / "config.toml"
    write_global_config_section(target, "telegram", {"enabled": True})
    assert not list(tmp_path.glob("*.tmp"))
    assert target.exists()


def test_sora_invariant_in_providers_pick(tmp_path):
    from tero2.tui.screens.providers_pick import ProvidersPickScreen
    screen = ProvidersPickScreen.__new__(ProvidersPickScreen)
    screen._roles = {"builder": ("claude", "sonnet")}  # missing architect+verifier
    assert not screen._validate_sora()

    screen._roles = {"builder": ("claude", "sonnet"), "architect": ("claude", "opus"), "verifier": ("claude", "sonnet")}
    assert screen._validate_sora()


def test_all_m3_imports():
    from tero2.config_writer import write_global_config_section  # noqa
    from tero2.tui.screens.settings import SettingsScreen  # noqa
    from tero2.tui.screens.providers_pick import ProvidersPickScreen  # noqa
```

- [ ] **Step 2: Run M3 integration tests**

```
pytest tests/test_m3_integration.py -v
```
Expected: all PASS

- [ ] **Step 3: Run M3 tests only**

```
pytest tests/test_config_m3.py tests/test_cli_telegram_m3.py tests/test_config_writer.py tests/test_settings_screen.py tests/test_providers_pick.py tests/test_m3_integration.py -v --tb=short
```
Expected: all PASS

- [ ] **Step 4: Final M3 commit**

```bash
git add tests/test_m3_integration.py
git commit -m "m3 integration tests: settings, config enabled, wizard step 3, atomic write"
```

---

## Summary

After M3 completion (all milestones done):
- `SettingsScreen` ([o]) with 3 tabs: Providers (future runs), Telegram (enable/disable + token), Behaviour (max_slices, idle_timeout)
- Settings writes atomically to `~/.tero2/config.toml` on [s]; does NOT affect running Runner
- `TelegramConfig.enabled` field: explicit opt-in; legacy fallback (no `enabled` field + non-empty `bot_token` → `enabled=True`)
- `tero2 telegram` refuses to start when `enabled=False`, with clear error message pointing to SettingsScreen
- `ProvidersPickScreen` (wizard step 3): shown only when project has no `.sora/config.toml`; validates SORA invariant (builder requires architect+verifier) before saving
- Checkbox "Save as global default" copies provider settings to `~/.tero2/config.toml`
- Full end-to-end: `tero2 go` → wizard (project → plan → providers if new) → DashboardApp
- All M1 + M2 tests continue passing

## Human QA — M3

After automated tasks complete:
- [ ] Run full regression suite: `pytest tests/ -v --tb=short`
- [ ] Open Settings [o] → verify 3 tabs render correctly
- [ ] In Settings → Telegram tab → enable, enter token, save → verify `~/.tero2/config.toml` updated
- [ ] Run `tero2 telegram` → verify it refuses when `enabled=false`
- [ ] New project without `.sora/config.toml` → verify wizard shows step 3 (ProvidersPick)
- [ ] In ProvidersPick → remove architect/verifier → verify SORA invariant blocks save
