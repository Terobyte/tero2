"""Pipeline panel widget — shows current SoraPhase, elapsed timer, per-role status."""

from __future__ import annotations

import time
from typing import ClassVar

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from tero2.state import SoraPhase

# ── localization ────────────────────────────────────────────────────────────

_PHASE_LABELS: dict[str, str] = {
    SoraPhase.NONE: "Ожидание",
    SoraPhase.HARDENING: "Закалка",
    SoraPhase.SCOUT: "Разведка",
    SoraPhase.COACH: "Коуч",
    SoraPhase.ARCHITECT: "Архитектор",
    SoraPhase.EXECUTE: "Выполнение",
    SoraPhase.SLICE_DONE: "Слайс завершён",
}

_ROLE_STATUS_DONE = "выполнено"
_ROLE_STATUS_ACTIVE = "активно"
_ROLE_STATUS_WAIT = "ожидание"

_STUCK_OPTIONS: list[str] = [
    "1. Увеличить температуру",
    "2. Откатить к чекпоинту",
    "3. Диверсификация",
    "4. Коуч",
    "5. Эскалация к человеку",
]

_ROLES_ORDER: list[str] = ["scout", "coach", "architect", "execute"]


class PipelinePanel(Widget):
    """Shows the current SORA pipeline phase, elapsed time, and per-role status.

    When ``stuck_mode`` reactive is set to ``True``, replaces the normal view
    with a stuck dialog showing the 5 recovery options.
    """

    DEFAULT_CSS: ClassVar[str] = """
    PipelinePanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    PipelinePanel .pipeline-title {
        text-style: bold;
        color: $text;
    }
    PipelinePanel .pipeline-phase {
        color: $success;
    }
    PipelinePanel .pipeline-elapsed {
        color: $text-muted;
    }
    PipelinePanel .role-done {
        color: $success;
    }
    PipelinePanel .role-active {
        color: $warning;
        text-style: bold;
    }
    PipelinePanel .role-wait {
        color: $text-disabled;
    }
    PipelinePanel .stuck-title {
        color: $error;
        text-style: bold;
    }
    PipelinePanel .stuck-option {
        color: $text;
    }
    """

    # ── reactives ───────────────────────────────────────────────────────────

    stuck_mode: reactive[bool] = reactive(False)
    sora_phase: reactive[str] = reactive(SoraPhase.NONE.value)

    # role → status string; updated via update_role_status()
    _role_statuses: dict[str, str]
    _start_time: float

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._role_statuses = {role: _ROLE_STATUS_WAIT for role in _ROLES_ORDER}
        self._start_time = time.monotonic()

    # ── compose ─────────────────────────────────────────────────────────────

    def compose(self):  # type: ignore[override]
        yield Static("", id="pipeline-content")

    def on_mount(self) -> None:
        self.set_interval(1, self._tick)
        self._refresh_content()

    # ── public API ──────────────────────────────────────────────────────────

    def update_phase(self, phase: SoraPhase) -> None:
        """Set the current SORA phase and reset timer."""
        self.sora_phase = phase.value
        self._start_time = time.monotonic()

    def update_role_status(self, role: str, status: str) -> None:
        """Update status for a single role.

        *status* should be one of: ``"выполнено"``, ``"активно"``, ``"ожидание"``.
        """
        self._role_statuses[role] = status
        self._refresh_content()

    # ── watchers ────────────────────────────────────────────────────────────

    def watch_stuck_mode(self, value: bool) -> None:  # noqa: FBT001
        self._refresh_content()

    def watch_sora_phase(self, value: str) -> None:
        self._refresh_content()

    # ── internal ────────────────────────────────────────────────────────────

    def _tick(self) -> None:
        if not self.stuck_mode:
            self._refresh_content()

    def _elapsed_str(self) -> str:
        secs = int(time.monotonic() - self._start_time)
        m, s = divmod(secs, 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h}ч {m:02d}м {s:02d}с"
        return f"{m:02}:{s:02}"

    def _refresh_content(self) -> None:
        try:
            content_widget = self.query_one("#pipeline-content", Static)
        except Exception:
            return

        if self.stuck_mode:
            lines = ["[bold red]⚠ Агент завис — выберите действие:[/bold red]", ""]
            lines.extend(f"  {opt}" for opt in _STUCK_OPTIONS)
            content_widget.update("\n".join(lines))
            return

        phase_label = _PHASE_LABELS.get(self.sora_phase, self.sora_phase)
        elapsed = self._elapsed_str()
        lines = [
            f"[bold]Фаза:[/bold] [green]{phase_label}[/green]  "
            f"[dim]⏱ {elapsed}[/dim]",
            "",
        ]
        for role in _ROLES_ORDER:
            status = self._role_statuses.get(role, _ROLE_STATUS_WAIT)
            if status == _ROLE_STATUS_ACTIVE:
                style = "yellow bold"
                marker = "▶"
            elif status == _ROLE_STATUS_DONE:
                style = "green"
                marker = "✓"
            else:
                style = "dim"
                marker = "·"
            lines.append(f"  [{style}]{marker} {role}: {status}[/{style}]")

        content_widget.update("\n".join(lines))
