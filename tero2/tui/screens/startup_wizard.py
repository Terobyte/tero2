"""StartupWizard — multi-step wizard for no-args tero2 go."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Label

from tero2.tui.screens.plan_pick import PlanPickScreen
from tero2.tui.screens.project_pick import ProjectPickScreen


class StartupWizard(Screen[tuple[Path, Path | None] | None]):
    """Guides user through project + plan selection."""

    def compose(self) -> ComposeResult:
        yield Label("")  # placeholder, wizard uses push_screen

    def on_mount(self) -> None:
        self.app.push_screen(ProjectPickScreen(), self._on_project_picked)

    def _on_project_picked(self, project_path: Path | None) -> None:
        if project_path is None:
            self.dismiss(None)
            return
        self.app.push_screen(
            PlanPickScreen(project_path),
            lambda plan: self.dismiss((project_path, plan)),
        )
