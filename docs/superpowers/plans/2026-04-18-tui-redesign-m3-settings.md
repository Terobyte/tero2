# TUI Redesign M3 — Settings Screen & Project Wizard Step 3 Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

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

- [ ] **Step 3: Run full test suite (all 3 milestones)**

```
pytest tests/ -v --tb=short
```
Expected: all PASS — no regressions from M1 or M2

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
