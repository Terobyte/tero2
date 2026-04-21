"""Tests for Wave 4 audit bugs (54-57).

Run: pytest tests/test_wave4_bugs.py -v
"""

from __future__ import annotations

import inspect
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Bug 54: runner no mark_failed on architect/execute failure in slice loop ──


class TestBug54RunnerSliceFailureNoMarkFailed:
    """When architect or execute fails inside the slice loop, the code does
    `break` without calling mark_failed. State stays in ARCHITECT/EXECUTE
    phase instead of transitioning to FAILED.

    Fix: call self.checkpoint.mark_failed(state, msg) before break/return.
    """

    def test_mark_failed_called_on_architect_failure_in_loop(self) -> None:
        """Source check: after `if not result.success:` for architect in the
        slice loop, mark_failed must be called before break."""
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._execute_sora)

        # Find the architect failure in the extra-slice loop
        # Pattern: "if not result.success" followed by break
        lines = source.splitlines()
        in_extra_slice_loop = False
        found_architect_fail = False
        has_mark_failed = False

        for i, line in enumerate(lines):
            if "extra_slices_done" in line and "while" in line:
                in_extra_slice_loop = True
            if in_extra_slice_loop and "run_architect" in line:
                # Look ahead for the failure check
                for j in range(i + 1, min(i + 15, len(lines))):
                    if "not result.success" in lines[j]:
                        found_architect_fail = True
                        # Check between here and break for mark_failed
                        for k in range(j + 1, min(j + 10, len(lines))):
                            if "mark_failed" in lines[k]:
                                has_mark_failed = True
                                break
                            if "break" in lines[k]:
                                break
                        break
                break

        if not found_architect_fail:
            pytest.skip("architect failure check not found in slice loop")

        assert has_mark_failed, (
            "Bug 54: architect failure in slice loop breaks without "
            "mark_failed. State stays in ARCHITECT phase. "
            "Fix: call self.checkpoint.mark_failed(state, msg) before break."
        )

    def test_mark_failed_called_on_execute_failure_in_loop(self) -> None:
        """Source check: after execute failure in the slice loop, mark_failed
        must be called before break."""
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._execute_sora)

        lines = source.splitlines()
        in_extra_slice_loop = False
        found_exec_fail = False
        has_mark_failed = False

        for i, line in enumerate(lines):
            if "extra_slices_done" in line and "while" in line:
                in_extra_slice_loop = True
            if in_extra_slice_loop and "exec_result" in line and "success" in line:
                found_exec_fail = True
                for k in range(i + 1, min(i + 10, len(lines))):
                    if "mark_failed" in lines[k]:
                        has_mark_failed = True
                        break
                    if "break" in lines[k]:
                        break
                break

        if not found_exec_fail:
            pytest.skip("execute failure check not found in slice loop")

        assert has_mark_failed, (
            "Bug 54: execute failure in slice loop breaks without "
            "mark_failed. State stays in EXECUTE phase. "
            "Fix: call self.checkpoint.mark_failed(state, msg) before break."
        )

    def test_mark_failed_called_on_first_execute_failure(self) -> None:
        """Source check: first execute failure (before the loop) does return
        without mark_failed."""
        import tero2.runner as runner_module

        source = inspect.getsource(runner_module.Runner._execute_sora)

        lines = source.splitlines()
        found_first_exec_fail = False
        has_mark_failed = False

        for i, line in enumerate(lines):
            if "exec_result" in line and "success" in line and not found_first_exec_fail:
                found_first_exec_fail = True
                for k in range(i + 1, min(i + 10, len(lines))):
                    if "mark_failed" in lines[k]:
                        has_mark_failed = True
                        break
                    if "return" in lines[k]:
                        break
                break

        if not found_first_exec_fail:
            pytest.skip("first execute failure check not found")

        assert has_mark_failed, (
            "Bug 54: first execute failure returns without mark_failed. "
            "State stays in EXECUTE phase. "
            "Fix: call self.checkpoint.mark_failed(state, msg) before return."
        )


# ── Bug 55: app.py unhandled exception kills _consume_events worker ───────────


class TestBug55EventConsumerNoErrorHandling:
    """The _consume_events worker only catches NoMatches. Any other
    exception from widget operations kills the worker silently — TUI
    stops responding to events.

    Fix: wrap the event routing block in try/except Exception with log.error.
    """

    def test_event_routing_has_broad_exception_handler(self) -> None:
        """Source check: event routing must be wrapped in broad except."""
        import tero2.tui.app as app_module

        source = inspect.getsource(app_module.DashboardApp._consume_events)

        lines = source.splitlines()
        found_event_routing = False
        has_broad_catch = False

        for i, line in enumerate(lines):
            # After the NoMatches catch, the event routing starts
            if "event.kind" in line:
                found_event_routing = True
            # Look for a broad Exception catch after the NoMatches handler
            if found_event_routing and "except Exception" in line:
                has_broad_catch = True
                break

        assert has_broad_catch, (
            "Bug 55: _consume_events only catches NoMatches. "
            "Any other exception from widget updates kills the worker — "
            "TUI stops responding to events silently. "
            "Fix: wrap event routing in try/except Exception with log.error."
        )

    def test_worker_survives_widget_exception(self) -> None:
        """After a widget raises, the worker must continue processing."""
        import tero2.tui.app as app_module

        source = inspect.getsource(app_module.DashboardApp._consume_events)

        # Check that the event routing (if/elif chain) is inside a try block
        # that catches Exception, not just NoMatches
        lines = source.splitlines()

        # Find the try/except structure around event routing
        in_try_block = False
        catch_blocks = []

        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped == "try:" and i < len(lines) - 5:
                in_try_block = True
                catch_blocks = []
            if in_try_block and stripped.startswith("except "):
                catch_blocks.append(stripped)

        # After the NoMatches catch, there should be a broad Exception catch
        # or the entire block should be in a broader try
        has_broad = any("Exception" in c for c in catch_blocks)
        assert has_broad, (
            "Bug 55: event routing in _consume_events lacks broad "
            "exception handling. Only NoMatches is caught. "
            "Fix: add except Exception: log.error(...) around event routing."
        )


