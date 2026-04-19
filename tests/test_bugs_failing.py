"""
Failing tests demonstrating known bugs from bugs.md.

  Bug 7  — CLIProvider: stdin fd leaked when drain() raises non-BrokenPipe
  Bug 8  — CLIProvider: stderr_task abandoned when stdout reading raises
  Bug 18 — CircuitBreaker: HALF_OPEN never blocks (unlimited trial calls)
  Bug 21 — ShellProvider: no proc.terminate() when communicate() raises
  Bug 25 — DiskLayer: read_file indistinguishable for missing / empty / permission-denied
  Bug 32 — PlanPickScreen: stat() in sort key unguarded (TOCTOU crash)

NOTE: Bug 31 (ProviderChain index stale) is NOT included — it was already fixed
in the current codebase (index is set after the CB skip check, not before).
"""

from __future__ import annotations

import asyncio
import stat as stat_mod
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.circuit_breaker import CBState, CircuitBreaker
from tero2.disk_layer import DiskLayer
from tero2.errors import CircuitOpenError
from tero2.providers.cli import CLIProvider
from tero2.providers.shell import ShellProvider


# ─────────────────────────────────────────────────────────────────────────────
# Bug 18 — CircuitBreaker HALF_OPEN never blocks
# ─────────────────────────────────────────────────────────────────────────────

def test_half_open_blocks_second_call_before_outcome_recorded():
    """HALF_OPEN must allow ONE trial call; every subsequent call must raise
    CircuitOpenError until success or failure is recorded.

    Current code::

        if self.state == CBState.HALF_OPEN:
            return  # ← always allows through, no single-trial enforcement

    Bug: a slow provider in HALF_OPEN lets unlimited concurrent/sequential
    calls through — defeats the purpose of the half-open probe.
    """
    cb = CircuitBreaker(name="svc", failure_threshold=1, recovery_timeout_s=0)
    cb.record_failure()
    assert cb.state == CBState.OPEN

    cb.last_failure_time = 0.0  # force recovery timeout to be elapsed

    # First call: OPEN → HALF_OPEN transition; trial call is allowed
    cb.check()
    assert cb.state == CBState.HALF_OPEN

    # Second call WITHOUT recording any outcome must block
    with pytest.raises(CircuitOpenError):
        cb.check()  # BUG: currently returns silently — never raises


# ─────────────────────────────────────────────────────────────────────────────
# Bug 25 — DiskLayer.read_file: indistinguishable error returns
# ─────────────────────────────────────────────────────────────────────────────

def test_read_file_missing_differs_from_empty(tmp_path):
    """A missing file and an empty file must produce distinguishable results.

    Current code::

        except (OSError, FileNotFoundError):
            return ""

    Bug: both missing files and empty files return ``""``. Callers that need to
    distinguish "file absent" from "file has no content" (e.g., to decide
    whether to create the file) are silently misled.
    """
    disk = DiskLayer(tmp_path)
    disk.init()

    # Write an explicitly empty file
    steer_path = disk.sora_dir / "human" / "STEER.md"
    steer_path.write_text("")

    empty_result = disk.read_file("human/STEER.md")
    missing_result = disk.read_file("human/NONEXISTENT_FILE.md")

    # BUG: both are "" → assertion fails
    assert empty_result != missing_result, (
        f"BUG: read_file returns {missing_result!r} for a missing file and "
        f"{empty_result!r} for an empty file — they are indistinguishable."
    )


def test_read_file_permission_error_differs_from_missing(tmp_path):
    """A PermissionError and a FileNotFoundError must produce different results.

    Bug: the broad ``except (OSError, ...)`` swallows permission errors the same
    way as missing-file errors — callers can't detect access-control violations.
    """
    disk = DiskLayer(tmp_path)
    disk.init()

    restricted = disk.sora_dir / "human" / "restricted.md"
    restricted.write_text("secret content")
    restricted.chmod(0o000)  # remove all permissions

    try:
        perm_result = disk.read_file("human/restricted.md")
        missing_result = disk.read_file("human/gone.md")

        # BUG: both are "" → assertion fails
        assert perm_result != missing_result, (
            f"BUG: PermissionError returns {perm_result!r}, same as "
            f"FileNotFoundError {missing_result!r}. "
            "Permission failures are silently swallowed."
        )
    finally:
        restricted.chmod(stat_mod.S_IRUSR | stat_mod.S_IWUSR)


