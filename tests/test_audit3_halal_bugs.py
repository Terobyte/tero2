"""Negative tests for Audit 3 medium/complex bugs — makes them "halal".

Convention: test FAILS when bug is present, PASSES when fixed.

Bugs tested:
  Bug 65  lock.py: fd leaked when os.write fails after flock succeeds
  Bug 67  cli.py: subprocess not cleaned up when BrokenPipeError on stdin drain
  Bug 71  architect: malformed task headers silently dropped (no tracking)
  Bug 72  coach: context truncation at _SIZE_CAP has no marker in output
  Bug 77  config: load_config has no thread synchronization
  Bug 78  startup_wizard: _on_plan_picked(None) continues instead of dismissing
  Bug 79  project_pick: _pending_delete persists across navigation (stale)
  Bug 80  model_pick: ListView rebuilt on every keystroke without debounce
  Bug 82  state: tmp file left on disk when os.replace fails
  Bug 83  disk_layer: write_file lets OSError propagate to caller
"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 65: lock.py fd leak on write error ────────────────────────────────────


class TestBug65LockFDLeakOnWriteError:
    """After flock() succeeds, os.write/os.lseek/os.truncate can raise.
    The fd is opened but never stored in self._fd — no cleanup path exists.
    Fix: wrap the write section in try/finally: os.close(fd) on any exception.
    """

    def test_acquire_has_write_section_try_block(self) -> None:
        """Structural: acquire() must have a second try block after flock."""
        from tero2.lock import FileLock

        source = inspect.getsource(FileLock.acquire)
        try_count = source.count("try:")
        assert try_count >= 2, (
            "Bug 65: acquire() only has one try/except (for flock). "
            "Need a second try/finally after flock to close fd if write/truncate fails."
        )

    def test_fd_closed_when_write_raises(self, tmp_path: Path) -> None:
        """Functional: fd must be closed even when os.write raises."""
        from tero2.lock import FileLock

        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path)

        captured_fd: list[int] = []

        orig_lseek = os.lseek

        def spy_lseek(fd: int, *args: object) -> int:
            captured_fd.append(fd)
            return orig_lseek(fd, *args)  # type: ignore[arg-type]

        with patch("tero2.lock.os.lseek", side_effect=spy_lseek), patch(
            "tero2.lock.os.write", side_effect=OSError("simulated disk full")
        ):
            with pytest.raises(OSError, match="disk full"):
                lock.acquire()

        assert captured_fd, "os.lseek was never called — check test setup"
        fd = captured_fd[0]
        try:
            os.fstat(fd)
            pytest.fail(
                f"Bug 65: fd {fd} still open after os.write failure in acquire(). "
                "Fix: add try/finally after flock succeeds to call os.close(fd) on error."
            )
        except OSError:
            pass  # fd is closed — fix is applied


# ── Bug 67: cli.py process leak on stdin exception ────────────────────────────


class TestBug67CLIProcessLeakOnBrokenPipe:
    """When proc.stdin.drain() raises BrokenPipeError, ProviderError propagates
    before _stream_events is ever reached, so proc.wait() inside _stream_events
    is never called.  The subprocess becomes a zombie.
    Fix: add try/finally around proc to call proc.kill()/proc.wait() on error.
    """

    @pytest.mark.asyncio
    async def test_proc_cleaned_up_on_broken_pipe(self, tmp_path: Path) -> None:
        from tero2.errors import ProviderError
        from tero2.providers.cli import CLIProvider

        provider = CLIProvider("echo", working_dir=str(tmp_path))

        mock_proc = MagicMock()
        mock_proc.stdin = AsyncMock()
        mock_proc.stdin.drain = AsyncMock(side_effect=BrokenPipeError("test"))
        mock_proc.stdin.close = MagicMock()
        mock_proc.stdin.wait_closed = AsyncMock()
        mock_proc.wait = AsyncMock(return_value=1)
        mock_proc.kill = MagicMock()
        mock_proc.returncode = None

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with pytest.raises(ProviderError, match="Broken pipe"):
                async for _ in provider.run(prompt="hello"):
                    pass

        assert mock_proc.wait.called or mock_proc.kill.called, (
            "Bug 67: proc.wait()/kill() not called when BrokenPipeError raised "
            "during stdin.drain(). Subprocess becomes zombie. "
            "Fix: wrap proc lifecycle in try/finally."
        )


# ── Bug 71: architect malformed headers silently dropped ──────────────────────


class TestBug71ArchitectMalformedHeadersDropped:
    """_parse_slice_plan silently drops headers not matching _TASK_ID_RE.
    Caller receives an incomplete SlicePlan with no indication of the loss.
    Fix: track dropped headers in SlicePlan (e.g. dropped_headers: list[str]).
    """

    def test_slice_plan_tracks_dropped_headers(self) -> None:
        from tero2.players.architect import _parse_slice_plan

        plan = (
            "## T01: Valid task\nDescription.\n**Must-haves:**\n- step 1\n\n"
            "## Task: Malformed Header\nThis body has no T-code.\n"
            "## Another Section Without Code\nMore content.\n"
        )
        result = _parse_slice_plan(plan, "S01", "milestones/M01/S01")

        assert len(result.tasks) == 1, "T01 should be parsed"

        dropped = getattr(result, "dropped_headers", None)
        assert dropped is not None and len(dropped) > 0, (
            "Bug 71: _parse_slice_plan drops malformed headers without tracking. "
            "Fix: add `dropped_headers: list[str]` to SlicePlan and populate it "
            "whenever a header has no T-code."
        )


# ── Bug 72: coach context truncation without marker ───────────────────────────


class TestBug72CoachTruncationNoMarker:
    """_gather_context breaks out of the task-reading loop when _SIZE_CAP (50K)
    is hit, but appends no truncation marker.  The output silently omits tasks.
    Fix: append a "[TRUNCATED ...]" sentinel to summaries when cap is hit.
    """

    def test_size_cap_break_adds_truncation_marker(self) -> None:
        from tero2.players.coach import Coach

        source = inspect.getsource(Coach._gather_context)
        lines = source.splitlines()

        # Find the line that checks _SIZE_CAP and breaks
        cap_line_idx = next(
            (i for i, l in enumerate(lines) if "_SIZE_CAP" in l and ">" in l), None
        )
        assert cap_line_idx is not None, "Could not find _SIZE_CAP check — check test setup"

        # Within the next 8 lines after the cap check there must be a truncation marker
        window = "\n".join(lines[cap_line_idx : cap_line_idx + 8])
        assert "truncat" in window.lower() or "TRUNCAT" in window, (
            "Bug 72: coach._gather_context breaks at _SIZE_CAP without appending "
            "a truncation marker to `summaries`. Tasks are silently omitted. "
            "Fix: append '[TRUNCATED — context limit reached]' to summaries on break."
        )


# ── Bug 77: config.load_config not thread-safe ────────────────────────────────


class TestBug77ConfigThreadUnsafe:
    """load_config() reads and merges TOML without any synchronization.
    Two threads can interleave TOML parsing and receive inconsistent config.
    Fix: add a module-level threading.Lock around config loading.
    """

    def test_config_module_has_load_lock(self) -> None:
        from tero2 import config as cfg_module

        source = inspect.getsource(cfg_module)
        has_lock = (
            "threading.Lock()" in source
            or "_config_lock" in source
            or "_load_lock" in source
        )
        assert has_lock, (
            "Bug 77: tero2.config module has no threading.Lock for load_config(). "
            "Fix: add a module-level Lock and acquire it inside load_config()."
        )

    def test_load_config_acquires_lock(self) -> None:
        from tero2 import config as cfg_module

        source = inspect.getsource(cfg_module.load_config)
        assert "lock" in source.lower(), (
            "Bug 77: load_config() body contains no reference to a lock. "
            "Fix: acquire a module-level threading.Lock inside load_config()."
        )


# ── Bug 78: startup_wizard _on_plan_picked None not handled ───────────────────


class TestBug78StartupWizardNonePlan:
    """_on_plan_picked(project_path, plan_file=None) is called when the user
    cancels the plan-pick step.  The current code ignores the None and proceeds
    to the config-check / providers-pick step, which then uses (project, None).
    Fix: add `if plan_file is None: self.dismiss(None); return` at the top.
    """

    def test_on_plan_picked_checks_none(self) -> None:
        from tero2.tui.screens.startup_wizard import StartupWizard

        source = inspect.getsource(StartupWizard._on_plan_picked)
        checks_none = "plan_file is None" in source or "if not plan_file" in source
        assert checks_none, (
            "Bug 78: StartupWizard._on_plan_picked never checks if plan_file is None. "
            "User cancel in plan_pick continues to config check with plan=None. "
            "Fix: add `if plan_file is None: self.dismiss(None); return` at method start."
        )

    def test_on_plan_picked_none_calls_dismiss(self, tmp_path: Path) -> None:
        """Functional: passing None must dismiss with None, not continue."""
        from tero2.tui.screens.startup_wizard import StartupWizard

        # Create a fake project with .sora/config.toml so the non-None path
        # would normally call dismiss((project_path, plan_file)) — not None.
        sora_dir = tmp_path / ".sora"
        sora_dir.mkdir(parents=True)
        (sora_dir / "config.toml").write_text("[project]\n")

        wizard = object.__new__(StartupWizard)
        dismissed_with: list[object] = []
        wizard.dismiss = lambda v: dismissed_with.append(v)  # type: ignore[method-assign]

        wizard._on_plan_picked(tmp_path, None)

        assert dismissed_with == [None], (
            f"Bug 78: _on_plan_picked(plan_file=None) called dismiss with "
            f"{dismissed_with!r} instead of [None]. "
            "Fix: add `if plan_file is None: self.dismiss(None); return` at method start."
        )


# ── Bug 79: project_pick _pending_delete persists across navigation ───────────


class TestBug79ProjectPickStalePendingDelete:
    """_pending_delete is only reset on confirmed deletion.  Navigating away
    and back leaves the stale value — a later 'd' on the same index confirms
    without the user intending to.
    Fix: reset _pending_delete in on_list_view_highlighted (or similar).
    """

    def test_pending_delete_reset_on_navigation(self) -> None:
        from tero2.tui.screens.project_pick import ProjectPickScreen

        source = inspect.getsource(ProjectPickScreen)
        lines = source.splitlines()

        # Count all places where _pending_delete is set to None
        reset_lines = [l for l in lines if "_pending_delete = None" in l or "_pending_delete=None" in l]

        # Bug present: only one reset (inside action_delete_entry on confirmation)
        # Fix: at least one additional reset on navigation / highlight change
        assert len(reset_lines) >= 2, (
            "Bug 79: _pending_delete is only reset on confirmed deletion. "
            "Navigating away leaves stale state — later 'd' on same index deletes silently. "
            "Fix: reset _pending_delete in on_list_view_highlighted or on any non-d key."
        )

    def test_stale_pending_delete_does_not_confirm_after_navigation(self) -> None:
        """Functional: after setting _pending_delete, a navigation event must clear it."""
        from tero2.tui.screens.project_pick import ProjectPickScreen
        from tero2.history import HistoryEntry

        screen = object.__new__(ProjectPickScreen)
        screen._pending_delete = 1  # simulates first 'd' on item 1

        # Simulate a highlight/navigation event — the fix hooks this to reset state
        highlight_handler = getattr(screen, "on_list_view_highlighted", None)
        if highlight_handler is not None:
            event = MagicMock()
            event.list_view = MagicMock()
            event.list_view.index = 0
            highlight_handler(event)

        assert screen._pending_delete is None, (
            "Bug 79: _pending_delete was NOT cleared by on_list_view_highlighted. "
            "Stale confirmation state persists across navigation. "
            "Fix: set self._pending_delete = None in the highlight handler."
        )


# ── Bug 80: model_pick rebuilds ListView on every keystroke ───────────────────


class TestBug80ModelPickRebuildOnKeystroke:
    """on_input_changed() rebuilds the entire ListView on each keystroke.
    O(n) rebuild per event causes visible lag for large model lists.
    Fix: debounce the handler using set_timer() or similar.
    """

    def test_model_pick_has_debounce(self) -> None:
        from tero2.tui.screens import model_pick as mp_module

        source = inspect.getsource(mp_module)
        has_debounce = (
            "set_timer" in source
            or "call_later" in source
            or "debounce" in source.lower()
        )
        assert has_debounce, (
            "Bug 80: model_pick.on_input_changed has no debounce. "
            "ListView is rebuilt on every single keystroke. "
            "Fix: use self.set_timer(delay, callback) to debounce the rebuild."
        )


# ── Bug 82: state tmp file persists when os.replace fails ────────────────────


class TestBug82StateTmpFileLeftOnFailure:
    """AgentState.save() writes to a .tmp file then calls os.replace().
    If os.replace raises, the .tmp file is never deleted.
    Fix: wrap os.replace in try/except and unlink tmp on failure.
    """

    def test_tmp_file_cleaned_on_replace_failure(self, tmp_path: Path) -> None:
        from tero2.state import AgentState

        state = AgentState()
        save_path = tmp_path / "STATE.json"

        with patch("tero2.state.os.replace", side_effect=OSError("replace failed")):
            with pytest.raises(OSError):
                state.save(save_path)

        tmp_file = tmp_path / "STATE.tmp"
        assert not tmp_file.exists(), (
            "Bug 82: STATE.tmp left on disk after os.replace raised OSError. "
            "Fix: in the except block call tmp.unlink(missing_ok=True)."
        )

    def test_save_has_tmp_cleanup_on_replace_error(self) -> None:
        """Structural: save() must handle replace failure and clean up tmp."""
        from tero2.state import AgentState

        source = inspect.getsource(AgentState.save)
        # Fix requires try/except around os.replace with unlink in except
        has_unlink = "unlink" in source
        assert has_unlink, (
            "Bug 82: AgentState.save() has no unlink call for tmp cleanup. "
            "Fix: add tmp.unlink(missing_ok=True) in the except/finally block."
        )


# ── Bug 83: disk_layer.write_file propagates OSError ─────────────────────────


class TestBug83DiskLayerWriteFileOSError:
    """write_file() calls path.write_text() without a try/except.
    An OSError (permissions, disk full) crashes the runner loop.
    Fix: wrap in try/except OSError, return bool for success/failure.
    """

    def test_write_file_does_not_raise_on_oserror(self, tmp_path: Path) -> None:
        from tero2.disk_layer import DiskLayer

        disk = DiskLayer(tmp_path)
        disk.init()

        # Make the target directory read-only to force a write failure
        target_dir = tmp_path / ".sora" / "reports"
        target_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(target_dir, 0o444)  # read-only

        try:
            result = disk.write_file("reports/test.txt", "content")
            # If no exception: fix is applied — result should be falsy on failure
        except OSError:
            pytest.fail(
                "Bug 83: disk_layer.write_file raised OSError on permission denied. "
                "Fix: wrap path.write_text in try/except OSError and return False."
            )
        finally:
            os.chmod(target_dir, 0o755)

    def test_write_file_returns_bool(self) -> None:
        """Structural: write_file must return bool (True=success, False=failure)."""
        from tero2.disk_layer import DiskLayer

        source = inspect.getsource(DiskLayer.write_file)
        has_return_bool = "return False" in source or "return True" in source
        assert has_return_bool, (
            "Bug 83: write_file has no `return False` on error path. "
            "Fix: catch OSError, log it, and `return False`."
        )
