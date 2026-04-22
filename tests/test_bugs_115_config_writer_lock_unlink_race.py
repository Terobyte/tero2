"""Bug 115: ``write_global_config_section`` unlinks its own lock file in
``finally``, which creates a two-processes-hold-the-lock race.

The flow is::

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    # ...do work...
    fcntl.flock(lock_fd, fcntl.LOCK_UN)
    os.close(lock_fd)
    lock_path.unlink(missing_ok=True)   # ← the bug

Unlink removes the *dirent*, not the inode. A second process that opened
``lock_path`` before the unlink holds an fd on the original inode and can
acquire ``flock`` on it after we release. A third process that opens
``lock_path`` *after* the unlink creates a brand-new inode via ``O_CREAT``
and acquires ``flock`` on **that** inode — which is completely independent
of the one the second process is holding. Result: two processes both
believe they exclusively hold the same config-writer lock.

This is textbook "do not unlink a lock file you rely on". The canonical
fix is simply **don't remove the lock file** — a tiny empty file at a
known path is cheap and avoids the race entirely. An acceptable alternate
is to rely on ``O_CREAT`` always finding the same inode, which requires
leaving the dirent in place.

Test strategy: the contract we assert is "after ``write_global_config_section``
returns, the lock file still exists". The current code violates this; the
fix makes it hold. This is a structural halal test — it does not need to
reproduce the race itself (hard to do deterministically with flock) but it
pins the behaviour that makes the race impossible.

Per feedback_tdd_order.md: this test is written before the fix. It is
expected to FAIL against the current (broken) unlink-in-finally code.
"""

from __future__ import annotations

import inspect
import os
import threading
import time
from pathlib import Path

import pytest

from tero2.config_writer import write_global_config_section


class TestLockFilePersistsAfterWrite:
    """The lock file must not be removed in the happy-path finally."""

    def test_lock_file_still_exists_after_write(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"
        lock_path = config_path.with_suffix(".lock")

        write_global_config_section(
            config_path, "general", {"projects_dir": "~/somewhere"}
        )

        assert config_path.exists(), "config file must be written"
        assert lock_path.exists(), (
            "lock file must persist after write — unlinking it allows a "
            "second writer to create a new-inode lock while a first writer "
            "still holds the old inode"
        )

    def test_lock_file_stable_across_many_writes(self, tmp_path: Path) -> None:
        """Many sequential writes must keep using the same lock-file inode.
        If the function unlinks and re-creates each call, the inode changes."""
        config_path = tmp_path / "config.toml"
        lock_path = config_path.with_suffix(".lock")

        write_global_config_section(config_path, "general", {"log_level": "INFO"})
        first_inode = lock_path.stat().st_ino

        for i in range(5):
            write_global_config_section(
                config_path,
                "general",
                {"log_level": "INFO", "iteration": i},
            )
            assert lock_path.exists()
            assert lock_path.stat().st_ino == first_inode, (
                "lock file inode changed between writes — the function is "
                "creating a new inode each time, which breaks flock semantics"
            )


class TestNoUnlinkInFinally:
    """Structural guard: the source of ``write_global_config_section`` must
    not contain a ``lock_path.unlink`` call. This catches regressions that
    re-introduce the buggy cleanup.

    This is belt-and-suspenders alongside the behavioural tests above — if
    someone wraps the unlink in a conditional that happens to skip in tests,
    the behavioural tests might pass but the race would still be there."""

    def test_function_source_does_not_unlink_lock(self) -> None:
        source = inspect.getsource(write_global_config_section)
        # We want: no `lock_path.unlink` anywhere in the body.
        assert "lock_path.unlink" not in source, (
            "write_global_config_section must not unlink its lock file — "
            "doing so breaks flock exclusivity across concurrent writers. "
            "See bug 115 test for the race details."
        )


class TestConcurrentWritesPreservesContent:
    """Stronger behavioural guard: two threads writing concurrently must end
    with a valid TOML file, not one whose content was clobbered mid-write.

    This does not directly prove the race is gone (threads share the same
    flock semantics so the Python-level test is best-effort) but it pins the
    end-state: content is a valid union, not a torn write."""

    def test_two_threads_both_writes_land(self, tmp_path: Path) -> None:
        config_path = tmp_path / "config.toml"

        errors: list[BaseException] = []

        def writer(section: str, key: str, val: str) -> None:
            try:
                for _ in range(20):
                    write_global_config_section(
                        config_path, section, {key: val}
                    )
                    time.sleep(0.001)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t1 = threading.Thread(target=writer, args=("alpha", "color", "red"))
        t2 = threading.Thread(target=writer, args=("beta", "size", "large"))
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        assert not errors, f"concurrent writer raised: {errors[0]!r}"

        # Parse the final file — must be valid TOML.
        import tomllib
        parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
        # Both sections should be present.
        assert "alpha" in parsed and "beta" in parsed, (
            f"concurrent write lost a section: {parsed!r}"
        )
        assert parsed["alpha"]["color"] == "red"
        assert parsed["beta"]["size"] == "large"
