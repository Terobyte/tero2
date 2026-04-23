"""ModelPickScreen — CLI -> model selection with fuzzy filter."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, ListItem, ListView, Static

from tero2.providers.catalog import ModelEntry

_DEBOUNCE_DELAY = 0.15  # seconds; avoids O(n) rebuild on every keystroke


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
        self._debounce_timer: Any = None

    def compose(self) -> ComposeResult:
        yield Static(
            f"Выбор модели для {self._role_name} ({self._cli_name})",
            classes="screen-title",
        )
        yield Input(placeholder="Поиск модели…", id="model-search")
        if not self._all_entries:
            yield Label("No models found — check provider configuration", classes="info-msg")
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
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
        self._debounce_timer = self.set_timer(
            _DEBOUNCE_DELAY,
            lambda: self._apply_filter(q),
        )

    def _apply_filter(self, q: str) -> None:
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
        # Reset cursor to first item — after clear(), index becomes None.
        if self._filtered:
            lv.index = 0

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        # Public `.index` attribute — avoids private `_index` usage.
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._filtered):
            self.dismiss(self._filtered[idx])

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_select_current(self) -> None:
        lv = self.query_one("#model-list", ListView)
        idx = lv.index
        if idx is not None and 0 <= idx < len(self._filtered):
            self.dismiss(self._filtered[idx])
        else:
            self.notify("No models available — refine your search", severity="warning")

    def on_mount(self) -> None:
        """Focus the list so Enter immediately selects without an extra Tab."""
        if self._filtered:
            self.query_one("#model-list", ListView).focus()

    def on_unmount(self) -> None:
        """Cancel any pending debounce timer to prevent callbacks on a destroyed screen."""
        if self._debounce_timer is not None:
            self._debounce_timer.stop()
            self._debounce_timer = None

    def action_focus_search(self) -> None:
        self.query_one("#model-search", Input).focus()
