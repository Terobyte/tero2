"""Halal tests for bugs 86–97 (Audit 4, 2026-04-21).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug 86  catalog.py: subprocess leak on TimeoutError in fetch_cli_models()
  Bug 87  cli provider: generator early exit leaks subprocess (no try/finally)
  Bug 88  events.py: subscribe/unsubscribe race with emit (no lock)
  Bug 89  stream_bus.py: subscribe/unsubscribe race with publish (no lock)
  Bug 90  execute_phase: div_steps not reset on BACKTRACK_COACH
  Bug 91  telegram_input: HTTP error responses not checked before json()
  Bug 92  telegram_input: subprocess stderr not drained on TimeoutError
  Bug 93  shell provider: proc.terminate() without kill fallback (can hang)
  Bug 94  state.py: bare except catches SystemExit and KeyboardInterrupt
  Bug 95  history.py: no locking in record_run() (TOCTOU on concurrent writes)
  Bug 96  notifier: TTS audio files never cleaned up after upload
  Bug 97  providers_pick: dead tmp_path code (misleads as atomic write)
"""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 86: catalog subprocess leak on TimeoutError ───────────────────────────


class TestBug86CatalogSubprocessLeakOnTimeout:
    """fetch_cli_models() catches TimeoutError but never kills the subprocess.
    The process keeps running as a zombie until the parent exits.
    Fix: call proc.kill() (and optionally proc.wait()) before returning fallback.
    """

    @pytest.mark.asyncio
    async def test_fetch_kills_proc_on_timeout(self) -> None:
        from tero2.providers.catalog import fetch_cli_models

        killed: list[bool] = []

        mock_proc = AsyncMock()
        mock_proc.returncode = None
        mock_proc.kill = MagicMock(side_effect=lambda: killed.append(True))
        mock_proc.wait = AsyncMock()

        with patch(
            "asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc
        ), patch("asyncio.wait_for", side_effect=asyncio.TimeoutError()):
            result = await fetch_cli_models("zai")

        # Returns fallback (not raises) — that part works
        assert isinstance(result, list)

        assert killed, (
            "Bug 86: fetch_cli_models() caught TimeoutError but never called proc.kill(). "
            "The subprocess becomes a zombie. "
            "Fix: add proc.kill() in the TimeoutError except block before returning fallback."
        )

    def test_fetch_source_kills_proc_on_timeout(self) -> None:
        """Structural: the except block that catches TimeoutError must call proc.kill()."""
        from tero2.providers import catalog as cat_module

        source = inspect.getsource(cat_module.fetch_cli_models)
        lines = source.splitlines()

        timeout_except_idx = next(
            (i for i, l in enumerate(lines) if "TimeoutError" in l and "except" in l), None
        )
        assert timeout_except_idx is not None, "TimeoutError except not found — check setup"

        window = "\n".join(lines[timeout_except_idx : timeout_except_idx + 6])
        assert "proc.kill" in window or "kill()" in window, (
            "Bug 86: except block for TimeoutError has no proc.kill() call. "
            "Fix: add proc.kill() before returning the static fallback."
        )


# ── Bug 87: cli provider generator early exit leaks subprocess ────────────────


