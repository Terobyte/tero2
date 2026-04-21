"""DashboardApp — main Textual TUI for tero2."""

from __future__ import annotations

import asyncio
import logging
from typing import ClassVar

from textual.app import App, ComposeResult
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Footer, Header
from textual.worker import WorkerState

from tero2.events import Command, EventDispatcher
from tero2.runner import Runner
from tero2.state import SoraPhase
from tero2.tui.commands import Tero2CommandProvider
from tero2.tui.screens.role_swap import RoleSwapScreen, SwitchProviderMessage
from tero2.tui.screens.steer import SteerMessage, SteerScreen
from tero2.tui.widgets.stuck_hint import StuckHintWidget
from tero2.tui.widgets.log_view import LogView
from tero2.tui.widgets.pipeline import PipelinePanel
from tero2.tui.widgets.usage import UsagePanel

log = logging.getLogger(__name__)


class DashboardApp(App):
    """Main Textual TUI dashboard for tero2."""

    CSS_PATH = "styles.tcss"

    COMMANDS: ClassVar[set] = {Tero2CommandProvider}

    BINDINGS: ClassVar[list] = [
        ("r", "roles", "Роли"),
        ("s", "steer", "Указание"),
        ("p", "pause", "Пауза"),
        ("q", "quit", "Выход"),
        ("k", "skip", "Пропустить"),
        ("l", "change_plan", "Смена плана"),
        ("n", "new_project", "Новый"),
        ("o", "settings", "Настройки"),
        ("1", "stuck_option_1", "1 retry"),
        ("2", "stuck_option_2", "2 switch"),
        ("3", "stuck_option_3", "3 skip"),
        ("4", "stuck_option_4", "4 escalate"),
        ("5", "stuck_option_5", "5 manual"),
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
        yield Header()
        yield PipelinePanel(id="pipeline")
        with Horizontal(id="main-row"):
            yield LogView(id="log-view")
            yield UsagePanel(id="usage-panel")
        hint = StuckHintWidget(id="stuck-hint")
        hint.display = False
        yield hint
        yield Footer()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def on_mount(self) -> None:
        self._event_queue = self._dispatcher.subscribe()
        self._runner_worker = self.run_worker(self._run_runner(), exclusive=True)
        self.run_worker(self._consume_events(), exclusive=False)

    def on_unmount(self) -> None:
        if self._event_queue is not None:
            self._dispatcher.unsubscribe(self._event_queue)

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

            try:
                pipeline = self.query_one("#pipeline", PipelinePanel)
                log_view = self.query_one("#log-view", LogView)
                usage_panel = self.query_one("#usage-panel", UsagePanel)
                stuck_hint = self.query_one("#stuck-hint", StuckHintWidget)
            except NoMatches:
                continue

            # route by event kind
            try:
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
                    stuck_hint.display = True

                elif event.kind == "done":
                    log_view.push_message("Задание выполнено.", style="green bold")
                    pipeline.stuck_mode = False
                    stuck_hint.display = False

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
            except Exception:
                log.error("error routing event %r", event, exc_info=True)

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

    def action_change_plan(self) -> None:
        project_path = getattr(self._runner, "project_path", None)
        if project_path is None:
            log_view = self.query_one("#log-view", LogView)
            log_view.push_message(
                "Смена плана недоступна: проект не задан.",
                style="yellow",
            )
            return

        from tero2.tui.screens.plan_pick import PlanPickScreen

        def _on_plan_selected(plan_file) -> None:
            if plan_file is not None:
                self._command_queue.put_nowait(
                    Command("new_plan", data={"text": str(plan_file)}, source="tui")
                )

        self.push_screen(PlanPickScreen(project_path), _on_plan_selected)

    def action_new_project(self) -> None:
        from tero2.tui.screens.project_pick import ProjectPickScreen

        def _on_project(path) -> None:
            if path is not None:
                self._command_queue.put_nowait(
                    Command("new_project", data={"path": str(path)}, source="tui")
                )

        self.push_screen(ProjectPickScreen(), _on_project)

    def action_settings(self) -> None:
        from tero2.tui.screens.settings import SettingsScreen
        self.push_screen(SettingsScreen())

    # ── action guard ──────────────────────────────────────────────────────────

    def _clear_stuck_mode(self) -> None:
        pipeline = self.query_one("#pipeline", PipelinePanel)
        stuck_hint = self.query_one("#stuck-hint", StuckHintWidget)
        pipeline.stuck_mode = False
        stuck_hint.display = False

    def check_action(self, action: str, parameters: tuple) -> bool:
        stuck_actions = {
            "stuck_option_1", "stuck_option_2", "stuck_option_3",
            "stuck_option_4", "stuck_option_5",
        }
        if action in stuck_actions:
            try:
                hint = self.query_one("#stuck-hint", StuckHintWidget)
                return bool(hint.display)
            except Exception:
                return False
        return True

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
                data={"role": msg.role, "provider": msg.provider, "model": msg.model},
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
