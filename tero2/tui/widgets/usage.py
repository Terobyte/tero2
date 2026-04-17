"""Usage panel widget — ProgressBar per provider + session totals."""

from __future__ import annotations

from typing import Any, ClassVar

from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Label, ProgressBar, Static

# ── helpers ──────────────────────────────────────────────────────────────────

_NO_DATA_MSG = "Данные использования недоступны"


def _fmt_cost(cost: float) -> str:
    if cost < 0.01:
        return f"${cost:.4f}"
    return f"${cost:.2f}"


def _fmt_tokens(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    if tokens >= 1_000:
        return f"{tokens / 1_000:.1f}K"
    return str(tokens)


class _ProviderRow(Widget):
    """One provider row: label + progress bar."""

    DEFAULT_CSS: ClassVar[str] = """
    _ProviderRow {
        height: 3;
        layout: vertical;
    }
    _ProviderRow Label {
        height: 1;
    }
    _ProviderRow ProgressBar {
        height: 1;
    }
    """

    def __init__(self, provider_name: str, fraction: float, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._provider_name = provider_name
        self._fraction = max(0.0, min(1.0, fraction))

    def compose(self):  # type: ignore[override]
        pct = int(self._fraction * 100)
        yield Label(f"{self._provider_name}: {pct}%")
        bar = ProgressBar(total=100, show_eta=False, show_percentage=False)
        yield bar

    def on_mount(self) -> None:
        bar = self.query_one(ProgressBar)
        bar.advance(self._fraction * 100)

    def refresh_fraction(self, fraction: float) -> None:
        self._fraction = max(0.0, min(1.0, fraction))
        pct = int(self._fraction * 100)
        try:
            self.query_one(Label).update(f"{self._provider_name}: {pct}%")
            bar = self.query_one(ProgressBar)
            bar.progress = self._fraction * 100
        except Exception:
            pass


class UsagePanel(Widget):
    """Shows per-provider usage bars and session totals.

    Public API::

        panel.update_limits({"claude": 0.42, "gpt4": 0.78})
        panel.update_session(tracker.session_summary())

    The ``compact`` reactive collapses provider rows when ``True``
    (useful for medium-width layouts).
    """

    DEFAULT_CSS: ClassVar[str] = """
    UsagePanel {
        height: auto;
        border: solid $accent;
        padding: 0 1;
    }
    UsagePanel #usage-summary {
        color: $text-muted;
    }
    UsagePanel #no-data {
        color: $text-disabled;
    }
    """

    compact: reactive[bool] = reactive(False)

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self._limits: dict[str, float] = {}
        self._session: dict[str, Any] = {}
        self._rows: dict[str, _ProviderRow] = {}

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self):  # type: ignore[override]
        yield Static(_NO_DATA_MSG, id="no-data")
        yield Static("", id="usage-summary")

    # ── public API ───────────────────────────────────────────────────────────

    def update_limits(self, limits: dict[str, float]) -> None:
        """Update provider limit fractions (0.0–1.0 per provider)."""
        self._limits = dict(limits)
        self._sync_rows()

    def update_session(self, summary: dict[str, Any]) -> None:
        """Update session totals from ``UsageTracker.session_summary()``."""
        self._session = dict(summary)
        self._refresh_summary()

    # ── watchers ────────────────────────────────────────────────────────────

    def watch_compact(self, value: bool) -> None:  # noqa: FBT001
        self._sync_rows()

    # ── internal ────────────────────────────────────────────────────────────

    def _sync_rows(self) -> None:
        """Ensure _ProviderRow widgets match current _limits."""
        # hide/show no-data placeholder
        try:
            no_data = self.query_one("#no-data", Static)
            no_data.display = len(self._limits) == 0
        except Exception:
            pass

        if self.compact:
            # in compact mode remove all rows, show only summary
            for row in list(self._rows.values()):
                row.remove()
            self._rows.clear()
            return

        # remove rows that are no longer in limits
        for name in list(self._rows):
            if name not in self._limits:
                self._rows[name].remove()
                del self._rows[name]

        # add or update rows
        for name, fraction in self._limits.items():
            if name in self._rows:
                self._rows[name].refresh_fraction(fraction)
            else:
                row = _ProviderRow(name, fraction, id=f"provider-{name}")
                self._rows[name] = row
                try:
                    summary_widget = self.query_one("#usage-summary")
                    self.mount(row, before=summary_widget)
                except Exception:
                    self.mount(row)

    def _refresh_summary(self) -> None:
        try:
            summary_widget = self.query_one("#usage-summary", Static)
        except Exception:
            return

        total_tokens = self._session.get("total_tokens", 0)
        total_cost = self._session.get("total_cost", 0.0)

        if total_tokens == 0 and total_cost == 0.0:
            summary_widget.update("")
            return

        parts = [
            f"Сессия: {_fmt_tokens(total_tokens)} токенов",
            _fmt_cost(total_cost),
        ]
        summary_widget.update("  ".join(parts))
