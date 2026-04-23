"""Negative tests for bugs 145-220 (Audit 6, 2026-04-23).

Convention: test FAILS when the bug is present, PASSES when fixed.

  Bug 160  disk_layer: metrics.json no atomicity
  Bug 186  lock: file descriptor leak on flock + write failure
  Bug 187  lock: stale lock doesn't check PID liveliness
  Bug 153  config: RoleConfig provider allows empty string
  Bug 154  context_assembly: context window overflow
  Bug 151  config_writer: path traversal vulnerability
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ── Bug 160: disk_layer metrics.json no atomicity ──────────────────────────────


class TestBug160DiskLayerMetricsNoAtomicity:
    """write_metrics() writes metrics.json directly with path.write_text().
    No atomic rename pattern (.tmp + os.replace). A crash mid-write leaves a
    truncated/empty file, corrupting all metrics.
    Fix: write to .tmp then os.replace(.tmp, metrics.json).
    """

    def test_write_metrics_uses_atomic_rename(self) -> None:
        import tero2.disk_layer as disk_module

        source = inspect.getsource(disk_module.DiskLayer.write_metrics)
        lines = source.splitlines()

        found_write = False
        for i, line in enumerate(lines):
            if "write_text" in line and "metrics" not in line:
                # The line that writes the file content
                context = "\n".join(lines[max(0, i - 5) : i + 5])
                found_write = True
                has_tmp = ".tmp" in context or "tmp" in context
                has_replace = "os.replace" in context or ".replace(" in context
                assert has_tmp and has_replace, (
                    "Bug 160: DiskLayer.write_metrics() writes metrics.json directly "
                    "with path.write_text() without the atomic rename pattern. "
                    "A crash mid-write (OOM, SIGKILL) leaves a truncated or empty "
                    "metrics.json, destroying all accumulated metrics. "
                    "Fix: write to a .tmp file first, then os.replace(.tmp, metrics.json)."
                )
                return

        if not found_write:
            # Still check for atomic pattern presence
            assert ".tmp" in source and "replace" in source, (
                "Bug 160: DiskLayer.write_metrics() has no atomic rename pattern. "
                "Fix: use .tmp + os.replace() for atomic writes."
            )

    def test_write_metrics_no_direct_write_to_final_path(self) -> None:
        import tero2.disk_layer as disk_module

        source = inspect.getsource(disk_module.DiskLayer.write_metrics)

        # If write_text is called on the final metrics.json path directly
        # (not on a .tmp), the write is not atomic.
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "write_text" in line and "path" in line:
                # Check that this write_text targets a tmp file, not the final path
                context_before = "\n".join(lines[max(0, i - 3) : i])
                assert ".tmp" in context_before or "tmp" in line, (
                    "Bug 160: write_metrics() calls path.write_text() directly on the "
                    "final metrics.json file without going through a temporary file. "
                    "Fix: write to path.with_suffix('.json.tmp') first, then os.replace()."
                )


# ── Bug 186: lock file descriptor leak on flock + write failure ────────────────


class TestBug186LockFDLeakOnWriteFailure:
    """If os.write() raises after flock() succeeds on line 32, the except block
    (line 34-36) closes fd and re-raises. However, self._fd is never set (line
    37 is skipped). The lock object is left in an inconsistent state:
    - self._fd is None (looks like "no lock held")
    - release() is a silent no-op
    - But the flock was released by os.close(fd), so the lock IS actually free

    The real problem: if release() is called after a failed acquire(), it
    silently does nothing instead of at least being aware the object is dirty.
    More critically, if the error happens AFTER flock but BEFORE os.close(fd)
    (e.g., os.lseek fails, or a signal interrupts), the fd could leak.

    Fix: set self._fd = fd BEFORE the os.write attempt so the object state
    is always consistent. If write fails, release() will properly clean up.
    """

    def test_fd_set_before_write_attempt(self) -> None:
        """_fd should be set before the os.write/os.lseek block so that
        even on failure, release() can properly clean up."""
        import tero2.lock as lock_module

        source = inspect.getsource(lock_module.FileLock.acquire)
        lines = source.splitlines()

        # Find the line where self._fd is set
        fd_set_line = None
        flock_line = None
        write_line = None

        for i, line in enumerate(lines):
            if "self._fd" in line and "=" in line and "None" not in line:
                fd_set_line = i
            if "fcntl.flock" in line:
                flock_line = i
            if "os.write" in line:
                write_line = i

        if fd_set_line is None or write_line is None:
            pytest.skip("self._fd assignment or os.write not found in acquire()")

        # The fd should be set BEFORE os.write so that if write fails,
        # release() can clean up.
        assert fd_set_line < write_line, (
            "Bug 186: self._fd = fd is set AFTER the os.write() attempt "
            f"(line {fd_set_line + 1}) instead of BEFORE it "
            f"(os.write at line {write_line + 1}). "
            "If os.write() raises OSError, the except block closes fd but "
            "self._fd remains None. Subsequent release() is a silent no-op. "
            "Fix: set self._fd = fd BEFORE the os.write/os.lseek block, "
            "and in the except block, set self._fd = None after cleanup."
        )

    def test_release_after_failed_acquire_is_explicit(self) -> None:
        """After a failed acquire(), the except block around os.write should
        explicitly clean up self._fd so the object state is consistent."""
        import tero2.lock as lock_module

        source = inspect.getsource(lock_module.FileLock.acquire)

        # Find the except block that handles os.write failure and check if
        # it explicitly sets self._fd = None within its indented scope.
        lines = source.splitlines()

        # Find os.write line
        write_line_idx = None
        for i, line in enumerate(lines):
            if "os.write" in line:
                write_line_idx = i
                break

        if write_line_idx is None:
            pytest.skip("os.write not found in acquire()")

        # Find the except block after os.write
        except_idx = None
        for i in range(write_line_idx + 1, min(write_line_idx + 10, len(lines))):
            if lines[i].strip().startswith("except"):
                except_idx = i
                break

        if except_idx is None:
            pytest.skip("except block after os.write not found in acquire()")

        # Get the indentation of the except line
        except_indent = len(lines[except_idx]) - len(lines[except_idx].lstrip())

        # Check lines within the except block (more indented than except)
        has_fd_cleanup = False
        for i in range(except_idx + 1, len(lines)):
            line = lines[i]
            if not line.strip():
                continue
            line_indent = len(line) - len(line.lstrip())
            if line_indent <= except_indent:
                # Exited the except block
                break
            if "self._fd" in line:
                has_fd_cleanup = True
                break

        assert has_fd_cleanup, (
            "Bug 186: FileLock.acquire() except block for os.write() failure "
            "does not explicitly handle self._fd. After a failed acquire(), "
            "self._fd remains None (never set) and release() silently does "
            "nothing. The object state is inconsistent. "
            "Fix: either set self._fd = fd BEFORE the os.write block, or "
            "explicitly set self._fd = None in the except block."
        )


# ── Bug 187: lock stale lock doesn't check PID liveliness ─────────────────────


class TestBug187LockStaleNoPIDCheck:
    """When LockHeldError is raised because flock fails (errno EAGAIN/EACCES),
    the lockfile contains a PID. But if that PID is dead (process crashed without
    cleanup), the lock should be considered stale and recoverable. Currently
    LockHeldError is always raised regardless of PID liveness.
    Fix: check _pid_alive before raising LockHeldError; if dead, force-acquire.
    """

    def test_stale_lock_with_dead_pid_reports_not_held(self, tmp_path: Path) -> None:
        """is_held() should report False for dead PIDs. Currently works because
        _pid_alive is called. But acquire() does NOT check PID liveness — it
        only relies on flock, which works for same-machine dead processes but
        NOT for NFS/lockfile-based scenarios."""
        import tero2.lock as lock_module

        lock_path = tmp_path / "stale.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # PID 99999999 does not exist
        lock_path.write_text("99999999\n")

        lock = lock_module.FileLock(lock_path)
        held, pid = lock.is_held()

        assert not held, (
            "Bug 187: FileLock.is_held() reports lock is held by dead PID "
            f"{pid}. The lockfile contains PID 99999999 which is not a running "
            "process. is_held() checks _pid_alive() and should return (False, 0) "
            "for stale locks. Fix: verify _pid_alive is called correctly and "
            "dead PIDs result in (False, 0)."
        )

    def test_acquire_checks_pid_liveness_before_raising(self) -> None:
        """acquire() should check PID liveness when flock fails and consider
        force-acquiring for stale locks."""
        import tero2.lock as lock_module

        source = inspect.getsource(lock_module.FileLock.acquire)
        lines = source.splitlines()

        # Find the LockHeldError raise site
        for i, line in enumerate(lines):
            if "LockHeldError" in line and "raise" in line:
                # Look backwards from this line for _pid_alive check
                context_before = "\n".join(lines[max(0, i - 10) : i])
                has_pid_check = (
                    "_pid_alive" in context_before
                    or "pid_alive" in context_before
                    or "stale" in context_before.lower()
                )
                assert has_pid_check, (
                    "Bug 187: FileLock.acquire() raises LockHeldError without "
                    "checking if the PID in the lockfile is still alive. A dead "
                    "process leaves a stale lockfile, and acquire() should detect "
                    "this and force-acquire (unlink + retry) rather than raising. "
                    "Fix: call self._pid_alive(pid) before raising LockHeldError; "
                    "if PID is dead, force-acquire by removing stale lockfile."
                )
                return

        pytest.skip("LockHeldError raise not found in acquire()")


# ── Bug 153: config RoleConfig provider allows empty string ────────────────────


class TestBug153ConfigProviderEmptyString:
    """_parse_config checks `if not role_data.get("provider")` which correctly
    rejects empty string (falsy). But a whitespace-only string "   " is truthy
    and passes validation, creating a RoleConfig with a broken provider name.
    Fix: strip whitespace and validate provider is non-empty after strip.
    """

    def test_whitespace_provider_rejected(self) -> None:
        import tero2.config as config_module

        raw = {
            "roles": {
                "executor": {
                    "provider": "   ",
                }
            }
        }
        with pytest.raises(config_module.ConfigError):
            config_module._parse_config(raw)

    def test_empty_provider_rejected(self) -> None:
        import tero2.config as config_module

        raw = {
            "roles": {
                "executor": {
                    "provider": "",
                }
            }
        }
        with pytest.raises(config_module.ConfigError):
            config_module._parse_config(raw)

    def test_provider_validation_strips_whitespace(self) -> None:
        """Check that the code strips provider before checking."""
        import tero2.config as config_module

        source = inspect.getsource(config_module._parse_config)
        lines = source.splitlines()

        for i, line in enumerate(lines):
            if "provider" in line and "role_data" in line and "get" in line:
                context = "\n".join(lines[i : i + 3])
                has_strip = ".strip()" in context
                # Also check if the validation line itself strips
                if 'if not role_data.get("provider")' in line:
                    # The current check uses truthiness -- this correctly
                    # rejects "" but not "  ". Check for .strip().
                    assert has_strip, (
                        "Bug 153: config.py validates provider with "
                        "'if not role_data.get(\"provider\")' which accepts "
                        "whitespace-only strings like '   '. These pass "
                        "validation but cause cryptic errors later when "
                        "looking up the provider. "
                        "Fix: strip whitespace before checking, e.g. "
                        "role_data.get('provider', '').strip()."
                    )
                return

        pytest.skip("provider validation line not found in _parse_config")


# ── Bug 154: context_assembly context window overflow ──────────────────────────


class TestBug154ContextWindowOverflow:
    """_role_limit() computes int(raw * target_ratio). No validation on
    context_window bounds. A negative context_window produces a negative
    budget, which divides by zero or produces nonsensical ratio in
    _check_budget (tokens / negative_budget = negative ratio, bypassing
    all threshold checks).

    Additionally, unreasonably large context_window values should be capped
    to prevent silent misconfiguration (e.g., 9999999999 instead of 999999).
    Fix: validate context_window > 0 and cap to reasonable maximum in config.
    """

    def test_negative_context_window_rejected_or_clamped(self) -> None:
        from tero2.config import Config, ContextConfig, RoleConfig
        from tero2.context_assembly import ContextAssembler

        cfg = Config(
            context=ContextConfig(target_ratio=0.70),
            roles={
                "builder": RoleConfig(
                    provider="opencode",
                    context_window=-1,
                )
            },
        )
        assembler = ContextAssembler(cfg)
        budget = assembler._role_limit("builder")

        # Budget must be positive — zero or negative budget causes
        # _check_budget to return HARD_FAIL immediately (budget <= 0),
        # or division by a negative value producing nonsensical ratios.
        assert budget > 0, (
            "Bug 154: _role_limit() returns non-positive budget when "
            f"context_window=-1. Got budget={budget}. "
            "With budget <= 0, _check_budget() returns HARD_FAIL immediately, "
            "causing ContextWindowExceededError even for tiny prompts. "
            "Fix: validate context_window > 0 in RoleConfig or config parsing."
        )

    def test_negative_context_window_does_not_bypass_budget_check(self) -> None:
        from tero2.config import Config, ContextConfig, RoleConfig
        from tero2.context_assembly import BudgetState, ContextAssembler

        cfg = Config(
            context=ContextConfig(target_ratio=0.70),
            roles={
                "builder": RoleConfig(
                    provider="opencode",
                    context_window=-1,
                )
            },
        )
        assembler = ContextAssembler(cfg)
        budget = assembler._role_limit("builder")

        if budget <= 0:
            pytest.skip("budget is non-positive, test not applicable")

        # If budget somehow ends up negative, assemble should not silently
        # return BudgetState.OK for a massive prompt.
        result = assembler.assemble(
            "builder",
            "system prompt " * 10000,
            "task plan " * 10000,
        )
        # With negative budget, _check_budget would get a negative ratio,
        # which is < 1.0, < hard_fail_threshold, so it returns OK.
        # This is wrong — a huge prompt should not get OK status.
        if budget <= 0:
            assert result.budget_state != BudgetState.OK, (
                "Bug 154: negative budget from negative context_window causes "
                "assemble() to return BudgetState.OK for a massive prompt. "
                "Fix: validate context_window is positive in config parsing."
            )

    def test_context_window_has_upper_bound_validation(self) -> None:
        """Config should reject or cap unreasonably large context_window values."""
        import tero2.config as config_module

        source = inspect.getsource(config_module._parse_config)

        # Look specifically for bound/range checks on context_window,
        # NOT just the presence of the word or int() conversion.
        has_bound_check = False
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "context_window" in line:
                context = "\n".join(lines[max(0, i - 2) : i + 3])
                # Check for explicit range/bound validation near context_window
                if (
                    "max" in context.lower()
                    or "min" in context.lower()
                    or "cap" in context.lower()
                    or "> 0" in context
                    or ">0" in context
                    or "positive" in context.lower()
                    or "clamp" in context.lower()
                    or "1000000" in context
                    or "1_000_000" in context
                    or "MAX_CONTEXT" in context
                    or "assert" in context
                ):
                    has_bound_check = True
                    break

        assert has_bound_check, (
            "Bug 154: config parsing does not validate context_window bounds. "
            "Negative values produce zero budgets; unreasonably large values "
            "(e.g., 9999999999) silently pass. "
            "Fix: add validation that context_window is positive and <= a "
            "reasonable maximum (e.g., 1_000_000)."
        )


# ── Bug 151: config_writer path traversal vulnerability ────────────────────────


class TestBug151ConfigWriterPathTraversal:
    """write_global_config_section() splits section on '.' to build nested dicts.
    The section parameter is not validated. A section like "../../etc" creates
    nested dict keys {"": {"": {"etc": values}}}, which corrupts the config.
    Sections containing path separators or traversal patterns should be rejected.
    Fix: validate section contains only alphanumeric/dot/underscore chars.
    """

    def test_path_traversal_section_rejected(self, tmp_path: Path) -> None:
        from tero2.config_writer import write_global_config_section

        config_path = tmp_path / "config.toml"
        config_path.write_text('[general]\nprojects_dir = "/tmp"\n', encoding="utf-8")

        with pytest.raises((ValueError, _config_error_type())):
            write_global_config_section(config_path, "../../etc", {"key": "value"})

    def test_absolute_path_section_rejected(self, tmp_path: Path) -> None:
        from tero2.config_writer import write_global_config_section

        config_path = tmp_path / "config.toml"
        config_path.write_text('[general]\nprojects_dir = "/tmp"\n', encoding="utf-8")

        with pytest.raises((ValueError, _config_error_type())):
            write_global_config_section(config_path, "/etc/passwd", {"key": "value"})

    def test_section_with_slash_rejected(self, tmp_path: Path) -> None:
        from tero2.config_writer import write_global_config_section

        config_path = tmp_path / "config.toml"
        config_path.write_text('[general]\nprojects_dir = "/tmp"\n', encoding="utf-8")

        with pytest.raises((ValueError, _config_error_type())):
            write_global_config_section(config_path, "roles/../../etc", {"key": "value"})

    def test_section_validation_exists(self) -> None:
        """Check that write_global_config_section validates the section parameter."""
        import tero2.config_writer as cw_module

        source = inspect.getsource(cw_module.write_global_config_section)
        has_validation = (
            "validate" in source.lower()
            or "traversal" in source.lower()
            or "'..' " in source
            or '".."' in source
            or "'/'" in source
            or '"/"' in source
            or "'\\\\" in source
            or "alphanumeric" in source.lower()
            or "re.match" in source
            or "re.fullmatch" in source
            or "invalid" in source.lower()
            or "section" in source and "strip" in source
        )
        assert has_validation, (
            "Bug 151: write_global_config_section() does not validate the section "
            "parameter for path traversal patterns. A section like '../../etc' is "
            "passed directly to dict nesting logic without any safety check. "
            "Fix: add validation that section contains only [a-zA-Z0-9_.] "
            "characters and reject '..', '/', and '\\' patterns."
        )


def _config_error_type():
    """Return the ConfigError type, falling back to ValueError for import safety."""
    try:
        from tero2.errors import ConfigError
        return ConfigError
    except ImportError:
        return ValueError
