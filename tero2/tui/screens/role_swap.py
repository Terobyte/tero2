"""RoleSwapScreen — three-step modal for switching a role's provider and model."""

from __future__ import annotations

from typing import ClassVar

from textual.message import Message
from textual.screen import Screen
from textual.widgets import Label, ListItem, ListView

from tero2.providers.catalog import DEFAULT_PROVIDERS, ModelEntry, get_models


class SwitchProviderMessage(Message):
    """Posted when the user confirms a provider+model switch.

    Attributes:
        role: The role whose provider should change (e.g. ``"builder"``).
        provider: The new provider name (e.g. ``"claude"``).
        model: The new model id (e.g. ``"sonnet"``). Empty string = provider default.
    """

    def __init__(self, role: str, provider: str, model: str = "") -> None:
        super().__init__()
        self.role = role
        self.provider = provider
        self.model = model


class RoleSwapScreen(Screen):
    """Full-screen modal: select a role then select a new provider then a model.

    Step 1 — role selection: arrow keys / click to choose a role, Enter to
    confirm.  Press ``q`` or Escape to cancel and dismiss the screen.

    Step 2 — provider selection: same controls. ``gemma`` shown as disabled.

    Step 3 — model selection: pushed as ModelPickScreen overlay. Confirming
    posts a :class:`SwitchProviderMessage` to the app and dismisses the screen.
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
        self._step: int = 1  # 1 = choose role, 2 = choose provider, 3 = model (via push)
        self._providers_order: list[str] = []

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
        idx = event.list_view.index
        if idx is None:
            return
        if self._step == 1:
            if 0 <= idx < len(self._roles):
                self._selected_role = self._roles[idx]
                self._enter_step2()
        elif self._step == 2:
            if 0 <= idx < len(self._providers_order):
                provider = self._providers_order[idx]
                self.run_worker(
                    self._handle_provider_selected(provider),
                    exclusive=True,
                )

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
            lv.index = 0
        except Exception:
            pass

    def _enter_step2(self) -> None:
        self._step = 2
        self._providers_order = list(DEFAULT_PROVIDERS)
        try:
            title = self.query_one("#rs-title", Label)
            title.update(f"Выберите провайдера для «{self._selected_role}»:")
            lv = self.query_one("#rs-list", ListView)
            lv.clear()
            for p in self._providers_order:
                if p == "gemma":
                    lv.append(
                        ListItem(Label(f"{p}  (in development)", classes="provider-disabled"))
                    )
                else:
                    lv.append(ListItem(Label(p)))
            # After clear+append, index becomes None — reset to first item so
            # pressing Enter immediately selects without a prior cursor-down.
            lv.index = 0
        except Exception:
            pass

    async def _handle_provider_selected(self, provider: str) -> None:
        """Async handler: fetch models and push ModelPickScreen."""
        if provider == "gemma":
            self.notify("gemma — in development", severity="warning")
            return
        models = await get_models(provider)
        from tero2.tui.screens.model_pick import ModelPickScreen

        def _on_model(entry: ModelEntry | None) -> None:
            if entry is not None and self._selected_role:
                self.app.post_message(
                    SwitchProviderMessage(
                        role=self._selected_role,
                        provider=provider,
                        model=entry.id,
                    )
                )
                self.dismiss(None)

        self.app.push_screen(
            ModelPickScreen(
                cli_name=provider,
                role_name=self._selected_role or "",
                entries=models,
            ),
            _on_model,
        )
