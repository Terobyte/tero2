"""ProvidersPickScreen — wizard step 3 for new projects (no .sora/config.toml)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Footer, Label, ListItem, ListView, Static

from textual.css.query import NoMatches

from tero2.config_writer import write_global_config_section
from tero2.providers import catalog as _catalog

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
        Binding("b", "back", "Назад", show=False),
        Binding("escape", "back", "Назад", show=False),
    ]

    def __init__(self, project_path: Path) -> None:
        super().__init__()
        self._project_path = project_path
        self._roles: dict[str, tuple[str, str]] = dict(_DEFAULT_ROLES)
        self._step: int = 1
        self._active_role: str | None = None
        self._providers_order: list[str] = []

    def compose(self) -> ComposeResult:
        yield Static(
            f"tero2 — провайдеры для нового проекта {self._project_path.name}",
            id="pp-title",
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
        except OSError as e:
            self.notify(f"Ошибка записи конфига проекта: {e}", severity="error")
            return
        try:
            if self.query_one("#save-global", Checkbox).value:
                self._write_global_config()
        except NoMatches:
            pass
        except Exception as e:
            self.notify(f"Ошибка записи глобального конфига: {e}", severity="error")
            return
        self.dismiss(True)

    def _validate_sora(self) -> bool:
        role_ids = set(self._roles.keys())
        if "builder" in role_ids and not _SORA_REQUIRES.issubset(role_ids):
            return False
        return True

    def _write_project_config(self) -> None:
        config_path = self._project_path / _SORA_CONFIG_PATH
        config_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = config_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            for role_id, (provider, model) in self._roles.items():
                write_global_config_section(config_path, f"roles.{role_id}", {
                    "provider": provider,
                    "model": model,
                })
        finally:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)

    def _write_global_config(self) -> None:
        global_path = Path.home() / ".tero2" / "config.toml"
        for role_id, (provider, model) in self._roles.items():
            write_global_config_section(global_path, f"roles.{role_id}", {
                "provider": provider,
                "model": model,
            })

    def _enter_step2(self) -> None:
        """Enter provider selection step. Raises IndexError if no providers available."""
        self._step = 2
        self._providers_order = list(_catalog.DEFAULT_PROVIDERS)
        if not self._providers_order:
            raise IndexError(
                "_enter_step2: no providers available — lv.index = 0 would fail on empty ListView"
            )
        try:
            title = self.query_one("#pp-title", Static)
            title.update(f"Выберите провайдера для «{self._active_role}»:")
            lv = self.query_one("#roles-list", ListView)
        except NoMatches:
            return
        lv.clear()
        for p in self._providers_order:
            lv.append(ListItem(Label(p)))
        lv.index = 0
        lv.focus()

    def action_back(self) -> None:
        self.dismiss(False)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        role_ids = list(self._roles.keys())
        idx = event.list_view.index  # public attribute
        if idx is None or not (0 <= idx < len(role_ids)):
            return
        role_id = role_ids[idx]
        self._active_role = role_id
        self.run_worker(
            self._handle_provider_selected(role_id),
            exclusive=True,
        )

    async def _handle_provider_selected(self, role_id: str) -> None:
        """Fetch models for the selected role's provider and let user pick."""
        provider, _ = self._roles.get(role_id, ("claude", ""))
        from tero2.providers.catalog import get_models
        try:
            models = await get_models(provider)
        except Exception as exc:
            self.notify(f"Ошибка получения моделей: {exc}", severity="error")
            return
        if not models:
            self.notify(f"Нет доступных моделей для {provider}", severity="warning")
            return
        from tero2.tui.screens.model_pick import ModelPickScreen
        from tero2.providers.catalog import ModelEntry

        def _on_model(entry: ModelEntry | None) -> None:
            if entry is not None:
                provider_val, _ = self._roles.get(role_id, ("claude", ""))
                self._roles[role_id] = (provider_val, entry.id)

        self.app.push_screen(
            ModelPickScreen(
                cli_name=provider,
                role_name=role_id,
                entries=models,
            ),
            _on_model,
        )
