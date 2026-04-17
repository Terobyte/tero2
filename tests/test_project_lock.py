"""Tests for project_lock context manager and ProjectLockError."""

from __future__ import annotations

import multiprocessing
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tero2.errors import LockHeldError, Tero2Error
from tero2.project_lock import ProjectLock, ProjectLockError, project_lock


def _write_pid(path: Path, pid: int) -> None:
    """Write a PID to a lock file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n")


def _child_hold_lock(sora_dir_str: str, ready: multiprocessing.Event) -> None:
    """Child process holds project lock for testing concurrent conflicts."""
    sora_dir = Path(sora_dir_str)
    with project_lock(sora_dir):
        ready.set()
        time.sleep(30)


def _child_brief(sora_dir_str: str) -> None:
    """Child process briefly acquires and releases lock."""
    sora_dir = Path(sora_dir_str)
    try:
        with project_lock(sora_dir):
            pass
    except Exception:
        pass


class TestProjectLockError:
    """Tests for ProjectLockError exception class."""

    def test_has_pid_and_lock_path(self) -> None:
        """ProjectLockError stores PID and lock path."""
        exc = ProjectLockError(1234, "/tmp/test.lock")
        assert exc.pid == 1234
        assert exc.lock_path == "/tmp/test.lock"

    def test_message_includes_pid_and_path(self) -> None:
        """ProjectLockError message is human-readable."""
        exc = ProjectLockError(5678, "/home/user/.sora/runtime/auto.lock")
        msg = str(exc)
        assert "5678" in msg
        assert "/home/user/.sora/runtime/auto.lock" in msg
        assert "locked" in msg.lower()

    def test_is_tero2_error(self) -> None:
        """ProjectLockError inherits from Tero2Error."""
        exc = ProjectLockError(999, "/test.lock")
        assert isinstance(exc, Tero2Error)


class TestProjectLockContextManager:
    """Tests for project_lock context manager."""

    def test_acquires_lock_on_entry(self, tmp_path: Path) -> None:
        """project_lock acquires lock when entering context."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        with project_lock(sora_dir):
            assert lock_path.exists()
            assert lock_path.parent.exists()

    def test_releases_lock_on_exit(self, tmp_path: Path) -> None:
        """project_lock releases lock when exiting context."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        with project_lock(sora_dir):
            assert lock_path.exists()

        # After exit, should be able to reacquire
        with project_lock(sora_dir):
            assert lock_path.exists()

    def test_releases_lock_on_exception(self, tmp_path: Path) -> None:
        """project_lock releases lock even if exception raised."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        try:
            with project_lock(sora_dir):
                assert lock_path.exists()
                raise ValueError("test error")
        except ValueError:
            pass

        # Lock should be released, reacquirable
        with project_lock(sora_dir):
            assert lock_path.exists()

    def test_raises_project_lock_error_when_held(self, tmp_path: Path) -> None:
        """project_lock raises ProjectLockError if another process holds it."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        # Actually acquire lock in first context
        with project_lock(sora_dir):
            # Now try to acquire again - should fail
            with pytest.raises(ProjectLockError) as exc_info:
                with project_lock(sora_dir):
                    pass
            # Should contain some PID info
            assert exc_info.value.pid > 0
            assert str(lock_path) in exc_info.value.lock_path

    def test_translates_lock_held_error_to_project_lock_error(
        self, tmp_path: Path
    ) -> None:
        """project_lock translates LockHeldError to ProjectLockError."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        # Get the first lock acquired
        with project_lock(sora_dir):
            # Now try to get another - FileLock will raise LockHeldError
            # which project_lock catches and translates to ProjectLockError
            with pytest.raises(ProjectLockError):
                with project_lock(sora_dir):
                    pass

    def test_derives_lock_path_correctly(self, tmp_path: Path) -> None:
        """project_lock places lock at sora_dir/runtime/auto.lock."""
        sora_dir = tmp_path / ".sora"
        expected_lock = sora_dir / "runtime" / "auto.lock"

        with project_lock(sora_dir):
            assert expected_lock.exists()

    def test_creates_runtime_directory(self, tmp_path: Path) -> None:
        """project_lock creates runtime directory if missing."""
        sora_dir = tmp_path / ".sora"
        runtime_dir = sora_dir / "runtime"

        assert not runtime_dir.exists()

        with project_lock(sora_dir):
            assert runtime_dir.exists()

    def test_nested_reacquire_fails_with_project_lock_error(self, tmp_path: Path) -> None:
        """Nested project_lock call fails (can't reacquire same lock)."""
        sora_dir = tmp_path / ".sora"

        with project_lock(sora_dir):
            with pytest.raises(ProjectLockError):
                with project_lock(sora_dir):
                    pass


