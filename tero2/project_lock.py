"""Project-level lock with error translation.

Wraps FileLock so callers receive typed tero2 errors instead of raw OSError.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

from tero2.errors import LockHeldError, Tero2Error
from tero2.lock import FileLock


class ProjectLockError(Tero2Error):
    """Project lock is held by another instance."""

    def __init__(self, pid: int, lock_path: str) -> None:
        self.pid = pid
        self.lock_path = lock_path
        super().__init__(f"Project locked by PID {pid}: {lock_path}")


class ProjectLock:
    def __init__(self, lock_path: Path) -> None:
        self._inner = FileLock(lock_path)

    def acquire(self) -> None:
        try:
            self._inner.acquire()
        except LockHeldError:
            raise
        except OSError as exc:
            raise Tero2Error(f"lock acquire failed: {exc}") from exc

    def release(self) -> None:
        try:
            self._inner.release()
        except OSError as exc:
            raise Tero2Error(f"lock release failed: {exc}") from exc


@contextlib.contextmanager
def project_lock(sora_dir: Path):
    """Acquire project lock, translated to ProjectLockError.

    Args:
        sora_dir: Path to .sora directory (lock file at sora_dir/runtime/auto.lock)

    Yields:
        None

    Raises:
        ProjectLockError: If lock is held by another process
        Tero2Error: If lock acquire/release fails
    """
    lock_path = sora_dir / "runtime" / "auto.lock"
    lock = ProjectLock(lock_path)
    try:
        lock.acquire()
    except LockHeldError as exc:
        raise ProjectLockError(exc.pid, exc.lock_path) from exc
    try:
        yield
    finally:
        try:
            lock.release()
        except Exception:
            import logging
            logging.getLogger(__name__).warning("lock release failed", exc_info=True)

