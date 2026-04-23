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

        await bot._plan_queue.put(("test-proj", "# Plan"))

        consumer = asyncio.create_task(bot._consume_plans())
        await asyncio.sleep(0.1)
        bot._running = False
        consumer.cancel()
        try:
            await consumer
        except asyncio.CancelledError:
            pass

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


class TestHandleUpdate:
    @pytest.mark.asyncio
    async def test_text_plan_routed_to_handle_plan(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        update = {
            "message": {
                "chat": {"id": 123},
                "text": "# My Project\nDo the thing",
            }
        }
        await bot._handle_update(update)
        assert bot._plan_queue.qsize() == 1
        name, content = await bot._plan_queue.get()
        assert name == "My Project"
        assert "Do the thing" in content

    @pytest.mark.asyncio
    async def test_command_routed_to_handle_command(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        update = {
            "message": {
                "chat": {"id": 123},
                "text": "/status",
            }
        }
        await bot._handle_update(update)
        bot.notifier.send.assert_called_once()
        assert "Queue size" in bot.notifier.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_disallowed_chat_id_ignored(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        update = {
            "message": {
                "chat": {"id": 999},
                "text": "# Secret Plan\nEvil stuff",
            }
        }
        await bot._handle_update(update)
        assert bot._plan_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_no_message_ignored(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        await bot._handle_update({"something": "else"})
        assert bot._plan_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_empty_text_ignored(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        update = {
            "message": {
                "chat": {"id": 123},
            }
        }
        await bot._handle_update(update)
        assert bot._plan_queue.qsize() == 0

    @pytest.mark.asyncio
    async def test_non_md_document_rejected(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        update = {
            "message": {
                "chat": {"id": 123},
                "document": {"file_name": "plan.pdf", "file_id": "abc"},
            }
        }
        with patch.object(bot, "_download_file", new_callable=AsyncMock):
            await bot._handle_update(update)
        bot.notifier.send.assert_called_once()
        assert ".md" in bot.notifier.send.call_args[0][0]

    @pytest.mark.asyncio
    async def test_md_document_downloaded_and_queued(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        update = {
            "message": {
                "chat": {"id": 123},
                "document": {"file_name": "plan.md", "file_id": "xyz"},
            }
        }
        with patch.object(
            bot, "_download_file", new_callable=AsyncMock, return_value="# DocPlan\nContent"
        ):
            await bot._handle_update(update)
        assert bot._plan_queue.qsize() == 1
        name, content = await bot._plan_queue.get()
        assert name == "DocPlan"


class TestConsumePlans:
    @pytest.mark.asyncio
    async def test_consume_calls_init_project(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._running = True

        await bot._plan_queue.put(("test-proj", "# Test\nContent"))

        processed = asyncio.Event()

        def _init_and_signal(*args, **kwargs):
            processed.set()
            return tmp_path / "test-proj"

        with (
            patch(
                "tero2.telegram_input.init_project",
                side_effect=_init_and_signal,
            ) as mock_init,
            patch.object(bot, "_launch_runner", new_callable=AsyncMock),
        ):
            consumer = asyncio.create_task(bot._consume_plans())
            await processed.wait()
            bot._running = False
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

            mock_init.assert_called_once_with("test-proj", "# Test\nContent", config)

    @pytest.mark.asyncio
    async def test_consume_handles_file_exists_error(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._running = True

        await bot._plan_queue.put(("dup-proj", "# Dup\nContent"))

        errored = asyncio.Event()

        def _raise_and_signal(*args, **kwargs):
            errored.set()
            raise FileExistsError("already exists")

        with patch(
            "tero2.telegram_input.init_project",
            side_effect=_raise_and_signal,
        ):
            consumer = asyncio.create_task(bot._consume_plans())
            await errored.wait()
            bot._running = False
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

        error_calls = [c for c in bot.notifier.send.call_args_list if "already exists" in c[0][0]]
        assert len(error_calls) >= 1

    @pytest.mark.asyncio
    async def test_consume_handles_generic_exception(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._running = True

        await bot._plan_queue.put(("bad-proj", "# Bad\nContent"))

        errored = asyncio.Event()

        def _raise_and_signal(*args, **kwargs):
            errored.set()
            raise RuntimeError("something broke")

        with patch(
            "tero2.telegram_input.init_project",
            side_effect=_raise_and_signal,
        ):
            consumer = asyncio.create_task(bot._consume_plans())
            await errored.wait()
            bot._running = False
            consumer.cancel()
            try:
                await consumer
            except asyncio.CancelledError:
                pass

        error_calls = [c for c in bot.notifier.send.call_args_list if "failed" in c[0][0]]
        assert len(error_calls) >= 1

    @pytest.mark.asyncio
    async def test_consume_requeues_on_paused_after_dequeue(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._running = True

        await bot._plan_queue.put(("paused-proj", "# Paused\nContent"))

        dequeued = asyncio.Event()
        proceed = asyncio.Event()
        original_get = bot._plan_queue.get

        async def _tracked_get():
            result = await original_get()
            dequeued.set()
            await proceed.wait()
            return result

        bot._plan_queue.get = _tracked_get

        with patch("tero2.telegram_input.init_project") as mock_init:
            consumer_task = asyncio.create_task(bot._consume_plans())

            await dequeued.wait()
            bot._paused = True
            proceed.set()
            await asyncio.sleep(0.05)

            bot._running = False
            consumer_task.cancel()
            try:
                await consumer_task
            except asyncio.CancelledError:
                pass

        mock_init.assert_not_called()
        assert bot._plan_queue.qsize() >= 1


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_clears_queue(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)
        bot._running = True
        bot._paused = True
        await bot._plan_queue.put(("a", "plan-a"))
        await bot._plan_queue.put(("b", "plan-b"))
        await bot.stop()
        assert bot._plan_queue.empty()
        assert not bot._running
        assert not bot._paused


class TestDownloadFile:
    @pytest.mark.asyncio
    async def test_download_file_returns_content(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)

        mock_resp_get_file = MagicMock()
        # Bug 117: real Telegram responses always carry file_size; include it
        # so the defense-in-depth guard lets the download through.
        mock_resp_get_file.json.return_value = {
            "result": {"file_path": "documents/file.md", "file_size": 42}
        }
        mock_resp_get_file.raise_for_status = MagicMock()

        mock_resp_download = MagicMock()
        mock_resp_download.status_code = 200
        mock_resp_download.content = b"# Downloaded Plan\nHello"
        mock_resp_download.raise_for_status = MagicMock()

        def mock_post(*args, **kwargs):
            return mock_resp_get_file

        def mock_get(*args, **kwargs):
            return mock_resp_download

        with (
            patch("tero2.telegram_input.requests.post", side_effect=mock_post),
            patch("tero2.telegram_input.requests.get", side_effect=mock_get),
        ):
            result = await bot._download_file("file123")
        assert result == "# Downloaded Plan\nHello"

    @pytest.mark.asyncio
    async def test_download_file_returns_none_on_failure(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)

        with patch("tero2.telegram_input.requests.post", side_effect=Exception("network error")):
            result = await bot._download_file("bad-file")
        assert result is None

    @pytest.mark.asyncio
    async def test_download_file_returns_none_on_missing_path(self, tmp_path: Path):
        config = _make_config(tmp_path)
        bot = _make_bot(config)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"result": {}}

        with patch("tero2.telegram_input.requests.post", return_value=mock_resp):
            result = await bot._download_file("file123")
        assert result is None
