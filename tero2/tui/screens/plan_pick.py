"""PlanPickScreen — modal for selecting a plan file from the project directory."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.screen import Screen
from textual.widgets import Label, ListItem, ListView


class PlanPickScreen(Screen):
    """Full-screen modal listing .md files in *project_path* for plan selection.

    Dismisses with the selected :class:`~pathlib.Path` on confirmation,
    or ``None`` if the user cancels.
    """

    DEFAULT_CSS: ClassVar[str] = """
    PlanPickScreen {
        align: center middle;
    }
    PlanPickScreen #pp-container {
        width: 70;
        height: auto;
        max-height: 80vh;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    PlanPickScreen #pp-title {
        text-style: bold;
        color: $text;
        margin-bottom: 1;
    }
    PlanPickScreen #pp-hint {
        color: $text-muted;
        margin-top: 1;
    }
    """

    BINDINGS: ClassVar[list] = [
        ("escape", "cancel", "Отмена"),
        ("q", "cancel", "Отмена"),
    ]

    def __init__(self, project_path: str | Path) -> None:
        super().__init__()
        self._project_path = Path(project_path)
        self._plan_files: list[Path] = sorted(
            p for p in self._project_path.rglob("*.md") if p.is_file()
        )

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self):  # type: ignore[override]
        from textual.containers import Vertical

        items = (
            [
                ListItem(
                    Label(str(p.relative_to(self._project_path))),
                    name=str(p),
                )
                for p in self._plan_files
            ]
            if self._plan_files
            else [ListItem(Label("Нет .md файлов в проекте"), name="")]
        )

        with Vertical(id="pp-container"):
            yield Label("Выберите файл плана:", id="pp-title")
            yield ListView(*items, id="pp-list")
            yield Label("Enter — выбрать  |  Esc / q — отмена", id="pp-hint")

    # ── event handlers ───────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        name = event.item.name
        if name:
            self.dismiss(Path(name))
        else:
            self.dismiss(None)

    # ── actions ──────────────────────────────────────────────────────────────

    def action_cancel(self) -> None:
        """Close the screen without selecting a plan."""
        self.dismiss(None)
