"""OS-level file lock for single-writer guarantee."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path

from tero2.errors import LockHeldError


class FileLock:
    def __init__(self, lock_path: Path) -> None:
        self.lock_path = lock_path
        self._fd: int | None = None

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                pid = self._read_pid()
                raise LockHeldError(pid, str(self.lock_path)) from exc
            raise
        pid_bytes = f"{os.getpid()}\n".encode()
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, pid_bytes)
            os.truncate(self.lock_path, len(pid_bytes))
        except:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None

    def is_held(self) -> tuple[bool, int]:
        pid = self._read_pid()
        if pid and self._pid_alive(pid):
            return True, pid
        return False, 0

    def _read_pid(self) -> int:
        try:
            return int(self.lock_path.read_text().strip())
        except (OSError, ValueError):
            return 0

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError as exc:
            if exc.errno == errno.ESRCH:
                return False
            if exc.errno == errno.EPERM:
                return True
            return False

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
