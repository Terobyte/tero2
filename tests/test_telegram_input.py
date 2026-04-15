"""Tests for tero2.telegram_input — Telegram bot plan input."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, TelegramConfig
from tero2.telegram_input import TelegramInputBot


def _make_config(tmp_path: Path) -> Config:
    config = Config()
    config.projects_dir = str(tmp_path / "projects")
    config.telegram = TelegramConfig(
        bot_token="test-token",
        chat_id="123",
        allowed_chat_ids=["123", "456"],
    )
    return config


def _make_bot(config: Config) -> TelegramInputBot:
    bot = TelegramInputBot(config)
    # Replace notifier with mock to avoid actual HTTP calls
    bot.notifier = MagicMock()
    bot.notifier.send = AsyncMock(return_value=True)
    return bot


class TestTelegramInputBotInit:
    def test_allowed_ids_set(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = TelegramInputBot(config)
        assert "123" in bot._allowed_ids
        assert "456" in bot._allowed_ids

    def test_not_running_initially(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = TelegramInputBot(config)
        assert not bot._running
        assert not bot._paused


class TestIsAllowed:
    def test_allowed_id(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = TelegramInputBot(config)
        assert bot._is_allowed("123")

    def test_disallowed_id(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = TelegramInputBot(config)
        assert not bot._is_allowed("999")

    def test_empty_allowed_list(self, tmp_path: Path):
        config = _make_config(tmp_path)
        config.telegram.allowed_chat_ids = []
        bot = TelegramInputBot(config)
        assert not bot._is_allowed("123")


class TestPauseResume:
    @pytest.mark.asyncio
    async def test_pause_sets_flag(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        assert not bot._paused
        await bot._handle_command("/pause", "123")
        assert bot._paused

    @pytest.mark.asyncio
    async def test_resume_clears_flag(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._paused = True
        await bot._handle_command("/resume", "123")
        assert not bot._paused

    @pytest.mark.asyncio
    async def test_consume_plans_respects_paused(self, tmp_path: Path):
        """When paused, _consume_plans should not dequeue plans."""
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._running = True
        bot._paused = True

        # Put a plan in the queue
        await bot._plan_queue.put(("test-proj", "# Plan"))

        # Run _consume_plans briefly -- it should NOT process the plan
        async def _stop_after_delay():
            await asyncio.sleep(0.3)
            bot._running = False

        consume_task = asyncio.create_task(bot._consume_plans())
        stop_task = asyncio.create_task(_stop_after_delay())
        await asyncio.gather(consume_task, stop_task)

        # Plan should still be in the queue
        assert bot._plan_queue.qsize() == 1


class TestHandlePlan:
    @pytest.mark.asyncio
    async def test_plan_enqueued(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        await bot._handle_plan("# Build Auth\nImplement JWT.", "123")
        assert bot._plan_queue.qsize() == 1
        name, content = await bot._plan_queue.get()
        assert name == "Build Auth"
        assert "Implement JWT" in content


class TestHandleCommand:
    @pytest.mark.asyncio
    async def test_unknown_command(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        await bot._handle_command("/unknown", "123")
        # Should send unknown command message
        bot.notifier.send.assert_called_once()
        call_args = bot.notifier.send.call_args[0][0]
        assert "Unknown command" in call_args

    @pytest.mark.asyncio
    async def test_stop_sets_running_false(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._running = True
        await bot._handle_command("/stop", "123")
        assert not bot._running

    @pytest.mark.asyncio
    async def test_status_shows_queue_size(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        await bot._handle_command("/status", "123")
        bot.notifier.send.assert_called_once()
        call_args = bot.notifier.send.call_args[0][0]
        assert "Queue size" in call_args
