"""RoleSwapScreen — two-step modal for switching a role's provider."""

from __future__ import annotations

from typing import ClassVar

from textual.message import Message
from textual.screen import Screen
from textual.widgets import Label, ListItem, ListView


_KNOWN_PROVIDERS: list[str] = ["claude", "codex", "opencode", "kilo"]


class SwitchProviderMessage(Message):
    """Posted when the user confirms a provider switch.

    Attributes:
        role: The role whose provider should change (e.g. ``"builder"``).
        provider: The new provider name (e.g. ``"claude"``).
    """

    def __init__(self, role: str, provider: str) -> None:
        super().__init__()
        self.role = role
        self.provider = provider


class RoleSwapScreen(Screen):
    """Full-screen modal: select a role then select a new provider.

    Step 1 — role selection: arrow keys / click to choose a role, Enter to
    confirm.  Press ``q`` or Escape to cancel and dismiss the screen.

    Step 2 — provider selection: same controls.  Confirming posts a
    :class:`SwitchProviderMessage` to the app and dismisses the screen.
    """

    DEFAULT_CSS: ClassVar[str] = """
    RoleSwapScreen {
        align: center middle;
    }
    RoleSwapScreen #rs-container {
        width: 50;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    RoleSwapScreen #rs-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    RoleSwapScreen #rs-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        ("escape", "cancel", "Отмена"),
        ("q", "cancel", "Отмена"),
    ]

    def __init__(self, roles: list[str] | None = None) -> None:
        super().__init__()
        self._roles: list[str] = roles or []
        self._selected_role: str | None = None
        self._step: int = 1  # 1 = choose role, 2 = choose provider

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self):  # type: ignore[override]
        from textual.containers import Vertical

        with Vertical(id="rs-container"):
            yield Label("Выберите роль:", id="rs-title")
            yield ListView(
                *[ListItem(Label(role), name=role) for role in self._roles],
                id="rs-list",
            )
            yield Label("[q] / Esc — отмена", id="rs-hint")

    # ── event handlers ───────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        if self._step == 1:
            self._selected_role = event.item.name
            self._enter_step2()
        else:
            provider = event.item.name
            if provider and self._selected_role:
                self.app.post_message(
                    SwitchProviderMessage(self._selected_role, provider)
                )
            self.dismiss()

    # ── actions ──────────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        """Cancel at any step and close the screen."""
        if self._step == 2:
            # Go back to step 1 instead of closing entirely
            self._enter_step1()
        else:
            self.dismiss()

    # ── internal ─────────────────────────────────────────────────────────────

    def _enter_step1(self) -> None:
        self._step = 1
        self._selected_role = None
        try:
            title = self.query_one("#rs-title", Label)
            title.update("Выберите роль:")
            lv = self.query_one("#rs-list", ListView)
            lv.clear()
            for role in self._roles:
                lv.append(ListItem(Label(role), name=role))
        except Exception:
            pass

    def _enter_step2(self) -> None:
        self._step = 2
        try:
            title = self.query_one("#rs-title", Label)
            title.update(f"Выберите провайдера для «{self._selected_role}»:")
            lv = self.query_one("#rs-list", ListView)
            lv.clear()
            for provider in _KNOWN_PROVIDERS:
                lv.append(ListItem(Label(provider), name=provider))
        except Exception:
            pass
