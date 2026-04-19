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
        Binding("b", "back", "Назад", show=False),
        Binding("escape", "back", "Назад", show=False),
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
            try:
                if self.query_one("#save-global", Checkbox).value:
                    self._write_global_config()
            except Exception:
                pass
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
        idx = event.list_view.index  # public attribute
        if idx is None or not (0 <= idx < len(role_ids)):
            return
        role_id = role_ids[idx]
        # For now: notify user to use [r] in Dashboard for runtime provider change.
        self.notify(
            f"Смена провайдера для {role_id} — нажмите [r] в Dashboard для runtime."
        )
