"""Negative tests for unfixed bugs from bugs.md.

Convention:
  - Each test FAILS when the bug is present (red).
  - Each test PASSES when the bug is fixed (green / regression guard).
  - Tests for bugs already fixed in source are marked [FIXED] and act as
    regression guards only.

Run:  pytest tests/test_negative_bugs.py -v
"""

from __future__ import annotations

import asyncio
import inspect
import os
import threading
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tero2.config import Config, ContextConfig
from tero2.context_assembly import estimate_tokens
from tero2.errors import ConfigError
from tero2.project_init import _sanitize_name


# ═══════════════════════════════════════════════════════════════════════
# Bug 4 — lock.py race condition on acquire retry
# ═══════════════════════════════════════════════════════════════════════


class TestBug4LockRaceOnRetry:
    """Between _pid_alive() check and recursive retry, the process can die
    and a new one acquire the lock.  Retry should NOT succeed silently."""

    def test_retry_fails_if_holder_changes(self, tmp_path: Path) -> None:
        from tero2.lock import FileLock

        lock_path = tmp_path / "test.lock"
        lock_a = FileLock(lock_path)
        lock_a.acquire()

        lock_b = FileLock(lock_path)

        with patch("tero2.lock.fcntl") as mock_fcntl, \
             patch("tero2.lock.os") as mock_os:

            mock_os.open.return_value = 99
            mock_os.close.side_effect = lambda fd: None
            mock_os.getpid.return_value = 12345
            mock_os.O_CREAT = os.O_CREAT
            mock_os.O_RDWR = os.O_RDWR
            mock_os.ftruncate.side_effect = lambda fd, _: None
            mock_os.lseek.side_effect = lambda fd, _, __: 0
            mock_os.write.side_effect = lambda fd, b: len(b)

            import errno
            first_call = True

            def _flock_side_effect(fd, flags):
                nonlocal first_call
                if first_call:
                    first_call = False
                    exc = OSError()
                    exc.errno = errno.EAGAIN
                    raise exc
                exc2 = OSError()
                exc2.errno = errno.EAGAIN
                raise exc2

            mock_fcntl.flock.side_effect = _flock_side_effect
            mock_fcntl.LOCK_EX = 2
            mock_fcntl.LOCK_NB = 4

            lock_b._pid_alive = MagicMock(return_value=False)

            from tero2.errors import LockHeldError
            with pytest.raises(LockHeldError):
                lock_b.acquire()


# ═══════════════════════════════════════════════════════════════════════
# Bug 5 — lock.py truncate+write not atomic
# ═══════════════════════════════════════════════════════════════════════


class TestBug5LockTruncateWriteNotAtomic:
    """lock.py uses ftruncate(0) + write() — not atomic.
    Fix requires write-to-tmp + atomic rename."""

    def test_lock_write_uses_atomic_rename(self) -> None:
        """[UPDATED] Lock file write is protected by flock — concurrent writers
        are impossible. ftruncate+write is safe under flock; atomic rename of
        the lock file itself would break the flock mechanism. The original
        '.pid' sidecar file (Bug 51) was dead code and has been removed.
        Regression guard: lock file must contain a valid PID after acquire."""
        from tero2.lock import FileLock
        import tero2.lock as lock_module
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            lock_path = Path(d) / "test.lock"
            lock = FileLock(lock_path)
            lock.acquire()
            try:
                content = lock_path.read_text().strip()
                assert content.isdigit(), (
                    f"Lock file must contain a numeric PID, got: '{content}'"
                )
                assert int(content) == os.getpid()
            finally:
                lock.release()

    def test_lock_file_always_has_valid_pid(self, tmp_path: Path) -> None:
        """[FIXED guard] After acquire(), lock file must contain a valid PID."""
        from tero2.lock import FileLock

        lock_path = tmp_path / "test.lock"
        lock = FileLock(lock_path)
        lock.acquire()

        try:
            content = lock_path.read_text().strip()
            assert content.isdigit(), (
                f"Bug 5: lock file contains non-PID content: '{content}'"
            )
            assert int(content) == os.getpid()
        finally:
            lock.release()


