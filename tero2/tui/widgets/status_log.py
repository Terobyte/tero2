"""Compact 4-line log for high-signal orchestration events.

Subscribes to ``EventDispatcher`` (NOT ``StreamBus``). Only surfaces the
small set of events that describe phase/state transitions; ignores the
high-volume per-step noise that flows into :class:`RoleStreamPanel` via
``StreamBus``.
"""

from __future__ import annotations

from textual.widgets import RichLog

from tero2.events import Event

_RENDERED_KINDS = frozenset(
    {"phase_change", "stuck", "done", "error", "escalation", "provider_switch"}
)


class StatusLog(RichLog):
    """4-line rolling log for orchestration-level events.

    Filters incoming :class:`~tero2.events.Event` by ``kind`` and renders
    matched events as ``[kind] key=value ...`` lines. Silently ignores
    event kinds outside :data:`_RENDERED_KINDS` so the log stays readable
    during active agent runs.
    """

    DEFAULT_CSS = """
    StatusLog {
        height: 6;
        border: round $accent;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(max_lines=4, markup=False, highlight=False, **kwargs)

    def push_event(self, event: Event) -> None:
        """Write *event* if its kind is rendered; otherwise drop silently.

        Named ``push_event`` (not ``on_event``) because Textual reserves the
        ``on_*`` prefix for its internal message system and would route
        ``Compose`` / ``Mount`` events here.
        """
        if event.kind not in _RENDERED_KINDS:
            return
        data_repr = " ".join(
            f"{k}={v}" for k, v in (event.data or {}).items()
        )
        line = f"[{event.kind}] {data_repr}".rstrip()
        self.write(line)