class TestBug87CLIGeneratorEarlyExitLeakSubprocess:
    """CLIProvider.run() yields events from _stream_events(proc) with no
    try/finally.  If the consumer breaks early or is cancelled, proc is never
    killed and becomes a zombie.
    Fix: wrap the async-for loop in try/finally: proc.kill(); await proc.wait().
    """

    def test_run_has_finally_to_cleanup_proc(self) -> None:
        from tero2.providers.cli import CLIProvider

        source = inspect.getsource(CLIProvider.run)
        lines = source.splitlines()

        yield_loop_idx = next(
            (i for i, l in enumerate(lines) if "_stream_events" in l), None
        )
        assert yield_loop_idx is not None, "_stream_events line not found — check setup"

        # proc cleanup (kill or wait) must appear AFTER the _stream_events line.
        # In the current buggy code, proc.kill/wait only appear BEFORE it (stdin section).
        after_yield = "\n".join(lines[yield_loop_idx + 1 :])
        has_cleanup = "proc.kill" in after_yield or "proc.wait" in after_yield

        assert has_cleanup, (
            "Bug 87: CLIProvider.run() has no proc.kill/wait after the event-yield loop. "
            "If the consumer breaks/cancels, the subprocess leaks. "
            "Fix: wrap `async for event in self._stream_events(proc): yield event` "
            "in try/finally that calls proc.kill() and await proc.wait()."
        )

    @pytest.mark.asyncio
    async def test_proc_killed_on_consumer_break(self, tmp_path: Path) -> None:
        """Functional: stopping iteration early must trigger proc cleanup."""
        from tero2.providers.cli import CLIProvider

        killed: list[bool] = []

        async def _infinite_stdout():
            while True:
                yield b"line\n"
                await asyncio.sleep(0)

        mock_proc = MagicMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.drain = AsyncMock()
        mock_proc.stdin.wait_closed = AsyncMock()
        mock_proc.returncode = None
        mock_proc.kill = MagicMock(side_effect=lambda: killed.append(True))
        mock_proc.wait = AsyncMock()
        mock_proc.stdout = _infinite_stdout()
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")

        with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
            provider = CLIProvider("claude", working_dir=str(tmp_path))
            gen = provider.run(prompt="hi")
            try:
                await gen.__anext__()  # get one item
            except StopAsyncIteration:
                pass
            await gen.aclose()  # simulate consumer stopping early

        assert killed or mock_proc.wait.called, (
            "Bug 87: proc.kill()/wait() not called when consumer closed generator early. "
            "Fix: add try/finally around the event-yield loop in CLIProvider.run()."
        )


# ── Bug 88: events.py subscribe/unsubscribe race with emit ───────────────────


class TestBug88EventsSubscribeRaceWithEmit:
    """subscribe() and unsubscribe() modify _subscribers without acquiring
    _emit_lock, while emit() holds the lock during iteration.
    A concurrent subscribe/unsubscribe can cause RuntimeError or dropped events.
    Fix: acquire _emit_lock in both subscribe() and unsubscribe().
    """

    def test_subscribe_acquires_emit_lock(self) -> None:
        from tero2.events import EventDispatcher

        source = inspect.getsource(EventDispatcher.subscribe)
        assert "_emit_lock" in source or "acquire" in source, (
            "Bug 88: EventDispatcher.subscribe() does not acquire _emit_lock. "
            "Concurrent subscribe during emit() can mutate the list mid-iteration. "
            "Fix: acquire self._emit_lock inside subscribe()."
        )

    def test_unsubscribe_acquires_emit_lock(self) -> None:
        from tero2.events import EventDispatcher

        source = inspect.getsource(EventDispatcher.unsubscribe)
        assert "_emit_lock" in source or "acquire" in source, (
            "Bug 88: EventDispatcher.unsubscribe() does not acquire _emit_lock. "
            "Concurrent unsubscribe during emit() risks RuntimeError on list iteration. "
            "Fix: acquire self._emit_lock inside unsubscribe()."
        )


# ── Bug 89: stream_bus.py subscribe/unsubscribe race with publish ─────────────


class TestBug89StreamBusSubscribeRaceWithPublish:
    """subscribe() and unsubscribe() modify _subscribers without any lock,
    while _publish_impl() iterates the same list.
    Fix: add a threading.Lock to protect _subscribers in all three methods.
    """

    def test_stream_bus_has_subscriber_lock(self) -> None:
        from tero2.stream_bus import StreamBus

        source = inspect.getsource(StreamBus)
        has_lock = (
            "threading.Lock()" in source
            or "_sub_lock" in source
            or "_lock" in source
        )
        assert has_lock, (
            "Bug 89: StreamBus has no threading.Lock protecting _subscribers. "
            "Concurrent subscribe/unsubscribe during _publish_impl() can crash. "
            "Fix: add a threading.Lock() and acquire it in subscribe(), unsubscribe(), "
            "and _publish_impl()."
        )

    def test_subscribe_acquires_lock(self) -> None:
        from tero2.stream_bus import StreamBus

        source = inspect.getsource(StreamBus.subscribe)
        has_lock = "lock" in source.lower() or "acquire" in source
        assert has_lock, (
            "Bug 89: StreamBus.subscribe() does not acquire the subscriber lock. "
            "Fix: wrap self._subscribers.append() with the lock."
        )

    def test_unsubscribe_acquires_lock(self) -> None:
        from tero2.stream_bus import StreamBus

        source = inspect.getsource(StreamBus.unsubscribe)
        has_lock = "lock" in source.lower() or "acquire" in source
        assert has_lock, (
            "Bug 89: StreamBus.unsubscribe() does not acquire the subscriber lock. "
            "Fix: wrap self._subscribers.remove() with the lock."
        )


