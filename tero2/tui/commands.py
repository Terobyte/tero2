"""Command Palette provider for DashboardApp (Ctrl+P)."""

from __future__ import annotations

from textual.command import Hit, Hits, Provider


class Tero2CommandProvider(Provider):
    """Provides tero2-specific commands for Textual Command Palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        commands = [
            ("Сменить план", "change_plan"),
            ("Сменить провайдера роли", "roles"),
            ("Отправить указание агенту", "steer"),
            ("Настройки (глобальные)", "settings"),
            ("Пауза / возобновить", "pause"),
            ("Пропустить задачу", "skip"),
            ("Выход", "quit"),
        ]
        for label, action_name in commands:
            score = matcher.match(label)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(label),
                    lambda a=action_name: self.app.action(a),
                    help=f"tero2: {label}",
                )