# ─────────────────────────────────────────────────────────────────────────────
# Bug 7 — CLIProvider: stdin not closed when drain() raises non-BrokenPipe
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cli_stdin_closed_when_drain_raises_connection_reset():
    """stdin.close() must be called even when drain() raises something other than
    BrokenPipeError.

    Current code::

        try:
            proc.stdin.write(data)
            await proc.stdin.drain()
        except BrokenPipeError:
            raise ProviderError(...)
        proc.stdin.close()          # ← NOT reached for any other error

    Bug: ConnectionResetError, OSError, etc. skip the close() path entirely —
    the underlying fd is leaked until GC runs.
    """
    closed: list[bool] = []

    class _MockStdin:
        def write(self, data: bytes) -> None:
            pass

        async def drain(self) -> None:
            raise ConnectionResetError("connection reset by peer")

        def close(self) -> None:
            closed.append(True)

        async def wait_closed(self) -> None:
            pass

    mock_proc = MagicMock()
    mock_proc.stdin = _MockStdin()
    mock_proc.returncode = 0

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        provider = CLIProvider("claude")
        with pytest.raises(ConnectionResetError):
            async for _ in provider.run(prompt="hello"):
                pass

    # BUG: closed is [] → assert fails
    assert closed, (
        "BUG: stdin.close() was NOT called after drain() raised ConnectionResetError. "
        "File descriptor leaked — only BrokenPipeError is guarded."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 21 — ShellProvider: no proc.terminate() on communicate() exception
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_shell_terminates_process_when_communicate_raises():
    """proc.terminate() must be called if communicate() raises.

    Current code::

        stdout, stderr = await proc.communicate()  # no try/finally
        ...

    Bug: if communicate() raises (timeout, I/O error, etc.), the subprocess is
    never signalled and becomes a zombie.
    """
    terminated: list[bool] = []

    class _MockProc:
        returncode = None

        async def communicate(self) -> tuple[bytes, bytes]:
            raise asyncio.TimeoutError("process took too long")

        def terminate(self) -> None:
            terminated.append(True)

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=_MockProc()):
        provider = ShellProvider()
        with pytest.raises(asyncio.TimeoutError):
            async for _ in provider.run(prompt="sleep 9999"):
                pass

    # BUG: terminated is [] → assert fails
    assert terminated, (
        "BUG: proc.terminate() was NOT called after communicate() raised TimeoutError. "
        "Subprocess became a zombie — no cleanup path for exception."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Bug 8 — CLIProvider: stderr_task abandoned on stdout read error
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cli_stderr_task_not_leaked_when_stdout_raises():
    """The stderr background task must be cancelled/awaited if stdout reading raises.

    Current code::

        stderr_task = asyncio.create_task(proc.stderr.read())
        async for line in proc.stdout:    # ← exception propagates here
            ...
        stderr_bytes = await stderr_task  # ← never reached → task leaked

    Bug: no try/finally — stderr_task runs indefinitely in the background,
    preventing process cleanup and generating "Task destroyed pending" warnings.
    """
    captured_tasks: list[asyncio.Task] = []
    _real_create_task = asyncio.create_task

    def _tracking_create_task(coro: Any, **kwargs: Any) -> asyncio.Task:
        task = _real_create_task(coro, **kwargs)
        captured_tasks.append(task)
        return task

    class _BrokenStdout:
        def __aiter__(self) -> "_BrokenStdout":
            return self

        async def __anext__(self) -> bytes:
            raise OSError("stdout pipe broken unexpectedly")

    class _BlockingStderr:
        async def read(self) -> bytes:
            await asyncio.sleep(60)  # never returns during test
            return b""

    mock_proc = MagicMock()
    mock_proc.stdout = _BrokenStdout()
    mock_proc.stderr = _BlockingStderr()
    mock_proc.returncode = 0
    mock_proc.wait = AsyncMock()
    mock_proc.stdin.drain = AsyncMock()
    mock_proc.stdin.wait_closed = AsyncMock()

    with patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc):
        with patch("asyncio.create_task", side_effect=_tracking_create_task):
            provider = CLIProvider("claude")
            with pytest.raises(OSError):
                async for _ in provider.run(prompt="test"):
                    pass

    assert captured_tasks, (
        "stderr_task was never created — proc.stderr was falsy in mock. "
        "Fix the test setup, not the source."
    )
    task = captured_tasks[0]

    try:
        # BUG: task.done() is False and task.cancelled() is False → assert fails
        assert task.done() or task.cancelled(), (
            f"BUG: stderr_task is still PENDING after stdout raised OSError. "
            f"done={task.done()}, cancelled={task.cancelled()}. "
            "Background task runs indefinitely — resource leak."
        )
    finally:
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=0.05)
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                pass


# ─────────────────────────────────────────────────────────────────────────────
# Bug 32 — PlanPickScreen: stat() in sort key unguarded (TOCTOU crash)
# ─────────────────────────────────────────────────────────────────────────────

def test_plan_pick_scan_does_not_crash_when_file_deleted_between_collect_and_sort(tmp_path):
    """PlanPickScreen must not crash if a .md file disappears after rglob but
    before the sort(key=p.stat().st_mtime) call.

    Current code in _scan_md_files()::

        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        # ← no try/except: FileNotFoundError propagates if a file was deleted

    Bug: TOCTOU race — file collected by rglob, is_file() returns True, file
    is added to the list; then another process deletes it; sort raises
    FileNotFoundError and __init__ crashes.
    Expected: return the surviving files without crashing.
    """
    from tero2.tui.screens.plan_pick import PlanPickScreen

    plan = tmp_path / "plan.md"
    plan.write_text("# plan")

    original_scan = PlanPickScreen._scan_md_files

    def _scan_with_race(self: PlanPickScreen) -> list[Path]:
        """Replicate _scan_md_files but inject a deletion just before the sort."""
        files: list[Path] = []
        for p in self._project_path.rglob("*.md"):
            if p.is_file():
                files.append(p)

        plan.unlink()  # simulate race: deleted after collection, before sort

        # Exact sort from the real implementation — triggers the bug
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:30]

    PlanPickScreen._scan_md_files = _scan_with_race  # type: ignore[method-assign]
    try:
        # BUG: raises FileNotFoundError — should return [] gracefully
        screen = PlanPickScreen(tmp_path)
        # If we reach here, the fix works
        assert screen._files == [], f"Expected [] after deleted file, got {screen._files}"
    except (OSError, FileNotFoundError) as exc:
        pytest.fail(
            f"BUG: _scan_md_files crashed with {type(exc).__name__} when a file was "
            f"deleted between rglob collection and the sort() call: {exc}"
        )
    finally:
        PlanPickScreen._scan_md_files = original_scan  # type: ignore[method-assign]
