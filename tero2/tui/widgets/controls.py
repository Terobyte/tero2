"""Controls panel widget — hotkey hints that change in stuck mode."""

from __future__ import annotations

from typing import ClassVar

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

# ── localization ─────────────────────────────────────────────────────────────

_NORMAL_HINTS = "[r] роли  [s] стир  [p] пауза  [q] выход  [k] пропустить"
_STUCK_HINTS = "[1-5] выбор опции  [q] отмена"


class ControlsPanel(Widget):
    """Displays hotkey hints at the bottom of the TUI.

    Set ``stuck_mode = True`` to switch to stuck-recovery hotkey hints.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ControlsPanel {
        height: 1;
        background: $panel;
        color: $text-muted;
    }
    ControlsPanel #controls-label {
        width: 1fr;
        text-align: center;
    }
    """

    stuck_mode: reactive[bool] = reactive(False)

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self):  # type: ignore[override]
        yield Static(_NORMAL_HINTS, id="controls-label")

    # ── watchers ─────────────────────────────────────────────────────────────

    def watch_stuck_mode(self, value: bool) -> None:  # noqa: FBT001
        try:
            label = self.query_one("#controls-label", Static)
            label.update(_STUCK_HINTS if value else _NORMAL_HINTS)
        except Exception:
            pass
