"""PlanPickScreen — wizard step 2: pick a plan file."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import Footer, Label, ListItem, ListView, Static

from tero2.constants import PROJECT_SCAN_SKIP_DIRS

_SKIP = PROJECT_SCAN_SKIP_DIRS | {".sora"}
_MAX_PLANS = 30


class PlanPickScreen(ModalScreen[Path | None]):
    """Pick a plan .md file from the project directory.

    Dismisses with the selected :class:`~pathlib.Path` on confirmation,
    or ``None`` if the user cancels or there are no .md files.
    """

    BINDINGS: ClassVar[list] = [
        Binding("i", "idle_mode", "Idle (без плана)"),
        Binding("b", "back", "Назад"),
        Binding("escape,q", "back", "Назад", show=False),
    ]

    def __init__(self, project_path: Path) -> None:
        super().__init__()
        self._project_path = project_path
        # Scan once; cache result. Index used in selection must match compose().
        try:
            self._files: list[Path] = self._scan_md_files()
        except OSError:
            self._files = []

    def _scan_md_files(self) -> list[Path]:
        files: list[Path] = []
        try:
            for p in self._project_path.rglob("*.md"):
                if not p.is_file():
                    continue
                try:
                    rel_parts = p.relative_to(self._project_path).parts
                except ValueError:
                    continue
                if any(part in _SKIP for part in rel_parts):
                    continue
                files.append(p)
        except PermissionError:
            pass
        def _mtime(p: Path) -> float:
            try:
                return p.stat().st_mtime
            except OSError:
                return 0.0
        files.sort(key=_mtime, reverse=True)
        return files[:_MAX_PLANS]

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield Static(
            f"tero2 — план для {self._project_path.name}",
            classes="screen-title",
        )
        if not self._files:
            yield Static("Нет .md файлов — запуск в idle-режиме…", classes="info-msg")
        else:
            items = [
                ListItem(
                    Label(p.name, classes="plan-name"),
                    Label(str(p.parent), classes="path-label"),
                )
                for p in self._files
            ]
            yield ListView(*items, id="plan-list")
        yield Footer()

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        # Kick off an async refresh to reload files if the initial scan
        # returned nothing. The async worker is responsible for dismissal;
        # don't schedule a second parallel dismissal here — two dismisses
        # race and the second raises ScreenStackError.
        self.run_worker(self._load_files(), exclusive=True)

    def _auto_idle(self) -> None:
        self.dismiss(None)

    async def _load_files(self) -> None:
        """Async loader: if no files found, dismiss from the event loop.

        The worker is launched via ``run_worker`` which runs the coroutine
        on the app's event loop. After ``asyncio.to_thread`` resumes we
        are back on the app thread, so ``call_from_thread`` would raise
        "must run in a different thread" — just call ``dismiss`` directly.
        """
        import asyncio
        self._files = await asyncio.to_thread(self._scan_md_files)
        if not self._files:
            if self.is_attached:
                self.dismiss(None)
            return

    # ── event handlers ───────────────────────────────────────────────────────

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        event.stop()
        idx = event.list_view.index  # public attr — avoids private _index
        if idx is not None and 0 <= idx < len(self._files):
            self.dismiss(self._files[idx])

    # ── actions ──────────────────────────────────────────────────────────────

    def action_idle_mode(self) -> None:
        """Dismiss without a plan (idle / no-plan mode)."""
        self.dismiss(None)

    def action_back(self) -> None:
        """Close the screen without selecting a plan."""
        self.dismiss(None)
