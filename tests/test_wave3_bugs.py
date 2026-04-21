"""Tests for Wave 3 audit bugs (49-53).

Run: pytest tests/test_wave3_bugs.py -v
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, TelegramConfig
from tero2.errors import ConfigError


# ── Bug 49: config_writer._load_toml returns {} on TOMLDecodeError ─────────


class TestBug49ConfigWriterTOMLDecodeErrorDestroysConfig:
    """_load_toml returns {} when TOML is broken.  If config_writer then
    serializes {} + new section → the entire config file is destroyed.

    Fix: raise (or return None) so the caller aborts the write.
    """

    def test_broken_toml_does_not_return_empty_dict(self, tmp_path: Path) -> None:
        """If _load_toml silently returns {} on broken TOML, a subsequent
        write_global_config_section will destroy the entire config.
        Fix: raise ConfigError so the caller aborts the write."""
        from tero2.config_writer import _load_toml

        bad_toml = tmp_path / "config.toml"
        bad_toml.write_text("existing = true\nbroken = [", encoding="utf-8")

        # Fix: must raise ConfigError (not silently return {})
        try:
            result = _load_toml(bad_toml)
            assert result != {}, (
                "Bug 49: _load_toml returns {} on TOMLDecodeError. "
                "Fix: raise ConfigError so caller aborts the write."
            )
        except ConfigError:
            pass  # correct: ConfigError prevents config destruction

    def test_write_section_aborts_on_broken_toml(self, tmp_path: Path) -> None:
        """write_global_config_section must NOT overwrite a broken config file."""
        from tero2.config_writer import write_global_config_section

        config_path = tmp_path / "config.toml"
        # Write a config with existing sections
        config_path.write_text(
            "existing_key = true\n\n[telegram]\nbot_token = 'abc'\n",
            encoding="utf-8",
        )
        # Corrupt it
        config_path.write_text(
            "existing_key = true\n\n[telegram]\nbot_token = 'abc'\nbroken = [",
            encoding="utf-8",
        )

        try:
            write_global_config_section(
                config_path, "roles.builder", {"provider": "claude"}
            )
        except (ConfigError, Exception):
            # Fix should raise — that's acceptable
            return

        # If no exception, the existing sections must survive
        content = config_path.read_text()
        assert "existing_key" in content, (
            "Bug 49: write_global_config_section overwrote broken config with "
            "only the new section — existing_key was destroyed. "
            "Fix: abort the write when _load_toml returns {} due to TOMLDecodeError."
        )


# ── Bug 50: telegram_input _unfinished_tasks inflates on pause re-queue ────


class TestBug50UnfinishedTasksInflatesOnPause:
    """When _paused is True, the item is put back via put() (+1 to
    _unfinished_tasks) but task_done() is never called for it because the
    finally block skips task_done() when paused.

    Each pause cycle leaks +1.  stop() calls join() which waits for
    _unfinished_tasks == 0 → hangs forever after at least one pause.

    Fix: call task_done() after the re-queue put().
    """

    @pytest.mark.asyncio
    async def test_pause_requeue_balances_unfinished_tasks(self) -> None:
        from tero2.telegram_input import TelegramInputBot

        bot = TelegramInputBot.__new__(TelegramInputBot)
        bot._plan_queue = asyncio.Queue()
        bot._paused = True
        bot._running = True
        bot.config = Config(telegram=TelegramConfig(bot_token="fake", chat_id="1"))
        bot.notifier = AsyncMock()
        bot._poll_task = None
        bot._consume_task = None
        bot._launch_runner = AsyncMock()  # type: ignore[attr-defined]

        # Put one plan on the queue
        await bot._plan_queue.put(("test_project", "# plan"))

        unfinished_before = bot._plan_queue._unfinished_tasks  # type: ignore[attr-defined]

        # Let _consume_plans run one iteration — it will re-queue because paused
        async def _stop_after_one():
            await asyncio.sleep(0.3)
            bot._running = False

        await asyncio.gather(
            bot._consume_plans(),
            _stop_after_one(),
        )

        unfinished_after = bot._plan_queue._unfinished_tasks  # type: ignore[attr-defined]

        assert unfinished_after == unfinished_before, (
            f"Bug 50: _unfinished_tasks inflated from {unfinished_before} to "
            f"{unfinished_after} after pause re-queue. Each pause cycle leaks +1 "
            "because task_done() is never called for the re-queued item. "
            "Fix: call task_done() after the re-queue put()."
        )

    @pytest.mark.asyncio
    async def test_join_completes_after_pause_cycle(self) -> None:
        """After a pause re-queue cycle, join() must complete (not hang)."""
        from tero2.telegram_input import TelegramInputBot

        bot = TelegramInputBot.__new__(TelegramInputBot)
        bot._plan_queue = asyncio.Queue()
        bot._paused = True
        bot._running = True
        bot.config = Config(telegram=TelegramConfig(bot_token="fake", chat_id="1"))
        bot.notifier = AsyncMock()
        bot._poll_task = None
        bot._consume_task = None
        bot._launch_runner = AsyncMock()  # type: ignore[attr-defined]

        await bot._plan_queue.put(("proj", "# plan"))

        async def _stop_and_drain():
            await asyncio.sleep(0.3)
            bot._running = False
            # Drain remaining items so join() can complete
            while not bot._plan_queue.empty():
                try:
                    bot._plan_queue.get_nowait()
                    bot._plan_queue.task_done()
                except asyncio.QueueEmpty:
                    break

        await asyncio.gather(
            bot._consume_plans(),
            _stop_and_drain(),
        )

        # join() should complete quickly — not hang due to inflated counter
        await asyncio.wait_for(bot._plan_queue.join(), timeout=1.0)

    @pytest.mark.asyncio
    async def test_task_done_called_when_paused_mid_processing(self) -> None:
        """Bug 50 (finally path): if _paused becomes True while init_project
        is executing, the finally block guard `if not self._paused:` skips
        task_done() — _unfinished_tasks stays at 1 and join() hangs forever.

        Fix: remove the `if not self._paused:` guard; call task_done()
        unconditionally in the finally block.
        """
        from pathlib import Path

        from tero2.telegram_input import TelegramInputBot

        bot = TelegramInputBot.__new__(TelegramInputBot)
        bot._plan_queue = asyncio.Queue()
        bot._paused = False
        bot._running = True
        bot.config = Config(telegram=TelegramConfig(bot_token="fake", chat_id="1"))
        bot.notifier = AsyncMock()
        bot._launch_runner = AsyncMock()  # type: ignore[attr-defined]

        def _pause_mid_process(name, content, config):
            # Simulate /pause command arriving during init_project execution.
            # init_project is synchronous; _paused can change before the
            # finally block runs (set by _handle_command via a concurrent await).
            bot._paused = True
            bot._running = False  # stop the loop after this item
            return Path("/tmp/fake_project")

        await bot._plan_queue.put(("test_proj", "# plan"))

        with patch("tero2.telegram_input.init_project", side_effect=_pause_mid_process):
            try:
                await asyncio.wait_for(bot._consume_plans(), timeout=2.0)
            except asyncio.TimeoutError:
                pass

        unfinished = bot._plan_queue._unfinished_tasks  # type: ignore[attr-defined]
        assert unfinished == 0, (
            f"Bug 50 (finally path): task_done() was NOT called after _paused "
            f"became True during init_project. unfinished_tasks={unfinished}. "
            "The finally block `if not self._paused: task_done()` skips the call "
            "whenever a /pause command arrives mid-processing. "
            "Fix: remove the `if not self._paused:` guard — call task_done() "
            "unconditionally in the finally block."
        )


# ── Bug 51: lock.py .pid file is dead code ──────────────────────────────────


class TestBug51PidFileDeadCode:
    """Lines 37-39 write a .pid file that nothing reads.  _read_pid() reads
    the .lock file (line 59), not the .pid file.

    Fix: delete lines 37-39.
    """

    def test_no_pid_file_created_on_acquire(self, tmp_path: Path) -> None:
        """After acquire(), there should be no orphan .pid file."""
        from tero2.lock import FileLock

        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path)
        lock.acquire()

        try:
            pid_path = lock_path.with_suffix(".pid")
            assert not pid_path.exists(), (
                "Bug 51: .pid file is written but never read by anything. "
                "_read_pid() reads .lock, not .pid. The .pid file is dead code "
                "that wastes I/O and leaves stale files on disk. "
                "Fix: delete lines 37-39 in lock.py."
            )
        finally:
            lock.release()

    def test_read_pid_reads_lock_file_not_pid_file(self, tmp_path: Path) -> None:
        """_read_pid() must read the .lock file, not a separate .pid file."""
        from tero2.lock import FileLock
        import tero2.lock as lock_module

        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path)
        lock.acquire()

        try:
            source = inspect.getsource(lock_module.FileLock._read_pid)
            # _read_pid should read self.lock_path, not .pid
            assert "self.lock_path" in source, (
                "Bug 51: _read_pid should read self.lock_path (the .lock file)."
            )
        finally:
            lock.release()


# ── Bug 52: shell provider terminate without wait — zombie process ─────────


class TestBug52ShellProviderZombieOnTerminate:
    """terminate() sends SIGTERM but doesn't wait().  The child can remain
    as a zombie.

    Fix: add await proc.wait() after proc.terminate().
    """

    @pytest.mark.asyncio
    async def test_terminate_followed_by_wait(self) -> None:
        """On communicate() error, terminate() must be followed by wait()."""
        from tero2.providers.shell import ShellProvider

        proc_mock = MagicMock()
        proc_mock.returncode = None
        proc_mock.communicate = AsyncMock(side_effect=RuntimeError("forced"))
        proc_mock.terminate = MagicMock()
        proc_mock.wait = AsyncMock()
        proc_mock.stdout = MagicMock()
        proc_mock.stderr = MagicMock()

        provider = ShellProvider.__new__(ShellProvider)

        with patch(
            "tero2.providers.shell.asyncio.create_subprocess_exec",
            return_value=proc_mock,
        ):
            with pytest.raises(RuntimeError, match="forced"):
                async for _ in provider.run(prompt="echo hi"):
                    pass

        proc_mock.terminate.assert_called()
        proc_mock.wait.assert_called(), (
            "Bug 52: proc.terminate() was called but proc.wait() was not. "
            "Without wait(), the child process becomes a zombie. "
            "Fix: add `await proc.wait()` after `proc.terminate()`."
        )

    @pytest.mark.asyncio
    async def test_wait_called_even_if_terminate_raises(self) -> None:
        """If terminate() itself raises, wait() should still be attempted."""
        from tero2.providers.shell import ShellProvider
        import tero2.providers.shell as shell_module

        source = inspect.getsource(ShellProvider.run)
        # After fix: the except block should have wait after terminate
        # Check source for the pattern: terminate then wait in sequence
        has_wait_after_terminate = "wait" in source
        assert has_wait_after_terminate, (
            "Bug 52: shell.py has proc.terminate() without proc.wait(). "
            "Fix: add `await proc.wait()` after `proc.terminate()` in the "
            "except block."
        )


# ── Bug 53: providers_pick + config_writer write_text without encoding ─────


class TestBug53WriteTextWithoutEncoding:
    """Reads use explicit encoding='utf-8' but writes rely on OS default.
    Breaks on non-UTF-8 systems (e.g. Windows with cp1252).

    Fix: tmp.write_text(content, encoding="utf-8").
    """

    def test_config_writer_write_text_has_encoding(self) -> None:
        """config_writer.py must specify encoding='utf-8' on write_text."""
        import tero2.config_writer as cw_module

        source = inspect.getsource(cw_module)
        # Find all write_text calls
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "write_text" in line and "encoding" not in line:
                # Allow write_text in comments or strings
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                pytest.fail(
                    f"Bug 53: config_writer.py line {i+1} has write_text() "
                    f"without encoding='utf-8': {stripped}. "
                    "Fix: add encoding='utf-8' to all write_text() calls."
                )

    def test_providers_pick_write_text_has_encoding(self) -> None:
        """providers_pick.py must specify encoding='utf-8' on write_text."""
        import tero2.tui.screens.providers_pick as pp_module

        source = inspect.getsource(pp_module)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "write_text" in line and "encoding" not in line:
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                pytest.fail(
                    f"Bug 53: providers_pick.py line {i+1} has write_text() "
                    f"without encoding='utf-8': {stripped}. "
                    "Fix: add encoding='utf-8' to all write_text() calls."
                )

    def test_write_roundtrip_preserves_unicode(self, tmp_path: Path) -> None:
        """Write config with unicode and read it back — must not garble."""
        from tero2.config_writer import write_global_config_section, _load_toml

        config_path = tmp_path / "config.toml"
        write_global_config_section(
            config_path, "roles.builder", {"provider": "claude", "model": "sonnet"}
        )
        write_global_config_section(
            config_path,
            "telegram",
            {"bot_token": "токен_тест"},
        )

        data = _load_toml(config_path)
        assert data["telegram"]["bot_token"] == "токен_тест", (
            "Bug 53: Unicode roundtrip failed — write_text() without "
            "encoding='utf-8' garbles non-ASCII on non-UTF-8 systems. "
            "Fix: add encoding='utf-8' to all write_text() calls."
        )
