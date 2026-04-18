"""Single-line hint widget shown during stuck state."""

from __future__ import annotations

from typing import ClassVar

from textual.widgets import Static


class StuckHintWidget(Static):
    """Shown above Footer when Runner is stuck. Hidden by default.

    Toggle via ``widget.display = True/False`` — Textual's built-in reactive
    that properly hides widget AND removes it from layout space.
    """

    DEFAULT_CSS: ClassVar[str] = """
    StuckHintWidget {
        height: 1;
        content-align: center middle;
        color: $warning;
    }
    """

    _HINT = "застряли — выбери: 1 retry  2 switch  3 skip  4 escalate  5 manual"

    def __init__(self, **kwargs) -> None:
        super().__init__(self._HINT, **kwargs)
        self.display = False
