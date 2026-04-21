"""Negative tests for new bugs N1-N6 from bugs.md.

Convention:
  - Each test FAILS when the bug is present (red).
  - Each test PASSES when the bug is fixed (green / regression guard).

Run:  pytest tests/test_new_bugs.py -v
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
# N1 — tui/app.py: log used but logging never imported
# ═══════════════════════════════════════════════════════════════════════


class TestN1AppLogNotImported:
    """`log` referenced in exception handling but `logging` module is never
    imported in app.py.  When the handler fires, Python raises NameError
    instead of logging the original error.

    Fix: add `import logging` and `log = logging.getLogger(__name__)`.
    """

    def test_app_module_imports_logging(self) -> None:
        import tero2.tui.app as app_module

        source = inspect.getsource(app_module)
        assert "import logging" in source, (
            "Bug N1: tui/app.py uses `log` but never imports `logging`. "
            "Any exception handler that calls log.error/log.warning raises NameError. "
            "Fix: add `import logging` and `log = logging.getLogger(__name__)`."
        )

    def test_app_module_has_log_logger(self) -> None:
        import tero2.tui.app as app_module

        source = inspect.getsource(app_module)
        has_logging_import = "import logging" in source
        has_log_define = "log = logging.getLogger" in source or "log=" in source.replace(
            " ", ""
        )
        assert has_logging_import and has_log_define, (
            "Bug N1: tui/app.py references `log` without defining it. "
            "Fix: add `import logging` and `log = logging.getLogger(__name__)`."
        )


# ═══════════════════════════════════════════════════════════════════════
# N2 — providers/chain.py: non-recoverable errors trip circuit breaker
# ═══════════════════════════════════════════════════════════════════════


class TestN2ChainNonRecoverableErrorsTripCB:
    """When a non-recoverable error (ConfigError, ValueError, TypeError)
    escapes a provider, chain.run() calls cb.record_failure() before
    re-raising.  This poisons the circuit breaker for a provider that is
    actually healthy — the failure is a config/logic bug, not a provider
    availability issue.

    Fix: only record_failure() for recoverable errors. Non-recoverable
    errors should `raise` without touching the CB.
    """

    def test_non_recoverable_error_does_not_record_failure(self) -> None:
        """Source check: the `except Exception` branch that handles
        non-recoverable errors must NOT call cb.record_failure()."""
        import tero2.providers.chain as chain_module

        source = inspect.getsource(chain_module.ProviderChain.run)
        lines = source.splitlines()

        # Find the pattern: `if not _is_recoverable_error(exc):` followed
        # by cb.record_failure() then raise — the record_failure is the bug.
        found_non_recoverable_branch = False
        records_failure_before_raise = False

        for i, line in enumerate(lines):
            if "_is_recoverable_error" in line and "not" in line:
                found_non_recoverable_branch = True
                # Check next 5 lines for cb.record_failure before raise
                for k in range(i + 1, min(i + 6, len(lines))):
                    stripped = lines[k].strip()
                    if "record_failure" in stripped:
                        records_failure_before_raise = True
                        break
                    if "raise" in stripped:
                        break
                break

        if not found_non_recoverable_branch:
            pytest.skip("non-recoverable error branch not found in chain.run()")

        assert not records_failure_before_raise, (
            "Bug N2: chain.run() calls cb.record_failure() for non-recoverable "
            "errors (ConfigError, ValueError, etc.) before re-raising. "
            "This disables a healthy provider via the circuit breaker. "
            "Fix: only record_failure for recoverable errors; non-recoverable "
            "should raise without touching the CB."
        )


# ═══════════════════════════════════════════════════════════════════════
# N3 — runner.py: error message says "max_slices additional" but loop
#        runs max_slices-1 additional slices
# ═══════════════════════════════════════════════════════════════════════


class TestN3RunnerSliceMessageMismatch:
    """The slice-limit error message says "max_slices additional slices
    beyond S01", but S01 runs BEFORE the loop.  With the loop condition
    `extra_slices_done < max_slices`, total slices = max_slices + 1.

    When max_slices=3, the user expects 3 slices total, but gets 4
    (S01 + S02 + S03 + S04).  The message says "3 additional" which is
    accurate for the loop count, but misleading because the TOTAL is 4.

    Fix: loop condition `< max_slices - 1` (Bug 24) or change message to
    state total slices (e.g. "max_slices+1 total slices").
    """

    def test_total_slices_equals_max_slices(self) -> None:
        """After S01 + loop, total must equal max_slices, not max_slices+1.
        Loop condition must be `< max_slices - 1` so that:
            total = 1 (S01) + (max_slices - 1) (loop) = max_slices."""
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._execute_sora)

        # Bug present: `< max_slices` → total = max_slices + 1
        # After fix:   `< max_slices - 1` → total = max_slices
        assert "extra_slices_done < max_slices - 1" in source, (
            "Bug N3/24: loop condition `< max_slices` gives total = "
            "max_slices + 1 (S01 + max_slices extra). "
            "Fix: use `< max_slices - 1` so total = max_slices."
        )


# ═══════════════════════════════════════════════════════════════════════
# N4 — events.py / tui/app.py: TUI subscribes but never unsubscribes
# ═══════════════════════════════════════════════════════════════════════


class TestN4TuiNeverUnsubscribes:
    """DashboardApp calls dispatcher.subscribe() in on_mount() but never
    calls dispatcher.unsubscribe() when the app exits.  Dead queues
    accumulate in EventDispatcher._subscribers — each queue holds up to
    500 events, wasting memory and slowing emit().

    Fix: call self._dispatcher.unsubscribe(self._event_queue) in a
    cleanup method (on_unmount / on_shutdown).
    """

    def test_app_calls_unsubscribe_on_shutdown(self) -> None:
        """DashboardApp must call unsubscribe in on_unmount or on_shutdown."""
        import tero2.tui.app as app_module

        has_on_unmount = hasattr(app_module.DashboardApp, "on_unmount")
        has_on_shutdown = hasattr(app_module.DashboardApp, "on_shutdown")

        if not has_on_unmount and not has_on_shutdown:
            assert False, (
                "Bug N4: DashboardApp has no on_unmount or on_shutdown method. "
                "subscribe() is called in on_mount() but the queue is never "
                "removed from the dispatcher. Dead queues accumulate. "
                "Fix: add on_unmount() that calls "
                "self._dispatcher.unsubscribe(self._event_queue)."
            )

        # Check that whichever lifecycle hook exists calls unsubscribe
        for method_name in ("on_unmount", "on_shutdown"):
            if hasattr(app_module.DashboardApp, method_name):
                source = inspect.getsource(
                    getattr(app_module.DashboardApp, method_name)
                )
                assert "unsubscribe" in source, (
                    f"Bug N4: {method_name} exists but doesn't call "
                    "self._dispatcher.unsubscribe(). Dead event queues "
                    "accumulate in EventDispatcher._subscribers. "
                    "Fix: call self._dispatcher.unsubscribe(self._event_queue)."
                )

    def test_subscribe_and_unsubscribe_are_paired(self) -> None:
        """Source check: on_mount calls subscribe, on_unmount calls unsubscribe."""
        import tero2.tui.app as app_module

        mount_source = inspect.getsource(app_module.DashboardApp.on_mount)
        assert "subscribe" in mount_source, "on_mount doesn't call subscribe"

        # Now verify unsubscribe exists in cleanup
        has_cleanup = False
        for method_name in ("on_unmount", "on_shutdown"):
            if hasattr(app_module.DashboardApp, method_name):
                source = inspect.getsource(
                    getattr(app_module.DashboardApp, method_name)
                )
                if "unsubscribe" in source:
                    has_cleanup = True
                    break

        assert has_cleanup, (
            "Bug N4: subscribe() in on_mount has no matching unsubscribe(). "
            "Each TUI session leaks an asyncio.Queue[500] in the dispatcher. "
            "Fix: call self._dispatcher.unsubscribe(self._event_queue) "
            "in on_unmount()."
        )


# ═══════════════════════════════════════════════════════════════════════
# N5 — scout.py: _count_files skips _SKIP_DIRS but not hidden dirs
# ═══════════════════════════════════════════════════════════════════════


class TestN5ScoutCountFilesSkipsHiddenDirs:
    """_count_files() filters `_SKIP_DIRS` but not hidden directories
    (starting with `.`).  Meanwhile, build_file_tree() DOES filter
    hidden dirs (line 155).  This inconsistency means should_skip()
    counts files inside .venv, .git, .mypy_cache etc. — inflating the
    count and potentially preventing Scout from being skipped on small
    projects.

    Fix: add `and not d.startswith(".")` to the _dirs filter in
    _count_files, matching build_file_tree's behavior.
    """

    def test_count_files_filters_hidden_dirs(self) -> None:
        """_count_files must filter hidden dirs like build_file_tree does."""
        import tero2.players.scout as scout_module

        source = inspect.getsource(scout_module._count_files)
        assert 'startswith(".")' in source or "startswith('.')" in source, (
            "Bug N5: _count_files filters _SKIP_DIRS but not hidden dirs "
            "(d.startswith('.')). build_file_tree filters both — inconsistent. "
            "Files in .venv, .git etc. inflate the count, breaking should_skip(). "
            "Fix: add `and not d.startswith('.')` to the _dirs filter."
        )

    def test_count_files_matches_build_file_tree_filtering(self) -> None:
        """Both _count_files and build_file_tree must apply the same
        directory exclusion rules."""
        import tero2.players.scout as scout_module

        count_source = inspect.getsource(scout_module._count_files)
        tree_source = inspect.getsource(scout_module.build_file_tree)

        # build_file_tree filters: not startswith(".") and not in _SKIP_DIRS
        tree_filters_hidden = 'startswith(".")' in tree_source or "startswith('.')" in tree_source
        count_filters_hidden = (
            'startswith(".")' in count_source or "startswith('.')" in count_source
        )

        if not tree_filters_hidden:
            pytest.skip("build_file_tree doesn't filter hidden dirs — can't compare")

        assert count_filters_hidden, (
            "Bug N5: build_file_tree filters hidden dirs but _count_files doesn't. "
            "should_skip() counts files in .venv/.git — inflated count prevents "
            "Scout skip on small projects. "
            "Fix: add hidden-dir filter to _count_files matching build_file_tree."
        )


# ═══════════════════════════════════════════════════════════════════════
# N6 — disk_layer.py: read_file returns "" on OSError vs None on FileNotFoundError
# ═══════════════════════════════════════════════════════════════════════


class TestN6DiskLayerReadFileInconsistentReturn:
    """read_file() returns:
      - None when FileNotFoundError (file doesn't exist)
      - ""   when OSError (permission denied, I/O error, etc.)

    Callers can't distinguish "file missing" from "read error returned
    empty data".  For example, read_override() does `return ... or ""`
    which treats both None and "" the same way.

    Fix: return None for ALL OSError subclasses (or raise a custom
    exception for non-FileNotFoundError OSError). At minimum, return
    None instead of "" for permission/I/O errors.
    """

    def test_oserror_returns_empty_string_not_none(self) -> None:
        """Source check: the OSError handler must return "", not None.
        This distinguishes 'file exists but unreadable' from FileNotFoundError."""
        import tero2.disk_layer as disk_module

        source = inspect.getsource(disk_module.DiskLayer.read_file)
        lines = source.splitlines()

        found = False
        for line in lines:
            stripped = line.strip()
            if stripped == 'return ""' or stripped == "return ''":
                for prev in lines:
                    if "OSError" in prev and "except" in prev:
                        oserror_indent = len(prev) - len(prev.lstrip())
                        return_indent = len(line) - len(line.lstrip())
                        if return_indent > oserror_indent:
                            found = True

        assert found, (
            "OSError handler must return '' (empty string) to distinguish "
            "from FileNotFoundError which returns None. Callers can check "
            "`is None` for missing vs `== ''` for unreadable."
        )

    def test_read_file_returns_none_on_permission_error(self, tmp_path: Path) -> None:
        """When read_text raises PermissionError (an OSError subclass),
        read_file must return None — not an empty string."""
        from tero2.disk_layer import DiskLayer

        dl = DiskLayer(tmp_path)
        dl.init()

        # Create a file
        test_file = dl.sora_dir / "human" / "OVERRIDE.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("test content", encoding="utf-8")

        with patch("pathlib.Path.read_text", side_effect=PermissionError("denied")):
            result = dl.read_file("human/OVERRIDE.md")

        assert result == "", (
            f"Bug N6: read_file returned {result!r} on PermissionError — "
            "should return '' (empty string). This distinguishes 'file exists "
            "but unreadable' from FileNotFoundError which returns None. "
            "Callers can check `is None` vs `== ''` to handle each case."
        )
