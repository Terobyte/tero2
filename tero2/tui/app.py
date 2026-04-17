"""DashboardApp — main Textual TUI for tero2."""

from __future__ import annotations

import asyncio
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.worker import WorkerState

from tero2.events import Command, EventDispatcher
from tero2.runner import Runner
from tero2.state import SoraPhase
from tero2.tui.screens.role_swap import RoleSwapScreen, SwitchProviderMessage
from tero2.tui.screens.steer import SteerMessage, SteerScreen
from tero2.tui.widgets.controls import ControlsPanel
from tero2.tui.widgets.log_view import LogView
from tero2.tui.widgets.pipeline import PipelinePanel
from tero2.tui.widgets.usage import UsagePanel


class DashboardApp(App):
    """Main Textual TUI dashboard for tero2."""

    CSS_PATH = "styles.tcss"

    BINDINGS: ClassVar[list] = [
        ("r", "roles", "Роли"),
        ("s", "steer", "Стир"),
        ("p", "pause", "Пауза"),
        ("q", "quit", "Выход"),
        ("k", "skip", "Пропустить"),
        ("1", "stuck_option_1", ""),
        ("2", "stuck_option_2", ""),
        ("3", "stuck_option_3", ""),
        ("4", "stuck_option_4", ""),
        ("5", "stuck_option_5", ""),
    ]

    def __init__(
        self,
        runner: Runner,
        dispatcher: EventDispatcher,
        command_queue: asyncio.Queue[Command],
    ) -> None:
        super().__init__()
        self._runner = runner
        self._dispatcher = dispatcher
        self._command_queue = command_queue
        self._event_queue: asyncio.Queue | None = None
        self._runner_worker = None

    # ── compose ──────────────────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        yield PipelinePanel(id="pipeline")
        with Horizontal(id="main-row"):
            yield LogView(id="log-view")
            yield UsagePanel(id="usage-panel")
        yield ControlsPanel(id="controls")

    # ── lifecycle ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._event_queue = self._dispatcher.subscribe()
        self._runner_worker = self.run_worker(self._run_runner(), exclusive=True)
        self.run_worker(self._consume_events(), exclusive=False)

    # ── workers ──────────────────────────────────────────────────────────────

    async def _run_runner(self) -> None:
        """Run the runner and push a completion log message when done."""
        await self._runner.run()
        log_view = self.query_one("#log-view", LogView)
        log_view.push_message("Выполнение завершено.", style="green bold")

    async def _consume_events(self) -> None:
        """Drain the event queue and route events to widgets."""
        if self._event_queue is None:
            return
        while True:
            event = await self._event_queue.get()

            pipeline = self.query_one("#pipeline", PipelinePanel)
            log_view = self.query_one("#log-view", LogView)
            usage_panel = self.query_one("#usage-panel", UsagePanel)
            controls = self.query_one("#controls", ControlsPanel)

            # route by event kind
            if event.kind == "phase_change":
                sora_phase_val = event.data.get("sora_phase", SoraPhase.NONE.value)
                try:
                    phase = SoraPhase(sora_phase_val)
                except ValueError:
                    phase = SoraPhase.NONE
                pipeline.update_phase(phase)
                if event.role:
                    pipeline.update_role_status(event.role, "активно")

            elif event.kind == "step":
                if event.role:
                    pipeline.update_role_status(event.role, "активно")

            elif event.kind == "stuck":
                pipeline.stuck_mode = True
                controls.stuck_mode = True

            elif event.kind == "done":
                log_view.push_message("Задание выполнено.", style="green bold")
                pipeline.stuck_mode = False
                controls.stuck_mode = False

            elif event.kind == "error":
                msg = event.data.get("message") or event.data.get("msg") or "ошибка"
                log_view.push_message(f"Ошибка: {msg}", style="red bold")

            elif event.kind == "provider_switch":
                role = event.role or event.data.get("role", "")
                provider = event.data.get("provider", "")
                log_view.push_message(
                    f"Провайдер изменён: {role} → {provider}", style="cyan"
                )

            elif event.kind == "usage_update":
                usage_panel.update_limits(event.data.get("limits", {}))

            # ALL events go to log_view
            log_view.push_event(event)

    # ── actions ──────────────────────────────────────────────────────────────

    def action_roles(self) -> None:
        roles = list(self._runner.config.roles.keys())
        self.push_screen(RoleSwapScreen(roles=roles))

    def action_steer(self) -> None:
        self.push_screen(SteerScreen())

    def action_pause(self) -> None:
        self._command_queue.put_nowait(Command("pause", source="tui"))

    def action_skip(self) -> None:
        self._command_queue.put_nowait(Command("skip_task", source="tui"))

    def _clear_stuck_mode(self) -> None:
        pipeline = self.query_one("#pipeline", PipelinePanel)
        controls = self.query_one("#controls", ControlsPanel)
        pipeline.stuck_mode = False
        controls.stuck_mode = False

    def action_stuck_option_1(self) -> None:
        self._command_queue.put_nowait(
            Command("steer", data={"text": "stuck_option_1"}, source="tui")
        )
        self._clear_stuck_mode()

    def action_stuck_option_2(self) -> None:
        self._command_queue.put_nowait(
            Command("steer", data={"text": "stuck_option_2"}, source="tui")
        )
        self._clear_stuck_mode()

    def action_stuck_option_3(self) -> None:
        self._command_queue.put_nowait(
            Command("steer", data={"text": "stuck_option_3"}, source="tui")
        )
        self._clear_stuck_mode()

    def action_stuck_option_4(self) -> None:
        self._command_queue.put_nowait(
            Command("steer", data={"text": "stuck_option_4"}, source="tui")
        )
        self._clear_stuck_mode()

    def action_stuck_option_5(self) -> None:
        self._command_queue.put_nowait(
            Command("steer", data={"text": "stuck_option_5"}, source="tui")
        )
        self._clear_stuck_mode()

    # ── message handlers ─────────────────────────────────────────────────────

    def on_switch_provider_message(self, msg: SwitchProviderMessage) -> None:
        self._command_queue.put_nowait(
            Command(
                "switch_provider",
                data={"role": msg.role, "provider": msg.provider},
                source="tui",
            )
        )
        log_view = self.query_one("#log-view", LogView)
        log_view.push_message(
            f"Переключение провайдера: {msg.role} → {msg.provider}", style="cyan"
        )

    def on_steer_message(self, msg: SteerMessage) -> None:
        self._command_queue.put_nowait(
            Command("steer", data={"text": msg.text}, source="tui")
        )
        log_view = self.query_one("#log-view", LogView)
        log_view.push_message(f"Стир: {msg.text}", style="yellow")

    # ── worker state ─────────────────────────────────────────────────────────

    def on_worker_state_changed(self, event) -> None:  # type: ignore[override]
        if (
            self._runner_worker is not None
            and event.worker is self._runner_worker
            and event.state == WorkerState.ERROR
        ):
            log_view = self.query_one("#log-view", LogView)
            log_view.push_message("Критическая ошибка: runner завершился с ошибкой.", style="red bold")

    # ── responsive layout ────────────────────────────────────────────────────

    def on_resize(self, event) -> None:  # type: ignore[override]
        usage_panel = self.query_one("#usage-panel", UsagePanel)
        if event.size.width < 100:
            usage_panel.compact = True
        else:
            usage_panel.compact = False