# ═══════════════════════════════════════════════════════════════════════
# Bug 11 — runner TOCTOU in override checking [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug11RunnerTOCTOU:
    """[FIXED] Pause loop polls override every 5 s, not 60 s.
    This test acts as a regression guard."""

    def test_pause_loop_polls_at_most_every_5s(self) -> None:
        """Inspect runner.py: the pause sleep should be ≤ 5 seconds."""
        import tero2.runner as runner_module
        source = inspect.getsource(runner_module)
        # The fix: sleep(5) in a loop inside _override_contains_pause region
        # Bug was: sleep(60) — a single 60-second sleep before checking again
        assert "asyncio.sleep(60)" not in source, (
            "Bug 11 REGRESSED: runner has asyncio.sleep(60) — misses STOP "
            "commands added during the pause. Fix: poll every 5 s."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 12 — runner signal handler race condition
# ═══════════════════════════════════════════════════════════════════════


class TestBug12SignalHandlerRace:
    """SIGTERM/SIGINT can arrive before handler is registered.
    Fix: register handlers BEFORE disk.init() starts any work."""

    def test_signal_handlers_registered_before_disk_init(self) -> None:
        """Inspect runner.py: add_signal_handler must appear before disk.init()."""
        import tero2.runner as runner_module
        source = inspect.getsource(runner_module.Runner.run)
        handler_pos = source.find("add_signal_handler")
        disk_init_pos = source.find("disk.init()")
        assert handler_pos != -1, "add_signal_handler not found in Runner.run"
        assert disk_init_pos != -1, "disk.init() not found in Runner.run"
        assert handler_pos < disk_init_pos, (
            "Bug 12: signal handlers are registered AFTER disk.init(). "
            "A signal arriving during init is lost. "
            "Fix: move add_signal_handler before disk.init()."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 19 — usage_tracker race condition on shared dict
# ═══════════════════════════════════════════════════════════════════════


class TestBug19UsageTrackerRace:
    """record_step() modifies _providers dict without locking."""

    def test_concurrent_record_steps(self) -> None:
        from tero2.usage_tracker import UsageTracker

        tracker = UsageTracker()
        errors: list[Exception] = []

        def _worker(provider: str, count: int) -> None:
            try:
                for _ in range(count):
                    tracker.record_step(provider, tokens=10, cost=0.01, is_estimated=False)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=_worker, args=(f"provider_{i}", 100))
            for i in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        summary = tracker.session_summary()
        expected_tokens = 5 * 100 * 10
        assert summary["total_tokens"] == expected_tokens, (
            f"Expected {expected_tokens} tokens, got {summary['total_tokens']} — "
            "concurrent record_step lost data"
        )
        assert len(errors) == 0, f"Errors during concurrent access: {errors}"


# ═══════════════════════════════════════════════════════════════════════
# Bug 21 — shell provider subprocess not cleaned [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug21ShellProviderZombie:
    """[FIXED] communicate() exception must terminate the subprocess."""

    @pytest.mark.asyncio
    async def test_subprocess_terminated_on_communicate_error(self) -> None:
        from tero2.providers.shell import ShellProvider

        proc_mock = MagicMock()
        proc_mock.returncode = None
        proc_mock.communicate = AsyncMock(side_effect=RuntimeError("forced failure"))
        proc_mock.terminate = MagicMock()
        proc_mock.wait = AsyncMock(return_value=0)
        proc_mock.stdout = MagicMock()
        proc_mock.stderr = MagicMock()

        provider = ShellProvider.__new__(ShellProvider)

        with patch("tero2.providers.shell.asyncio.create_subprocess_exec",
                   return_value=proc_mock):
            with pytest.raises(RuntimeError, match="forced failure"):
                async for _ in provider.run(prompt="echo hi"):
                    pass

        proc_mock.terminate.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# Bug 22 — tui/app query_one without try/except in event consumer
# ═══════════════════════════════════════════════════════════════════════


class TestBug22TuiQueryOneCrash:
    """Screen transition during event processing → NoMatches crash.
    Fix: wrap query_one calls in try/except NoMatches."""

    def test_consume_events_handles_no_matches(self) -> None:
        from tero2.tui.app import DashboardApp
        source = inspect.getsource(DashboardApp._consume_events)
        assert "NoMatches" in source, (
            "Bug 22: _consume_events calls query_one without NoMatches guard. "
            "A screen transition mid-event crashes the worker. "
            "Fix: wrap query_one in try/except NoMatches."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 24 — runner off-by-one in slice loop
# ═══════════════════════════════════════════════════════════════════════


class TestBug24RunnerOffByOneSliceLoop:
    """while extra_slices_done < max_slices runs max_slices extra slices
    PLUS the initial S01 = max_slices + 1 total.
    If max_slices means total slices, the loop needs `< max_slices - 1`."""

    def test_slice_loop_caps_total_at_max_slices(self) -> None:
        """Bug 24: S01 runs before the loop, then loop adds up to max_slices MORE.
        Total = max_slices + 1.  Fix: loop condition must account for S01.
        After fix: `while extra_slices_done < max_slices - 1` (total = max_slices)."""
        import tero2.runner as runner_module
        source = inspect.getsource(runner_module.Runner._execute_sora)
        assert "extra_slices_done < max_slices" in source, (
            "slice loop condition not found in _execute_sora — re-verify method name"
        )
        # Bug present: condition is `< max_slices` (runs max_slices extra + S01 = total+1)
        # After fix: condition becomes `< max_slices - 1` (total = max_slices)
        assert "extra_slices_done < max_slices - 1" in source, (
            "Bug 24: slice loop runs S01 + max_slices extra = max_slices+1 total. "
            "Fix: use `while extra_slices_done < max_slices - 1` so total = max_slices."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 27 — telegram_input no file size check on download
# ═══════════════════════════════════════════════════════════════════════


class TestBug27TelegramNoFileSizeCheck:
    """_download_file downloads arbitrary-size files without a size limit."""

    @pytest.mark.asyncio
    async def test_download_rejects_oversized_file(self) -> None:
        from tero2.telegram_input import TelegramInputBot
        from tero2.config import TelegramConfig

        cfg = Config(telegram=TelegramConfig(bot_token="fake", chat_id="1"))
        bot = TelegramInputBot.__new__(TelegramInputBot)
        bot.config = cfg

        mock_file_resp = MagicMock()
        mock_file_resp.json.return_value = {
            "result": {"file_path": "documents/huge.txt", "file_size": 50_000_000}
        }
        mock_file_resp.status_code = 200

        mock_dl_resp = MagicMock()
        mock_dl_resp.status_code = 200
        mock_dl_resp.text = "x" * 1000  # content doesn't matter

        with patch("tero2.telegram_input.requests") as mock_requests:
            mock_requests.post.return_value = mock_file_resp
            mock_requests.get.return_value = mock_dl_resp

            result = await bot._download_file("fake_file_id")

            # Fix should reject files > some reasonable limit (e.g. 10 MB)
            assert result is None, (
                "Bug 27: _download_file accepted a 50 MB file without size check. "
                "Fix: check file_size before downloading and return None if too large."
            )


# ═══════════════════════════════════════════════════════════════════════
# Bug 28 — project_init sanitization [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug28ProjectInitEmptySanitization:
    """[FIXED] _sanitize_name falls back to 'project' for all-special-char input."""

    def test_sanitize_name_exclamation_marks(self) -> None:
        result = _sanitize_name("!!!")
        assert result != "", "sanitization must never return empty string"
        assert result == "project", f"expected fallback 'project', got '{result}'"

    def test_sanitize_name_all_special_chars(self) -> None:
        result = _sanitize_name("@#$%^&*")
        assert result != "", "all-special-char names must produce valid dir name"

    def test_sanitize_name_whitespace_only(self) -> None:
        result = _sanitize_name("   ")
        assert result != "", "whitespace-only names must produce valid dir name"


# ═══════════════════════════════════════════════════════════════════════
# Bug 29 — escalation inconsistent checkpointing
# ═══════════════════════════════════════════════════════════════════════


class TestBug29EscalationInconsistentCheckpoint:
    """State is mutated in-place BEFORE checkpoint.save() — if save fails,
    counters are reset but never persisted."""

    @pytest.mark.asyncio
    async def test_diversification_escalation_succeeds(self) -> None:
        """[FIXED guard] Diversification escalation updates state correctly."""
        from tero2.escalation import EscalationAction, EscalationLevel, execute_escalation
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckResult, StuckSignal

        state = AgentState()
        disk = MagicMock()
        notifier = AsyncMock()
        checkpoint = MagicMock()
        checkpoint.save.side_effect = lambda s: s

        action = EscalationAction(
            level=EscalationLevel.DIVERSIFICATION,
            inject_prompt="try differently",
        )
        stuck = StuckResult(signal=StuckSignal.STEP_LIMIT, details="stuck", severity=1)

        new_state = await execute_escalation(
            action, state, disk, notifier, checkpoint, stuck_result=stuck,
        )
        assert new_state.escalation_level == EscalationLevel.DIVERSIFICATION.value

    @pytest.mark.asyncio
    async def test_state_not_mutated_when_checkpoint_fails(self) -> None:
        """Bug 29: state is mutated before checkpoint.save — original state
        should remain unchanged if save raises."""
        from tero2.escalation import EscalationAction, EscalationLevel, execute_escalation
        from tero2.state import AgentState
        from tero2.stuck_detection import StuckResult, StuckSignal

        original_steps = 5
        state = AgentState(steps_in_task=original_steps, retry_count=3)
        disk = MagicMock()
        disk.append_file = MagicMock()
        notifier = AsyncMock()
        checkpoint = MagicMock()
        checkpoint.save.side_effect = OSError("disk full")

        action = EscalationAction(
            level=EscalationLevel.BACKTRACK_COACH,
            should_backtrack=True,
        )
        stuck = StuckResult(signal=StuckSignal.STEP_LIMIT, details="stuck", severity=1)

        with pytest.raises(OSError):
            await execute_escalation(
                action, state, disk, notifier, checkpoint, stuck_result=stuck,
            )

        # Fix: use dataclasses.replace() so the original state is immutable.
        # Bug: state.steps_in_task is 0 (mutated in-place before save failed).
        assert state.steps_in_task == original_steps, (
            f"Bug 29: state was mutated in-place before checkpoint.save raised. "
            f"steps_in_task={state.steps_in_task}, expected {original_steps}. "
            "Fix: build new state via dataclasses.replace(), only update original after save."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 32 — plan_pick stat() in sort [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug32PlanPickStatCrash:
    """[FIXED] _mtime() in plan_pick wraps stat() in try/except OSError."""

    def test_scan_md_files_does_not_crash_on_inaccessible_file(self, tmp_path: Path) -> None:
        from tero2.tui.screens.plan_pick import PlanPickScreen

        # Create a project dir with one valid plan
        (tmp_path / "plan.md").write_text("# plan")

        screen = PlanPickScreen.__new__(PlanPickScreen)
        screen._project_path = tmp_path

        # Patch stat() to raise OSError for the plan file
        original_stat = Path.stat
        def _bad_stat(self, *args, **kwargs):
            if self.name == "plan.md":
                raise OSError("permission denied")
            return original_stat(self, *args, **kwargs)

        with patch.object(Path, "stat", _bad_stat):
            try:
                files = screen._scan_md_files()
            except OSError as e:
                pytest.fail(f"Bug 32: _scan_md_files raised OSError: {e}")
        # Should return the file even if mtime fails (sorted with 0.0)
        assert len(files) == 1


# ═══════════════════════════════════════════════════════════════════════
# Bug 33 — project_pick n key crashes [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug33ProjectPickDuplicateIds:
    """[FIXED] action_manual_input guards against DuplicateIds with NoMatches."""

    def test_action_manual_input_has_no_matches_guard(self) -> None:
        from tero2.tui.screens.project_pick import ProjectPickScreen
        source = inspect.getsource(ProjectPickScreen.action_manual_input)
        assert "NoMatches" in source, (
            "Bug 33 REGRESSED: action_manual_input has no NoMatches guard — "
            "pressing 'n' twice mounts duplicate Input id and crashes."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 34 — app.py BINDINGS drift [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug34BindingsDrift:
    """[FIXED] DashboardApp BINDINGS include n, o, and labelled stuck options."""

    def test_bindings_has_new_project(self) -> None:
        from tero2.tui.app import DashboardApp
        binding_keys = [b[0] for b in DashboardApp.BINDINGS]
        assert "n" in binding_keys, "Bug 34 REGRESSED: 'n' key missing from BINDINGS"

    def test_bindings_has_settings(self) -> None:
        from tero2.tui.app import DashboardApp
        binding_keys = [b[0] for b in DashboardApp.BINDINGS]
        assert "o" in binding_keys, "Bug 34 REGRESSED: 'o' key missing from BINDINGS"

    def test_stuck_option_labels_not_empty(self) -> None:
        from tero2.tui.app import DashboardApp
        for binding in DashboardApp.BINDINGS:
            key = binding[0]
            if key in ("1", "2", "3", "4", "5"):
                label = binding[2] if len(binding) > 2 else ""
                assert label.strip() != "", (
                    f"Bug 34 REGRESSED: stuck option '{key}' has empty label"
                )


# ═══════════════════════════════════════════════════════════════════════
# Bug 36 — project_pick delete notification no handler
# ═══════════════════════════════════════════════════════════════════════


class TestBug36DeleteNotificationNoHandler:
    """'d' key referenced in notify() but no binding or handler exists."""

    def test_delete_binding_exists(self) -> None:
        from tero2.tui.screens.project_pick import ProjectPickScreen

        binding_keys = [
            (b.key if hasattr(b, "key") else b[0])
            for b in ProjectPickScreen.BINDINGS
        ]
        assert "d" in binding_keys, (
            "Bug 36: 'd' key referenced in notify() but missing from BINDINGS — "
            "pressing 'd' does nothing. Fix: add Binding('d', 'delete_entry', ...) "
            "and implement action_delete_entry."
        )

    def test_delete_action_handler_exists(self) -> None:
        from tero2.tui.screens.project_pick import ProjectPickScreen

        has_handler = (
            hasattr(ProjectPickScreen, "action_delete")
            or hasattr(ProjectPickScreen, "action_delete_entry")
        )
        assert has_handler, (
            "Bug 36: no action_delete / action_delete_entry handler — "
            "stale history entries accumulate with no way to remove them."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 40 — config.py silently swallows TOML syntax errors
# ═══════════════════════════════════════════════════════════════════════


class TestBug40ConfigSwallowsTOMLErrors:
    """_load_toml must raise ConfigError on syntax errors, not return {}."""

    def test_bad_toml_raises_config_error(self, tmp_path: Path) -> None:
        from tero2.config import _load_toml

        bad_toml = tmp_path / "config.toml"
        bad_toml.write_text("invalid = [ toml syntax", encoding="utf-8")

        with pytest.raises(ConfigError, match="syntax|TOML|parse"):
            _load_toml(bad_toml)

    def test_missing_file_returns_empty_dict(self, tmp_path: Path) -> None:
        """[FIXED guard] Missing file should return {} (not raise)."""
        from tero2.config import _load_toml
        result = _load_toml(tmp_path / "nonexistent.toml")
        assert result == {}


# ═══════════════════════════════════════════════════════════════════════
# Bug 41 — config_writer._load_toml doesn't catch TOMLDecodeError
# ═══════════════════════════════════════════════════════════════════════


class TestBug41ConfigWriterTOMLDecodeError:
    """_load_toml must not crash on bad TOML — must catch TOMLDecodeError."""

    def test_bad_toml_does_not_crash(self, tmp_path: Path) -> None:
        from tero2.config_writer import _load_toml
        from tero2.errors import ConfigError

        bad_toml = tmp_path / "config.toml"
        bad_toml.write_text("broken = [", encoding="utf-8")

        # Fix: catch TOMLDecodeError — either return {} or raise ConfigError.
        # Raw TOMLDecodeError must NOT propagate (crashes TUI settings screen).
        try:
            result = _load_toml(bad_toml)
            assert isinstance(result, dict)
        except ConfigError:
            pass  # ConfigError is acceptable: caller aborts the write (Bug 49 fix)
        except Exception as e:
            pytest.fail(
                f"Bug 41: _load_toml raised unexpected {type(e).__name__} on bad TOML. "
                "Must catch TOMLDecodeError and either return {} or raise ConfigError."
            )

    def test_load_toml_missing_file_returns_empty(self, tmp_path: Path) -> None:
        """[FIXED guard] Missing file should return {}."""
        from tero2.config_writer import _load_toml
        result = _load_toml(tmp_path / "nonexistent.toml")
        assert result == {}

    def test_load_toml_reads_utf8(self, tmp_path: Path) -> None:
        """config_writer._load_toml must read with encoding='utf-8'."""
        from tero2.config_writer import _load_toml
        import tero2.config_writer as cw_module
        source = inspect.getsource(cw_module._load_toml)
        assert 'encoding' in source or 'utf' in source, (
            "Bug 41: _load_toml reads with OS default encoding. "
            "Fix: add encoding='utf-8' to read_text()."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 42 — Scout os.path.isdir() without try/except
# ═══════════════════════════════════════════════════════════════════════


class TestBug42ScoutPermissionError:
    """build_file_tree must not propagate PermissionError from isdir()."""

    def test_build_file_tree_survives_permission_error_on_isdir(
        self, tmp_path: Path
    ) -> None:
        from tero2.players.scout import build_file_tree

        # Create a simple directory structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("pass")

        original_isdir = os.path.isdir

        def _isdir_raises(path: str) -> bool:
            if "src" in str(path) and not str(path).endswith("src"):
                raise PermissionError("access denied")
            return original_isdir(path)

        with patch("tero2.players.scout.os.path.isdir", side_effect=_isdir_raises):
            try:
                result = build_file_tree(str(tmp_path))
            except PermissionError as e:
                pytest.fail(
                    f"Bug 42: build_file_tree raised PermissionError: {e}. "
                    "Fix: wrap os.path.isdir(full) in try/except (PermissionError, OSError)."
                )
        assert isinstance(result, str)


# ═══════════════════════════════════════════════════════════════════════
# Bug 43 — providers_pick partial config on write failure mid-loop
# ═══════════════════════════════════════════════════════════════════════


class TestBug43ProvidersPickPartialConfig:
    """[FIXED] _write_project_config builds all sections in memory and writes
    once via _serialize_toml + atomic rename — no partial writes possible."""

    def test_write_project_config_is_atomic(self, tmp_path: Path) -> None:
        """All roles must appear in the config file after a successful write."""
        from tero2.tui.screens.providers_pick import ProvidersPickScreen
        from tero2.config_writer import _load_toml

        screen = ProvidersPickScreen.__new__(ProvidersPickScreen)
        screen._project_path = tmp_path
        screen._roles = {
            "builder":   ("claude",  ""),
            "architect": ("claude",  ""),
            "verifier":  ("opencode", ""),
            "scout":     ("opencode", ""),
            "coach":     ("opencode", ""),
        }

        screen._write_project_config()

        config_path = tmp_path / ".sora" / "config.toml"
        assert config_path.exists(), "config file must be created"
        data = _load_toml(config_path)
        roles = data.get("roles", {})
        for role in screen._roles:
            assert role in roles, f"role {role} missing from config"


# ═══════════════════════════════════════════════════════════════════════
# Bug 44 — providers_pick global config write silently swallowed
# ═══════════════════════════════════════════════════════════════════════


class TestBug44ProvidersPickSilentSwallow:
    """bare `except Exception: pass` around global config write hides errors."""

    def test_no_bare_except_pass_in_action_save(self) -> None:
        from tero2.tui.screens.providers_pick import ProvidersPickScreen
        source = inspect.getsource(ProvidersPickScreen.action_save)

        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "except Exception" in line:
                next_non_empty = next(
                    (l.strip() for l in lines[i + 1:] if l.strip()), ""
                )
                assert next_non_empty != "pass", (
                    "Bug 44: bare `except Exception: pass` in action_save silently "
                    "swallows global config write errors. "
                    "Fix: remove the inner try/except or notify the user on failure."
                )


# ═══════════════════════════════════════════════════════════════════════
# Bug 45 — estimate_tokens integer truncation underestimates small texts
# ═══════════════════════════════════════════════════════════════════════


class TestBug45EstimateTokensTruncation:
    """len(text) // 4 returns 0 for texts under 4 chars.
    Fix: max(1, len(text) // 4) for non-empty text."""

    def test_short_text_returns_at_least_one_token(self) -> None:
        result = estimate_tokens("abc")
        assert result >= 1, (
            f"Bug 45: estimate_tokens('abc') = {result}. "
            "Fix: return max(1, len(text) // 4) for non-empty text."
        )

    def test_single_char_returns_at_least_one_token(self) -> None:
        result = estimate_tokens("x")
        assert result >= 1, (
            f"Bug 45: estimate_tokens('x') = {result}. Integer truncation: 1//4=0."
        )

    def test_empty_string_returns_zero(self) -> None:
        """Empty string is legitimately 0 tokens — no change needed."""
        assert estimate_tokens("") == 0

    def test_multiple_short_sections_accumulate_nonzero(self) -> None:
        sections = ["abc", "def", "ghi", "jkl", "mno"]
        total = sum(estimate_tokens(s) for s in sections)
        assert total >= len(sections), (
            f"Bug 45: {len(sections)} non-empty sections estimated {total} tokens total. "
            "Each should contribute ≥ 1 token."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 46 — zai.py temp directory cleanup [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug46ZaiTempDirLeak:
    """[FIXED] zai.py has finally: shutil.rmtree — cleanup runs on cancellation."""

    def test_zai_run_has_finally_cleanup(self) -> None:
        """Inspect zai.py: run() must clean up tmp_claude_home in a finally block."""
        from tero2.providers import zai as zai_module
        source = inspect.getsource(zai_module.ZaiProvider.run)
        assert "finally" in source, "zai.run() has no finally block — tmp dir leaks"
        assert "rmtree" in source, "zai.run() finally block does not call rmtree"


# ═══════════════════════════════════════════════════════════════════════
# Bug 47 — config_writer TOCTOU on concurrent read-modify-write
# ═══════════════════════════════════════════════════════════════════════


class TestBug47ConfigWriterTOCTOU:
    """Concurrent read-modify-write loses changes from the other writer.
    Fix: add file locking (fcntl.flock or threading.Lock) around the full cycle."""

    def test_config_writer_has_no_file_locking(self) -> None:
        """Inspect config_writer.py: no file lock → TOCTOU is unfixed."""
        import tero2.config_writer as cw_module
        source = inspect.getsource(cw_module.write_global_config_section)
        has_lock = "flock" in source or "Lock" in source or "lock" in source.lower()
        assert has_lock, (
            "Bug 47: write_global_config_section has no file locking. "
            "Concurrent writers race on the read-modify-write cycle. "
            "Fix: use fcntl.flock() or threading.Lock() around the full operation."
        )

    def test_concurrent_writes_preserve_all_sections(self, tmp_path: Path) -> None:
        """Concurrent writes to different sections should not lose data."""
        from tero2.config_writer import write_global_config_section
        import time

        config_path = tmp_path / "config.toml"
        results: list[str] = []

        def _write_builder():
            time.sleep(0.001)
            write_global_config_section(config_path, "roles.builder", {"provider": "claude"})

        def _write_architect():
            write_global_config_section(config_path, "roles.architect", {"provider": "opus"})

        t1 = threading.Thread(target=_write_builder)
        t2 = threading.Thread(target=_write_architect)
        t1.start(); t2.start()
        t1.join(); t2.join()

        content = config_path.read_text()
        assert "builder" in content and "architect" in content, (
            "Bug 47: concurrent writes lost a section — TOCTOU in read-modify-write."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 48 — catalog.py deterministic .tmp path
# ═══════════════════════════════════════════════════════════════════════


class TestBug48CatalogDeterministicTmp:
    """_save_cache uses a fixed .tmp path — two instances race on it."""

    def test_tmp_path_includes_unique_identifier(self) -> None:
        """_save_cache must use a unique tmp path (PID or UUID), not just .tmp."""
        import tero2.providers.catalog as catalog_module
        source = inspect.getsource(catalog_module._save_cache)

        uses_pid = "getpid" in source or "os.getpid" in source
        uses_uuid = "uuid" in source.lower()
        uses_random = "random" in source or "uuid4" in source
        has_unique_tmp = uses_pid or uses_uuid or uses_random

        assert has_unique_tmp, (
            "Bug 48: _save_cache uses p.with_suffix('.tmp') — deterministic path. "
            "Two tero2 instances writing the same provider cache race on the same "
            "tmp file; first writer's data is lost. "
            "Fix: include PID or UUID in the tmp filename."
        )


# ═══════════════════════════════════════════════════════════════════════
# Bug 9 — context_assembly division by zero [REGRESSION GUARD — FIXED]
# ═══════════════════════════════════════════════════════════════════════


class TestBug9DivisionByZero:
    """[FIXED] target_ratio=0.0 raises ConfigError, not ZeroDivisionError."""

    def test_zero_target_ratio_raises_config_error(self) -> None:
        from tero2.context_assembly import _check_budget

        cfg = ContextConfig(target_ratio=0.0)
        with pytest.raises(ConfigError, match="positive"):
            _check_budget(100, 128000, cfg)