class TestProjectLockConcurrentConflict:
    """Tests for concurrent lock conflicts."""

    def test_concurrent_processes_conflict_with_project_lock_error(
        self, tmp_path: Path
    ) -> None:
        """Two processes can't both hold project_lock (second gets ProjectLockError)."""
        sora_dir = tmp_path / ".sora"

        ready = multiprocessing.Event()
        child = multiprocessing.Process(
            target=_child_hold_lock,
            args=(str(sora_dir), ready),
        )
        child.start()
        try:
            assert ready.wait(timeout=5), "child did not acquire lock in time"
            # Now child holds lock, second process should fail
            with pytest.raises(ProjectLockError):
                with project_lock(sora_dir):
                    pass
        finally:
            child.terminate()
            child.join(timeout=5)

    def test_can_acquire_after_child_releases(self, tmp_path: Path) -> None:
        """Lock can be acquired after child process releases it."""
        sora_dir = tmp_path / ".sora"

        child = multiprocessing.Process(
            target=_child_brief,
            args=(str(sora_dir),),
        )
        child.start()
        child.join(timeout=5)

        # Now should be able to acquire
        lock_path = sora_dir / "runtime" / "auto.lock"
        with project_lock(sora_dir):
            assert lock_path.exists()


class TestProjectLockOsErrors:
    """Tests for OS-level errors in project_lock."""

    def test_oserror_during_acquire_raises_tero2_error(self, tmp_path: Path) -> None:
        """OSError during acquire (not LockHeldError) raises Tero2Error."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        with patch("tero2.lock.os.open", side_effect=OSError("permission denied")):
            with pytest.raises(Tero2Error) as exc_info:
                with project_lock(sora_dir):
                    pass
            assert "lock acquire failed" in str(exc_info.value).lower()

    def test_release_works_normally(self, tmp_path: Path) -> None:
        """Release works normally in project_lock."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        with project_lock(sora_dir):
            assert lock_path.exists()

        # After exiting, should be able to reacquire
        with project_lock(sora_dir):
            assert lock_path.exists()


class TestProjectLockPreservesProjectLock:
    """Tests that ProjectLock class is preserved and working."""

    def test_project_lock_class_still_exists(self, tmp_path: Path) -> None:
        """ProjectLock class is still available for direct use."""
        lock_path = tmp_path / "test.lock"
        lock = ProjectLock(lock_path)
        lock.acquire()
        try:
            assert lock_path.exists()
        finally:
            lock.release()

    def test_project_lock_acquire_raises_lock_held_error(self, tmp_path: Path) -> None:
        """ProjectLock.acquire() still raises LockHeldError (not ProjectLockError)."""
        sora_dir = tmp_path / ".sora"
        lock_path = sora_dir / "runtime" / "auto.lock"

        # Acquire lock first
        lock1 = ProjectLock(lock_path)
        lock1.acquire()

        try:
            # Now try to acquire same lock with different instance
            lock2 = ProjectLock(lock_path)
            with pytest.raises(LockHeldError):
                lock2.acquire()
        finally:
            lock1.release()
