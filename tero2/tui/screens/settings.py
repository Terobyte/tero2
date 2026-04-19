"""SettingsScreen — global ~/.tero2/config.toml editor (3 tabs)."""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

from textual.app import ComposeResult
from textual.binding import Binding
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import (
    Checkbox,
    Footer,
    Input,
    Label,
    Static,
    Tab,
    TabbedContent,
    TabPane,
)

from tero2.config_writer import write_global_config_section

_GLOBAL_CONFIG = Path.home() / ".tero2" / "config.toml"


class SettingsScreen(ModalScreen[None]):
    """Global settings editor. Writes to ~/.tero2/config.toml on [s]."""

    BINDINGS: ClassVar[list] = [
        Binding("s", "save", "Сохранить"),
        Binding("b", "cancel", "Закрыть", show=False),
        Binding("escape", "cancel", "Закрыть", show=False),
        Binding("q", "cancel", "Закрыть", show=False),
    ]

    def __init__(self, config_path: Path | None = None) -> None:
        super().__init__()
        self._config_path = config_path or _GLOBAL_CONFIG

    def compose(self) -> ComposeResult:
        yield Static(
            f"tero2 — настройки (глобальные {self._config_path})",
            classes="screen-title",
        )
        with TabbedContent():
            with TabPane("Провайдеры", id="tab-providers"):
                yield Label("Глобальные настройки провайдеров применяются к новым проектам.")
                yield Label("Используйте [r] в Dashboard для смены провайдера текущего запуска.")

            with TabPane("Telegram", id="tab-telegram"):
                yield Label("Telegram-бот:", classes="field-label")
                yield Checkbox("Включить", id="tg-enabled")
                yield Label("Bot token:", classes="field-label")
                yield Input(placeholder="bot:TOKEN", id="tg-token", password=True)
                yield Label("Разрешённые chat_id:", classes="field-label")
                yield Input(placeholder="614473938", id="tg-chat-ids")
                yield Checkbox("Голосовое уведомление при завершении", id="tg-voice", value=True)
                yield Static(
                    "Бот принимает .md-планы и текстовые сообщения.\n"
                    "НЕ чат с агентом — только входящий канал.",
                    classes="info-msg",
                )

            with TabPane("Поведение", id="tab-behaviour"):
                yield Label("Максимум слайсов:", classes="field-label")
                yield Input(placeholder="12", id="max-slices")
                yield Label("Idle timeout (сек, 0=выкл):", classes="field-label")
                yield Input(placeholder="0", id="idle-timeout")

        yield Footer()

    def action_save(self) -> None:
        try:
            self._do_save()
            self.notify("Настройки сохранены", severity="information")
        except Exception as e:
            self.notify(f"Ошибка сохранения: {e}", severity="error")

    def _do_save(self) -> None:
        # Telegram section — widget lookup is optional (tab may not be rendered),
        # but TOML write failures must propagate so action_save() can report them.
        try:
            enabled_cb = self.query_one("#tg-enabled", Checkbox)
            token_in = self.query_one("#tg-token", Input)
            chat_ids_in = self.query_one("#tg-chat-ids", Input)
            voice_cb = self.query_one("#tg-voice", Checkbox)
        except NoMatches:
            pass  # tab not rendered — skip section
        else:
            write_global_config_section(self._config_path, "telegram", {
                "enabled": enabled_cb.value,
                "bot_token": token_in.value,
                "allowed_chat_ids": [
                    c.strip() for c in chat_ids_in.value.split(",") if c.strip()
                ],
                "voice_on_done": voice_cb.value,
            })

        # Behaviour section — same pattern: only suppress missing-widget, not write errors.
        try:
            max_slices_in = self.query_one("#max-slices", Input)
            idle_in = self.query_one("#idle-timeout", Input)
        except NoMatches:
            return  # tab not rendered — skip section
        sora_data: dict = {}
        if max_slices_in.value.isdigit():
            sora_data["max_slices"] = int(max_slices_in.value)
        if idle_in.value.isdigit():
            sora_data["idle_timeout_s"] = int(idle_in.value)
        if sora_data:
            write_global_config_section(self._config_path, "sora", sora_data)

    def action_cancel(self) -> None:
        self.dismiss(None)
