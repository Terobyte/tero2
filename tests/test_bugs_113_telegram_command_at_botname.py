"""Bug 113: Telegram bot silently rejects group-chat ``/cmd@botname`` syntax.

Telegram's documented behaviour is that in group chats — and any chat where
more than one bot is present — the client appends ``@<bot_username>`` to the
command, so ``/stop`` becomes ``/stop@tero2_nightrun_bot``. This tells
Telegram which bot the command is aimed at and suppresses delivery to other
bots in the same chat.

``TelegramInputBot._handle_command`` currently does::

    command = text.strip().split()[0].lower()
    if command == "/status": ...
    elif command == "/stop": ...

So ``/stop@tero2_nightrun_bot`` maps to the literal string
``"/stop@tero2_nightrun_bot"``, matches none of the ``elif`` branches, and
falls into the "Unknown command" reply.

The bug is silent for users because the "Unknown command" reply goes out to
the chat, looking like the bot misunderstood. For the night-run setup (where
the user DMs the bot directly) this doesn't bite — but any group-chat use
breaks every command. It is a pure parsing bug with a clean fix: strip the
``@botname`` suffix before branching.

Per the new TDD discipline (feedback_tdd_order.md): these tests are written
**before** the fix. They are expected to FAIL against the current (broken)
``_handle_command``. The fix is committed only after I've seen them red.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tero2.config import Config, TelegramConfig
from tero2.telegram_input import TelegramInputBot


def _make_bot(tmp_path: Path) -> TelegramInputBot:
    config = Config()
    config.projects_dir = str(tmp_path / "projects")
    config.telegram = TelegramConfig(
        bot_token="test-token",
        chat_id="123",
        allowed_chat_ids=["123"],
    )
    bot = TelegramInputBot(config)
    bot.notifier = MagicMock()
    bot.notifier.send = AsyncMock(return_value=True)
    return bot


class TestGroupChatCommandSyntax:
    """Group chats append ``@<botname>`` to commands. Each supported slash
    command must behave identically with or without the suffix.

    The ``.lower()`` in the parser means we must also tolerate mixed case in
    the bot suffix — Telegram usernames are case-insensitive."""

    @pytest.mark.asyncio
    async def test_stop_with_bot_suffix(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._running = True

        await bot._handle_command("/stop@tero2_nightrun_bot", "123")

        # Effect: _running is cleared, and the reply text is the same as /stop.
        assert bot._running is False, (
            "group-chat /stop@bot must stop the bot, not be rejected as unknown"
        )
        reply = bot.notifier.send.call_args_list[0].args[0]
        assert "Stopping" in reply, f"unexpected reply: {reply!r}"

    @pytest.mark.asyncio
    async def test_pause_with_bot_suffix(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._paused = False

        await bot._handle_command("/pause@tero2_nightrun_bot", "123")

        assert bot._paused is True, (
            "group-chat /pause@bot must pause, not be rejected as unknown"
        )

    @pytest.mark.asyncio
    async def test_resume_with_bot_suffix(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._paused = True

        await bot._handle_command("/resume@tero2_nightrun_bot", "123")

        assert bot._paused is False, (
            "group-chat /resume@bot must resume, not be rejected as unknown"
        )

    @pytest.mark.asyncio
    async def test_status_with_bot_suffix(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._running = True
        bot._paused = False

        await bot._handle_command("/status@tero2_nightrun_bot", "123")

        reply = bot.notifier.send.call_args_list[0].args[0]
        # /status reply contains "Status:" and queue size; Unknown command
        # reply contains "Unknown command" and the literal token.
        assert "Unknown command" not in reply, (
            f"group-chat /status@bot rejected as unknown: {reply!r}"
        )
        assert "Status:" in reply or "Queue size" in reply, (
            f"expected status payload, got {reply!r}"
        )

    @pytest.mark.asyncio
    async def test_mixed_case_bot_suffix_accepted(self, tmp_path: Path) -> None:
        """Telegram usernames are case-insensitive. ``/Stop@Tero2_Bot`` must
        be accepted the same as ``/stop@tero2_bot``."""
        bot = _make_bot(tmp_path)
        bot._running = True

        await bot._handle_command("/Stop@Tero2_Bot", "123")

        assert bot._running is False, (
            "mixed-case group-chat command must work — parser already lowercases"
        )


class TestBareCommandsStillWork:
    """The fix must not regress DM-style bare commands."""

    @pytest.mark.asyncio
    async def test_bare_stop(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._running = True
        await bot._handle_command("/stop", "123")
        assert bot._running is False

    @pytest.mark.asyncio
    async def test_unknown_command_still_rejected(self, tmp_path: Path) -> None:
        """Genuinely unknown commands (including unknown-command-with-botname)
        must still produce the ``Unknown command`` reply so users notice."""
        bot = _make_bot(tmp_path)

        await bot._handle_command("/definitelynotacommand", "123")

        reply = bot.notifier.send.call_args_list[0].args[0]
        assert "Unknown command" in reply

    @pytest.mark.asyncio
    async def test_unknown_command_with_bot_suffix_rejected(
        self, tmp_path: Path
    ) -> None:
        """Unknown-command with ``@bot`` suffix must still hit the "unknown"
        branch — the fix strips ``@suffix`` before matching, not after."""
        bot = _make_bot(tmp_path)

        await bot._handle_command("/notarealcmd@tero2_bot", "123")

        reply = bot.notifier.send.call_args_list[0].args[0]
        assert "Unknown command" in reply


class TestArgumentsAfterCommand:
    """Commands are delimited by whitespace — arguments after the command
    must not break recognition (e.g., ``/stop now`` in DMs)."""

    @pytest.mark.asyncio
    async def test_stop_with_trailing_arg(self, tmp_path: Path) -> None:
        bot = _make_bot(tmp_path)
        bot._running = True

        await bot._handle_command("/stop please", "123")

        assert bot._running is False

    @pytest.mark.asyncio
    async def test_stop_with_bot_suffix_and_trailing_arg(
        self, tmp_path: Path
    ) -> None:
        bot = _make_bot(tmp_path)
        bot._running = True

        await bot._handle_command("/stop@tero2_bot please", "123")

        assert bot._running is False
