"""Runner lifecycle integration tests.

Covers:
  Step 1 — Lock prevents two tero2 run instances on the same project
  Step 2 — Stale lock (dead PID) is automatically cleaned up
  Step 3 — OVERRIDE.md with PAUSE → runner pauses → Telegram notification
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from tero2.config import Config, RoleConfig, TelegramConfig
from tero2.constants import EXIT_LOCK_HELD
from tero2.disk_layer import DiskLayer
from tero2.lock import FileLock
from tero2.notifier import NotifyLevel
from tero2.runner import Runner
from tero2.state import Phase


# ── helpers ──────────────────────────────────────────────────────────


def _make_project(tmp_path: Path) -> tuple[Path, Path, Config, DiskLayer]:
    project = tmp_path / "project"
    project.mkdir()
    disk = DiskLayer(project)
    disk.init()
    plan = project / "plan.md"
    plan.write_text("# Plan\n1. do something")
    config = Config()
    config.roles["executor"] = RoleConfig(provider="fake", timeout_s=5)
    config.telegram = TelegramConfig(bot_token="tok", chat_id="chat")
    return project, plan, config, disk


class _ImmediateChain:
    """Minimal chain: yields one tool_result then finishes (success)."""

    current_provider_index = 0

    async def run_prompt(self, prompt: str):
        yield {"type": "tool_result", "content": "ok"}


async def _fake_notify_noop(text: str, level=None) -> bool:
    return True


# ── Step 1: Lock prevents two instances ─────────────────────────────


class TestLockExclusion:
    """Second Runner on the same project must be rejected while first holds the lock."""

    async def test_second_runner_exits_with_lock_held_code(self, tmp_path: Path) -> None:
        """Hold lock externally → Runner.run() raises SystemExit(EXIT_LOCK_HELD)."""
        project, plan, config, disk = _make_project(tmp_path)

        # Simulate runner1 holding the OS lock
        lock1 = FileLock(disk.lock_path)
        lock1.acquire()
        try:
            runner2 = Runner(project, plan, config=config)
            with pytest.raises(SystemExit) as exc_info:
                await runner2.run()
            assert exc_info.value.code == EXIT_LOCK_HELD, (
                f"expected exit code {EXIT_LOCK_HELD}, got {exc_info.value.code}"
            )
        finally:
            lock1.release()

    async def test_lock_file_not_deleted_by_blocked_runner(self, tmp_path: Path) -> None:
        """Runner that can't acquire the lock must not delete the existing lock file."""
        project, plan, config, disk = _make_project(tmp_path)

        lock1 = FileLock(disk.lock_path)
        lock1.acquire()

        runner2 = Runner(project, plan, config=config)
        with pytest.raises(SystemExit):
            await runner2.run()

        # The lock file must still be there — runner2 must not have unlinked it.
        assert disk.lock_path.exists(), (
            "runner2 deleted the lock file that was held by runner1"
        )
        lock1.release()

    async def test_first_runner_completes_second_can_then_start(self, tmp_path: Path) -> None:
        """After runner1 finishes (releases lock), runner2 can acquire and run."""
        project, plan, config, disk = _make_project(tmp_path)

        fast = _ImmediateChain()
        runner1 = Runner(project, plan, config=config)
        runner1.notifier.notify = _fake_notify_noop  # type: ignore[method-assign]

        with patch.object(runner1, "_build_chain", return_value=fast):
            await runner1.run()

        assert disk.read_state().phase == Phase.COMPLETED

        # Now runner2 must succeed — no lock held
        runner2 = Runner(project, plan, config=config)
        runner2.notifier.notify = _fake_notify_noop  # type: ignore[method-assign]

        fast2 = _ImmediateChain()
        with patch.object(runner2, "_build_chain", return_value=fast2):
            await runner2.run()

        assert disk.read_state().phase == Phase.COMPLETED


# ── Step 2: Stale lock (dead PID) cleaned up ─────────────────────────


class TestStaleLockCleanup:
    """Lock file left behind with a dead PID must not prevent a new run."""

    async def test_runner_starts_when_lock_file_has_dead_pid(self, tmp_path: Path) -> None:
        """Stale lock file (dead PID, no flock held) → Runner acquires and completes."""
        project, plan, config, disk = _make_project(tmp_path)

        # Write a PID that definitely doesn't exist
        disk.lock_path.write_text("999999999\n")

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify_noop  # type: ignore[method-assign]

        with patch.object(runner, "_build_chain", return_value=_ImmediateChain()):
            await runner.run()

        assert disk.read_state().phase == Phase.COMPLETED

    async def test_stale_lock_overwritten_with_current_pid(self, tmp_path: Path) -> None:
        """After acquiring over a stale file, the lock file contains the running PID."""
        import os

        project, plan, config, disk = _make_project(tmp_path)
        disk.lock_path.write_text("999999999\n")

        pid_during_run: list[int] = []

        async def spy_execute(state, shutdown_event=None) -> None:
            try:
                pid_during_run.append(int(disk.lock_path.read_text().strip()))
            except (OSError, ValueError):
                pass

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify_noop  # type: ignore[method-assign]

        with patch.object(runner, "_execute_plan", spy_execute):
            await runner.run()

        assert pid_during_run, "execute_plan spy was never called"
        assert pid_during_run[0] == os.getpid(), (
            f"Lock file should contain own PID {os.getpid()}, got {pid_during_run[0]}"
        )

    async def test_lock_released_on_normal_completion(self, tmp_path: Path) -> None:
        """After a successful run the lock file is removed (not left behind)."""
        project, plan, config, disk = _make_project(tmp_path)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify_noop  # type: ignore[method-assign]

        with patch.object(runner, "_build_chain", return_value=_ImmediateChain()):
            await runner.run()

        assert not disk.lock_path.exists(), (
            "Lock file was not cleaned up after successful run"
        )