# ── Bug 90: execute_phase div_steps not reset on BACKTRACK_COACH ─────────────


class TestBug90ExecutePhaseDivStepsNotResetOnBacktrackCoach:
    """execute_phase increments div_steps on DIVERSIFICATION but never resets it
    on BACKTRACK_COACH, while runner.py does reset it.  Escalation logic in
    execute_phase uses stale div_steps, potentially escalating prematurely.
    Fix: add div_steps reset for BACKTRACK_COACH in execute_phase.py.
    """

    def test_execute_phase_resets_div_steps_on_backtrack_coach(self) -> None:
        from tero2.phases import execute_phase as ep_module

        source = inspect.getsource(ep_module)
        has_reset = (
            "BACKTRACK_COACH" in source
            and "div_steps" in source
        )
        if not has_reset:
            pytest.fail(
                "Bug 90: execute_phase never references BACKTRACK_COACH with div_steps. "
                "Fix: add `ctx.div_steps = 0` when esc_action.level == BACKTRACK_COACH."
            )

        lines = source.splitlines()
        backtrack_indices = [
            i for i, l in enumerate(lines) if "BACKTRACK_COACH" in l
        ]
        for idx in backtrack_indices:
            window = "\n".join(lines[idx : idx + 5])
            if "div_steps" in window and ("= 0" in window or "=0" in window):
                return  # fix is present

        pytest.fail(
            "Bug 90: execute_phase has BACKTRACK_COACH branch but does not reset div_steps. "
            "runner.py resets ctx.div_steps = 0 on BACKTRACK_COACH (line 329-330). "
            "Fix: add `ctx.div_steps = 0` in the BACKTRACK_COACH handler."
        )


# ── Bug 91: telegram_input HTTP error responses not checked ───────────────────


class TestBug91TelegramInputHTTPErrorNotChecked:
    """_poll_once() calls resp.json() without checking resp.status_code.
    A 429 or 500 response with valid JSON is silently processed as if success.
    Fix: check resp.status_code == 200 before parsing, skip on error.
    """

    def test_poll_once_checks_status_code(self) -> None:
        from tero2.telegram_input import TelegramInput

        source = inspect.getsource(TelegramInput._poll_once)
        has_status_check = "status_code" in source or "status" in source.lower()
        assert has_status_check, (
            "Bug 91: _poll_once() never checks resp.status_code. "
            "A 429 or 500 with valid JSON body is processed as if it were success. "
            "Fix: add `if resp.status_code != 200: return [], offset` before resp.json()."
        )

    @pytest.mark.asyncio
    async def test_poll_once_skips_updates_on_non_200(self) -> None:
        """A 429 response must not produce any updates."""
        from tero2.telegram_input import TelegramInput, TelegramConfig

        cfg = TelegramConfig(
            bot_token="fake",
            chat_id="123",
            allowed_ids=["123"],
        )
        ti = TelegramInput.__new__(TelegramInput)
        ti.config = cfg

        mock_resp = MagicMock()
        mock_resp.status_code = 429
        mock_resp.json.return_value = {
            "ok": False,
            "result": [{"update_id": 1, "message": {"text": "hi"}}],
        }

        with patch("requests.post", return_value=mock_resp):
            updates, new_offset = await ti._poll_once(0)

        assert updates == [], (
            "Bug 91: _poll_once returned updates from a 429 response. "
            "Fix: skip resp.json() when status_code != 200."
        )


# ── Bug 92: telegram_input stderr not drained on TimeoutError ────────────────


class TestBug92TelegramWatcherStderrNotDrained:
    """_watch_runner() silently passes on TimeoutError without draining stderr.
    When the subprocess fills its stderr buffer it blocks on write, causing a
    silent hang.
    Fix: close or drain proc.stderr after the timeout.
    """

    def test_watch_runner_drains_stderr_on_timeout(self) -> None:
        from tero2.telegram_input import TelegramInput

        source = inspect.getsource(TelegramInput._watch_runner)
        lines = source.splitlines()

        timeout_idx = next(
            (i for i, l in enumerate(lines) if "TimeoutError" in l), None
        )
        assert timeout_idx is not None, "TimeoutError handler not found in _watch_runner"

        window = "\n".join(lines[timeout_idx : timeout_idx + 8])
        has_drain = (
            "stderr" in window
            and any(kw in window for kw in ("read", "drain", "close", "cancel"))
        )
        assert has_drain, (
            "Bug 92: _watch_runner on TimeoutError just `pass`es without draining stderr. "
            "If subprocess fills the stderr pipe buffer it will hang on write. "
            "Fix: drain or close proc.stderr after the timeout."
        )


