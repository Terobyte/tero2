# TUI Redesign M2 — Model Catalog & Provider Picker Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

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

- [ ] **Step 3: Run full test suite to verify no regressions**

```
pytest tests/ -v --tb=short
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
