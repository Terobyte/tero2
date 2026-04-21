"""ProjectPickScreen — wizard step 1: pick or enter project path."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from tero2.history import HistoryEntry, load_history


class ProjectPickScreen(ModalScreen[Path | None]):
    """Pick project from history or enter path manually."""

    BINDINGS: ClassVar[list] = [
        Binding("n", "manual_input", "Ввести путь"),
        Binding("d", "delete_entry", "Удалить"),
        Binding("escape,q", "cancel", "Выход", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[HistoryEntry] = load_history()
        self._manual_mode = False
        self._pending_delete: int | None = None

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
            self.notify("Папка не найдена", severity="warning")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        p = Path(event.value).expanduser().resolve()
        if p.is_dir():
            self.dismiss(p)
        else:
            self.notify("Папка не найдена", severity="error")

    def action_manual_input(self) -> None:
        try:
            self.query_one("#path-input")
        except NoMatches:
            self.mount(Input(placeholder="Путь к проекту…", id="path-input"))

    def action_delete_entry(self) -> None:
        try:
            lv = self.query_one("#project-list", ListView)
        except NoMatches:
            return
        idx = lv.index
        if idx is None or not (0 <= idx < len(self._entries)):
            return
        if self._pending_delete == idx:
            # Second press: confirm and delete
            self._entries.pop(idx)
            self._pending_delete = None
            from tero2.history import _write
            _write(self._entries)
            lv.clear()
            for item in self._build_items():
                lv.append(item)
            self.notify("Запись удалена", severity="information")
        else:
            self._pending_delete = idx
            entry = self._entries[idx]
            self.notify(
                f"Нажмите [d] ещё раз для подтверждения удаления «{entry.name}»",
                severity="warning",
            )

    def action_cancel(self) -> None:
        self.dismiss(None)