# ── Bug 93: shell provider proc.terminate() without kill fallback ─────────────


class TestBug93ShellTerminateNoKillFallback:
    """ShellProvider.run() calls proc.terminate() on exception then awaits
    proc.wait() with no timeout.  If the child ignores SIGTERM, wait() blocks
    forever.
    Fix: use asyncio.wait_for(proc.wait(), timeout=5), then proc.kill() on
    TimeoutError.
    """

    def test_shell_run_has_kill_fallback(self) -> None:
        from tero2.providers.shell import ShellProvider

        source = inspect.getsource(ShellProvider.run)
        has_kill = "proc.kill" in source or ".kill()" in source
        has_wait_timeout = "wait_for" in source and "proc.wait" in source

        assert has_kill or has_wait_timeout, (
            "Bug 93: ShellProvider.run() calls proc.terminate() then awaits proc.wait() "
            "with no timeout. If SIGTERM is ignored, wait() hangs indefinitely. "
            "Fix: use asyncio.wait_for(proc.wait(), timeout=5) and call proc.kill() on "
            "TimeoutError."
        )

    @pytest.mark.asyncio
    async def test_shell_proc_wait_is_bounded(self) -> None:
        """proc.wait() after terminate() must have a timeout so it cannot hang.

        Bug: `await proc.wait()` has no timeout. If child ignores SIGTERM the
        provider hangs forever in the except block.
        Fix: asyncio.wait_for(proc.wait(), timeout=5) + proc.kill() on TimeoutError.
        """
        from tero2.providers.shell import ShellProvider

        wait_started = asyncio.Event()
        wait_unblocked = asyncio.Event()

        class _SigtermIgnoringProc:
            returncode = None

            async def communicate(self):
                raise asyncio.TimeoutError("simulate timeout")

            def terminate(self):
                pass  # SIGTERM ignored — proc.wait() will hang

            async def wait(self):
                wait_started.set()
                await wait_unblocked.wait()  # blocks until kill() is called

            def kill(self):
                wait_unblocked.set()  # SIGKILL unblocks wait()

        with patch(
            "asyncio.create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=_SigtermIgnoringProc(),
        ):
            provider = ShellProvider()
            task = asyncio.create_task(_collect_all(provider.run(prompt="sleep 9999")))
            try:
                # Give the task a short window; if it hangs proc.wait() will time out here
                await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
            except (asyncio.TimeoutError, Exception):
                pass

        # BUG present: task is still running (blocked in proc.wait())
        # Fix present: task finished because proc.kill() was called after wait_for timeout
        assert task.done(), (
            "Bug 93: ShellProvider.run() blocked forever in proc.wait() after proc.terminate(). "
            "proc.kill() was never called as a fallback. "
            "Fix: use asyncio.wait_for(proc.wait(), timeout=5) and call proc.kill() on "
            "TimeoutError."
        )
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _collect_all(gen):
    result = []
    async for item in gen:
        result.append(item)
    return result


# ── Bug 94: state.py bare except catches SystemExit / KeyboardInterrupt ───────


class TestBug94StateBareExcept:
    """AgentState.save() uses `except:` (bare) around os.replace().
    This catches KeyboardInterrupt and SystemExit, preventing process
    interruption during a failed save.
    Fix: change `except:` to `except OSError:`.
    """

    def test_save_uses_oserror_not_bare_except(self) -> None:
        from tero2.state import AgentState

        source = inspect.getsource(AgentState.save)
        # bare `except:` (with no exception type)
        import re
        bare_excepts = re.findall(r"\bexcept\s*:", source)
        assert not bare_excepts, (
            "Bug 94: AgentState.save() uses bare `except:` around os.replace(). "
            "This silently catches KeyboardInterrupt and SystemExit. "
            "Fix: change `except:` to `except OSError:`."
        )



