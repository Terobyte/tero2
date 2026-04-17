"""SteerScreen — single-input modal for sending a steering instruction."""

from __future__ import annotations

from typing import ClassVar

from textual.message import Message
from textual.screen import Screen
from textual.widgets import Input, Label


class SteerMessage(Message):
    """Posted when the user submits a steering instruction.

    Attributes:
        text: The instruction text entered by the user.
    """

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class SteerScreen(Screen):
    """Full-screen modal with a single text input for a steering instruction.

    Press Enter to submit (empty input cancels).  Press Escape to cancel.
    On submit, posts :class:`SteerMessage` to the app and dismisses.
    """

    DEFAULT_CSS: ClassVar[str] = """
    SteerScreen {
        align: center middle;
    }
    SteerScreen #steer-container {
        width: 60;
        height: auto;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    SteerScreen #steer-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    SteerScreen #steer-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        ("escape", "cancel", "Отмена"),
    ]

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self):  # type: ignore[override]
        from textual.containers import Vertical

        with Vertical(id="steer-container"):
            yield Label("Введите инструкцию для агента:", id="steer-title")
            yield Input(placeholder="Инструкция…", id="steer-input")
            yield Label("Enter — отправить  |  Esc — отмена", id="steer-hint")

    def on_mount(self) -> None:
        self.query_one("#steer-input", Input).focus()

    # ── event handlers ───────────────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        text = event.value.strip()
        if text:
            self.app.post_message(SteerMessage(text))
        self.dismiss()

    # ── actions ──────────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        """Close the screen without posting a message."""
        self.dismiss()