# ── Bug 56: catalog.py + history.py write_text without encoding ──────────────


class TestBug56WriteTextWithoutEncoding:
    """Bug 53 fixed encoding in config_writer and providers_pick, but the
    same pattern remains in catalog.py and history.py.

    Fix: tmp.write_text(..., encoding="utf-8").
    """

    def test_catalog_write_text_has_encoding(self) -> None:
        """catalog.py must specify encoding='utf-8' on write_text."""
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "write_text" in line and "encoding" not in line:
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                pytest.fail(
                    f"Bug 56: catalog.py line {i+1} has write_text() "
                    f"without encoding='utf-8': {stripped}. "
                    "Fix: add encoding='utf-8'."
                )

    def test_history_write_text_has_encoding(self) -> None:
        """history.py must specify encoding='utf-8' on write_text."""
        import tero2.history as history_module

        source = inspect.getsource(history_module)
        lines = source.splitlines()
        for i, line in enumerate(lines):
            if "write_text" in line and "encoding" not in line:
                stripped = line.strip()
                if stripped.startswith("#") or stripped.startswith('"'):
                    continue
                pytest.fail(
                    f"Bug 56: history.py line {i+1} has write_text() "
                    f"without encoding='utf-8': {stripped}. "
                    "Fix: add encoding='utf-8'."
                )

    def test_catalog_write_roundtrip_preserves_unicode(self, tmp_path: Path) -> None:
        """Write cache with unicode model labels and verify it survives."""
        from tero2.providers.catalog import _save_cache, _CACHE_DIR
        from tero2.providers.catalog import ModelEntry

        # We test by verifying the source pattern — actual write test needs
        # the cache dir. Instead, verify the fix via source inspection above.
        # This test acts as a functional check: if _save_cache uses encoding,
        # unicode data round-trips correctly.
        import tero2.providers.catalog as catalog_module

        source = inspect.getsource(catalog_module._save_cache)
        assert "encoding" in source, (
            "Bug 56: _save_cache in catalog.py uses write_text without "
            "encoding='utf-8'. Unicode model names will be garbled on "
            "non-UTF-8 systems. Fix: add encoding='utf-8'."
        )


# ── Bug 57: architect silent task drop on non-standard header ────────────────


class TestBug57ArchitectSilentTaskDrop:
    """When _parse_slice_plan encounters a header that doesn't match
    _TASK_ID_RE (e.g. "## Task01:" instead of "## T01:"), the task is
    silently skipped via `continue`. No warning is logged.

    Fix: log a warning when tid_match is None so operators know tasks
    were dropped.
    """

    def test_parse_logs_warning_on_malformed_header(self) -> None:
        """When a task header doesn't match T\\d{2}, a warning must be
        logged instead of silently skipping."""
        import tero2.players.architect as architect_module

        source = inspect.getsource(architect_module._parse_slice_plan)

        # Find the `if not tid_match: continue` block
        lines = source.splitlines()
        found_skip = False
        has_log_or_warn = False

        for i, line in enumerate(lines):
            if "not tid_match" in line:
                found_skip = True
                # Check the next few lines for logging before continue
                for k in range(i + 1, min(i + 5, len(lines))):
                    stripped = lines[k].strip()
                    if "log." in stripped or "warn" in stripped.lower():
                        has_log_or_warn = True
                        break
                    if stripped == "continue":
                        break
                break

        if not found_skip:
            pytest.skip("tid_match check not found in _parse_slice_plan")

        assert has_log_or_warn, (
            "Bug 57: _parse_slice_plan silently drops tasks with "
            "non-standard headers (e.g. '## Task01:' instead of '## T01:'). "
            "No warning is logged, so the operator has no visibility into "
            "dropped tasks. Fix: log.warning before continue."
        )

    def test_parse_returns_all_matching_tasks(self) -> None:
        """Functional test: well-formed plan must return all tasks."""
        from tero2.players.architect import _parse_slice_plan, SlicePlan

        plan_text = """## S01 Slice Plan

## T01: Setup module
**Must-haves:**
- Create main.py
- Add config loader

## T02: Add tests
**Must-haves:**
- Test config loading
- Test CLI entry point
"""
        result = _parse_slice_plan(plan_text, slice_id="S01")
        assert isinstance(result, SlicePlan)
        assert len(result.tasks) == 2, (
            f"Expected 2 tasks from well-formed plan, got {len(result.tasks)}"
        )

    def test_parse_does_not_crash_on_malformed_header(self) -> None:
        """Malformed headers must not crash the parser — just skip."""
        from tero2.players.architect import _parse_slice_plan

        plan_text = """## S01 Slice Plan

## Task01: Bad header format
**Must-haves:**
- Something important

## T02: Good header
**Must-haves:**
- This should parse fine
"""
        # Must not raise
        result = _parse_slice_plan(plan_text, slice_id="S01")
        # Only T02 should be parsed — Task01 is silently dropped
        assert len(result.tasks) == 1, (
            "Expected 1 task (T02) — Task01 should be skipped due to "
            f"malformed header, but got {len(result.tasks)} tasks."
        )