# ── Bug 95: history.py no locking in record_run() ────────────────────────────


class TestBug95HistoryNoLockOnConcurrentWrite:
    """record_run() does read-modify-write without any file lock.
    Two concurrent tero2 instances lose each other's history entries.
    Fix: use fcntl.flock() around the read-modify-write in _write() or record_run().
    """

    def test_record_run_uses_file_lock(self) -> None:
        import tero2.history as hist_module

        source = inspect.getsource(hist_module)
        has_lock = (
            "flock" in source
            or "fcntl" in source
            or "FileLock" in source
            or "threading.Lock" in source
        )
        assert has_lock, (
            "Bug 95: tero2/history.py has no file locking in record_run() / _write(). "
            "Two concurrent tero2 instances both read→update→write, last writer wins. "
            "Fix: use fcntl.flock() around the read-modify-write in _write()."
        )


# ── Bug 96: notifier TTS audio file never cleaned up ─────────────────────────


class TestBug96NotifierAudioFileNotCleaned:
    """send_voice() generates a TTS audio file and uploads it but never deletes it.
    Each voice notification leaks ~50-200KB on disk.
    Fix: add `finally: audio_path.unlink(missing_ok=True)` after upload.
    """

    def test_send_voice_has_finally_unlink(self) -> None:
        from tero2.notifier import Notifier

        source = inspect.getsource(Notifier.send_voice)
        has_finally = "finally" in source
        has_unlink = "unlink" in source

        assert has_finally and has_unlink, (
            "Bug 96: Notifier.send_voice() never deletes the TTS audio file. "
            "Each call leaks a temporary file on disk. "
            "Fix: add `finally: audio_path.unlink(missing_ok=True)` after upload."
        )

    @pytest.mark.asyncio
    async def test_audio_file_deleted_after_upload(self, tmp_path: Path) -> None:
        """Functional: the audio temp file must be gone after send_voice() returns."""
        from tero2.notifier import Notifier, NotifierConfig

        audio_file = tmp_path / "tts.ogg"
        audio_file.write_bytes(b"fake audio")

        cfg = NotifierConfig(bot_token="tok", chat_id="123", enabled=True)
        notifier = Notifier.__new__(Notifier)
        notifier.config = cfg
        notifier._enabled = True

        with patch.object(notifier, "_generate_tts", return_value=audio_file), patch(
            "requests.post",
            return_value=MagicMock(status_code=200),
        ):
            await notifier.send_voice("hello")

        assert not audio_file.exists(), (
            "Bug 96: TTS audio file still exists after send_voice() returned. "
            "Fix: add `finally: audio_path.unlink(missing_ok=True)` after upload."
        )


# ── Bug 97: providers_pick dead tmp_path code ────────────────────────────────


class TestBug97ProvidersPickDeadTmpPath:
    """_write_project_config() creates tmp_path but writes directly to config_path.
    The finally block cleans up a file that was never created.
    This misleads readers into thinking atomic write semantics are in place.
    Fix: either remove dead tmp_path code, or implement actual atomic write
    (write to tmp, then rename over config_path).
    """

    def test_write_project_config_actually_uses_tmp(self) -> None:
        from tero2.tui.screens.providers_pick import ProvidersPickScreen

        source = inspect.getsource(ProvidersPickScreen._write_project_config)
        lines = source.splitlines()

        tmp_line_idx = next(
            (i for i, l in enumerate(lines) if "tmp_path" in l and "=" in l), None
        )
        assert tmp_line_idx is not None, "tmp_path variable not found — check test setup"

        # Either tmp_path is written to (write_text / open) or renamed (replace/rename)
        # OR the dead code is removed entirely (no tmp_path at all)
        after_tmp = "\n".join(lines[tmp_line_idx + 1 :])
        has_tmp_write = any(
            kw in after_tmp
            for kw in ("tmp_path.write", "open(tmp_path", "tmp_path.rename", "os.replace(tmp_path")
        )
        no_tmp_at_all = "tmp_path" not in after_tmp

        assert has_tmp_write or no_tmp_at_all, (
            "Bug 97: _write_project_config creates tmp_path but never writes to it. "
            "Writes go directly to config_path, making the finally block dead code. "
            "Fix: either remove tmp_path entirely, or implement atomic write "
            "(write to tmp_path first, then os.replace(tmp_path, config_path))."
        )
