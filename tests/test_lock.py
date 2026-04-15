import errno
import fcntl
import multiprocessing
import os
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tero2.errors import LockHeldError
from tero2.lock import FileLock


def _write_pid(path: Path, pid: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{pid}\n")


def _child_hold_lock(lock_path_str: str, ready: multiprocessing.Event) -> None:
    lock_path = Path(lock_path_str)
    fl = FileLock(lock_path)
    fl.acquire()
    ready.set()
    time.sleep(30)
    fl.release()


class TestAcquireStaleLockRecovery:
    def test_alive_pid_raises_lock_held_error(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        alive_pid = 9999999
        _write_pid(lock_path, alive_pid)
        fl = FileLock(lock_path)
        with (
            patch("tero2.lock.os.open", return_value=3),
            patch(
                "tero2.lock.fcntl.flock",
                side_effect=OSError(errno.EAGAIN, "Resource temporarily unavailable"),
            ),
            patch("tero2.lock.os.close"),
            patch("tero2.lock.os.getpid", return_value=1),
            patch.object(fl, "_pid_alive", return_value=True),
            pytest.raises(LockHeldError) as exc_info,
        ):
            fl.acquire()
        assert exc_info.value.pid == alive_pid

    def test_dead_pid_retries_without_unlink(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        dead_pid = 8888888
        _write_pid(lock_path, dead_pid)
        fl = FileLock(lock_path)
        call_count = 0

        def flock_side_effect(fd, op):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError(errno.EACCES, "Permission denied")

        with (
            patch("tero2.lock.os.open", return_value=3),
            patch("tero2.lock.fcntl.flock", side_effect=flock_side_effect),
            patch("tero2.lock.os.close"),
            patch("tero2.lock.os.ftruncate"),
            patch("tero2.lock.os.lseek"),
            patch("tero2.lock.os.write"),
            patch("tero2.lock.os.getpid", return_value=1),
            patch.object(fl, "_pid_alive", return_value=False),
        ):
            fl.acquire()

        assert call_count == 2

    def test_retry_once_raises_on_second_failure(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        dead_pid = 7777777
        _write_pid(lock_path, dead_pid)
        fl = FileLock(lock_path)
        call_count = 0

        def flock_side_effect(fd, op):
            nonlocal call_count
            call_count += 1
            raise OSError(errno.EAGAIN, "Resource temporarily unavailable")

        with (
            patch("tero2.lock.os.open", return_value=3),
            patch("tero2.lock.fcntl.flock", side_effect=flock_side_effect),
            patch("tero2.lock.os.close"),
            patch("tero2.lock.os.getpid", return_value=1),
            patch.object(fl, "_pid_alive", return_value=False),
            pytest.raises(LockHeldError),
        ):
            fl.acquire()

        assert call_count == 2

    def test_no_pid_in_file_removes_and_retries(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "test.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text("not_a_number\n")
        fl = FileLock(lock_path)
        call_count = 0

        def flock_side_effect(fd, op):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError(errno.EAGAIN, "try again")

        with (
            patch("tero2.lock.os.open", return_value=3),
            patch("tero2.lock.fcntl.flock", side_effect=flock_side_effect),
            patch("tero2.lock.os.close"),
            patch("tero2.lock.os.ftruncate"),
            patch("tero2.lock.os.lseek"),
            patch("tero2.lock.os.write"),
            patch("tero2.lock.os.getpid", return_value=1),
        ):
            fl.acquire()

        assert call_count == 2


class TestPidAliveEperm:
    def test_eperm_treated_as_alive(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "eperm.lock"
        pid = 9999999
        _write_pid(lock_path, pid)
        fl = FileLock(lock_path)
        with (
            patch("tero2.lock.os.open", return_value=3),
            patch(
                "tero2.lock.fcntl.flock",
                side_effect=OSError(errno.EAGAIN, "Resource temporarily unavailable"),
            ),
            patch("tero2.lock.os.close"),
            patch("tero2.lock.os.getpid", return_value=1),
            patch(
                "tero2.lock.os.kill", side_effect=OSError(errno.EPERM, "Operation not permitted")
            ),
            pytest.raises(LockHeldError) as exc_info,
        ):
            fl.acquire()
        assert exc_info.value.pid == pid
        assert lock_path.exists()

    def test_esrch_treated_as_dead(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "esrch.lock"
        pid = 8888888
        _write_pid(lock_path, pid)
        fl = FileLock(lock_path)
        call_count = 0

        def flock_side_effect(fd, op):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError(errno.EAGAIN, "try again")

        with (
            patch("tero2.lock.os.open", return_value=3),
            patch("tero2.lock.fcntl.flock", side_effect=flock_side_effect),
            patch("tero2.lock.os.close"),
            patch("tero2.lock.os.ftruncate"),
            patch("tero2.lock.os.lseek"),
            patch("tero2.lock.os.write"),
            patch("tero2.lock.os.getpid", return_value=1),
            patch("tero2.lock.os.kill", side_effect=OSError(errno.ESRCH, "No such process")),
        ):
            fl.acquire()
        assert call_count == 2


class TestFlockPidWriteRace:
    """Regression: competing acquire must not unlink while flock is held by
    a process that has not yet written its PID to the file."""

    def test_no_unlink_when_flock_held_pid_not_written(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "race.lock"
        stale_pid = 8888888
        _write_pid(lock_path, stale_pid)
        fl = FileLock(lock_path)
        flock_calls = 0

        def flock_side_effect(fd, op):
            nonlocal flock_calls
            flock_calls += 1
            raise OSError(errno.EAGAIN, "try again")

        with (
            patch("tero2.lock.os.open", return_value=3),
            patch("tero2.lock.fcntl.flock", side_effect=flock_side_effect),
            patch("tero2.lock.os.close"),
            patch("tero2.lock.os.getpid", return_value=1),
            patch.object(fl, "_pid_alive", return_value=False),
            pytest.raises(LockHeldError),
        ):
            fl.acquire()

        assert flock_calls == 2
        assert lock_path.exists()


class TestTruncationRegression:
    def test_second_process_gets_lock_held_over_stale_long_pid(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "regression.lock"
        _write_pid(lock_path, int("9" * 20))

        ready = multiprocessing.Event()
        child = multiprocessing.Process(
            target=_child_hold_lock,
            args=(str(lock_path), ready),
        )
        child.start()
        try:
            assert ready.wait(timeout=5), "child did not acquire lock in time"
            fl = FileLock(lock_path)
            with pytest.raises(LockHeldError):
                fl.acquire()
        finally:
            child.terminate()
            child.join(timeout=5)

    def test_pid_file_clean_after_acquire_over_stale(self, tmp_path: Path) -> None:
        lock_path = tmp_path / "clean.lock"
        _write_pid(lock_path, int("9" * (len(str(os.getpid())) + 10)))

        fl = FileLock(lock_path)
        fl.acquire()
        try:
            content = lock_path.read_text().strip()
            assert "\n" not in content
            assert content == str(os.getpid())
        finally:
            fl.release()