# ── Step 3: OVERRIDE.md PAUSE → pause + Telegram notification ────────


class TestOverridePause:
    """PAUSE directive in OVERRIDE.md must pause the runner and notify via Telegram."""

    async def test_pause_override_sends_notification(self, tmp_path: Path) -> None:
        """OVERRIDE.md containing PAUSE → notifier receives a 'paused' message."""
        project, plan, config, disk = _make_project(tmp_path)

        override_path = disk.sora_dir / "human" / "OVERRIDE.md"
        override_path.write_text("PAUSE\n")

        notified: list[tuple[str, object]] = []

        async def capture_notify(text: str, level=None) -> bool:
            notified.append((text, level))
            return True

        sleep_calls: list[float] = []

        async def fake_sleep(secs: float) -> None:
            sleep_calls.append(secs)
            if len(sleep_calls) == 1:
                # Clear PAUSE so runner exits the polling loop
                override_path.unlink(missing_ok=True)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = capture_notify  # type: ignore[method-assign]

        with (
            patch("tero2.runner.asyncio.sleep", fake_sleep),
            patch.object(runner, "_build_chain", return_value=_ImmediateChain()),
        ):
            await runner.run()

        pause_msgs = [t for t, _ in notified if "pause" in t.lower()]
        assert pause_msgs, (
            f"Expected at least one 'paused' notification. Got: {notified}"
        )

    async def test_pause_notification_has_stuck_level(self, tmp_path: Path) -> None:
        """The pause notification must use NotifyLevel.STUCK (for voice/priority)."""
        project, plan, config, disk = _make_project(tmp_path)

        override_path = disk.sora_dir / "human" / "OVERRIDE.md"
        override_path.write_text("PAUSE\n")

        notified: list[tuple[str, object]] = []

        async def capture_notify(text: str, level=None) -> bool:
            notified.append((text, level))
            return True

        async def fake_sleep(secs: float) -> None:
            override_path.unlink(missing_ok=True)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = capture_notify  # type: ignore[method-assign]

        with (
            patch("tero2.runner.asyncio.sleep", fake_sleep),
            patch.object(runner, "_build_chain", return_value=_ImmediateChain()),
        ):
            await runner.run()

        stuck_msgs = [t for t, lvl in notified if lvl == NotifyLevel.STUCK]
        assert stuck_msgs, (
            f"Expected pause notification with NotifyLevel.STUCK. Got: {notified}"
        )

    async def test_runner_resumes_and_completes_after_pause_cleared(
        self, tmp_path: Path
    ) -> None:
        """After PAUSE is removed from OVERRIDE.md, runner continues to COMPLETED."""
        project, plan, config, disk = _make_project(tmp_path)

        override_path = disk.sora_dir / "human" / "OVERRIDE.md"
        override_path.write_text("PAUSE\n")

        async def fake_sleep(secs: float) -> None:
            # Remove PAUSE on any sleep call → runner exits polling loop
            override_path.unlink(missing_ok=True)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = _fake_notify_noop  # type: ignore[method-assign]

        with (
            patch("tero2.runner.asyncio.sleep", fake_sleep),
            patch.object(runner, "_build_chain", return_value=_ImmediateChain()),
        ):
            await runner.run()

        final = disk.read_state()
        assert final.phase == Phase.COMPLETED, (
            f"Expected COMPLETED after PAUSE cleared, got {final.phase}"
        )

    async def test_pause_state_written_to_disk(self, tmp_path: Path) -> None:
        """During PAUSE, the PAUSED phase must be persisted to STATE.json."""
        project, plan, config, disk = _make_project(tmp_path)

        override_path = disk.sora_dir / "human" / "OVERRIDE.md"
        override_path.write_text("PAUSE\n")

        paused_phases: list[Phase] = []

        async def capture_notify(text: str, level=None) -> bool:
            if "pause" in text.lower():
                # Check what's on disk at the moment the pause notification is sent
                paused_phases.append(disk.read_state().phase)
            return True

        async def fake_sleep(secs: float) -> None:
            override_path.unlink(missing_ok=True)

        runner = Runner(project, plan, config=config)
        runner.notifier.notify = capture_notify  # type: ignore[method-assign]

        with (
            patch("tero2.runner.asyncio.sleep", fake_sleep),
            patch.object(runner, "_build_chain", return_value=_ImmediateChain()),
        ):
            await runner.run()

        assert paused_phases, "No pause notification was observed"
        assert paused_phases[0] == Phase.PAUSED, (
            f"Expected PAUSED on disk during pause, got {paused_phases[0]}"
        )
