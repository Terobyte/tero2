"""StartupWizard — multi-step wizard for no-args tero2 go."""

from __future__ import annotations

from pathlib import Path

from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import Label

from tero2.tui.screens.plan_pick import PlanPickScreen
from tero2.tui.screens.project_pick import ProjectPickScreen


class StartupWizard(Screen[tuple[Path, Path | None] | None]):
    """Guides user through project + plan + (optional) providers selection."""

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
            lambda plan: self._on_plan_picked(project_path, plan),
        )

    def _on_plan_picked(self, project_path: Path, plan_file: Path | None) -> None:
        if plan_file is None:
            self.dismiss(None)
            return
        sora_config = project_path / ".sora" / "config.toml"
        if sora_config.exists():
            # project already configured — skip providers step
            self.dismiss((project_path, plan_file))
        else:
            from tero2.tui.screens.providers_pick import ProvidersPickScreen

            def _on_providers(saved: bool) -> None:
                self.dismiss((project_path, plan_file))

            self.app.push_screen(ProvidersPickScreen(project_path), _on_providers)
